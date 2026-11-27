"""把 Markdown 摘要推送到企业微信群机器人（外部协作引擎 MCP 的双保险兜底）。

用法:
    python tools/push_wecom.py --text "### 今日待办\\n- ..."
    python tools/push_wecom.py --file output/todo-20261028-1930.md
    python tools/push_wecom.py --file x.md --dry-run     # 只打印载荷，不联网
    python tools/push_wecom.py --text "..." --url https://qyapi.weixin.qq.com/...  # 临时指定

退出码: 0 成功 / 2 没配 Webhook / 3 推送失败
只用标准库 urllib，不装第三方 requests。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402

MAX_BYTES = {"markdown": 4000, "text": 2000}  # 企微硬限制 4096 / 2048，留余量
TIMEOUT = 15
RETRY = 2


def _truncate_bytes(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    cut = raw[:limit]
    while cut:
        try:
            return cut.decode("utf-8").rstrip() + "\n…（已截断）"
        except UnicodeDecodeError:
            cut = cut[:-1]
    return ""


def _truncate_markdown(text: str, limit: int) -> str:
    """按「条目」维度截断 Markdown，而不是硬切字节。

    硬切 UTF-8 字节有两个副作用：一是可能把一行待办切成半句；二是可能切在
    ``` 代码块中间，企微渲染出来整张卡片都变成代码样式（"排版爆屏"）。
    所以这里按行累加，尽量在 `- ` 条目边界收尾，并在尾部补上未闭合的代码块标记。
    """
    if len(text.encode("utf-8")) <= limit:
        return text

    reserve = 40            # 给「…（已截断）」和可能的 ``` 留位置
    keep: list[str] = []
    size = 0
    for line in text.splitlines():
        add = len(line.encode("utf-8")) + 1     # +1 是换行符
        if size + add > limit - reserve:
            break
        keep.append(line)
        size += add
    if not keep:
        return _truncate_bytes(text, limit)

    # 尽量停在完整条目上（" - " 开头），别把最后一条砍成半句
    while len(keep) > 1 and not keep[-1].startswith("- "):
        keep.pop()

    out = "\n".join(keep).rstrip() + "\n…（已截断）"
    if out.count("```") % 2 == 1:       # 代码块没闭合，补上围栏
        out += "\n```"
    if len(out.encode("utf-8")) > limit:
        return _truncate_bytes(out, limit)
    return out


def build_payload(content: str, msgtype: str = "markdown") -> dict:
    limit = MAX_BYTES.get(msgtype, 4000)
    if msgtype == "markdown":
        content = _truncate_markdown(content, limit)
    else:
        content = _truncate_bytes(content, limit)
    if msgtype == "text":
        return {"msgtype": "text", "text": {"content": content}}
    return {"msgtype": "markdown", "markdown": {"content": content}}


def push(content: str, url: str | None = None, msgtype: str = "markdown", quiet: bool = False) -> tuple[bool, str]:
    """推送一段内容。返回 (成功, 说明)。不抛异常，失败一定给原因。"""
    url = (url or common.load_env().get("WECOM_WEBHOOK_URL", "")).strip()
    if not url:
        return False, "未配置 WECOM_WEBHOOK_URL（在 .env 里填企业微信群机器人地址）"
    ok_url, url_err = common.validate_outbound_url(url, require_https=True)
    if not ok_url:
        return False, f"Webhook 地址被安全策略拒绝: {url_err}"

    body = json.dumps(build_payload(content, msgtype), ensure_ascii=False).encode("utf-8")
    last_err = ""
    for attempt in range(1, RETRY + 1):
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return False, f"返回不是 JSON: {text[:120]}"
            code = data.get("errcode")
            if code == 0:
                if not quiet:
                    common.log("  [OK] 企业微信推送成功")
                return True, "推送成功"
            # 45009 = 超过频率限制，可重试
            last_err = f"errcode={code} errmsg={data.get('errmsg', '')}"
            if code != 45009:
                hint = ""
                if code in (93000, 40058):
                    hint = "（Webhook 地址或 key 不对，或被删除了）"
                elif code == 45009:
                    hint = "（发送太频繁，每分钟限 20 条）"
                return False, last_err + hint
        except urllib.error.HTTPError as exc:
            last_err = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_err = f"网络错误: {getattr(exc, 'reason', exc)}"
        if attempt < RETRY:
            time.sleep(2 * attempt)
    return False, f"重试 {RETRY} 次仍失败: {last_err}"


def main() -> int:
    ap = argparse.ArgumentParser(description="推送消息到企业微信群机器人")
    ap.add_argument("--text", help="直接给 Markdown 文本")
    ap.add_argument("--file", help="从文件读 Markdown")
    ap.add_argument("--url", help="临时指定 Webhook（默认读 .env）")
    ap.add_argument("--msgtype", choices=("markdown", "text"), default="markdown")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要发送的内容")
    args = ap.parse_args()

    if args.file:
        p = Path(args.file)
        if not p.exists():
            common.log(f"[失败] 文件不存在: {p}")
            return 3
        content = p.read_text(encoding="utf-8", errors="replace")
    elif args.text:
        content = args.text
    else:
        content = sys.stdin.read()

    content = content.strip()
    if not content:
        common.log("[失败] 内容为空，拒绝发送（避免把空清单推给人）")
        return 3

    if args.dry_run:
        common.head("推送预览（未联网）")
        common.log(json.dumps(build_payload(content, args.msgtype), ensure_ascii=False, indent=2))
        return 0

    ok, msg = push(content, args.url, args.msgtype)
    common.log(f"{'[OK]' if ok else '[失败]'} {msg}")
    if not ok:
        common.log("      兜底：WorkBuddy 定时任务里也可以改用 wecom 连接器推送。")
        return 3 if "未配置" not in msg else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
