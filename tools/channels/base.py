"""多平台通道适配层的统一契约。

设计目标（对应手册 「不臆造、不静默」）：
    1. 每个通道自己声明结果状态，**不允许**用「0 条」冒充「正常」。
    2. 通道失败绝不能影响其他通道；一个挂了，其余照常产出。
    3. 所有通道产出同一种消息结构，聚合层不需要知道平台差异。
    4. 纯标准库发 HTTP（urllib），不引入 requests。

状态语义（聚合层据此判定整体是否可信）：
    OK               采集成功，有数据或确有 0 条（可判「没有新消息」）
    STALE            采集成功，但数据源本身陈旧（如微信库未更新）
    FAIL             尝试了但失败（网络/解密/解析错误）
    NOT_CONFIGURED   用户勾选了但没填凭证
    NOT_INSTALLED    本机缺少运行前提（没装客户端 / vendor 未随包）
    NOT_IMPLEMENTED  平台侧没有可用接口或权限不足，本程序无法读取
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import common

# ---- 状态常量 ----
OK = "OK"
STALE = "STALE"
FAIL = "FAIL"
NOT_CONFIGURED = "NOT_CONFIGURED"
NOT_INSTALLED = "NOT_INSTALLED"
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"

# 采集成功（可以据以判断「确实没有新消息」）的状态
HEALTHY = (OK, STALE)
# 需要向用户明确告警、但不一定算整体失败的状态
PROBLEM = (FAIL, NOT_CONFIGURED, NOT_INSTALLED, NOT_IMPLEMENTED)

# 单条消息正文上限。待办正文不可能比这更长，超长一律截断，
# 避免一个超大转发把 work/待办素材.json 撑到几十 MB。
MAX_CONTENT_CHARS = 1200
# 单通道单轮最多返回条数（防一次拉爆内存/上下文）
MAX_MESSAGES_PER_CHANNEL = 800

DEFAULT_TIMEOUT = 12
DEFAULT_RETRIES = 1


@dataclass
class ChannelResult:
    """一个通道一轮采集的结果。字段名与 extract_impl.Result 保持一致的语义。"""

    channel: str
    status: str = FAIL
    messages: list = field(default_factory=list)
    raw_count: int = 0          # 窗口内原始条数（过滤前）
    dropped: int = 0            # 被当作噪音/重复筛掉的
    max_ts: float = 0.0         # 本通道读到的最新消息时间（推进游标用）
    message: str = ""           # 给人看的一句话
    note: str = ""              # 给日志/排障看的补充
    latency_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status in HEALTHY


def make_message(
    source: str,
    chat_type: str,
    chat_id: str,
    chat_name: str,
    sender: str,
    ts: float,
    msg_id: str,
    content: str,
) -> dict:
    """所有通道统一的消息结构（与 QQ 侧 extract_impl 输出逐字段对齐）。"""
    text = (content or "").strip()
    if len(text) > MAX_CONTENT_CHARS:
        text = text[:MAX_CONTENT_CHARS] + "…"
    return {
        "source": source,
        "chat_type": chat_type,
        "chat_id": str(chat_id or "").strip(),
        "chat_name": str(chat_name or "").strip() or "未命名会话",
        "sender": str(sender or "").strip() or "未知发送人",
        "timestamp": common.fmt(ts),
        "msg_id": str(msg_id or "").strip(),
        "content": text,
    }


def dedupe(messages: list[dict], seen: set[str] | None = None) -> tuple[list[dict], int]:
    """按 (source, msg_id) 去重。同一条消息跨页/跨轮重复拉取时必须只留一条。"""
    seen = set() if seen is None else seen
    out: list[dict] = []
    dropped = 0
    for m in messages:
        key = f"{m.get('source', '')}:{m.get('msg_id', '')}"
        if not m.get("msg_id") or key in seen:
            dropped += 1
            continue
        seen.add(key)
        out.append(m)
    return out, dropped


# HTTP：纯标准库，统一超时/错误归一化
class ApiError(RuntimeError):
    """平台接口返回了业务错误（HTTP 200 但 code != 0 也算）。"""


def http_json(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    payload: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
) -> dict:
    """发一个 JSON 请求并解析返回。

    重试只针对「明确没送达」的错误（连接失败/超时/5xx），
    避免像 push_wecom 那样在响应丢失时重复发送造成副作用。
    """
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)

    # 本机 / 内网地址绕开系统代理：企业环境里 http_proxy 常常是全局设置的，
    # 会让 http://127.0.0.1 的请求被代理吞掉（返回 502），本地联调时非常难排查。
    host = urllib.parse.urlparse(url).hostname or ""
    if host in ("127.0.0.1", "localhost", "::1"):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    else:
        opener = urllib.request.build_opener()

    last = ""
    for attempt in range(1, retries + 2):
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                raise ApiError(f"返回不是 JSON（前 120 字符）: {raw[:120]}") from None
            if not isinstance(data, dict):
                raise ApiError("返回结构不是 JSON 对象")
            return data
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001
                detail = ""
            finally:
                try:
                    exc.close()
                except Exception:  # noqa: BLE001
                    pass
            # 4xx 是请求本身的问题，重试没用；429(限频) 或 5xx 才重试
            if (exc.code < 500 and exc.code != 429) or attempt > retries:
                raise ApiError(f"HTTP {exc.code}: {detail or exc.reason}") from None
            last = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = f"网络错误: {getattr(exc, 'reason', exc)}"
            if attempt > retries:
                raise ApiError(last) from None
        time.sleep(min(2.0 * attempt, 5.0))
    raise ApiError(last or "未知网络错误")


def qs(params: dict) -> str:
    """拼查询串。值统一转字符串，None 直接丢弃（平台对空参数很敏感）。"""
    clean = {k: str(v) for k, v in params.items() if v is not None and v != ""}
    return urllib.parse.urlencode(clean)


# 收件箱：给「程序无法直接读取」的平台留的统一入口
def inbox_dir() -> Path:
    return common.WORK / "inbox"


def inbox_path(channel: str) -> Path:
    return inbox_dir() / f"{channel.lower()}.jsonl"


def read_inbox(channel: str, since: float, until: float, skew: float = 60.0) -> ChannelResult:
    """读 work/inbox/<channel>.jsonl。

    为什么需要它：
        企业微信的消息由 外部协作引擎 的 wecom 连接器负责拉取，钉钉的消息需要
        「会话内容存档」或机器人回调 —— 这两种能力都**不在本 Python 进程内**。
        与其假装能读（就是之前 NOT_IMPLEMENTED 的根因），不如定义一个落地点：
        任何一方（连接器/回调脚本/手工导入）把消息按约定格式写进这个文件，
        聚合器就能像读原生通道一样读它。

    支持两种格式（按首字符判断，不猜）：
        - JSONL：一行一条消息
        - JSON 数组 / 单个对象：整体一个 json.document
    """
    path = inbox_path(channel)
    if not path.exists():
        return ChannelResult(
            channel=channel,
            status=NOT_INSTALLED,
            message=(
                f"没有收到任何{channel}消息：{path} 不存在。"
                "该平台需要由 WorkBuddy 定时任务（连接器）或回调脚本先写入收件箱"
            ),
            note=f"inbox 缺失: {path}",
        )

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ChannelResult(channel=channel, status=FAIL, message=f"收件箱读取失败: {exc}")

    rows: list[dict] = []
    stripped = text.lstrip()
    if stripped.startswith("["):
        try:
            data = json.loads(stripped)
            rows = [r for r in data if isinstance(r, dict)]
        except json.JSONDecodeError as exc:
            return ChannelResult(channel=channel, status=FAIL, message=f"收件箱不是合法 JSON 数组: {exc}")
    elif stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
            rows = obj.get("messages") if isinstance(obj.get("messages"), list) else [obj]
        except json.JSONDecodeError:
            # 退化成逐行解析（有些写手会混着写）
            rows = []
    invalid_records = 0
    if not rows:
        for line in text.splitlines():
            line = line.strip().rstrip(",")
            if not line or line[0] not in "{[":
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                # 坏行可能是并发写入的半行，也可能是文件损坏；绝不能当成无消息。
                invalid_records += 1
                continue
            if isinstance(item, dict):
                rows.append(item)
            else:
                invalid_records += 1

    valid_rows: list[dict] = []
    for row in rows:
        if isinstance(row, dict):
            valid_rows.append(row)
        else:
            invalid_records += 1
    rows = valid_rows

    kept: list[dict] = []
    raw = 0
    dropped = 0
    max_ts = 0.0
    for r in rows:
        ts = _row_ts(r)
        if ts is None:
            invalid_records += 1
            dropped += 1
            continue
        if ts < since or ts > until + skew:
            continue
        raw += 1
        if not str(r.get("msg_id") or "").strip() or not str(r.get("chat_id") or "").strip():
            invalid_records += 1
            dropped += 1
            continue
        content = str(r.get("content") or "").strip()
        if not content:
            dropped += 1
            continue
        kept.append(
            make_message(
                source=channel,
                chat_type=str(r.get("chat_type") or "unknown"),
                chat_id=str(r.get("chat_id") or ""),
                chat_name=str(r.get("chat_name") or ""),
                sender=str(r.get("sender") or ""),
                ts=ts,
                msg_id=str(r.get("msg_id") or ""),
                content=content,
            )
        )
        max_ts = max(max_ts, ts)

    kept, dup = dedupe(kept)
    if invalid_records:
        status = STALE if (kept or raw or rows) else FAIL
    else:
        # 合法记录只是空正文/噪音时是成功采集，不应误报为通道离线。
        status = OK
    return ChannelResult(
        channel=channel,
        status=status,
        messages=kept,
        raw_count=raw,
        dropped=dropped + dup,
        max_ts=max_ts,
        message=f"{channel} 收件箱共 {len(kept)} 条（原文 {len(rows)} 条，异常 {invalid_records} 条）",
        note=f"inbox={path}",
    )


def _row_ts(row: dict) -> float | None:
    """解析一条收件箱记录的时间。支持 epoch 秒、毫秒、以及 'YYYY-MM-DD HH:MM:SS'。"""
    for key in ("_ts", "ts", "timestamp", "create_time", "time"):
        val = row.get(key)
        if val is None or val == "":
            continue
        if isinstance(val, (int, float)):
            v = float(val)
            return v / 1000.0 if v > 1e11 else v
        text = str(val).strip()
        try:
            v = float(text)
            return v / 1000.0 if v > 1e11 else v
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
            try:
                return time.mktime(time.strptime(text, fmt))
            except ValueError:
                continue
    return None


def parse_ts(row: dict) -> float | None:
    """解析一条外部记录的绝对时间。公开版本，供 inbox_push 复用。"""
    return _row_ts(row)


def append_inbox(channel: str, rows: list[dict]) -> int:
    """供 inbox_push.py 使用：追加写入，返回写入条数。"""
    path = inbox_path(channel)
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            if not isinstance(r, dict):
                continue
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            written += 1
        f.flush()
    return written


__all__ = [
    "OK", "STALE", "FAIL", "NOT_CONFIGURED", "NOT_INSTALLED", "NOT_IMPLEMENTED",
    "HEALTHY", "PROBLEM", "MAX_CONTENT_CHARS", "MAX_MESSAGES_PER_CHANNEL",
    "ChannelResult", "ApiError", "make_message", "dedupe", "http_json", "qs",
    "inbox_dir", "inbox_path", "read_inbox", "append_inbox", "parse_ts",
]
