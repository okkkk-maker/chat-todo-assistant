"""把 QQ 消息体解析成可读纯文本。

NTQQ 的消息正文不是统一格式，实测见过的有：
    - JSON 文本（富文本元素数组，键名 content / text / fileName ...）
    - 二进制 protobuf（元素列表序列化后的字节串）
    - 纯文本
所以这里不假设单一格式，按「JSON 优先、否则从字节流里捞可读文本段」处理，
捞不出来就返回空串 —— 宁可漏一条，也不往外吐乱码（不臆造）。
"""

from __future__ import annotations

import json
import re
import unicodedata

# JSON 里可能是正文的键（按优先级）
TEXT_KEYS = (
    "content", "text", "msg", "message", "summary", "title", "desc", "description",
    "fileName", "filename", "name", "prompt", "brief", "nick", "nickName", "card",
)

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
WS_RE = re.compile(r"[ \t\u00a0\u3000]+")

# 系统提示 / 占位符，不算待办素材
NOISE_PATTERNS = [
    r"^\[(图片|表情|动画表情|语音|视频|文件|位置|链接|红包|转账|名片|音乐|小程序|合并转发|聊天记录|涂鸦|戳一戳|礼物)\]$",
    # 无标题的卡片/富文本占位：这是解析残留，不是消息内容。
    # 带标题的 "[卡片] 群相册《班级活动》" 会被保留（有信息量）。
    r"^\[(卡片消息|富文本|群名片|合并转发|文件/附件)\]$",
    r"撤回了一条消息$",
    r"加入了群聊$",
    r"退出了群聊$",
    r"修改群名为",
    r"^以上是打招呼的内容",
    r"消息已发出，但被对方拒收",
    r"对方开启了朋友验证",
    r"^\s*$",
]
NOISE_RE = [re.compile(p) for p in NOISE_PATTERNS]

# 商业营销推广 / 优惠券 / 促销返现 / 垃圾广告（严防污染待办池）
MARKETING_PATTERNS = [
    r'(?:优惠券|代金券|满减券|折扣券|立减券|免单券|抵用券|兑换券|专属券|外卖红包|打车券|酒店券|外卖神券|神券|大额券)',
    r'(?:满\s*\d+\s*(?:元|块)?\s*减\s*\d+|立减\s*\d+\s*元?|最高立减|首单立减|无门槛(?:券|立减)?|免费领取|点击领取|立即核销|立即抢购|去使用|戳链接领|戳我领)',
    r'(?:神券节|百亿补贴|爆款秒杀|限时秒杀|限时特惠|超值优惠|专享福利|下单返|低至\s*\d+\s*折|特价房|返现|抽免单|买一赠一)',
    r'(?:回复\s*(?:TD|T|td|t)\s*退订|退订回\s*(?:T|TD|t|td)|退订请回复|拒收请回复|退订回复)',
    r'(?:帮我助力|帮我点一下|帮我砍一刀|拼团成功|来和我一起拼|复制淘口令)',
]
MARKETING_RE = [re.compile(p) for p in MARKETING_PATTERNS]


def _is_cjk(ch: str) -> bool:
    return bool(CJK_RE.match(ch))


def _is_text_ascii(ch: str) -> bool:
    o = ord(ch)
    return 32 <= o <= 126


def _flush(buf: bytearray, out: list[str]) -> None:
    if len(buf) >= 2:
        try:
            out.append(buf.decode("utf-8"))
        except UnicodeDecodeError:
            pass
    buf.clear()


def harvest_runs(buf: bytes, limit: int = 200) -> list[str]:
    """从二进制里捞出连续可读文本段（中文按 UTF-8 多字节识别）。"""
    out: list[str] = []
    cur = bytearray()
    i, n = 0, len(buf)
    while i < n and len(out) < limit:
        b = buf[i]
        if b < 0x80:
            ch = chr(b)
            if _is_text_ascii(ch) or ch in "\r\n\t":
                cur.append(b)
                i += 1
                continue
            _flush(cur, out)
            i += 1
            continue
        matched = False
        for ln in (4, 3, 2):
            chunk = buf[i:i + ln]
            if len(chunk) < ln:
                continue
            try:
                s = chunk.decode("utf-8")
            except UnicodeDecodeError:
                continue
            cat = unicodedata.category(s[0])
            if cat.startswith("L") or cat.startswith("N") or cat.startswith("P"):
                cur.extend(chunk)
                i += ln
                matched = True
                break
        if not matched:
            _flush(cur, out)
            i += 1
    _flush(cur, out)
    return out


