"""离线/在线获取并装配便携 Python 运行时。

为什么需要这个模块
v1.5.3.2 及以前，本程序只在启动脚本里提示「请去 python.org 下载安装」——
目标机器上一旦没有 3.10~3.13，整条 QQ/微信链路就用不了。而 docs/BUILD-INFO.txt
里却写着「便携 Python 自动就绪」，属于文档承诺了、代码没实现。

本模块补上这个缺口，做三件事：
    1. 取：优先用随包的 vendor\\python\\*.zip（纯离线、零网络），没有才联网下载，
       四个源依次回退；支持企业内网 --base-url（http/https/file/UNC）。
    2. 装：把官方 embeddable 包装配成**标准 venv 布局**，让全工程既有的
       `.venv\\Scripts\\python.exe` 路径约定原样成立，不用改任何调用方。
    3. 分发：--pack 把已就绪的运行时打成 zip，拷到别的机器解压即用（零网络）。

关键技术点（都是实测出来的，别随手改）
    · 官方 embeddable 包**没有 venv、也没有 ensurepip**，所以 `python -m venv`
      必然失败 —— 本模块不调用它，而是手工铺 venv 目录：
          .venv/Scripts/             ← 整个 embed 包内容（exe + dll + *.pyd + pythonXY.zip）
          .venv/pyvenv.cfg           ← home = <root>/Scripts，version = X.Y.Z
          .venv/Lib/site-packages/   ← 依赖 wheel 直接解压到这里
    · 必须删除 .venv/Scripts/pythonXY._pth。只要它还在，CPython 就按 ._pth 里列的
      路径重建 sys.path，site-packages 永远不在路径上，import 必定失败。
    · pythonXY.dll 必须和 python.exe 放在一起。否则 Windows 会从 PATH 上捞到
      别的版本 DLL，出现「3.13.9 的解释器报 3.13.14」这种串版本运行。
    · purelib 必须在删完 ._pth **之后**再问解释器（sysconfig.get_paths），
      删之前拿到的是错的（指向 Scripts/Lib/site-packages）。
    · 没有 pip 也能装依赖：wheel 本质就是 zip，直接解压到 site-packages 即可；
      sqlcipher3 / pycryptodome / zstandard 三个包都是纯二进制 wheel，没有
      需要执行的安装脚本，解压即可用。

用法
----
    python tools\\python_bootstrap.py --plan            # 体检：目标版本/本地资产/现状
    python tools\\python_bootstrap.py --auto            # 一键获取并装配（本地优先，其次联网）
    python tools\\python_bootstrap.py --auto --offline  # 断网：只认 vendor\\python\\*.zip
    python tools\\python_bootstrap.py --fetch           # 只把 zip 取到 vendor\\python\\（便于再分发）
    python tools\\python_bootstrap.py --finalize        # 只装配（zip 已就位）
    python tools\\python_bootstrap.py --pack            # 打成可分发的运行时 zip
    python tools\\python_bootstrap.py --verify          # 校验现有运行时是否真的可用
    python tools\\python_bootstrap.py --json            # 上面任意动作 + 机器可读输出

    --version 3.13.9     指定精确版本
    --base-url URL       自定义源（内网文件服务器 / file:// / UNC）
    --sha256 HEX         强制校验 zip 摘要
    --force              已存在真实 venv 时也覆盖重建
    --keep               联网取到 zip 后保留在 vendor\\python\\（默认保留，便于分发）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WHEEL_DIR = ROOT / "vendor" / "wheels"
PY_DIST_DIR = ROOT / "vendor" / "python"
MANIFEST = PY_DIST_DIR / "sources.json"
LOG_DIR = ROOT / "work"

VENV = ROOT / ".venv"
# 便携运行时的标记文件：有它 = 这个 .venv 是本模块手工铺的（没有 pip、不能重建）
MARKER_NAME = ".qq-todo-portable.json"

UA = "qq-todo-python-bootstrap/1.0 (+offline runtime provisioner)"
TIMEOUT_CONNECT = 20
MIN_ZIP_BYTES = 5 * 1024 * 1024      # 官方 embed 包实测 ~10.9MB，小于 5MB 一定是错误页

# 依次尝试的公共源。顺序按「先官方、后国内镜像」——官方拿不到才换镜像，
# 避免镜像同步延迟导致取到半旧版本。
MIRRORS = (
    "https://www.python.org/ftp/python/{ver}/python-{ver}-embed-amd64.zip",
    "https://registry.npmmirror.com/-/binary/python/{ver}/python-{ver}-embed-amd64.zip",
    "https://mirrors.huaweicloud.com/python/{ver}/python-{ver}-embed-amd64.zip",
    "https://mirrors.aliyun.com/python-release/windows/python-{ver}-embed-amd64.zip",
)
INDEX_URL = "https://registry.npmmirror.com/-/binary/python/"
OFFICIAL_INDEX = "https://www.python.org/ftp/python/"

# 本程序解析依赖所必需的三个包（顺序无关，但都要装上）
REQUIRED_IMPORTS = ("sqlcipher3", "Crypto")
OPTIONAL_IMPORTS = ("zstandard",)


# 小工具
def say(msg: str = "", err: bool = False) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass
    print(msg, file=sys.stderr if err else sys.stdout, flush=True)


def _log(line: str) -> None:
    """把关键动作落到 work/python-bootstrap.log，便于事后排查。"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / "python-bootstrap.log", "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}\n")
    except OSError:
        pass


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _work_tmp(prefix: str) -> Path:
    """临时目录放在 work/ 下（避免落到 C 盘系统临时目录）。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(LOG_DIR)))


# 版本与资产发现
def supported_minors() -> list[int]:
    """从 vendor/wheels 里反推本包支持的 CPython 小版本（10~13）。

    sqlcipher3 只有 cp310~cp313 的预编译包，所以「支持哪些版本」这件事的
    唯一权威来源是轮子文件名，不能写死。
    """
    found: set[int] = set()
    if WHEEL_DIR.is_dir():
        for f in WHEEL_DIR.glob("sqlcipher3_wheels-*-cp3*-cp3*-win_amd64.whl"):
            m = re.search(r"-cp3(\d+)-cp3", f.name)
            if m:
                found.add(int(m.group(1)))
    return sorted(found, reverse=True)


def _local_zips() -> list[tuple[tuple[int, int, int], Path]]:
    """vendor/python 下已有的 embed zip，按版本号降序。"""
    out: list[tuple[tuple[int, int, int], Path]] = []
    if not PY_DIST_DIR.is_dir():
        return out
    for f in PY_DIST_DIR.glob("python-3.*-embed-amd64.zip"):
        m = re.search(r"python-3\.(\d+)\.(\d+)-embed-amd64\.zip$", f.name)
        if m:
            out.append(((3, int(m.group(1)), int(m.group(2))), f))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def _http_json(url: str, timeout: int = 25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _dir_listing(base: str, timeout: int = 25) -> list[str] | None:
    """列出一个版本目录下的文件名。

    优先用镜像的 JSON 索引（快、结构化）；官方 python.org 只有 HTML 目录页，
    用正则捞 href。两者都拿不到就返回 None，由调用方决定是否盲试下载。
    """
    try:
        req = urllib.request.Request(base if base.endswith("/") else base + "/",
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        if raw.lstrip().startswith("["):
            data = json.loads(raw)
            return [str(x.get("name")) for x in data if isinstance(x, dict) and x.get("name")]
        return re.findall(r'href="([^"/?][^"]*)"', raw)
    except Exception:  # noqa: BLE001
        return None


def discover_versions(minors: list[int]) -> list[tuple[tuple[int, int, int], list[str]]]:
    """联网发现「确实带 embed-amd64 资产」的版本，按版本降序返回。

    为什么不能简单取「最新小版本」：官方对部分安全修复版只发源码包，不发
    embed-amd64（实测 3.13.18 有目录、没 zip）。盲取会 404，所以这里逐个版本
    目录确认资产存在。
    """
    cache = _load_manifest()
    cached_dirs = cache.get("dirs", {}) if isinstance(cache, dict) else {}

    names: list[str] = []
    for idx in (INDEX_URL, OFFICIAL_INDEX):
        listing = _dir_listing(idx)
        if listing:
            names = [n.strip("/") for n in listing if re.match(r"^3\.1[0-3]\.\d+/?$", n)]
            if names:
                break
    if not names:
        say("  [注意] 版本索引取不到（可能断网/被墙），只能使用本地 zip 或 --version 明确的版本", err=True)
        return []

    ver_list = sorted({tuple(int(x) for x in n.split(".")) for n in names}, reverse=True)
    out: list[tuple[tuple[int, int, int], list[str]]] = []
    probes = 0
    for ver in ver_list:
        if ver[1] not in minors:
            continue
        if probes >= 6:            # 最多探 6 个版本目录，避免一串 HTTP 拖时间
            break
        probes += 1
        ver_s = f"{ver[0]}.{ver[1]}.{ver[2]}"
        files = cached_dirs.get(ver_s)
        if not files:
            files = _dir_listing(f"{INDEX_URL}{ver_s}/")
            if files:
                cached_dirs[ver_s] = files
        if not files:
            continue
        if f"python-{ver_s}-embed-amd64.zip" in files:
            out.append((ver, files))
        else:
            say(f"  · {ver_s} 官方未提供 embed-amd64（只有源码包），跳过", err=True)
    if cached_dirs:
        _save_manifest_dirs(cached_dirs)
    out.sort(key=lambda x: x[0], reverse=True)
    return out


# manifest（sources.json）
def _load_manifest() -> dict:
    if MANIFEST.exists():
        try:
            data = json.loads(MANIFEST.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            pass
    return {}


def _save_manifest(data: dict) -> None:
    try:
        PY_DIST_DIR.mkdir(parents=True, exist_ok=True)
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        say(f"  [注意] 写入 {MANIFEST.name} 失败: {exc}", err=True)


def _save_manifest_dirs(dirs: dict) -> None:
    data = _load_manifest()
    data.setdefault("dirs", {}).update(dirs)
    _save_manifest(data)


def manifest_entry(version: str) -> dict:
    return (_load_manifest().get("pins") or {}).get(version) or {}


def pin_version(version: str, digest: str, source: str) -> None:
    data = _load_manifest()
    pins = data.setdefault("pins", {})
    pins[version] = {"sha256": digest, "source": source,
                     "pinned_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    _save_manifest(data)


# 运行时现状
def portable_marker(target: Path | None = None) -> dict | None:
    p = (target or VENV) / MARKER_NAME
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def runtime_python(target: Path | None = None) -> Path:
    return (target or VENV) / "Scripts" / "python.exe"


def probe_capabilities(py: Path) -> dict:
    """问解释器自己：版本、site-packages、三大依赖是否导入得动。"""
    info: dict = {"ok": False, "python": str(py), "version": "", "purelib": "",
                  "imports": {}, "error": ""}
    if not py.exists():
        info["error"] = "解释器不存在"
        return info
    code = (
        "import sys,sysconfig,json;"
        "caps={};\n"
        "for n in ('sqlcipher3','Crypto','zstandard'):\n"
        "    try:\n"
        "        __import__(n);caps[n]=True\n"
        "    except Exception:\n"
        "        caps[n]=False\n"
        "print(json.dumps({'v':sys.version.split()[0],"
        "'purelib':sysconfig.get_paths().get('purelib',''),'caps':caps}))"
    )
    try:
        r = subprocess.run([str(py), "-c", code], capture_output=True, text=True,
                           timeout=90, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            info["error"] = (r.stderr or "").strip().splitlines()[-1:] or ["-c 执行失败"]
            info["error"] = str(info["error"])
            return info
        data = json.loads((r.stdout or "").strip().splitlines()[-1])
        info.update(ok=True, version=data.get("v", ""), purelib=data.get("purelib", ""),
                    imports=data.get("caps", {}))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def runtime_ready(target: Path | None = None) -> tuple[bool, str]:
    target = target or VENV
    py = runtime_python(target)
    if not py.exists():
        return False, f"还没有运行时（{py} 不存在）"
    cap = probe_capabilities(py)
    if not cap["ok"]:
        return False, f"解释器起不来：{cap['error']}"
    missing = [n for n in REQUIRED_IMPORTS if not cap["imports"].get(n)]
    if missing:
        return False, f"Python {cap['version']} 可用，但缺少 {', '.join(missing)}"
    return True, f"就绪（Python {cap['version']}，依赖齐全）"


def is_real_venv(target: Path | None = None) -> bool:
    """判断是不是「真正 python -m venv 建的」venv（有 pip、可重建）。"""
    target = target or VENV
    if portable_marker(target) is not None:
        return False
    return (target / "pyvenv.cfg").exists() and (target / "Scripts" / "pip.exe").exists()


# 取包
def _download(url: str, dest: Path) -> tuple[bool, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_CONNECT) as r, open(dest, "wb") as fh:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            last = 0.0
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                now = time.time()
                if now - last > 2:          # 每 2 秒刷一行进度，别把控制台刷爆
                    last = now
                    pct = f"{got * 100 // total}%" if total else f"{got // 1024}KB"
                    say(f"      ... {pct}", err=True)
        if got < MIN_ZIP_BYTES:
            return False, f"文件只有 {got} 字节，不像官方 embed 包（多半是错误页）"
        return True, f"{got} 字节"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _validate_zip(path: Path) -> tuple[bool, str, str]:
    """校验 zip 结构并读出内嵌版本号。"""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if z.testzip() is not None:
                return False, "zip 内部 CRC 校验失败（下载不完整）", ""
            if "python.exe" not in names:
                return False, "包内没有 python.exe，不是官方 embed 包", ""
            stdlib = [n for n in names if re.match(r"python3\d+\.zip$", n)]
            if not stdlib:
                return False, "包内没有标准库 zip，包结构异常", ""
            minor = re.search(r"python3(\d+)\.zip$", stdlib[0])
            return True, "结构 OK", f"3.{minor.group(1)}" if minor else ""
    except (zipfile.BadZipFile, OSError) as exc:
        return False, f"不是合法 zip: {exc}", ""


def fetch(version: str | None = None, offline: bool = False,
          base_url: str | None = None, want_sha: str | None = None,
          keep: bool = True) -> tuple[Path | None, str]:
    """把目标版本的 embed zip 弄到本地（优先复用 vendor/python 里的）。"""
    minors = supported_minors()
    if not minors:
        return None, "vendor/wheels 里没有 sqlcipher3 预编译包，无法确定支持的版本"

    # 1) 本地已有
    local = _local_zips()
    if local:
        for ver, path in local:
            if version and f"{ver[0]}.{ver[1]}.{ver[2]}" != version:
                continue
            ok, msg, _ = _validate_zip(path)
            if ok:
                say(f"  [OK] 使用随包资产：{path.name}（{path.stat().st_size // 1024} KB，零网络）")
                return path, "local"
            say(f"  [注意] 本地 {path.name} 校验不过（{msg}），跳过", err=True)

    if offline:
        return None, (
            "离线模式下没有可用的本地资产。请把 python-3.x.y-embed-amd64.zip 放进 "
            f"{PY_DIST_DIR}（实施员联网机器上跑 --fetch 即可生成）"
        )

    # 2) 确定版本
    if version:
        target_ver = version
    else:
        say("  正在查询可用版本 ...")
        found = discover_versions(minors)
        if not found:
            return None, "联网也拿不到版本清单；请用 --version 指定，或用 --offline 配合本地 zip"
        target_ver = f"{found[0][0][0]}.{found[0][0][1]}.{found[0][0][2]}"
    say(f"  目标版本：Python {target_ver} embeddable (win-amd64)")

    # 3) 依次尝试各源
    urls: list[str] = []
    if base_url:
        urls.append(base_url.rstrip("/") + f"/python-{target_ver}-embed-amd64.zip")
    urls += [m.format(ver=target_ver) for m in MIRRORS]

    tmp = Path(tempfile.mkdtemp(prefix="pydl-", dir=str(_work_tmp("pydl-root"))))
    dest = tmp / f"python-{target_ver}-embed-amd64.zip"
    last_err = ""
    for url in urls:
        say(f"  → 尝试 {url}")
        ok, msg = _download(url, dest)
        if not ok:
            last_err = f"{url} → {msg}"
            say(f"      [失败] {msg}", err=True)
            continue
        ok, msg, _ = _validate_zip(dest)
        if not ok:
            last_err = f"{url} → {msg}"
            say(f"      [失败] {msg}", err=True)
            continue
        digest = sha256_of(dest)
        pin = (want_sha or manifest_entry(target_ver).get("sha256") or "").lower()
        if pin:
            if digest != pin:
                last_err = f"{url} → 摘要不匹配（期望 {pin[:16]}…，实得 {digest[:16]}…）"
                say(f"      [失败] {last_err}", err=True)
                continue
            say(f"      [OK] sha256 与固化值一致（{digest[:16]}…）")
        else:
            say(f"      [OK] 已下载，sha256={digest}（未固化；可用 --sha256 或 --pin 锁定）")
            _log(f"download {target_ver} from {url} sha256={digest}")
        if keep:
            PY_DIST_DIR.mkdir(parents=True, exist_ok=True)
            final = PY_DIST_DIR / dest.name
            shutil.copy2(dest, final)
            say(f"  已保存随包资产：{final}（下次可在完全断网的机器上直接用）")
        return dest, ("pinned" if pin else "downloaded")

    return None, f"所有源都失败。最后一次：{last_err}"


# 装配
def _safe_extract_zip(zf: zipfile.ZipFile, dest: Path) -> None:
    """安全解压 Zip，防御 Zip Slip（路径穿越、绝对路径与符号链接逃逸）。"""
    dest_resolved = dest.resolve()
    for info in zf.infolist():
        name = info.filename
        if not name or name.startswith(("/", "\\")):
            raise ValueError(f"压缩包包含非法绝对路径: {name}")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError(f"压缩包包含不安全的符号链接条目: {name}")
        target = (dest_resolved / name).resolve()
        try:
            in_dest = target.is_relative_to(dest_resolved)
        except AttributeError:
            in_dest = str(target).startswith(str(dest_resolved))
        if not in_dest:
            raise ValueError(f"压缩包包含路径穿越条目: {name}")
    zf.extractall(dest_resolved)


def _pick_wheel(prefix: str, minor: int) -> Path | None:
    """在 vendor/wheels 里挑出适用于 3.<minor> 的那个 wheel。

    兼容两类命名：cp3XX-cp3XX（sqlcipher3 / zstandard）与 cp3X-abi3
    （pycryptodome，向上升级兼容，只要解释器 ≥ 该 ABI 版本即可）。
    """
    if not WHEEL_DIR.is_dir():
        return None
    best: tuple[int, Path] | None = None
    for w in WHEEL_DIR.glob("*.whl"):
        low = w.name.lower()
        if not low.startswith(prefix.lower()):
            continue
        m = re.search(r"-cp3(\d+)-cp3(\d+)-win_amd64\.whl$", low)
        if not m:
            m2 = re.search(r"-cp3(\d+)-abi3-win_amd64\.whl$", low)
            if not m2:
                continue
            abi = int(m2.group(1))
            if minor >= abi and (best is None or abi > best[0]):
                best = (abi, w)
            continue
        if int(m.group(1)) == minor and int(m.group(2)) == minor:
            return w
    return best[1] if best else None


def install_wheels(py: Path, minor: int, extra: tuple[str, ...] = ()) -> dict:
    """把依赖 wheel 直接解压进该解释器的 site-packages（不需要 pip）。

    embeddable 包没有 pip，而 wheel 本身就是 zip，所以按 zip 解压是最稳的
    离线安装方式。三个依赖都是纯二进制包，没有需要执行的安装脚本。
    """
    cap = probe_capabilities(py)
    if not cap["ok"]:
        return {"ok": False, "message": f"解释器不可用：{cap['error']}", "installed": []}
    purelib = Path(cap["purelib"])
    purelib.mkdir(parents=True, exist_ok=True)

    wanted = ["sqlcipher3", "pycryptodome", "zstandard"] + list(extra)
    installed: list[str] = []
    problems: list[str] = []
    for name in wanted:
        whl = _pick_wheel(name, minor)
        if whl is None:
            if name in ("sqlcipher3", "pycryptodome"):
                problems.append(f"{name}: vendor/wheels 里没有适用于 3.{minor} 的 wheel")
            continue
        try:
            with zipfile.ZipFile(whl) as z:
                _safe_extract_zip(z, purelib)
            installed.append(f"{name}（{whl.name}）")
            say(f"      [OK] {name} ← {whl.name}")
        except (zipfile.BadZipFile, OSError) as exc:
            problems.append(f"{name}: 解压失败 {exc}")

    cap2 = probe_capabilities(py)
    missing = [n for n in REQUIRED_IMPORTS if not cap2["imports"].get(n)]
    ok = not missing and not problems
    if missing:
        problems.append("导入校验未通过：" + ", ".join(missing))
    return {"ok": ok, "message": "；".join(problems) or "依赖齐全", "installed": installed,
            "site_packages": str(purelib), "capacities": cap2["imports"]}


def running_from(target: Path | None = None) -> bool:
    """当前进程是不是就跑在 target 这个运行时里。

    这个判断非常关键：install_portable() 会 rmtree 目标目录再重建。如果当前
    解释器正是目标目录里的 python.exe，删目录等于把自己脚下的地板抽掉，
    表现为 `Fatal Python error: error evaluating path`（getpath 找不到自己）。
    所以重建前必须先排掉这种「自己重建自己」的情形，改用就地收尾。
    """
    target = (target or VENV).resolve()
    try:
        exe = Path(sys.executable).resolve()
    except OSError:
        return False
    try:
        return exe.is_relative_to(target)
    except (ValueError, OSError):
        return str(exe).lower().startswith(str(target).lower())


def finalize_in_place(target: Path | None = None) -> dict:
    """就地收尾：不重建目录，只补依赖 + 写标记 + 校验。

    适用场景：引导层（get_python.ps1）已经把 embed 包铺进 .venv/Scripts 了，
    只差 wheel 还没装。此时用 .venv 自己的 python 跑本函数是安全的。
    """
    target = target or VENV
    py = runtime_python(target)
    if not py.exists():
        return {"ok": False, "message": f"{py} 不存在，无法就地收尾"}
    cap = probe_capabilities(py)
    if not cap["ok"]:
        return {"ok": False, "message": f"现有解释器起不来：{cap['error']}"}

    try:
        minor = int(str(cap["version"]).split(".")[1])
    except (IndexError, ValueError):
        return {"ok": False, "message": f"读不出 Python 版本号：{cap['version']}"}
    if minor not in supported_minors():
        return {"ok": False,
                "message": f"现有运行时是 Python 3.{minor}，但 vendor/wheels 只支持 "
                           f"{', '.join('3.' + str(m) for m in supported_minors())}"}

    say(f"  就地收尾：Python {cap['version']} @ {target}")

    # 引导层（PowerShell）可能漏写或写坏 pyvenv.cfg（例如用 ASCII 编码写中文路径）。
    # 这里按最终路径重写一份，保证后续所有按 sys.prefix 推断 site-packages 的代码可用。
    cfg_path = target / "pyvenv.cfg"
    want = f"home = {target / 'Scripts'}"
    try:
        current = cfg_path.read_text(encoding="utf-8", errors="replace") if cfg_path.exists() else ""
    except OSError:
        current = ""
    if want not in current:
        try:
            cfg_path.write_text(
                f"{want}\n"
                f"include-system-site-packages = false\n"
                f"version = {cap['version']}\n"
                f"executable = {py}\n"
                f"command = qq-todo portable runtime (embeddable)\n",
                encoding="utf-8",
            )
            if want not in current:
                say("  已修正 pyvenv.cfg（home 路径指向最终目录）")
        except OSError as exc:
            say(f"  [注意] 写 pyvenv.cfg 失败：{exc}", err=True)

    res = install_wheels(py, minor)
    marker = {
        "kind": "portable-embeddable",
        "version": cap["version"],
        "minor": minor,
        "zip": "",
        "sha256": "",
        "pip": False,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "wheels": res.get("installed", []),
        "note": "由 tools/python_bootstrap.py 就地收尾；没有 pip，改用 wheel 解压安装依赖",
    }
    try:
        (target / MARKER_NAME).write_text(json.dumps(marker, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    except OSError:
        pass
    _log(f"finalize_in_place {cap['version']} -> {target} ok={res['ok']} {res['message']}")
    return {"ok": res["ok"], "message": res["message"], "version": cap["version"],
            "target": str(target), "wheels": res.get("installed", []),
            "site_packages": res.get("site_packages", "")}


def install_portable(zip_path: Path, target: Path | None = None,
                     force: bool = False, version_hint: str = "") -> dict:
    """把 embed zip 装配成标准 venv 布局（会重建目标目录）。"""
    target = target or VENV
    if running_from(target):
        # 自己重建自己一定会崩：退化为就地收尾（目录布局已经对的话）
        say("  检测到当前解释器就在目标目录内，改用就地收尾（不重建）", err=True)
        return finalize_in_place(target)
    ok, msg, minor_s = _validate_zip(zip_path)
    if not ok:
        return {"ok": False, "message": f"zip 校验失败：{msg}"}
    minor = int(minor_s.split(".")[1]) if minor_s else 0
    if not minor or minor not in supported_minors():
        return {"ok": False,
                "message": f"该包是 Python {minor_s}，但 vendor/wheels 只支持 "
                           f"{', '.join('3.' + str(m) for m in supported_minors())}"}

    if target.exists() and not force:
        if is_real_venv(target):
            return {"ok": False, "message":
                    f"{target} 是一个真正 venv（带 pip），不覆盖。确实要重建请加 --force"}
        if portable_marker(target) is not None:
            say(f"  检测到已存在的便携运行时，先清理重建：{target}")

    say(f"  正在装配到 {target} ...")
    tmp = Path(tempfile.mkdtemp(prefix="pyunzip-", dir=str(_work_tmp("pyunzip-root"))))
    staged = target.with_name(target.name + ".new")
    try:
        with zipfile.ZipFile(zip_path) as z:
            _safe_extract_zip(z, tmp)

        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
        (staged / "Scripts").mkdir(parents=True, exist_ok=True)
        (staged / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)

        # 1) embed 包内容整体进 Scripts/（exe + dll + *.pyd + pythonXY.zip 必须同目录）
        for item in tmp.iterdir():
            dst = staged / "Scripts" / item.name
            if item.is_dir():
                shutil.copytree(item, dst)
            else:
                shutil.copy2(item, dst)

        # 2) 删 ._pth，否则 sys.path 被它锁死，site-packages 永远不在路径上
        for pth in (staged / "Scripts").glob("python3*._pth"):
            pth.unlink(missing_ok=True)

        # 3) 就位：先删旧目录再改名，避免出现半新半旧
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            if target.exists():
                broken = target.with_name(target.name + ".broken")
                shutil.rmtree(broken, ignore_errors=True)
                try:
                    target.rename(broken)
                except OSError as exc:
                    return {"ok": False, "message": f"旧运行时被占用，无法替换：{exc}"}
        staged.rename(target)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)

    # 4) pyvenv.cfg 必须在**改名之后**写：里面是最终绝对路径。
    #    编码必须是 UTF-8 无 BOM —— 安装路径含中文时，用 ASCII 会把路径写成 "?????"，
    #    CPython 的 getpath 随后直接 "OSError: [Errno 22] Invalid argument"。
    py = runtime_python(target)
    ver_full = version_hint or f"3.{minor}.0"
    cfg_path = target / "pyvenv.cfg"

    def _write_cfg(ver: str) -> None:
        cfg_path.write_text(
            f"home = {target / 'Scripts'}\n"
            f"include-system-site-packages = false\n"
            f"version = {ver}\n"
            f"executable = {py}\n"
            f"command = qq-todo portable runtime (embeddable)\n",
            encoding="utf-8",
        )

    _write_cfg(ver_full)
    try:
        r = subprocess.run([str(py), "-c", "import sys;print(sys.version.split()[0])"],
                           capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
            return {"ok": False, "message": "装配后解释器无法启动：" + " / ".join(tail)}
        ver_full = (r.stdout or "").strip().splitlines()[-1]
    except (OSError, subprocess.SubprocessError, IndexError) as exc:
        return {"ok": False, "message": f"装配后解释器无法启动：{exc}"}
    _write_cfg(ver_full)

    # 5) 装依赖（此时 ._pth 已删，purelib 才是对的）
    res = install_wheels(py, minor)
    digest = sha256_of(zip_path)
    marker = {
        "kind": "portable-embeddable",
        "version": ver_full,
        "minor": minor,
        "zip": zip_path.name,
        "sha256": digest,
        "pip": False,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "wheels": res.get("installed", []),
        "note": "由 tools/python_bootstrap.py 生成；没有 pip，改用 wheel 解压安装依赖",
    }
    try:
        (target / MARKER_NAME).write_text(json.dumps(marker, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    except OSError:
        pass
    _log(f"install_portable {ver_full} -> {target} ok={res['ok']} {res['message']}")

    return {"ok": res["ok"], "message": res["message"], "version": ver_full,
            "target": str(target), "wheels": res.get("installed", []),
            "site_packages": res.get("site_packages", ""), "sha256": digest}


# 分发打包
def pack(out: Path | None = None) -> tuple[bool, str, Path | None]:
    """把已取到的 zip + manifest 打成一个可拷走的压缩包。

    拷到别的机器上解压到 vendor/python/，即可完全断网装配（--offline）。
    """
    zips = _local_zips()
    if not zips:
        return False, f"{PY_DIST_DIR} 里没有 embed zip，先跑 --fetch", None
    ver, path = zips[0]
    ver_s = f"{ver[0]}.{ver[1]}.{ver[2]}"
    out = out or (ROOT / "dist" / f"python-runtime-{ver_s}-win-amd64.zip")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(path, f"vendor/python/{path.name}")
        if MANIFEST.exists():
            z.write(MANIFEST, "vendor/python/sources.json")
        z.writestr(
            "使用说明.txt",
            "本压缩包是「待办总结助手」的离线 Python 运行时资产。\n"
            "用法：解压后把 vendor 目录合并到程序根目录，然后运行\n"
            "    .venv\\Scripts\\python.exe tools\\python_bootstrap.py --auto --offline\n"
            "或直接双击「获取Python.bat」（它会自动识别本资产，全程不需要联网）。\n"
            f"版本：Python {ver_s} embeddable (win-amd64)\n"
            f"sha256：{sha256_of(path)}\n",
        )
    return True, f"{out}（{out.stat().st_size // 1024} KB）", out


# 对外主流程
def ensure_runtime(offline: bool = False, version: str | None = None,
                   base_url: str | None = None, want_sha: str | None = None,
                   force: bool = False, quiet: bool = False) -> dict:
    say("=" * 62)
    say("  准备便携 Python 运行时")
    say("=" * 62)

    ready, why = runtime_ready()
    if ready and not force:
        say(f"  [OK] {why}，无需处理")
        return {"ok": True, "action": "skip", "message": why,
                "runtime": probe_capabilities(runtime_python())}
    if why:
        say(f"  现状：{why}")

    # 当前进程就跑在这个 .venv 里、但依赖不全（典型：引导层刚铺好壳子）：
    # 只能就地补依赖，绝不能重建（会把自己删掉）。
    if running_from(VENV) and runtime_python().exists() and not force:
        say("  当前解释器位于目标运行时内，采用就地补依赖（不重建目录）")
        res = finalize_in_place()
        if res["ok"]:
            say(f"  [OK] 依赖已补齐：Python {res['version']}")
        else:
            say(f"  [失败] {res['message']}", err=True)
        res["action"] = "finalize-in-place"
        return res

    minors = supported_minors()
    say(f"  vendor/wheels 支持的版本：{', '.join('3.' + str(m) for m in minors) or '（空！）'}")
    if not minors:
        return {"ok": False, "message": "vendor/wheels 不完整，无法确定目标版本"}

    zip_path, how = fetch(version, offline=offline, base_url=base_url,
                          want_sha=want_sha, keep=True)
    if zip_path is None:
        say(f"  [失败] {how}", err=True)
        say("")
        say("  可以这样解决：")
        say("    1) 找一台能上网的机器：python tools\\python_bootstrap.py --fetch")
        say(f"       把生成的 {PY_DIST_DIR}\\*.zip 一起拷过来，再在目标机器上跑 --auto --offline")
        say("    2) 或用 --base-url 指向内网文件服务器")
        return {"ok": False, "message": how}

    res = install_portable(zip_path, force=force)
    if res["ok"]:
        say("")
        say(f"  [OK] 便携运行时已就绪：Python {res['version']}")
        say(f"       解释器：{res['target']}\\Scripts\\python.exe")
        say(f"       依赖：{'、'.join(res.get('wheels') or []) or '（无）'}")
        say("       该运行时无需安装、可整目录拷走，重启电脑后依然有效。")
    else:
        say(f"  [失败] 装配未完成：{res['message']}", err=True)
    res["source"] = how
    res["zip"] = str(zip_path)
    return res


def plan() -> dict:
    minors = supported_minors()
    local = _local_zips()
    ready, why = runtime_ready()
    marker = portable_marker()
    data = {
        "supported_versions": ["3." + str(m) for m in minors],
        "wheels_dir": str(WHEEL_DIR),
        "wheels_present": sorted(p.name for p in WHEEL_DIR.glob("*.whl")) if WHEEL_DIR.is_dir() else [],
        "local_assets": [str(p) for _, p in local],
        "manifest": str(MANIFEST),
        "runtime_ready": ready,
        "runtime_note": why,
        "runtime_python": str(runtime_python()),
        "portable_marker": marker,
        "is_real_venv": is_real_venv(),
        "mirrors": list(MIRRORS),
    }
    return data


def _print_plan(data: dict) -> None:
    say("=" * 62)
    say("  便携 Python 运行时 · 体检")
    say("=" * 62)
    say(f"  支持版本      : {', '.join(data['supported_versions']) or '（未知）'}")
    say(f"  依赖轮子      : {len(data['wheels_present'])} 个")
    say(f"  本地离线资产  : {'、'.join(Path(p).name for p in data['local_assets']) or '（无，需联网取一次）'}")
    say(f"  运行时状态    : {'就绪' if data['runtime_ready'] else '未就绪'} — {data['runtime_note']}")
    mark = data["portable_marker"]
    if mark:
        say(f"  运行时类型    : 便携 embeddable（Python {mark.get('version')}，无 pip）")
    elif data["is_real_venv"]:
        say("  运行时类型    : 常规 venv（由系统 Python 创建，带 pip）")
    say(f"  Pin 清单      : {data['manifest']}")
    say("  可用下载源    :")
    for m in data["mirrors"]:
        say(f"      · {m}")
    say("")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="获取并装配便携 Python 运行时（离线优先，可用于分发）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="不带动作参数时等同于 --plan",
    )
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--plan", action="store_true", help="只体检，不动手")
    g.add_argument("--auto", action="store_true", help="获取 + 装配（默认动作）")
    g.add_argument("--fetch", action="store_true", help="只把 zip 取到 vendor/python（便于再分发）")
    g.add_argument("--finalize", action="store_true", help="只装配（zip 已在本地）")
    g.add_argument("--pack", action="store_true", help="打成可分发的运行时 zip")
    g.add_argument("--verify", action="store_true", help="校验现有运行时是否真可用")
    ap.add_argument("--offline", action="store_true", help="禁止联网，只用 vendor/python/*.zip")
    ap.add_argument("--version", default=None, help="指定精确版本，如 3.13.9")
    ap.add_argument("--base-url", default=None, help="自定义源（http/https/file/UNC 目录）")
    ap.add_argument("--sha256", default=None, help="强制校验 zip 摘要")
    ap.add_argument("--pin", action="store_true", help="把本次取到的摘要固化进 sources.json")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的运行时")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args(argv)

    if args.verify:
        ok, why = runtime_ready()
        cap = probe_capabilities(runtime_python())
        if args.json:
            print(json.dumps({"ok": ok, "message": why, "runtime": cap}, ensure_ascii=False))
        else:
            say(f"[{'OK' if ok else '失败'}] {why}")
            if cap["ok"]:
                say(f"       解释器 {cap['python']}  Python {cap['version']}")
                say(f"       site-packages {cap['purelib']}")
                for k, v in cap["imports"].items():
                    say(f"       {'✔' if v else '✘'} {k}")
        return 0 if ok else 1

    if args.pack:
        ok, msg, out = pack()
        if args.json:
            print(json.dumps({"ok": ok, "message": msg, "path": str(out) if out else ""},
                             ensure_ascii=False))
        else:
            say(f"[{'OK' if ok else '失败'}] {msg}")
        return 0 if ok else 1

    if args.plan or not any((args.auto, args.fetch, args.finalize)):
        data = plan()
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            _print_plan(data)
        return 0

    if args.fetch:
        zip_path, how = fetch(args.version, offline=args.offline, base_url=args.base_url,
                              want_sha=args.sha256, keep=True)
        if zip_path is None:
            say(f"[失败] {how}", err=True)
            return 1
        if args.pin:
            pin_version(args.version or _validate_zip(zip_path)[2], sha256_of(zip_path), how)
        say(f"[OK] 资产就位：{zip_path}（来源：{how}）")
        return 0

    if args.finalize:
        # 目录里已经有能跑的解释器（引导层铺好的）-> 就地补依赖，别重建
        if runtime_python().exists() and not args.force:
            res = finalize_in_place()
        else:
            zips = _local_zips()
            if not zips:
                say(f"[失败] {PY_DIST_DIR} 里没有 embed zip，先跑 --fetch", err=True)
                return 1
            ver, path = zips[0]
            res = install_portable(path, force=args.force,
                                   version_hint=f"{ver[0]}.{ver[1]}.{ver[2]}")
        if args.json:
            print(json.dumps(res, ensure_ascii=False))
        else:
            say(f"[{'OK' if res['ok'] else '失败'}] {res['message']}")
        return 0 if res["ok"] else 1

    # --auto
    res = ensure_runtime(offline=args.offline, version=args.version, base_url=args.base_url,
                         want_sha=args.sha256, force=args.force)
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
