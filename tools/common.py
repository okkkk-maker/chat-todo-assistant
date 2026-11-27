"""公共层：路径、.env 读写、控制台输出、异常说明落盘。

所有模块都从这里取路径与配置，避免各处硬编码路径后又忘了同步。
只用标准库，不引入第三方依赖。
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# 本程序自己的"我是这个包"的特征文件。用于判断某个目录到底是本程序的根，
# 还是别人家的目录（见 _resolve_home）。
OWN_MARKERS = ("tools/version.py", "server.py", "run.py", "index.html")


def _looks_like_this_app(folder: Path) -> bool:
    try:
        return all((folder / m).is_file() for m in OWN_MARKERS[:3])
    except OSError:
        return False


def _resolve_home() -> Path:
    """数据目录（.env / config / work / output）放哪儿。

    正常情况就是程序所在目录；但如果使用者把「用户日常包」解压成了子文件夹
    （Windows 拖拽解压的默认行为），数据和代码就分家了 —— 这时往上一层找
    已经初始化过的那份，避免出现"找不到 .env、又要重新初始化"的困惑。

    安全约束（v1.5.3.3 加）：向上取一层**必须**满足下面全部条件，否则只用 ROOT：
        1. ROOT 自己还没初始化（既没有 .env 也没有 config/schema.json）——
           已经初始化过就绝不上溯，避免"故意清空 .env 之后被父目录接管"；
        2. 父目录里确实有本程序的特征文件（tools/version.py 等）——
           否则父目录可能是桌面/别的项目，写进去会踩坏别人的文件。
    旧实现只判断"父目录有没有 .env"，于是把程序解压在桌面上、桌面上又恰好
    有别的项目的 .env 时，HOME 会被劫持到桌面，save_env() 直接把别人的
    .env 覆写成我们的配置。
    """
    if (ROOT / ".env").exists() or (ROOT / "config" / "schema.json").exists():
        return ROOT
    parent = ROOT.parent
    try:
        same_drive_root = parent == parent.parent
    except OSError:
        same_drive_root = True
    if not same_drive_root and _looks_like_this_app(parent):
        if (parent / ".env").exists() or (parent / "config" / "schema.json").exists():
            return parent
    return ROOT


HOME = _resolve_home()

# 允许用环境变量把工作目录/输出目录指到别处（自测时用，避免污染真实 work/ output/）
WORK = Path(os.environ.get("QQTODO_WORK_DIR") or (HOME / "work"))
OUT = Path(os.environ.get("QQTODO_OUT_DIR") or (HOME / "output"))
TMP_DB = WORK / "tmp_db"
CONFIG = HOME / "config"
DATA = HOME / "data"
ENV_PATH = Path(os.environ.get("QQTODO_ENV") or (HOME / ".env"))
SCHEMA_PATH = Path(os.environ.get("QQTODO_SCHEMA") or (CONFIG / "schema.json"))
MATERIAL_PATH = WORK / "待办素材.json"
CURSOR_PATH = WORK / "last_sync_ts.txt"

# .env 中本程序关心的键，以及写入时的顺序
ENV_KEYS = [
    "QQ_TARGET_UIN",
    "QQ_DB_PATH",
    "QQ_DBKEY",
    "QQ_CIPHER_PROFILE",
    "WECOM_WEBHOOK_URL",
    "SUMMARY_HOURS",
]

DEFAULT_HOURS = 4
CURSOR_MAX_AGE = 7 * 24 * 3600   # 游标最长有效期（秒）
CLOCK_SKEW_TOL = 120             # 判定游标有效性时容忍的时钟偏差（秒）

# 异常说明文件名（固定名，方便定时任务用 异常说明-*.txt 通配匹配到）
EXC_NO_KEY = "异常说明-密钥未就绪.txt"
EXC_DECRYPT = "异常说明-QQ解密失败.txt"
EXC_NO_DB = "异常说明-QQ库文件丢失.txt"
EXC_NO_SCHEMA = "异常说明-表结构未适配.txt"
EXC_EXTRACT = "异常说明-QQ抽取异常.txt"


# 控制台
def _enable_utf8_console() -> None:
    """Windows 控制台默认 GBK，输出中文和 emoji 会炸，这里强制 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


