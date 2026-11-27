"""程序包 A：实施员一键初始化向导。

五个步骤，跑完就能把「程序包 B」交给院长双击使用：
    A1 扫描本机 QQ 目录，自动推荐主号（按 nt_msg.db 体积 + 最近活跃）
    A2 取 QQ 数据库密钥（唯一需要人工配合点一次登录的地方），自动回填 .env
    A3 探测 SQLCipher 参数并固化真实表结构到 config/schema.json
    A4 锁死 NTQQ 自动更新（保护密钥与表结构）
    A5 配置企业微信机器人 Webhook 并注册开机自启

用法:
    python tools/admin_init.py               # 交互式完整流程（建议由 Admin_Init.bat 提权后调用）
    python tools/admin_init.py --check       # 只体检不修改
    python tools/admin_init.py --key abcdefgh12345678 --webhook https://... --yes
    python tools/admin_init.py --skip-lock --skip-key   # 跳过指定步骤
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
import lock_qq_update  # noqa: E402
import probe  # noqa: E402
import push_wecom  # noqa: E402
import qqdb  # noqa: E402
import qqkey  # noqa: E402

KEY_RE = re.compile(r"(?<![0-9A-Za-z])[!-~]{16}(?![0-9A-Za-z])")
# QQ 默认把聊天记录放在「文档\Tencent Files」；但很多用户改成了各盘根目录。
# 少 C:\Tencent Files 会让默认装在系统盘的用户第一步就报“没找到任何带 nt_msg.db 的 QQ 账号目录”。
QQ_SEARCH_ROOTS = [
    Path(r"C:\Tencent Files"),
    Path(r"E:\Tencent Files"),
    Path(r"D:\Tencent Files"),
    Path(r"F:\Tencent Files"),
]
PROMPT_SRC = common.ROOT / "automation-prompt.md"
PROMPT_LOCAL = common.ROOT / "automation-prompt.local.md"
WECOM_OK_MD = """### ✅ 待办汇总助手 · 初始化成功
- **绑定 QQ 账号**：`{uin}`（解密校验通过，表结构已锁定）
- **QQ 防升级保护**：{lock}
- **定时任务状态**：已就绪，将按设定周期自动为您推送待办摘要"""


# 提示词本地化渲染
def render_prompt_local() -> Path | None:
    """把 automation-prompt.md 渲染成一份「带本机绝对路径」的提示词副本。

    为什么不在提示词里直接写死盘符：
        交付到别的电脑、或用户把包解压到 D:\\tools\\qq-todo 时，
        写死的 `E:\\外部协作引擎\\Workspaces\\...\\output\\` 会让定时任务永远找不到
        异常说明与素材，QQ 这一路静默断链。
    做法：模板只用相对路径（output/ work/ config/），本函数在初始化时把它们
        替换成运行机的真实绝对路径，落到 automation-prompt.local.md，
        供实施员直接复制去建定时任务；模板本身保持可移植。
    """
    if not PROMPT_SRC.exists():
        return None
    try:
        text = PROMPT_SRC.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    body = (
        text.replace("`output/`", f"`{common.OUT}`")
        .replace("`work/`", f"`{common.WORK}`")
        .replace("`config/`", f"`{common.CONFIG}`")
    )
    header = (
        f"<!-- 本文件由 Admin_Init 于 {datetime.now():%Y-%m-%d %H:%M:%S} 自动生成，"
        f"路径已换成本机绝对路径；程序目录 = {common.HOME} -->\n\n"
    )
    try:
        common.atomic_write_text(PROMPT_LOCAL, header + body)
    except OSError:
        return None
    return PROMPT_LOCAL


# 控制台小工具
def _enable_ansi() -> bool:
    try:
        import ctypes  # noqa: PLC0415
        k = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


ANSI = _enable_ansi()


def yellow(text: str) -> str:
    return f"\033[93m{text}\033[0m" if ANSI else f"*** {text} ***"


def green(text: str) -> str:
    return f"\033[92m{text}\033[0m" if ANSI else text


def red(text: str) -> str:
    return f"\033[91m{text}\033[0m" if ANSI else text


@dataclass
class Ctx:
    env: dict = field(default_factory=dict)
    uin: str = ""
    db_path: Path | None = None
    key: str = ""
    profile: str = ""
    schema_ok: bool = False
    lock_ok: bool = False
    lock_note: str = ""
    push_mode: str = "连接器"
    webhook_ok: bool = False
    autostart_ok: bool = False
    non_interactive: bool = False
    skip_key: bool = False
    skip_lock: bool = False
    skip_webhook: bool = False
    skip_autostart: bool = False


def ask(prompt: str, default: str = "") -> str:
    """非交互环境（后台/管道）下直接返回默认值，绝不卡住。"""
    if not sys.stdin.isatty():
        common.log(f"{prompt} {default}（非交互环境，采用默认）")
        return default
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        common.log("")
        return default
    return val or default


def confirm(prompt: str, default: bool = True) -> bool:
    if not sys.stdin.isatty():
        return default
    d = "Y/n" if default else "y/N"
    val = ask(f"{prompt} ({d})", "y" if default else "n").lower()
    return val.startswith("y")


# A1 扫描 QQ 账号
def detect_qq_accounts(extra_roots: list[Path] | None = None) -> list[dict]:
    """扫描常见 Tencent Files 目录，返回有真实 nt_msg.db 的账号列表（按活跃度排序）。"""
    roots = list(QQ_SEARCH_ROOTS)
    users = Path("C:/Users")
    if users.exists():
        for p in users.glob("*/Documents/Tencent Files"):
            roots.append(p)
    if extra_roots:
        roots.extend(extra_roots)

    found: list[dict] = []
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for d in entries:
            if not d.is_dir() or not d.name.isdigit():
                continue
            db = d / "nt_qq" / "nt_db" / "nt_msg.db"
            if not db.exists():
                continue
            if str(db).lower() in seen:
                continue
            seen.add(str(db).lower())
            try:
                st = db.stat()
            except OSError:
                continue
            if st.st_size < 4096:
                continue
            wal = Path(str(db) + "-wal")
            found.append({
                "uin": d.name,
                "db": db,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "live": wal.exists() and wal.stat().st_size > 0,
            })
    found.sort(key=lambda a: (a["mtime"], a["size"]), reverse=True)
    return found


def step_a1(ctx: Ctx, accounts: list[dict]) -> bool:
    common.log("\n[A1] 扫描本机 QQ 账号")
    if not accounts:
        common.log(red("  没找到任何带 nt_msg.db 的 QQ 账号目录。"))
        common.log("  请确认：① QQ 装在本机 ② 至少登录过一次 ③ 聊天记录存在本机")
        return False

    for i, a in enumerate(accounts, 1):
        flag = "运行中" if a["live"] else "未运行"
        common.log(
            f"  {i}. QQ {a['uin']}  {a['size'] / 1048576:.1f} MB  "
            f"最后写入 {common.fmt(a['mtime'])}（{common.human_age(time.time() - a['mtime'])}，{flag}）"
        )
        common.log(f"     路径 {a['db']}")

    default_idx = 1
    if len(accounts) == 1:
        pick = accounts[0]
        common.log(f"\n  只有一个候选账号，自动选中 {pick['uin']}")
    else:
        raw = ask(f"\n  请选择主号序号（默认 {default_idx}，体积最大且最近活跃）", str(default_idx))
        try:
            idx = int(raw)
        except ValueError:
            idx = default_idx
        idx = min(max(1, idx), len(accounts))
        pick = accounts[idx - 1]

    ctx.uin = pick["uin"]
    ctx.db_path = pick["db"]
    common.save_env({"QQ_TARGET_UIN": ctx.uin, "QQ_DB_PATH": str(pick["db"])})
    common.log(green(f"  [OK] 已锁定主号 {ctx.uin}"))
    common.log(f"       已写入 .env: QQ_TARGET_UIN / QQ_DB_PATH")
    return True


# A2 取密钥
def verify_key(db: Path, key: str, preferred: str | None = None) -> tuple[bool, str]:
    """真刀真枪解一次，解不开就不算数。返回 (是否可用, profile 或原因)。"""
    if not db.exists():
        return False, "库文件不存在"
    if not qqdb.key_is_plausible(key):
        return False, f"格式不对（{len(key)} 位，应为 16 位 ASCII）"
    tmp = common.TMP_DB
    shutil.rmtree(tmp, ignore_errors=True)
    con = None
    try:
        con, profile, _ = qqdb.open_encrypted(db, tmp, key, preferred)
        n = con.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
        return (True, profile) if n else (False, "库里没有表")
    except qqdb.DecryptError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"未知错误: {exc}"
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def _try_candidates(db: Path, candidates: list[str], preferred: str | None) -> tuple[str, str] | None:
    tried: set[str] = set()
    for cand in candidates:
        if cand in tried:
            continue
        tried.add(cand)
        ok, detail = verify_key(db, cand, preferred)
        if ok:
            return cand, detail
        common.log(f"      候选 {common.mask(cand)} 解不开，继续试下一个")
    return None


def step_a2(ctx: Ctx, force: bool = False) -> bool:
    common.log("\n[A2] 取 QQ 数据库密钥")
    assert ctx.db_path is not None
    env_key = (ctx.env.get("QQ_DBKEY", "") or "").strip()
    preferred = ctx.env.get("QQ_CIPHER_PROFILE") or None

    if env_key and not force:
        ok, detail = verify_key(ctx.db_path, env_key, preferred)
        if ok:
            ctx.key, ctx.profile = env_key, detail
            common.log(green(f"  [OK] .env 里已有可用密钥（参数 {detail}），跳过取密钥"))
            return True
        common.log(yellow(f"  [注意] .env 里的密钥解不开（{detail.splitlines()[0]}），需要重新获取"))

    if ctx.skip_key:
        common.log(yellow("  --skip-key 指定：跳过取密钥。没有密钥时 QQ 这一路为空，企业微信仍可正常工作。"))
        return False
    if ctx.non_interactive:
        common.log(yellow("  非交互环境，不能启动取密钥流程（会重启 QQ 并要求点登录）。"))
        return False

    # ---- 抓取 + 校验：交给 qqkey.extract_verified_key（与控制台按钮同一条链路）----
    # 旧版这里自己又写了一遍 定位/关 QQ/跑脚本/解析候选/逐个试解，
    # 与 qqkey.extract_and_save 的实现细节渐渐走偏（那边会兜底写未验证密钥）。
    # 现在只有一份实现，向导和控制台不可能再出现"一个入口能抓、另一个不行"。
    print()
    print(yellow("【人工配合 30 秒】接下来会关掉当前 QQ，并重新启动它并挂上调试器。"))
    print(yellow("请在随后弹出的 QQ 登录框点“登录”（或扫码）。登录成功后会自动读密钥，无需其他操作。"))
    print(yellow("说明：先静态分析 wrapper.node，约 1 分钟；分析完才会拉起 QQ，请耐心等。"))
    print()

    common.log("  正在定位 QQ 并抓取密钥（这一步最久，属正常）...")
    key, profile, detail = qqkey.extract_verified_key(ctx.db_path)
    if key:
        ctx.key, ctx.profile = key, profile
        common.save_env({"QQ_DBKEY": key, "QQ_CIPHER_PROFILE": profile})
        common.log(green(f"  [OK] {detail}，已写入 .env（{common.mask(key)}）"))
        return True

    common.log(yellow(f"  [注意] 没抓到可用密钥：{detail}"))

    common.log("\n  可以这样处理：")
    common.log("    1) 重跑本向导（保持网络正常，登录框出现后 15 秒内点登录）")
    common.log("    2) 若脚本报“找不到 wrapper.node / 不支持”，说明该 QQ 版本换了结构，请把日志发我")
    common.log("    3) 只想先用起来：跳过 QQ，只用企业微信（本向导会自动降级）")

    for _ in range(3):
        pasted = ask("  也可以直接粘贴 16 位密钥（回车跳过）", "").strip().replace(" ", "")
        if not pasted:
            break
        m = KEY_RE.search(pasted)
        pasted = m.group(0) if m else pasted
        ok, detail = verify_key(ctx.db_path, pasted, preferred)
        if ok:
            ctx.key, ctx.profile = pasted, detail
            common.save_env({"QQ_DBKEY": pasted, "QQ_CIPHER_PROFILE": detail})
            common.log(green(f"  [OK] 密钥已写入 .env（{common.mask(pasted)}）"))
            return True
        common.log(red(f"  这个也解不开：{str(detail).splitlines()[0]}"))

    common.log(red("  [未完成] QQ 这一路暂时不可用；企业微信那一路不受影响。"))
    return False


# A3 探测并固化 schema


# A2-b 个人微信 4.x 数据目录检测与密钥引导
def step_wechat(ctx: Ctx) -> bool:
    common.log(""); common.log("[A2-b] 个人微信 (4.x) 状态检测与密钥引导")
    env = common.load_env()
    key = (env.get("WX_DBKEY") or "").strip()
    from channels import wechat
    source = wechat.find_source_storage((env.get("WX_DATA_PATH") or "").strip())
    if not source:
        common.log("  未检测到电脑版微信 4.x 数据目录（xwechat_files），若不使用微信可跳过。")
        return True

    common.log(f"  已检测到微信数据目录: {source}")
    if key and len(key) == 64:
        common.log(green(f"  [OK] 微信 64 位密钥已配置（{key[:4]}****{key[-4:]}）"))
        mirror = wechat.find_mirror()
        if not mirror:
            common.log("  正在执行首次明文镜像解密...")
            ok, detail = wechat._try_decrypt(None, key, source)
            common.log(green(f"  [OK] 明文镜像: {detail}") if ok else yellow(f"  [注意] 解密未完成: {detail}"))
        return True

    from health import handle_wechat_key_sync
    res = handle_wechat_key_sync()
    if res.get("ok"):
        common.log(green(f"  [OK] 自动从本地文件识别并同步微信密钥: {res.get('message')}"))
        return True

    common.log("  微信 64 位密钥尚未就绪。")
    common.log("  说明：抓取微信密钥通过 Windows 原生内存只读扫描自动捕获，无需关闭微信，保持微信正常登录即可。")
    if ctx.non_interactive:
        return False

    if confirm("  是否现在启动微信取密钥向导？", default=True):
        if os.name == "nt":
            cands = [
                common.HOME / "WeChat_Key.bat",
                common.HOME / "vendor" / "2-get-wechat-key.bat",
                common.HOME / "02-启动与安装脚本" / "WeChat_Key.bat",
                common.HOME / "02-启动与安装脚本" / "vendor" / "2-get-wechat-key.bat",
            ]
            bat = next((c for c in cands if c.exists()), None)
            if bat:
                common.log(f"  已启动 {bat.name}，请按弹窗提示操作...")
                subprocess.Popen(["cmd.exe", "/c", "start", "微信取密钥", str(bat)])
                return True
            else:
                py_exe = sys.executable
                subprocess.Popen([
                    "cmd.exe", "/c", "start", "微信4.x取密钥向导",
                    str(py_exe), str(common.HOME / "tools" / "wxkey.py"), "acquire",
                ])
                return True
        common.log("  请在安装目录下运行 WeChat_Key.bat。")

    pasted = ask("  也可以直接粘贴微信 64 位十六进制密钥（回车跳过）", "").strip().replace(" ", "")
    if pasted and len(pasted) == 64:
        common.save_env({"WX_DBKEY": pasted})
        os.environ["WX_DBKEY"] = pasted
        common.log(green(f"  [OK] 微信密钥已写入 .env（{pasted[:4]}****{pasted[-4:]}）"))
        common.log("  正在执行首次明文镜像解密...")
        ok, detail = wechat._try_decrypt(None, pasted, source)
        common.log(green(f"  [OK] 明文镜像: {detail}") if ok else yellow(f"  [注意] 解密未完成: {detail}"))
        return True
    return False

def step_a3(ctx: Ctx) -> bool:
    common.log("\n[A3] 探测解密参数与真实表结构")
    if not ctx.key:
        common.log(red("  没有可用密钥，无法探测表结构（禁止盲猜列名）。"))
        return False
    assert ctx.db_path is not None

    def _remember(profile: str) -> None:
        ctx.profile = profile
        common.save_env({"QQ_CIPHER_PROFILE": profile})

    try:
        schema = probe.verify_decrypt_and_dump_schema(
            ctx.db_path,
            ctx.key,
            ctx.profile or ctx.env.get("QQ_CIPHER_PROFILE") or None,
            save_profile_cb=_remember,
        )
    except qqdb.DecryptError as exc:
        common.log(red(f"  [失败] {exc}"))
        return False
    finally:
        shutil.rmtree(common.TMP_DB, ignore_errors=True)

    tables = schema.get("tables") or []
    if not tables:
        common.log(red("  [失败] 没有识别出可用消息表，抽取会拒绝运行。"))
        common.log(f"         请把 {common.WORK / 'schema.txt'} 交给开发者看样本行。")
        return False

    ctx.schema_ok = True
    common.log(green(f"  [OK] 表结构已固化到 {common.SCHEMA_PATH}（{len(tables)} 张消息表）"))
    for t in tables:
        cols = ", ".join(f"{k}={v}" for k, v in t["columns"].items() if v)
        common.log(f"       - {t['name']}（{t['rows']} 行，{t['chat_type']}）：{cols}")
    if schema.get("group_names", {}).get("count"):
        common.log(f"       顺带取到 {schema['group_names']['count']} 个群名，清单里会显示群名而不是群号")
    return True


# A4 锁死自动更新
def step_a4(ctx: Ctx) -> bool:
    if ctx.skip_lock:
        common.log("\n[A4] 已跳过（--skip-lock）")
        ctx.lock_note = "已跳过"
        return False
    ok, detail = lock_qq_update.lock()
    ctx.lock_ok = ok
    ctx.lock_note = detail if not ok else "已锁死"
    if not ok and "未找到" in (detail or ""):
        common.log(yellow("  这台机器的 QQ 没有独立的升级程序（升级在主程序内完成），"))
        common.log(yellow("  三步免疫法没有落点。这不影响采集，但请在 1~2 个月内复查一次密钥是否仍有效。"))
    return ok


# A5 推送通道 + 开机自启
def step_a5(ctx: Ctx) -> bool:
    """推送默认走 外部协作引擎 内置的企业微信连接器，不需要任何地址。

    连接器是 外部协作引擎 进程内的能力（MCP 工具），独立的 Python 程序调不到它；
    所以真正的推送动作由「外部协作引擎 定时任务」完成（详见 automation-prompt.md）。
    群机器人 Webhook 只是“程序自己也能推”的兜底通道，属于可选项。
    """
    common.log("\n[A5] 推送通道与开机自启")
    common.log("  [推送] 使用 WorkBuddy 内置「企业微信」连接器，无需填写任何地址或密钥")
    common.log("         定时任务跑起来后，摘要会直接发到您的企业微信（连接器已授权即可）")
    prompt_copy = render_prompt_local()
    if prompt_copy:
        common.log(f"  [提示词] 已按本机路径渲染：{prompt_copy.name}（建定时任务时优先用这份）")

    url = (ctx.env.get("WECOM_WEBHOOK_URL", "") or "").strip()
    ctx.push_mode = "连接器"

    if ctx.skip_webhook:
        common.log("  已跳过群机器人兜底（--skip-webhook）")
    elif url:
        common.log(f"  检测到已配置群机器人兜底（{url[:44]}...）")
        if confirm("  发一条测试消息确认它还活着吗？", True):
            sent, msg = push_wecom.push(
                WECOM_OK_MD.format(
                    uin=ctx.uin or "（未锁定）",
                    lock="已开启（版本已固化）" if ctx.lock_ok else "未开启",
                ),
                url,
            )
            if sent:
                ctx.push_mode = "连接器+群机器人"
                common.log(green("  [OK] 群机器人兜底可用"))
            else:
                common.log(red(f"  [注意] 群机器人发送失败：{msg}"))
                common.log("         不影响主通道，摘要仍由连接器发出；需要的话删掉 .env 里的 WECOM_WEBHOOK_URL 即可。")
    else:
        common.log("  群机器人 Webhook 未配置 —— 不影响交付，可以完全不管。")
        common.log("  只有在“WorkBuddy 没开也要把摘要推到手机”时才需要它。")
        if confirm("  现在就配一条兜底通道吗？", False):
            entered = ask("  粘贴群机器人 Webhook 地址（回车跳过）", "").strip()
            if entered.startswith("http"):
                common.save_env({"WECOM_WEBHOOK_URL": entered})
                sent, msg = push_wecom.push(
                    WECOM_OK_MD.format(
                        uin=ctx.uin or "（未锁定）",
                        lock="已开启（版本已固化）" if ctx.lock_ok else "未开启",
                    ),
                    entered,
                )
                if sent:
                    ctx.push_mode = "连接器+群机器人"
                    common.log(green("  [OK] 兜底通道已配置并测试通过"))
                else:
                    common.log(red(f"  [注意] 测试发送失败：{msg}"))
            elif entered:
                common.log(yellow("  地址不像 Webhook（应以 http 开头），已跳过。"))

    ctx.webhook_ok = True  # 连接器即主通道，恒为可用

    ok_auto = False
    if ctx.skip_autostart:
        common.log("  已跳过开机自启（--skip-autostart）")
    else:
        try:
            import setup_autostart  # noqa: PLC0415
            setup_autostart.install_scheduler()
            ok_auto = True
            common.log(green("  [OK] 已注册常驻采集调度（登录后按 config\\schedule.json 自动采集）"))
            common.log("        计划: 每 60 分钟一轮（回看 6 小时）｜日志 output\\scheduler.log")
            common.log("        改计划: python tools\\scheduler.py --interval 30  或  --daily 08:30,17:30")
        except Exception as exc:  # noqa: BLE001
            common.log(red(f"  [失败] 注册采集调度失败：{exc}"))

    # 调度计划文件：缺了就写一份默认的，避免调度器起来后没计划可读
    try:
        sched_cfg = common.CONFIG / "schedule.json"
        if not sched_cfg.exists():
            import scheduler as _sched  # noqa: PLC0415
            _sched.save_config({})
            common.log(f"  [OK] 已生成默认调度计划: {sched_cfg}")
    except Exception as exc:  # noqa: BLE001
        common.log(yellow(f"  [!] 调度计划文件未生成：{exc}"))

    ctx.autostart_ok = ok_auto
    return ok_auto


# 体检 / 汇总
def run_check(ctx: Ctx) -> int:
    common.head("初始化状态体检（只读，不改任何东西）")
    accounts = detect_qq_accounts()
    common.log(f"\n发现 {len(accounts)} 个 QQ 账号:")
    for a in accounts:
        common.log(f"  - {a['uin']}  {a['size'] / 1048576:.1f} MB  {common.fmt(a['mtime'])}")
        common.log(f"    {a['db']}")

    env = ctx.env
    db = common.safe_path(env.get("QQ_DB_PATH", ""))
    key = (env.get("QQ_DBKEY", "") or "").strip()
    common.log("\n配置状态:")
    common.log(f"  QQ_TARGET_UIN      : {env.get('QQ_TARGET_UIN') or '(未设置)'}")
    db_text = env.get("QQ_DB_PATH") or "(未设置)"
    db_state = "存在" if db_text != "(未设置)" and Path(db_text).exists() else "不存在"
    common.log(f"  QQ_DB_PATH         : {db_text}  {db_state}")
    common.log(f"  QQ_DBKEY           : {common.mask(key) if key else '(未设置)'}")
    common.log(f"  QQ_CIPHER_PROFILE  : {env.get('QQ_CIPHER_PROFILE') or '(未探测)'}")
    common.log(f"  WECOM_WEBHOOK_URL  : {'已设置' if env.get('WECOM_WEBHOOK_URL') else '(未设置)'}")
    common.log(f"  config/schema.json : {'已生成' if common.SCHEMA_PATH.exists() else '(未生成)'}")

    if key and db is not None and db.exists():
        ok, detail = verify_key(db, key, env.get("QQ_CIPHER_PROFILE") or None)
        common.log(f"\n密钥实测: {'可用' if ok else '不可用'}  {detail if not ok else f'参数 {detail}'}")
    else:
        common.log("\n密钥实测: 跳过（缺密钥或缺库路径）")

    info = qqkey.find_qq()
    common.log("\nQQ 客户端定位:")
    if info:
        common.log(f"  安装目录 : {info['install_dir']}（{info['source']}）")
        common.log(f"  QQ.exe   : {info['qq_exe']}")
        common.log(f"  wrapper  : {info['wrapper_node']}")
        common.log(f"  版本目录 : {info['version'] or '未知'}")
    else:
        common.log("  [异常] 没找到 QQ.exe / wrapper.node，取密钥会失败")

    exes, _ = lock_qq_update.find_targets()
    locked = [e for e in exes if e.is_dir()]
    common.log(f"QQ 防升级: 发现 {len(exes)} 个更新组件，已锁死 {len(locked)} 个")
    return 0


def sync_to_user_package(ctx: Ctx) -> None:
    """将初始化成果（.env, schema.json, active_platforms.json）自动同步到「2-用户日常全自动运行包」。"""
    prog_dir = common.ROOT.parent / "2-用户日常全自动运行包"
    if not prog_dir.is_dir():
        return

    common.log("")
    common.log("[同步] 正在将初始化配置同步至「2-用户日常全自动运行包」...")

    # 1. 同步 .env
    prog_env = prog_dir / ".env"
    env_keys = {}
    if ctx.uin: env_keys["QQ_TARGET_UIN"] = ctx.uin
    if ctx.db_path: env_keys["QQ_DB_PATH"] = str(ctx.db_path)
    if ctx.key: env_keys["QQ_DBKEY"] = ctx.key
    if ctx.profile: env_keys["QQ_CIPHER_PROFILE"] = ctx.profile

    cur_env = common.load_env()
    for k in ["WX_DBKEY", "WX_DATA_PATH", "WECOM_WEBHOOK_URL", "FEISHU_APP_ID", "FEISHU_APP_SECRET", "DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"]:
        if cur_env.get(k):
            env_keys[k] = cur_env[k]

    if env_keys:
        try:
            common.save_env(env_keys, prog_env)
            common.log("  [✓] 密钥与参数已自动同步至 2-用户日常全自动运行包/.env")
        except Exception as exc:
            common.log(f"  [!] 同步 .env 异常: {exc}")

    # 2. 同步 schema.json
    schema_src = common.CONFIG / "schema.json"
    schema_dst = prog_dir / "config" / "schema.json"
    if schema_src.exists():
        try:
            schema_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(schema_src, schema_dst)
            common.log("  [✓] 数据库表结构 (schema.json) 已同步至日常运行包")
        except Exception as exc:
            common.log(f"  [!] 同步 schema.json 异常: {exc}")

    # 3. 确保 active_platforms.json 存在
    active_cfg = prog_dir / "config" / "active_platforms.json"
    if not active_cfg.exists():
        try:
            active_cfg.parent.mkdir(parents=True, exist_ok=True)
            active_cfg.write_text(json.dumps({"config_mode": "custom", "enabled_platforms": ["QQ"]}, ensure_ascii=False), encoding="utf-8")
            common.log("  [✓] 已在日常运行包中激活默认配置 (active_platforms.json: QQ)")
        except Exception as exc:
            common.log(f"  [!] 写入 active_platforms.json 异常: {exc}")

def summary(ctx: Ctx) -> None:
    common.head("初始化结果")
    rows = [
        ("绑定 QQ 账号", bool(ctx.uin), f"{ctx.uin}（{ctx.db_path}）" if ctx.uin else "未完成"),
        (
            "数据库密钥",
            bool(ctx.key),
            f"可用（{common.mask(ctx.key)}，参数 {ctx.profile or '未知'}）" if ctx.key else "不可用",
        ),
        ("表结构固化", ctx.schema_ok, "已完成" if ctx.schema_ok else "未完成"),
        ("QQ 防升级保护", ctx.lock_ok,
         "已锁死" if ctx.lock_ok else (ctx.lock_note or "未完成")),
        ("企业微信推送", True, ctx.push_mode),
        ("采集调度", ctx.autostart_ok, "已注册常驻调度（登录后自动采集）" if ctx.autostart_ok else "未注册"),
    ]
    for name, ok, val in rows:
        mark = green("✔") if ok else red("✘")
        common.log(f"  {mark} {name:<14}: {val}")

    ready = ctx.key and ctx.schema_ok
    common.log("")
    if ready:
        common.log(green("  程序包 A 完成。接下来把「2-用户日常全自动运行包」整个文件夹交给使用者："))
        common.log("  · 双击 Start.bat 即可试跑一次")
        common.log("  · 常驻调度器登录后自动按计划采集（config\\schedule.json 可改）")
        common.log("  · WorkBuddy 定时任务用 automation-prompt.local.md 的提示词（已含本机绝对路径）")
        if not ctx.lock_ok:
            common.log(yellow("  提示：防升级未锁死，QQ 大版本升级后可能需要重跑本向导。"))
    else:
        common.log(yellow("  QQ 这一路还没通，但企业微信这一路是独立的，可以先用起来。"))
        if not ctx.schema_ok:
            common.log("  · 缺表结构/密钥时，run.py 会写「异常说明-密钥未就绪」并按规范拒绝产出假清单")
            common.log("  · 定时任务读到异常说明会在摘要首行报警，企微数据照常汇总")
        common.log("  想补齐 QQ：重跑本向导，登录框出现后请及时点“登录”。")


def main() -> int:
    ap = argparse.ArgumentParser(description="实施员一键初始化向导")
    ap.add_argument("--check", action="store_true", help="只体检，不改任何东西")
    ap.add_argument("--key", help="直接给 16 位密钥，跳过取密钥流程")
    ap.add_argument("--webhook", help="直接给企业微信 Webhook")
    ap.add_argument("--yes", action="store_true", help="全自动：所有询问取默认")
    ap.add_argument("--skip-key", action="store_true")
    ap.add_argument("--skip-lock", action="store_true")
    ap.add_argument("--skip-webhook", action="store_true")
    ap.add_argument("--skip-autostart", action="store_true")
    args = ap.parse_args()

    common.ensure_dirs()
    # 与其它入口共用同一套解释器裁决：缺 sqlcipher3 时先自动切到 .venv，
    # 否则向导跑到 A2 才会以「缺少 SQLCipher」失败，用户白等一轮。
    try:
        import runtime_env  # noqa: PLC0415

        guard = runtime_env.ensure_capable(verbose=True)
        if not guard["ok"]:
            common.log(yellow(f"  运行环境提示: {guard['reason']}"))
    except Exception as exc:  # noqa: BLE001
        common.log(yellow(f"  运行环境自检跳过: {exc}"))

    ctx = Ctx(env=common.load_env())
    if args.yes:
        ctx.non_interactive = True
    ctx.skip_key = args.skip_key
    ctx.skip_lock = args.skip_lock
    ctx.skip_webhook = args.skip_webhook
    ctx.skip_autostart = args.skip_autostart
    if args.key:
        ctx.key = args.key.strip()
    if args.webhook:
        ctx.env["WECOM_WEBHOOK_URL"] = args.webhook

    if args.check:
        return run_check(ctx)

    common.set_title("待办汇总助手 - 实施员一键初始化（仅首次运行）")
    common.head("待办汇总助手 · 实施员一键初始化")
    common.log(f"  工作目录: {common.ROOT}")
    common.log(f"  时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    common.log("  流程: A1 选号 → A2 取密钥 → A3 探表结构 → A4 锁更新 → A5 推送与自启")

    # 依赖自检（L1：缺什么直接明确记录，不静默）
    try:
        import sqlcipher3  # noqa: F401,PLC0415
    except ImportError:
        common.log(red("\n  缺少 sqlcipher3-wheels，先执行："))
        common.log("    .venv\\Scripts\\python.exe -m pip install sqlcipher3-wheels pycryptodome")
        return 2

    accounts = detect_qq_accounts()
    if not step_a1(ctx, accounts):
        summary(ctx)
        return 3

    if ctx.key and not args.skip_key:
        ok, detail = verify_key(ctx.db_path, ctx.key)  # 命令行直接给的密钥
        if ok:
            ctx.profile = detail
            common.save_env({"QQ_DBKEY": ctx.key, "QQ_CIPHER_PROFILE": detail})
            common.log(green(f"\n[A2] 命令行给的密钥可用（{common.mask(ctx.key)}，参数 {detail}），已写入 .env"))
        else:
            common.log(red(f"\n[A2] 命令行给的密钥解不开：{detail.splitlines()[0]}"))
            ctx.key = ""
    if not ctx.key:
        step_a2(ctx)

    step_a3(ctx)
    step_a4(ctx)
    step_a5(ctx)
    sync_to_user_package(ctx)
    summary(ctx)
    return 0 if (ctx.key and ctx.schema_ok) else 4


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        common.log("\n已中断。已写入的配置会保留，可重新运行继续。")
        sys.exit(130)
