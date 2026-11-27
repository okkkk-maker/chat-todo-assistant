"""待办自动汇总助手 · 用户端入口（双击 Start.bat 或开机自启运行）。

主要执行流程：
    [1/4] 检查 QQ 与企业微信运行状态
    [2/4] 只读方式读取本地增量消息
    [3/4] 生成待办素材，唤醒 外部协作引擎 提炼
    [4/4] 提示去企业微信看待办清单

核心数据约束：
    1. 不臆造：素材里每条消息都带 (来源, 会话, 发送人, 时间, msg_id)。
    2. 不静默：任何失败都写 output\\异常说明-*.txt 并在窗口里明确记录。
    3. 没密钥就明说：不输出一份“看起来正常”的空清单。

用法:
    python run.py              # 正常跑，跑完拉起 外部协作引擎
    python run.py --no-open    # 只准备素材（定时任务/后台用）
    python run.py --hours 6    # 手动指定回看小时数
退出码: 0 正常 / 3 失败（已写异常说明）/ 4 密钥未就绪 / 75 另一实例正在运行（本轮跳过）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tools"))

import common  # noqa: E402
import extract_impl  # noqa: E402


# 单实例互斥锁
class SingleInstanceLock:
    """跨进程互斥：同一时刻只允许一个采集实例动 work/tmp_db 与素材。

    设计说明：
        开机自启（后台）与用户双击 Start.bat 很容易撞在同一分钟。两个进程
        都会 shutil.rmtree(work/tmp_db) 再重新拷库解密 —— 后启动的那个把
        前一个正在读的副本删掉，前一个立刻抛错并误发「QQ抽取异常」告警。
    实现：Windows 用 msvcrt.locking 锁文件首字节（非阻塞），进程退出时由
        操作系统自动释放，不会因为崩溃留下"死锁文件"。
    """

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self.fp = None

    def acquire(self) -> bool:
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            self.fp = open(self.lock_path, "a+b")
            # 空文件锁 0 字节区域在部分系统上无意义，先确保至少有 1 字节
            if self.lock_path.stat().st_size == 0:
                self.fp.write(b"\0")
                self.fp.flush()
            self.fp.seek(0)
            if os.name == "nt":
                import msvcrt  # noqa: PLC0415

                msvcrt.locking(self.fp.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # 非 Windows 仅用于自测，用 fcntl 等价实现
                import fcntl  # noqa: PLC0415

                fcntl.flock(self.fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, ImportError):
            self.release()
            return False

    def release(self) -> None:
        if not self.fp:
            return
        try:
            self.fp.seek(0)
            if os.name == "nt":
                import msvcrt  # noqa: PLC0415

                msvcrt.locking(self.fp.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl  # noqa: PLC0415

                fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
        except (OSError, ImportError):
            pass
        finally:
            try:
                self.fp.close()
            except OSError:
                pass
            self.fp = None


LOCK_FILE = common.WORK / ".process.lock"


def step(n: int, text: str) -> None:
    print(f"[{n}/4] {text}", flush=True)


TITLE = "待办自动汇总助手 - 正在运行中（请勿关闭，数秒后自动完成）"


def set_title(text: str = TITLE) -> None:
    """用 Unicode API 设标题，避免 bat 里中文在 GBK 控制台下变乱码。"""
    try:
        import ctypes  # noqa: PLC0415
        ctypes.windll.kernel32.SetConsoleTitleW(text)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def detail(text: str) -> None:
    print(f"      {text}", flush=True)


def banner() -> None:
    try:
        import version as _ver  # noqa: PLC0415
        tag = f" v{_ver.__version__}"
    except Exception:  # noqa: BLE001
        tag = ""
    print("=" * 66)
    print(f"        待办自动汇总助手{tag} · 正在为您整理消息")
    print(f"        {common.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 66)


def launch_web_console() -> None:
    """交给服务端绑定实际端口后唤起前端，禁止批处理盲开固定端口。"""
    try:
        subprocess.Popen([sys.executable, str(ROOT / "server.py")], cwd=str(ROOT))
    except OSError as exc:
        detail(f"[注意] 无法启动可视化控制台: {exc}")


def check_state() -> dict:
    """第 1 步：环境与数据源状态检查（只读，动态适配已激活平台）。"""
    env = common.load_env()
    import channel_collector
    enabled = channel_collector.get_enabled_platforms()
    cfg_file = channel_collector.ACTIVE_PLATFORMS_FILE
    has_mode = False
    if cfg_file.exists():
        try:
            has_mode = bool(json.loads(cfg_file.read_text(encoding="utf-8")).get("config_mode"))
        except Exception:
            pass

    info = {
        "env_ok": common.ENV_PATH.exists(),
        "has_mode": has_mode,
        "enabled": enabled,
    }

    if not has_mode:
        detail("⚪ 尚未选择多平台配置模式（可在可视化控制台 http://localhost:8000 中选择与接入）")
        detail("   支持平台：腾讯QQ、个人微信、飞书、钉钉、企业微信")
        return info

    detail(f"📋 已激活平台: {'、'.join(sorted(enabled)) if enabled else '未勾选任何平台'}")

    # 1. QQ 检查
    if "QQ" in enabled:
        # safe_path 把空值变成 None：直接 Path("") 会等于 "."，exists() 恒真，
        # 于是"没配路径"会被显示成"QQ 在线、消息库刚刚更新"。
        db_path = common.safe_path(env.get("QQ_DB_PATH", ""))
        key = (env.get("QQ_DBKEY", "") or "").strip()
        if db_path is not None and db_path.exists():
            f = extract_impl.freshness(db_path)
            if f["level"] == 0:
                detail(f"🟢 [QQ] 电脑 QQ 在线，消息库 {common.human_age(f['age'] or 0)}更新")
            else:
                detail(f"🟡 [QQ] 电脑 QQ 可能已退出或换号（{f['reason']}）")
        else:
            detail("🔴 [QQ] 未配置或找不到消息库 (QQ_DB_PATH)")
        if key:
            detail("🟢 [QQ] 数据库密钥已就绪")
        else:
            detail("🔴 [QQ] 尚未配置 QQ 密钥（可运行 Admin_Init.bat 或在控制台填入）")

    # 2. 微信检查
    if "WeChat" in enabled:
        wx_key = (env.get("WX_DBKEY") or "").strip()
        if wx_key:
            detail("🟢 [微信] WX_DBKEY 密钥已就绪")
        else:
            detail("🟡 [微信] 尚未配置 WX_DBKEY（可通过 vendor/2-get-wechat-key 提取并在控制台填入）")

    # 3. 飞书检查
    if "Feishu" in enabled:
        fs_id = (env.get("FEISHU_APP_ID") or "").strip()
        fs_sec = (env.get("FEISHU_APP_SECRET") or "").strip()
        if fs_id and fs_sec:
            detail("🟢 [飞书] FEISHU_APP_ID / SECRET 凭据已就绪")
        else:
            detail("🟡 [飞书] 尚未配置 App ID 或 Secret（请在控制台填入并把应用加入群聊）")

    # 4. 钉钉检查
    if "DingTalk" in enabled:
        dt_inbox = (common.WORK / "inbox" / "dingtalk.jsonl").exists()
        dt_id = (env.get("DINGTALK_CLIENT_ID") or "").strip()
        if dt_inbox:
            detail("🟢 [钉钉] 收件箱已有消息缓冲 (work/inbox/dingtalk.jsonl)")
        elif dt_id:
            detail("🟢 [钉钉] 凭据已配置 (需会话存档/回调写入收件箱)")
        else:
            detail("ℹ️ [钉钉] 收件箱模式：由钉钉会话存档或回调写入 work/inbox/dingtalk.jsonl")

    # 5. 企业微信检查
    if "WeCom" in enabled:
        hook = (env.get("WECOM_WEBHOOK_URL") or "").strip()
        wecom_inbox = (common.WORK / "inbox" / "wecom.jsonl").exists()
        if hook or wecom_inbox:
            detail("🟢 [企业微信] Webhook 机器人或连接器收件箱已就绪")
        else:
            detail("ℹ️ [企业微信] 收件箱/机器人模式：由 WorkBuddy 连接器同步或通过群机器人推送")

    return info


def prepare_cursor_note() -> None:
    cur = common.read_cursor()
    if cur:
        detail(f"上次成功采集: {common.fmt(cur)}")
    else:
        detail("首次运行，将按默认回看窗口采集")


def advance_cursor(ts: float) -> float:
    """把游标推进到「ts 的下一秒」，但绝不越过现在。"""
    return common.advance_cursor(ts)


WB_URI = "workbuddy://"


def find_workbuddy_exe() -> Path | None:
    """兼容原入口；实际查找逻辑与 Web 控制台共用。"""
    return common.find_workbuddy_exe()


def launch_workbuddy() -> bool:
    """唤醒 外部协作引擎：优先尝试协议，其次全盘动态定位启动。"""
    try:
        os.startfile(WB_URI)  # type: ignore[attr-defined]
        detail("🟢 已通过 workbuddy:// 协议拉起 WorkBuddy")
        return True
    except (OSError, AttributeError):
        pass

    exe = find_workbuddy_exe()
    if exe and exe.exists():
        try:
            subprocess.Popen([str(exe)], close_fds=True)
            detail(f"🟢 已直接启动 WorkBuddy ({exe})")
            return True
        except OSError as exc:
            detail(f"[注意] 启动 WorkBuddy 失败: {exc}")
            return False

    detail("🟡 未能在本机常见路径自动定位到 WorkBuddy.exe")
    detail("   若您已安装 WorkBuddy，可直接在桌面或开始菜单中打开它；")
    detail("   素材已安全落盘至 work/待办素材.json，WorkBuddy 打开后可直接使用。")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="待办自动汇总助手")
    ap.add_argument("--no-open", action="store_true", help="不拉起 WorkBuddy（后台/定时用）")
    ap.add_argument("--hours", type=float, default=None, help="回看小时数，默认 4 或按上次游标")
    ap.add_argument("--force-open", action="store_true", help="即使采集失败也拉起 WorkBuddy")
    ap.add_argument("--ignore-cursor", action="store_true", help="强制按指定回看小时数采集，忽略当前已推进的游标（适合排查与历史补采）")
    args = ap.parse_args()

    # 单实例保护：开机自启与手动双击撞车时，后一个实例直接优雅退出，
    # 绝不并发清空 work/tmp_db —— 那是「QQ抽取异常」误报的主要来源。
    inst_lock = SingleInstanceLock(LOCK_FILE)
    if not inst_lock.acquire():
        print("[注意] 另一个待办汇总实例正在运行中（采集或推送尚未结束），本轮自动跳过。")
        # 用 75（EX_TEMPFAIL）而不是 0：否则 scheduler 会把"没采到任何东西"
        # 记成 rc=0 成功，监控台显示"刚跑过、成功"，与事实相反。
        return 75
    try:
        return _run(args)
    finally:
        inst_lock.release()


def _run(args) -> int:
    """真正的四步流程（调用前已确保本进程是唯一实例）。"""
    banner()
    set_title()
    started = time.time()
    should_open = True   # 本轮是否值得拉起 外部协作引擎（没采到东西就别打扰人）

    # ---------- 1/4 ----------
    step(1, "正在检查多平台服务与运行状态...")
    state = check_state()
    prepare_cursor_note()

    # 老 .venv 里可能缺 zstandard（微信正文有 zstd 压缩段），开头补一次。
    # 已装时只是一次 import，几乎无耗时；缺 wheel 时安静跳过，不影响任何通道。
    try:
        import bootstrap_deps
        if bootstrap_deps.ensure_optional_silent():
            detail("已补齐可选组件 zstandard（微信压缩正文可解码）")
    except Exception:  # noqa: BLE001
        pass

    if not state["env_ok"]:
        detail("配置还没有初始化（缺少 .env），按“密钥未就绪”处理。")
        detail("请让维护人员运行一次「1-实施员一键初始化包」里的 Admin_Init.bat。")

    # ---------- 2/4 ----------
    step(2, "正在安全读取本地增量消息（只读模式，不影响聊天）...")
    try:
        # 只走聚合器，不再「失败就静默改跑 extract_impl」。
        # 旧写法的两个后果：① 一次失败会把整个库解密两遍（多花几十秒）；
        # ② 真正的异常被第二遍的「看起来成功」掩盖，故障无从追查。
        # 聚合器内部已按通道分别 try/except，能抛到这里说明是包缺失或代码级错误。
        import channel_collector
        res = channel_collector.run(hours=args.hours, ignore_cursor=getattr(args, "ignore_cursor", False))
    except Exception:  # noqa: BLE001
        # 兜底：连抽取器自己都崩了，也必须留下人话说明
        detail("抽取程序发生未预期错误，已记录详细日志。")
        path = common.write_exception(
            common.EXC_EXTRACT,
            "抽取程序内部错误，未能读取消息。",
            traceback.format_exc()[-1500:],
        )
        detail(f"异常说明: {path}")
        print("\n本次没有生成待办清单。以上是原因，不是“您没有待办”。")
        return 3

    if res.exception:
        detail(f"已记录异常说明: {res.exception}")

    # ---------- 3/4 ----------
    if not res.ok:
        step(3, "本次没能读取任何通道的消息，已生成异常说明（不会给您假清单）")
        detail(res.message)
        if res.status == "NOT_READY":
            pending = (res.meta or {}).get("not_ready_channels") or []
            detail("尚未接入的通道：" + ("、".join(pending) if pending else "全部勾选通道"))
            detail("处理办法：按 docs/全平台接入指南.md 配一次；只想先用起来就只勾 QQ/企微。")
        if getattr(res, "data_state", "") == "unavailable" and res.status != "NOT_READY":
            broken = (res.meta or {}).get("broken_channels") or []
            detail("采集失败的通道：" + ("、".join(broken) if broken else "全部勾选通道"))
            detail("游标保持不变：问题修好后，这段时间的消息会一次性补齐，不会被跳过。")
        broken = (res.meta or {}).get("broken_channels") or []
        for b in broken:
            if b == "Feishu":
                detail("【飞书接入】请在可视化控制台填入 FEISHU_APP_ID / SECRET，并将机器人加入目标群聊。")
            elif b == "WeChat":
                detail("【微信接入】请在可视化控制台填入 WX_DBKEY，并确认本机已安装微信 4.x 且登录。")
            elif b == "QQ":
                if res.status == "NO_KEY":
                    detail("【QQ接入】尚未配置 QQ 密钥，可运行 Admin_Init.bat 或在控制台填入。")
                else:
                    detail("【QQ接入】确认电脑 QQ 处于登录状态且消息库路径正确。")
            elif b in ("DingTalk", "WeCom"):
                detail(f"【{b}接入】收件箱为空，请启动 WorkBuddy 连接器或回调写入脚本。")
        if not broken:
            detail("处理办法：请打开 Web_Console.bat 检查各通道配置并保存。")
    else:
        state = getattr(res, "data_state", "ok")
        if state == "offline":
            step(3, "本地通道离线或覆盖不完整：本轮不能视为正常完成")
            detail(res.message)
            detail("处理办法：检查通道灯与异常说明，恢复登录/刷新镜像/修复查询后再采集。")
            if res.count:
                detail(f"（历史窗口内仍读到 {res.count} 条可参考资料，但不会触发正式推送）")
        elif state == "waiting_config":
            should_open = False
            step(3, "尚未选择配置模式，本轮没有采集任何通道")
            detail(res.message)
            detail("处理办法：已为您自动唤起可视化控制台，请在浏览器中选择模式并保存。")
            if not args.no_open:
                launch_web_console()
        elif state == "no_new":
            step(3, f"已激活通道在线，本时段确实没有新消息（共扫描 {res.raw_count} 条）")
            detail(res.message)
        elif state == "only_noise":
            if getattr(res, "deduped_count", 0):
                step(3, f"已激活通道在线，扫描 {res.raw_count} 条，"
                        f"其中 {res.deduped_count} 条与前几轮重复（已幂等去重）")
                detail("这不是故障：这些消息上一轮已经采集过并出过清单，本轮不重复投递。")
            else:
                step(3, f"已激活通道在线，扫描 {res.raw_count} 条，但都是表情/卡片/系统提示")
                detail("这不是故障：消息读到了，只是没有可提炼的待办内容。")
            detail(res.message)
        else:
            step(3, f"已提取 {res.count} 条有效消息素材，正在唤醒 WorkBuddy 智能提炼...")
            detail(f"素材文件: {common.MATERIAL_PATH}")
            detail(f"数据窗口: {res.meta.get('time_range', '')}")

        cursor_blocked = bool(getattr(res, "cursor_blocked", False) or state == "offline")
        if cursor_blocked:
            should_open = False
            # 任一启用通道离线、失败、未接入或截断：共享游标严格保持不变。
            detail("本轮覆盖不完整：游标保持不变，修复后将从原位置补录，不会跳过。")
        elif res.count and res.max_ts:
            # 游标 = 「已处理到这一秒」+1 秒，而非 time.time()。
            advance_cursor(res.max_ts)
        elif state in ("no_new", "only_noise"):
            # 通道在线且正常，本时段无待办：游标安全前移，无需回溯重复扫描
            moved = advance_cursor(res.max_ts or res.until or time.time())
            detail(f"游标已前移至 {common.fmt(moved)}（{state}，无需回溯重复扫描）")

    # 本地自动提炼并生成规范待办清单 (闭环输出 output/todo-*.md)
    try:
        import local_summarizer
        todo_p = local_summarizer.generate_todo_markdown()
        if todo_p:
            detail(f"🟢 本地已自动提炼并生成待办清单: {todo_p.name}")
    except Exception as exc:
        detail(f"ℹ️ 本地待办提炼跳过: {exc}")

    # ---------- 4/4 ----------
    step(4, "正在触发 WorkBuddy 汇总推送...")
    if args.no_open:
        if res.ok and should_open:
            detail("--no-open：后台静默模式，素材已就绪，交给 WorkBuddy 定时任务读取")
        else:
            detail("--no-open：后台静默模式，本轮未生成新素材，定时任务将读取状态说明")
    else:
        if (res.ok and should_open) or args.force_open:
            launch_workbuddy()
            if res.ok and should_open:
                detail("稍后请在「企业微信 / WorkBuddy」查看您的待办清单")
            else:
                detail("WorkBuddy 已按 --force-open 启动；本轮采集未完整，请先查看异常说明")
        else:
            detail("本轮采集未完整或没有新素材，不自动唤醒 WorkBuddy，也不触发推送")

    print()
    print("-" * 66)
    if res.ok and should_open:
        print(f"  完成，用时 {time.time() - started:.1f} 秒。")
        print("  完整清单会保存到 output 目录，同时推送到企业微信。")
    elif res.ok:
        print("  本轮采集未完成或没有新素材，未触发清单推送。")
    else:
        print("  本次没有待办清单，原因见上（不是您没有待办）。")
    print("-" * 66)
    if res.ok and not getattr(res, "cursor_blocked", False):
        return 0
    return 4 if res.status == "NO_KEY" else 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(130)