_enable_utf8_console()


def log(msg: str = "") -> None:
    print(msg, flush=True)


def head(title: str, char: str = "=", width: int = 64) -> None:
    log(char * width)
    log(f"  {title}")
    log(char * width)


def set_title(text: str) -> None:
    """设控制台窗口标题。用 Unicode API，绕开 bat 文件里的中文编码坑。"""
    try:
        import ctypes  # noqa: PLC0415
        ctypes.windll.kernel32.SetConsoleTitleW(text)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


# 时间
def now() -> datetime:
    return datetime.now()


def fmt(ts: float | int | None) -> str:
    if ts is None:
        return "未知"
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")


def human_age(sec: float) -> str:
    sec = max(0.0, sec)
    if sec < 60:
        return f"{sec:.0f} 秒前"
    if sec < 3600:
        return f"{sec / 60:.0f} 分钟前"
    if sec < 172800:
        return f"{sec / 3600:.1f} 小时前"
    return f"{sec / 86400:.1f} 天前"


# 目录
def ensure_dirs() -> None:
    for p in (WORK, TMP_DB, OUT, CONFIG, DATA, WORK / "inbox"):
        p.mkdir(parents=True, exist_ok=True)


# 原子落盘
def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """原子写文本：先写同目录临时文件，fsync 落盘后再 os.replace 顶替目标。

    为什么必须这样：
        path.write_text() 是「打开即截断」，真遇到断电/强制关机/用户关黑窗口，
        文件就停在 0 字节。.env 变空 -> 后续全部判定 NO_KEY 停摆；
        游标变空 -> 读不出数字 -> 每次都退回默认回看窗口。
    临时文件放在同一目录，os.replace 才是同卷的原子改名（跨卷会退化成复制）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp_{os.getpid()}_{time.time_ns()}")
    try:
        with open(tmp_path, "w", encoding=encoding, errors="replace") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, path)
    finally:
        # 走到这里只剩两种可能：replace 成功（临时文件已消失）或中途出错（清掉残骸）
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


# .env 读写（保留注释与顺序，逐键替换，不整体重写）
def safe_path(text: object) -> Path | None:
    """把可能为空的配置值转成 Path；空值返回 None。

    陷阱：`Path("")` 等于 `Path(".")`，而 `Path(".").exists()` 永远为 True。
    直接 `Path(env.get("QQ_DB_PATH","")).exists()` 会把"根本没配置"判成
    "路径存在"——状态页因此误报过「QQ 在线，消息库 50 秒前更新」（实际是
    在给当前工作目录算新鲜度）。
    """
    s = str(text or "").strip().strip('"').strip("'")
    if not s or s in (".", ".."):
        return None
    return Path(s)


def load_env(path: Path | None = None) -> dict[str, str]:
    path = path or ENV_PATH
    out: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()

    # 跨包兜底同步：只在本程序**完全没有 .env**（首次运行、还没初始化）时生效。
    #
    # 旧实现的条件是"缺少 QQ_DBKEY 或 QQ_CIPHER_PROFILE"，后果是：维护者按
    # 发布清单把 .env 里的密钥清空后，程序自己又会从同级实施员包抄回来并写盘，
    # 等于清理动作被程序撤销；两个包放在同一父目录时凭证还会在包之间自动流动。
    # 现在改成：文件存在就完全信任它（哪怕里面是空的）。
    if not path.exists() and path.parent.exists():
        sibling_env = ROOT.parent / "1-实施员一键初始化包" / ".env"
        if sibling_env.exists() and sibling_env.resolve() != path.resolve():
            try:
                for raw in sibling_env.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    ks, vs = k.strip(), v.strip()
                    if vs and ks not in out:
                        out[ks] = vs
                if out:
                    log("      [配置] 首次运行：已从同级实施员包复制初始配置（本机 .env 原先不存在）")
            except OSError:
                pass
    return out


@contextmanager
def _exclusive_file_lock(lock_path: Path):
    """跨进程独占锁；由操作系统在进程退出时自动释放。"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fp = open(lock_path, "a+b")
    try:
        if lock_path.stat().st_size == 0:
            fp.write(b"\0")
            fp.flush()
        fp.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fp.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fp.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        except (OSError, ImportError):
            pass
        fp.close()


