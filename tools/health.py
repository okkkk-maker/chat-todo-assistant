"""待办自动汇总助手 · 健康检查与多通道连通性诊断服务

模块结构：
1. 本地 QQ 抽取健康体检（check_source / check_key / check_schema / check_progress / main）
2. 多平台环境探测、凭据验证、微信内存抓取及收件箱消息导入（inspect_channels_environment 等）
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common
import extract_impl
import qqdb
import wxdecrypt
import wxkey
from channels import base, feishu, wechat

HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
ASCII16_RE = re.compile(r"^[!-~]{16}$")


def locate_qq_db(env: dict | None = None) -> tuple[str, str, bool]:
    """智能定位 QQ 数据库路径与 UIN。自动扫描多盘符、多用户 Documents/文档 目录与 OneDrive。"""
    env = env or common.load_env()
    uin = (env.get("QQ_TARGET_UIN") or "").strip()
    db_path = (env.get("QQ_DB_PATH") or "").strip()
    if db_path and Path(db_path).exists():
        return uin, db_path, True

    search_roots = [
        Path(r"C:\Tencent Files"),
        Path(r"D:\Tencent Files"),
        Path(r"E:\Tencent Files"),
        Path(r"F:\Tencent Files"),
    ]
    u = os.environ.get("USERPROFILE")
    if u:
        up = Path(u)
        search_roots.extend([
            up / "Documents" / "Tencent Files",
            up / "文档" / "Tencent Files",
            up / "OneDrive" / "Documents" / "Tencent Files",
            up / "OneDrive" / "文档" / "Tencent Files",
        ])
    for drive in ["C", "D", "E"]:
        users = Path(f"{drive}:/Users")
        if users.exists():
            try:
                for p_sub in users.glob("*/Documents/Tencent Files"):
                    search_roots.append(p_sub)
                for p_sub in users.glob("*/文档/Tencent Files"):
                    search_roots.append(p_sub)
                for p_sub in users.glob("*/OneDrive/Documents/Tencent Files"):
                    search_roots.append(p_sub)
                for p_sub in users.glob("*/OneDrive/文档/Tencent Files"):
                    search_roots.append(p_sub)
            except OSError:
                pass

    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
                personal, _ = winreg.QueryValueEx(k, "Personal")
                personal_expanded = Path(os.path.expandvars(personal))
                tf = personal_expanded / "Tencent Files"
                if tf.exists():
                    search_roots.append(tf)
        except Exception:
            pass

    unique_roots = []
    seen_roots = set()
    for r in search_roots:
        try:
            k = str(r).lower()
            if k not in seen_roots and r.exists():
                seen_roots.add(k)
                unique_roots.append(r)
        except OSError:
            pass

    if uin:
        for root in unique_roots:
            for sub in [
                root / uin / "nt_qq" / "nt_db" / "nt_msg.db",
                root / uin / "nt_db" / "nt_msg.db",
                root / uin / "nt_msg.db",
            ]:
                if sub.exists():
                    try:
                        common.save_env({"QQ_DB_PATH": str(sub), "QQ_TARGET_UIN": uin})
                    except Exception:
                        pass
                    return uin, str(sub), True

    candidates = []
    seen = set()
    for root in unique_roots:
        try:
            for d in root.iterdir():
                if not d.is_dir() or not d.name.isdigit():
                    continue
                subpaths = [
                    d / "nt_qq" / "nt_db" / "nt_msg.db",
                    d / "nt_db" / "nt_msg.db",
                    d / "nt_msg.db",
                ]
                for cand in subpaths:
                    if not cand.exists():
                        continue
                    k = str(cand).lower()
                    if k in seen:
                        continue
                    seen.add(k)
                    try:
                        st = cand.stat()
                        if st.st_size >= 4096:
                            candidates.append((st.st_mtime, d.name, str(cand)))
                    except OSError:
                        continue
        except OSError:
            continue

    if candidates:
        candidates.sort(reverse=True)
        best_mtime, best_uin, best_path = candidates[0]
        try:
            common.save_env({"QQ_DB_PATH": best_path, "QQ_TARGET_UIN": best_uin})
        except Exception:
            pass
        return best_uin, best_path, True

    return uin, db_path, False

"""双通道聊天数据体检（给人看的中文报告，不需要懂技术）。

用法:
    python tools/health.py

检查五件事:
    1. 数据源在不在、新不新鲜
    2. 密钥配没配、能不能真解开
    3. 表结构有没有固化、能不能用
    4. 上次采集什么时候、抓到多少
    5. 结论 + 下一步怎么办
