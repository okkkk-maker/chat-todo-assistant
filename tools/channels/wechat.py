"""个人微信（Windows 客户端 4.x）通道：读本地加密库的明文镜像。

链路（每一步都必须能独立解释自己的失败）
    定位 db_storage → 取密钥（必须过 page-1 校验）→ 解密成明文镜像（work/wx_mirror）
    → 以普通 sqlite 只读方式扫描 Msg_ 表 → 解码成统一消息结构

为什么不再走 vendor/decrypt_all.py
    1. 那个脚本靠 `<WECHAT_DATA_ROOT>/*/db_storage` 匹配，历史上两个调用方都
       少传/多传一层，导致「密钥有了却没有镜像」；
    2. 它把所有 *.db 的失败压成一句 "key/format mismatch"，无法区分密钥错、
       库正在写、还是文件坏；
    3. 镜像默认落在 vendor/ 下，用户包往往没有 vendor 目录。
    现在统一由 tools/wxkey.py + tools/wxdecrypt.py 负责，镜像落在
    work/wx_mirror/<wxid>/db_storage（可用 WX_MIRROR_DIR 覆盖）。

只读保证
    * 原始库只做「读文件 + 读 -wal」，不打开写事务，不与微信抢锁；
    * 明文镜像写在程序自己的 work/ 下，微信完全感知不到。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import common
import msgparse
import wxdecrypt
import wxkey
import wxmsg

from . import base

CHANNEL = "WeChat"
PER_TABLE_LIMIT = 500        # 单表单轮上限（达到上限即视为覆盖不完整）
MAX_TABLES = 400             # 单轮最多扫多少张会话表
REQUIRED_COLUMNS = ("local_id", "create_time", "local_type", "message_content")


# 向后兼容壳：health.py / 旧调用方仍可能引用这些名字
def find_vendor() -> Path | None:
    for cand in (common.HOME / "vendor" / "wechat-key",
                 common.HOME.parent / "1-实施员一键初始化包" / "vendor" / "wechat-key"):
        if cand.is_dir():
            return cand
    return None


def find_source_storage(configured: str = "") -> Path | None:
    storage, _roots = wxkey.pick_storage(configured)
    return storage


def _try_decrypt(vendor, raw_key_hex: str, source: Path) -> tuple[bool, str]:
    """用新内核解密一个 db_storage；vendor 参数保留只为兼容旧签名。"""
    try:
        bytes.fromhex(raw_key_hex)
    except ValueError:
        return False, "密钥不是合法十六进制"
    out = wxkey.mirror_root() / wxkey.account_of(source) / "db_storage"
    res = wxdecrypt.decrypt_storage(source, out, _keyring_for(common.load_env(), source, raw_key_hex))
    return res.ok, res.message


def _keyring_for(env: dict, storage: Path, primary_hex: str = "") -> dict[str, str]:
    """拼出 {salt: key}。微信 4.1+ 每库一把，缺一把就少一个分片，必须整环带上。"""
    ring = wxkey.load_keyring(env)
    if primary_hex:
        try:
            salt = wxdecrypt.db_salt(wxkey.message_db(storage))
        except OSError:
            salt = ""
        if salt:
            ring.setdefault(salt, primary_hex)
    return ring


def find_mirror(vendor=None) -> Path | None:
    """兼容旧调用（health/inspect 仍在用）：返回当前账号的明文镜像目录。

    旧实现是从 `vendor/wechat-key/decrypted` 里找；现在镜像改放
    `work/wx_mirror/<wxid>/db_storage`，所以 vendor 参数不再参与判断。
    """
    env = common.load_env()
    storage, _roots = wxkey.pick_storage((env.get("WX_DATA_PATH") or "").strip())
    if storage is None:
        return None
    mirror = wxkey.mirror_root(env) / wxkey.account_of(storage) / "db_storage"
    return mirror if mirror.is_dir() else None


def message_dbs(storage) -> list[Path]:
    """镜像里的 message_N.db 列表（兼容旧调用的辅助函数）。"""
    if not storage:
        return []
    msg_dir = Path(storage) / "message"
    if not msg_dir.is_dir():
        return []
    return sorted(msg_dir.glob("message_[0-9]*.db"))


# 镜像准备
def _mirror_path(env: dict, storage: Path) -> Path:
    return wxkey.mirror_root(env) / wxkey.account_of(storage) / "db_storage"


def _ensure_mirror(env: dict, storage: Path, keys):
    """镜像不存在、比源库旧（包含检测 WAL 写入）、或分片有缺口，就重新解一次。

    keys 可以是 {salt: key}（4.1+ 正确形态）或 bytes（老调用）。
    判断「有缺口」很有必要：新到一个 message_N.db 时，只有它的密钥在环里，
    否则会一直沿用旧镜像、永远读不到新分片。
    同时，由于微信主程序频繁写入 WAL，必须比对主库与 WAL 的最新修改时间与大小，
    杜绝因仅 WAL 更新而漏刷镜像的问题。
    """
    mirror = _mirror_path(env, storage)
    src_db = storage / "message" / "message_0.db"
    dst_db = mirror / "message" / "message_0.db"
    need = True
    if dst_db.exists():
        try:
            wal_file = storage / "message" / "message_0.db-wal"
            src_mtime = src_db.stat().st_mtime
            if wal_file.exists():
                wal_stat = wal_file.stat()
                if wal_stat.st_size > 0 and wal_stat.st_mtime > src_mtime:
                    src_mtime = wal_stat.st_mtime
            need = src_mtime > dst_db.stat().st_mtime + 1
        except OSError:
            need = True

    if not need:
        try:
            for s in storage.glob("message/message_[0-9]*.db"):
                d = mirror / s.relative_to(storage)
                if not d.exists():
                    if isinstance(keys, dict) and keys.get(wxdecrypt.db_salt(s)):
                        need = True
                        break
                else:
                    s_mtime = s.stat().st_mtime
                    s_wal = s.parent / f"{s.name}-wal"
                    if s_wal.exists():
                        w_stat = s_wal.stat()
                        if w_stat.st_size > 0 and w_stat.st_mtime > s_mtime:
                            s_mtime = w_stat.st_mtime
                    if s_mtime > d.stat().st_mtime + 1:
                        need = True
                        break
        except OSError:
            need = True

    if not need:
        return mirror, None
    res = wxdecrypt.decrypt_storage(storage, mirror, keys)
    return (mirror if res.ok else None), res


# 读镜像
def _table_map(con: sqlite3.Connection) -> dict[str, str]:
    """表名 → wxid。微信 4.x 一个会话一张 Msg_<md5(user_name)> 表。"""
    import hashlib

    out: dict[str, str] = {}
    has = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='Name2Id'"
    ).fetchone()
    if not has:
        return out
    cols = {r[1].lower() for r in con.execute('PRAGMA table_info("Name2Id")')}
    col = "user_name" if "user_name" in cols else ("username" if "username" in cols else "")
    if not col:
        return out
    for (uname,) in con.execute(f'SELECT {common.quote_ident(col)} FROM "Name2Id"'):
        uname = str(uname or "").strip()
        if uname:
            out["Msg_" + hashlib.md5(uname.encode("utf-8")).hexdigest()] = uname
    return out


SYSTEM_OR_OFFICIAL_ACCOUNTS = {
    "weixin", "notifymessage", "fmessage", "filehelper", "medianote",
    "qmessage", "qqmail", "floatbottle", "shake", "lbsapp", "newsapp",
    "blogapp", "brandsessionholder", "masssendgetmsg", "mphelper",
}


def is_marketing_or_system_account(username: str) -> bool:
    """过滤微信公众号/服务号（gh_开头）与系统服务通知号，彻底杜绝外卖打车营销消息进入待办池。"""
    u = (username or "").strip().lower()
    if not u:
        return False
    if u in SYSTEM_OR_OFFICIAL_ACCOUNTS:
        return True
    if u.startswith("gh_"):  # 微信服务号/公众号统一以 gh_ 开头
        return True
    return False

def _my_sender_id(con: sqlite3.Connection, self_wxid: str) -> int | None:
    """登录账号在 Name2Id 里的 id，用来把「本人发言」标出来。"""
    try:
        cols = {r[1].lower() for r in con.execute('PRAGMA table_info("Name2Id")')}
        if not {"user_name", "id"} <= cols:
            return None
        row = con.execute(
            'SELECT "id" FROM "Name2Id" WHERE "user_name"=?', (self_wxid,)
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None
    except (sqlite3.Error, ValueError, TypeError):
        return None


def _read_shard(db_path: Path, since: float, until: float, self_wxid: str,
                book: "wxmsg.NameBook", stats: dict):
    """读一个分片库。返回 (消息, 原文条数, 备注, 是否覆盖不完整)。"""
    notes: list[str] = []
    msgs: list[dict] = []
    raw_total = 0
    incomplete = False

    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        return [], 0, [f"{db_path.name} 打不开: {exc}"], True

    try:
        mapping = _table_map(con)
        my_id = _my_sender_id(con, self_wxid)
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg!_%' ESCAPE '!'"
        )]
        for table in tables:
            peer = mapping.get(table, "")
            # 严格过滤微信公众号、服务号与系统号（外卖、打车、酒店优惠券均来自此类账号）
            if is_marketing_or_system_account(peer):
                continue

            cols = {r[1].lower() for r in con.execute(f'PRAGMA table_info({common.quote_ident(table)})')}
            if not all(c in cols for c in REQUIRED_COLUMNS):
                incomplete = True
                stats["skipped_tables"] = stats.get("skipped_tables", 0) + 1
                continue
            has_sender = "real_sender_id" in cols
            q = (
                "SELECT local_id, create_time, local_type, message_content"
                f'{", real_sender_id" if has_sender else ""} FROM "{table}" '
                "WHERE create_time >= ? AND create_time <= ? "
                f"ORDER BY create_time DESC LIMIT {PER_TABLE_LIMIT}"
            )
            try:
                rows = con.execute(q, (int(since), int(until + 60))).fetchall()
            except sqlite3.Error as exc:
                incomplete = True
                notes.append(f"{table} 查询失败: {exc}")
                continue
            if len(rows) >= PER_TABLE_LIMIT:
                incomplete = True
                stats["capped_tables"] = stats.get("capped_tables", 0) + 1
            raw_total += len(rows)

            peer = mapping.get(table, "")
            is_group = str(peer).endswith("@chatroom")
            for row in rows:
                local_id, create_time, local_type, blob = row[0], row[1], row[2], row[3]
                sender_id = row[4] if has_sender else None
                try:
                    ltype = int(local_type) & 0xFFFF
                except (TypeError, ValueError):
                    ltype = 0
                text, kind, note = wxmsg.decode_message(ltype, blob)
                stats["kinds"][kind] = stats["kinds"].get(kind, 0) + 1
                if note:
                    stats["decode_notes"].add(note)
                try:
                    ts = float(create_time)
                except (TypeError, ValueError):
                    continue
                if ts < since or ts > until + 60:
                    continue
                if not text or msgparse.is_noise(text):
                    continue

                # 发送人：群聊正文前缀里的 wxid 最可靠；否则用 real_sender_id
                sender_wxid = wxmsg.group_sender_from_blob(blob)
                if sender_wxid and sender_wxid == self_wxid:
                    sender = "我（本人）"
                elif sender_wxid:
                    sender = book.resolve(sender_wxid)
                elif sender_id is not None and my_id is not None and sender_id == my_id:
                    sender = "我（本人）"
                elif not is_group and peer:
                    sender = book.resolve(peer)
                else:
                    sender = "群成员"

                chat_name = book.resolve(peer, f"微信群·{peer[:12]}" if is_group
                                         else f"微信·{peer[:16] or table[-8:]}")
                msgs.append(base.make_message(
                    source=CHANNEL,
                    chat_type="group" if is_group else "c2p",
                    chat_id=peer or table,
                    chat_name=chat_name,
                    sender=sender,
                    ts=ts,
                    msg_id=f"{db_path.name}:{table}:{local_id}",
                    content=text,
                ))
    except sqlite3.Error as exc:
        incomplete = True
        notes.append(f"{db_path.name} 读取失败: {exc}")
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass
    return msgs, raw_total, notes, incomplete


# 主入口
def fetch(env: dict, since: float, until: float) -> base.ChannelResult:
    started = time.time()
    notes: list[str] = []

    storage, _roots = wxkey.pick_storage((env.get("WX_DATA_PATH") or "").strip())
    if storage is None:
        return base.ChannelResult(
            channel=CHANNEL,
            status=base.NOT_INSTALLED,
            message=(
                "没找到微信数据目录（xwechat_files/<wxid>/db_storage）。"
                "确认电脑版微信 4.x 已登录过，或在控制台把 WX_DATA_PATH 指到 xwechat_files"
            ),
            note=f"已尝试: {[str(r) for r in wxkey._user_dirs()][:6]}",
        )
    db_path = wxkey.message_db(storage)
    if not db_path.exists():
        return base.ChannelResult(
            channel=CHANNEL,
            status=base.NOT_INSTALLED,
            message=f"{storage} 下没有 message/message_0.db（微信还没产生聊天记录）",
            note=str(storage),
        )
    notes.append(f"数据源: {storage}")

    # 1) 取密钥（必须过校验，不能拿旧密钥硬解）
    allow_scan = str(env.get("WX_ALLOW_SCAN", "1")).strip().lower() not in ("0", "false", "no")
    try:
        scan_seconds = float(env.get("WX_SCAN_SECONDS") or 25)
    except ValueError:
        scan_seconds = 25.0
    key = wxkey.acquire(
        env, db_path,
        allow_scan=allow_scan,
        manual_key=(env.get("WX_DBKEY_MANUAL") or "").strip(),
        timeout=scan_seconds,
    )
    notes.extend(key.notes)
    if not key.ok:
        # 库存在但没密钥：都归到「还没配置好」，让用户按提示去取；
        # 只有真正把库读坏/密钥不匹配才算 FAIL。
        status = {
            wxkey.NOT_CONFIGURED: base.NOT_CONFIGURED,
            wxkey.NO_WECHAT: base.NOT_CONFIGURED,      # 微信没运行 → 取不到密钥，不是没装
            wxkey.KEY_MISMATCH: base.FAIL,
            wxkey.SCAN_FAILED: base.FAIL,
        }.get(key.status, base.FAIL)
        return base.ChannelResult(
            channel=CHANNEL,
            status=status,
            message=key.message,
            note="；".join(n for n in notes if n)[:1500],
            latency_ms=int((time.time() - started) * 1000),
        )
    ring = dict(key.keyring)
    if key.key:
        ring.setdefault(wxdecrypt.db_salt(db_path), key.key)
    if key.source != "env":
        written = wxkey.store_key(key.key, env=env, keyring=ring)   # 校验通过才落盘
        notes.append(f"密钥来源 {key.source}，已落盘: {', '.join(written[:2])}")
    if len(ring) > 1:
        notes.append(f"密钥环覆盖 {len(ring)} 个库")

    # 2) 准备明文镜像
    mirror, dres = _ensure_mirror(env, storage, ring)
    if dres is not None:
        notes.append(f"镜像刷新: {dres.message}（{dres.elapsed_ms} ms）")
        notes.extend(dres.notes)
    if mirror is None:
        detail = dres.message if dres else "镜像不可用"
        return base.ChannelResult(
            channel=CHANNEL,
            status=base.FAIL,
            message=f"微信明文镜像生成失败：{detail}",
            note="；".join(notes)[:1500],
            latency_ms=int((time.time() - started) * 1000),
        )
    notes.append(f"镜像: {mirror}")

    # 3) 扫镜像
    dbs = sorted((mirror / "message").glob("message_[0-9]*.db"))
    if not dbs:
        return base.ChannelResult(
            channel=CHANNEL,
            status=base.FAIL,
            message="镜像里没有 message_N.db，解密流程有问题",
            note="；".join(notes)[:1500],
        )

    book = wxmsg.NameBook(mirror)
    stats: dict = {"kinds": {}, "decode_notes": set()}
    messages: list[dict] = []
    raw_total = 0
    incomplete = False
    shards = 0
    self_wxid = wxkey.account_of(storage)
    for db in dbs:
        if shards >= MAX_TABLES:
            incomplete = True
            notes.append(f"分片数达到上限 {MAX_TABLES}")
            break
        got, raw_n, shard_notes, bad = _read_shard(db, since, until, self_wxid, book, stats)
        messages.extend(got)
        raw_total += raw_n
        incomplete = incomplete or bad
        notes.extend(shard_notes)
        shards += 1

    messages, dup = base.dedupe(messages)
    messages.sort(key=lambda m: base.parse_ts(m) or 0)
    if len(messages) > base.MAX_MESSAGES_PER_CHANNEL:
        incomplete = True
        messages = messages[-base.MAX_MESSAGES_PER_CHANNEL:]
        notes.append("消息数达到通道上限，已保留最新部分")

    # 4) 覆盖度：镜像比窗口起点还旧 → 本轮不通当完整
    try:
        if (mirror / "message" / "message_0.db").stat().st_mtime + 60 < since:
            incomplete = True
            notes.append("镜像比本次时间窗还旧，覆盖不完整")
    except OSError:
        pass

    kinds = ", ".join(
        f"{wxmsg.KIND_LABEL.get(k, k)}{v}"
        for k, v in sorted(stats["kinds"].items(), key=lambda x: -x[1])
    )
    if kinds:
        notes.append(f"类型分布: {kinds}")
    for note in sorted(stats["decode_notes"]):
        notes.append(f"解码提示: {note}")
    if dup:
        notes.append(f"去重丢弃 {dup} 条")

    status = base.STALE if incomplete else base.OK
    msg = f"微信共采集 {len(messages)} 条（{shards} 个分片 / 原文 {raw_total} 条）"
    if not messages and status == base.OK:
        msg += "；窗口内没有可提炼的文本消息"

    return base.ChannelResult(
        channel=CHANNEL,
        status=status,
        messages=messages,
        raw_count=raw_total,
        dropped=dup,
        max_ts=max((base.parse_ts(m) or 0) for m in messages) if messages else 0.0,
        message=msg,
        note="；".join(n for n in notes if n)[:1800],
        latency_ms=int((time.time() - started) * 1000),
    )
