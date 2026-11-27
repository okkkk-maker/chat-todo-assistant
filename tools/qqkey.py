"""QQ NT 数据库密钥提取：定位安装目录 + 驱动 qq-key 脚本 + 抓候选值。

为什么不能直接用 vendor 里的脚本：
    它的自动检测只认注册表 Uninstall\\QQ / NTQQ 的 UninstallString，
    QQ 装在非标准目录（比如直接装在 D:\\ 根下）时 UninstallString 为空，
    脚本在 Get-InstalledQQInfo 就抛异常退出 —— 表现就是“窗口一闪/不弹 QQ、拿不到密钥”。
    这里我们先把 wrapper.node 与 QQ.exe 找出来，用 -WrapperNodePath / -QQExePath
    显式传给它，跳过那段脆弱的检测。

为什么还要先关 QQ：
    脚本是用 CreateProcess + DEBUG_ONLY_THIS_PROCESS 起一个**新**的 QQ 进程并挂调试器。
    QQ 单实例互斥，旧进程还在时新进程会立刻退出，于是“什么都看不到”。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402

KEY_RE = re.compile(r"(?<![0-9A-Za-z])[!-~]{16}(?![0-9A-Za-z])")
PS1_REL = Path("vendor/qq-key/scripts/windows/ntqq/windows_ntqq_get_key.ps1")

# 注册表里可能写着安装目录的位置
REG_VALUES = [
    (r"HKLM\SOFTWARE\WOW6432Node\Tencent\QQNT", ("Install", "InstallDir", "InstallPath")),
    (r"HKLM\SOFTWARE\Tencent\QQNT", ("Install", "InstallDir", "InstallPath")),
    (r"HKCU\SOFTWARE\Tencent\QQNT", ("Install", "InstallDir", "InstallPath")),
]


def _reg_install_dirs() -> list[Path]:
    out: list[Path] = []
    try:
        import winreg  # noqa: PLC0415
    except ImportError:
        return out
    hive_map = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}
    for full, names in REG_VALUES:
        hive_name, rest = full.split("\\", 1)
        hive = hive_map.get(hive_name)
        if hive is None:
            continue
        try:
            with winreg.OpenKey(hive, rest) as key:
                for n in names:
                    try:
                        val, _ = winreg.QueryValueEx(key, n)
                    except OSError:
                        continue
                    if isinstance(val, str) and val.strip():
                        p = Path(val.strip().strip('"'))
                        if p.exists() and p.is_dir():
                            out.append(p)
        except OSError:
            continue
    return out


def _process_dirs() -> list[Path]:
    """运行中的 QQ 进程所在目录（不依赖 wmic）。"""
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        k32 = ctypes.windll.kernel32
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == -1:
            return []
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        out: list[Path] = []
        ok = k32.Process32First(snap, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile.decode("mbcs", errors="ignore").lower()
            if name in {"qq.exe", "qqex.exe"}:
                h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, entry.th32ProcessID)
                if h:
                    buf = ctypes.create_unicode_buffer(1024)
                    size = wintypes.DWORD(1024)
                    if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                        out.append(Path(buf.value).parent)
                    k32.CloseHandle(h)
            ok = k32.Process32Next(snap, ctypes.byref(entry))
        k32.CloseHandle(snap)
        return out
    except Exception:  # noqa: BLE001
        return []


def _drive_roots() -> list[Path]:
    roots: list[Path] = []
    for letter in "CDEFG":
        p = Path(f"{letter}:/")
        if p.exists():
            roots.append(p)
    return roots


def find_wrapper(install_dir: Path) -> tuple[Path | None, str]:
    """在安装目录里找最新版本下的 wrapper.node。返回 (路径, 版本号)。"""
    versions = install_dir / "versions"
    if versions.is_dir():
        best: tuple[tuple, Path] | None = None
        try:
            for d in versions.iterdir():
                if not d.is_dir():
                    continue
                node = d / "resources" / "app" / "wrapper.node"
                if node.exists():
                    ver = tuple(int(x) for x in re.findall(r"\d+", d.name)[:4])
                    if best is None or ver > best[0]:
                        best = (ver, node)
        except OSError:
            pass
        if best:
            return best[1], best[1].parents[2].name
    node = install_dir / "resources" / "app" / "wrapper.node"
    if node.exists():
        return node, ""
    return None, ""


def find_qq() -> dict | None:
    """返回 {install_dir, qq_exe, wrapper_node, version, sources}；找不到返回 None。"""
    seen: set[str] = set()
    candidates: list[tuple[Path, str]] = []
    for d in _process_dirs():
        candidates.append((d, "运行中的 QQ 进程"))
    for d in _reg_install_dirs():
        candidates.append((d, "注册表 QQNT"))
    # 兜底：各盘根的常见目录
    for root in _drive_roots():
        for sub in ("Tencent/QQNT", "Program Files/Tencent/QQNT", "Program Files (x86)/Tencent/QQNT", "."):
            p = (root / sub).resolve() if sub != "." else root
            if p.exists():
                candidates.append((p, "常见目录扫描"))

    for install_dir, source in candidates:
        key = str(install_dir).lower()
        if key in seen:
            continue
        seen.add(key)
        qq_exe = install_dir / "QQ.exe"
        if not qq_exe.exists():
            continue
        node, version = find_wrapper(install_dir)
        if not node:
            continue
        return {
            "install_dir": install_dir,
            "qq_exe": qq_exe,
            "wrapper_node": node,
            "version": version,
            "source": source,
        }
    return None


def qq_running() -> bool:
    try:
        flags = common.CREATE_NO_WINDOW
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq QQ.exe"],
                           capture_output=True, timeout=20, creationflags=flags)
        out = (r.stdout or b"").decode("gbk", errors="replace")
        return "QQ.exe" in out
    except Exception:  # noqa: BLE001
        return False


def close_qq(timeout: int = 15) -> bool:
    """先礼后兵地关掉 QQ（脚本要冷启动一个新进程才能挂上调试器）。"""
    if not qq_running():
        return True
    try:
        flags = common.CREATE_NO_WINDOW
        subprocess.run(["taskkill", "/IM", "QQ.exe"], capture_output=True, timeout=20, creationflags=flags)
    except Exception:  # noqa: BLE001
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not qq_running():
            return True
        time.sleep(1)
    try:
        flags = common.CREATE_NO_WINDOW
        subprocess.run(["taskkill", "/F", "/IM", "QQ.exe"], capture_output=True, timeout=20, creationflags=flags)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(2)
    return not qq_running()


JUNK_SUBSTR = ("\\", "/", ".exe", ".node", ".dll", ".ps1", "----", "====", "    ", "FunctionRVA", "LeaInstruction")


def _plausible(k: str) -> bool:
    """16 字符只是必要条件；路径片段、表格分隔线都会被正则误抓，这里筛掉。"""
    if len(k) != 16:
        return False
    if not all(32 <= ord(c) <= 126 for c in k):
        return False
    if any(s in k for s in JUNK_SUBSTR):
        return False
    if len(set(k)) < 4:
        return False
    if not any(c.isalpha() for c in k):
        return False
    return True


def parse_candidates(text: str) -> list[str]:
    """从脚本输出里挑出可能的 16 字符密钥。

    优先“加密密钥: xxx”这种明确标注；没有的话再全量扫，
    但会过滤掉路径片段/表格线这类必然误报的字符串。
    真伪最终靠 SQLCipher 解密验证，这里只负责别把垃圾喂进去。
    """
    preferred = [m.group(1).strip() for m in re.finditer(r"加密密钥[:：]\s*(\S{16})", text)]
    preferred = [k for k in preferred if _plausible(k)]
    if preferred:
        return list(dict.fromkeys(preferred))

    out: list[str] = []
    for m in KEY_RE.finditer(text):
        k = m.group(0)
        if _plausible(k) and k not in out:
            out.append(k)
    return out[:12]


def powershell_exe() -> str:
    """定位 PowerShell。有些环境 PATH 里没有它（但文件一定在 System32 下）。"""
    import shutil  # noqa: PLC0415

    for name in ("powershell", "pwsh"):
        p = shutil.which(name)
        if p:
            return p
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    for sub in (
        r"System32\WindowsPowerShell\v1.0\powershell.exe",
        r"SysWOW64\WindowsPowerShell\v1.0\powershell.exe",
        r"System32\WindowsPowerShell\v1.0\pwsh.exe",
    ):
        cand = Path(sysroot) / sub
        if cand.exists():
            return str(cand)
    return "powershell"


EXTRACT_TIMEOUT = 180   # 取密钥总时长硬上限（秒）：静态分析约 1 分钟 + 等人点登录


def run_extract(qq: dict, log_path: Path, on_line=None, no_debug: bool = False,
                timeout: int = EXTRACT_TIMEOUT) -> tuple[int, str]:
    """跑取密钥脚本，边跑边打印，返回 (退出码, 全部输出)。

    必须带硬超时：脚本会等 QQ 弹窗、挂调试器，一旦用户没看到弹窗、杀软拦了
    进程、或 PowerShell 自身卡死，`proc.wait()` 会无限等下去，实施员向导就
    永远停在 [A2]，既不能中断也报不出原因。超时后强杀子进程并写诊断建议。
    """
    ps1 = common.ROOT / PS1_REL
    if not ps1.exists():
        return -1, f"找不到取密钥脚本: {ps1}"

    cmd = [
        powershell_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1),
        "-WrapperNodePath", str(qq["wrapper_node"]),
        "-QQExePath", str(qq["qq_exe"]),
    ]
    if no_debug:
        cmd.append("-NoDebugForKey")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(ps1.parent), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except OSError as exc:
        return -1, f"启动 PowerShell 失败: {exc}"

    timed_out = False

    def _kill_on_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass

    guard = threading.Timer(max(1, int(timeout)), _kill_on_timeout)
    guard.daemon = True
    guard.start()

    chunks: list[str] = []
    rc = -1
    try:
        with open(log_path, "w", encoding="utf-8", errors="replace") as log:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                chunks.append(line)
                log.write(line + "\n")
                if on_line:
                    on_line(line)
                else:
                    print("    | " + line, flush=True)
            try:
                rc = proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                _kill_on_timeout()
                rc = proc.wait(timeout=10)
    finally:
        guard.cancel()

    if timed_out:
        hint = (
            f"[超时] 取密钥脚本运行超过 {timeout} 秒，已强制结束。\n"
            "  可能原因：QQ 登录框没弹出/没点登录、杀软拦截了调试器、或 PowerShell 卡住。\n"
            "  建议：关闭杀毒软件实时防护后重跑；或确认 wrapper.node 版本是否被 QQ 升级替换。"
        )
        chunks.append(hint)
        try:
            with open(log_path, "a", encoding="utf-8", errors="replace") as log:
                log.write(hint + "\n")
        except OSError:
            pass
    return rc, "\n".join(chunks)


def tail(path: Path, n: int = 12) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])



def verify_key(db: Path, key: str, preferred: str | None = None) -> tuple[bool, str]:
    """真刀真枪解一次。返回 (是否可用, profile 或失败原因)。

    和 admin_init.verify_key 同一语义——QQ 密钥只有"能解开这个库"才叫抓到，
    候选串、内存里扫到的 16 字符、别人贴的值，一律以这一次解密为准。
    """
    import qqdb  # noqa: PLC0415

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


def extract_verified_key(
    db_path: Path | None = None,
    log_hint: list[str] | None = None,
    timeout: int = EXTRACT_TIMEOUT,
) -> tuple[str, str, str]:
    """定位 QQ -> 冷启动挂调试器 -> 抓候选 -> 逐个真解密校验。

    返回 (key, profile, 说明)；失败时 key 为空，说明里写清楚卡在哪一步。

    这是**唯一**的 QQ 取密钥实现：Admin_Init 向导、控制台按钮、vendor 的 bat
    全部调到这来。旧版这里有两套（admin_init 会校验、qqkey 会兜底），
    结果就是"桌面 App 入口抓到的密钥看着成功、实际解不开"。
    """
    notes = log_hint if log_hint is not None else []

    env = common.load_env()
    preferred = env.get("QQ_CIPHER_PROFILE") or None
    if preferred and preferred not in ("ntqq_official", "ntqq_official_sa1", "ntqq_v4",
                                       "compat3", "default", "raw_hex"):
        preferred = None          # 旧版写进去的伪 profile（profile_1）不再采信

    # 1) 目标数据库
    if not db_path:
        db_str = (env.get("QQ_DB_PATH") or "").strip()
        if db_str and Path(db_str).exists():
            db_path = Path(db_str)
        else:
            try:
                import admin_init  # noqa: PLC0415

                accs = admin_init.detect_qq_accounts()
                if accs:
                    db_path = Path(accs[0]["db"])
                    common.save_env({"QQ_TARGET_UIN": accs[0]["uin"], "QQ_DB_PATH": str(db_path)})
                    common.log(f"  [OK] 自动绑定主 QQ 号: {accs[0]['uin']}")
                    common.log(f"       数据库路径: {db_path}")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"扫描 QQ 账号失败: {exc}")

    if not db_path or not db_path.exists():
        return "", "", "没有定位到 nt_msg.db（请确认 QQ 已登录过且聊天记录在本机）"

    # 2) 先看 .env 里已有的密钥还能不能用：能用就不折腾 QQ
    old = (env.get("QQ_DBKEY") or "").strip()
    if old:
        ok, detail = verify_key(db_path, old, preferred)
        if ok:
            return old, detail, f"已有密钥仍可用（参数 {detail}），无需重抓"

    # 3) 定位 QQ 安装目录
    info = find_qq()
    if not info:
        return "", "", "没找到 QQ 安装目录（需要 QQ.exe 与 wrapper.node 同时存在）"
    common.log(f"  [OK] QQ 安装目录 : {info['install_dir']}")
    common.log(f"  [OK] 核心组件    : {info['wrapper_node']}")

    # 4) 冷启动
    if qq_running():
        common.log("[*] 正在关闭当前运行中的 QQ（脚本需冷启动新进程才能附加调试器）...")
        if not close_qq():
            return "", "", "QQ 进程无法自动退出，请手动在托盘退出 QQ 后重试"
        common.log("  [OK] QQ 进程已安全退出")

    log_path = common.WORK / f"取密钥日志-{datetime.now():%Y%m%d-%H%M}.txt"
    common.log("  [*] 正在静态分析 wrapper.node 并等待登录...")
    rc, output_text = run_extract(info, log_path, timeout=timeout)

    # 5) 候选值逐个真解密验证（绝不把未验证的值写进 .env）
    cands = parse_candidates(output_text)
    if not cands:
        return "", "", f"未能从内存捕获到 16 位候选密钥（脚本退出码 {rc}，日志 {log_path.name}）"

    common.log(f"  [*] 捕获到 {len(cands)} 个候选口令，逐个验证解密有效性...")
    for cand in cands:
        ok, detail = verify_key(db_path, cand, preferred)
        if ok:
            return cand, detail, f"密钥校验通过（参数 {detail}）"
        common.log(f"      候选 {common.mask(cand)} 解不开，继续下一个")

    tail_txt = tail(log_path, 8)
    if tail_txt:
        common.log("  ---- 脚本最后几行 ----")
        for line in tail_txt.splitlines():
            common.log("  |  " + line)
    return "", "", f"{len(cands)} 个候选都解不开（脚本退出码 {rc}，日志 {log_path.name}）"


def extract_and_save(db_path: Path | None = None) -> tuple[bool, str]:
    """控制台/桌面 App 入口的 QQ 一键抓取：抓 -> 验 -> 写 .env -> 固化 schema。

    与 Admin_Init 向导（admin_init.step_a2 + step_a3）现在是同一条链路：
      · 写 .env 前必须解密成功，不再"先写候选再祈祷"；
      · schema.json 用 probe.verify_decrypt_and_dump_schema 固化。
        旧版这里调用的是 probe.probe_and_freeze（该函数从未存在，纯属笔误），异常被 except 吞掉，
        于是密钥有了、schema 永远没有 —— extract_impl 见到缺 schema 直接拒绝采集，
        表现就是"密钥抓取成功，但一条 QQ 消息都读不出来"。
    """
    common.head("待办自动汇总助手 · QQ 数据库密钥一键自动抓取向导")
    common.log("  启动时间: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    common.log("  运行环境: " + _runtime_line())

    key, profile, detail = extract_verified_key(db_path)
    if not key:
        common.log(f"  [X] {detail}")
        common.log("      排查建议：登录框出现后 15 秒内点登录；确认杀软未拦截调试器；")
        common.log("      日志见 work/取密钥日志-*.txt，把它发给维护人员即可。")
        return False, detail

    common.log(f"  [OK] {detail}")

    env = common.load_env()
    target_db = db_path or Path((env.get("QQ_DB_PATH") or "").strip())
    common.save_env({"QQ_DBKEY": key, "QQ_CIPHER_PROFILE": profile})
    os.environ["QQ_DBKEY"] = key
    common.log(f"  [OK] 已写入 .env: QQ_DBKEY={common.mask(key)} / QQ_CIPHER_PROFILE={profile}")

    # 固化表结构：缺这一步，extract_impl 会以"没有 schema"为由拒绝采集
    schema_ok = False
    if target_db and target_db.exists():
        try:
            import probe  # noqa: PLC0415

            schema = probe.verify_decrypt_and_dump_schema(target_db, key, profile)
            schema_ok = bool(schema.get("tables"))
            if schema_ok:
                common.log(f"  [OK] 表结构已固化至 {common.SCHEMA_PATH.name}"
                           f"（{len(schema['tables'])} 张消息表）")
            else:
                common.log("  [!] 解密成功但没识别出可用消息表，采集会拒绝运行（宁可不产出也不编造）")
        except Exception as exc:  # noqa: BLE001
            common.log(f"  [!] 表结构固化失败: {exc}")
    else:
        common.log("  [!] 数据库路径不存在，跳过表结构固化")

    print()
    print("=" * 68)
    if schema_ok:
        print("  🎉 全部就绪！QQ 密钥 + 表结构均已装配，可直接开始采集。")
    else:
        print("  ⚠ 密钥已就绪，但表结构未固化：请在控制台点「立即采集」重试，或重跑本向导。")
    print("=" * 68)
    return schema_ok, ("ok" if schema_ok else "schema_missing")


def _runtime_line() -> str:
    try:
        import runtime_env  # noqa: PLC0415

        return runtime_env.describe()
    except Exception:  # noqa: BLE001
        return f"{sys.executable}（{sys.version.split()[0]}）"


if __name__ == "__main__":
    # 任何入口（bat / 双击 / 控制台 / 提权窗口）都先落到同一个能解密的解释器上，
    # 避免"这个入口的 python 没装 sqlcipher3"再次变成抓不到密钥的理由。
    try:
        import runtime_env  # noqa: PLC0415

        runtime_env.ensure_capable(verbose=True)
    except Exception:  # noqa: BLE001
        pass

    if "--extract" in sys.argv:
        ok, res = extract_and_save()
        # 提权窗口里 input() 会卡住进程，只在真正的交互终端才等回车
        if sys.stdin.isatty():
            print("\n请按回车键返回控制台...")
            try:
                input()
            except Exception:  # noqa: BLE001
                pass
        sys.exit(0 if ok else 1)

    common.log("=== QQ 安装信息 ===")
    info = find_qq()
    if not info:
        common.log("没找到 QQ 安装目录（需要 QQ.exe 与 wrapper.node 同时存在）")
        raise SystemExit(1)
    for k, v in info.items():
        common.log(f"  {k:14}: {v}")
    common.log(f"  QQ 正在运行  : {qq_running()}")