def _walk_json(obj, acc: list[str], depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(obj, str):
        s = obj.strip()
        if s:
            acc.append(s)
        return
    if isinstance(obj, dict):
        for key in TEXT_KEYS:
            val = obj.get(key)
            if isinstance(val, (str, int, float)) and str(val).strip():
                acc.append(str(val).strip())
        for key, val in obj.items():
            if key in TEXT_KEYS:
                continue
            if isinstance(val, (dict, list)):
                _walk_json(val, acc, depth + 1)
        return
    if isinstance(obj, list):
        for item in obj:
            _walk_json(item, acc, depth + 1)


def _try_json(text: str):
    s = text.strip()
    if not s or s[0] not in "[{":
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None


def _dedupe_keep(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in seq:
        k = s.strip()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


def clean(text: str) -> str:
    if not text:
        return ""
    # 去掉控制字符与零宽字符
    text = "".join(
        ch for ch in text
        if ch in "\n\t" or (unicodedata.category(ch)[0] != "C" and ch not in "\u200b\u200c\u200d\ufeff")
    )
    text = WS_RE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def is_marketing_noise(text: str) -> bool:
    """识别营销推广、外卖优惠券、打车满减等非人际有效办公消息。"""
    if not text:
        return False
    t = text.strip()
    return any(rx.search(t) for rx in MARKETING_RE)


def is_noise(text: str) -> bool:
    if not text:
        return True
    t = text.strip()
    if len(t) <= 1:
        return True
    for rx in NOISE_RE:
        if rx.search(t):
            return True
    if is_marketing_noise(t):
        return True
    # 纯标点 / 纯 emoji / 纯符号
    meaningful = [ch for ch in t if unicodedata.category(ch)[0] in "LN"]
    if not meaningful:
        return True
    return False


def _score(run: str) -> float:
    """给候选文本段打分：中文权重高，太长太短都扣分。"""
    s = run.strip()
    if not s:
        return 0.0
    cjk = len(CJK_RE.findall(s))
    letters = sum(1 for ch in s if ch.isalpha())
    score = cjk * 3.0 + letters * 0.6 + min(len(s), 120) * 0.05
    if cjk == 0 and len(s) < 6:
        score -= 4.0          # 纯英数短串多半是字段名/uid
    if re.fullmatch(r"[0-9a-fA-F]{8,}", s):
        score -= 6.0          # hex / 随机串
    if re.fullmatch(r"[A-Za-z0-9_\-+/=]{12,}", s):
        score -= 3.0
    return score


MAX_PARSE_BYTES = 16384   # 待办正文/摘要不可能超过 16KB，超长部分直接丢弃


def decode_content(raw, mode: str = "auto", max_len: int = 1200) -> str:
    """把一条消息的原始字段值解析成纯文本。解析不出来返回空串。"""
    if raw is None:
        return ""
    if isinstance(raw, (bytes, bytearray, memoryview)):
        buf = bytes(raw)
        if not buf:
            return ""
        # 大 Blob 熔断：图片原图、离线文件缓存、长语音动辄几 MB 到上百 MB，
        # 下面每一步（utf-8 解码、clean 逐字符过滤、harvest_runs 的
        # unicodedata.category 逐字节循环）都是 O(N)。不设上限时 CPU 直接打满
        # 几十秒，上层定时任务超时。正文只可能在前 16KB，超出部分没有解析价值。
        if len(buf) > MAX_PARSE_BYTES:
            buf = buf[:MAX_PARSE_BYTES]
        text = None
        try:
            text = buf.decode("utf-8")
        except UnicodeDecodeError:
            text = None

        if text is not None:
            if mode in ("auto", "json"):
                data = _try_json(text)
                if data is not None:
                    acc: list[str] = []
                    _walk_json(data, acc)
                    merged = _merge(acc)
                    if merged:
                        return merged[:max_len]
            if mode in ("auto", "text") and not is_noise(clean(text)):
                # 二进制里夹带的明文，可能带不可见控制字符，过滤后长度明显缩水就作废
                cleaned = trim_artifacts(clean(text))
                if cleaned and len(cleaned) >= 0.5 * len(text.strip()):
                    return cleaned[:max_len]
        if mode == "json":
            return ""
        runs = harvest_runs(buf)
        return _merge(runs)[:max_len]

    if isinstance(raw, (dict, list)):
        acc = []
        _walk_json(raw, acc)
        return _merge(acc)[:max_len]

    text = str(raw)
    data = _try_json(text) if mode in ("auto", "json") else None
    if data is not None:
        acc = []
        _walk_json(data, acc)
        merged = _merge(acc)
        if merged:
            return merged[:max_len]
    return trim_artifacts(clean(text))[:max_len]


def _merge(candidates: list[str]) -> str:
    """从候选段里挑出正文：优先含中文的长段，最多拼 3 段。"""
    picked: list[tuple[float, str]] = []
    for c in _dedupe_keep(candidates):
        s = clean(c)
        if not s or is_noise(s):
            continue
        sc = _score(s)
        if sc <= 0:
            continue
        picked.append((sc, s))
    if not picked:
        return ""
    picked.sort(key=lambda x: -x[0])
    best = [s for _, s in picked[:3]]
    # 若最优段已含中文，丢掉其它纯 ASCII 短段（多半是协议字段）
    if CJK_RE.search(best[0]):
        best = [s for s in best if CJK_RE.search(s)] or best[:1]
    return trim_artifacts(" ".join(best).strip())


def trim_artifacts(text: str) -> str:
    """去掉 protobuf 前后残留的 1~2 个 ASCII 字母/数字（长度前缀被当成了字符）。"""
    t = text.strip()
    for _ in range(2):
        m = re.match(r"^[A-Za-z0-9]{1,2}(?=[\u3400-\u9fff])", t)
        if m:
            t = t[m.end():]
            continue
        m = re.search(r"(?<=[\u3400-\u9fff])[A-Za-z0-9]{1,2}$", t)
        if m:
            t = t[:m.start()]
            continue
        break
    return t.strip()


def looks_like_json(raw) -> bool:
    if isinstance(raw, (dict, list)):
        return True
    if isinstance(raw, (bytes, bytearray, memoryview)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            return False
    if not isinstance(raw, str):
        return False
    return _try_json(raw) is not None


# 收尾清洗：卡片 JSON / 附件链接 / 前导噪音
_CARD_KEYS = ("prompt", "summary", "title", "desc", "content", "text", "wording")
_LEAD_JUNK = "'\"$`:;,。、"          # protobuf 长度字节或引用标记残留


def polish(text: str, max_len: int = 500) -> str:
    """把一条消息整理成"人能读、AI 能懂"的样子。

    实测（QQ 9.9.36）三类噪音：
      1. 卡片消息会把整个 JSON 当正文：{"app":"com.tencent.feed.lua","prompt":"群相册《班级活动》"}
      2. 文件消息只剩下载链接：/download?appid=1407&fileid=EhQB3Gq...
      3. protobuf 长度字节被当成首字符：'不愧是... / $这才... / : 神了呀
    """
    s = (text or "").strip()
    if not s:
        return ""

    # 1) 卡片 JSON（可能因长度截断而不完整，所以解析失败还要用正则兜底）
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                for k in _CARD_KEYS:
                    v = obj.get(k)
                    if isinstance(v, str) and v.strip():
                        return f"[卡片] {v.strip()}"[:max_len]
                return "[卡片消息]"
        except (ValueError, TypeError):
            pass
        m = re.search(
            r'"(?:prompt|summary|title|desc|wording)"\s*:\s*"([^"\\]{2,100})"', s
        )
        if m:
            return f"[卡片] {m.group(1).strip()}"[:max_len]
        if s.count('"') > 4 and ":" in s:
            return "[卡片消息]"

    # 2) 附件/下载链接
    if s.startswith("/download?") or ("fileid=" in s[:60] and len(s) < 400):
        return "[文件/附件]"

    # 3) 前导噪音：单个标点或"数字 + 空格"后紧跟中文
    if len(s) > 2:
        if s[0] in _LEAD_JUNK and CJK_RE.search(s[1:3]):
            s = s[1:].lstrip()
        else:
            m = re.match(r"^([0-9]{1,3}|[!-/:-@\[-`{-~])\s+([\u4e00-\u9fff])", s)
            if m:
                s = s[m.end(1):].strip()

    return s[:max_len]