def quote_ident(name: str) -> str:
    """安全转义 SQLite 标识符（双引号包裹并转义内部双引号）。"""
    return '"' + str(name).replace('"', '""') + '"'


def validate_outbound_url(url: str, *, require_https: bool = True, allow_private: bool = False) -> tuple[bool, str]:
    """校验出站 URL，防止 SSRF（拒绝非法协议、本地回环、内网及云元数据地址）。"""
    import ipaddress
    from urllib.parse import urlparse

    text = (url or "").strip()
    if not text:
        return False, "URL 不能为空"
    if any(c in text for c in ("\r", "\n", "\x00")):
        return False, "URL 包含非法控制字符"
    try:
        parsed = urlparse(text)
    except ValueError:
        return False, "URL 格式非法"

    allowed_schemes = ("https",) if require_https else ("https", "http")
    if parsed.scheme.lower() not in allowed_schemes:
        return False, f"仅允许 {'/'.join(allowed_schemes)} 协议"

    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False, "URL 缺少主机名"
    if not allow_private:
        if host in ("localhost", "localhost.localdomain", "0.0.0.0", "::1") or host.endswith(".local"):
            return False, "出于安全限制，不允许向本机或内网地址发起请求"
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
                return False, "出于安全限制，不允许向私有/回环/链路本地 IP 发起请求"
        except ValueError:
            pass
    return True, ""


def save_env(values: dict[str, str], path: Path | None = None) -> None:
    """在跨进程锁内读-改-写 .env，原子替换防止半文件与丢失更新。"""
    for k, v in values.items():
        ks, vs = str(k), str(v)
        if not ks.strip() or any(c in ks for c in ("\r", "\n", "\x00", "=")):
            raise ValueError(f"非法的环境变量键名: {ks!r}")
        if any(c in vs for c in ("\r", "\n", "\x00")):
            raise ValueError(f"环境变量 {ks} 的值不能包含换行或空字符")
    path = path or ENV_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with _exclusive_file_lock(lock_path):
        lines: list[str] = []
        if path.exists():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

        pending = dict(values)
        out: list[str] = []
        for raw in lines:
            stripped = raw.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in pending:
                    out.append(f"{key}={pending.pop(key)}")
                    continue
            out.append(raw)

        if pending:
            if out and out[-1].strip():
                out.append("")
            for key in ENV_KEYS:
                if key in pending:
                    out.append(f"{key}={pending.pop(key)}")
            for key, val in pending.items():
                out.append(f"{key}={val}")

        atomic_write_text(path, "\n".join(out).rstrip() + "\n")
        if os.name != "nt":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass


