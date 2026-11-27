"""基于 CowAgent（chatgpt-on-wechat）多通道架构的流式消息监听与暂存服务。

功能定位：
    针对飞书（WebSocket 长连接）、钉钉（Stream 模式）、企业微信（智能机器人/自建应用）
    等无需公网 IP 的长连接平台，提供消息捕获与本地暂存。
    捕获到的消息以原子追加方式写入 work/channel_messages.jsonl，供 channel_collector.py 统一汇总。
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common
import msgparse

BUFFER_FILE = common.WORK / "channel_messages.jsonl"


def append_message(
    source: str,
    chat_type: str,
    chat_id: str,
    chat_name: str,
    sender: str,
    content: str,
    msg_id: str,
    ts: float | None = None
) -> bool:
    clean_text = msgparse.polish(content)
    if not clean_text or msgparse.is_noise(clean_text):
        return False

    ts = ts or time.time()
    record = {
        "source": source,
        "chat_type": chat_type,
        "chat_id": str(chat_id).strip(),
        "chat_name": str(chat_name).strip() or "未命名会话",
        "sender": str(sender).strip() or "未知发送人",
        "timestamp": common.fmt(ts),
        "msg_id": str(msg_id).strip(),
        "content": clean_text,
        "_ts": ts,
    }

    try:
        BUFFER_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(BUFFER_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except OSError as exc:
        common.log(f"  [写入缓冲失败] {exc}")
        return False


def read_buffered_messages(source: str, since: float, until: float) -> tuple[list[dict], int, int]:
    if not BUFFER_FILE.exists():
        return [], 0, 0

    messages: list[dict] = []
    seen = set()
    raw_count = 0
    dropped = 0

    try:
        with open(BUFFER_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except Exception:
                    continue

                if m.get("source", "").lower() != source.lower():
                    continue

                ts = m.get("_ts") or 0.0
                if ts < since or ts > until + 60:
                    continue

                raw_count += 1
                mid = m.get("msg_id", "")
                if mid in seen:
                    dropped += 1
                    continue
                seen.add(mid)

                item = dict(m)
                item.pop("_ts", None)
                messages.append(item)
    except OSError:
        pass

    messages.sort(key=lambda x: x.get("timestamp", ""))
    return messages, raw_count, dropped
