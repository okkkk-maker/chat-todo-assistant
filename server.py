#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
待办自动汇总助手 · 程序进程监控台 (Process Monitor Console)
开源项目: Todo Summary Assistant
对应系统: 外部协作引擎 多平台待办自动汇总流水线 (QQ/微信/飞书/钉钉/企微)
开发规范: Python 3.7+ 纯标准库开发，零第三方依赖，开箱即用
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import json
import time
import ctypes
import datetime
import threading
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
try:
    from http.server import ThreadingHTTPServer as _ServerCls
except ImportError:
    _ServerCls = HTTPServer
import urllib.request
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("QQTODO_CONSOLE_PORT", os.environ.get("PORT", 8000)))
HOST = os.environ.get("QQTODO_CONSOLE_HOST", "127.0.0.1")
BASE_DIR = Path(__file__).resolve().parent
HTML_FILE_PATH = BASE_DIR / "index.html"

# 自动探测实施包目录
PKG_ROOT = BASE_DIR
for candidate in [
    BASE_DIR,
    BASE_DIR / "1-实施员一键初始化包",
    BASE_DIR.parent / "unpacked_pkg" / "1-实施员一键初始化包",
    BASE_DIR.parent / "1-实施员一键初始化包"
]:
    if (candidate / "run.py").exists() and (candidate / "tools").exists():
        PKG_ROOT = candidate
        break

# 与 run.py 共用同一路径解析，避免父目录初始化/QQTODO_WORK_DIR 下各锁各的文件。
sys.path.insert(0, str(PKG_ROOT / "tools"))
import common  # noqa: E402
import runtime_env  # noqa: E402  解释器/依赖能力的唯一裁决点（见 tools/runtime_env.py）
import scheduler as sched  # noqa: E402  定时调度（真调度，不再是装饰性开关）

WORK_DIR = common.WORK
OUT_DIR = common.OUT
CONFIG_DIR = common.CONFIG
ENV_PATH = common.ENV_PATH
MATERIAL_PATH = common.MATERIAL_PATH
CURSOR_PATH = common.CURSOR_PATH
LOCK_PATH = common.WORK / ".process.lock"
ACTIVE_PLATFORMS_FILE = CONFIG_DIR / "active_platforms.json"

START_TIME = time.time()
_STATUS_CACHE = {"ts": 0.0, "data": None}
# 旧版本这个变量只是个布尔开关：改它只会改一句日志，没有任何线程在读。
# 现在它只用于展示，真实启停由 scheduler 的配置文件 + 常驻线程决定。
IS_SCHEDULER_RUNNING = True
SCHEDULER_THREAD: threading.Thread | None = None
STATE_LOCK = threading.Lock()

# 微信密钥任务队列（异步 + 可轮询）
#
# 旧实现把最长 25 秒的内存扫描直接放在 HTTP 处理函数里同步执行：
#   · 前端 api() 的默认超时只有 8 秒 → 必然先断开；
#   · 断开后弹出「请求超时（8s），监控台可能已卡住」，但后台其实还在跑；
#   · 用户看到的就是「抓取失败 + 监控台卡死」，而真正的扫描结果被丢掉。
# 现在改成 job：POST 立即返回 job id，前端每秒轮询进度，界面不再假死。
WX_JOB_LOCK = threading.Lock()
WX_JOB_SEQ = 0
WX_JOB: dict = {
    "id": "",
    "action": "",
    "state": "idle",       # idle | running | done | failed
    "stage": "",
    "message": "",
    "started": 0.0,
    "updated": 0.0,
    "progress": {},
    "result": None,
}


def _wx_job_snapshot() -> dict:
    with WX_JOB_LOCK:
        snap = dict(WX_JOB)
        snap["progress"] = dict(WX_JOB.get("progress") or {})
        return snap


def _wx_job_update(job_id: str, **fields) -> None:
    with WX_JOB_LOCK:
        if WX_JOB.get("id") != job_id:
            return
        progress = fields.pop("progress", None)
        WX_JOB.update(fields)
        if progress:
            WX_JOB.setdefault("progress", {}).update(progress)
        WX_JOB["updated"] = time.time()


def _wx_job_start(action: str, timeout: float) -> dict:
    """启动一个后台取密钥任务。同一时刻只允许一个，避免重复扫描拖慢微信。"""
    global WX_JOB_SEQ
    with WX_JOB_LOCK:
        if WX_JOB.get("state") == "running":
            return {
                "ok": True, "job": WX_JOB["id"], "state": "running",
                "message": "已有一轮抓取正在进行，直接看进度即可",
            }
        WX_JOB_SEQ += 1
        job_id = f"wx{int(time.time())}-{WX_JOB_SEQ}"
        WX_JOB.update({
            "id": job_id, "action": action, "state": "running",
            "stage": "正在准备", "message": "", "started": time.time(),
            "updated": time.time(), "progress": {}, "result": None,
        })
    threading.Thread(
        target=_wx_job_run, args=(job_id, action, timeout), daemon=True
    ).start()
    return {"ok": True, "job": job_id, "state": "running", "message": "已开始"}


def _wx_job_run(job_id: str, action: str, timeout: float) -> None:
    import health as inspect_helper  # noqa: PLC0415

    def progress(stage: str, **fields) -> None:
        _wx_job_update(job_id, stage=stage, progress=fields)

    try:
        if action == "hook":
            res = inspect_helper.wechat_key_capture(progress=progress)
        elif action == "sync":
            res = inspect_helper.handle_wechat_key_sync()
            if not res.get("ok"):
                progress("内存中没找到，继续扫描")
                res = inspect_helper.wechat_key_scan(timeout=timeout, progress=progress)
        else:
            res = inspect_helper.wechat_key_scan(timeout=timeout, progress=progress)
        ok = bool(res.get("ok"))
        _wx_job_update(
            job_id, state="done" if ok else "failed",
            stage="完成" if ok else "未取到",
            message=str(res.get("message") or ""), result=res,
        )
        with STATE_LOCK:
            CONSOLE_LOGS.append({
                "time": datetime.datetime.now().strftime("%H:%M:%S"),
                "level": "SUCCESS" if ok else "WARN",
                "msg": f"[微信密钥] {res.get('message')}",
            })
    except Exception as exc:  # noqa: BLE001
        _wx_job_update(
            job_id, state="failed", stage="异常",
            message=f"{type(exc).__name__}: {exc}",
            result={"ok": False, "message": f"抓取任务异常: {exc}"},
        )

# 采集互斥：与 run.py 的 SingleInstanceLock 争同一个文件、同一字节区间
EXTRACT_LOCK = threading.Lock()
CONFIG_LOCK = threading.Lock()
STOP_EVENT = threading.Event()
_LOCK_FP = None


def _acquire_instance_lock() -> bool:
    """真正的跨进程互斥（Windows 用 msvcrt，Unix/Linux 用 fcntl 锁文件首字节）。"""
    global _LOCK_FP
    fp = None
    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        fp = open(LOCK_PATH, "a+b")
        if LOCK_PATH.stat().st_size == 0:
            fp.write(b"\0")
            fp.flush()
        fp.seek(0)
        if os.name == "nt":
            import msvcrt  # noqa: PLC0415
            msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl  # noqa: PLC0415
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LOCK_FP = fp
        return True
    except (OSError, ImportError):
        if fp is not None:
            try:
                fp.close()
            except OSError:
                pass
        return False


def _release_instance_lock() -> None:
    global _LOCK_FP
    fp, _LOCK_FP = _LOCK_FP, None
    if fp is None:
        return
    try:
        fp.seek(0)
        if os.name == "nt":
            import msvcrt  # noqa: PLC0415
            msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl  # noqa: PLC0415
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
    except (OSError, ImportError):
        pass
    finally:
        try:
            fp.close()
        except OSError:
            pass


def is_instance_locked() -> bool:
    """真实探测当前是否有进程持有采集锁（非仅判断文件存在）。"""
    if _LOCK_FP is not None:
        return True
    if not LOCK_PATH.exists():
        return False
    fp = None
    try:
        fp = open(LOCK_PATH, "a+b")
        fp.seek(0)
        if os.name == "nt":
            import msvcrt  # noqa: PLC0415
            msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
            fp.seek(0)
            msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl  # noqa: PLC0415
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        return False
    except (OSError, ImportError):
        return True
    finally:
        if fp is not None:
            try:
                fp.close()
            except OSError:
                pass

# 核心 6 大业务流水线任务定义（只存静态信息，状态/进度由 build_tasks() 从真实文件推导）
TASK_DEFS = [
    ("TSK-A101", "[实施] 扫描本机 QQ 目录并锁定主 UIN 账号", "tools/admin_init.py", "高"),
    ("TSK-A102", "[实施] 抓取 SQLCipher 16位密钥并锁死 NTQQ 升级", "tools/lock_qq_update.py", "紧急"),
    ("TSK-B201", "[采集] 多平台快照安全拷贝与只读增量解密抽取", "tools/channel_collector.py", "高"),
    ("TSK-B202", "[解析] 富文本脱敏去噪与跨平台真实昵称/群名映射", "tools/msgparse & qqnames", "普通"),
    ("TSK-B301", "[素材] 待办素材原子落盘与多渠道时钟游标安全推进", "work/待办素材.json", "高"),
    ("TSK-B401", "[提炼] 唤醒 WorkBuddy 智能提炼与企业微信卡片摘要推送", "output/todo-*.md & 企微", "高"),
]

CONSOLE_LOGS: list[dict] = [
    {"time": datetime.datetime.now().strftime("%H:%M:%S"), "level": "SYSTEM",
     "msg": "监控台已启动。本控制台只显示真实事件：配网/采集/控制指令的返回值。"}
]