def find_workbuddy_exe() -> Path | None:
    """从环境变量、PATH、注册表和常见安装目录定位 外部协作引擎。"""
    import shutil

    def valid(path: Path | None) -> Path | None:
        return path if path and path.is_file() else None

    env_exe = os.environ.get("WB_EXE", "").strip()
    if env_exe:
        hit = valid(Path(env_exe))
        if hit:
            return hit
    for name in ("WorkBuddy.exe", "workbuddy", "workbuddy.exe"):
        found = shutil.which(name)
        if found:
            hit = valid(Path(found))
            if hit:
                return hit

    candidates: list[Path] = []
    if os.name == "nt":
        try:
            import winreg

            uninstall_roots = (
                r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
                r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
            )
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(
                        hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths\WorkBuddy.exe"
                    ) as key:
                        value, _ = winreg.QueryValueEx(key, "")
                        if value:
                            candidates.append(Path(str(value).strip().strip('"')))
                except OSError:
                    pass
                for sub_path in uninstall_roots:
                    try:
                        with winreg.OpenKey(hive, sub_path) as root_key:
                            for index in range(winreg.QueryInfoKey(root_key)[0]):
                                try:
                                    sub_name = winreg.EnumKey(root_key, index)
                                    with winreg.OpenKey(root_key, sub_name) as item:
                                        try:
                                            location, _ = winreg.QueryValueEx(item, "InstallLocation")
                                            if location:
                                                candidates.append(Path(str(location)) / "WorkBuddy.exe")
                                        except OSError:
                                            pass
                                        try:
                                            icon, _ = winreg.QueryValueEx(item, "DisplayIcon")
                                            if icon and "workbuddy" in str(icon).lower():
                                                candidates.append(Path(str(icon).strip().strip('"').split(",")[0]))
                                        except OSError:
                                            pass
                                except OSError:
                                    continue
                    except OSError:
                        continue
        except ImportError:
            pass

    local = os.environ.get("LOCALAPPDATA", "")
    user = os.environ.get("USERPROFILE", "")
    program_files = os.environ.get("ProgramFiles", "")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", "")
    candidates.extend(
        p for p in (
            Path(local) / "Programs" / "WorkBuddy" / "WorkBuddy.exe" if local else None,
            Path(local) / "WorkBuddy" / "WorkBuddy.exe" if local else None,
            Path(user) / "AppData" / "Local" / "Programs" / "WorkBuddy" / "WorkBuddy.exe" if user else None,
            Path(program_files) / "WorkBuddy" / "WorkBuddy.exe" if program_files else None,
            Path(program_files_x86) / "WorkBuddy" / "WorkBuddy.exe" if program_files_x86 else None,
        ) if p is not None
    )
    for drive in ("C", "D", "E", "F"):
        candidates.extend((
            Path(f"{drive}:/WorkBuddy/WorkBuddy.exe"),
            Path(f"{drive}:/Program Files/WorkBuddy/WorkBuddy.exe"),
        ))
    for candidate in candidates:
        hit = valid(candidate)
        if hit:
            return hit
    return None


def mask(secret: str) -> str:
    if not secret:
        return "(空)"
    if len(secret) <= 6:
        return secret[0] + "*" * (len(secret) - 1)
    return f"{secret[:3]}{'*' * (len(secret) - 6)}{secret[-3:]}"


# JSON
def write_json(path: Path, obj) -> Path:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))
    return path


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# 异常说明（绝不静默失败）
def write_exception(name: str, reason: str, extra: str = "") -> Path:
    """写一份人话异常说明，返回路径。禁止用空清单掩盖失败。"""
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    body = (
        f"本次没有生成待办清单。\n\n"
        f"时间: {now():%Y-%m-%d %H:%M:%S}\n"
        f"原因:\n{reason}\n"
    )
    if extra:
        body += f"\n补充信息:\n{extra}\n"
    body += (
        "\n这说明：不是您没有待办，而是工具没读到数据。请按上面原因处理。\n"
        "如需协助，请把本文件发给维护人员。\n"
    )
    atomic_write_text(path, body)
    return path


def clear_exceptions() -> list[str]:
    """抓取恢复正常后清理旧告警，避免定时任务一直读到历史异常。"""
    removed: list[str] = []
    if not OUT.exists():
        return removed
    for p in OUT.glob("异常说明-*.txt"):
        try:
            p.unlink()
            removed.append(p.name)
        except OSError:
            pass
    return removed


