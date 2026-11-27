"""微信 4.x 消息解码：类型覆盖、正文提取、群聊发送人还原、通讯录取名。

消息类型（微信 4.x 的 local_type，低位 16 位有效）
    1      文本                  → 参与待办提炼
    3      图片                  → 无正文（库里只有媒体指纹），计入统计不产出素材
    34     语音                  → 同上（语音转文字结果不在 message_content 里）
    42     名片                  → 提取昵称
    43     视频                  → 无正文
    47     表情                  → 无正文（多为贴纸）
    48     位置                  → 提取标题/地址
    49     链接/文件/小程序/引用/转账/红包 → 解析 appmsg XML 取 title/des
    50     通话                  → 提取时长/状态
    10000  系统消息              → 解析 sysmsg，取 replacemsg（撤回/入群/群名等）
    10002  撤回                  → 同上

正文压缩
4.x 会把部分 message_content 用 zstd 压缩（WCDB_CT_message_content=4，或 blob 以
28 B5 2F FD 开头）。本模块会自动识别并解压；机器上没装 zstd 库时**不静默**，
而是把这条消息标记为「压缩正文未能解压」，由通道汇总成一句提示。
"""
from __future__ import annotations

import html
import re
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path

import common
import msgparse

KIND_LABEL = {
    "text": "文本", "image": "图片", "voice": "语音", "video": "视频", "sticker": "表情",
    "location": "位置", "link": "链接", "file": "文件", "miniprogram": "小程序",
    "quote": "引用回复", "transfer": "转账", "redpacket": "红包", "card": "名片",
    "call": "通话", "system": "系统消息", "recall": "撤回", "other": "其他",
}

# local_type → kind（49/10000 需要看 XML 再细分）
_BY_TYPE = {
    1: "text", 3: "image", 34: "voice", 42: "card", 43: "video", 47: "sticker",
    48: "location", 49: "link", 50: "call", 10000: "system", 10002: "recall",
}

# appmsg 的 <type> → kind
_APPMSG_TYPE = {
    3: "link", 5: "link", 6: "file", 19: "link", 33: "miniprogram", 36: "miniprogram",
    57: "quote", 2000: "transfer", 2001: "redpacket", 51: "quote",
}

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
# 群聊正文前的发送人前缀有四种真实写法，早期只认第一种，于是 4.1 上
# 群消息全都退化成「群成员」：
#   wxid_xxx://n            （好友）
#   123456789@chatroom://n  （群成员）
#   25984983492799273@openim: （外部/开放 IM，冒号后可能带空格）
#   纯数字 id: （部分灰度版本）
_SENDER_PREFIX = re.compile(
    r"^("
    r"wxid_[A-Za-z0-9_\-]{4,64}"
    r"|[A-Za-z0-9_\-]{4,64}@(?:chatroom|openim|im)"
    r"|\d{8,20}"
    r")"
    r":(?:\n| )\s*"
)

_zstd_state: str | None = None


def _decompress(data: bytes) -> bytes | None:
    global _zstd_state
    for mod_name, fn in (("zstandard", None), ("pyzstd", None), ("zstd", None)):
        try:
            mod = __import__(mod_name)  # noqa: PLC0415
        except ImportError:
            continue
        try:
            if mod_name == "zstandard":
                out = mod.ZstdDecompressor().decompress(data)
            else:
                out = mod.decompress(data)
            _zstd_state = mod_name
            return out
        except Exception:  # noqa: BLE001
            continue
    _zstd_state = ""
    return None


def zstd_available() -> bool:
    if _zstd_state is None:
        _decompress(_ZSTD_MAGIC + b"\x00" * 4)     # 故意失败，只为探测后端是否存在
    return bool(_zstd_state)


def _xml_text(raw: str) -> tuple[str, str]:
    """解析 appmsg / sysmsg XML，返回 (kind 提示, 可读文本)。解析失败就正则兜底。"""
    kind_hint = ""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        m = re.search(r"<(?:title|des|replacemsg)>(?:<!\[CDATA\[)?([\s\S]{1,300}?)(?:\]\]>)?</", raw)
        return "", html.unescape(m.group(1)).strip() if m else ""
    tag = root.tag.lower()
    fields: dict[str, str] = {}
    for node in root.iter():
        if len(node) == 0 and (node.text or "").strip():
            fields.setdefault(node.tag.lower(), html.unescape(node.text.strip()))

    if tag == "sysmsg":
        # 撤回/入群/群名变更：真正给人看的是 replacemsg
        for key in ("replacemsg", "content", "template"):
            if fields.get(key):
                return "system", fields[key]
        stype = (root.attrib.get("type") or "").lower()
        if stype in ("revokemsg", "recall"):
            return "recall", "撤回了一条消息"
        return "system", ""

    if tag == "msg" or "appmsg" in raw[:400]:
        atype = fields.get("type", "")
        try:
            kind_hint = _APPMSG_TYPE.get(int(atype), "link")
        except (TypeError, ValueError):
            kind_hint = "link"
        title = fields.get("title", "")
        des = fields.get("des", "")
        if kind_hint == "file":
            return "file", f"[文件] {title}" if title else "[文件]"
        if kind_hint == "quote":
            return "quote", title or des
        if kind_hint == "transfer":
            return "transfer", f"[转账] {title or des or ''}".strip()
        if kind_hint == "redpacket":
            return "redpacket", f"[红包] {title or des or ''}".strip()
        if kind_hint == "miniprogram":
            return "miniprogram", f"[小程序] {title}" if title else "[小程序]"
        txt = " ".join(x for x in (title, des) if x).strip()
        return kind_hint, txt
    if tag == "location":
        return "location", " ".join(
            x for x in (fields.get("poiname", ""), fields.get("label", "")) if x
        ).strip()
    return kind_hint or "other", ""


