"""待办自动汇总助手 · 桌面应用外壳与快捷方式管理器 (Desktop App Shell)

提供：
1. 以原生桌面应用窗口模式（Chromium App Mode）唤起界面，无浏览器地址栏、无标签页、独立窗口与专属任务栏图标；
2. 一键在 Windows 桌面与开始菜单创建带专属应用图标（favicon.ico）的「待办总结助手.lnk」快捷方式；
3. 支持通过 pythonw.exe 实现完全后台静默拉起，后台静默启动，不显示控制台窗口。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
FAVICON_PATH = ROOT_DIR / "favicon.ico"
APP_PY_PATH = ROOT_DIR / "app.py"
SERVER_PY_PATH = ROOT_DIR / "server.py"


def find_app_browser() -> tuple[Path | None, str]:
    """寻找支持 --app 独立窗口模式的浏览器（Edge 或 Chrome）。"""
    # 1. 优先探测 Windows 10/11 预装的 Microsoft Edge
    candidates = [
        # Edge 官方路径
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / r"Microsoft\Edge\Application\msedge.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / r"Microsoft\Edge\Application\msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / r"Microsoft\Edge\Application\msedge.exe",
        # Chrome 路径
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / r"Google\Chrome\Application\chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / r"Google\Chrome\Application\chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / r"Google\Chrome\Application\chrome.exe",
    ]

    for p in candidates:
        if p.exists() and p.is_file():
            name = "Microsoft Edge" if "msedge" in str(p).lower() else "Google Chrome"
            return p, name

    # PATH 环境变量探测
    edge_in_path = shutil.which("msedge")
    if edge_in_path:
        return Path(edge_in_path), "Microsoft Edge"

    chrome_in_path = shutil.which("chrome")
    if chrome_in_path:
        return Path(chrome_in_path), "Google Chrome"

    return None, "默认系统浏览器"


def launch_app_window(url: str = "http://127.0.0.1:8000") -> bool:
    """以独立桌面应用（App Mode）形式打开窗口。"""
    browser_exe, browser_name = find_app_browser()
    if browser_exe:
        profile_dir = ROOT_DIR / "work" / ".app_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(browser_exe),
            f"--app={url}",
            f"--user-data-dir={profile_dir}",
            "--window-size=1360,860",
            "--window-position=120,60",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-plugins",
        ]
        flags = 0x08000000 if sys.platform == "win32" else 0
        try:
            subprocess.Popen(cmd, creationflags=flags)
            return True
        except Exception:
            pass

    # 兜底：若均无法调用，则回退到系统默认浏览器
    try:
        import webbrowser
        webbrowser.open(url)
        return True
    except Exception:
        return False


def _known_folder(csidl_name: str, fallback: Path) -> Path:
    """用 SHGetKnownFolderPath 取系统认定的目录（桌面/开始菜单）。

    这是唯一权威来源：它同时覆盖「桌面被重定向到 D 盘」「OneDrive 重定向」
    「OneDrive - 某某单位\\桌面」这些情况。旧实现靠猜四个固定路径，企业环境里
    目录名带租户后缀（OneDrive - 单位名）就必然猜错，快捷方式会建到
    一个用户根本看不见的地方，表现为"装完找不到图标"。
    """
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        def _guid(text: str) -> GUID:
            g = GUID()
            ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
            return g

        # FOLDERID_Desktop / FOLDERID_Programs / FOLDERID_StartMenu
        ids = {
            "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
            "programs": "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}",
            "startmenu": "{625B53C3-AB48-4EC1-BA1F-A1EF4146FC19}",
        }
        guid = _guid(ids[csidl_name])
        ptr = ctypes.c_wchar_p()
        hres = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(ptr))
        if hres == 0 and ptr.value:
            path = Path(ptr.value)
            ctypes.windll.ole32.CoTaskMemFree(ptr)
            if path.is_dir():
                return path
    except Exception:  # noqa: BLE001
        pass
    return fallback


def get_desktop_path() -> Path:
    """获取用户桌面路径（系统权威值，支持多盘符与各类 OneDrive 重定向）。"""
    home = Path(os.environ.get("USERPROFILE", os.path.expanduser("~")))
    guessed = home / "Desktop"
    for cand in (home / "OneDrive" / "Desktop", home / "OneDrive" / "桌面",
                 home / "桌面", home / "Desktop"):
        if cand.is_dir():
            guessed = cand
            break
    resolved = _known_folder("desktop", guessed)
    if resolved.is_dir():
        return resolved
    return guessed


def get_start_menu_programs() -> Path:
    """开始菜单「程序」目录（同样走系统 API，兼容中文与重定向）。"""
    appdata = Path(os.environ.get("APPDATA", os.path.expanduser(r"~\AppData\Roaming")))
    fallback = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    return _known_folder("programs", fallback)


def _create_lnk(lnk: Path, target_exe: Path, target_args: str,
                icon_loc: str, working_dir: str) -> tuple[bool, str]:
    """创建一个 .lnk：先用 WScript.Shell（PowerShell），失败再退 VBScript。

    路径含空格没问题（单引号包裹）；含单引号的极端路径会失败，此时
    VBS 兜底同样会失败，调用方按"创建失败"处理，不会假装成功。
    """
    def _ps_esc(v: Any) -> str:
        return str(v).replace("\r", "").replace("\n", "").replace("'", "''")

    def _vbs_esc(v: Any) -> str:
        return str(v).replace("\r", "").replace("\n", "").replace('"', '""')

    ps_script = f"""
    $ws = New-Object -ComObject WScript.Shell
    $s = $ws.CreateShortcut('{_ps_esc(lnk)}')
    $s.TargetPath = '{_ps_esc(target_exe)}'
    $s.Arguments = '{_ps_esc(target_args)}'
    $s.WorkingDirectory = '{_ps_esc(working_dir)}'
    $s.IconLocation = '{_ps_esc(icon_loc)},0'
    $s.Description = '待办总结助手 · 桌面应用'
    $s.Save()
    """
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            capture_output=True, text=True, timeout=20,
        )
        if res.returncode == 0 and lnk.exists():
            return True, "powershell"
    except (OSError, subprocess.SubprocessError) as exc:
        err = f"{type(exc).__name__}: {exc}"
    else:
        err = "powershell 未能生成快捷方式"

    vbs_path = ROOT_DIR / "work" / f"_mklnk_{os.getpid()}_{abs(hash(str(lnk))) % 10000}.vbs"
    try:
        vbs_path.parent.mkdir(parents=True, exist_ok=True)
        vbs_content = f'''
Set oWS = WScript.CreateObject("WScript.Shell")
Set oLink = oWS.CreateShortcut("{_vbs_esc(lnk)}")
oLink.TargetPath = "{_vbs_esc(target_exe)}"
oLink.Arguments = "{_vbs_esc(target_args)}"
oLink.WorkingDirectory = "{_vbs_esc(working_dir)}"
oLink.IconLocation = "{_vbs_esc(icon_loc)},0"
oLink.Description = "待办总结助手 · 桌面应用"
oLink.Save
'''
        vbs_path.write_text(vbs_content, encoding="gbk", errors="ignore")
        subprocess.run(["cscript", "//nologo", str(vbs_path)], check=True, timeout=20,
                       capture_output=True)
        if lnk.exists():
            return True, "vbscript"
        return False, "VBScript 执行完但没有生成 .lnk"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{err} / VBScript 也失败: {exc}"
    finally:
        try:
            if vbs_path.exists():
                vbs_path.unlink()
        except OSError:
            pass


def install_shortcuts() -> dict[str, Any]:
    """在桌面与开始菜单创建「待办总结助手」快捷方式。

    v1.5.3.3 修正：
      · 桌面路径改由系统 API 提供（旧实现猜路径，企业 OneDrive 重定向必错）；
      · 补上开始菜单那一份 —— 文档和卸载脚本一直都按"桌面+开始菜单"写的，
        只有代码没做。
    """
    # 选用静默 pythonw 还是常规 python
    venv_pyw = ROOT_DIR / ".venv" / "Scripts" / "pythonw.exe"
    venv_py = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
    if venv_pyw.exists():
        target_exe = venv_pyw
    elif venv_py.exists():
        target_exe = venv_py
    else:
        py_dir = Path(sys.executable).parent
        pythonw = py_dir / "pythonw.exe"
        target_exe = pythonw if pythonw.exists() else Path(sys.executable)

    target_args = f'"{APP_PY_PATH}"'
    icon_loc = str(FAVICON_PATH) if FAVICON_PATH.exists() else str(target_exe)
    working_dir = str(ROOT_DIR)

    created: list[str] = []
    failed: list[str] = []
    detail: list[str] = []

    desktop_lnk = get_desktop_path() / "待办总结助手.lnk"
    ok, how = _create_lnk(desktop_lnk, target_exe, target_args, icon_loc, working_dir)
    (created if ok else failed).append(str(desktop_lnk))
    detail.append(f"桌面: {'OK' if ok else '失败'}（{how}）")

    start_lnk = get_start_menu_programs() / "待办总结助手.lnk"
    ok2, how2 = _create_lnk(start_lnk, target_exe, target_args, icon_loc, working_dir)
    (created if ok2 else failed).append(str(start_lnk))
    detail.append(f"开始菜单: {'OK' if ok2 else '失败'}（{how2}）")

    ok_any = bool(created)
    if ok_any:
        message = ("已创建快捷方式：" + "；".join(detail) +
                   f"。目标：{target_exe}")
    else:
        message = ("未能创建任何快捷方式（" + "；".join(detail) +
                   "）。请确认对桌面/开始菜单有写入权限。")
    return {
        "ok": ok_any,
        "shortcut_path": str(desktop_lnk) if desktop_lnk.exists() else (
            str(start_lnk) if start_lnk.exists() else ""),
        "desktop": str(desktop_lnk) if desktop_lnk.exists() else "",
        "start_menu": str(start_lnk) if start_lnk.exists() else "",
        "created": [p for p in created if Path(p).exists()],
        "failed": failed,
        "target": str(target_exe),
        "icon": icon_loc,
        "message": message,
    }
