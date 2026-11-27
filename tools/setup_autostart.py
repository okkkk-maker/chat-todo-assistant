"""注册/取消开机自启动。

做法：往「启动」文件夹放一个后台批处理，不依赖 schtasks、不需要管理员权限。
- 本机无法使用 schtasks（沙箱禁用），启动文件夹是最稳的等效手段。
- 后台模式不弹窗、不 pause，跑完自己退出。

用法:
    python tools/setup_autostart.py
    python tools/setup_autostart.py --remove
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        # 精简系统 / 服务账户下 APPDATA 可能不存在，旧写法 os.environ["APPDATA"]
        # 会在 import 阶段直接 KeyError，连 --remove 都用不了。
        appdata = os.path.expanduser(r"~\AppData\Roaming")
    return Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup"


def _ascii_safe_path(p: Path) -> str:
    """路径含中文时改用 8.3 短名。

    cmd.exe 解析 UTF-8 批处理时按字节偏移读取，`chcp 65001` 之后遇到中文路径
    容易错位（表现为 cd 到错误目录、或 .venv 找不到）。短名是纯 ASCII，绕开这个问题。
    """
    s = str(p)
    if s.isascii():
        return s
    try:
        import ctypes  # noqa: PLC0415
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.GetShortPathNameW(s, buf, 1024)  # type: ignore[attr-defined]
        if n and buf.value:
            return buf.value
    except Exception:  # noqa: BLE001
        pass
    raise RuntimeError(
        f"路径含非 ASCII 字符且无法取得 8.3 短名，拒绝注册可能无法运行的 BAT: {s}"
    )


STARTUP = _startup_dir()
TARGET = STARTUP / "ChatTodoWatcher.bat"
# 调度器自启项：与「采集一次」分开，避免开机时两个采集撞车
SCHED_TARGET = STARTUP / "ChatTodoScheduler.bat"

# 注意：启动脚本同样只写 ASCII、用 CRLF。
# 日志文件名也用 ASCII，避免 cmd.exe 解析中文重定向路径时错位。
BAT = """@echo off
chcp 65001 >nul
cd /d "{root}"
if not exist "output" mkdir "output"
"{py}" run.py --no-open >> "output\\background.log" 2>&1
set "RC=%ERRORLEVEL%"
exit /b %RC%
""".replace("\n", "\r\n")

# 常驻调度：登录后启动，按 config/schedule.json 的计划反复采集。
SCHED_BAT = """@echo off
chcp 65001 >nul
cd /d "{root}"
if not exist "output" mkdir "output"
"{py}" tools\\scheduler.py daemon >> "output\\scheduler.log" 2>&1
""".replace("\n", "\r\n")


def _python_exe() -> str:
    """优先用包内 .venv；用户包没有 .venv 时退回当前解释器（外部协作引擎 自带 Python）。

    旧脚本写死 .venv 且「不存在就 exit 1」—— 用户包里永远没有 .venv，
    于是开机自启静默什么都不做，看起来像「自启失效」。
    """
    venv = ROOT / ".venv" / "Scripts" / "python.exe"
    return _ascii_safe_path(venv if venv.exists() else Path(sys.executable))


def _write_bat(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload.encode("ascii")          # 写入前验证，避免中文路径被 UTF-8 BAT 错位解析
    tmp = path.with_name(f"{path.name}.tmp_{os.getpid()}")
    try:
        with open(tmp, "w", encoding="ascii", newline="") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def install() -> None:
    _write_bat(TARGET, BAT.format(root=_ascii_safe_path(ROOT), py=_python_exe()))
    print(f"       auto-start enabled: {TARGET}")
    print("       (runs silently on login, log -> output\\background.log)")


def install_scheduler() -> None:
    """把常驻调度器也注册到启动项（登录后按计划采集）。"""
    _write_bat(SCHED_TARGET, SCHED_BAT.format(root=_ascii_safe_path(ROOT), py=_python_exe()))
    print(f"       scheduler auto-start enabled: {SCHED_TARGET}")
    print("       (按 config\\schedule.json 的计划采集, log -> output\\scheduler.log)")


def remove() -> None:
    for path in (TARGET, SCHED_TARGET):
        if path.exists():
            path.unlink()
            print(f"       removed: {path}")
        else:
            print(f"       nothing to remove: {path.name}")


def status() -> None:
    print(f"       采集一次: {'已注册 ' + str(TARGET) if TARGET.exists() else '未注册'}")
    print(f"       常驻调度: {'已注册 ' + str(SCHED_TARGET) if SCHED_TARGET.exists() else '未注册'}")


def main() -> None:
    if "--remove" in sys.argv:
        remove()
    elif "--status" in sys.argv:
        status()
    elif "--scheduler-only" in sys.argv:
        install_scheduler()
    else:
        install()
        install_scheduler()      # 装上调度器才算「真定时」，否则只是个开机跑一次的脚本


if __name__ == "__main__":
    main()