def decode_message(local_type: int, blob: bytes | str | None) -> tuple[str, str, str]:
    """解一条消息。返回 (正文, kind, 备注)。

    正文为空是正常的：图片/语音/表情本来就没有可读文本 —— 上层据此计数，
    而不是把它当成解析失败。
    """
    kind = _BY_TYPE.get(local_type, "other")
    note = ""
    if blob is None:
        return "", kind, note
    raw: bytes
    if isinstance(blob, str):
        raw = blob.encode("utf-8", errors="replace")
    else:
        raw = bytes(blob)
    if not raw:
        return "", kind, note

    if raw.startswith(_ZSTD_MAGIC):
        plain = _decompress(raw)
        if plain is None:
            return "", kind, "正文是 zstd 压缩的，但本机没有可用的 zstd 解压库"
        raw = plain

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # 少量二进制体（名片/通话）直接捞可读段，捞不到就当无正文
        runs = msgparse.harvest_runs(raw[:8192])
        text = " ".join(runs)

    text = text.strip()
    if not text:
        return "", kind, note

    # 群聊里对方发的消息，正文前会带 "<wxid>:\n"，sender 单独从 real_sender_id 拿
    text = _SENDER_PREFIX.sub("", text)

    if local_type in (49, 10000, 10002) or text.startswith("<"):
        hint, parsed = _xml_text(text)
        if hint:
            kind = hint
        if parsed:
            return msgparse.polish(parsed), kind, note
        if text.startswith("<"):
            return "", kind, note

    return msgparse.polish(text), kind, note


def group_sender_from_blob(blob: bytes | str | None) -> str:
    """群聊消息正文前缀里的真实发送人 wxid（拿不到返回空）。"""
    if blob is None:
        return ""
    raw = blob.encode("utf-8", "replace") if isinstance(blob, str) else bytes(blob)
    if raw.startswith(_ZSTD_MAGIC):
        plain = _decompress(raw)
        if plain is None:
            return ""
        raw = plain
    try:
        head = raw[:120].decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""
    m = _SENDER_PREFIX.match(head)
    return m.group(1) if m else ""


# 通讯录：把 wxid 换成人能读的名字（best effort，失败不影响主流程）
class NameBook:
    """从镜像里的 contact.db 读 wxid → 昵称/备注 映射。

    schema 在不同 4.x 版本里叫法不一（user_name/username、nick_name/nickname…），
    所以这里靠 PRAGMA 探查列名，探不到就整体放弃 —— 名字取不到只是不好看，
    取错了会误导溯源，两者都不能接受。
    """

    def __init__(self, mirror_db_storage: Path | None = None):
        self.map: dict[str, str] = {}
        if mirror_db_storage is None:
            return
        for name in ("contact/contact.db", "contact/contact_0.db"):
            path = mirror_db_storage / name
            if path.exists():
                try:
                    self._load(path)
                except Exception:  # noqa: BLE001
                    continue
                if self.map:
                    return

    def _load(self, path: Path) -> None:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )]
            for table in tables:
                cols = {r[1].lower() for r in con.execute(f'PRAGMA table_info({common.quote_ident(table)})')}
                id_col = next((c for c in ("username", "user_name") if c in cols), None)
                if not id_col:
                    continue
                name_cols = [c for c in ("remark", "nick_name", "nickname", "alias") if c in cols]
                if not name_cols:
                    continue
                sel = ", ".join(f'"{c}"' for c in [id_col, *name_cols])
                for row in con.execute(f'SELECT {sel} FROM {common.quote_ident(table)}'):
                    wxid = str(row[0] or "").strip()
                    if not wxid:
                        continue
                    for val in row[1:]:
                        if val and str(val).strip():
                            self.map.setdefault(wxid, str(val).strip())
                            break
        finally:
            con.close()

    def resolve(self, wxid: str, default: str = "") -> str:
        if not wxid:
            return default
        hit = self.map.get(wxid)
        if hit:
            return hit
        if wxid.endswith("@chatroom"):
            return default or f"群聊·{wxid[:-9][:10]}"
        return default or wxid


__all__ = [
    "KIND_LABEL", "decode_message", "group_sender_from_blob", "zstd_available", "NameBook",
]