"""


OK, WARN, BAD = "[正常]", "[注意]", "[异常]"


def check_source(env: dict) -> tuple[bool, list[str]]:
    db = Path(env.get("QQ_DB_PATH", "") or "")
    lines = [f"{OK} 企业微信: 走官方连接器采集（消息/待办/日程），不需要本地密钥",
             "        请在 WorkBuddy 连接器管理里确认「企业微信」已信任并登录，本脚本无法代为调用"]
    if not db or str(db) in ("", "."):
        # 没锁定路径不代表没有库：扫一下，把实际情况明确记录
        try:
            from admin_init import detect_qq_accounts  # noqa: PLC0415
            accs = detect_qq_accounts()
        except Exception:  # noqa: BLE001
            accs = []
        if accs:
            big = accs[0]
            lines.append(
                f"{WARN} QQ: 已发现 {len(accs)} 个账号（最大 {big['uin']}，"
                f"{big['size'] / 1048576:.1f} MB，{common.fmt(big['mtime'])}），但尚未锁定"
            )
            lines.append("      运行 Admin_Init.bat 会自动选号并写入 .env")
        else:
            lines.append(f"{BAD} QQ: 没有配置 QQ_DB_PATH，也没扫到任何 nt_msg.db")
        return False, lines
    if not db.exists():
        lines.append(f"{BAD} QQ: 找不到消息库 {db}")
        return False, lines
    st = db.stat()
    fresh = extract_impl.freshness(db)
    icon = {0: OK, 1: WARN}.get(fresh["level"], BAD)
    lines.append(f"{OK} QQ: {st.st_size / 1048576:.1f} MB，最后写入 {common.fmt(st.st_mtime)}")
    lines.append(f"      {icon} {fresh['reason']}")
    if fresh["level"] == 1:
        lines.append("      这段期间手机端新消息没同步过来，清单会偏空；双击登录电脑 QQ 即可")
    return True, lines


def check_key(env: dict, db: Path) -> tuple[bool, list[str]]:
    key = (env.get("QQ_DBKEY", "") or "").strip()
    lines: list[str] = []
    if not key:
        return False, [f"{BAD} QQ 密钥未填写：运行 Admin_Init.bat 让向导自动获取（需点一次 QQ 登录）"]
    if not qqdb.key_is_plausible(key):
        return False, [f"{BAD} QQ 密钥格式不对：当前 {len(key)} 位，应为 16 位 ASCII 口令（不是 64 位 hex）"]
    lines.append(f"{OK} QQ 密钥已填写（{common.mask(key)}，16 位 ASCII）")

    if not db.exists():
        lines.append(f"{WARN} 库文件不在，跳过错实测")
        return False, lines

    # 用户日常运行包里没有 admin_init.py（它只在「实施员一键初始化包」里），
    # 这里必须容忍缺失，否则只要 .env 里有一个格式正确的密钥，
    # health.py 就会直接 ModuleNotFoundError 崩掉（已实测复现）。
    try:
        from admin_init import verify_key  # 复用同一套实测逻辑，避免两份实现漂移
    except ImportError:
        lines.append(f"{WARN} 本包不含 admin_init.py，跳过密钥实测（只校验了格式）")
        lines.append("      要实测解密请把本工具放到「1-实施员一键初始化包」里运行")
        return True, lines

    ok, detail = verify_key(db, key, env.get("QQ_CIPHER_PROFILE") or None)
    if ok:
        lines.append(f"{OK} 实测解密成功（参数 {detail}）")
    else:
        lines.append(f"{BAD} 实测解不开：{str(detail).splitlines()[0]}")
        lines.append("      常见原因：QQ 升级过、重新登录导致密钥轮换、库文件被换。重跑 Admin_Init.bat")
    return ok, lines


def check_schema() -> tuple[bool, list[str]]:
    if not common.SCHEMA_PATH.exists():
        return False, [f"{BAD} 还没有 config/schema.json（表结构未适配），抽取会拒绝运行"]
    try:
        schema = common.read_json(common.SCHEMA_PATH)
    except Exception as exc:  # noqa: BLE001
        return False, [f"{BAD} schema.json 读不出来: {exc}"]
    tables = schema.get("tables") or []
    lines = [
        f"{OK if tables else BAD} 表结构已固化（{schema.get('generated_at', '未知时间')}，参数 {schema.get('source', {}).get('profile', '?')}）"
    ]
    for t in tables:
        cols = ", ".join(f"{k}={v}" for k, v in t["columns"].items() if v)
        lines.append(f"      - {t['name']}（{t['chat_type']}，{t['rows']} 行）：{cols}")
    if not tables:
        lines.append("      上次探测没识别出消息列，请重跑 Admin_Init.bat 并查看 work/schema.txt")
    return bool(tables), lines


def check_progress() -> list[str]:
    lines: list[str] = []
    cur = common.read_cursor()
    lines.append(f"{OK} 上次成功采集: {common.fmt(cur)}（{common.human_age(__import__('time').time() - cur)}）" if cur
                 else f"{WARN} 还没有成功采集过")
    if common.MATERIAL_PATH.exists():
        try:
            m = common.read_json(common.MATERIAL_PATH)
            meta = m.get("meta", {})
            flag = "带陈旧告警" if meta.get("stale_warning") else "正常"
            lines.append(f"{OK} 素材: {meta.get('message_count', 0)} 条，截止 {meta.get('cutoff_time', '?')}（{flag}）")
        except Exception:  # noqa: BLE001
            lines.append(f"{WARN} 素材文件存在但读不出来")
    else:
        lines.append(f"{WARN} 还没有生成过 work/待办素材.json")
    exc = common.list_exceptions()
    if exc:
        lines.append(f"{BAD} 存在 {len(exc)} 份未清除的异常说明：{exc[0].name}")
    return lines


def main() -> int:
    env = common.load_env()
    print("=" * 68)
    print("  企业微信 + QQ  待办采集 · 体检报告")
    print(f"  生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"  项目目录: {common.ROOT}")
    print("=" * 68)

    src_ok, src_lines = check_source(env)
    db = Path(env.get("QQ_DB_PATH", "") or "")
    key_ok, key_lines = check_key(env, db if str(db) not in ("", ".") else Path("."))
    schema_ok, schema_lines = check_schema()

    print("\n【一、数据源】")
    print("\n".join(src_lines))
    print("\n【二、密钥】")
    print("\n".join(key_lines))
    print("\n【三、表结构】")
    print("\n".join(schema_lines))
    print("\n【四、采集进度】")
    print("\n".join(check_progress()))

    print("\n" + "=" * 68)
    print("  结论")
    print("=" * 68)
    problems: list[str] = []
    if not src_ok:
        if not env.get("QQ_DB_PATH"):
            problems.append("QQ 账号未锁定：运行 Admin_Init.bat 自动选号写 .env")
        else:
            problems.append("QQ 消息库找不到：确认 QQ 装在本机并登录过一次")
    if not key_ok:
        problems.append("QQ 密钥不可用：运行 Admin_Init.bat 重新获取")
    if not schema_ok:
        problems.append("表结构未固化：运行 Admin_Init.bat 完成探测")
    if not env.get("WECOM_WEBHOOK_URL"):
        problems.append("企业微信机器人未配置：清单生成后推不到手机（不影响生成）")

    if problems:
        for i, p in enumerate(problems, 1):
            print(f"  {i}. {p}")
        print("\n  企业微信那一路不受影响，可以先单独跑起来。")
        return 1
    print(f"  {OK} QQ 侧就绪。企业微信侧请在连接器管理里确认已授权。")
    return 0


# 多平台环境探测、密钥同步与连通性测试 (全平台协同模式支持)
def candidate_key_files() -> list[Path]:
    files = []
    vendor = wechat.find_vendor()
    if vendor:
        files.append(vendor / "key_windows.txt")
    files.extend([
        common.HOME / "vendor" / "wechat-key" / "key_windows.txt",
        common.HOME / "key_windows.txt",
        common.HOME.parent / "1-实施员一键初始化包" / "vendor" / "wechat-key" / "key_windows.txt",
        common.HOME.parent / "2-用户日常全自动运行包" / "vendor" / "wechat-key" / "key_windows.txt",
    ])
    return files


_INSPECT_CACHE: dict = {"ts": 0.0, "key": "", "data": None}
INSPECT_TTL = 8.0


def inspect_channels_environment(env_path: Path | None = None, force: bool = False) -> dict:
    """全面扫描 5 大平台在本机上的环境配置、数据路径及凭证状态。

    加了 8 秒短缓存：前端 is_poll 每次都会问一遍，而这个函数要扫多盘 Tencent Files、
    多个微信分库目录、还要读 N 个密钥文件。旧实现在慢盘上会超过前端 8 秒超时被
    AbortError 掐掉，界面就永远停在"检测中..."——用户看到的就是"线路检测出不来结果"。
    缓存键包含 .env 修改时间，改完配置立刻能看到新状态。
    """
    try:
        env_stamp = str((env_path or common.ENV_PATH).stat().st_mtime_ns)
    except OSError:
        env_stamp = "0"
    try:
        schema_stamp = str(common.SCHEMA_PATH.stat().st_mtime_ns)
    except OSError:
        schema_stamp = "0"
    try:
        cfg_stamp = str((common.CONFIG / "active_platforms.json").stat().st_mtime_ns)
    except OSError:
        cfg_stamp = "0"
    cache_key = f"{env_path or common.ENV_PATH}|{env_stamp}|{schema_stamp}|{cfg_stamp}"
    now = time.time()
    if (not force and _INSPECT_CACHE["data"] is not None
            and _INSPECT_CACHE["key"] == cache_key
            and now - _INSPECT_CACHE["ts"] < INSPECT_TTL):
        return _INSPECT_CACHE["data"]

    data = _inspect_channels_uncached(env_path)
    _INSPECT_CACHE.update({"ts": now, "key": cache_key, "data": data})
    return data


def _inspect_channels_uncached(env_path: Path | None = None) -> dict:
    env = common.load_env(env_path)

    # 1. QQ 状态
    uin, qq_db_path, qq_db_exists = locate_qq_db(env)
    qq_key = (env.get("QQ_DBKEY") or "").strip()
    # 表结构是否已固化：只有 schema.json 里有可用消息表，抽取才跑得起来。
    # 旧版 UI 只看"密钥在不在"，于是「密钥抓到了但一条消息都读不出来」被显示成绿灯。
    schema_ready = False
    try:
        schema_data = common.read_json(common.SCHEMA_PATH)
        schema_ready = bool(schema_data.get("tables"))
    except Exception:  # noqa: BLE001
        schema_ready = False

    if not qq_key:
        qq_desc = "待填密钥"
    elif len(qq_key) != 16:
        qq_desc = f"密钥格式不对（{len(qq_key)} 位，应为 16）"
    elif not qq_db_exists:
        qq_desc = "待定位数据库"
    elif not schema_ready:
        qq_desc = "密钥已配置 · 待固化表结构"
    else:
        qq_desc = "在线监听 (NTQQ 只读解密就绪)"

    qq_info = {
        "channel": "QQ",
        "uin": uin or "未绑定",
        "key_configured": bool(qq_key),
        "key_valid": bool(qq_key and len(qq_key) == 16),
        "db_path": qq_db_path or "未定位",
        "db_exists": qq_db_exists,
        "schema_ready": schema_ready,
        "status_desc": qq_desc,
    }

    # 2. 个人微信 状态
    wx_key = (env.get("WX_DBKEY") or "").strip()
    vendor = wechat.find_vendor()
    source = wechat.find_source_storage((env.get("WX_DATA_PATH") or "").strip())
    mirror = wechat.find_mirror()
    mirror_dbs = wechat.message_dbs(mirror) if mirror else []

    key_file_found = None
    key_file_val = ""
    for kf in candidate_key_files():
        if kf.exists():
            try:
                txt = kf.read_text(encoding="ascii", errors="ignore").strip()
                m = re.search(r"[0-9a-fA-F]{64}", txt)
                if m:
                    key_file_found = str(kf)
                    key_file_val = m.group(0)
                    break
            except OSError:
                pass

    wx_info = {
        "channel": "WeChat",
        "key_configured": bool(wx_key),
        "key_valid": bool(wx_key and len(wx_key) == 64),
        "vendor_found": vendor is not None,
        "vendor_dir": str(vendor) if vendor else "",
        "data_dir_found": source is not None,
        "data_dir": str(source) if source else "",
        "mirror_ready": bool(mirror and mirror_dbs),
        "mirror_db_count": len(mirror_dbs),
        "key_file_detected": key_file_found is not None,
        "key_file_path": key_file_found or "",
        "key_file_value": (key_file_val[:4] + "****" + key_file_val[-4:]) if key_file_val else "",
        "status_desc": (
            "本地解密就绪 (读明文镜像)" if (wx_key and mirror and mirror_dbs)
            else ("待首次解密" if (wx_key and source)
            else ("已检测到本地密钥文件，点击即可导入" if key_file_val
            else ("待填密钥" if source
            else ("未登录微信4.x或未找到数据目录" if not source else "待提取密钥"))))
        ),
    }

    # 3. 飞书 状态
    fs_app = (env.get("FEISHU_APP_ID") or "").strip()
    fs_sec = (env.get("FEISHU_APP_SECRET") or "").strip()
    feishu_info = {
        "channel": "Feishu",
        "app_id_configured": bool(fs_app),
        "app_secret_configured": bool(fs_sec),
        "status_desc": "Open API 拉取就绪" if (fs_app and fs_sec) else "待填凭证",
    }

    # 4. 钉钉 状态
    dt_app = (env.get("DINGTALK_CLIENT_ID") or "").strip()
    dt_sec = (env.get("DINGTALK_CLIENT_SECRET") or "").strip()
    dt_inbox = base.inbox_path("DingTalk")
    dt_inbox_exists = dt_inbox.exists()
    dt_inbox_count = 0
    if dt_inbox_exists:
        try:
            dt_inbox_count = sum(1 for line in dt_inbox.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip().startswith("{"))
        except OSError:
            pass
    dingtalk_info = {
        "channel": "DingTalk",
        "client_id_configured": bool(dt_app),
        "client_secret_configured": bool(dt_sec),
        "inbox_exists": dt_inbox_exists,
        "inbox_count": dt_inbox_count,
        "status_desc": (f"收件箱模式 (已有 {dt_inbox_count} 条消息)" if dt_inbox_count > 0 else ("收件箱空 (需会话存档/回调写入)" if (dt_app and dt_sec) else "待填凭证/收件箱")),
    }

    # 5. 企业微信 状态
    wecom_hook = (env.get("WECOM_WEBHOOK_URL") or "").strip()
    wc_inbox = base.inbox_path("WeCom")
    wc_inbox_exists = wc_inbox.exists()
    wc_inbox_count = 0
    if wc_inbox_exists:
        try:
            wc_inbox_count = sum(1 for line in wc_inbox.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip().startswith("{"))
        except OSError:
            pass
    wecom_info = {
        "channel": "WeCom",
        "webhook_configured": bool(wecom_hook),
        "inbox_exists": wc_inbox_exists,
        "inbox_count": wc_inbox_count,
        "status_desc": (f"收件箱模式 (已有 {wc_inbox_count} 条消息)" if wc_inbox_count > 0 else ("已配置 Webhook 推送" if wecom_hook else "收件箱模式 (由连接器写入)")),
    }

    return {
        "qq": qq_info,
        "wechat": wx_info,
        "feishu": feishu_info,
        "dingtalk": dingtalk_info,
        "wecom": wecom_info,
    }


def handle_wechat_key_sync(key_file_path: Path | None = None, key_str: str = "", env_path: Path | None = None) -> dict:
    """把手工/文件来源的微信密钥**校验通过后**写回 .env，并解一次镜像。

    旧实现在这里不校验就直接写 .env：旧账号、旧机器、上次重装前的密钥
    都会被子写进去，之后采集只会报「解密失败」，没人知道其实是密钥过期。
    现在一律先过 page-1 校验，密钥不对就明确告诉用户哪里不对。
    """
    target_env = env_path or common.ENV_PATH
    env = common.load_env(target_env)
    storage, roots = wxkey.pick_storage((env.get("WX_DATA_PATH") or "").strip())
    if storage is None:
        return {
            "ok": False,
            "message": "没找到微信数据目录（xwechat_files/<wxid>/db_storage）。请先登录电脑版微信 4.x。",
        }
    db_path = wxkey.message_db(storage)
    if not db_path.exists():
        return {"ok": False, "message": f"{storage} 下没有 message/message_0.db"}

    source_desc = ""
    raw_key = ""
    if key_str:
        raw_key = wxkey.normalize_key(key_str)
        source_desc = "人工输入"
    elif key_file_path and Path(key_file_path).exists():
        try:
            raw_key = wxkey.normalize_key(Path(key_file_path).read_text(encoding="ascii", errors="ignore"))
            source_desc = str(key_file_path)
        except OSError as exc:
            return {"ok": False, "message": f"读取密钥文件失败: {exc}"}
    if not raw_key:
        raw_key, kf = wxkey.read_key_file(env)
        source_desc = str(kf) if kf else ""

    if not raw_key:
        return {
            "ok": False,
            "need_scan": True,
            "message": "没有找到可用的 64 位密钥。可点「自动抓取」扫描微信进程，或直接粘贴密钥。",
        }

    ok, why = wxkey.validate(raw_key, db_path)
    masked = raw_key[:4] + "****" + raw_key[-4:]
    if not ok:
        return {
            "ok": False,
            "stale_key": True,
            "masked_key": masked,
            "message": f"这份密钥（{masked}，来自 {source_desc or '未知来源'}）不能用：{why}。请重新抓取。",
        }

    wxkey.store_key(raw_key, target_env)
    raw = bytes.fromhex(raw_key)
    mirror = wxkey.mirror_root(env) / wxkey.account_of(storage) / "db_storage"
    res = wxdecrypt.decrypt_storage(storage, mirror, raw)
    decrypt_msg = f"；明文镜像：{res.message}（{res.elapsed_ms} ms）"
    return {
        "ok": True,
        "masked_key": masked,
        "source_file": source_desc,
        "mirror": str(mirror),
        "message": f"微信密钥已校验通过并同步至 .env（{masked}）{decrypt_msg}",
    }


def wechat_key_scan(timeout: float = 45.0, progress=None) -> dict:
    """扫描微信进程内存取密钥（只读），校验通过才落盘。

    progress(stage, **fields)：给控制台的任务进度用。长耗时动作必须能被
    轮询到，否则前端只会看到"卡住"。
    """
    emit = progress or (lambda *_a, **_k: None)

    emit("检查微信与数据目录")
    env = common.load_env()
    storage, _roots = wxkey.pick_storage((env.get("WX_DATA_PATH") or "").strip())
    if storage is None:
        return {"ok": False, "message": "没找到微信数据目录，无法校验密钥；请先登录电脑版微信 4.x。"}
    db_path = wxkey.message_db(storage)
    if not db_path.exists():
        return {"ok": False, "message": f"{storage} 下没有 message/message_0.db"}

    running = wxkey._wechat_pids()
    if not running:
        return {
            "ok": False,
            "status": wxkey.NO_WECHAT,
            "message": "没有检测到正在运行的微信（Weixin.exe）。请先在桌面上登录微信，再点抓取。",
            "notes": [],
        }
    main_pid, main_kb = running[0]
    emit("扫描微信内存", pid=main_pid, wechat_mb=main_kb // 1024,
         scanned_mb=0, candidates=0)

    def scan_progress(scanned, regions, *_extra):
        emit("扫描微信内存", pid=main_pid, wechat_mb=main_kb // 1024,
             scanned_mb=scanned // 1024 // 1024, regions=regions)

    # 只扫主进程。Weixin.exe 还有 4 个 --type=wxocr/wxutility/... 的插件子进程，
    # 它们不碰数据库；一起扫会多读 500~700MB，纯属浪费。
    res = wxkey.scan_memory_for_key(
        db_path, timeout=timeout, pids=[main_pid], progress=scan_progress
    )
    if not res.ok:
        return {
            "ok": False,
            "status": res.status,
            "message": res.message,
            "notes": res.notes[:10],
        }

    emit("校验通过，正在写入配置")
    written = wxkey.store_key(res.key, env=env, keyring=res.keyring)
    mirror = wxkey.mirror_root(env) / wxkey.account_of(storage) / "db_storage"
    emit("正在生成明文镜像", mirror=str(mirror))
    dres = wxdecrypt.decrypt_storage(storage, mirror, res.keyring or bytes.fromhex(res.key))
    return {
        "ok": True,
        "masked_key": res.key[:4] + "****" + res.key[-4:],
        "source": res.source,
        "keyring": len(res.keyring),
        "mirror": str(mirror),
        "written": written,
        "message": (
            f"已捕获并通过校验（{res.key[:4]}****{res.key[-4:]}，"
            f"覆盖 {len(res.keyring)} 个库）；镜像：{dres.message}"
        ),
        "notes": res.notes[:8],
    }


def wechat_key_capture(progress=None) -> dict:
    """启动时挂载抓取（Frida Hook 备选方案；推荐优先使用内存扫描）。具体实现在 tools/wxhook.py。"""
    emit = progress or (lambda *_a, **_k: None)
    try:
        import wxhook  # noqa: PLC0415
    except ImportError as exc:
        return {"ok": False, "message": f"缺少启动钩子模块: {exc}"}

    env = common.load_env()
    storage, _roots = wxkey.pick_storage((env.get("WX_DATA_PATH") or "").strip())
    if storage is None:
        return {"ok": False, "message": "没找到微信数据目录，请先登录电脑版微信 4.x。"}
    db_path = wxkey.message_db(storage)
    if not db_path.exists():
        return {"ok": False, "message": f"{storage} 下没有 message/message_0.db"}

    res = wxhook.capture(db_path, timeout=180.0, progress=emit)
    if not res.ok:
        return {"ok": False, "status": res.status, "message": res.message,
                "notes": res.notes[:10]}
    written = wxkey.store_key(res.key, env=env)
    raw = bytes.fromhex(res.key)
    mirror = wxkey.mirror_root(env) / wxkey.account_of(storage) / "db_storage"
    emit("正在生成明文镜像", mirror=str(mirror))
    dres = wxdecrypt.decrypt_storage(storage, mirror, raw)
    return {
        "ok": True,
        "masked_key": res.key[:4] + "****" + res.key[-4:],
        "source": "hook",
        "mirror": str(mirror),
        "written": written,
        "message": f"已在微信启动瞬间捕获密钥并通过校验（{res.key[:4]}****{res.key[-4:]}）；镜像：{dres.message}",
        "notes": res.notes[:8],
    }


def trigger_wechat_key_extractor() -> dict:
    """在 Windows 下唤起微信取密钥向导脚本或助手窗口。"""
    cands = [
        common.HOME / "WeChat_Key.bat",
        common.HOME / "vendor" / "2-get-wechat-key.bat",
        common.HOME / "02-启动与安装脚本" / "WeChat_Key.bat",
        common.HOME / "02-启动与安装脚本" / "vendor" / "2-get-wechat-key.bat",
    ]
    bat_path = None
    for cand in cands:
        if cand.exists():
            bat_path = cand
            break

    if os.name == "nt":
        try:
            if bat_path and bat_path.exists():
                subprocess.Popen(["cmd.exe", "/c", "start", "微信4.x取密钥向导", str(bat_path)])
            else:
                py_exe = sys.executable
                subprocess.Popen([
                    "cmd.exe", "/c", "start", "微信4.x取密钥向导",
                    str(py_exe), str(common.HOME / "tools" / "wxkey.py"), "acquire",
                ])
            return {
                "ok": True,
                "action": "launched",
                "message": "已唤起【微信 4.x 密钥提取向导】！正在通过 Windows 原生内存只读扫描自动捕获 64 位密钥，微信无需关闭，请保持微信正常登录。",
            }
        except OSError as exc:
            return {"ok": False, "message": f"启动取密钥脚本失败: {exc}"}
    else:
        return {
            "ok": False,
            "manual": True,
            "message": "非 Windows 环境无法直接注入微信。请在目标 Windows 主机上双击运行 vendor/2-get-wechat-key.bat。",
        }


def trigger_qq_key_extractor() -> dict:
    """在 Windows 下唤起 QQ 取密钥向导，以管理员权限拉起全自动抓取、解密校验与 .env 写入流程。"""
    if os.name != "nt":
        return {
            "ok": False,
            "manual": True,
            "message": "非 Windows 环境无法直接附加 QQ 进程。",
        }

    home = common.HOME
    bat = home / "vendor" / "1-get-qq-key.bat"
    if not bat.exists():
        bat = home.parent / "1-实施员一键初始化包" / "vendor" / "1-get-qq-key.bat"

    if bat.exists():
        try:
            # 以管理员权限唤起 1-get-qq-key.bat，保证具备调试器权限，且执行完整的抓取+写入.env+固化schema流程
            safe_bat = str(bat).replace("'", "''")
            ps_cmd = f"Start-Process -FilePath '{safe_bat}' -Verb RunAs"
            subprocess.Popen(["powershell", "-NoProfile", "-Command", ps_cmd])
            return {
                "ok": True,
                "action": "launched",
                "message": "已在桌面以管理员权限唤起【QQ取密钥向导】！请在弹出的窗口中配合点击登录或扫码，抓取成功后会自动写入配置。",
            }
        except Exception:
            try:
                subprocess.Popen(["cmd.exe", "/c", "start", "QQ取密钥向导", str(bat)])
                return {
                    "ok": True,
                    "action": "launched",
                    "message": "已在桌面唤起【QQ取密钥向导】！",
                }
            except OSError as exc:
                return {"ok": False, "message": f"启动 QQ 取密钥脚本失败: {exc}"}

    return {"ok": False, "message": "未找到 vendor/1-get-qq-key.bat 向导脚本。"}
def test_feishu_connection(env_dict: dict | None = None) -> dict:
    """测试飞书 Open API 凭证连通性，并统计可见群聊数。"""
    env = dict(common.load_env())
    if env_dict:
        env.update(env_dict)

    app_id = (env.get("FEISHU_APP_ID") or "").strip()
    app_sec = (env.get("FEISHU_APP_SECRET") or "").strip()
    if not app_id or not app_sec:
        return {
            "ok": False,
            "message": "FEISHU_APP_ID 或 FEISHU_APP_SECRET 为空，请先填写凭证。",
            "chat_count": 0,
            "tip": "登录飞书开放平台 (open.feishu.cn) 创建自建应用并获取凭证。",
        }

    try:
        token = feishu.tenant_token(env)
        chats = feishu.list_chats(env, token, limit=10)
        chat_names = [c.get("name") or "未命名群" for c in chats[:5]]
        count = len(chats)
        tip = ""
        if count == 0:
            tip = "⚠️ 凭证有效但当前可见群聊为 0。飞书机制限制：必须在群设置中将自建应用/机器人拉入（加群）需要提取待办的群中，否则无法读取任何群消息！"
        else:
            tip = f"连通正常！当前已识别群聊: {', '.join(chat_names)}"

        return {
            "ok": True,
            "message": f"飞书连接成功！已获取 tenant_access_token，当前可见群聊数: {count}。",
            "chat_count": count,
            "sample_chats": chat_names,
            "tip": tip,
        }
    except Exception as exc:
        return {
            "ok": False,
            "message": f"飞书连接测试失败: {exc}",
            "chat_count": 0,
            "sample_chats": [],
            "tip": "请检查 App ID 与 Secret 是否正确，且自建应用已开通 im:message:readonly 与 im:chat:readonly 权限并已发布版本。",
        }


def test_dingtalk_connection(env_dict: dict | None = None) -> dict:
    """测试钉钉 Client ID / Secret 连通性。"""
    env = dict(common.load_env())
    if env_dict:
        env.update(env_dict)

    client_id = (env.get("DINGTALK_CLIENT_ID") or "").strip()
    client_sec = (env.get("DINGTALK_CLIENT_SECRET") or "").strip()
    if not client_id or not client_sec:
        return {
            "ok": False,
            "message": "DINGTALK_CLIENT_ID 或 DINGTALK_CLIENT_SECRET 为空。",
            "tip": "钉钉企业内部应用需在钉钉开放平台获取 AppKey 与 AppSecret。",
        }

    url = f"https://oapi.dingtalk.com/gettoken?appkey={base.urllib.parse.quote(client_id)}&appsecret={base.urllib.parse.quote(client_sec)}"
    try:
        data = base.http_json(url, timeout=8)
        code = data.get("errcode")
        if code == 0:
            return {
                "ok": True,
                "message": "钉钉凭证验证成功，已获取 AccessToken！",
                "tip": "重要提示：钉钉普通应用无权直接拉取群消息，必须在钉钉后台开通「会话内容存档」或通过机器人事件回调将消息写入收件箱 (work/inbox/dingtalk.jsonl)。若无存档权限，建议在控制台取消勾选钉钉。",
            }
        return {
            "ok": False,
            "message": f"钉钉验证失败: errcode={code} errmsg={data.get('errmsg')}",
            "tip": "请核对 AppKey (Client ID) 与 AppSecret (Client Secret)。",
        }
    except Exception as exc:
        return {
            "ok": False,
            "message": f"钉钉请求失败: {exc}",
            "tip": "请检查本机网络或凭证配置。",
        }


def test_wecom_webhook(webhook_url: str = "") -> dict:
    """测试企微群机器人 Webhook 连通性。"""
    url = (webhook_url or common.load_env().get("WECOM_WEBHOOK_URL") or "").strip()
    if not url:
        return {"ok": False, "message": "未配置 WECOM_WEBHOOK_URL。"}
    ok_url, url_err = common.validate_outbound_url(url, require_https=True)
    if not ok_url:
        return {"ok": False, "message": f"Webhook 地址被安全策略拒绝：{url_err}"}

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": f"### 🔔 待办汇总助手 · 连通性测试\n- **状态**：正常联通\n- **时间**：{time.strftime('%Y-%m-%d %H:%M:%S')}\n- **说明**：此消息由进程监控台连通性测试触发。",
        },
    }
    try:
        res = base.http_json(url, method="POST", payload=payload, timeout=8)
        if res.get("errcode") == 0:
            return {"ok": True, "message": "企业微信 Webhook 连通成功！群内应已收到测试通知。"}
        return {"ok": False, "message": f"企微 Webhook 响应错误: errcode={res.get('errcode')} errmsg={res.get('errmsg')}"}
    except Exception as exc:
        return {"ok": False, "message": f"企业微信 Webhook 发送失败: {exc}"}


def import_inbox_messages(channel: str, messages: list[dict] | str) -> dict:
    """将外部消息安全写入指定通道的收件箱文件 (work/inbox/<channel>.jsonl)。"""
    if channel not in ("WeCom", "DingTalk", "WeChat", "Feishu"):
        return {"ok": False, "message": f"不支持的通道类型: {channel}"}

    rows: list[dict] = []
    if isinstance(messages, str):
        text = messages.strip()
        if text.startswith("[") or text.startswith("{"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    rows = [r for r in parsed if isinstance(r, dict)]
                elif isinstance(parsed, dict):
                    rows = [parsed]
            except json.JSONDecodeError as exc:
                return {"ok": False, "message": f"JSON 解析失败: {exc}"}
        else:
            rows = [{
                "chat_name": "测试导入会话",
                "sender": "导入助手",
                "msg_id": f"manual-{int(time.time()*1000)}",
                "content": text,
                "timestamp": common.fmt(time.time()),
            }]
    elif isinstance(messages, list):
        rows = [r for r in messages if isinstance(r, dict)]

    if not rows:
        return {"ok": False, "message": "没有解析到有效的消息记录。"}

    # 规范化并追加到收件箱
    common.ensure_dirs()
    normalized = []
    now = time.time()
    for r in rows:
        content = str(r.get("content") or r.get("text") or "").strip()
        if not content:
            continue
        mid = str(r.get("msg_id") or r.get("id") or f"{channel.lower()}-{int(time.time()*1000)}-{len(normalized)}")
        ts = base.parse_ts(r) or now
        normalized.append({
            "source": channel,
            "chat_type": str(r.get("chat_type") or "group"),
            "chat_id": str(r.get("chat_id") or ""),
            "chat_name": str(r.get("chat_name") or "外部导入会话"),
            "sender": str(r.get("sender") or "未知发送人"),
            "timestamp": common.fmt(ts),
            "msg_id": mid,
            "content": content,
        })

    if not normalized:
        return {"ok": False, "message": "所有输入记录均缺少 content 文本。"}

    written = base.append_inbox(channel, normalized)
    return {
        "ok": True,
        "written": written,
        "message": f"成功写入 {written} 条待办消息至 {channel} 收件箱 ({base.inbox_path(channel).name})！",
    }


if __name__ == "__main__":
    sys.exit(main())
