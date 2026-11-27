"""把消息写进收件箱（work/inbox/<channel>.jsonl）。

为什么需要这个脚本：
    企业微信的消息由 外部协作引擎 的 wecom 连接器拉取，钉钉需要「会话内容存档」
    或机器人回调 —— 这两件事都**不在本 Python 进程内**。本程序能做的，是
    定义一个明确的落地点：谁来取都行，取到就按约定格式写进收件箱，
    聚合器（channel_collector）就能像读原生通道一样读它。

    所以这个脚本是「全平台协同模式」能把企微/钉钉跑通的关键一环：
    外部协作引擎 定时任务在提示词里调用它，把连接器拿到的消息交进来。

用法：
    # 1) 从文件导入（JSON 数组、JSONL、或 {"messages":[...]} 都支持）
    python tools/inbox_push.py --channel WeCom --file wecom_msgs.json

    # 2) 从标准输入导入
    type wecom.json | python tools/inbox_push.py --channel WeCom

    # 3) 直接写一条（适合在提示词里拼）
    python tools/inbox_push.py --channel DingTalk --chat "项目群" --sender "张三" \
        --chat-id cid123 --text "明天上午十点评审，请带上方案" --msg-id dt-001

    # 4) 只看不写
    python tools/inbox_push.py --channel WeCom --file x.json --dry-run

退出码：0 成功 / 2 参数或数据有问题 / 3 写入失败
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
from channels import base  # noqa: E402

VALID_CHANNELS = ("WeCom", "DingTalk", "WeChat", "Feishu")


def load_rows(args) -> list[dict]:
    """从文件/stdin/命令行三种来源拿到原始记录列表。"""
    if args.text:
        return [
            {
                "chat_type": args.chat_type,
                "chat_id": args.chat_id,
                "chat_name": args.chat_name or args.chat_id or "未命名会话",
                "sender": args.sender,
                "msg_id": args.msg_id or f"manual-{int(time.time() * 1000)}",
                "content": args.text,
                "_ts": args.ts or time.time(),
            }
        ]

    if args.file:
        raw = Path(args.file).read_text(encoding="utf-8", errors="replace")
    else:
        raw = sys.stdin.read()
    text = raw.strip()
    if not text:
        raise SystemExit("没有输入内容")

    if text.startswith("["):
        data = json.loads(text)
        return [r for r in data if isinstance(r, dict)]
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            if isinstance(obj.get("messages"), list):
                return [r for r in obj["messages"] if isinstance(r, dict)]
            return [obj]
    # 退化成 JSONL
    rows = []
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if not line or line[0] not in "{[":
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def normalize(rows: list[dict], channel: str) -> tuple[list[dict], int]:
    """统一字段。缺 msg_id / 时间 / 正文的整条丢弃（没有溯源价值）。"""
    out: list[dict] = []
    bad = 0
    now = time.time()
    for r in rows:
        content = str(r.get("content") or r.get("text") or "").strip()
        ts = base.parse_ts(r)
        if ts is None:
            ts = now
        mid = str(r.get("msg_id") or r.get("id") or "").strip()
        if not content or not mid:
            bad += 1
            continue
        out.append(
            {
                "source": str(r.get("source") or channel),
                "chat_type": str(r.get("chat_type") or "unknown"),
                "chat_id": str(r.get("chat_id") or r.get("conversation_id") or ""),
                "chat_name": str(r.get("chat_name") or r.get("conversation") or "未命名会话"),
                "sender": str(r.get("sender") or r.get("from") or "未知发送人"),
                "msg_id": mid,
                "content": content,
                "_ts": ts,
            }
        )
    return out, bad


def existing_ids(channel: str) -> set[str]:
    """读已有收件箱的 msg_id，避免定时任务每轮重复追加同一条。"""
    path = base.inbox_path(channel)
    ids: set[str] = set()
    if not path.exists():
        return ids
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip().rstrip(",")
            if not line.startswith("{"):
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            mid = str(item.get("msg_id") or "").strip()
            if mid:
                ids.add(mid)
    except OSError:
        pass
    return ids


def main() -> int:
    ap = argparse.ArgumentParser(description="把消息写入 work/inbox/<channel>.jsonl")
    ap.add_argument("--channel", required=True, choices=VALID_CHANNELS)
    ap.add_argument("--file", help="JSON/JSONL 文件；不给则读 stdin")
    ap.add_argument("--text", help="直接写一条消息的正文")
    ap.add_argument("--chat", dest="chat_name", help="会话名（群名/对方名）")
    ap.add_argument("--chat-id", default="", help="会话 id")
    ap.add_argument("--chat-type", default="group", choices=("group", "p2p", "c2p"), help="会话类型")
    ap.add_argument("--sender", default="未知发送人", help="发送人")
    ap.add_argument("--msg-id", default="", help="消息唯一 id（不填则按时间生成）")
    ap.add_argument("--ts", type=float, help="消息时间（epoch 秒，不填则用当前时间）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    ap.add_argument("--no-dedupe", action="store_true", help="不做 msg_id 去重")
    args = ap.parse_args()

    try:
        raw_rows = load_rows(args)
    except (OSError, json.JSONDecodeError) as exc:
        common.log(f"[失败] 读取输入失败: {exc}")
        return 2

    rows, bad = normalize(raw_rows, args.channel)
    if not rows:
        common.log(f"[失败] 没有可用记录（{len(raw_rows)} 条里 {bad} 条缺 msg_id 或正文）")
        return 2

    skipped = 0
    if not args.no_dedupe:
        known = existing_ids(args.channel)
        deduped = [r for r in rows if r["msg_id"] not in known]
        skipped = len(rows) - len(deduped)
        rows = deduped

    if args.dry_run:
        common.head("收件箱写入预览（未落盘）")
        common.log(f"通道: {args.channel}   路径: {base.inbox_path(args.channel)}")
        common.log(f"待写入 {len(rows)} 条，已存在跳过 {skipped} 条，丢弃 {bad} 条")
        for r in rows[:5]:
            common.log(f"  - [{r['chat_name']}] {r['sender']}: {r['content'][:40]}")
        return 0

    if not rows:
        common.log(f"[OK] 没有新消息（已存在 {skipped} 条，重复不重复写入）")
        return 0

    try:
        written = base.append_inbox(args.channel, rows)
    except OSError as exc:
        common.log(f"[失败] 写入收件箱失败: {exc}")
        return 3

    common.log(
        f"[OK] {args.channel} 收件箱 +{written} 条"
        + (f"，跳过重复 {skipped} 条" if skipped else "")
        + (f"，丢弃无效 {bad} 条" if bad else "")
        + f"  ->  {base.inbox_path(args.channel)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
