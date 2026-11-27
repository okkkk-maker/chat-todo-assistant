"""飞书（Feishu / Lark）通道：自建应用 + Open API 主动拉取。

为什么是「主动拉」而不是「WebSocket 长连接」：
    原设计里的 cow_listener 号称走长连接，但没有任何代码建立连接（append_message
    全仓库无调用方），等于一个永远空的缓冲。本程序的使用模型本来就是
    「定时增量拉取」（run.py 跑一轮就退出），长连接需要一个常驻进程 + 断线重连
    + 事件去重，复杂度高且没有必要。Open API 的 im/v1/messages 已经能按
    时间窗增量取消息，语义正好匹配。

需要的权限（飞书开放平台 → 自建应用 → 权限管理）：
    im:message:readonly      读取单聊/群聊消息（必需）
    im:chat:readonly         读取会话列表与群信息（必需）
    并把应用**加入需要读取的群**（未加入的群读不到，这是平台限制，不是 bug）

环境变量（.env）：
    FEISHU_APP_ID / FEISHU_APP_SECRET   自建应用凭据
    FEISHU_BASE_URL                     可选，默认 https://open.feishu.cn
    FEISHU_MAX_CHATS                    可选，单轮最多遍历多少个会话（默认 60）
    FEISHU_SENDER_MAP                   可选，JSON，如 {"ou_xxx":"张老师"}，用于把
                                        open_id 显示成人名（无通讯录权限时很有用）
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import common

from . import base

CHANNEL = "Feishu"
DEFAULT_BASE = "https://open.feishu.cn"
MAX_CHATS_DEFAULT = 60
PAGE_SIZE = 50          # 飞书 im/v1/messages 单页上限就是 50
MAX_PAGES_PER_CHAT = 4  # 单个会话最多翻 4 页（200 条），超出记告警
WORKERS = 4             # 并发拉取会话数，兼顾速度与频控


def _env(env: dict, key: str, default: str = "") -> str:
    return (env.get(key) or default).strip()


def _host(env: dict) -> str:
    url = _env(env, "FEISHU_BASE_URL", DEFAULT_BASE).rstrip("/")
    ok_url, url_err = common.validate_outbound_url(url, require_https=True)
    if not ok_url:
        raise base.ApiError(f"FEISHU_BASE_URL 不合法: {url_err}")
    return url


def tenant_token(env: dict) -> str:
    """换 tenant_access_token（自建应用）。有效期约 2 小时，短进程每次现取。"""
    data = base.http_json(
        f"{_host(env)}/open-apis/auth/v3/tenant_access_token/internal",
        method="POST",
        payload={
            "app_id": _env(env, "FEISHU_APP_ID"),
            "app_secret": _env(env, "FEISHU_APP_SECRET"),
        },
    )
    if data.get("code") not in (0, None):
        raise base.ApiError(f"取 token 失败 code={data.get('code')} msg={data.get('msg')}")
    token = data.get("tenant_access_token") or ""
    if not token:
        raise base.ApiError("取 token 失败：返回里没有 tenant_access_token")
    return token


def list_chats(env: dict, token: str, limit: int) -> list[dict]:
    """列应用可见的会话。分页直到取满 limit 或没有 next_page_token。"""
    chats: list[dict] = []
    page_token = ""
    while len(chats) < limit:
        url = f"{_host(env)}/open-apis/im/v1/chats?" + base.qs(
            {"page_size": min(100, limit - len(chats)), "page_token": page_token}
        )
        data = base.http_json(url, headers={"Authorization": f"Bearer {token}"})
        if data.get("code") not in (0, None):
            raise base.ApiError(f"列会话失败 code={data.get('code')} msg={data.get('msg')}")
        items = ((data.get("data") or {}).get("items")) or []
        chats.extend(items)
        page_token = ((data.get("data") or {}).get("page_token")) or ""
        if not page_token or not items:
            break
    return chats[:limit]


def list_messages(env: dict, token: str, chat_id: str, since: float, until: float) -> tuple[list[dict], int, bool]:
    """拉一个会话的消息，返回 (items, 原始条数, 是否完整覆盖窗口)。"""
    items: list[dict] = []
    raw = 0
    page_token = ""
    complete = True
    for _ in range(MAX_PAGES_PER_CHAT):
        url = f"{_host(env)}/open-apis/im/v1/messages?" + base.qs(
            {
                "container_id_type": "chat",
                "container_id": chat_id,
                "start_time": int(since),
                "end_time": int(until),
                "sort_type": "ByCreateTimeAsc",
                "page_size": PAGE_SIZE,
                "page_token": page_token,
            }
        )
        data = base.http_json(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=base.DEFAULT_TIMEOUT,
        )
        if data.get("code") not in (0, None):
            raise base.ApiError(f"拉消息失败 code={data.get('code')} msg={data.get('msg')}")
        page = ((data.get("data") or {}).get("items")) or []
        raw += len(page)
        items.extend(page)
        page_token = ((data.get("data") or {}).get("page_token")) or ""
        # 只以 next token 判结束；短页仍可能有下一页。
        if not page_token:
            break
    else:
        complete = False
    return items, raw, complete


def _extract_text(item: dict) -> str:
    """从飞书消息体里取纯文本。非文本类统一转成 [类型] 占位，绝不吐 JSON 原文。"""
    msg_type = (item.get("msg_type") or "").lower()
    body = item.get("body") or {}
    content = body.get("content") or ""
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = None
    else:
        parsed = content

    if msg_type == "text":
        if isinstance(parsed, dict):
            text = str(parsed.get("text") or "").strip()
            return "" if text in ("{}", "[]") else text
        text = str(content).strip()
        return "" if text in ("{}", "[]") else text
    if msg_type == "post":
        # 富文本：把所有 text 段落拼起来
        if isinstance(parsed, dict):
            chunks: list[str] = []

            def walk(node):
                if isinstance(node, dict):
                    if isinstance(node.get("text"), str):
                        chunks.append(node["text"])
                    for v in node.values():
                        walk(v)
                elif isinstance(node, list):
                    for v in node:
                        walk(v)

            walk(parsed)
            joined = " ".join(c.strip() for c in chunks if c.strip())
            if joined:
                return joined
        return "[富文本]"
    # 非文本类一律返回空串 -> 由上层计为 dropped。
    # 不返回 "[image]" 这种占位：它会一路流进待办素材，变成一条没有内容的假消息。
    return ""


def _sender_name(item: dict, name_map: dict) -> str:
    sender = item.get("sender") or {}
    if (sender.get("sender_type") or "") == "app":
        return "机器人"
    sid = str(sender.get("id") or "").strip()
    if not sid:
        return "未知发送人"
    if sid in name_map:
        return str(name_map[sid])
    # 没有通讯录权限时只能显示短标识，至少比一串 open_id 可读
    return f"飞书用户·{sid[-6:]}" if len(sid) > 6 else f"飞书用户·{sid}"


def fetch(env: dict, since: float, until: float) -> base.ChannelResult:
    """通道入口。任何异常都必须在这里收敛成 ChannelResult，绝不向上抛。"""
    started = time.time()
    app_id = _env(env, "FEISHU_APP_ID")
    app_secret = _env(env, "FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        missing = "FEISHU_APP_ID" if not app_id else "FEISHU_APP_SECRET"
        return base.ChannelResult(
            channel=CHANNEL,
            status=base.NOT_CONFIGURED,
            message=f"飞书通道已勾选，但 .env 缺少 {missing}，本轮未采集",
        )

    try:
        max_chats = int(_env(env, "FEISHU_MAX_CHATS", str(MAX_CHATS_DEFAULT)))
    except ValueError:
        max_chats = MAX_CHATS_DEFAULT
    name_map: dict = {}
    raw_map = _env(env, "FEISHU_SENDER_MAP")
    if raw_map:
        try:
            parsed = json.loads(raw_map)
            if isinstance(parsed, dict):
                name_map = parsed
        except json.JSONDecodeError:
            pass

    try:
        token = tenant_token(env)
        chats = list_chats(env, token, max_chats)
    except base.ApiError as exc:
        return base.ChannelResult(
            channel=CHANNEL,
            status=base.FAIL,
            message=f"飞书接口调用失败: {exc}",
            note="检查 App ID/Secret、应用是否已开启「机器人」能力、网络是否可达 open.feishu.cn",
            latency_ms=int((time.time() - started) * 1000),
        )
    except Exception as exc:  # noqa: BLE001
        return base.ChannelResult(
            channel=CHANNEL,
            status=base.FAIL,
            message=f"飞书通道异常: {exc}",
            latency_ms=int((time.time() - started) * 1000),
        )

    if not chats:
        return base.ChannelResult(
            channel=CHANNEL,
            status=base.NOT_IMPLEMENTED,
            message=(
                "飞书应用可见会话数为 0：应用没有被加入任何群，或未申请 im:chat:readonly。"
                "把应用加进群后重试（这是平台侧限制，不是程序 bug）"
            ),
            latency_ms=int((time.time() - started) * 1000),
        )

    chat_names = {str(c.get("chat_id") or ""): str(c.get("name") or "") for c in chats}
    messages: list[dict] = []
    raw_total = 0
    dropped = 0
    max_ts = 0.0
    failed_chats: list[str] = []
    incomplete_chats: list[str] = []

    def work(chat: dict) -> tuple[str, list[dict], int, bool]:
        cid = str(chat.get("chat_id") or "")
        items, raw, complete = list_messages(env, token, cid, since, until)
        return cid, items, raw, complete

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(work, c): c for c in chats}
        for fut in as_completed(futures):
            chat = futures[fut]
            cid = str(chat.get("chat_id") or "")
            try:
                _, items, raw, complete = fut.result()
            except Exception as exc:  # noqa: BLE001
                failed_chats.append(f"{chat_names.get(cid) or cid}({exc})")
                continue
            if not complete:
                incomplete_chats.append(chat_names.get(cid) or cid)
            raw_total += raw
            chat_type = "p2p" if (chat.get("chat_mode") or "") == "p2p" else "group"
            cname = chat_names.get(cid) or ("飞书单聊" if chat_type == "p2p" else f"飞书群·{cid[-6:]}")
            for item in items:
                if item.get("deleted"):
                    dropped += 1
                    continue
                ts = _ms_to_sec(item.get("create_time"))
                if ts is None or ts < since or ts > until + 60:
                    continue
                text = _extract_text(item)
                if not text:
                    dropped += 1
                    continue
                mid = str(item.get("message_id") or "")
                if not mid:
                    dropped += 1
                    continue
                messages.append(
                    base.make_message(
                        source=CHANNEL,
                        chat_type=chat_type,
                        chat_id=cid,
                        chat_name=cname,
                        sender=_sender_name(item, name_map),
                        ts=ts,
                        msg_id=mid,
                        content=text,
                    )
                )
                max_ts = max(max_ts, ts)

    messages, dup = base.dedupe(messages)
    dropped += dup
    messages.sort(key=lambda m: base.parse_ts(m) or 0)
    capped = len(messages) > base.MAX_MESSAGES_PER_CHANNEL
    if capped:
        dropped += len(messages) - base.MAX_MESSAGES_PER_CHANNEL
        messages = messages[-base.MAX_MESSAGES_PER_CHANNEL:]

    notes = [f"遍历 {len(chats)} 个会话，原始 {raw_total} 条"]
    if failed_chats:
        notes.append(f"{len(failed_chats)} 个会话拉取失败: {', '.join(failed_chats[:3])}")
    if incomplete_chats:
        notes.append(f"{len(incomplete_chats)} 个会话达到分页上限，覆盖不完整: {', '.join(incomplete_chats[:3])}")
    if capped:
        notes.append("消息数达到通道上限，覆盖不完整")
    if len(chats) >= max_chats:
        notes.append(f"会话数达到上限 {max_chats}，可能还有群未覆盖（调大 FEISHU_MAX_CHATS）")

    # 部分失败/截断可交付已读消息，但必须标 STALE 以冻结共享游标。
    if failed_chats and len(failed_chats) == len(chats):
        status = base.FAIL
    elif failed_chats or incomplete_chats or capped or len(chats) >= max_chats:
        status = base.STALE
    else:
        status = base.OK
    return base.ChannelResult(
        channel=CHANNEL,
        status=status,
        messages=messages,
        raw_count=raw_total,
        dropped=dropped,
        max_ts=max_ts,
        message=f"飞书共采集 {len(messages)} 条（{len(chats)} 个会话）",
        note="；".join(notes),
        latency_ms=int((time.time() - started) * 1000),
    )


def _ms_to_sec(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v > 1e11:      # 飞书 create_time 是毫秒字符串
        v /= 1000.0
    if v < 1e8 or v > 4e9:
        return None
    return v