def _safe_int(val, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _resolve_safe_key_file(file_val: str) -> Path | None:
    """限制外部传入的密钥文件路径必须位于程序目录或工作目录内，防止任意文件读取。"""
    text = (file_val or "").strip()
    if not text or any(c in text for c in ("\r", "\n", "\x00")):
        return None
    try:
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = PKG_ROOT / candidate
        if candidate.is_symlink():
            return None
        resolved = candidate.resolve()
        for allowed_root in (PKG_ROOT.resolve(), WORK_DIR.resolve()):
            try:
                if resolved.is_relative_to(allowed_root):
                    return resolved
            except AttributeError:
                if str(resolved).startswith(str(allowed_root)):
                    return resolved
    except OSError:
        return None
    return None


def _mask(secret: str) -> str:
    """密钥脱敏。绝不再把 .env 里的明文密钥经 HTTP 返回给浏览器。"""
    if not secret:
        return ""
    if len(secret) <= 6:
        return secret[0] + "*" * (len(secret) - 1)
    return f"{secret[:3]}{'*' * (len(secret) - 6)}{secret[-3:]}"


# QQ 密钥向导：单飞 + 就绪即复用
#
# 旧实现每次点按钮都无条件以管理员权限拉起一个取密钥向导，而且：
#   · 向导会**关掉正在运行的 QQ**、静态分析 wrapper.node 约 1 分钟、再要求点登录；
#   · 前端轮询用 cfg.env.QQ_DBKEY 判断成功，但 /api/config 根本不返回 env，
#     于是即便密钥早已抓取成功，界面也永远等不到"成功"，用户只能再点一次；
#   · 于是"每点一次就重启一次 QQ"，看起来像"重复抓取/永远缺少密钥"。
# 现在：已经能解密就直接告诉用户"无需重抓"，同一时刻只允许一个向导在跑。
QQ_JOB_LOCK = threading.Lock()
QQ_JOB: dict = {"state": "idle", "started": 0.0, "message": ""}


def _qq_key_ready(env: dict, deep: bool = False) -> tuple[bool, str]:
    """QQ 这条链路现在通不通。

    默认走快检（密钥形状 + 库文件 + 表结构），走一次拷贝解密要几秒到几十秒，
    只有用户主动点「立即采集/重新校验」时才值得付这个代价（deep=True）。
    """
    key = (env.get("QQ_DBKEY") or "").strip()
    db_text = (env.get("QQ_DB_PATH") or "").strip()
    if not key:
        return False, "密钥为空"
    if len(key) != 16:
        return False, f"密钥长度 {len(key)}，应为 16"
    if not db_text or not Path(db_text).exists():
        return False, "数据库路径不存在"
    if not _qq_schema_ready():
        return False, "表结构未固化（config/schema.json 缺失或没有可用消息表）"
    if not deep:
        return True, "密钥与表结构就绪"
    try:
        import qqdb  # noqa: PLC0415

        profile = env.get("QQ_CIPHER_PROFILE") or None
        if profile and profile not in qqdb.PROFILE_NAMES:
            profile = None      # 旧版本写进去的伪 profile（如 profile_1）不再采信
        tmp = WORK_DIR / ".tmp_db_verify"     # 独立目录：绝不和采集共用工作区副本
        shutil.rmtree(tmp, ignore_errors=True)
        con, used, _ = qqdb.open_encrypted(Path(db_text), tmp, key, profile)
        try:
            n = con.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
        finally:
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(tmp, ignore_errors=True)
        return (n > 0), (f"密钥实测可用（参数 {used}）" if n else "库里没有表")
    except Exception as exc:  # noqa: BLE001
        return False, str(exc).splitlines()[0]


def _qq_schema_ready() -> bool:
    path = CONFIG_DIR / "schema.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return bool(data.get("tables"))
    except (OSError, ValueError):
        return False


def _material_meta() -> dict:
    """读 work/待办素材.json 的 meta，读不到就给空 dict（不臆造默认值）。"""
    if not MATERIAL_PATH.exists():
        return {}
    try:
        data = json.loads(MATERIAL_PATH.read_text(encoding="utf-8", errors="replace"))
        meta = data.get("meta")
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def _latest_todo_file() -> Path | None:
    try:
        files = sorted(OUT_DIR.glob("todo-*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    return files[0] if files else None


def _count_todo_items(path: Path | None) -> int:
    if path is None:
        return 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return sum(1 for line in text.splitlines() if line.lstrip().startswith(("- ", "* ")))


def _age_hours(ts_text: str) -> float | None:
    try:
        dt = datetime.datetime.strptime(str(ts_text), "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None
    return round((datetime.datetime.now() - dt).total_seconds() / 3600, 1)


_WX_DIAG_CACHE = {"ts": 0.0, "data": {}}

def _cached_wx_diag(env: dict, ttl: float = 15.0) -> dict:
    global _WX_DIAG_CACHE
    now = time.time()
    if now - _WX_DIAG_CACHE["ts"] < ttl and _WX_DIAG_CACHE["data"]:
        return _WX_DIAG_CACHE["data"]
    try:
        import wxkey as _wxkey  # noqa: PLC0415
        res = _wxkey.diagnose(env)
        _WX_DIAG_CACHE = {"ts": now, "data": res}
        return res
    except Exception:
        return {}

def build_tasks() -> list[dict]:
    """把 6 个阶段的状态从「真实产物」推导出来。

    旧实现是后台线程每 3.5 秒给 running 的任务随机 +1~3 进度、随机把 queued
    变成 running，并往日志里塞伪造的成功事件。监控台因此永远显示"正在推进"，
    与磁盘上到底有没有产出完全脱钩 —— 对一个主打「不臆造」的交付物来说，
    这比功能缺失更伤。现在每个阶段都必须有可验证的落盘证据才算 complete。
    """
    env = load_env_dict()
    meta = _material_meta()
    notes = meta.get("notes") or []
    todo_file = _latest_todo_file()

    qq_locked = bool(env.get("QQ_TARGET_UIN"))
    key_ok = bool(env.get("QQ_DBKEY"))
    material_ok = MATERIAL_PATH.exists() and bool(meta)
    cutoff_age = _age_hours(meta.get("cutoff_time", "")) if material_ok else None
    fresh = cutoff_age is not None and cutoff_age <= 2.0
    material_failed = str(meta.get("status", "")).upper() in ("FAIL", "PARTIAL")

    if material_ok and material_failed:
        b201 = (50, "paused")
    elif material_ok and fresh:
        b201 = (100, "complete")
    elif material_ok:
        b201 = (60, "running")
    else:
        b201 = (0, "queued")

    if not material_ok:
        b202 = (0, "queued")
    elif notes:
        b202 = (80, "running")
    else:
        b202 = (100, "complete")

    state = {
        "TSK-A101": (100, "complete") if qq_locked else (0, "queued"),
        "TSK-A102": (100, "complete") if key_ok else (0, "queued"),
        "TSK-B201": b201,
        "TSK-B202": b202,
        "TSK-B301": (100, "complete") if material_ok else (0, "queued"),
        "TSK-B401": (100, "complete") if todo_file else ((30, "running") if material_ok else (0, "queued")),
    }
    return [
        {
            "id": tid,
            "name": name,
            "module": module,
            "priority": prio,
            "progress": state[tid][0],
            "status": state[tid][1],
        }
        for tid, name, module, prio in TASK_DEFS
    ]

def load_env_dict() -> dict[str, str]:
    env = {}
    if ENV_PATH.exists():
        try:
            with open(ENV_PATH, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env[k.strip()] = v.strip()
        except Exception:
            pass
    return env

def save_env_dict(updates: dict[str, str]) -> None:
    sensitive_keys = {
        "QQ_DBKEY", "WX_DBKEY", "FEISHU_APP_SECRET",
        "DINGTALK_CLIENT_SECRET", "WECOM_SECRET"
    }
    current = load_env_dict()
    clean_updates = {}
    for k, v in updates.items():
        v_str = str(v).strip()
        if k in sensitive_keys:
            # 前端只会拿到脱敏值（abc****xyz）。真正的密钥里不可能有 '*'，
            # 所以凡是带星号、或等于当前脱敏结果、或空串，一律视为"没改"，直接丢弃。
            # 旧版只挡了"与当前脱敏值完全相同"这一种，长度一变就会把星号写进 .env，
            # 之后每轮都报"密钥不可用"，用户只能反复重抓——这就是重复抓取的来源之一。
            if not v_str or "*" in v_str or v_str == _mask(current.get(k, "")):
                continue
        clean_updates[k] = v_str
    common.save_env(clean_updates, ENV_PATH)

def load_active_platforms() -> dict:
    if ACTIVE_PLATFORMS_FILE.exists():
        try:
            return json.loads(ACTIVE_PLATFORMS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "config_mode": "",
        "enabled_platforms": ["QQ", "WeCom"],
        "updated_at": ""
    }

def save_active_platforms(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # 界面保存只传 config_mode + enabled_platforms，别把手工声明的
    # optional_platforms 顺手抹掉（否则企微一保存又会开始阻塞整轮）。
    if "optional_platforms" not in data:
        try:
            old = json.loads(ACTIVE_PLATFORMS_FILE.read_text(encoding="utf-8"))
            if old.get("optional_platforms"):
                data["optional_platforms"] = old["optional_platforms"]
        except Exception:
            pass
    data["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tmp_cfg = ACTIVE_PLATFORMS_FILE.with_name(
        f"{ACTIVE_PLATFORMS_FILE.name}.tmp_{os.getpid()}_{threading.get_ident()}_{time.time_ns()}"
    )
    try:
        with open(tmp_cfg, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_cfg, ACTIVE_PLATFORMS_FILE)
    finally:
        if tmp_cfg.exists():
            try:
                tmp_cfg.unlink()
            except OSError:
                pass

def save_config_transaction(mode: str, enabled: list[str], params: dict) -> None:
    """串行保存两个配置文件；任一步失败都恢复调用前快照。"""
    with CONFIG_LOCK:
        previous_cfg = ACTIVE_PLATFORMS_FILE.read_bytes() if ACTIVE_PLATFORMS_FILE.exists() else None
        previous_env = ENV_PATH.read_bytes() if ENV_PATH.exists() else None

        def restore(path: Path, content: bytes | None) -> None:
            if content is None:
                if path.exists():
                    path.unlink()
            else:
                common.atomic_write_text(path, content.decode("utf-8", errors="replace"))

        try:
            save_active_platforms({"config_mode": mode, "enabled_platforms": enabled})
            if params:
                save_env_dict(params)
        except Exception:
            rollback_errors: list[str] = []
            for path, content in ((ACTIVE_PLATFORMS_FILE, previous_cfg), (ENV_PATH, previous_env)):
                try:
                    restore(path, content)
                except OSError as exc:
                    rollback_errors.append(f"{path.name}: {exc}")
            if rollback_errors:
                raise RuntimeError("配置保存失败且回滚未完成: " + "; ".join(rollback_errors)) from None
            raise


def get_cursor_time() -> str:
    if CURSOR_PATH.exists():
        try:
            val = float(CURSOR_PATH.read_text().strip())
            return datetime.datetime.fromtimestamp(val).strftime("%H:%M:%S")
        except Exception:
            pass
    # 不再返回写死的 "22:30:01"：没有游标就如实显示没有
    return "--:--:--"


# 真实硬件指标（纯标准库：GetSystemTimes / GlobalMemoryStatusEx）
#
# 旧实现直接 random.uniform() 编造 CPU/内存占用（Windows 上 /proc 不存在，
# 两个 if 全部跳过，永远走随机数分支）。监控台因此显示一条漂亮的假曲线。
class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _MEMSTATUS(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


_CPU_SAMPLE: dict = {"percent": None}


def _ft_to_int(v: "_FILETIME") -> int:
    return (v.dwHighDateTime << 32) | v.dwLowDateTime


def _read_sys_times() -> tuple[int, int, int] | None:
    try:
        idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
        ok = ctypes.windll.kernel32.GetSystemTimes(  # type: ignore[attr-defined]
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        )
        if not ok:
            return None
        return _ft_to_int(idle), _ft_to_int(kernel), _ft_to_int(user)
    except Exception:
        return None


def _read_mem_percent() -> float | None:
    try:
        st = _MEMSTATUS()
        st.dwLength = ctypes.sizeof(_MEMSTATUS)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):  # type: ignore[attr-defined]
            return None
        return float(st.dwMemoryLoad)
    except Exception:
        return None


def background_sampler_loop() -> None:
    """每 2 秒采一次全机 CPU/内存，供 /api/stats 读取。

    用两次 GetSystemTimes 的差值算占用（kernel 时间含 idle，所以 busy=total-idle）。
    只做采样，不改任何业务状态 —— 原来那个"随机推进流水线"的线程已删除。
    """
    prev = _read_sys_times()
    while not STOP_EVENT.wait(2.0):
        cur = _read_sys_times()
        if prev and cur:
            d_idle = cur[0] - prev[0]
            d_total = (cur[1] - prev[1]) + (cur[2] - prev[2])
            if d_total > 0:
                pct = (1.0 - d_idle / d_total) * 100.0
                _CPU_SAMPLE["percent"] = round(max(0.0, min(100.0, pct)), 1)
        elif cur is None:
            _CPU_SAMPLE["percent"] = None
        prev = cur


def get_system_hardware_stats() -> dict:
    cpu_val = _CPU_SAMPLE["percent"]
    mem_val = _read_mem_percent()

    wal_size_mb = None
    tmp_wal = WORK_DIR / "tmp_db" / "nt_msg.db-wal"
    if tmp_wal.exists():
        try:
            wal_size_mb = round(tmp_wal.stat().st_size / (1024 * 1024), 1)
        except OSError:
            wal_size_mb = None

    mat_size_kb = None
    if MATERIAL_PATH.exists():
        try:
            mat_size_kb = max(1, round(MATERIAL_PATH.stat().st_size / 1024))
        except OSError:
            mat_size_kb = None

    meta = _material_meta()
    age = _age_hours(meta.get("cutoff_time", "")) if meta else None

    return {
        "cpu_percent": cpu_val,
        "mem_percent": mem_val,
        "cpu_spec": "全机 CPU 占用（GetSystemTimes 差值，2s 采样）" if cpu_val is not None else "不可用（非 Windows 或采样未就绪）",
        "mem_spec": "全机物理内存占用（GlobalMemoryStatusEx）" if mem_val is not None else "不可用",
        "wal_size": f"{wal_size_mb} MB" if wal_size_mb is not None else "无快照",
        "material_size": f"{mat_size_kb} KB" if mat_size_kb is not None else "未生成",
        # 真实素材新鲜度；没有素材就如实返回 None
        "stale_hours": f"{age}h" if age is not None else None,
    }

def _trim_logs() -> None:
    """日志只保留最近 120 条，防内存无上限增长（调用方需持有 STATE_LOCK）。"""
    global CONSOLE_LOGS
    if len(CONSOLE_LOGS) > 120:
        CONSOLE_LOGS = CONSOLE_LOGS[-60:]


def inspect_channels_environment(env_path: Path | None = None, force: bool = False) -> dict:
    """多平台环境体检。出错就明确抛出去（由 HTTP 层返回 500 + 原因），
    旧实现把异常吞成 {"error": ...} 还回 200，前端拿到空卡片只能一直显示"检测中"。"""
    import health as inspect_helper
    return inspect_helper.inspect_channels_environment(env_path, force=force)

def handle_wechat_key_sync(key_file_path: Path | None = None, key_str: str = "", env_path: Path | None = None) -> dict:
    import health as inspect_helper
    return inspect_helper.handle_wechat_key_sync(key_file_path, key_str, env_path)

def test_feishu_connection(env_dict: dict | None = None) -> dict:
    import health as inspect_helper
    return inspect_helper.test_feishu_connection(env_dict)

def test_dingtalk_connection(env_dict: dict | None = None) -> dict:
    import health as inspect_helper
    return inspect_helper.test_dingtalk_connection(env_dict)

def test_wecom_webhook(webhook_url: str = "") -> dict:
    import health as inspect_helper
    return inspect_helper.test_wecom_webhook(webhook_url)

def import_inbox_messages(channel: str, messages: list[dict] | str) -> dict:
    import health as inspect_helper
    return inspect_helper.import_inbox_messages(channel, messages)


class ProcessConsoleHTTPHandler(SimpleHTTPRequestHandler):

    def setup(self):
        super().setup()
        # 防止声明了 Content-Length 却永不发送完整请求体的慢连接长期占用线程。
        self.connection.settimeout(15.0)

    def _validate_host(self) -> tuple[bool, str]:
        """校验 Host 头，防御 DNS Rebinding 攻击。"""
        host = self.headers.get("Host", "").strip()
        try:
            host_name = urlparse(f"http://{host}").hostname
        except ValueError:
            host_name = None
        allowed_hosts = {"127.0.0.1", "localhost", "::1"}
        if HOST not in ("0.0.0.0", "::"):
            allowed_hosts.add(HOST)
        if host_name not in allowed_hosts:
            return False, "仅允许通过本机地址访问控制台"
        return True, ""

    def _send_security_headers(self, no_store: bool = False) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if no_store:
            self.send_header("Cache-Control", "no-store")

    def _validate_post_source(self) -> tuple[bool, int, str]:
        ok_host, msg_host = self._validate_host()
        if not ok_host:
            return False, 403, msg_host
        host = self.headers.get("Host", "").strip()
        origin = self.headers.get("Origin")
        if origin and origin.rstrip("/") != f"http://{host}":
            return False, 403, "拒绝跨站写请求"
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return False, 415, "写操作仅接受 application/json"
        return True, 200, ""

    def _cors_origin(self) -> str | None:
        """只对本机来源回显 Origin。

        旧实现无条件 `Access-Control-Allow-Origin: *`，而 /api/config 会返回
        .env 里的明文密钥 —— 用户只要用浏览器打开过任意一个恶意页面，
        那个页面就能 fetch('http://127.0.0.1:8000/api/config') 读走 QQ_DBKEY。
        现在：非同源不回 CORS 头，浏览器端读不到响应体。
        """
        origin = self.headers.get("Origin")
        if not origin:
            return None
        try:
            u = urlparse(origin)
        except ValueError:
            return None
        if u.scheme in ("http", "https") and (u.hostname or "") in ("127.0.0.1", "localhost", "::1"):
            return origin
        return None

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers(no_store=True)
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_HEAD(self):
        """拦截 HEAD 请求，防止基类 SimpleHTTPRequestHandler 探测本地磁盘文件。"""
        ok_host, _ = self._validate_host()
        if not ok_host:
            self.send_error(403, "Forbidden")
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            if HTML_FILE_PATH.exists():
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(HTML_FILE_PATH.stat().st_size))
                self.end_headers()
            else:
                self.send_error(404, "index.html not found")
            return
        self.send_error(404, "Endpoint not found")

    def do_OPTIONS(self):
        self.send_response(200)
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _guarded(self, handler):
        """把未预料的异常变成一条 JSON 500，而不是"空响应"。

        旧行为：处理函数里一旦抛异常，连接被直接掐断，浏览器只会看到
        "Failed to fetch / 响应不是合法 JSON"，把人引向"服务挂了"的错误结论。
        """
        try:
            handler()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            raise
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            try:
                self.send_json({"status": "error",
                                "message": f"控制台内部错误：{type(exc).__name__}: {exc}"}, 500)
            except Exception:  # noqa: BLE001
                pass

    def do_GET(self):
        self._guarded(self._handle_get)

    def _handle_get(self):
        ok_host, msg_host = self._validate_host()
        if not ok_host:
            self.send_json({"status": "error", "message": msg_host}, 403)
            return
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            if HTML_FILE_PATH.exists():
                with open(HTML_FILE_PATH, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self._send_security_headers()
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_error(404, "index.html not found")
            return

        if path == "/api/ping":
            # 单实例探测用：只要能回这个，就说明端口上是本程序的控制台。
            # 同时给出 pid + 解释器能力，让启动器能判断「这个实例能不能干 QQ 的活」。
            # 旧版只回 ok/pid：桌面 App 入口于是会安心复用任何一个应答 8000 的
            # 进程——包括用系统 Python 起、没有 sqlcipher3 的那个，然后 QQ 必崩。
            snap = runtime_env.runtime_snapshot()
            self.send_json({
                "ok": True,
                "app": "todo-console",
                "pid": os.getpid(),
                "port": self.server.server_port,
                "started": START_TIME,
                "root": snap["root"],
                "python": snap["python"],
                "python_version": snap["version"],
                "capable": snap["capable"],
                "capabilities": snap["capabilities"],
            })
            return

        if path == "/api/qq/status":
            # 前端轮询这个：抓取成功 = 密钥可用 + 表结构已固化（缺一不可，缺表结构一条消息也读不出来）
            query = parse_qs(urlparse(self.path).query)
            deep = str((query.get("deep") or ["0"])[0]).lower() in ("1", "true", "yes")
            env_q = load_env_dict()
            ready, why = _qq_key_ready(env_q, deep=deep)
            self.send_json({
                "ok": ready,
                "key_configured": bool(env_q.get("QQ_DBKEY")),
                "key_masked": _mask(env_q.get("QQ_DBKEY", "")),
                "schema_ready": _qq_schema_ready(),
                "profile": env_q.get("QQ_CIPHER_PROFILE", ""),
                "detail": why,
                "runtime": runtime_env.runtime_snapshot(),
            })
            return

        if path == "/api/status":
            now_t = time.time()
            if _STATUS_CACHE["data"] is not None and (now_t - _STATUS_CACHE["ts"]) < 3.0:
                self.send_json(_STATUS_CACHE["data"])
                return
            env = load_env_dict()
            cfg = load_active_platforms()
            uin = env.get("QQ_TARGET_UIN") or "未配置"
            db_path = env.get("QQ_DB_PATH") or ""
            if not db_path or not Path(db_path).exists():
                try:
                    from health import locate_qq_db
                    auto_uin, auto_path, ok = locate_qq_db(env)
                    if ok:
                        db_path = auto_path
                        if uin == "未配置":
                            uin = auto_uin
                except Exception:
                    pass
            # 去掉原来的 +9918：那是为了让"运行时长"好看而写死的偏移，
            # 会让用户以为进程已经稳定跑了两个多小时。
            elapsed_sec = max(0, int(time.time() - START_TIME))
            h = str(elapsed_sec // 3600).zfill(2)
            m = str((elapsed_sec % 3600) // 60).zfill(2)
            s = str(elapsed_sec % 60).zfill(2)

            tasks = build_tasks()
            complete_tasks = sum(1 for t in tasks if t["status"] == "complete")
            overall_progress = round(sum(t["progress"] for t in tasks) / len(tasks), 1) if tasks else 0.0

            mode_str = cfg.get("config_mode", "")
            is_configured = bool(mode_str)
            enabled_list = cfg.get("enabled_platforms", [])

            meta = _material_meta()
            todo_file = _latest_todo_file()
            # 以下四个数字全部来自磁盘上的真实产物，不再写死 8 / 38 / 0
            material_count = _safe_int(meta.get("message_count"), 0) if meta else 0
            todo_count = _count_todo_items(todo_file)
            alarm_count = len(list(OUT_DIR.glob("异常说明-*.txt"))) if OUT_DIR.exists() else 0
            mat_status = str(meta.get("status", "")).upper() if meta else ""
            mat_age = _age_hours(meta.get("cutoff_time", "")) if meta else None

            # QQ 真实状态判定（不臆造、不假就绪）
            qq_key_ok = bool(env.get("QQ_DBKEY"))
            qq_db_exists = bool(db_path and Path(db_path).exists())
            if "QQ" not in enabled_list:
                qq_status = "未启用"
            elif not uin or uin == "未配置":
                qq_status = "未检测到账号"
            elif not qq_db_exists:
                qq_status = "未定位数据库"
            elif not qq_key_ok:
                qq_status = "待配置密钥 (NO_KEY)"
            else:
                schema_ok = (CONFIG_DIR / "schema.json").exists()
                qq_status = "在线监听 (NTQQ 只读解密就绪)" if schema_ok else "密钥已配置 (待固化表结构)"

            channels_status = {
                "qq": qq_status,
                "wechat": "本地解密就绪" if ("WeChat" in enabled_list and env.get("WX_DBKEY")) else ("未启用" if "WeChat" not in enabled_list else "待填密钥"),
                "feishu": "WebSocket长连接就绪" if ("Feishu" in enabled_list and env.get("FEISHU_APP_ID")) else ("未启用" if "Feishu" not in enabled_list else "待填凭证"),
                "dingtalk": "Stream模式长连接就绪" if ("DingTalk" in enabled_list and env.get("DINGTALK_CLIENT_ID")) else ("未启用" if "DingTalk" not in enabled_list else "待填凭证"),
                "wecom": "WorkBuddy 官方连接器就绪" if "WeCom" in enabled_list else "未启用"
            }
            # 通道灯必须反映"现在到底能不能采"，不能写死文案。
            inbox = WORK_DIR / "inbox"
            _wx_diag = _cached_wx_diag(env, ttl=15.0)
            _wx_storage = bool(_wx_diag.get("wechat_installed"))
            _wx_mirror = bool(_wx_diag.get("mirror_ready"))
            channels_status["wechat"] = (
                "本地解密就绪（读明文镜像）" if ("WeChat" in enabled_list and env.get("WX_DBKEY") and _wx_mirror)
                else "密钥已配置，待首次解密" if ("WeChat" in enabled_list and env.get("WX_DBKEY") and _wx_storage)
                else "未找到微信数据目录" if ("WeChat" in enabled_list and not _wx_storage)
                else "待取密钥" if "WeChat" in enabled_list
                else "未启用"
            )
            channels_status["feishu"] = (
                "Open API 拉取就绪" if ("Feishu" in enabled_list and env.get("FEISHU_APP_ID") and env.get("FEISHU_APP_SECRET"))
                else "待填凭证" if "Feishu" in enabled_list
                else "未启用"
            )
            channels_status["dingtalk"] = (
                "收件箱模式（需会话存档/回调写入）" if ("DingTalk" in enabled_list and env.get("DINGTALK_CLIENT_ID"))
                else "待填凭证" if "DingTalk" in enabled_list
                else "未启用"
            )
            channels_status["wecom"] = (
                "收件箱模式（由连接器写入）" if "WeCom" in enabled_list else "未启用"
            )
            inbox_files = sorted(p.name for p in inbox.glob("*.jsonl")) if inbox.exists() else []

            data = {
                "project": "待办自动汇总助手",
                "console": "待办自动汇总助手 · 程序进程监控台",
                "author": "Open Source Community",
                "status": "RUNNING" if (IS_SCHEDULER_RUNNING and is_configured) else ("WAITING_CONFIG" if not is_configured else "PAUSED"),
                "config_mode": mode_str,
                "is_configured": is_configured,
                "enabled_platforms": enabled_list,
                "uin": uin,
                "db_path": env.get("QQ_DB_PATH") or (f"<Tencent Files>\\{uin}\\nt_qq\\nt_db\\nt_msg.db" if uin != "未配置" else "待配置"),
                "key_configured": bool(env.get("QQ_DBKEY")),
                "lock_active": is_instance_locked(),
                "cursor": get_cursor_time(),
                "todo_count": todo_count,
                "material_count": material_count,
                "material_status": mat_status,
                "material_age_hours": mat_age,
                "total_progress": overall_progress if is_configured else 0,
                "completed_tasks": complete_tasks if is_configured else 0,
                "alarm_count": alarm_count,
                "elapsed_time": f"{h}:{m}:{s}",
                "channels": channels_status,
                "inbox_files": inbox_files,
                "schedule": _schedule_brief(),
            }
            _STATUS_CACHE["ts"] = time.time()
            _STATUS_CACHE["data"] = data
            self.send_json(data)
            return

        if path == "/api/config":
            env = load_env_dict()
            cfg = load_active_platforms()
            # 敏感项一律脱敏返回；同时给出 *_set 布尔位供前端显示"已配置/未配置"。
            # 旧实现把 QQ_DBKEY / 各种 Secret 明文回传给浏览器（且 CORS 是 *），
            # 等于把本地数据库密钥暴露给任何能访问 127.0.0.1:8000 的页面。
            sensitive = ("QQ_DBKEY", "WX_DBKEY", "FEISHU_APP_SECRET",
                         "DINGTALK_CLIENT_SECRET", "WECOM_SECRET")
            params = {
                "QQ_TARGET_UIN": env.get("QQ_TARGET_UIN", ""),
                "QQ_DB_PATH": env.get("QQ_DB_PATH", ""),
                "WX_DATA_PATH": env.get("WX_DATA_PATH", ""),
                "FEISHU_APP_ID": env.get("FEISHU_APP_ID", ""),
                "DINGTALK_CLIENT_ID": env.get("DINGTALK_CLIENT_ID", ""),
                "WECOM_CORP_ID": env.get("WECOM_CORP_ID", ""),
                "WECOM_WEBHOOK_URL": env.get("WECOM_WEBHOOK_URL", ""),
                "SUMMARY_HOURS": env.get("SUMMARY_HOURS", "4"),
            }
            for k in sensitive:
                raw = env.get(k, "")
                params[k] = _mask(raw)          # 只给前 3 后 3，中间打星
                params[f"{k}_set"] = bool(raw)  # 前端据此显示"已配置（不回显）"
            data = {
                "config_mode": cfg.get("config_mode", ""),
                "enabled_platforms": cfg.get("enabled_platforms", ["QQ", "WeCom"]),
                "params": params,
            }
            self.send_json(data)
            return

        if path == "/api/tasks":
            self.send_json({"total": len(tasks := build_tasks()), "tasks": tasks})
            return

        if path == "/api/logs":
            with STATE_LOCK:
                logs_snapshot = list(CONSOLE_LOGS)
            self.send_json({"total": len(logs_snapshot), "logs": logs_snapshot})
            return

        if path == "/api/stats":
            self.send_json(get_system_hardware_stats())
            return

        if path == "/api/channels/inspect":
            # force=1：用户点了「重新检测环境」，绕过 8 秒短缓存
            _q = parse_qs(urlparse(self.path).query)
            _force = str((_q.get("force") or ["0"])[0]).lower() in ("1", "true", "yes")
            try:
                self.send_json({"status": "success", "data": inspect_channels_environment(force=_force)})
            except Exception as exc:  # noqa: BLE001
                self.send_json({"status": "error", "message": f"环境检测失败: {exc}"}, 500)
            return

        if path == "/api/schedule":
            self.send_json({"status": "success", "data": sched.status()})
            return

        if path == "/api/wechat/key-job":
            # 前端每 800ms 轮询一次：进度、阶段、最终结果都从这里拿。
            self.send_json({"status": "success", "data": _wx_job_snapshot()})
            return

        if path == "/api/wechat/key-status":
            # 微信通道体检：数据目录在哪、进程在不在、密钥能不能解开当前库
            try:
                import wxkey  # noqa: PLC0415
                env_now = load_env_dict()
                self.send_json({"status": "success", "data": wxkey.diagnose(env_now)})
            except Exception as exc:  # noqa: BLE001
                self.send_json({"status": "error", "message": f"体检失败: {exc}"}, 500)
            return

        if path == "/favicon.ico":
            ico_file = BASE_DIR / "favicon.ico"
            if ico_file.exists():
                with open(ico_file, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/x-icon")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            svg = (
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
                '<rect width="32" height="32" rx="6" fill="#1e293b"/>'
                '<path d="M9 16l5 5 9-10" fill="none" stroke="#10b981" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>'
                '</svg>'
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(svg)))
            self.end_headers()
            self.wfile.write(svg)
            return

        if path == "/favicon.svg":
            svg = (
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
                '<rect width="32" height="32" rx="6" fill="#1e293b"/>'
                '<path d="M9 16l5 5 9-10" fill="none" stroke="#10b981" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>'
                '</svg>'
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(svg)))
            self.end_headers()
            self.wfile.write(svg)
            return

        if path == "/api/wizard/state":
            try:
                import wizard_service  # noqa: PLC0415
                self.send_json({"status": "success", "data": wizard_service.get_wizard_state()})
            except Exception as exc:  # noqa: BLE001
                self.send_json({"status": "error", "message": str(exc)}, 500)
            return

        if path == "/api/wizard/detect":
            try:
                import wizard_service  # noqa: PLC0415
                self.send_json({"status": "success", "data": wizard_service.run_auto_detect()})
            except Exception as exc:  # noqa: BLE001
                self.send_json({"status": "error", "message": str(exc)}, 500)
            return

        if path == "/api/diagnose":
            # 采集自检：一条请求回答"为什么一条消息都没采到"。
            # deep=1 会做一次真实解密并统计窗口内行数（几秒，够快，值得做）。
            query = parse_qs(urlparse(self.path).query)
            deep = str((query.get("deep") or ["1"])[0]).lower() in ("1", "true", "yes")
            try:
                import diagnose_collect  # noqa: PLC0415

                report = diagnose_collect.collect(deep=deep)
                text = diagnose_collect.render_text(report)
                saved = ""
                try:
                    OUT_DIR.mkdir(parents=True, exist_ok=True)
                    p = OUT_DIR / f"采集诊断-{datetime.datetime.now():%Y%m%d-%H%M%S}.txt"
                    p.write_text(text, encoding="utf-8")
                    saved = str(p)
                except OSError:
                    pass
                self.send_json({"status": "success", "data": report, "text": text, "saved_to": saved})
            except Exception as exc:  # noqa: BLE001
                self.send_json({"status": "error", "message": f"自检失败: {exc}"}, 500)
            return

        if path == "/api/artifacts/latest":
            try:
                import wizard_service  # noqa: PLC0415
                self.send_json({"status": "success", "data": wizard_service.get_latest_artifacts()})
            except Exception as exc:  # noqa: BLE001
                self.send_json({"status": "error", "message": str(exc)}, 500)
            return

        if path == "/api/artifacts/file":
            try:
                import wizard_service  # noqa: PLC0415
                query = parse_qs(urlparse(self.path).query)
                filename = query.get("name", [""])[0]
                res = wizard_service.read_output_file(filename)
                if res.get("ok"):
                    self.send_json({"status": "success", "data": res})
                else:
                    self.send_json({"status": "error", "message": res.get("message", "读取失败")}, 404)
            except Exception as exc:  # noqa: BLE001
                self.send_json({"status": "error", "message": str(exc)}, 500)
            return

        self.send_error(404, "Endpoint not found")

    def do_POST(self):
        self._guarded(self._handle_post)

    def _handle_post(self):
        global IS_SCHEDULER_RUNNING, CONSOLE_LOGS
        path = urlparse(self.path).path
        now_time = datetime.datetime.now().strftime("%H:%M:%S")

        allowed, status, message = self._validate_post_source()
        if not allowed:
            self.send_json({"status": "error", "message": message}, status)
            return
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self.send_json({"status": "error", "message": "缺少 Content-Length"}, 411)
            return
        try:
            length = int(raw_length)
        except (ValueError, TypeError):
            self.send_json({"status": "error", "message": "Content-Length 非法"}, 400)
            return
        if length < 0:
            self.send_json({"status": "error", "message": "Content-Length 不能为负数"}, 400)
            return
        if length > 1024 * 1024:
            self.send_json({"status": "error", "message": "Payload too large"}, 413)
            return
        payload = {}
        if length > 0:
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except TimeoutError:
                self.send_json({"status": "error", "message": "读取请求体超时"}, 408)
                return
            except (ValueError, UnicodeDecodeError) as exc:
                self.send_json({"status": "error", "message": f"请求体不是合法 JSON: {exc}"}, 400)
                return
            if not isinstance(payload, dict):
                self.send_json({"status": "error", "message": "请求体必须是 JSON 对象"}, 400)
                return

        # 保存多平台配置与模式
        if path == "/api/config":
            mode = payload.get("config_mode", "")
            enabled = payload.get("enabled_platforms", [])
            params = payload.get("params", {})
            valid_platforms = {"QQ", "WeChat", "Feishu", "DingTalk", "WeCom"}
            allowed_params = {
                "QQ_TARGET_UIN", "QQ_DB_PATH", "QQ_DBKEY", "QQ_CIPHER_PROFILE",
                "WX_DATA_PATH", "WX_DBKEY", "FEISHU_APP_ID", "FEISHU_APP_SECRET",
                "FEISHU_BASE_URL", "FEISHU_MAX_CHATS", "FEISHU_SENDER_MAP",
                "DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET", "WECOM_CORP_ID",
                "WECOM_SECRET", "WECOM_WEBHOOK_URL", "SUMMARY_HOURS",
            }
            if mode not in ("", "base", "all", "custom"):
                self.send_json({"status": "error", "message": "未知配置模式"}, 400)
                return
            if not isinstance(enabled, list) or any(p not in valid_platforms for p in enabled):
                self.send_json({"status": "error", "message": "启用平台必须是受支持的平台数组"}, 400)
                return
            if not isinstance(params, dict):
                self.send_json({"status": "error", "message": "params 必须是 JSON 对象"}, 400)
                return
            for key, value in params.items():
                text = str(value)
                if key not in allowed_params:
                    self.send_json({"status": "error", "message": f"不支持的配置键: {key}"}, 400)
                    return
                if len(text) > 4096 or any(c in text for c in ("\r", "\n", "\x00")):
                    self.send_json({"status": "error", "message": f"配置值非法或过长: {key}"}, 400)
                    return
            enabled = list(dict.fromkeys(enabled))
            try:
                save_config_transaction(mode, enabled, params)
            except Exception as exc:  # noqa: BLE001
                self.send_json({"status": "error", "message": f"配置未保存: {exc}"}, 500)
                return

            with STATE_LOCK:
                if not mode:
                    CONSOLE_LOGS.append({
                        "time": now_time,
                        "level": "WARN",
                        "msg": "用户重置配置模式：当前未选择模式，后台采集已暂停，等待用户重新勾选"
                    })
                else:
                    mode_name = {"base": "默认QQ+企微基础模式", "all": "全平台协同模式", "custom": "自定义接入模式"}.get(mode, mode)
                    CONSOLE_LOGS.append({
                        "time": now_time,
                        "level": "SUCCESS",
                        "msg": f"配置模式已确认: 【{mode_name}】，激活平台: {enabled}，参数已同步至 .env"
                    })
            self.send_json({"status": "success", "config_mode": mode, "enabled_platforms": enabled})
            return

        if path == "/api/channels/inspect":
            try:
                self.send_json({"status": "success",
                                "data": inspect_channels_environment(force=bool(payload.get("force")))})
            except Exception as exc:  # noqa: BLE001
                self.send_json({"status": "error", "message": f"环境检测失败: {exc}"}, 500)
            return

        if path == "/api/wechat/extract-key":
            # action: scan（内存扫描）| hook（重启时挂载抓取，4.1+ 必用）|
            #         paste（粘贴）| sync（从文件导入）| launch（旧向导）
            action = str(payload.get("action") or "").strip().lower()
            key_val = str(payload.get("key") or "").strip()
            file_val = str(payload.get("file") or "").strip()
            import health as inspect_helper
            if action == "launch":
                res = inspect_helper.trigger_wechat_key_extractor()
            elif action == "paste" or key_val:
                safe_key_file = _resolve_safe_key_file(file_val) if file_val else None
                if file_val and safe_key_file is None:
                    self.send_json({"ok": False, "message": "密钥文件路径仅限程序工作目录内，已拒绝访问"}, 400)
                    return
                res = inspect_helper.handle_wechat_key_sync(safe_key_file, key_val)
            elif action in ("scan", "hook", "sync", ""):
                # 长耗时动作一律异步：8 秒的前端超时不再把结果丢掉。
                timeout = 45.0 if action != "hook" else 240.0
                res = _wx_job_start(action or "sync", timeout)
                res["async"] = True
            else:
                res = {"ok": False, "message": f"未知操作: {action}"}

            if not res.get("async"):
                lvl = "SUCCESS" if res.get("ok") else "WARN"
                with STATE_LOCK:
                    CONSOLE_LOGS.append({
                        "time": now_time,
                        "level": lvl,
                        "msg": f"[微信密钥] {res.get('message')}"
                    })
            self.send_json(res)
            return

        if path == "/api/qq/extract-key":
            import health as inspect_helper
            payload_force = bool(payload.get("force"))
            env = load_env_dict()

            # 0) 解释器能力自检：没有 sqlcipher3 时抓半天也解不开，先把真因明确记录
            if not runtime_env.has_all(("sqlcipher3",)):
                res = {
                    "ok": False,
                    "runtime": runtime_env.runtime_snapshot(),
                    "message": (
                        "当前控制台运行在没有 SQLCipher 的解释器上，无法验证/读取 QQ 库。"
                        "请关闭所有控制台窗口后重新双击「启动桌面App.bat」（它会自动切到包内 .venv）。"
                    ),
                }
                self.send_json(res)
                return

            # 1) 已经就绪就别再重启一次 QQ —— 这正是"每次都提示重新抓取"的来源
            ready, why = _qq_key_ready(env)
            if ready and not payload_force:
                res = {
                    "ok": True,
                    "action": "already_ready",
                    "skipped": True,
                    "key_configured": True,
                    "schema_ready": _qq_schema_ready(),
                    "message": f"QQ 密钥已配置且校验通过（{why}），无需重复抓取。若要强制重抓，请带 force=true。",
                }
                with STATE_LOCK:
                    CONSOLE_LOGS.append({"time": now_time, "level": "INFO",
                                         "msg": f"[QQ密钥] 已就绪，跳过重复抓取（{why}）"})
                self.send_json(res)
                return

            # 1.5) 密钥已存在但缺 config/schema.json 时，直接在后台用已保存密钥固化表结构，免去弹窗重抓
            saved_key = (env.get("QQ_DBKEY") or "").strip()
            saved_db = (env.get("QQ_DB_PATH") or "").strip()
            if not payload_force and len(saved_key) == 16 and saved_db and Path(saved_db).exists() and not _qq_schema_ready():
                if EXTRACT_LOCK.acquire(blocking=False):
                    try:
                        import probe  # noqa: PLC0415

                        preferred = env.get("QQ_CIPHER_PROFILE") or None
                        schema = probe.verify_decrypt_and_dump_schema(Path(saved_db), saved_key, preferred)
                        if schema.get("tables") and _qq_schema_ready():
                            used_prof = str((schema.get("source") or {}).get("profile") or preferred or "")
                            if used_prof and used_prof != env.get("QQ_CIPHER_PROFILE"):
                                try:
                                    common.save_env({"QQ_CIPHER_PROFILE": used_prof}, ENV_PATH)
                                except Exception:  # noqa: BLE001
                                    pass
                            _STATUS_CACHE["ts"] = 0.0
                            tbl_cnt = len(schema["tables"])
                            msg = f"QQ 密钥有效，已自动完成表结构固化（识别 {tbl_cnt} 张消息表），无需重新抓取！"
                            with STATE_LOCK:
                                CONSOLE_LOGS.append({"time": now_time, "level": "SUCCESS", "msg": f"[QQ表结构] {msg}"})
                            self.send_json({
                                "ok": True,
                                "action": "schema_repaired",
                                "skipped": True,
                                "key_configured": True,
                                "schema_ready": True,
                                "profile": used_prof,
                                "message": msg,
                            })
                            return
                    except Exception as exc:  # noqa: BLE001
                        with STATE_LOCK:
                            CONSOLE_LOGS.append({
                                "time": now_time,
                                "level": "WARN",
                                "msg": f"[QQ表结构] 使用已有密钥自动固化失败，将唤起取密向导重试: {exc}",
                            })
                    finally:
                        EXTRACT_LOCK.release()

            # 2) 单飞：向导会杀掉 QQ 并静态分析 wrapper.node，绝不能并发拉两个
            with QQ_JOB_LOCK:
                if QQ_JOB.get("state") == "running" and time.time() - float(QQ_JOB.get("started") or 0) < 600:
                    self.send_json({
                        "ok": True, "action": "already_running", "skipped": True,
                        "message": "上一轮 QQ 取密钥向导仍在运行（它会重启 QQ 并等待登录），请先完成那一轮。",
                    })
                    return
                QQ_JOB.update({"state": "running", "started": time.time(), "message": ""})

            try:
                res = inspect_helper.trigger_qq_key_extractor()
            finally:
                # 向导是独立提权进程，这里只解除"入口互斥"；
                # 抓取结果（密钥 + 表结构）由前端轮询 GET /api/qq/status 得到
                with QQ_JOB_LOCK:
                    QQ_JOB["state"] = "launched"

            res["key_configured"] = bool(env.get("QQ_DBKEY"))
            res["schema_ready"] = _qq_schema_ready()
            with STATE_LOCK:
                CONSOLE_LOGS.append({
                    "time": now_time,
                    "level": "INFO" if res.get("ok") else "WARN",
                    "msg": f"[QQ密钥向导] {res.get('message')}"
                })
            self.send_json(res)
            return

        if path == "/api/channels/test-feishu":
            res = test_feishu_connection(payload.get("params"))
            with STATE_LOCK:
                CONSOLE_LOGS.append({
                    "time": now_time,
                    "level": "SUCCESS" if res.get("ok") else "WARN",
                    "msg": f"[飞书连通测试] {res.get('message')} {res.get('tip', '')}".strip()
                })
            self.send_json(res)
            return

        if path == "/api/channels/test-dingtalk":
            res = test_dingtalk_connection(payload.get("params"))
            with STATE_LOCK:
                CONSOLE_LOGS.append({
                    "time": now_time,
                    "level": "SUCCESS" if res.get("ok") else "WARN",
                    "msg": f"[钉钉凭证验证] {res.get('message')} {res.get('tip', '')}".strip()
                })
            self.send_json(res)
            return

        if path == "/api/channels/test-wecom-webhook":
            res = test_wecom_webhook(payload.get("webhook_url", ""))
            with STATE_LOCK:
                CONSOLE_LOGS.append({
                    "time": now_time,
                    "level": "SUCCESS" if res.get("ok") else "WARN",
                    "msg": f"[企微Webhook测试] {res.get('message')}"
                })
            self.send_json(res)
            return

        if path == "/api/channels/import-inbox":
            ch = payload.get("channel", "WeCom")
            msgs = payload.get("messages", [])
            res = import_inbox_messages(ch, msgs)
            with STATE_LOCK:
                CONSOLE_LOGS.append({
                    "time": now_time,
                    "level": "SUCCESS" if res.get("ok") else "WARN",
                    "msg": f"[{ch}收件箱写入] {res.get('message')}"
                })
            self.send_json(res)
            return

        # 触发增量提取
        if path == "/api/extract":
            cfg = load_active_platforms()
            enabled = cfg.get("enabled_platforms", [])
            safe_mode = bool(payload.get("safe_mode"))
            orig_enabled = list(enabled)
            if safe_mode:
                env = load_env_dict()
                ready = []
                skipped = []
                for p in enabled:
                    # 判"就绪"的口径必须一致：所有通道都要求**凭证**存在。
                    # 旧写法对 QQ 用了 `DBKEY or DB_PATH`，只填了库路径没填密钥
                    # 也会被判为就绪，然后必然解密失败 —— 与"安全采集模式
                    # 只跑能跑的平台"的语义正好相反。
                    if p == "QQ" and env.get("QQ_DBKEY") and env.get("QQ_DB_PATH"):
                        ready.append(p)
                    elif p == "WeChat" and env.get("WX_DBKEY"):
                        ready.append(p)
                    elif p == "Feishu" and env.get("FEISHU_APP_ID") and env.get("FEISHU_APP_SECRET"):
                        ready.append(p)
                    elif p in ("WeCom", "DingTalk") and (WORK_DIR / "inbox" / f"{p.lower()}.jsonl").exists():
                        ready.append(p)
                    else:
                        skipped.append(p)
                if not ready:
                    self.send_json({"status": "error", "message": "没有检测到任何已就绪平台，请先配置凭证或提取密钥"}, 400)
                    return
                with STATE_LOCK:
                    CONSOLE_LOGS.append({
                        "time": now_time,
                        "level": "INFO",
                        "msg": f"[安全采集模式] 跳过未配置凭证平台 {skipped}，执行已就绪平台采集: {ready}"
                    })
                enabled = ready
                save_active_platforms({
                    "config_mode": cfg.get("config_mode"),
                    "enabled_platforms": ready,
                })
            if not cfg.get("config_mode"):
                self.send_json({"status": "error", "message": "尚未选择配置模式，请先选择模式并保存"}, 400)
                return

            # 单飞 + 跨进程互斥：连点两次按钮、或定时任务正好在跑，都要挡住。
            # 旧实现每点一次就新起一个线程，没有任何限制，直接踩坏 work/tmp_db。
            if not EXTRACT_LOCK.acquire(blocking=False):
                self.send_json({"status": "busy", "message": "已有采集任务在运行，本轮已跳过"}, 409)
                return
            if not _acquire_instance_lock():
                EXTRACT_LOCK.release()
                self.send_json(
                    {"status": "busy",
                     "message": "已有采集任务在运行（定时任务或另一个窗口），本轮已跳过"},
                    409,
                )
                return

            hours_val = payload.get("hours")
            try:
                hours_float = float(hours_val) if hours_val is not None else None
            except (ValueError, TypeError):
                hours_float = None
            ignore_cur = bool(payload.get("ignore_cursor"))

            def _async_extract():
                t_str = datetime.datetime.now().strftime("%H:%M:%S")
                with STATE_LOCK:
                    CONSOLE_LOGS.append({
                        "time": t_str,
                        "level": "STEP",
                        "msg": f"[1/4] 手动触发多平台采集 (已激活: {enabled})..."
                    })
                try:
                    sys.path.insert(0, str(PKG_ROOT / "tools"))
                    # 与 run.py 一致：只走聚合器，不再"失败就静默改跑 extract_impl"
                    import channel_collector
                    res = channel_collector.run(hours=hours_float, ignore_cursor=ignore_cur)

                    done_str = datetime.datetime.now().strftime("%H:%M:%S")
                    with STATE_LOCK:
                        if res and res.ok:
                            extra = ""
                            broken = (getattr(res, "meta", {}) or {}).get("broken_channels") or []
                            if broken:
                                extra = f"，部分通道未采集: {broken}"

                            if ignore_cur:
                                extra += "（忽略游标回看模式，游标保持未变）"
                            else:
                                data_state = getattr(res, "data_state", "ok")
                                if getattr(res, "cursor_blocked", False) or data_state == "offline":
                                    extra += "（覆盖不完整，游标保持不变）"
                                elif res.count and res.max_ts:
                                    val = common.advance_cursor(res.max_ts)
                                    extra += f"（游标推进至 {common.fmt(val)}）"
                                elif data_state in ("no_new", "only_noise"):
                                    val = common.advance_cursor(res.max_ts or res.until or time.time())
                                    extra += f"（无新待办，游标推进至 {common.fmt(val)}）"

                            CONSOLE_LOGS.append({
                                "time": done_str,
                                "level": "SUCCESS",
                                "msg": f"[3/4] 采集完成: {res.count} 条素材 ({res.status}){extra}"
                            })
                            try:
                                import local_summarizer
                                todo_p = local_summarizer.generate_todo_markdown()
                                if todo_p:
                                    CONSOLE_LOGS.append({
                                        "time": done_str,
                                        "level": "SUCCESS",
                                        "msg": f"[4/4] 待办清单已生成: {todo_p.name}"
                                    })
                            except Exception:
                                pass
                        else:
                            msg = getattr(res, "message", "未知原因")
                            CONSOLE_LOGS.append({
                                "time": done_str,
                                "level": "WARN",
                                "msg": f"[3/4] 采集失败，未产出清单: {msg}"
                            })
                except Exception as exc:  # noqa: BLE001
                    err_str = datetime.datetime.now().strftime("%H:%M:%S")
                    with STATE_LOCK:
                        CONSOLE_LOGS.append({
                            "time": err_str,
                            "level": "ERROR",
                            "msg": f"[!] 采集执行异常: {exc}"
                        })
                finally:
                    if safe_mode:
                        # 安全模式只是"本轮跳过未就绪平台"，跑完必须把用户原来的
                        # 勾选原样写回，否则界面上企业微信会莫名其妙消失。
                        save_active_platforms({
                            "config_mode": cfg.get("config_mode"),
                            "enabled_platforms": orig_enabled,
                        })
                    _release_instance_lock()
                    EXTRACT_LOCK.release()
                    with STATE_LOCK:
                        _trim_logs()

            try:
                threading.Thread(target=_async_extract, daemon=True).start()
            except RuntimeError as exc:
                _release_instance_lock()
                EXTRACT_LOCK.release()
                self.send_json({"status": "error", "message": f"无法启动采集线程: {exc}"}, 500)
                return
            self.send_json({"status": "success", "message": "采集任务已在后台启动"})
            return

        # 暂停/恢复采集调度（真调度：写配置 + 启停线程）
        if path == "/api/control":
            if "enabled" in payload:
                want = bool(payload.get("enabled"))
            elif "action" in payload:
                want = str(payload.get("action")).lower() in ("start", "resume", "on", "enable")
            else:
                want = not sched.load_config().get("enabled")
            st = _scheduler_apply(want)
            with STATE_LOCK:
                CONSOLE_LOGS.append({
                    "time": now_time,
                    "level": "INFO" if want else "WARN",
                    "msg": (f"采集调度已启用，模式 {st['mode']}，下次 {st['next_run'] or '—'}"
                            if want else
                            "采集调度已停用：不会自动采集（手动「立即采集」仍可用）"),
                })
            self.send_json({"status": "success", "is_running": st["enabled"], "schedule": st})
            return

        # 保存调度配置（间隔 / 固定时刻 / 回看小时数）
        if path == "/api/schedule":
            patch: dict = {}
            if "enabled" in payload:
                patch["enabled"] = bool(payload.get("enabled"))
            if "mode" in payload:
                mode = str(payload.get("mode"))
                if mode not in ("interval", "daily"):
                    self.send_json({"status": "error", "message": "mode 只能是 interval 或 daily"}, 400)
                    return
                patch["mode"] = mode
            if "interval_minutes" in payload:
                try:
                    patch["interval_minutes"] = max(1, int(payload.get("interval_minutes")))
                except (TypeError, ValueError):
                    self.send_json({"status": "error", "message": "interval_minutes 必须是整数"}, 400)
                    return
            if "daily_times" in payload:
                times = payload.get("daily_times")
                if not isinstance(times, list) or any(not sched._parse_hhmm(str(t)) for t in times):
                    self.send_json({"status": "error", "message": "daily_times 必须是 HH:MM 数组"}, 400)
                    return
                patch["daily_times"] = [str(t) for t in times]
            if "skip_holidays" in payload:
                patch["skip_holidays"] = bool(payload.get("skip_holidays"))
            if "hours" in payload:
                try:
                    hours = float(payload.get("hours"))
                except (TypeError, ValueError):
                    self.send_json({"status": "error", "message": "hours 必须是数字"}, 400)
                    return
                if not 0.1 <= hours <= 168:
                    self.send_json({"status": "error", "message": "hours 需在 0.1~168 之间"}, 400)
                    return
                patch["hours"] = hours
            st = sched.save_config(patch)
            if st.get("enabled"):
                _scheduler_start()
            else:
                _scheduler_stop()
            with STATE_LOCK:
                CONSOLE_LOGS.append({
                    "time": now_time, "level": "SUCCESS",
                    "msg": f"[定时调度] 配置已保存: {json.dumps(patch, ensure_ascii=False)}",
                })
            self.send_json({"status": "success", "data": sched.status()})
            return

        # 清空日志
        if path == "/api/logs/clear":
            with STATE_LOCK:
                CONSOLE_LOGS = [{
                    "time": now_time,
                    "level": "SYSTEM",
                    "msg": "控制台日志已清屏，监听队列继续待命..."
                }]
            self.send_json({"status": "success"})
            return

        # 唤醒 外部协作引擎
        if path == "/api/workbuddy":
            ok = _launch_workbuddy()
            with STATE_LOCK:
                CONSOLE_LOGS.append({
                    "time": now_time,
                    "level": "SUCCESS" if ok else "WARN",
                    "msg": "已向系统广播 workbuddy:// 协议并尝试全盘定位启动 WorkBuddy" if ok else "未能在本机常见路径定位到 WorkBuddy.exe，请确认已安装或在桌面手动打开"
                })
            self.send_json({"status": "success" if ok else "warning", "message": "WorkBuddy triggered"})
            return

        # 任务控制
        if path == "/api/tasks/toggle":
            task_id = payload.get("id")
            # 阶段状态现在是从落盘产物推导出来的（见 build_tasks），
            # 不再是一个可以被"挂起/重置"的内存对象。这里只如实告知，
            # 不假装把某个阶段从 100% 拨回 0%（旧实现正是这么做的，
            # 操作完流水线看起来真的重新跑了，实际什么都没发生）。
            exists = any(tid == task_id for tid, *_ in TASK_DEFS)
            with STATE_LOCK:
                CONSOLE_LOGS.append({
                    "time": now_time,
                    "level": "INFO" if exists else "WARN",
                    "msg": (
                        f"阶段 [{task_id}] 状态由真实产物决定，不能手动重置；"
                        "如需重跑请用「立即采集」"
                        if exists else
                        f"未知阶段 [{task_id}]"
                    ),
                })
            if exists:
                self.send_json({"status": "unsupported", "task_id": task_id,
                                "message": "阶段状态由真实产物推导，请用「立即采集」重新执行"})
            else:
                self.send_json({"status": "error", "message": "Task not found"}, 404)
            return

        # ------------------ 桌面 App 与快捷方式管理接口 ------------------
        if path == "/api/app/uninstall":
            try:
                import uninstaller  # noqa: PLC0415

                _scheduler_stop()
                res = uninstaller.full_uninstall(purge_env=True)
                self.send_json({"status": "success", "data": res})
                if res.get("ok"):
                    _schedule_uninstall_exit()
            except Exception as exc:  # noqa: BLE001
                self.send_json({"status": "error", "message": str(exc)}, 500)
            return

        if path == "/api/app/create-shortcut":
            try:
                import desktop_app  # noqa: PLC0415
                res = desktop_app.install_shortcuts()
                self.send_json({"status": "success" if res.get("ok") else "error", "data": res})
            except Exception as exc:  # noqa: BLE001
                self.send_json({"status": "error", "message": str(exc)}, 500)
            return

        # ------------------ 新手引导向导相关接口 ------------------
        if path == "/api/wizard/state":
            try:
                import wizard_service  # noqa: PLC0415
                saved = wizard_service.save_wizard_state(payload)
                self.send_json({"status": "success", "data": saved})
            except Exception as exc:  # noqa: BLE001
                self.send_json({"status": "error", "message": str(exc)}, 500)
            return

        if path == "/api/wizard/auto-detect":
            try:
                import wizard_service  # noqa: PLC0415
                detected = wizard_service.run_auto_detect()
                self.send_json({"status": "success", "data": detected})
            except Exception as exc:  # noqa: BLE001
                self.send_json({"status": "error", "message": str(exc)}, 500)
            return

        if path == "/api/wizard/init-demo":
            try:
                import wizard_service  # noqa: PLC0415
                demo = wizard_service.generate_demo_material()
                with STATE_LOCK:
                    CONSOLE_LOGS.append({
                        "time": now_time,
                        "level": "INFO",
                        "msg": "[向导] 已生成脱敏示例待办素材并完成提炼预览",
                    })
                self.send_json({"status": "success", "data": demo})
            except Exception as exc:  # noqa: BLE001
                self.send_json({"status": "error", "message": str(exc)}, 500)
            return

        if path == "/api/wizard/complete":
            try:
                import wizard_service  # noqa: PLC0415
                res = wizard_service.complete_wizard()
                with STATE_LOCK:
                    CONSOLE_LOGS.append({
                        "time": now_time,
                        "level": "SUCCESS",
                        "msg": "🎉 新手引导向导已顺利完成，系统已转入正常运行态",
                    })
                self.send_json({"status": "success", "data": res})
            except Exception as exc:  # noqa: BLE001
                self.send_json({"status": "error", "message": str(exc)}, 500)
            return

        if path == "/api/wizard/reset":
            try:
                import wizard_service  # noqa: PLC0415
                res = wizard_service.reset_wizard()
                with STATE_LOCK:
                    CONSOLE_LOGS.append({
                        "time": now_time,
                        "level": "WARN",
                        "msg": "用户重置了新手引导向导状态，重新进入新手引导向导模式",
                    })
                self.send_json({"status": "success", "data": res})
            except Exception as exc:  # noqa: BLE001
                self.send_json({"status": "error", "message": str(exc)}, 500)
            return

        self.send_error(404, "Endpoint not found")


class ConsoleAlreadyRunning(RuntimeError):
    """8000 端口已被本程序的另一个控制台占用。"""

    def __init__(self, port: int):
        super().__init__(f"console already listening on {port}")
        self.port = port


def _probe_console(port: int, timeout: float = 1.5) -> bool:
    """端口上是不是「本程序的控制台」——只看 /api/ping 是否回 200。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


class BoundedThreadingHTTPServer(_ServerCls):
    """限制并发请求线程，过载时快速返回 503 而不是无限创建线程。"""
    request_queue_size = 64
    daemon_threads = True
    # 关键：Windows 下 allow_reuse_address=1 会让第二个实例也能绑上同一端口，
    # 两个进程同时监听 8000，连接被随机分给旧进程 —— 用户看到的是
    # 「有时正常、有时像是卡住」，实际上请求根本没到新代码。
    allow_reuse_address = False
    allow_reuse_port = False

    def __init__(self, *args, **kwargs):
        self._request_slots = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


def _bind_server():
    """绑端口。本程序的控制台已在跑就明确报出来，绝不悄悄换端口。

    旧实现会退到 8080 / 随机端口，于是新实例自以为启动成功，而浏览器
    仍然打开 8000（旧实例）—— 用户看到的就是「明明改了代码却没生效」。
    """
    for port in (PORT, 8080):
        try:
            return BoundedThreadingHTTPServer((HOST, port), ProcessConsoleHTTPHandler)
        except OSError:
            if _probe_console(port):
                raise ConsoleAlreadyRunning(port) from None
            continue
    return BoundedThreadingHTTPServer((HOST, 0), ProcessConsoleHTTPHandler)


def _find_workbuddy_exe() -> Path | None:
    """与命令行入口共用注册表、PATH 和常见目录动态查找。"""
    return common.find_workbuddy_exe()


def _launch_workbuddy() -> bool:
    try:
        os.startfile("workbuddy://")
        return True
    except (OSError, AttributeError):
        pass
    exe = _find_workbuddy_exe()
    if exe and exe.exists():
        try:
            import subprocess
            subprocess.Popen([str(exe)], close_fds=True)
            return True
        except OSError:
            pass
    return False


def _schedule_brief() -> dict:
    """给 /api/status 用的一小段调度摘要（完整版在 /api/schedule）。"""
    try:
        st = sched.status()
        return {
            "enabled": st["enabled"],
            "mode": st["mode"],
            "next_run": st["next_run"],
            "last_start": st["last_start"],
            "last_rc": st["last_rc"],
            "running_now": st["running_now"],
        }
    except Exception as exc:  # noqa: BLE001
        return {"enabled": False, "error": str(exc)}


def _scheduler_start() -> bool:
    """拉起采集调度线程（幂等：已在跑就直接返回）。"""
    global SCHEDULER_THREAD, IS_SCHEDULER_RUNNING
    if SCHEDULER_THREAD and SCHEDULER_THREAD.is_alive():
        IS_SCHEDULER_RUNNING = True
        return True
    sched._stop.clear()
    try:
        SCHEDULER_THREAD = threading.Thread(
            target=sched.daemon_loop, kwargs={"poll_seconds": 20.0},
            name="todo-scheduler", daemon=True,
        )
        SCHEDULER_THREAD.start()
        IS_SCHEDULER_RUNNING = True
        return True
    except RuntimeError:
        IS_SCHEDULER_RUNNING = False
        return False


def _scheduler_stop() -> None:
    global IS_SCHEDULER_RUNNING
    sched.stop()
    IS_SCHEDULER_RUNNING = False


def _schedule_uninstall_exit(delay_sec: float = 2.6) -> None:
    """在完整返回卸载结果后再延迟退出主服务进程，便于脱离脚本清理被占用日志与缓存。"""
    def _delayed_exit() -> None:
        time.sleep(delay_sec)
        STOP_EVENT.set()
        os._exit(0)

    threading.Thread(target=_delayed_exit, name="uninstall-exit", daemon=True).start()


def _scheduler_apply(enabled: bool) -> dict:
    """把「启用/停用」落到配置文件，并同步线程状态。"""
    sched.save_config({"enabled": bool(enabled)})
    if enabled:
        _scheduler_start()
    else:
        _scheduler_stop()
    return sched.status()


def main(argv=None):
    parser = argparse.ArgumentParser(description="待办自动汇总助手可视化控制台")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器或 WorkBuddy")
    parser.add_argument("--no-scheduler", action="store_true", help="不启动采集调度线程（只开控制台）")
    args = parser.parse_args(argv)

    # 0) 解释器自检：控制台是「所有入口」的汇合点，能力必须在这里兜住。
    #    任何入口（桌面 App / 快捷方式 / 直接 python server.py）只要缺 sqlcipher3，
    #    就在绑端口前原地切到包内 .venv，绝不让控制台以"残废"状态对外提供服务。
    guard = runtime_env.ensure_capable(verbose=True)
    if not guard["ok"]:
        print(f"[!] 运行环境不完整：{guard['reason']}")
        print("    QQ 通道将无法解密（其余平台不受影响）。")

    STOP_EVENT.clear()
    sampler = threading.Thread(target=background_sampler_loop, daemon=True)
    sampler.start()

    # 采集调度：配置里 enabled=true 才真正开始按计划采集。
    # 采集与「提炼推送」是两件事 —— 后者由 外部协作引擎 定时任务负责。
    if not args.no_scheduler and sched.load_config().get("enabled"):
        if _scheduler_start():
            print(f"[✓] 采集调度线程已启动，配置: {sched.CONFIG_PATH}")
        else:
            print("[!] 采集调度线程启动失败，可用 tools/scheduler.py run-now 手动采集")
    else:
        print("[·] 采集调度未启用（tools/scheduler.py enable 可开启）")

    try:
        httpd = _bind_server()
    except ConsoleAlreadyRunning as exc:
        url = f"http://127.0.0.1:{exc.port}"
        print(f"[!] 控制台已经在运行：{url}")
        print("    为避免两个进程争抢同一端口（会让界面时好时坏），本次不重复启动。")
        print("    如需重启：先关闭已有的控制台窗口，再运行本程序。")
        if not args.no_open:
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass
        return 0

    active_port = httpd.server_port
    browser_host = "127.0.0.1" if HOST in ("0.0.0.0", "::") else HOST
    browser_url = f"http://{browser_host}:{active_port}"

    # 把「真正绑上的端口 + 解释器能力」落盘，启动器（app.py）据此决定用哪个地址开窗，
    # 而不是自己猜 8000——端口被占时旧实现会让窗口指向别人家的控制台。
    try:
        common.write_json(WORK_DIR / "console_port.json", {
            "pid": os.getpid(),
            "port": active_port,
            "url": browser_url,
            "python": sys.executable,
            "capable": runtime_env.has_all(("sqlcipher3",)),
            "written_at": time.time(),
        })
    except Exception:  # noqa: BLE001
        pass

    # 自动弹出默认浏览器打开前端控制台
    def _auto_open_browser():
        if STOP_EVENT.wait(0.5):
            return
        try:
            import desktop_app
            desktop_app.launch_app_window(browser_url)
        except Exception:
            try:
                import webbrowser
                webbrowser.open(browser_url)
            except Exception:
                pass

    # 启动时联动唤醒 外部协作引擎
    def _auto_launch_wb():
        if not STOP_EVENT.wait(1.0):
            _launch_workbuddy()

    if not args.no_open:
        threading.Thread(target=_auto_open_browser, daemon=True).start()
        threading.Thread(target=_auto_launch_wb, daemon=True).start()
    # 版本号只认 tools/version.py 这一个来源，绝不在横幅里手写死版本。
    try:
        import version as _version_mod  # noqa: PLC0415
        _ver = f"v{_version_mod.__version__}"
    except Exception:  # noqa: BLE001
        _ver = "v?"
    banner = f"""
=============================================================================
  待办自动汇总助手 · 桌面监控台 {_ver} (开源版)
  开源项目: Todo Summary Assistant
=============================================================================
  [✓] 静态仪表盘已挂载: {HTML_FILE_PATH}
  [✓] 待办助手核心模块探测: {PKG_ROOT}
  [✓] 增量监控 API 端点就绪:
      - GET  http://localhost:{active_port}/api/status
      - GET  http://localhost:{active_port}/api/config
      - GET  http://localhost:{active_port}/api/tasks
      - GET  http://localhost:{active_port}/api/logs
      - GET  http://localhost:{active_port}/api/stats
      - POST http://localhost:{active_port}/api/config
      - POST http://localhost:{active_port}/api/extract
  [✓] 桌面监控台访问地址:
      ==> {browser_url} <==
=============================================================================
  服务已在后台运行中，按 Ctrl+C 退出...
"""
    print(banner)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[!] 进程监控台服务已安全停止。")
    finally:
        STOP_EVENT.set()
        httpd.server_close()

if __name__ == "__main__":
    main()
