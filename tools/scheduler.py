"""本地定时调度器：按计划执行采集（run.py --no-open），不依赖 Windows 计划任务。

角色边界（重要，与外部定时任务职责分离）
    本模块负责「**采集**」：定时把 QQ/微信/飞书/企微的消息读出来，落盘成
        work/待办素材.json；
    外部协作引擎 的定时任务负责「**提炼 + 推送**」：读素材、写成待办清单、
        推给院长本人（提示词见 automation-prompt.md）。

    两者解耦的好处：外部协作引擎 没开、网络断了、外部接口异常，采集照样在跑，
    素材不会丢；反之提炼失败也不会阻塞下一轮采集。

为什么不用 schtasks
    1. 目标机常见「无管理员权限 / 组策略禁用计划任务」，注册会直接失败；
    2. schtasks 起的进程不是交互会话，历史上出现过「起了个空壳」的坑；
    3. 本程序已经用「启动文件夹 + 常驻线程」跑通了开机自启，同一套机制更可控。

调度模式
    interval : 每 N 分钟跑一次（配合游标不会漏消息）
    daily    : 每天固定几个时刻跑（默认工作日 09:00 / 12:00 / 15:00 / 18:00，
               夜间休眠，并借助 chinese-calendar 跳过中国法定节假日与周末、兼容调休补班）

并发安全
    真正的单实例由 run.py 里的文件锁保证：两个采集撞在一起时，后一个会
    立刻优雅退出（返回 0），不会互相删 work/tmp_db。本模块只额外做一次
    「上一轮还没跑完就跳过」的本地判断，避免堆积。

用法
----
    python tools\\scheduler.py status
    python tools\\scheduler.py run-now              # 立刻采集一次
    python tools\\scheduler.py daemon               # 前台常驻（Ctrl+C 退出）
    python tools\\scheduler.py once                 # 跑一轮就退出（给外部调度用）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import common  # noqa: E402

CONFIG_PATH = common.CONFIG / "schedule.json"
STATE_PATH = common.WORK / "schedule_state.json"
PID_PATH = common.WORK / "scheduler.pid"
LOG_PATH = common.OUT / "scheduler.log"
HISTORY_MAX = 30
DAILY_GRACE_SECONDS = 300  # daily 模式下整点后的触发容错窗口（秒）

DEFAULT_CONFIG = {
    "enabled": True,
    "mode": "daily",                 # interval | daily
    "interval_minutes": 180,
    "daily_times": ["09:00", "12:00", "15:00", "18:00"],
    "hours": 4.0,                    # 传给 run.py --hours
    "max_runtime_seconds": 900,      # 单轮硬超时，防止卡死拖住调度
    "skip_if_last_unfinished": True,
    "skip_holidays": True,           # 跳过中国法定节假日与周末（兼容调休补班）
    "quiet_window": ["21:00-08:00"], # 例如 ["21:00-08:00"]，留空表示全天
}


# 配置与状态
def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update({k: v for k, v in raw.items() if v is not None})
        except (OSError, ValueError):
            pass
    return cfg


def save_config(patch: dict) -> dict:
    cfg = load_config()
    for key, val in patch.items():
        if key in DEFAULT_CONFIG:
            cfg[key] = val
    common.atomic_write_text(CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2))
    return cfg


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        common.atomic_write_text(STATE_PATH, json.dumps(state, ensure_ascii=False, indent=2))
    except OSError:
        pass


# 时间与节假日计算
_holiday_warned = False


def _is_workday(cfg: dict, now: datetime) -> bool:
    """判断是否为工作日（启用 skip_holidays 时优先使用 chinese-calendar，兼容调休补班）。"""
    global _holiday_warned
    if not cfg.get("skip_holidays"):
        return True
    try:
        import chinese_calendar  # noqa: PLC0415

        return bool(chinese_calendar.is_workday(now.date()))
    except (ImportError, NotImplementedError, ValueError) as exc:
        if not _holiday_warned:
            _holiday_warned = True
            _append_log(f"[holiday] chinese-calendar 不可用或年份未覆盖 ({exc})，降级为周一至周五判断")
        return now.weekday() < 5


def _parse_hhmm(text: str) -> tuple[int, int] | None:
    try:
        h, m = str(text).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except (ValueError, AttributeError):
        pass
    return None


def _in_quiet_window(cfg: dict, now: datetime) -> bool:
    for span in cfg.get("quiet_window") or []:
        try:
            start_s, end_s = str(span).split("-")
        except ValueError:
            continue
        start, end = _parse_hhmm(start_s), _parse_hhmm(end_s)
        if not start or not end:
            continue
        cur = now.hour * 60 + now.minute
        a, b = start[0] * 60 + start[1], end[0] * 60 + end[1]
        inside = (a <= cur < b) if a <= b else (cur >= a or cur < b)
        if inside:
            return True
    return False


def _due_daily_slot(cfg: dict, state: dict, now: datetime) -> datetime | None:
    """daily 模式下检查当前是否命中今日尚未执行过的固定时刻（含容错窗口）。"""
    if not _is_workday(cfg, now) or _in_quiet_window(cfg, now):
        return None
    times = [t for t in (_parse_hhmm(x) for x in (cfg.get("daily_times") or [])) if t]
    if not times:
        times = [(_parse_hhmm(x) or (9, 0)) for x in DEFAULT_CONFIG["daily_times"]]
    last_start = float(state.get("last_start") or 0)
    base = now.replace(second=0, microsecond=0)
    for h, m in sorted(times):
        slot = base.replace(hour=h, minute=m)
        if slot <= now and (now - slot).total_seconds() <= DAILY_GRACE_SECONDS:
            if last_start < slot.timestamp():
                return slot
    return None


def next_run_at(cfg: dict, now: datetime | None = None) -> datetime | None:
    """算下一次该跑的时刻。返回 None 表示不会自动跑（已停用）。"""
    if not cfg.get("enabled"):
        return None
    now = now or datetime.now()
    mode = str(cfg.get("mode") or "daily")

    if mode == "daily":
        times = [t for t in (_parse_hhmm(x) for x in (cfg.get("daily_times") or [])) if t]
        if not times:
            times = [(_parse_hhmm(x) or (9, 0)) for x in DEFAULT_CONFIG["daily_times"]]
        for day in range(0, 31):
            base = (now + timedelta(days=day)).replace(second=0, microsecond=0)
            if not _is_workday(cfg, base):
                continue
            for h, m in sorted(times):
                slot = base.replace(hour=h, minute=m)
                if slot > now and not _in_quiet_window(cfg, slot):
                    return slot
        return None

    try:
        minutes = max(1, int(cfg.get("interval_minutes") or 180))
    except (TypeError, ValueError):
        minutes = 180
    last = load_state().get("last_start")
    nxt = now
    if last:
        try:
            base = datetime.fromtimestamp(float(last))
            cand = base + timedelta(minutes=minutes)
            nxt = cand if cand > now else now
        except (TypeError, ValueError, OSError):
            nxt = now
    for _ in range(31):
        if _is_workday(cfg, nxt) and not _in_quiet_window(cfg, nxt):
            return nxt
        nxt = (nxt + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    return nxt


# 执行一轮
@dataclass
class RunResult:
    rc: int = -1
    seconds: float = 0.0
    started: str = ""
    output_tail: str = ""
    skipped: str = ""
    extra: dict = field(default_factory=dict)


def _python() -> str:
    """采集子进程用的解释器：统一由 runtime_env 裁决（缺 sqlcipher3 的绝对不用）。"""
    try:
        import runtime_env  # noqa: PLC0415

        return str(runtime_env.work_python())
    except Exception:  # noqa: BLE001
        venv = ROOT / ".venv" / "Scripts" / "python.exe"
        return str(venv) if venv.exists() else sys.executable


_runtime_checked = False


def _ensure_runtime() -> None:
    """确保有能解 QQ 库的解释器（缺 sqlcipher3 就离线补一次）。

    为什么调度器也要管这件事：
        Start.bat 会在双击时自动建 .venv 装依赖，但调度器是被启动项/控制台
        直接拉起的，绕过了那一步。用户包又没有 .venv —— 于是每一轮都
        「QQ 解密失败」，而且看不出是环境问题。这里补上，只补一次。

    v1.5.3.3 修正：不再只看「.venv\\Scripts\\python.exe 是否存在」。
    文件在、但依赖被清理或装坏时，旧实现会直接 return，于是每轮都以
    ImportError 失败且永远不触发修复。现在改成按**能力**探测。
    """
    global _runtime_checked
    if _runtime_checked:
        return
    _runtime_checked = True

    venv_py = ROOT / ".venv" / "Scripts" / "python.exe"
    if venv_py.exists():
        try:
            probe = subprocess.run([str(venv_py), "-c", "import sqlcipher3"],
                                   capture_output=True, timeout=90)
            if probe.returncode == 0:
                return
            _append_log("[runtime] .venv 存在但缺少 sqlcipher3，触发离线修复")
        except (OSError, subprocess.SubprocessError) as exc:
            _append_log(f"[runtime] .venv 探测失败: {exc}")
    else:
        try:
            import importlib.util  # noqa: PLC0415

            if importlib.util.find_spec("sqlcipher3") is not None:
                return
        except (ImportError, ValueError):
            pass

    boot = ROOT / "tools" / "bootstrap_deps.py"
    if not boot.exists():
        return
    _append_log("[runtime] 尝试离线补齐运行时 ...")
    try:
        proc = subprocess.run(
            [sys.executable, str(boot)], cwd=str(ROOT),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600,
        )
        tail = ((proc.stdout or "") + (proc.stderr or "")).strip()[-400:]
        _append_log(f"[runtime] bootstrap rc={proc.returncode} {tail}")
    except (OSError, subprocess.SubprocessError) as exc:
        _append_log(f"[runtime] 依赖补齐失败: {exc}")


def run_once(hours: float | None = None, timeout: int | None = None) -> RunResult:
    """跑一轮采集。返回退出码与最后一屏输出（供控制台/状态文件展示）。"""
    cfg = load_config()
    state = load_state()
    if cfg.get("skip_if_last_unfinished") and state.get("running"):
        elapsed = time.time() - float(state.get("running_since") or 0)
        if 0 < elapsed < float(cfg.get("max_runtime_seconds") or 900):
            return RunResult(skipped="上一轮采集尚未结束，本轮跳过")

    _ensure_runtime()          # 缺 sqlcipher3 时先离线补 .venv，否则每轮都「QQ 解密失败」
    hours = hours if hours is not None else cfg.get("hours")
    cmd = [_python(), str(ROOT / "run.py"), "--no-open"]
    if hours:
        cmd += ["--hours", str(hours)]
    limit = int(timeout or cfg.get("max_runtime_seconds") or 900)

    started_at = datetime.now()
    state.update({
        "running": True,
        "running_since": time.time(),
        "last_start": time.time(),
        "last_start_text": started_at.strftime("%Y-%m-%d %H:%M:%S"),
    })
    save_state(state)

    t0 = time.time()
    tail = ""
    rc = -1
    try:
        proc = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=limit,
        )
        rc = proc.returncode
        tail = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()[-1200:]
    except subprocess.TimeoutExpired:
        rc = 124
        tail = f"采集超过 {limit} 秒被调度器强制结束（可能是巨量消息或库被占用）"
    except OSError as exc:
        rc = 127
        tail = f"无法启动采集进程: {exc}"
    seconds = round(time.time() - t0, 1)

    state = load_state()
    history = state.get("history") or []
    history.append({
        "at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
        "rc": rc,
        "seconds": seconds,
        "tail": tail[-300:],
    })
    state.update({
        "running": False,
        "running_since": 0,
        "last_end": time.time(),
        "last_end_text": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_rc": rc,
        "last_seconds": seconds,
        "last_output": tail[-600:],
        "history": history[-HISTORY_MAX:],
    })
    nxt = next_run_at(cfg)
    state["next_run_text"] = nxt.strftime("%Y-%m-%d %H:%M:%S") if nxt else ""
    save_state(state)
    _append_log(f"[{started_at:%Y-%m-%d %H:%M:%S}] rc={rc} {seconds}s {tail[-200:]}")
    # run.py 用 75 表示"抢锁失败、本轮没采集"。这不是成功，必须如实标注，
    # 否则监控台会把"啥也没采"显示成"刚跑过、成功"。
    skipped = "另一采集实例正在运行（本机开机自启与定时任务撞车），本轮已跳过" if rc == 75 else ""
    return RunResult(rc=rc, seconds=seconds, output_tail=tail, skipped=skipped,
                     started=started_at.strftime("%Y-%m-%d %H:%M:%S"))


def _append_log(line: str) -> None:
    """追加一行日志，并在超过 2 MB 时滚动一次（只留一个 .1 备份）。"""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        # 无轮转的日志在长期常驻的机器上会无限增长，这里做个便宜的滚动。
        try:
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > 2 * 1024 * 1024:
                backup = LOG_PATH.with_suffix(LOG_PATH.suffix + ".1")
                if backup.exists():
                    backup.unlink()
                LOG_PATH.rename(backup)
        except OSError:
            pass
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line.replace("\n", " ") + "\n")
    except OSError:
        pass


# 常驻循环
_stop = threading.Event()
_alive = threading.Event()      # 本进程内调度线程是否活着，用于 status 判断


def stop() -> None:
    _stop.set()


def daemon_loop(poll_seconds: float = 20.0) -> None:
    """按计划反复执行。可被 server.py 以线程方式拉起，也可前台运行。"""
    _write_pid()
    _alive.set()
    last_cfg_mtime = 0.0
    try:
        while not _stop.is_set():
            cfg = load_config()
            try:
                mtime = CONFIG_PATH.stat().st_mtime
                if mtime != last_cfg_mtime:
                    last_cfg_mtime = mtime
            except OSError:
                pass
            now = datetime.now()
            due = next_run_at(cfg, now)
            state = load_state()
            state["next_run_text"] = due.strftime("%Y-%m-%d %H:%M:%S") if due else ""
            state["enabled"] = bool(cfg.get("enabled"))
            state["mode"] = cfg.get("mode")
            state["pid"] = os.getpid()
            save_state(state)

            mode = str(cfg.get("mode") or "daily")
            if mode == "daily":
                should_run = bool(cfg.get("enabled") and _due_daily_slot(cfg, state, now) is not None)
            else:
                should_run = bool(
                    cfg.get("enabled")
                    and due
                    and due <= now
                    and not _in_quiet_window(cfg, now)
                    and _is_workday(cfg, now)
                )

            if should_run:
                if state.get("running"):
                    # 上一轮还没结束：interval 模式下把下次推到 1 分钟后，别堆任务
                    if mode == "interval":
                        st = load_state()
                        st["last_start"] = time.time() - (int(cfg.get("interval_minutes") or 180) * 60) + 60
                        save_state(st)
                else:
                    run_once()
            _stop.wait(poll_seconds)
    finally:
        _alive.clear()
        try:
            PID_PATH.unlink(missing_ok=True)
        except OSError:
            pass


def _write_pid() -> None:
    try:
        common.atomic_write_text(PID_PATH, str(os.getpid()))
    except OSError:
        pass


def is_running() -> bool:
    if _alive.is_set():
        return True
    if not PID_PATH.exists():
        return False
    try:
        pid = int(PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            flags = common.CREATE_NO_WINDOW
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                errors="replace", timeout=5, creationflags=flags,
            )
            # 精确匹配 PID 字段，别用 `str(pid) in out` 做子串匹配：
            # PID 123 会命中 1234，PID 被系统复用后也会被误判成"仍在运行"。
            text = str(out or "")
            return bool(re.search(rf'"{pid}"', text)) or text.strip().startswith(str(pid))
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# 状态输出
def status() -> dict:
    cfg = load_config()
    state = load_state()
    now = datetime.now()
    nxt = next_run_at(cfg, now)
    return {
        "enabled": bool(cfg.get("enabled")),
        "mode": cfg.get("mode"),
        "hours": cfg.get("hours"),
        "interval_minutes": cfg.get("interval_minutes"),
        "daily_times": cfg.get("daily_times"),
        "skip_holidays": bool(cfg.get("skip_holidays")),
        "quiet_window": cfg.get("quiet_window") or [],
        "is_workday_today": _is_workday(cfg, now),
        "daemon_running": is_running(),
        "running_now": bool(state.get("running")),
        "last_start": state.get("last_start_text", ""),
        "last_end": state.get("last_end_text", ""),
        "last_rc": state.get("last_rc"),
        "last_seconds": state.get("last_seconds"),
        "last_output": state.get("last_output", ""),
        "next_run": nxt.strftime("%Y-%m-%d %H:%M:%S") if nxt else "",
        "history": (state.get("history") or [])[-5:],
        "config_path": str(CONFIG_PATH),
    }


def _print_status(data: dict) -> None:
    common.log("=" * 60)
    common.log("  定时调度状态")
    common.log("=" * 60)
    common.log(f"  启用: {'是' if data['enabled'] else '否'}   模式: {data['mode']}")
    if data["mode"] == "interval":
        common.log(f"  间隔: 每 {data['interval_minutes']} 分钟（回看 {data['hours']} 小时）")
    else:
        common.log(f"  时刻: {', '.join(data['daily_times'] or [])}（回看 {data['hours']} 小时）")
    common.log(
        f"  跳过节假日: {'是' if data.get('skip_holidays') else '否'}"
        f"   今日状态: {'工作日' if data.get('is_workday_today') else '节假日/休息日（自动休眠）'}"
    )
    if data.get("quiet_window"):
        common.log(f"  夜间休眠: {', '.join(data['quiet_window'])}")
    common.log(f"  常驻进程: {'运行中' if data['daemon_running'] else '未运行'}"
               f"   本轮采集: {'进行中' if data['running_now'] else '空闲'}")
    common.log(f"  上次开始: {data['last_start'] or '—'}   上次结束: {data['last_end'] or '—'}"
               f"   rc={data['last_rc']}")
    common.log(f"  下次计划: {data['next_run'] or '—'}")
    if data["last_output"]:
        common.log("  上次输出末段:")
        for line in data["last_output"].splitlines()[-6:]:
            common.log(f"    {line}")
    common.log("=" * 60)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="待办自动汇总 · 本地定时调度器")
    ap.add_argument("action", nargs="?", default="status",
                    choices=["status", "run-now", "once", "daemon", "enable", "disable", "config"])
    ap.add_argument("--hours", type=float, default=None)
    ap.add_argument("--interval", type=int, default=None, help="设置间隔分钟数")
    ap.add_argument("--daily", default=None, help="设置每日时刻，逗号分隔，如 09:00,12:00,15:00,18:00")
    args = ap.parse_args(argv)

    if args.interval is not None:
        save_config({"interval_minutes": max(1, args.interval), "mode": "interval"})
        common.log(f"已设置：每 {args.interval} 分钟采集一次")
    if args.daily:
        times = [t.strip() for t in args.daily.split(",") if t.strip()]
        bad = [t for t in times if not _parse_hhmm(t)]
        if bad:
            common.log(f"[X] 时刻格式不对: {bad}（应为 HH:MM）")
            return 2
        save_config({"daily_times": times, "mode": "daily"})
        common.log(f"已设置：每日 {', '.join(times)} 采集")

    if args.action == "status":
        _print_status(status())
        return 0
    if args.action in ("run-now", "once"):
        res = run_once(hours=args.hours)
        if res.skipped:
            common.log(f"[跳过] {res.skipped}")
            return 0
        common.log(f"采集结束 rc={res.rc} 用时 {res.seconds}s")
        if res.output_tail:
            common.log(res.output_tail[-800:])
        return 0 if res.rc == 0 else 1
    if args.action == "enable":
        save_config({"enabled": True})
        common.log("已启用定时调度")
        return 0
    if args.action == "disable":
        save_config({"enabled": False})
        common.log("已停用定时调度（不会自动采集，可用 run-now 手动跑）")
        return 0
    if args.action == "daemon":
        common.log(f"调度器常驻启动，配置文件: {CONFIG_PATH}")
        common.log("Ctrl+C 退出")
        try:
            daemon_loop()
        except KeyboardInterrupt:
            common.log("已退出调度器")
        return 0
    if args.action == "config":
        common.log(json.dumps(load_config(), ensure_ascii=False, indent=2))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
