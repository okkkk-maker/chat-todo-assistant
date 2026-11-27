"""锁死 NTQQ 自动更新（保护已取到的数据库密钥与表结构）。

为什么要做：QQ NT 一旦后台升级，SQLCipher 参数或 nt_msg.db 表结构可能变化，
已固化的 config/schema.json 与 .env 里的密钥就会同时失效。锁住升级组件后，
密钥与表结构在相当长时间内保持有效。

免疫三步法（对每个更新可执行文件）：
    1. 正在运行 -> taskkill /F
    2. 原文件改名 <name>.disabled_bak
    3. 原路径建一个同名空目录（attrib +r +s），QQ 想下载覆盖时写不进去

用法:
    python tools/lock_qq_update.py --check      # 只体检，不改任何东西（默认）
    python tools/lock_qq_update.py --apply      # 执行锁死（由 Admin_Init 自动调用）
    python tools/lock_qq_update.py --restore    # 还原（恢复自动更新）

注意: apply / restore 需要管理员权限；不成功会明确报出来，不静默。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402

# 已知的升级组件名（NTQQ 不同版本用过不同名字）
UPDATE_EXES = ["QQUpdate.exe", "txupd.exe", "auclt.exe", "QQUpdateHelper.exe"]
BACKUP_SUFFIX = ".disabled_bak"

# 可能的注册表位置：值里含安装路径
REG_KEYS = [
    (r"HKLM\SOFTWARE\WOW6432Node\Tencent\QQNT", ("InstallDir", "Install", "InstallPath", "Install Path", "Path")),
    (r"HKLM\SOFTWARE\Tencent\QQNT", ("InstallDir", "Install", "InstallPath", "Install Path", "Path")),
    (r"HKCU\SOFTWARE\Tencent\QQNT", ("InstallDir", "Install", "InstallPath", "Install Path", "Path")),
    (
        r"HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
        ("InstallLocation", "DisplayIcon", "UninstallString"),
    ),
    (
        r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
        ("InstallLocation", "DisplayIcon", "UninstallString"),
    ),
    (
        r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\QQ.exe",
        ("", "Path"),
    ),
]


def _process_dirs() -> list[Path]:
    """从正在运行的 QQ 进程取安装目录（不需要 wmic，纯 Win32 API）。"""
    out: list[Path] = []
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
            return out
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        targets = {"qq.exe", "qqex.exe"}
        ok = k32.Process32First(snap, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile.decode("mbcs", errors="ignore").lower()
            if name in targets:
                h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, entry.th32ProcessID)
                if h:
                    buf = ctypes.create_unicode_buffer(1024)
                    size = wintypes.DWORD(1024)
                    if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                        out.append(Path(buf.value).parent)
                    k32.CloseHandle(h)
            ok = k32.Process32Next(snap, ctypes.byref(entry))
        k32.CloseHandle(snap)
    except Exception:  # noqa: BLE001
        pass
    return out

# 常见安装位置兜底（注册表读不到时扫这些）
def _fallback_dirs() -> list[Path]:
    out: list[Path] = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if not base:
            continue
        for sub in ("Tencent/QQNT", "Tencent/QQ", "Programs/Tencent/QQNT", "Programs/Tencent/QQ"):
            out.append(Path(base) / sub)
    for drive in ("C:", "D:", "E:", "F:"):
        out.append(Path(drive + "/Program Files/Tencent/QQNT"))
        out.append(Path(drive + "/Tencent/QQNT"))
        out.append(Path(drive + "/Program Files (x86)/Tencent/QQNT"))
    return out


def _reg_paths() -> list[Path]:
    """从注册表收集安装/更新目录。读不到就跳过，不报错。"""
    out: list[Path] = []
    try:
        import winreg  # noqa: PLC0415
    except ImportError:
        return out

    hive_map = {
        "HKLM": winreg.HKEY_LOCAL_MACHINE,
        "HKCU": winreg.HKEY_CURRENT_USER,
    }
    for full, value_names in REG_KEYS:
        hive_name, rest = full.split("\\", 1)
        hive = hive_map.get(hive_name)
        if hive is None:
            continue
        try:
            with winreg.OpenKey(hive, rest) as key:
                for vn in value_names:
                    try:
                        val, _ = winreg.QueryValueEx(key, vn)
                    except OSError:
                        continue
                    if not isinstance(val, str) or not val.strip():
                        continue
                    text = val.strip().strip('"')
                    # DisplayIcon / UninstallString 可能带参数，取路径部分
                    for token in (text, text.split(",")[0], text.split('"')[1] if '"' in text else text):
                        p = Path(token.strip().strip('"'))
                        if p.exists():
                            out.append(p if p.is_dir() else p.parent)
                            break
        except OSError:
            continue
    return out


def candidate_roots() -> list[Path]:
    """所有可能藏升级组件的目录（去重、存在的排前面）。"""
    roots: list[Path] = []
    roots.extend(_process_dirs())
    for p in _reg_paths():
        roots.append(p)
    for env in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            roots.append(Path(base) / "Tencent/QQ")
            roots.append(Path(base) / "Tencent/QQNT")
    roots.extend(_fallback_dirs())

    seen: set[str] = set()
    uniq: list[Path] = []
    for p in roots:
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    uniq.sort(key=lambda p: not p.exists())
    return uniq


def find_targets() -> tuple[list[Path], list[Path]]:
    """返回 (更新可执行文件, versions/update 目录)。"""
    exes: list[Path] = []
    ver_dirs: list[Path] = []
    for root in candidate_roots():
        if not root.exists():
            continue
        for name in UPDATE_EXES:
            p = root / name
            if p.is_file():
                exes.append(p)
        # NTQQ 的更新缓存可能在子目录
        for depth in (root, root / "resources", root / "bin"):
            if depth.is_dir():
                for name in UPDATE_EXES:
                    p = depth / name
                    if p.is_file() and p not in exes:
                        exes.append(p)
        vdir = root / "versions"
        if vdir.is_dir():
            upd = vdir / "update"
            if upd.exists():
                ver_dirs.append(upd)
            # QQNT 把程序体放在 versions\<版本号>\，升级器也可能在里面
            try:
                for ver in vdir.iterdir():
                    if ver.is_dir():
                        for name in UPDATE_EXES:
                            p = ver / name
                            if p.is_file() and p not in exes:
                                exes.append(p)
            except OSError:
                pass
    return exes, ver_dirs


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        flags = common.CREATE_NO_WINDOW
        r = subprocess.run(cmd, capture_output=True, timeout=30, creationflags=flags)
        out = (r.stdout or b"").decode("gbk", errors="replace") + (r.stderr or b"").decode("gbk", errors="replace")
        return r.returncode, out.strip()
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


def _is_running(image: str) -> bool:
    code, out = _run(["tasklist", "/FI", f"IMAGENAME eq {image}"])
    return image.lower() in out.lower()


def lock_one(exe: Path, hard: bool = False) -> tuple[bool, str]:
    """对单个更新程序执行三步免疫。返回 (成功, 说明)。"""
    bak = exe.with_name(exe.name + BACKUP_SUFFIX)

    if exe.is_dir():
        return True, f"已是占位目录，跳过: {exe}"

    if bak.exists():
        # 已经备份过，只需确保占位目录在
        if not exe.exists():
            exe.mkdir(parents=True, exist_ok=True)
            _run(["attrib", "+r", "+s", str(exe)])
            return True, f"备份已在，补建占位目录: {exe}"
        return True, f"已锁死过，跳过: {exe}"

    if _is_running(exe.name):
        code, out = _run(["taskkill", "/F", "/IM", exe.name])
        if code != 0:
            return False, f"结束进程失败: {out}"

    try:
        exe.rename(bak)
    except OSError as exc:
        return False, f"改名失败（大概率需要管理员权限）: {exc}"

    try:
        exe.mkdir(parents=True, exist_ok=True)
        code, out = _run(["attrib", "+r", "+s", str(exe)])
        if code != 0:
            common.log(f"      [注意] attrib 未生效: {out}")
        if hard:
            # 额外拒绝写入（更彻底，但需要管理员，失败不影响主流程）
            _run(["icacls", str(exe), "/deny", "*S-1-1-0:(W,D)"])
        return True, f"已锁死: {exe} -> {bak.name} + 只读目录占位"
    except OSError as exc:
        # 关键：这里必须回滚。否则留下「更新器已被改名、却没有占位目录」的
        # 半锁死状态 —— QQ 的更新链被破坏了一半，用户完全不知情。
        rolled_back = False
        try:
            if exe.is_dir() and not any(exe.iterdir()):
                exe.rmdir()
            bak.rename(exe)
            rolled_back = True
        except OSError:
            pass
        hint = "已回滚到原始状态" if rolled_back else f"回滚也失败，请手工把 {bak.name} 改回 {exe.name}"
        return False, f"建占位目录失败（{exc}）；{hint}"


def restore_one(exe: Path) -> tuple[bool, str]:
    bak = exe.with_name(exe.name + BACKUP_SUFFIX)
    if not bak.exists():
        return True, f"无备份，跳过: {exe}"
    try:
        if exe.exists() and exe.is_dir():
            # 先把 icacls 的拒绝项撤掉，否则普通权限下删不掉占位目录
            _run(["icacls", str(exe), "/remove:d", "*S-1-1-0"])
            _run(["attrib", "-r", "-s", "-h", str(exe)])
            try:
                exe.rmdir()
            except OSError:
                # 目录非空 = 有别的东西写进去了，别硬删，如实报出来
                inner = [p.name for p in exe.iterdir()][:5]
                return False, (
                    f"占位目录非空，未强行删除：{exe}（内有 {', '.join(inner)}）。"
                    f"请先清空该目录，再把 {bak.name} 改回 {exe.name}"
                )
        bak.rename(exe)
        return True, f"已还原: {exe}"
    except OSError as exc:
        return False, (
            f"还原失败（{exc}）。可手工执行：\n"
            f"      attrib -r -s -h \"{exe}\" && rmdir \"{exe}\" && ren \"{bak}\" \"{exe.name}\""
        )


def lock(hard: bool = False) -> tuple[bool, str]:
    """执行锁死，返回 (是否有实际动作成功, 汇总说明)。供 admin_init 调用。"""
    common.log("\n【QQ 防升级保护】")
    exes, ver_dirs = find_targets()
    if not exes and not ver_dirs:
        common.log("  [注意] 没找到 NTQQ 升级组件（可能装在非标准目录，或已是精简版）")
        common.log("         这不影响数据抽取，但升级风险需人工留意。")
        return False, "未找到升级组件"

    ok_all = True
    for exe in exes:
        ok, msg = lock_one(exe, hard=hard)
        common.log(f"  [{'OK' if ok else '失败'}] {msg}")
        ok_all = ok_all and ok

    for vdir in ver_dirs:
        try:
            marker = vdir / "README_已禁用.txt"
            if not marker.exists():
                marker.write_text(
                    "该目录已被 qq-todo 工具锁死，用于保护聊天数据库的密钥与表结构。\n"
                    "如需恢复 QQ 自动更新，运行: python tools\\lock_qq_update.py --restore\n",
                    encoding="utf-8",
                )
            common.log(f"  [OK] 更新缓存目录已标记: {vdir}")
        except OSError as exc:
            common.log(f"  [注意] 更新缓存目录标记失败: {exc}")

    if ok_all:
        common.log("  [OK] 已锁死 NTQQ 自动更新，当前数据库密钥与表结构已固化保护。")
    else:
        common.log("  [注意] 部分组件未锁死：请用管理员身份重新运行一次。")
    return ok_all, "已处理 " + str(len(exes)) + " 个组件"


def report() -> None:
    common.head("NTQQ 自动更新 · 体检")
    exes, ver_dirs = find_targets()
    common.log(f"  扫描目录: {len(candidate_roots())} 个")
    if not exes and not ver_dirs:
        common.log("  [注意] 未发现升级组件（无法锁死，但也不影响采集）")
        return
    for exe in exes:
        state = "已锁死" if exe.is_dir() else ("有备份待补占位" if exe.with_name(exe.name + BACKUP_SUFFIX).exists() else "未锁死")
        common.log(f"  {exe}  -> {state}")
    for vdir in ver_dirs:
        common.log(f"  更新缓存: {vdir}")
    common.log("\n  提示: 加 --apply 执行锁死，加 --restore 还原。")


def main() -> None:
    args = set(sys.argv[1:])
    if "--restore" in args:
        exes, _ = find_targets()
        if not exes:
            common.log("没有需要还原的组件。")
            return
        for exe in exes:
            ok, msg = restore_one(exe)
            common.log(f"[{'OK' if ok else '失败'}] {msg}")
        return
    if "--apply" in args:
        lock(hard="--hard" in args)
        return
    report()


if __name__ == "__main__":
    main()
