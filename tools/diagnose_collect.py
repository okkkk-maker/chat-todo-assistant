"""采集自检（一条命令回答"为什么一条消息都没采到"）

用法：
    python tools/diagnose_collect.py            # 打印报告 + 写 output/采集诊断-<时间>.txt
    python tools/diagnose_collect.py --deep     # 额外做一次真实解密并统计窗口内行数（慢，但最准）
    python tools/diagnose_collect.py --json     # 机器可读，供控制台 /api/diagnose 调用

设计原则
1. **不猜**：每个结论都带证据（文件时间、行数、错误原文）。
2. **区分三种"0 条"**：
       A. 真没有新消息（数据源在线、窗口内确实为空）
       B. 有消息但被过滤/表结构不适配（库里有、抽取没取到）
       C. 根本没读到数据源（缺密钥/缺库/权限/解释器缺依赖）
   三者处理方式完全不同，混在一起就会变成"永远查不出为什么"。
3. **只读**：除了写一份报告到 output/，不改任何配置、不动 .env、不碰原位库。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import common  # noqa: E402

LINE = "=" * 72


def _f(p: Path) -> str:
    try:
        st = p.stat()
        return f"{datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M:%S}  {st.st_size/1048576:.1f} MB"
    except OSError:
        return "不存在"


def _age_hours(ts: float | None) -> str:
    if not ts:
        return "—"
    gap = time.time() - ts
    if gap < 3600:
        return f"{gap/60:.0f} 分钟前"
    return f"{gap/3600:.1f} 小时前"


def _read_cursor() -> float | None:
    try:
        return float(common.CURSOR_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def collect(deep: bool = False) -> dict:
    """采集所有诊断证据，返回结构化结果（结论在 conclusion 里）。"""
    common.ensure_dirs()
    now = time.time()
    env = common.load_env()
    out: dict = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "now": now,
        "findings": {},     # 分组证据
        "problems": [],     # 命中问题（严重）
        "notes": [],        # 提示（不一定是故障）
        "conclusion": "",
        "verdict": "unknown",   # A_no_new | B_filtered | C_no_source | ok | error
    }

    # ---------- 1. 运行环境 ----------
    rt: dict = {}
    try:
        import runtime_env  # noqa: PLC0415

        snap = runtime_env.runtime_snapshot()
        rt = {
            "python": snap.get("python"),
            "version": snap.get("version"),
            "capable": snap.get("capable"),
            "capabilities": snap.get("capabilities"),
            "venv": snap.get("venv"),
        }
        if not snap.get("capable"):
            out["problems"].append(
                "当前解释器缺少 sqlcipher3，QQ 库无法解密。请用 .venv\\Scripts\\python.exe "
                "运行本工具，或直接双击 启动桌面App.bat（会自动切到 .venv）。"
            )
    except Exception as exc:  # noqa: BLE001
        rt = {"error": str(exc)}
        out["problems"].append(f"运行环境自检失败: {exc}")
    out["findings"]["runtime"] = rt

    # ---------- 2. 平台配置 ----------
    cfg_path = common.CONFIG / "active_platforms.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    except (OSError, ValueError) as exc:
        cfg = {}
        out["problems"].append(f"active_platforms.json 读不出来: {exc}")
    enabled = cfg.get("enabled_platforms") or []
    mode = str(cfg.get("config_mode") or "")
    out["findings"]["platforms"] = {
        "config_mode": mode or "(未选择)",
        "enabled_platforms": enabled,
        "optional": cfg.get("optional_platforms") or [],
        "updated_at": cfg.get("updated_at", ""),
    }
    if not mode:
        out["problems"].append("尚未选择配置模式：采集会被直接拒绝（WAITING_CONFIG），一条都不会产出。")
    if not enabled:
        out["problems"].append("未勾选任何采集平台：enabled_platforms 为空。")

    # 调度配置
    try:
        sched_cfg = json.loads((common.CONFIG / "schedule.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sched_cfg = {}
    try:
        sched_state = json.loads((common.WORK / "schedule_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sched_state = {}
    out["findings"]["schedule"] = {
        "enabled": sched_cfg.get("enabled"),
        "mode": sched_cfg.get("mode"),
        "interval_minutes": sched_cfg.get("interval_minutes"),
        "hours": sched_cfg.get("hours"),
        "last_start": sched_state.get("last_start_text", ""),
        "last_end": sched_state.get("last_end_text", ""),
        "last_rc": sched_state.get("last_rc"),
        "next_run": sched_state.get("next_run_text", ""),
        "running": sched_state.get("running"),
    }

    # ---------- 3. 游标与时间窗（"触发条件"）----------
    cur = _read_cursor()
    try:
        since, until, why = common.window(None)
    except Exception:  # noqa: BLE001
        since, until, why = now - 4 * 3600, now, "无法计算"
    out["findings"]["window"] = {
        "cursor": common.fmt(cur) if cur else "(无游标，首轮)",
        "cursor_age": _age_hours(cur),
        "since": common.fmt(since),
        "until": common.fmt(until),
        "why": why,
        "span_minutes": round((until - since) / 60, 1),
    }
    if cur and until - cur > 24 * 3600:
        out["notes"].append(
            f"游标距今已 {_age_hours(cur)}：本轮会从游标处追赶，窗口很大，单轮可能被截断（截断时游标会冻结）。"
        )

    # ---------- 4. 数据源：QQ ----------
    qq: dict = {
        "db_path": env.get("QQ_DB_PATH", ""),
        "key_set": bool(env.get("QQ_DBKEY")),
        "key_len": len(env.get("QQ_DBKEY", "") or ""),
        "profile": env.get("QQ_CIPHER_PROFILE", ""),
        "schema_tables": 0,
        "in_window": None,
        "latest": None,
        "sample_types": [],
    }
    db = common.safe_path(env.get("QQ_DB_PATH", ""))
    if db is not None and db.exists():
        qq["db_mtime"] = datetime.fromtimestamp(db.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        qq["db_mtime_age"] = _age_hours(db.stat().st_mtime)
        qq["wal"] = _f(Path(str(db) + "-wal"))
        qq["shm"] = _f(Path(str(db) + "-shm"))
    else:
        out["problems"].append(f"QQ 数据库文件不存在: {db or '(未配置)'}（QQ 未登录过 / 聊天记录不在本机 / 盘符变了）")

    if qq["key_set"] and qq["key_len"] != 16:
        out["problems"].append(f"QQ_DBKEY 长度 {qq['key_len']}，NTQQ 要求 16 位；密钥可能被脱敏值覆盖过，需重新抓取。")

    # schema
    try:
        schema = json.loads(common.SCHEMA_PATH.read_text(encoding="utf-8"))
        qq["schema_tables"] = len(schema.get("tables") or [])
    except (OSError, ValueError):
        schema = {}
        out["problems"].append("config/schema.json 不存在或损坏：抽取会以『表结构未适配』拒绝运行。")
    if schema and not qq["schema_tables"]:
        out["problems"].append("schema.json 里没有可用消息表：抽取会拒绝运行（宁可不产出也不编造）。")
    out["findings"]["qq"] = qq

    # 深度检查：真解密 + 统计窗口内行数 + 最新消息时间
    if deep and db is not None and db.exists() and qq["key_set"] and qq["key_len"] == 16 and rt.get("capable"):
        try:
            import qqdb  # noqa: PLC0415

            tmp = common.WORK / ".tmp_diagnose"
            shutil.rmtree(tmp, ignore_errors=True)
            con, used, _ = qqdb.open_encrypted(db, tmp, qq["key_set"] and env["QQ_DBKEY"],
                                               env.get("QQ_CIPHER_PROFILE") or None)
            qq["decrypt_profile"] = used
            latest_ts = 0.0
            in_win = 0
            for spec in schema.get("tables") or []:
                cols = spec.get("columns") or {}
                ts_col = cols.get("timestamp")
                if not ts_col:
                    continue
                try:
                    mx = con.execute(f'SELECT MAX({common.quote_ident(ts_col)}) FROM {common.quote_ident(spec["name"])}').fetchone()[0]
                    if mx:
                        latest_ts = max(latest_ts, float(mx))
                    cnt = con.execute(
                        f'SELECT count(*) FROM "{spec["name"]}" WHERE "{ts_col}">=? AND "{ts_col}"<=?',
                        (int(since), int(until)),
                    ).fetchone()[0]
                    in_win += int(cnt)
                    qq["sample_types"].append({"table": spec["name"], "rows_in_window": int(cnt)})
                except Exception:  # noqa: BLE001
                    continue
            con.close()
            shutil.rmtree(tmp, ignore_errors=True)
            qq["in_window"] = in_win
            if latest_ts:
                qq["latest"] = datetime.fromtimestamp(latest_ts).strftime("%Y-%m-%d %H:%M:%S")
                qq["latest_age"] = _age_hours(latest_ts)
                if latest_ts < since:
                    out["notes"].append(
                        f"QQ 库内最新消息是 {qq['latest']}（{qq['latest_age']}），早于本轮窗口起点 "
                        f"{common.fmt(since)} —— 所以本轮 0 条属于「确实没有新消息」。"
                    )
            else:
                out["problems"].append("QQ 库能解密，但按 schema 找不到任何时间列，无法统计（表结构需重探）。")
        except Exception as exc:  # noqa: BLE001
            out["problems"].append(f"QQ 深度检查失败（解密/查询出错）: {str(exc).splitlines()[0]}")

    # ---------- 5. 数据源：微信 ----------
    wx: dict = {"key_set": bool(env.get("WX_DBKEY")), "key_len": len(env.get("WX_DBKEY", "") or "")}
    try:
        from channels import wechat  # noqa: PLC0415

        storage = wechat.find_source_storage((env.get("WX_DATA_PATH") or "").strip())
        wx["storage"] = str(storage) if storage else ""
        if storage:
            src = storage / "message" / "message_0.db"
            wx["src_db"] = _f(src)
            vendor = wechat.find_vendor()
            mirror = wechat._mirror_path(env, storage) if vendor else None
            if mirror:
                wx["mirror"] = _f(mirror / "message" / "message_0.db")
                try:
                    need = src.stat().st_mtime > (mirror / "message" / "message_0.db").stat().st_mtime + 1
                    wx["mirror_stale"] = bool(need)
                except OSError:
                    wx["mirror_stale"] = True
        else:
            out["problems"].append("未找到微信 4.x 数据目录（xwechat_files/<wxid>/db_storage）。")
    except Exception as exc:  # noqa: BLE001
        wx["error"] = str(exc)
    if wx.get("key_len") and wx["key_len"] != 64:
        out["problems"].append(f"WX_DBKEY 长度 {wx['key_len']}，应为 64 位；可能被脱敏值覆盖。")
    out["findings"]["wechat"] = wx

    # ---------- 6. 收件箱类通道 ----------
    inbox: dict = {}
    for name in ("wecom", "dingtalk"):
        p = common.WORK / "inbox" / f"{name}.jsonl"
        try:
            n = sum(1 for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip().startswith("{"))
        except OSError:
            n = -1
        inbox[name] = {"path": str(p), "exists": p.exists(), "lines": n}
    out["findings"]["inbox"] = inbox
    if "WeCom" in enabled and inbox["wecom"]["exists"] is False:
        out["notes"].append("企业微信走收件箱模式，但 work/inbox/wecom.jsonl 不存在（连接器/回调尚未写入）。")

    # ---------- 7. 上次产物 ----------
    mat: dict = {"exists": common.MATERIAL_PATH.exists()}
    if common.MATERIAL_PATH.exists():
        try:
            data = json.loads(common.MATERIAL_PATH.read_text(encoding="utf-8"))
            meta = data.get("meta") or {}
            mat.update({
                "updated_at": datetime.fromtimestamp(common.MATERIAL_PATH.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "message_count": meta.get("message_count"),
                "status": meta.get("status"),
                "data_state": meta.get("data_state"),
                "time_range": meta.get("time_range"),
                "channel_stats": {
                    k: {"status": v.get("status"), "count": v.get("count"),
                        "raw": v.get("raw"), "message": (v.get("message") or "")[:80]}
                    for k, v in (meta.get("channel_stats") or {}).items()
                },
            })
        except (OSError, ValueError) as exc:
            mat["error"] = str(exc)
    out["findings"]["material"] = mat

    todos = sorted(common.OUT.glob("todo-*.md")) if common.OUT.exists() else []
    excs = sorted(common.OUT.glob("异常说明-*.txt")) if common.OUT.exists() else []
    out["findings"]["output"] = {
        "todo_files": [p.name for p in todos],
        "exception_files": [p.name for p in excs],
    }
    if not todos:
        out["notes"].append(
            "output/ 下没有任何 todo-*.md：待办清单由 WorkBuddy 定时任务读素材后提炼生成，"
            "程序自身不会凭空生成清单。素材为 0 条时清单自然也不会出现。"
        )

    # ---------- 8. 权限 ----------
    perm: dict = {}
    for label, p in (("work", common.WORK), ("output", common.OUT), ("config", common.CONFIG)):
        try:
            probe = p / f".perm_probe_{os.getpid()}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            perm[label] = True
        except OSError as exc:
            perm[label] = f"不可写: {exc}"
            out["problems"].append(f"{label} 目录不可写：{exc}")
    if db is not None and db.exists():
        try:
            with open(db, "rb") as fh:
                fh.read(16)
            perm["qq_db_readable"] = True
        except OSError as exc:
            perm["qq_db_readable"] = f"不可读: {exc}"
            out["problems"].append(f"QQ 库不可读（可能被 QQ 独占/权限不足/杀软拦截）：{exc}")
    out["findings"]["permissions"] = perm

    # ---------- 9. 结论 ----------
    if out["problems"]:
        out["verdict"] = "C_no_source"
        out["conclusion"] = (
            "存在阻断性问题（见 problems）。这一类属于「根本没读到数据源」，"
            "而不是「没有待办」，按列表逐条修即可。"
        )
    elif qq.get("in_window") == 0 and (qq.get("latest") or "") and mat.get("message_count") == 0:
        out["verdict"] = "A_no_new"
        out["conclusion"] = (
            "数据源、密钥、表结构、权限都正常；本轮窗口内确实没有新消息"
            f"（QQ 最新消息 {qq.get('latest')}）。这属于「没有新待办」，不是故障。"
        )
    elif qq.get("in_window", 0) > 0 and (mat.get("message_count") or 0) == 0:
        out["verdict"] = "B_filtered"
        out["conclusion"] = (
            f"库里有 {qq['in_window']} 行落在本轮窗口内，但素材 0 条 —— 属于「有消息但被过滤/抽取没取到」，"
            "请把本报告连同 work/schema.txt 一起发回，重点核对时间列单位与正文列映射。"
        )
    elif (mat.get("message_count") or 0) > 0:
        out["verdict"] = "ok"
        out["conclusion"] = f"采集正常：最近一轮已产出 {mat.get('message_count')} 条素材。"
    else:
        out["verdict"] = "unknown"
        out["conclusion"] = "证据不足（建议加 --deep 再跑一次；若仍无结论，请把本报告发回）。"
    return out


def render_text(report: dict) -> str:
    f = report["findings"]
    lines: list[str] = []
    add = lines.append
    add(LINE)
    add("  待办自动汇总助手 · 采集自检报告")
    add(f"  生成时间: {report['generated_at']}    结论: {report['verdict']}")
    add(LINE)

    rt = f.get("runtime", {})
    add("\n[1] 运行环境")
    add(f"    解释器      : {rt.get('python')}（{rt.get('version')}）")
    caps = rt.get("capabilities") or {}
    add(f"    依赖        : sqlcipher3={'有' if caps.get('sqlcipher3') else '缺'} "
        f"pycryptodome={'有' if caps.get('Crypto') else '缺'} "
        f"zstd={'有' if caps.get('zstandard') else '缺'}")
    add(f"    .venv       : {rt.get('venv') or '(无)'}")

    pf = f.get("platforms", {})
    add("\n[2] 平台配置")
    add(f"    配置模式    : {pf.get('config_mode')}")
    add(f"    已启用平台  : {', '.join(pf.get('enabled_platforms') or []) or '(空)'}")
    add(f"    可选通道    : {', '.join(pf.get('optional') or []) or '(无)'}")

    sc = f.get("schedule", {})
    add("\n[3] 触发条件（调度）")
    add(f"    调度启用    : {sc.get('enabled')}   模式 {sc.get('mode')}  间隔 {sc.get('interval_minutes')} 分钟  回看 {sc.get('hours')} 小时")
    add(f"    上次执行    : {sc.get('last_start') or '—'}  rc={sc.get('last_rc')}   下次 {sc.get('next_run') or '—'}")

    w = f.get("window", {})
    add("\n[4] 本轮时间窗（增量）")
    add(f"    游标        : {w.get('cursor')}（{w.get('cursor_age')}）")
    add(f"    窗口        : {w.get('since')} ~ {w.get('until')}  共 {w.get('span_minutes')} 分钟")
    add(f"    依据        : {w.get('why')}")

    qq = f.get("qq", {})
    add("\n[5] 数据源 · QQ")
    add(f"    数据库      : {qq.get('db_path') or '(未配置)'}")
    if qq.get("db_mtime_age"):
        add(f"    库文件      : 最后写入 {qq.get('db_mtime')}（{qq.get('db_mtime_age')}）")
        add(f"    WAL / SHM   : {qq.get('wal')}  |  {qq.get('shm')}")
    add(f"    密钥        : {'已配置 %d 位' % qq.get('key_len') if qq.get('key_set') else '未配置'}   参数 {qq.get('profile') or '(未探测)'}")
    add(f"    表结构      : {qq.get('schema_tables')} 张消息表")
    if qq.get("decrypt_profile"):
        add(f"    实测解密    : 成功（参数 {qq['decrypt_profile']}）")
    if qq.get("latest"):
        add(f"    库内最新    : {qq['latest']}（{qq.get('latest_age')}）")
    if qq.get("in_window") is not None:
        add(f"    窗口内行数  : {qq['in_window']}")

    wx = f.get("wechat", {})
    add("\n[6] 数据源 · 个人微信")
    add(f"    数据目录    : {wx.get('storage') or '(未找到)'}")
    add(f"    源库        : {wx.get('src_db', '(无)')}")
    add(f"    明文镜像    : {wx.get('mirror', '(无)')}   需刷新={wx.get('mirror_stale')}")
    add(f"    密钥        : {'已配置 %d 位' % wx.get('key_len') if wx.get('key_set') else '未配置'}")

    inbox = f.get("inbox", {})
    add("\n[7] 收件箱通道")
    for k, v in inbox.items():
        add(f"    {k:<9}   : 存在={v['exists']}  行数={v['lines']}  {v['path']}")

    mat = f.get("material", {})
    add("\n[8] 最近一次产物（work/待办素材.json）")
    add(f"    文件        : 存在={mat.get('exists')}  更新于 {mat.get('updated_at') or '—'}")
    add(f"    结果        : 素材 {mat.get('message_count')} 条  status={mat.get('status')}  data_state={mat.get('data_state')}")
    add(f"    覆盖时间    : {mat.get('time_range') or '—'}")
    for ch, st in (mat.get("channel_stats") or {}).items():
        add(f"      - {ch:<8} status={st.get('status')} count={st.get('count')} raw={st.get('raw')}  {st.get('message')}")

    out = f.get("output", {})
    add("\n[9] output 产物")
    add(f"    待办清单    : {', '.join(out.get('todo_files') or []) or '(无)'}")
    add(f"    异常说明    : {', '.join(out.get('exception_files') or []) or '(无)'}")

    perm = f.get("permissions", {})
    add("\n[10] 权限")
    for k, v in perm.items():
        add(f"    {k:<14}: {v}")

    if report["problems"]:
        add("\n[!] 阻断性问题")
        for p in report["problems"]:
            add(f"    · {p}")
    if report["notes"]:
        add("\n[i] 提示")
        for n in report["notes"]:
            add(f"    · {n}")

    add("\n" + LINE)
    add("  结论")
    add(LINE)
    add("  " + report["conclusion"])
    add(LINE)
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="采集自检：定位「为什么一条消息都没采到」")
    ap.add_argument("--deep", action="store_true", help="额外做真实解密与窗口内行数统计（慢）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（不写文件）")
    ap.add_argument("--save", action="store_true", help="强制写 output/采集诊断-*.txt")
    args = ap.parse_args(argv)

    report = collect(deep=args.deep)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    text = render_text(report)
    print(text)
    if args.save or not args.json:
        try:
            common.OUT.mkdir(parents=True, exist_ok=True)
            path = common.OUT / f"采集诊断-{datetime.now():%Y%m%d-%H%M%S}.txt"
            path.write_text(text, encoding="utf-8")
            print(f"\n报告已保存: {path}")
        except OSError as exc:
            print(f"\n[!] 报告保存失败: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