def list_exceptions() -> list[Path]:
    if not OUT.exists():
        return []
    return sorted(OUT.glob("异常说明-*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)


# 游标
def read_cursor(path: Path | None = None) -> float | None:
    p = path or CURSOR_PATH
    if not p.exists():
        return None
    try:
        return float(p.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def write_cursor(ts: float, path: Path | None = None) -> None:
    p = path or CURSOR_PATH
    # 保留小数，避免毫秒级消息因四舍五入被下一轮起点越过。
    atomic_write_text(p, f"{float(ts):.6f}\n")


def advance_cursor(ts: float, path: Path | None = None) -> float:
    """单调推进游标，绝不倒退，也绝不越过系统当前时间。"""
    candidate = min(float(ts) + 1.0, time.time())
    previous = read_cursor(path)
    value = max(previous, candidate) if previous is not None else candidate
    write_cursor(value, path)
    return value


CHANNEL_CURSORS_PATH = WORK / "channel_cursors.json"
SEEN_IDS_PATH = WORK / "seen_msg_ids.json"


def read_channel_cursors() -> dict[str, float]:
    """按通道读取独立游标水位 (共享游标阻塞)。"""
    if not CHANNEL_CURSORS_PATH.exists():
        return {}
    try:
        data = read_json(CHANNEL_CURSORS_PATH)
        return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}
    except Exception:
        return {}


def advance_channel_cursor(channel: str, ts: float) -> float:
    """按通道独立单调推进游标，单个通道受阻不连带冻结其它正常通道。"""
    candidate = min(float(ts) + 1.0, time.time())
    cursors = read_channel_cursors()
    prev = cursors.get(channel)
    val = max(prev, candidate) if prev is not None else candidate
    cursors[channel] = val
    try:
        write_json(CHANNEL_CURSORS_PATH, cursors)
    except Exception:
        pass
    # 同步更新全局兜底游标（取活跃通道最小值）
    try:
        write_cursor(min(cursors.values()))
    except Exception:
        write_cursor(val)
    return val


def split_seen_msg_ids(messages: list[dict], max_seen: int = 5000) -> tuple[list[dict], set[str]]:
    """跨轮去重（只读）：返回 (本轮新消息, 本轮新出现的 msg_id 集合)。

    与 filter_seen_msg_ids 的区别：**不落盘**。调用方拿到 ids 后，只有在
    「本轮结果可采信」时才调 commit_seen_msg_ids() 提交。

    为什么必须拆成两步：
        旧实现一进来就把 msg_id 记成"已处理"。可当本轮因为通道离线/失败/截断
        被判定 cursor_blocked（游标冻结）时，下游对用户的承诺是
        「游标保持不变，问题修好后这段时间的消息会一次性补齐」。
        而 msg_id 已经被写进去重表 —— 修好后再采，这些消息全被判为重复丢掉，
        承诺反了，且丢得没有任何日志痕迹。这是不可逆的数据丢失。
    """
    seen: dict[str, float] = {}
    if SEEN_IDS_PATH.exists():
        try:
            seen = read_json(SEEN_IDS_PATH)
        except Exception:
            seen = {}
    if not isinstance(seen, dict):
        seen = {}

    unseen: list[dict] = []
    fresh: set[str] = set()
    for m in messages:
        mid = str(m.get("msg_id") or "").strip()
        if not mid or mid in seen or mid in fresh:
            continue
        fresh.add(mid)
        unseen.append(m)
    return unseen, fresh


def commit_seen_msg_ids(ids: set[str] | list[str], max_seen: int = 5000) -> int:
    """把本轮确认处理完的 msg_id 落到 work/seen_msg_ids.json。返回写入条数。"""
    if not ids:
        return 0
    seen: dict[str, float] = {}
    if SEEN_IDS_PATH.exists():
        try:
            seen = read_json(SEEN_IDS_PATH)
        except Exception:
            seen = {}
    if not isinstance(seen, dict):
        seen = {}
    now = time.time()
    added = 0
    for mid in ids:
        key = str(mid).strip()
        if key and key not in seen:
            seen[key] = now
            added += 1
    if len(seen) > max_seen:
        keep = sorted(seen.keys(), key=lambda k: seen[k], reverse=True)[:max_seen]
        seen = {k: seen[k] for k in keep}
    try:
        write_json(SEEN_IDS_PATH, seen)
    except Exception:
        return 0
    return added


def filter_seen_msg_ids(messages: list[dict], max_seen: int = 5000,
                        commit: bool = True) -> list[dict]:
    """跨轮次消息去重与幂等保证 ()：过滤掉近 5000 条已处理过的 msg_id。

    commit=False 时只过滤不落盘（供聚合器在判定 cursor_blocked 前使用）。
    """
    unseen, fresh = split_seen_msg_ids(messages, max_seen=max_seen)
    if commit:
        commit_seen_msg_ids(fresh, max_seen=max_seen)
    return unseen


def window(hours: float | None, overlap_sec: int = 600, ignore_cursor: bool = False) -> tuple[float, float, str]:
    """算抽取窗口；有效游标不因离线时间长而被静默作废。支持 ignore_cursor 模式忽略游标直接按小时数回看。"""
    end = time.time()
    hours = DEFAULT_HOURS if hours is None else float(hours)
    if not math.isfinite(hours) or hours <= 0:
        raise ValueError("回看小时数必须是大于 0 的有限数字")
    cur = read_cursor()
    # 只拒绝明显来自未来的游标。离线 7 天以上仍从原位置追赶，
    # 由上层在截断/失败时冻结游标，防止为控制单轮体量而永久跳过历史。
    if not ignore_cursor and cur is not None and cur > 0 and (end - cur) >= -CLOCK_SKEW_TOL:
        gap = max(0.0, end - cur)
        overlap = overlap_sec if gap <= 900 else 0
        since = cur - overlap
        why = (
            f"上次游标 {fmt(cur)}（跨度 {gap / 3600:.1f} 小时"
            + (f"，回退 {overlap // 60} 分钟防漏" if overlap else "")
            + "）"
        )
    else:
        since = end - hours * 3600
        why = f"强制回看最近 {hours:g} 小时 (忽略游标)" if (cur and ignore_cursor) else (f"游标超前，退回最近 {hours:g} 小时" if cur else f"最近 {hours:g} 小时")
    return since, end, why


def since_text(since: float, until: float) -> str:
    return f"{fmt(since)} ~ {fmt(until)}"


def hours_between(a: float, b: float) -> str:
    return f"{abs(b - a) / 3600:.1f} 小时"


# Windows 控制台静默执行封装（消除 conhost 窗口闪现）
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def run_hidden(cmd: list[str] | str, **kwargs) -> subprocess.CompletedProcess:
    """静默执行命令：Windows 下自动附加 CREATE_NO_WINDOW，绝不弹黑框。"""
    flags = kwargs.pop("creationflags", 0)
    if sys.platform == "win32":
        flags |= CREATE_NO_WINDOW
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    kwargs.setdefault("stdout", subprocess.PIPE)
    kwargs.setdefault("stderr", subprocess.PIPE)
    return subprocess.run(cmd, creationflags=flags, **kwargs)


def check_output_hidden(cmd: list[str] | str, **kwargs) -> bytes:
    """静默执行 check_output：Windows 下自动附加 CREATE_NO_WINDOW，绝不弹黑框。"""
    flags = kwargs.pop("creationflags", 0)
    if sys.platform == "win32":
        flags |= CREATE_NO_WINDOW
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.check_output(cmd, creationflags=flags, **kwargs)


def popen_hidden(cmd: list[str] | str, **kwargs) -> subprocess.Popen:
    """静默 Popen：Windows 下自动附加 CREATE_NO_WINDOW，绝不弹黑框。"""
    flags = kwargs.pop("creationflags", 0)
    if sys.platform == "win32":
        flags |= CREATE_NO_WINDOW
    return subprocess.Popen(cmd, creationflags=flags, **kwargs)


__all__ = [
    "ROOT", "HOME", "WORK", "TMP_DB", "OUT", "CONFIG", "DATA", "ENV_PATH", "SCHEMA_PATH",
    "MATERIAL_PATH", "CURSOR_PATH", "ENV_KEYS", "DEFAULT_HOURS", "CURSOR_MAX_AGE",
    "CLOCK_SKEW_TOL",
    "EXC_NO_KEY", "EXC_DECRYPT", "EXC_NO_DB", "EXC_NO_SCHEMA", "EXC_EXTRACT",
    "log", "head", "set_title", "now", "fmt", "human_age", "ensure_dirs",
    "atomic_write_text",
    "load_env", "save_env", "mask", "find_workbuddy_exe", "write_json", "read_json",
    "write_exception", "clear_exceptions", "list_exceptions",
    "read_cursor", "write_cursor", "advance_cursor", "window", "since_text", "hours_between",
]


def get_schema_path() -> Path:
    if SCHEMA_PATH.exists():
        return SCHEMA_PATH
    sibling_schema = ROOT.parent / "1-实施员一键初始化包" / "config" / "schema.json"
    if sibling_schema.exists():
        try:
            CONFIG.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(sibling_schema, SCHEMA_PATH)
            return SCHEMA_PATH
        except Exception:
            return sibling_schema
    return SCHEMA_PATH
