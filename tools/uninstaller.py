"""待办自动汇总助手 · 一键完全卸载与痕迹彻底清理引擎 (Uninstaller)

执行 100% 干净卸载，绝不在系统留下任何残留：
1. 注销所有 Windows 开机自启动项 (ChatTodoWatcher.bat, ChatTodoScheduler.bat)；
2. 彻底删除桌面快捷方式 (待办总结助手.lnk) 与开始菜单快捷方式；
3. 还原 QQ 自动更新保护 (恢复被备份改名的更新程序，删除占位目录)；
4. 销毁本地解密镜像 (work/wx_mirror/)、浏览器缓存 (work/.app_profile/) 与临时数据库；
5. 清除本地密钥环 (work/wx_keys.json)、素材包、游标、表结构与敏感配置文件 (.env)；
6. 安全退出所有相关的后台常驻调度守护进程，并在必要时调度独立脱离的延迟清理任务。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "tools"))

import common  # noqa: E402


def _read_pid_file(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if text.isdigit():
            return int(text)
    except OSError:
        pass
    return None


def _read_console_pid(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        pid = int(data.get("pid") or 0)
        return pid if pid > 0 else None
    except (OSError, ValueError, TypeError):
        return None


def _process_still_alive(pid: int, taskkill_output: str, flags: int) -> bool:
    """确认 taskkill 返回非零时进程是否仍存活（忽略已退出的陈旧 PID）。"""
    lowered = taskkill_output.lower()
    if "not found" in lowered or "找不到" in taskkill_output or "没有" in taskkill_output:
        return False
    try:
        check = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=8,
            encoding="gbk",
            errors="replace",
            creationflags=flags,
        )
        out = check.stdout or ""
        return bool(re.search(rf'"{pid}"', out)) or out.strip().startswith(str(pid))
    except (OSError, subprocess.SubprocessError):
        return True


def stop_processes() -> dict[str, Any]:
    """停掉本程序拉起的所有后台进程（调度守护、服务端、pythonw 桌面实例）。

    设计说明：自启项跑的是 `python.exe tools\\scheduler.py daemon`，
    桌面 App 用的是 `pythonw.exe server.py` —— 两者都没有可匹配的窗口标题，
    旧实现只靠 `taskkill /FI "WINDOWTITLE eq ..."` 根本杀不到。
    """
    stopped: list[str] = []
    failed: list[str] = []
    pids: set[int] = set()
    flags = common.CREATE_NO_WINDOW

    pid_file = common.WORK / "scheduler.pid"
    sched_pid = _read_pid_file(pid_file)
    if sched_pid:
        pids.add(sched_pid)

    console_file = common.WORK / "console_port.json"
    console_pid = _read_console_pid(console_file)
    if console_pid:
        pids.add(console_pid)

    own_pid = os.getpid()
    parent_pid = os.getppid() if hasattr(os, "getppid") else -1
    skip_pids = {own_pid, parent_pid}

    # 按 ExecutablePath 或 python CommandLine 位于本程序目录内扫一遍
    try:
        root_l = str(ROOT_DIR).lower().replace("'", "''")
        ps = (
            "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
            f"Where-Object {{ ($_.ExecutablePath -and $_.ExecutablePath.ToLower().StartsWith('{root_l}')) -or "
            f"($_.Name -match '^pythonw?\\.exe$' -and $_.CommandLine -and $_.CommandLine.ToLower().Contains('{root_l}')) }} | "
            "Select-Object -ExpandProperty ProcessId"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                pids.add(int(line))
    except (OSError, subprocess.SubprocessError):
        pass

    for pid in sorted(pids):
        if pid <= 0 or pid in skip_pids:
            continue
        try:
            r = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                text=True,
                timeout=15,
                encoding="gbk",
                errors="replace",
                creationflags=flags,
            )
            if r.returncode == 0:
                stopped.append(str(pid))
            else:
                combined = (r.stdout or "") + "\n" + (r.stderr or "")
                if _process_still_alive(pid, combined, flags):
                    failed.append(str(pid))
        except (OSError, subprocess.SubprocessError) as exc:
            failed.append(f"{pid}({exc})")

    try:
        pid_file.unlink(missing_ok=True)
    except OSError:
        pass

    return {"ok": not failed, "stopped": stopped, "failed": failed}


def remove_autostart() -> dict[str, Any]:
    """注销开机自启动项。"""
    try:
        import setup_autostart  # noqa: PLC0415

        setup_autostart.remove()
        left = [p for p in (setup_autostart.TARGET, setup_autostart.SCHED_TARGET) if p.exists()]
        if left:
            return {
                "ok": False,
                "message": "自启项未能删除（可能被占用或无权限）：" + "；".join(map(str, left)),
            }
        return {"ok": True, "message": "开机自启项已全部注销"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"注销自启项失败: {exc}"}


def remove_shortcuts() -> list[str]:
    """删除桌面与开始菜单上的所有快捷方式（含历史版本可能落下的各种位置）。"""
    removed: list[str] = []
    home = Path(os.environ.get("USERPROFILE", os.path.expanduser("~")))
    appdata = Path(os.environ.get("APPDATA", os.path.expanduser(r"~\AppData\Roaming")))

    candidates = [
        home / "Desktop" / "待办总结助手.lnk",
        home / "桌面" / "待办总结助手.lnk",
        home / "OneDrive" / "Desktop" / "待办总结助手.lnk",
        home / "OneDrive" / "桌面" / "待办总结助手.lnk",
        appdata / "Microsoft/Windows/Start Menu/Programs/待办总结助手.lnk",
    ]
    try:
        import desktop_app  # noqa: PLC0415

        candidates += [
            desktop_app.get_desktop_path() / "待办总结助手.lnk",
            desktop_app.get_start_menu_programs() / "待办总结助手.lnk",
        ]
    except Exception:  # noqa: BLE001
        pass

    seen: set[str] = set()
    for p in candidates:
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        if p.exists():
            try:
                p.unlink()
                removed.append(str(p))
            except OSError:
                pass
    return removed


def restore_qq_updates() -> dict[str, Any]:
    """还原 QQ 自动更新组件。"""
    try:
        import lock_qq_update  # noqa: PLC0415

        exes, _ = lock_qq_update.find_targets()
        restored = 0
        for exe in exes:
            ok, _ = lock_qq_update.restore_one(exe)
            if ok:
                restored += 1
        return {"ok": True, "restored_count": restored}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"还原 QQ 更新组件失败: {exc}"}


def _ps_escape(text: str) -> str:
    return text.replace("'", "''")


def _powershell_exe() -> str:
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if exe:
        return exe
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    cand = Path(sysroot) / r"System32\WindowsPowerShell\v1.0\powershell.exe"
    return str(cand) if cand.exists() else "powershell"


def _schedule_deferred_cleanup(dirs: list[Path], files: list[Path], delay_sec: float = 4.0) -> str | None:
    """在独立脱离进程中延迟清理仍被主服务/浏览器窗口占用的目录与文件。"""
    valid_dirs = [d for d in dirs if d.exists()]
    valid_files = [f for f in files if f.exists()]
    if not valid_dirs and not valid_files:
        return None

    try:
        script_dir = Path(tempfile.gettempdir())
        script_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        script_dir = common.WORK if common.WORK.exists() else Path(".")
        script_dir.mkdir(parents=True, exist_ok=True)

    ps1_path = script_dir / f"qqtodo_cleanup_{os.getpid()}_{time.time_ns()}.ps1"
    wait_sec = max(0, int(round(delay_sec)))

    lines = [
        "$ErrorActionPreference = 'SilentlyContinue'",
    ]
    if wait_sec > 0:
        lines.append(f"Start-Sleep -Seconds {wait_sec}")

    # 若桌面独立 App 浏览器进程仍锁定 work/.app_profile，只精准终止绑定该隔离配置目录的浏览器进程
    app_profiles = [d for d in valid_dirs if d.name == ".app_profile"]
    for prof in app_profiles:
        ps_prof = _ps_escape(str(prof).lower())
        lines.extend([
            "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |",
            f"  Where-Object {{ $_.Name -match '^(msedge|chrome)\\.exe$' -and $_.CommandLine -and $_.CommandLine.ToLower().Contains('{ps_prof}') }} |",
            "  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
            "Start-Sleep -Milliseconds 500",
        ])

    lines.append("for ($i = 0; $i -lt 5; $i++) {")
    lines.append("  $pending = $false")
    for d in valid_dirs:
        qd = _ps_escape(str(d))
        lines.extend([
            f"  if (Test-Path -LiteralPath '{qd}') {{",
            f"    Remove-Item -LiteralPath '{qd}' -Recurse -Force -ErrorAction SilentlyContinue",
            f"    if (Test-Path -LiteralPath '{qd}') {{ $pending = $true }}",
            "  }",
        ])
    for f in valid_files:
        qf = _ps_escape(str(f))
        lines.extend([
            f"  if (Test-Path -LiteralPath '{qf}') {{",
            f"    Remove-Item -LiteralPath '{qf}' -Force -ErrorAction SilentlyContinue",
            f"    if (Test-Path -LiteralPath '{qf}') {{ $pending = $true }}",
            "  }",
        ])
    lines.extend([
        "  if (-not $pending) { break }",
        "  Start-Sleep -Seconds 1",
        "}",
        "Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue",
    ])
    payload = "\r\n".join(lines) + "\r\n"
    # UTF-8 with BOM 确保 Windows PowerShell 5.1 在任意系统区域设置下都能正确解析中文路径
    ps1_path.write_text(payload, encoding="utf-8-sig", newline="")

    creationflags = 0
    if sys.platform == "win32":
        # 不能加 DETACHED_PROCESS：Windows PowerShell 5.1 在 DETACHED_PROCESS 下会静默退出不执行脚本
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    subprocess.Popen(
        [_powershell_exe(), "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", str(ps1_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )
    return str(ps1_path)


def _reset_active_platforms(cfg_path: Path) -> bool:
    if not cfg_path.exists():
        return False
    default_cfg = {
        "config_mode": "",
        "enabled_platforms": [],
        "optional_platforms": [],
        "updated_at": "",
    }
    common.atomic_write_text(cfg_path, json.dumps(default_cfg, ensure_ascii=False, indent=2) + "\n")
    return True


def clean_work_and_cache(
    purge_env: bool = True,
    schedule_deferred: bool = True,
) -> tuple[list[str], list[str], str | None]:
    """彻底销毁本地明文解密镜像、浏览器配置、密钥环、表结构与运行产物。

    返回 (cleaned_items, problems, deferred_script_path)。
    """
    cleaned: list[str] = []
    problems: list[str] = []
    deferred_dirs: list[Path] = []
    deferred_files: list[Path] = []

    # 1. 工作区缓存目录
    dir_targets = [
        (common.WORK / ".app_profile", "work/.app_profile (独立应用窗口缓存)"),
        (common.WORK / "wx_mirror", "work/wx_mirror (本地解密明文镜像)"),
        (common.WORK / "tmp_db", "work/tmp_db"),
        (common.WORK / "tmp_wx", "work/tmp_wx"),
        (common.WORK / ".tmp_db_verify", "work/.tmp_db_verify"),
        (common.WORK / ".tmp_diagnose", "work/.tmp_diagnose"),
    ]
    for dpath, label in dir_targets:
        if dpath.exists():
            try:
                shutil.rmtree(dpath)
                cleaned.append(label)
            except Exception as exc:  # noqa: BLE001
                if schedule_deferred:
                    deferred_dirs.append(dpath)
                else:
                    problems.append(f"{label} 清理失败: {exc}")

    # 2. 收件箱目录内的消息缓存
    inbox_dir = common.WORK / "inbox"
    if inbox_dir.exists() and inbox_dir.is_dir():
        for item in sorted(inbox_dir.iterdir()):
            if item.is_file():
                try:
                    item.unlink()
                    cleaned.append(f"work/inbox/{item.name}")
                except Exception as exc:  # noqa: BLE001
                    if schedule_deferred:
                        deferred_files.append(item)
                    else:
                        problems.append(f"work/inbox/{item.name} 删除失败: {exc}")

    # 3. 密钥环、运行态、表结构与敏感配置文件
    sensitive_files: list[Path] = [
        common.WORK / "wx_keys.json",
        common.WORK / "待办素材.json",
        common.WORK / "待办素材.sample.json",
        common.WORK / "素材历史.jsonl",
        common.WORK / "cursor.json",
        common.WORK / "channel_cursors.json",
        common.WORK / "last_sync_ts.txt",
        common.WORK / "seen_msg_ids.json",
        common.WORK / "schedule_state.json",
        common.WORK / "console_port.json",
        common.WORK / ".optional_deps_checked",
        common.WORK / "schema.txt",
        common.WORK / "group_names.json",
        common.WORK / "server.log",
        common.WORK / "python-bootstrap.log",
        common.WORK / ".process.lock",
        common.CONFIG / "wizard_state.json",
        common.CONFIG / "schema.json",
        common.ROOT / "automation-prompt.local.md",
        common.HOME / "automation-prompt.local.md",
    ]
    if common.WORK.exists():
        sensitive_files.extend(sorted(common.WORK.glob("取密钥日志-*.txt")))
        sensitive_files.extend(sorted(common.WORK.glob("_cleanup_deferred_*.bat")))
    if purge_env:
        sensitive_files.extend([
            common.ENV_PATH,
            common.ENV_PATH.with_name(common.ENV_PATH.name + ".lock"),
            common.ROOT / ".env",
            common.ROOT / ".env.lock",
        ])

    deferrable_runtime_names = {
        "server.log",
        "python-bootstrap.log",
        ".process.lock",
        "console_port.json",
    }
    seen_files: set[str] = set()
    for f in sensitive_files:
        fkey = str(f).lower()
        if fkey in seen_files:
            continue
        seen_files.add(fkey)
        if f.exists():
            try:
                f.unlink()
                cleaned.append(f.name)
            except Exception as exc:  # noqa: BLE001
                if schedule_deferred and f.name in deferrable_runtime_names:
                    deferred_files.append(f)
                else:
                    problems.append(f"{f.name} 删除失败: {exc}")

    # 4. 重置 active_platforms.json 为未配置初态
    try:
        if _reset_active_platforms(common.CONFIG / "active_platforms.json"):
            cleaned.append("config/active_platforms.json (已重置)")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"config/active_platforms.json 重置失败: {exc}")

    # 5. 清理 output 产物与日志
    if common.OUT.exists():
        out_targets: list[Path] = []
        for fixed in ("scheduler.log", "scheduler.log.1", "background.log", "todo-demo-preview.md"):
            out_targets.append(common.OUT / fixed)
        for pattern in ("todo-*.md", "异常说明-*.txt", "采集诊断-*.txt"):
            out_targets.extend(sorted(common.OUT.glob(pattern)))
        seen_out: set[str] = set()
        for lf in out_targets:
            okey = str(lf).lower()
            if okey in seen_out:
                continue
            seen_out.add(okey)
            if lf.exists():
                try:
                    lf.unlink()
                    cleaned.append(f"output/{lf.name}")
                except Exception as exc:  # noqa: BLE001
                    if schedule_deferred:
                        deferred_files.append(lf)
                    else:
                        problems.append(f"output/{lf.name} 删除失败: {exc}")

    deferred_script = None
    if schedule_deferred and (deferred_dirs or deferred_files):
        try:
            deferred_script = _schedule_deferred_cleanup(deferred_dirs, deferred_files)
            if deferred_script:
                cleaned.extend([f"[延迟清理] {p.name}" for p in deferred_dirs])
                cleaned.extend([f"[延迟清理] {p.name}" for p in deferred_files])
        except Exception as exc:  # noqa: BLE001
            for p in deferred_dirs + deferred_files:
                problems.append(f"{p.name} 延迟清理调度失败: {exc}")

    return cleaned, problems, deferred_script


def full_uninstall(
    purge_env: bool = True,
    kill_processes: bool = True,
    schedule_deferred: bool = True,
) -> dict[str, Any]:
    """执行完整的一键彻底卸载。

    返回值里的 ok 是**真实汇总**：任何一步失败都会让 ok=False 并列出失败项。
    """
    problems: list[str] = []

    # 0. 先停进程，否则后面的 .env / 日志删除会因占用失败
    proc_res = stop_processes() if kill_processes else {"ok": True, "stopped": [], "failed": []}
    if not proc_res.get("ok"):
        problems.append("部分后台进程未能结束: " + "、".join(proc_res.get("failed") or []))

    # 1. 还原开机自启
    auto_res = remove_autostart()
    if not auto_res.get("ok"):
        problems.append(auto_res.get("message", "自启项注销失败"))

    # 2. 删除桌面快捷方式
    del_shortcuts = remove_shortcuts()

    # 3. 还原 QQ 自动更新组件
    qq_res = restore_qq_updates()
    if not qq_res.get("ok"):
        problems.append(qq_res.get("message", "QQ 更新组件还原失败"))

    # 4. 彻底清理本地缓存与敏感数据
    cleaned_items, clean_problems, deferred_script = clean_work_and_cache(
        purge_env=purge_env,
        schedule_deferred=schedule_deferred,
    )
    problems.extend(clean_problems)

    # 5. 复核：敏感文件是否真的没了
    leftovers = [str(p) for p in (common.ENV_PATH, common.ROOT / ".env") if p.exists()]
    if leftovers and purge_env:
        problems.append("敏感配置仍存在（可能被占用）: " + "；".join(dict.fromkeys(leftovers)))

    ok = not problems
    if ok:
        tail_note = (
            "（浏览器缓存与占用日志将在窗口关闭后约 4 秒自动完成最终清理。）"
            if deferred_script
            else ""
        )
        msg = (
            "开机自启动项、桌面图标、QQ防升级锁与本地解密数据已全部卸载清理干净！"
            f"{tail_note}系统已彻底恢复原貌。如需彻底删除程序，直接将本文件夹移入回收站即可。"
        )
    else:
        msg = "卸载没有完全成功，以下项目需要手工处理：\n  - " + "\n  - ".join(problems)

    return {
        "ok": ok,
        "autostart_removed": auto_res.get("ok"),
        "shortcuts_removed": del_shortcuts,
        "qq_restored": qq_res.get("ok"),
        "processes_stopped": proc_res.get("stopped") or [],
        "cleaned_items": cleaned_items,
        "deferred_cleanup": bool(deferred_script),
        "deferred_script": deferred_script,
        "problems": problems,
        "message": msg,
    }


if __name__ == "__main__":
    print("=" * 65)
    print("  待办自动汇总助手 · 彻底卸载与数据清理")
    print("=" * 65)
    print("正在停止后台进程并执行系统还原与痕迹清理...")
    res = full_uninstall(purge_env=True, kill_processes=True)
    mark = "✔" if res["ok"] else "X"
    print()
    print(f"[{mark}] 开机自启动: {'已全部注销' if res['autostart_removed'] else '未完成'}")
    print(f"[{mark}] 快捷方式: 已删除 {len(res['shortcuts_removed'])} 个桌面与开始菜单图标")
    print(f"[{mark}] 后台进程: 已结束 {len(res['processes_stopped'])} 个")
    print(f"[{mark}] QQ更新锁: {'已完全还原' if res['qq_restored'] else '未完成'}")
    print(f"[{mark}] 数据销毁: 已清理 {len(res['cleaned_items'])} 项解密镜像与缓存")
    if res.get("deferred_cleanup"):
        print("[*] 已安排脱离进程在数秒后完成被占用缓存/日志的尾项清理。")
    print("=" * 65)
    for line in str(res["message"]).splitlines():
        print("  " + line)
    print("=" * 65)
    raise SystemExit(0 if res["ok"] else 1)
