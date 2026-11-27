"""多平台聊天记录统一聚合器（向后完全兼容现有 QQ 方案）。

核心功能：
    1. 统一调度：QQ（本地解密）+ 微信/飞书（原生适配器）+ 企微/钉钉（收件箱）。
    2. 零破坏保障：若未配置其他平台，100% 保持 QQ 原有逻辑与输出不变。
    3. 严格溯源：每条素材携带 (source, chat_type, chat_id, chat_name, sender, timestamp, msg_id)。
    4. 模式感知：读取 config/active_platforms.json，未勾选的平台坚决不采集。
    5. 状态诚实：每个通道自己声明 OK/FAIL/NOT_CONFIGURED/NOT_INSTALLED，
       任何一个通道「没读到」都不得被写成「没有待办」。

通道落地方式见 tools/channels/__init__.py：
    NATIVE  WeChat / Feishu  —— 本程序自己拉
    INBOX   WeCom / DingTalk —— 由 外部协作引擎 连接器或回调脚本写入 work/inbox/
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common
import extract_impl
import channels
from channels import base as chan

MAX_TOTAL_MESSAGES = 4000
ACTIVE_PLATFORMS_FILE = common.CONFIG / "active_platforms.json"
HISTORY_PATH = common.WORK / "素材历史.jsonl"
HISTORY_MAX_LINES = 2000


def _append_history(messages: list[dict]) -> None:
    """把本轮抽到的消息追加进历史（按 source+msg_id 去重，只保留最近 2000 条）。

    只服务于"回显证明链路通"，不参与游标/去重判定，出错一律静默（不能因为
    写历史失败把整轮采集搞挂）。"""
    try:
        existing: dict[str, dict] = {}
        if HISTORY_PATH.exists():
            try:
                for line in HISTORY_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    key = f"{row.get('source','')}|{row.get('msg_id','')}"
                    existing[key] = row
            except OSError:
                existing = {}

        for m in messages:
            key = f"{m.get('source','')}|{m.get('msg_id','')}"
            existing[key] = {
                "source": m.get("source", ""),
                "chat_type": m.get("chat_type", ""),
                "chat_name": m.get("chat_name", ""),
                "sender": m.get("sender", ""),
                "timestamp": m.get("timestamp", ""),
                "msg_id": m.get("msg_id", ""),
                "content": str(m.get("content", ""))[:2000],
            }

        rows = sorted(existing.values(), key=lambda r: r.get("timestamp", ""))[-HISTORY_MAX_LINES:]
        common.WORK.mkdir(parents=True, exist_ok=True)
        tmp = HISTORY_PATH.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
        tmp.replace(HISTORY_PATH)
    except OSError:
        pass


def read_history(limit: int = 60) -> list[dict]:
    """读最近的历史素材（新→旧），供归档页在"本轮 0 条"时回显。"""
    if not HISTORY_PATH.exists():
        return []
    rows: list[dict] = []
    try:
        for line in HISTORY_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    rows.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return rows[:limit]


@dataclass
class AggregatedResult:
    level: int = 0
    status: str = "OK"
    count: int = 0
    stale_warning: bool = False
    message: str = ""
    exception: Path | None = None
    meta: dict = field(default_factory=dict)
    messages: list = field(default_factory=list)
    raw_count: int = 0
    dropped_count: int = 0
    deduped_count: int = 0
    max_ts: float = 0.0
    until: float = 0.0
    data_state: str = "ok"
    channel_stats: dict = field(default_factory=dict)
    cursor_blocked: bool = True

    @property
    def ok(self) -> bool:
        return self.level < 2


def _read_active_config() -> dict:
    if not ACTIVE_PLATFORMS_FILE.exists():
        return {}
    try:
        data = json.loads(ACTIVE_PLATFORMS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get_enabled_platforms() -> set[str]:
    """获取启用的平台列表；显式空数组表示用户未勾选，不擅自改回默认。"""
    data = _read_active_config()
    platforms = data.get("enabled_platforms")
    if isinstance(platforms, list):
        return {str(p) for p in platforms}
    if data:
        return set()
    return {"QQ", "WeCom"}


def run(hours: float | None = None, env_path: Path | None = None, ignore_cursor: bool = False) -> AggregatedResult:
    common.ensure_dirs()
    env = common.load_env(env_path)
    since, until, why = common.window(hours, overlap_sec=600, ignore_cursor=ignore_cursor)

    config = _read_active_config()
    config_mode = str(config.get("config_mode") or "").strip()
    enabled = get_enabled_platforms()
    if not config_mode or not enabled:
        message = "尚未选择配置模式" if not config_mode else "尚未勾选任何采集平台"
        meta = {
            "source": "MultiPlatform",
            "active_channels": [],
            "cutoff_time": datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M:%S"),
            "time_range": common.since_text(since, until),
            "status": "WAITING_CONFIG",
            "stale_warning": False,
            "message_count": 0,
            "data_state": "waiting_config",
            "raw_count": 0,
            "dropped_count": 0,
            "channel_stats": {},
            "usable_channels": [],
            "broken_channels": [],
            "not_ready_channels": [],
            "notes": [message + "，本轮未采集且游标保持不变"],
            "cursor_blocked": True,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        common.write_json(common.MATERIAL_PATH, {"meta": meta, "messages": []})
        return AggregatedResult(
            level=1,
            status="WAITING_CONFIG",
            message=message + "，请在可视化控制台完成配置",
            meta=meta,
            until=until,
            data_state="waiting_config",
            cursor_blocked=True,
        )

    all_messages: list[dict] = []
    channel_stats: dict[str, dict] = {}
    notes: list[str] = []
    stale_any = False
    max_global_ts = 0.0
    total_raw = 0
    total_dropped = 0

    # 1. QQ 提取 (核心基底)
    if "QQ" in enabled:
        try:
            qq_configured = bool(env.get("QQ_DBKEY") or env.get("QQ_DB_PATH"))
            if qq_configured:
                qq_res = extract_impl.run(hours=hours, env_path=env_path, ignore_cursor=ignore_cursor)
                channel_stats["QQ"] = {
                    "status": qq_res.status,
                    "data_state": qq_res.data_state,
                    "count": len(qq_res.messages),
                    "raw": qq_res.raw_count,
                    "dropped": qq_res.dropped_count,
                    "stale": qq_res.stale_warning,
                    "message": qq_res.message,
                }
                all_messages.extend(qq_res.messages)
                total_raw += qq_res.raw_count
                total_dropped += qq_res.dropped_count
                stale_any = stale_any or qq_res.stale_warning
                if qq_res.max_ts:
                    max_global_ts = max(max_global_ts, qq_res.max_ts)
                if qq_res.status in ("OK", "NO_NEW", "ONLY_NOISE"):
                    if not ignore_cursor:
                        common.advance_channel_cursor("QQ", qq_res.max_ts or until)
                if qq_res.meta.get("notes"):
                    notes.extend([f"[QQ] {n}" for n in qq_res.meta["notes"]])
            else:
                channel_stats["QQ"] = {"status": "NO_KEY", "count": 0, "message": "QQ 密钥未就绪，需跑 Admin_Init"}
        except Exception as exc:
            channel_stats["QQ"] = {"status": "FAIL", "count": 0, "message": f"QQ 提取异常: {exc}"}
            notes.append(f"[QQ] 提取失败: {exc}")
    else:
        channel_stats["QQ"] = {"status": "DISABLED", "count": 0, "message": "用户在配置界面未勾选 QQ"}

    # 2~5. 其余通道：统一走适配层
    #
    # 原生通道（WeChat / Feishu）由本程序自己拉；企微 / 钉钉由外部写入收件箱。
    # 每个适配器都返回同一套 ChannelResult，聚合层不需要知道平台差异；
    # 逐个 try，一个平台崩了不能拖垮全局（旧版正是靠"永远不抛异常"来掩盖问题）。
    for name in ("WeChat", "Feishu", "WeCom", "DingTalk"):
        cn = channels.CHANNEL_CN.get(name, name)
        if name not in enabled:
            channel_stats[name] = {"status": "DISABLED", "count": 0, "message": f"{cn}通道未勾选"}
            continue

        adapter = channels.NATIVE_ADAPTERS.get(name)
        try:
            if adapter is not None:
                res = adapter(env, since, until)
            else:
                res = chan.read_inbox(name, since, until)
        except Exception as exc:  # noqa: BLE001
            channel_stats[name] = {
                "status": "FAIL", "count": 0, "message": f"{cn}通道异常: {exc}",
            }
            notes.append(f"[{cn}] 适配器异常: {exc}")
            continue

        channel_stats[name] = {
            "status": res.status,
            "data_state": "ok" if res.messages else ("no_new" if res.ok else "unavailable"),
            "count": len(res.messages),
            "raw": res.raw_count,
            "dropped": res.dropped,
            "stale": res.status == chan.STALE,
            "message": res.message,
            "note": res.note,
            "latency_ms": res.latency_ms,
        }
        if res.messages:
            all_messages.extend(res.messages)
        total_raw += res.raw_count
        total_dropped += res.dropped
        stale_any = stale_any or res.status == chan.STALE
        if res.max_ts:
            max_global_ts = max(max_global_ts, res.max_ts)
        if res.status in chan.HEALTHY:
            if not ignore_cursor:
                common.advance_channel_cursor(name, res.max_ts or until)
        if res.status not in chan.HEALTHY:
            notes.append(f"[{cn}] {res.status}: {res.message}")

    all_messages, global_duplicates = chan.dedupe(all_messages)
    # 跨轮次持久化去重 (跨轮重复投递)。这里只"读"不"写"：
    # 真正提交推迟到 cursor_blocked 判定之后，见下方 commit_seen_msg_ids 处。
    before_seen = len(all_messages)
    all_messages, fresh_ids = common.split_seen_msg_ids(all_messages)
    # 记下"被跨轮去重掉多少条"：否则界面只能笼统说"都是表情/卡片"，
    # 而这轮的真实原因是"前几轮已经采过了"，会把人误导向"没读到消息"。
    cross_round_deduped = before_seen - len(all_messages)
    total_dropped += global_duplicates
    all_messages.sort(key=lambda m: m.get("timestamp", ""))
    truncated = False
    if len(all_messages) > MAX_TOTAL_MESSAGES:
        truncated = True
        notes.append(f"全渠道消息总数达 {len(all_messages)} 条，已截取最新 {MAX_TOTAL_MESSAGES} 条；游标保持")
        all_messages = all_messages[-MAX_TOTAL_MESSAGES:]

    # 历史留存：work/待办素材.json 每轮覆盖，用户看到的永远只有"本轮窗口"。
    # 窗口里恰好没消息时，归档页就一片空白，很容易被误读成"采集坏了"。
    # 这里把每条抽到的消息追加到 work/素材历史.jsonl（按 msg_id 去重），
    # 归档页在"本轮 0 条"时可以回显最近采到过的消息，用来证明链路是通的。
    if all_messages:
        _append_history(all_messages)

    # 各通道的统计字段补齐（QQ 的早退分支只给了 status/count/message，
    # 少了 raw/dropped 会让下游按 raw 判「有消息但全是噪音」时拿到 None）。
    for _k, _v in channel_stats.items():
        _v.setdefault("count", 0)
        _v.setdefault("raw", 0)
        _v.setdefault("dropped", 0)
        _v.setdefault("stale", False)

    # 状态判定：「绝不把没读到写成没有待办」
    #
    # 四组通道必须分开算：
    #   local      用户勾选、本程序应当采集的通道（DISABLED 不算）
    #   usable     本次真正采集成功，可以据以判定「没有新消息」
    #   broken     本该有却没有 / 尝试了但失败（NO_KEY、凭证错、网络错、解密失败）
    #   not_ready  平台侧根本还没接上（没装客户端、没有 vendor、收件箱从未写入）
    #
    # 为什么 broken 和 not_ready 要分开：
    #   都算「失败」的话，用户一旦勾了企微却没接连接器，就会每天收到红色告警，
    #   很快就麻木了 —— 告警疲劳比没有告警更危险。所以：
    #     broken    -> 红（真的坏了，要修）
    #     not_ready -> 黄（还没接，按接入指南配一次即可）
    #   但两者都不能被当成「没有新消息」，这一点不变。
    #
    # 旧代码只算了 all_fail = 所有 local 都是 FAIL。而 NO_KEY 既不是 FAIL
    # 也不是 DISABLED，于是「没填密钥」被漏出判定，最终报出 status=OK /
    # data_state=no_new —— run.py 会打印「QQ 在线，本时段确实没有新消息」，
    # 退出码 0，游标还照常推进。这是最危险的一类假阴性，必须堵死。
    NOT_TRIED = ("DISABLED",)
    BROKEN = ("FAIL", "NO_KEY", "NOT_CONFIGURED")
    NOT_READY = ("NOT_INSTALLED", "NOT_IMPLEMENTED")
    USABLE = ("OK", "STALE")

    # config/active_platforms.json 里可以声明 optional_platforms。
    # 语义：这个通道我确实想要，但**数据源还没接上**（比如企微要先授权连接器）。
    # 它未接入时不冻结游标、不把整轮打成 PARTIAL，但仍会出现在报告与清单末尾，
    # 不会静默消失。没声明的通道一律按原有严格规则处理 —— 勾了就必须有数据。
    optional = {str(x) for x in (config.get("optional_platforms") or [])}

    local_channels = [
        k for k, v in channel_stats.items() if v.get("status") not in NOT_TRIED
    ]
    usable_channels = [k for k in local_channels if channel_stats[k].get("status") in USABLE]
    broken_channels = [k for k in local_channels if channel_stats[k].get("status") in BROKEN]
    not_ready_channels = [k for k in local_channels if channel_stats[k].get("status") in NOT_READY]
    optional_missing = [k for k in not_ready_channels if k in optional]
    blocking_not_ready = [k for k in not_ready_channels if k not in optional]
    # 共享游标代表所有启用通道的完成水位：任一通道离线、失败、未接入或截断，
    # 都必须冻结，否则健康通道的 max_ts 会越过故障通道尚未交付的历史。
    cursor_blocked = (
        stale_any or truncated
        or bool(broken_channels or blocking_not_ready)
    )

    # 去重表提交闸门：只有「本轮结果可采信」才把 msg_id 记成已处理。
    # 否则（离线/失败/未接入/截断）下一轮用户修好后补采时，这些消息会被
    # 当成"前几轮已处理"而丢掉 —— 与 run.py 里"游标保持不变，修好后一次性补齐"
    # 的承诺正好相反，且丢得无声无息。
    if fresh_ids:
        if cursor_blocked:
            notes.append(
                f"跨轮去重表本轮未提交（保留 {len(fresh_ids)} 条待下轮补采）："
                f"本轮覆盖不完整，消息仍会被重新扫描"
            )
            common.log(f"      [去重] 本轮 cursor_blocked，暂不提交 {len(fresh_ids)} 条 msg_id")
        else:
            added = common.commit_seen_msg_ids(fresh_ids)
            if added:
                common.log(f"      [去重] 已提交 {added} 条 msg_id 到 work/seen_msg_ids.json")

    for k in broken_channels:
        st = channel_stats[k]
        notes.append(f"[{k}] 采集失败：{st.get('status')} {st.get('message', '')}".strip())
    for k in not_ready_channels:
        st = channel_stats[k]
        tag = "尚未接入（已声明为可选，不阻塞）" if k in optional else "尚未接入"
        notes.append(f"[{k}] {tag}：{st.get('message', '')}".strip())

    if usable_channels:
        # 只有「必须覆盖」的通道缺失才算 PARTIAL；已声明可选的通道缺失见 optional_missing
        overall_status = "PARTIAL" if (broken_channels or blocking_not_ready) else (
            "STALE" if stale_any else "OK"
        )
    elif broken_channels:
        overall_status = "FAIL"
    elif blocking_not_ready or local_channels:
        # 勾了通道但一个都还没接上：不是「没有新消息」，是「还没有数据源」
        overall_status = "NOT_READY"
    else:
        overall_status = "OK"

    if stale_any:
        data_state = "offline"
    elif overall_status in ("FAIL", "NOT_READY"):
        data_state = "unavailable"
    elif all_messages:
        data_state = "ok"
    else:
        data_state = "no_new" if total_raw == 0 else "only_noise"

    meta = {
        "source": "MultiPlatform",
        "active_channels": list(enabled),
        "optional_channels": sorted(optional),
        "cutoff_time": datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M:%S"),
        "time_range": common.since_text(since, until),
        "status": overall_status,
        "stale_warning": stale_any,
        "message_count": len(all_messages),
        "data_state": data_state,
        "raw_count": total_raw,
        "dropped_count": total_dropped,
        "channel_stats": channel_stats,
        "usable_channels": usable_channels,
        "broken_channels": broken_channels,
        "not_ready_channels": not_ready_channels,
        "optional_missing_channels": optional_missing,
        "cursor_blocked": cursor_blocked,
        "notes": notes,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    payload = {"meta": meta, "messages": all_messages}
    common.write_json(common.MATERIAL_PATH, payload)

    exception = None
    blocked = overall_status in ("FAIL", "NOT_READY")
    needs_attention = blocked or overall_status in ("PARTIAL", "STALE")
    if needs_attention:
        # 部分失败也必须留痕；不能被其他成功通道或下一次 QQ 成功静默清除。
        problem = broken_channels + blocking_not_ready
        detail_lines = [
            f"  - {k}: {channel_stats[k].get('status')} {channel_stats[k].get('message', '')}".rstrip()
            for k in problem
        ]
        if overall_status in ("PARTIAL", "STALE"):
            head_text = "部分通道离线、失败或覆盖不完整，本轮清单不完整，游标已冻结。\n"
        elif overall_status == "FAIL":
            head_text = "本次勾选的通道全部没能采到数据，清单为空**不代表没有待办**。\n"
        else:
            head_text = "勾选的通道都还没有接入成功，本轮没有任何数据源，清单为空**不代表没有待办**。\n"
        exception = common.write_exception(
            "异常说明-多平台采集失败.txt",
            head_text + "\n".join(detail_lines),
            f"通道状态: {json.dumps(channel_stats, ensure_ascii=False)}\n备注: {notes}",
        )
    else:
        # 仅清理本聚合器自己的旧告警，不动其他工具/通道的异常档案。
        # 顺带清掉"密钥未就绪/QQ 解密失败"这两份历史告警：QQ 本轮已经真采到数据
        # （usable=OK），再挂着它们只会让控制台永久显示 2 个红色报警，
        # 用户会一直以为"采集坏了"。这类文件是瞬时故障的证据，不是长期状态。
        stale_names = ["异常说明-多平台采集失败.txt"]
        if "QQ" in usable_channels:
            stale_names += [common.EXC_DECRYPT, common.EXC_NO_KEY]
        for name in stale_names:
            p = common.OUT / name
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass

    if overall_status == "FAIL":
        message = f"所有勾选通道均采集失败（{', '.join(broken_channels)}），本轮不产出清单"
    elif overall_status == "NOT_READY":
        message = f"勾选通道尚未接入（{', '.join(not_ready_channels)}），本轮没有数据源"
    elif overall_status == "PARTIAL":
        missed = broken_channels + blocking_not_ready
        message = f"多平台共汇总 {len(all_messages)} 条待办素材（未采到：{', '.join(missed)}）"
    elif optional_missing:
        message = (
            f"多平台共汇总 {len(all_messages)} 条待办素材"
            f"（可选通道未接入：{', '.join(optional_missing)}）"
        )
    else:
        message = f"多平台共汇总 {len(all_messages)} 条待办素材"

    return AggregatedResult(
        level=2 if blocked else (1 if cursor_blocked else 0),
        status=overall_status,
        count=len(all_messages),
        stale_warning=stale_any,
        message=message,
        meta=meta,
        messages=all_messages,
        raw_count=total_raw,
        dropped_count=total_dropped,
        deduped_count=cross_round_deduped,
        # 失败时不再兜成 until：那会让 run.py 认为「已处理到这里」而推进游标，
        # 失败的那整个时间段从此再也不会被扫到。0.0 表示本轮没有有效进展。
        max_ts=max_global_ts,
        until=until,
        data_state=data_state,
        channel_stats=channel_stats,
        cursor_blocked=cursor_blocked,
        exception=exception,
    )
