"""待办自动汇总助手 · 桌面 App 主入口 (Desktop Application Entry)

无需再开黑框或浏览器普通标签页，本入口将软件直接包装为完整的 Windows 桌面客户端形态：
1. 自动启动后台守护服务 (http://127.0.0.1:8000)；
2. 自动唤醒专属于本应用的独立窗口（Chromium App Mode：无地址栏、无标签页、专属图标）；
3. 支持一键创建桌面快捷方式（python app.py --install）。

关于"从桌面 App 进入 QQ 抓不到"的修复（v1.5.1）
旧实现有两个洞，叠在一起就必然复现「桌面 App 进不去 QQ、Start.bat 一切正常」：

  a) 本文件用 `python app.py` 启动 —— Windows 上的 `python` 很可能是系统/其它
     工具自带的 3.13~3.15，没有 sqlcipher3。它虽然会去拉 .venv 的 server，
     但只要 8000 端口上**已经有**一个进程在应答 /api/ping，`is_server_running()`
     就直接返回 True，于是永远不会拉起自己那个"能解 QQ"的实例，用户实际在用的
     就是那个残废实例。
  b) 判断只看"端口有人应答"，不看"那个进程有没有解密能力"，也不看它是不是
     同一个安装目录的实例。

现在：解释器由 runtime_env 统一裁决（缺能力自动切 .venv），并且复用之前必须
核对对方上报的 python / root / capable —— 不匹配就自己在空闲端口起一个能用的
实例，把窗口指过去，全程不需要用户理解这些差异。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR / "tools"))

import runtime_env  # noqa: E402

CONSOLE_PORT_FILE = ROOT_DIR / "work" / "console_port.json"


def _ping(port: int, timeout: float = 1.2) -> dict | None:
    """探测端口上是不是本程序的控制台；返回它的自述信息（含解释器能力）。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=timeout) as r:
            if r.status != 200:
                return None
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def _compatible(info: dict | None) -> bool:
    """复用的前提：同一个安装目录 + 有 sqlcipher3。缺一个都不算可用。"""
    return _same_root(info) and bool(info.get("capable"))


def _same_root(info: dict | None) -> bool:
    if not info or info.get("app") != "todo-console":
        return False
    try:
        return Path(str(info.get("root") or "")).resolve() == ROOT_DIR.resolve()
    except OSError:
        return False


def _free_port(start: int = 8000, tries: int = 20) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0


def _port_from_file() -> int:
    try:
        info = json.loads(CONSOLE_PORT_FILE.read_text(encoding="utf-8"))
        port = int(info.get("port") or 0)
        if port and _same_root(_ping(port)):
            return port
    except (OSError, ValueError):
        pass
    return 0


def start_console(preferred_port: int) -> tuple[bool, int]:
    """用「能解 QQ 的解释器」拉起控制台，返回 (成功, 实际端口)。"""
    runner = runtime_env.work_python(windowless=True)
    if not Path(runner).exists():          # 极端情况：连 pythonw 都没有
        runner = runtime_env.work_python()

    port = _free_port(preferred_port)
    log_path = ROOT_DIR / "work" / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "ab")

    env = dict(os.environ)
    env["QQTODO_CONSOLE_PORT"] = str(port or preferred_port)
    env.pop(runtime_env.REEXEC_FLAG, None)   # 子进程不需要继承"已 re-exec"标记

    flags = 0x08000000 if sys.platform == "win32" else 0
    subprocess.Popen(
        [str(runner), str(ROOT_DIR / "server.py"), "--no-open"],
        cwd=str(ROOT_DIR),
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=log_file,
        env=env,
        creationflags=flags,
    )

    # 等它就绪（最多 15 秒）。server 会把真正绑上的端口写进 work/console_port.json，
    # 端口被抢占时也能拿到正确地址。
    for _ in range(75):
        time.sleep(0.2)
        for candidate in (_port_from_file(), port):
            info = _ping(candidate) if candidate else None
            if _same_root(info):
                if not info.get("capable"):
                    print("[!] 控制台已启动，但仍缺少 SQLCipher：QQ 通道会不可用。")
                return True, int(info.get("port") or candidate)
    return False, port


def main() -> int:
    parser = argparse.ArgumentParser(description="待办自动汇总助手 · 桌面应用启动器")
    parser.add_argument("--install", action="store_true", help="在桌面生成专属应用快捷方式")
    parser.add_argument("--port", type=int, default=8000, help="控制台端口 (默认 8000)")
    args = parser.parse_args()

    # 先把自己换到"能干活的解释器"上再谈启动：快捷方式、bat、双击都走这一条路。
    guard = runtime_env.ensure_capable(verbose=True)
    capable = bool(guard.get("runtime", {}).get("capable"))
    if not capable:
        print(f"[!] 运行环境不完整：{guard.get('reason')}")
        print("    QQ 通道会不可用；其余平台不受影响。请检查 vendor/wheels 是否随包完整。")

    # 1. 若指定安装快捷方式
    rc = 0
    if args.install:
        print("[*] 正在为「待办总结助手」创建桌面与开始菜单快捷方式...")
        import desktop_app  # noqa: PLC0415  （延迟导入：--install 之外不需要）

        res = desktop_app.install_shortcuts()
        print(f"[{'✔' if res.get('ok') else 'X'}] {res.get('message')}")
        if not res.get("ok"):
            # 旧实现这里无论成败都继续 return 0，于是"安装桌面App.bat"报成功、
            # 用户却找不到图标。现在把失败如实传给退出码。
            rc = 2

    # 2. 端口上已有实例？只有"同目录 + 能解 QQ"才复用
    existing = _ping(args.port)
    if _compatible(existing):
        port = int(existing.get("port") or args.port)
        print(f"[✔] 已有可用的后台服务: http://127.0.0.1:{port}（pid {existing.get('pid')}，"
              f"{existing.get('python_version')}）")
    else:
        if existing is not None:
            # 有实例但能力不全（典型：别人用系统 Python 起的旧控制台）：
            # 不动它，自己在空闲端口起一个可用实例，窗口指到新实例上。
            print("[!] 端口上已有控制台，但它缺少 SQLCipher 解密能力，"
                  "QQ 抓取会失败；将另起一个可用实例。")
        print("[*] 正在拉起待办助手后台服务与流水线引擎...")
        ok, port = start_console(args.port)
        if not ok:
            print(f"[!] 后台服务未能就绪，请检查 {ROOT_DIR / 'work' / 'server.log'}")
            return 1

    server_url = f"http://127.0.0.1:{port}"
    print(f"[✔] 后台核心服务已就绪: {server_url}")
    print("[*] 正在以原生桌面应用窗口模式打开待办总结助手...")

    # 3. 呼出独立桌面 App 窗口
    import desktop_app  # noqa: PLC0415

    desktop_app.launch_app_window(server_url)
    return rc


if __name__ == "__main__":
    sys.exit(main())
