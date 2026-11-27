"""准备运行环境：挑一个受支持的 Python，建 .venv，离线装依赖。

为什么需要它：
    目标机器上的 `python` 可能是 3.14/3.15。sqlcipher3-wheels 只提供
    3.10~3.13 的预编译包，遇到新版本 pip 会尝试源码编译（要 MSVC），
    表现就是“卡在安装依赖这一步、不报错也不动”。
    所以这里先挑解释器，再用随包附带的 wheel 离线安装，全程不联网。

用法（由 Admin_Init.bat / Install.bat 调用，用系统 python 执行）:
    python tools\\bootstrap_deps.py            # 建环境 + 装依赖
    python tools\\bootstrap_deps.py --check    # 只报告，不动手
    python tools\\bootstrap_deps.py --force    # 强制重建 .venv
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
VENV_PY = VENV / "Scripts" / "python.exe"
def _resolve_wheels() -> Path:
    for cand in (
        ROOT / "vendor" / "wheels",
        ROOT.parent / "1-实施员一键初始化包" / "vendor" / "wheels",
        ROOT.parent / "vendor" / "wheels",
    ):
        if cand.is_dir():
            return cand
    return ROOT / "vendor" / "wheels"


WHEELS = _resolve_wheels()
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
PKGS = ["sqlcipher3-wheels", "pycryptodome"]
# 可选依赖：微信 4.x 的正文可能是 zstd 压缩。装不上只影响解压，不影响取密钥/解密，
# 所以单独装、失败不阻塞（不能因为它让 QQ 通道起不来）。
OPTIONAL_PKGS = ["zstandard", "chinese-calendar"]

# 便携运行时的标记文件：存在说明 .venv 是 python_bootstrap 手工铺的，没有 pip。
PORTABLE_MARKER = VENV / ".qq-todo-portable.json"

# 安装/下载的硬超时，绝不允许无限等待
TIMEOUT_LOCAL = 300
TIMEOUT_NET = 240


def say(msg: str = "") -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass
    print(msg, flush=True)


def supported_minors() -> set[int]:
    """看 vendor/wheels 里带了哪些 CPython 小版本的 sqlcipher3，只支持这些。"""
    found: set[int] = set()
    if not WHEELS.is_dir():
        return found
    for f in WHEELS.glob("sqlcipher3_wheels-*-cp3*-cp3*-win_amd64.whl"):
        m = re.search(r"-cp3(\d+)-cp3", f.name)
        if m:
            found.add(int(m.group(1)))
    return found


def probe_version(exe: str | Path) -> tuple[int, int] | None:
    try:
        r = subprocess.run(
            [str(exe), "-c", "import sys, venv;print('%d %d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            return None
        major, minor = (r.stdout or "").strip().split()
        return int(major), int(minor)
    except Exception:  # noqa: BLE001
        return None


def candidate_interpreters() -> list[Path]:
    """按“优先正规安装的 Python”顺序列出候选解释器。"""
    out: list[Path] = []

    def add(p: Path) -> None:
        if p.exists() and p not in out:
            out.append(p)

    # 1) PATH 上的 python / py 启动器
    for name in ("python.exe", "python3.exe"):
        p = shutil.which(name)
        if p:
            add(Path(p))
    py = shutil.which("py.exe")
    if py:
        for minor in (13, 12, 11, 10):
            try:
                r = subprocess.run([py, f"-3.{minor}", "-c", "import sys;print(sys.executable)"],
                                   capture_output=True, text=True, timeout=15)
                if r.returncode == 0 and r.stdout.strip():
                    add(Path(r.stdout.strip()))
            except Exception:  # noqa: BLE001
                pass

    # 2) 常见安装目录
    local = os.environ.get("LOCALAPPDATA", "")
    for minor in (13, 12, 11, 10):
        for base in (
            Path(local) / "Programs" / "Python" / f"Python3{minor}" if local else None,
            Path(f"C:/Python3{minor}"),
            Path(local) / "Python" / f"pythoncore-3.{minor}-64" if local else None,
            Path(local) / "Python" / f"pythoncore-3.{minor}-32" if local else None,
        ):
            if base:
                add(base / "python.exe")

    # 3) 外部协作引擎 自带的 Python（院长机器上必然有，最后兜底）
    home = os.environ.get("USERPROFILE", "")
    if home:
        versions = Path(home) / ".workbuddy" / "binaries" / "python" / "versions"
        if versions.is_dir():
            try:
                for d in sorted(versions.iterdir(), reverse=True):
                    add(d / "python.exe")
            except OSError:
                pass
    return out


def pick_interpreter(verbose: bool = True) -> tuple[Path | None, str]:
    """找一个版本受支持的解释器。返回 (路径, 说明)。"""
    sup = supported_minors()
    if not sup:
        return None, "vendor/wheels 里没有找到 sqlcipher3 的预编译包，发行包不完整"
    say(f"  支持的 Python 小版本: {', '.join('3.' + str(m) for m in sorted(sup))}")

    cur = probe_version(sys.executable)
    if verbose:
        say(f"  当前解释器: {sys.executable}  ({cur[0]}.{cur[1]})" if cur else f"  当前解释器: {sys.executable}")

    best: tuple[tuple[int, int], Path] | None = None
    seen: set[str] = set()
    for exe in candidate_interpreters():
        key = str(exe).lower()
        if key in seen:
            continue
        seen.add(key)
        ver = probe_version(exe)
        if not ver:
            continue
        if ver[0] != 3 or ver[1] not in sup:
            continue
        say(f"    发现可用: {exe}  ({ver[0]}.{ver[1]})")
        if best is None or ver > best[0]:
            best = (ver, exe)
    if best is None:
        which = "/".join(sorted({f"3.{m}" for m in sup}))
        cur_text = f"{cur[0]}.{cur[1]}" if cur else "读不出（当前解释器无法运行）"
        return None, f"本机没有 {which} 版本的 Python（当前是 {cur_text}）"
    ver, exe = best
    return exe, f"选用 Python {ver[0]}.{ver[1]}：{exe}"


def venv_ready(interp: Path) -> tuple[bool, str]:
    """.venv 是否已经是用目标解释器建的、且依赖可用。"""
    if not VENV_PY.exists():
        return False, "还没有 .venv"
    cfg = VENV / "pyvenv.cfg"
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, "pyvenv.cfg 读不到"
    m = re.search(r"^version\s*=\s*(\d+)\.(\d+)", text, re.M)
    if not m:
        return False, "pyvenv.cfg 里没有版本号"
    minor = int(m.group(2))
    if minor not in supported_minors():
        return False, f"现有 .venv 是 Python 3.{minor}，依赖不支持该版本"
    r = subprocess.run([str(VENV_PY), "-c", "import sqlcipher3, Crypto"],
                       capture_output=True, timeout=60)
    if r.returncode != 0:
        return False, "依赖未装齐"
    return True, "已就绪"


def remove_venv() -> None:
    if not VENV.exists():
        return
    for attempt in range(3):
        try:
            shutil.rmtree(VENV)
            return
        except OSError:
            time.sleep(1.5)
            # 被占用时改名后放弃（占位目录不影响后续新建）
            try:
                VENV.rename(VENV.with_name(f".venv.broken{attempt}"))
                return
            except OSError:
                continue


def make_venv(interp: Path) -> bool:
    say(f"  正在创建 .venv（用 {interp}）...")
    remove_venv()
    try:
        r = subprocess.run([str(interp), "-m", "venv", str(VENV)],
                           capture_output=True, text=True, timeout=180,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        say("  [失败] 创建虚拟环境超时")
        return False
    if r.returncode != 0 or not VENV_PY.exists():
        say(f"  [失败] 创建虚拟环境失败：{(r.stderr or r.stdout or '').strip()[:300]}")
        return False
    return True


def install_optional() -> None:
    """尽力装可选依赖（zstd 解压）。没有 wheel 就跳过，不报错、不阻塞。"""
    cmd = [str(VENV_PY), "-m", "pip", "install", "--disable-pip-version-check",
           "--no-input", "--no-cache-dir"]
    if WHEELS.is_dir() and any(WHEELS.glob("zstandard*.whl")):
        cmd += ["--no-index", "--find-links", str(WHEELS), "--only-binary=:all:"]
    else:
        cmd += ["-i", MIRROR, "--timeout", "15", "--retries", "0", "--only-binary=:all:"]
    try:
        r = subprocess.run(cmd + OPTIONAL_PKGS, capture_output=True, text=True,
                           timeout=120, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            say("  [OK] 可选组件 zstandard 已就绪（微信压缩消息可正常解码）")
    except (subprocess.TimeoutExpired, OSError):
        pass


def ensure_optional_silent(marker: Path | None = None) -> bool:
    """老 .venv 里缺 zstandard 时补装一次，之后靠 marker 不再尝试。

    为什么需要它：install_optional() 只在「新建 .venv」时被调用。
    升级覆盖安装的用户 .venv 已经存在，永远走不到那条路，于是微信里
    zstd 压缩的正文会一直解不出来。这里在每轮采集前做一次轻量检查，
    成功或失败都写 marker，避免每轮都跑 pip。
    """
    try:
        import zstandard  # noqa: F401, PLC0415
        return False        # 已就绪：不算「补装」，调用方不要打印安装提示
    except ImportError:
        pass
    if not WHEELS.is_dir() or not any(WHEELS.glob("zstandard*.whl")):
        return False
    mark = marker or (ROOT / "work" / ".optional_deps_checked")
    if mark.exists():
        return False
    install_optional()
    try:
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text("zstandard\n", encoding="utf-8")
    except OSError:
        pass
    return True


def pip_install(args: list[str], timeout: int, label: str) -> bool:
    say(f"  {label}")
    cmd = [str(VENV_PY), "-m", "pip", "install",
           "--disable-pip-version-check", "--no-input", "--no-cache-dir"] + args + PKGS
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        say(f"      超时（>{timeout}s），换下一种方式")
        return False
    if r.returncode == 0:
        return True
    tail = ((r.stderr or "") + (r.stdout or "")).strip().splitlines()[-4:]
    say("      " + " / ".join(tail) if tail else "      安装失败")
    return False


def is_portable_runtime() -> bool:
    return PORTABLE_MARKER.exists()


def install_deps() -> bool:
    # 0) 便携运行时（embeddable，没有 pip）：wheel 就是 zip，直接解压到 site-packages。
    #    这一支必须排在 pip 之前，否则 `-m pip` 直接 "No module named pip"。
    if is_portable_runtime():
        try:
            import python_bootstrap  # noqa: PLC0415
        except ImportError:
            say("  [失败] 缺少 tools/python_bootstrap.py，无法给便携运行时装依赖")
            return False
        cap = python_bootstrap.probe_capabilities(VENV_PY)
        try:
            minor = int(str(cap.get("version") or "").split(".")[1])
        except (IndexError, ValueError):
            say(f"  [失败] 读不出便携运行时的版本号：{cap.get('version')}")
            return False
        say(f"  便携运行时（Python 3.{minor}，无 pip），改用 wheel 解压安装 ...")
        res = python_bootstrap.install_wheels(VENV_PY, minor)
        if res["ok"]:
            say("  [OK] 离线安装完成（wheel 解压）")
            return True
        say(f"  [失败] {res['message']}")
        return False

    # 1) 离线 wheel（首选：不联网、不会卡）
    if WHEELS.is_dir() and any(WHEELS.glob("*.whl")):
        ok = pip_install(
            ["--no-index", "--find-links", str(WHEELS), "--only-binary=:all:"],
            TIMEOUT_LOCAL, "从随包 wheels 离线安装（约 10 秒）...",
        )
        if ok:
            say("  [OK] 离线安装完成")
            install_optional()
            return True
    else:
        say("  [注意] 发行包里没有 vendor\\wheels，只能尝试联网安装")

    # 2) 国内镜像（带超时，绝不无限等）
    ok = pip_install(
        ["-i", MIRROR, "--timeout", "20", "--retries", "1", "--only-binary=:all:"],
        TIMEOUT_NET, "离线包不可用，改用清华镜像（最多 4 分钟）...",
    )
    if ok:
        say("  [OK] 镜像安装完成")
        install_optional()
        return True

    # 3) 官方源兜底
    ok = pip_install(
        ["--timeout", "20", "--retries", "1", "--only-binary=:all:"],
        TIMEOUT_NET, "镜像失败，改用官方源再试一次（最多 4 分钟）...",
    )
    if ok:
        say("  [OK] 官方源安装完成")
        install_optional()
        return True
    return False


def verify() -> tuple[bool, str]:
    r = subprocess.run(
        [str(VENV_PY), "-c",
         "import sqlcipher3, Crypto;"
         "from sqlcipher3 import dbapi2 as sc;"
         "print('sqlcipher', sc.sqlite_version)"],
        capture_output=True, text=True, timeout=90, encoding="utf-8", errors="replace",
    )
    if r.returncode == 0:
        return True, (r.stdout or "").strip()
    return False, ((r.stderr or "").strip().splitlines() or ["未知错误"])[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description="准备 Python 运行环境与依赖")
    ap.add_argument("--check", action="store_true", help="只报告，不修改")
    ap.add_argument("--force", action="store_true", help="强制重建 .venv")
    ap.add_argument("--offline", action="store_true",
                    help="禁止联网：只允许用 vendor/wheels 与 vendor/python 里的随包资产")
    args = ap.parse_args()

    say("=" * 60)
    say("  准备运行环境")
    say("=" * 60)

    interp, why = pick_interpreter()
    say(f"  {why}")
    if interp is None:
        # 本机确实没有受支持的 Python：先尝试自动获取便携运行时（离线资产优先），
        # 拿到就直接可用；拿不到才报错退出 —— 绝不退化成"残缺控制台"。
        if args.check:
            say("  [提示] 本机没有 3.10~3.13；加 --auto 或双击「获取Python.bat」可自动准备")
            return 1
        say("")
        say("  [*] 本机没有 3.10~3.13，尝试自动准备便携 Python 运行时 ...")
        try:
            import python_bootstrap  # noqa: PLC0415
        except ImportError:
            python_bootstrap = None  # type: ignore[assignment]
        if python_bootstrap is not None:
            res = python_bootstrap.ensure_runtime(offline=args.offline)
            if res.get("ok"):
                say("")
                say("  [OK] 便携运行时已就绪，后续采集可直接使用。")
                return 0
            say(f"  [失败] {res.get('message')}", err=True)
        else:
            say("  [失败] 缺少 tools/python_bootstrap.py，无法自动准备运行时", err=True)
        say("")
        say("  [失败] 找不到可用的 Python。")
        say("  可以这样准备（任选其一）：")
        say("    1) 双击「获取Python.bat」，自动下载便携版（约 11 MB，无需安装）")
        say("    2) 手工安装 Python 3.13（Windows x64），安装时勾选 Add Python to PATH，")
        say("       下载：https://www.python.org/downloads/")
        say("    3) 断网环境：在能上网的机器上执行")
        say("         python tools\\python_bootstrap.py --fetch --pack")
        say("       把 dist\\python-runtime-*.zip 解压合并到本程序目录后再运行本脚本")
        return 1
    say(f"  受支持版本，可以继续。")

    if args.check:
        ok, note = venv_ready(interp)
        say(f"  .venv 状态: {note}")
        return 0 if ok else 2

    # 自我保护：不能用 .venv 里的 python 去重建 .venv（文件被占用，删不掉）
    try:
        running_in_venv = Path(sys.executable).resolve().is_relative_to(VENV.resolve())
    except (ValueError, OSError):
        running_in_venv = str(VENV.resolve()).lower() in str(Path(sys.executable).resolve()).lower()
    if running_in_venv:
        say("  [失败] 现在是用项目自带的 Python 在跑，没法重建它自己。")
        say("         请改用 Admin_Init.bat / Install.bat 启动（它们会用系统 Python 来准备环境）。")
        return 1

    if not args.force:
        ok, note = venv_ready(interp)
        if ok:
            say(f"  [OK] {note}，跳过安装")
            return 0
        say(f"  需要重建环境：{note}")

    # 便携运行时（embeddable 铺的）不能重建，也没有 pip —— 只能补依赖。
    if is_portable_runtime() and not args.force:
        say("  检测到便携运行时：就地补依赖（不重建目录、不用 pip）")
        if not install_deps():
            say("  [失败] 便携运行时补依赖失败")
            return 1
        ok, note = verify()
        if ok:
            say(f"  [OK] 依赖可用（{note}）")
            return 0
        say(f"  [失败] 依赖装了但导入失败：{note}")
        return 1

    if not make_venv(interp):
        return 1
    if not install_deps():
        say("  [失败] 依赖安装没成功。")
        say("  下面的办法任选其一：")
        say("    1) 让发行包里带上 vendor\\wheels（可离线装）")
        say("    2) 手动执行：.venv\\Scripts\\python.exe -m pip install sqlcipher3-wheels pycryptodome -i https://pypi.tuna.tsinghua.edu.cn/simple")
        return 1

    ok, note = verify()
    if ok:
        say(f"  [OK] 依赖可用（{note}）")
        return 0
    say(f"  [失败] 依赖装了但导入失败：{note}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
