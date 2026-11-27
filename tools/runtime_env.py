"""运行环境唯一裁决层（Runtime Environment Resolver）

为什么需要这个文件（线上事故复盘）
本程序有 5 个启动入口：Admin_Init.bat / Start.bat / Web_Console.bat /
安装桌面App.bat / 启动桌面App.bat，外加 App 快捷方式、scheduler 子进程、
vendor/1-get-qq-key.bat。旧实现里**每个入口各写一套解释器选择逻辑**：

    Start.bat / Web_Console.bat ...... 优先 .venv\\Scripts\\python.exe（3.13 + sqlcipher3）
    安装桌面App.bat / 启动桌面App.bat .. 硬编码 `python`（系统 3.15，**没有 sqlcipher3**）
    vendor/1-get-qq-key.bat .......... 优先 .venv，缺了退回 `python`
    app.py .......................... 只在 .venv 缺失时才用 sys.executable
    server.py ........................ 完全不自检

结果是同一个功能在不同入口下能力不同：从桌面 App 进入时，控制台跑在系统
Python 3.15 上，`qqdb._sqlcipher()` 直接抛「缺少 SQLCipher 组件」，QQ 库
一个字节都解不开；从 Start.bat 进入则一切正常。这就是「换个入口 QQ 就抓不到」
的根因——不是抓取算法有两套，而是**跑抓取的解释器有两套**。

本模块把「用哪个解释器、有没有解密能力」收敛成唯一答案：
    · work_python()       当前环境该用的解释器
    · capabilities()      某个解释器到底有没有 sqlcipher3 / pycryptodome / zstandard
    · ensure_capable()    缺能力就离线补 .venv，补不出来就原地 re-exec 到 .venv

约定：任何入口都不再自己拼解释器路径，一律调用本模块。这样加一个入口
（未来再加个 .exe、计划任务）也不会重新长出「某入口抓不到 QQ」的坑。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = ROOT / ".venv"
VENV_PY = VENV_DIR / "Scripts" / "python.exe"
VENV_PYW = VENV_DIR / "Scripts" / "pythonw.exe"
BOOTSTRAP = ROOT / "tools" / "bootstrap_deps.py"

# 解 QQ 库 / 解微信库 / 解 zstd 正文 所必需的包
REQUIRED_FOR_QQ = ("sqlcipher3",)
REQUIRED_FULL = ("sqlcipher3", "Crypto")

# re-exec 只做一次：环境变量打标，防止两套解释器互相踢皮球死循环
REEXEC_FLAG = "QQTODO_REEXEC_ED"

_CAP_CACHE: dict[str, dict] = {}


def _is_pythonw(exe: str | Path) -> bool:
    return Path(exe).name.lower() == "pythonw.exe"


def venv_python(windowless: bool = False) -> Path | None:
    """包内专属解释器（3.13 + 离线依赖）。不存在返回 None。"""
    if windowless and VENV_PYW.exists():
        return VENV_PYW
    return VENV_PY if VENV_PY.exists() else None


def work_python(windowless: bool = False) -> Path:
    """本机该用的解释器：有 .venv 用 .venv，否则用当前解释器。"""
    return venv_python(windowless) or Path(sys.executable)


def _probe(py: str | Path) -> dict:
    """问子进程：你装了哪些包。只问一次，结果进进程内缓存。"""
    key = str(py).lower()
    if key in _CAP_CACHE:
        return dict(_CAP_CACHE[key])
    code = (
        "import importlib.util,json,sys;"
        "print(json.dumps({"
        "'version':sys.version.split()[0],"
        "'sqlcipher3':importlib.util.find_spec('sqlcipher3') is not None,"
        "'Crypto':importlib.util.find_spec('Crypto') is not None,"
        "'zstandard':importlib.util.find_spec('zstandard') is not None"
        "}))"
    )
    info = {"python": str(py), "version": "", "sqlcipher3": False, "Crypto": False, "zstandard": False}
    try:
        flags = 0x08000000 if os.name == "nt" else 0
        proc = subprocess.run(
            [str(py), "-c", code], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=25, creationflags=flags,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout.strip().splitlines()[-1])
            info.update({k: data.get(k, info[k]) for k in info if k != "python"})
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    _CAP_CACHE[key] = dict(info)
    return info


def capabilities(py: str | Path | None = None) -> dict:
    """某个解释器的能力清单。默认问当前进程（直接 find_spec，最快）。"""
    if py is None:
        return {
            "python": sys.executable,
            "version": sys.version.split()[0],
            "sqlcipher3": importlib.util.find_spec("sqlcipher3") is not None,
            "Crypto": importlib.util.find_spec("Crypto") is not None,
            "zstandard": importlib.util.find_spec("zstandard") is not None,
        }
    return _probe(py)


def has_all(required: tuple[str, ...] = REQUIRED_FOR_QQ, caps: dict | None = None) -> bool:
    caps = caps or capabilities()
    return all(bool(caps.get(name)) for name in required)


def runtime_snapshot() -> dict:
    """给 /api/ping、日志、诊断用的身份信息。"""
    caps = capabilities()
    portable = (VENV_DIR / ".qq-todo-portable.json").exists()
    return {
        "python": caps.get("python") or sys.executable,
        "version": caps.get("version") or sys.version.split()[0],
        "root": str(ROOT),
        "pid": os.getpid(),
        "capabilities": {k: bool(caps.get(k)) for k in ("sqlcipher3", "Crypto", "zstandard")},
        "capable": has_all(REQUIRED_FOR_QQ, caps),
        "venv": str(VENV_DIR) if VENV_PY.exists() else "",
        "portable": portable,
    }


def _bootstrap_venv() -> bool:
    """缺依赖时离线建/补 .venv（vendor/wheels 随包，不需要联网）。"""
    if not BOOTSTRAP.exists():
        return False
    try:
        proc = subprocess.run(
            [sys.executable, str(BOOTSTRAP)], cwd=str(ROOT),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
        )
        return proc.returncode == 0 and VENV_PY.exists()
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_capable(
    required: tuple[str, ...] = REQUIRED_FOR_QQ,
    reexec: bool = True,
    verbose: bool = False,
) -> dict:
    """确保当前进程具备 required 能力。

    返回 {"ok":bool, "reexec":bool, "reason":str, "runtime":snapshot}
        · ok=True  当前解释器已经够用（或者已经 re-exec 成功，进程被替换）
        · ok=False 补不齐，调用方应降级并**明确报错**，绝不假装成功
    """
    caps = capabilities()
    if has_all(required, caps):
        return {"ok": True, "reexec": False, "reason": "", "runtime": runtime_snapshot()}

    missing = [m for m in required if not caps.get(m)]
    detail = f"当前解释器 {sys.executable}（{caps.get('version')}）缺少 {', '.join(missing)}"

    # 1) 包内解释器本来就够用 -> 直接切过去
    target = venv_python(windowless=_is_pythonw(sys.executable))
    if target and target.exists() and target.resolve() != Path(sys.executable).resolve():
        if has_all(required, capabilities(target)):
            if reexec and not os.environ.get(REEXEC_FLAG):
                if verbose:
                    print(f"[runtime] {detail}，切换到包内解释器 {target}", flush=True)
                _reexec(target)
            return {"ok": False, "reexec": False, "reason": detail + "；包内解释器可用但未切换", "runtime": runtime_snapshot()}

    # 2) 没有 .venv（或 .venv 也不全）-> 离线补一次再切
    if _bootstrap_venv():
        target = venv_python(windowless=_is_pythonw(sys.executable)) or VENV_PY
        if target.exists() and has_all(required, capabilities(target)):
            if reexec and not os.environ.get(REEXEC_FLAG):
                if verbose:
                    print(f"[runtime] 已补齐依赖，切换到 {target}", flush=True)
                _reexec(target)
            return {"ok": True, "reexec": False, "reason": "已离线补齐依赖", "runtime": runtime_snapshot()}

    return {"ok": False, "reexec": False, "reason": detail + "，且离线依赖补齐失败", "runtime": runtime_snapshot()}


def _reexec(target: Path) -> None:
    """用目标解释器原地替换当前进程（Windows 上 os.execv 即换进程映像）。"""
    os.environ[REEXEC_FLAG] = "1"
    argv = [str(target)] + [str(a) for a in sys.argv]
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    try:
        os.execv(str(target), argv)
    except OSError:
        # 极端情况下 execv 不可用：退化成子进程并退出当前进程
        os.environ.pop(REEXEC_FLAG, None)
        raise


def describe() -> str:
    snap = runtime_snapshot()
    caps = snap["capabilities"]
    return (
        f"解释器 {snap['python']}（{snap['version']}）"
        f" sqlcipher3={'有' if caps['sqlcipher3'] else '无'}"
        f" pycryptodome={'有' if caps['Crypto'] else '无'}"
        f" zstd={'有' if caps['zstandard'] else '无'}"
    )


if __name__ == "__main__":
    # 排障用：python tools/runtime_env.py
    res = ensure_capable(verbose=True)
    print(describe())
    print(json.dumps(res["runtime"], ensure_ascii=False, indent=2))
    sys.exit(0 if res["runtime"].get("capable") else 1)
