"""真实增量抽取：把 nt_msg.db 里最近一段时间的消息变成 work/待办素材.json。

设计要点（对应手册 B1 / B2，任何改动都要保住这五条）：
    1. 只读：先拷 db + -wal 到 work/tmp_db/，原位库一个字节都不碰
       （-shm 不拷，SQLite 打开 WAL 库时自动重建）。
    2. 不猜列名：列名全部来自 config/schema.json（程序包 A 探测固化的）。
       对不上就报错，不硬编码 40001/40800。
    3. 三级状态：正常 / 离线或切号（陈旧告警）/ 解密失败，各自有明确产物。
    4. 不静默：失败写 output/异常说明-*.txt，正常则清理旧告警；
       密钥没就绪时直接拒绝产出素材（红线 ）。
    5. 不截断最新：窗口内按时间倒序取最新记录，再在内存里反转成升序；
       单表满页时继续向更早分页，硬上限处必须留告警。

命令行:
    python tools/extract_impl.py                 # 按 .env + 游标取数据
    python tools/extract_impl.py --hours 6
    python tools/extract_impl.py --db X --schema Y --out Z --env tests/tmp/test.env   # 自测用
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
import msgparse  # noqa: E402
import qqnames  # noqa: E402
import qqdb  # noqa: E402

MAX_MESSAGES = 3000
PER_TABLE_CAP = 1500
# 单表单轮最多抓多少条（= 2 页）。3 天积压一般在 2000 条内，2 页足够；
# 真超过这个量就明确告警，而不是悄悄丢消息。
TABLE_FETCH_CAP = PER_TABLE_CAP * 2
# 时钟倾斜容差：对方/本机时钟快几秒时，消息时间戳会落在「当前时间」之后。
# 严格 ts > until 会把它当未来消息丢弃，而游标一推进就再也取不回来。
CLOCK_SKEW_TOL = 60

# 只从这几张"真·消息表"取数。nt_msg.db 里还有大量结构表
# （recent_contact_v3_table / group_at_me_msg / *_flow_table / 各种 storage），
# 它们的列号含义完全不同，混进来就是噪音（实测会把"会话1"这种假消息塞进清单）。
CORE_MSG_TABLES = ("group_msg_table", "c2c_msg_table", "c2c_temp_msg_table", "discuss_msg_table")

# 陈旧判定：工作时段 4 小时，夜间放宽到 8 小时（人都睡了，不该报警）
STALE_HOURS_WORK = 4.0
STALE_HOURS_NIGHT = 8.0
WORK_HOUR_START = 7
WORK_HOUR_END = 22


@dataclass
class Result:
    level: int = 0                     # 0 正常 / 1 陈旧 / 2 失败
    status: str = "OK"                 # OK / STALE / FAIL / NO_KEY
    count: int = 0
    stale_warning: bool = False
    message: str = ""
    exception: Path | None = None
    meta: dict = field(default_factory=dict)
    messages: list = field(default_factory=list)
    tables_used: list = field(default_factory=list)
    raw_count: int = 0          # 窗口内原始行数（过滤前）
    dropped_count: int = 0      # 被当作表情/卡片/系统提示筛掉的行数
    max_ts: float = 0.0         # 本次读到的最新消息时间（用于推进游标）
    until: float = 0.0          # 本次查询窗口的右边界（无有效消息时用它推进游标）
    data_state: str = "ok"      # ok / no_new / only_noise / offline

    @property
    def ok(self) -> bool:
        return self.level < 2


# 新鲜度
def classify_state(stale: bool, raw_count: int, kept: int) -> str:
    """把「0 条」拆成三种互不相同的含义 —— 这是本模块最容易被误读的地方。

    不区分的话，"没读到"会被当成"没有待办"，院长据此以为今天没事。

        offline    库根本没更新（QQ 没登录/没同步），新消息不在这台电脑上
        no_new     库是活的，但这段窗口确实没有新消息
        only_noise 有新消息，但全是表情/卡片/系统提示，没有可提炼内容
        ok         有可用内容
    """
    if stale:
        return "offline"
    if raw_count == 0:
        return "no_new"
    if kept == 0:
        return "only_noise"
    return "ok"


def stale_threshold_hours(now_ts: float | None = None) -> float:
    h = datetime.fromtimestamp(now_ts or time.time()).hour
    return STALE_HOURS_WORK if WORK_HOUR_START <= h < WORK_HOUR_END else STALE_HOURS_NIGHT


def freshness(db_path: Path, now_ts: float | None = None) -> dict:
    """判别库是不是"活的"：看 db / -wal / -shm 三者里**最新的写入时间**。

     绝不能用"WAL 文件存在"当在线的证据。QQ 正常退出后 -wal 不会被删除，
    内容永远停在最后一次写入，文件一直躺在那里。2026-10-29 实测踩到：
    库和 wal 都停在 00:16、QQ 进程已经不在，代码仍报 「存在 -wal，QQ 在线」，
    于是陈旧告警从不触发、游标照常推到当前时间，离线期间的消息被永久跳过。
    """
    now_ts = now_ts or time.time()
    if not db_path.exists():
        return {"level": 2, "age": None, "newest": None, "reason": f"库文件不存在: {db_path}"}

    stamps: dict[str, float] = {}
    for suffix, label in (("", "主库"), ("-wal", "-wal"), ("-shm", "-shm")):
        p = Path(str(db_path) + suffix)
        if p.exists():
            try:
                stamps[label] = p.stat().st_mtime
            except OSError:
                continue
    if not stamps:
        return {"level": 2, "age": None, "newest": None, "reason": "库文件无法读取"}

    newest_label = max(stamps, key=lambda k: stamps[k])
    newest = stamps[newest_label]
    age = max(0.0, now_ts - newest)
    threshold = stale_threshold_hours(now_ts) * 3600
    if age <= threshold:
        return {
            "level": 0, "age": age, "newest": newest, "newest_label": newest_label,
            "reason": f"{newest_label} {common.human_age(age)}前有写入",
        }
    return {
        "level": 1, "age": age, "newest": newest, "newest_label": newest_label,
        "reason": (
            f"最新写入是 {newest_label} 在 {common.fmt(newest)}，"
            f"已 {common.human_age(age)}没有更新（阈值 {threshold / 3600:g} 小时）"
        ),
    }


# 取数
def _ts_to_epoch(value, unit: str) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if unit == "ms" or v > 1e11:
        v /= 1000.0
    if v < 1e8 or v > 4e9:
        return None
    return v


def _load_group_names(schema: dict) -> dict[str, str]:
    info = schema.get("group_names") or {}
    rel = info.get("file") or ""
    if not rel:
        return {}
    p = common.ROOT / rel
    if not p.exists():
        return {}
    try:
        data = common.read_json(p)
        return {str(k): str(v) for k, v in data.items()}
    except (json.JSONDecodeError, OSError):
        return {}


def _build_query(spec: dict, paged: bool = False) -> tuple[str, list[str]]:
    """拼取数 SQL。

    排序刻意用 DESC：窗口内**最新**的一批才是当下的待办。
    以前用 ASC + LIMIT 1500，离线几天后一次同步 2000 条时，取到的全是
    最老的那批，当天最新的紧急批示被整体截断；而游标按这批老消息推进，
    最新消息从此永久取不到（这是最隐蔽的一类数据漏失）。
    paged=True 时追加 `时间戳 < ?`，供分页兜底继续往更早的方向取。
    """
    cols = spec["columns"]
    table = spec["name"]
    id_expr = "rowid" if cols["msg_id"] == "rowid" else f'"{cols["msg_id"]}"'
    parts = [
        f"{id_expr} AS _id",
        f'"{cols["timestamp"]}" AS _ts',
        f'"{cols["content"]}" AS _content',
    ]
    parts.append(f'"{cols["chat_id"]}" AS _chat' if cols.get("chat_id") else "'' AS _chat")
    parts.append(f'"{cols["sender"]}" AS _sender' if cols.get("sender") else "'' AS _sender")
    parts.append(f'"{cols["sender_name"]}" AS _name' if cols.get("sender_name") else "'' AS _name")

    where = [f'"{cols["timestamp"]}" >= ?', f'"{cols["timestamp"]}" <= ?', f'"{cols["content"]}" IS NOT NULL']
    if paged:
        # 时间戳 + 稳定 ID 的 keyset 分页：同秒超过一页时，不能只按 ts<页尾，
        # 否则与页尾同秒但尚未交付的行会被整批跳过。
        where.append(
            f'("{cols["timestamp"]}" < ? OR '
            f'("{cols["timestamp"]}" = ? AND {id_expr} < ?))'
        )
    sql = (
        f'SELECT {", ".join(parts)} FROM "{table}" '
        f'WHERE {" AND ".join(where)} '
        f'ORDER BY "{cols["timestamp"]}" DESC, {id_expr} DESC LIMIT {PER_TABLE_CAP}'
    )
    return sql, []


def _fetch_rows(con, spec: dict, lo: int, hi: int, notes: list[str]) -> list:
    """按「最新优先」取数，必要时往更早的方向分页，最终返回时间升序的行。

    单页上限 PER_TABLE_CAP。满页说明可能还有更早的，就带着「本页最早时间戳」
    再取一页，最多攒到 TABLE_FETCH_CAP；真顶到硬上限就写一条明确告警，
    绝不在无提示的情况下把更早的消息丢掉。
    """
    sql, _ = _build_query(spec)
    paged_sql, _ = _build_query(spec, paged=True)

    rows = con.execute(sql, (lo, hi)).fetchall()
    while len(rows) >= PER_TABLE_CAP and len(rows) < TABLE_FETCH_CAP:
        floor_ts = rows[-1][1]          # DESC 结果集里最后一条即本页最早
        floor_id = rows[-1][0]
        if floor_ts is None or floor_id is None:
            break
        try:
            page = con.execute(paged_sql, (lo, hi, floor_ts, floor_ts, floor_id)).fetchall()
        except sqlite3.DatabaseError as exc:
            notes.append(f"表 {spec['name']} 续页失败: {exc}")
            break
        if not page:
            break
        rows.extend(page)

    if len(rows) >= TABLE_FETCH_CAP:
        notes.append(
            f"表 {spec['name']} 窗口内消息超过 {TABLE_FETCH_CAP} 条，"
            f"已优先保留最新 {TABLE_FETCH_CAP} 条；更早的部分需缩短回看窗口后重跑"
        )
    rows.reverse()                      # 倒序取出的结果，在内存里反转为时间升序
    return rows


def _validate_spec(con, spec: dict) -> str | None:
    have = set(qqdb.columns(con, spec["name"]))
    need = [v for k, v in spec["columns"].items() if v and v != "rowid"]
    missing = [c for c in need if c not in have]
    if missing:
        return f"表 {spec['name']} 缺少列 {missing}（库结构变了？请重跑初始化探测）"
    if not spec["columns"].get("timestamp") or not spec["columns"].get("content"):
        return f"表 {spec['name']} 的 schema 缺少时间戳或正文列"
    return None


def _chat_name(spec: dict, chat_id: str, group_names: dict[str, str]) -> str:
    if chat_id and chat_id in group_names:
        return group_names[chat_id]
    kind = spec.get("chat_type", "unknown")
    if kind == "group":
        return f"QQ群{chat_id}" if chat_id else "QQ群(未知)"
    if kind == "c2c":
        return f"私聊{chat_id}" if chat_id else "QQ私聊(未知)"
    return f"会话{chat_id}" if chat_id else "QQ会话(未知)"


def _sender_text(name: str, uid: str) -> str:
    name = (name or "").strip()
    uid = (uid or "").strip()
    if name and uid and name != uid:
        return f"{name}({uid})"
    return name or uid or "未知发送人"


def _load_names(db_path: Path, key: str, tmp_dir: Path):
    """加载群名/昵称映射。失败一律降级为空映射，只记一条提示。"""
    try:
        self_uin = (common.load_env().get("QQ_TARGET_UIN", "") or "").strip()
        names = qqnames.load(db_path.parent, key, tmp_dir / "names_db",
                             profile=None, self_uin=self_uin)
        if names.ok:
            return names, ""
        return names, (names.note or "昵称库未就绪（不影响清单，只影响显示真名）")
    except Exception as exc:  # noqa: BLE001
        return None, f"昵称库加载异常（已忽略）：{exc}"


def _collect(con, schema: dict, since: float, until: float, group_names: dict[str, str],
             notes: list[str], names: "qqnames.Names | None" = None):
    messages: list[dict] = []
    used: list[str] = []
    seen: set[str] = set()
    raw_in_window = 0      # 窗口内 SQL 取到的原始行数（过滤前）
    dropped = 0            # 被"空内容 / 表情 / 系统提示 / 重复"筛掉的
    max_ts = 0.0           # 本次真正读到的最新消息时间
    core_tables = 0
    successful_queries = 0
    query_errors = 0
    truncated = False

    skipped: list[str] = []
    for spec in schema.get("tables", []):
        if spec.get("name") not in CORE_MSG_TABLES:
            skipped.append(spec.get("name", "?"))
            continue
        core_tables += 1
        err = _validate_spec(con, spec)
        if err:
            query_errors += 1
            notes.append(err)
            continue
        unit = spec.get("timestamp_unit", "s")
        mult = 1000 if unit == "ms" else 1
        lo = int(since * mult)
        hi = int((until + CLOCK_SKEW_TOL) * mult)   # 容差见 CLOCK_SKEW_TOL 注释
        try:
            rows = _fetch_rows(con, spec, lo, hi, notes)
        except sqlite3.DatabaseError as exc:
            query_errors += 1
            notes.append(f"表 {spec['name']} 查询失败: {exc}")
            continue
        successful_queries += 1
        if len(rows) >= TABLE_FETCH_CAP:
            truncated = True
        if rows:
            used.append(spec["name"])

        mode = spec.get("content_mode", "auto")
        for _id, _ts, _content, _chat, _sender, _name in rows:
            ts = _ts_to_epoch(_ts, unit)
            if ts is None or ts < since or ts > until + CLOCK_SKEW_TOL:
                continue
            raw_in_window += 1
            text = msgparse.polish(msgparse.decode_content(_content, mode=mode))
            if not text or msgparse.is_noise(text):
                dropped += 1
                continue
            chat_id = "" if _chat is None else str(_chat).strip()
            sender_uid = "" if _sender is None else str(_sender).strip()
            sender_name = "" if _name is None else str(_name).strip()
            if not chat_id and spec.get("chat_type") == "c2c":
                # 单聊没有独立会话列时，对方就是发送人，用他当会话标识
                chat_id = sender_uid or sender_name
            if not chat_id:
                dropped += 1
                continue
            mid = "r" + str(_id) if spec["columns"]["msg_id"] == "rowid" else str(_id)
            key = f"{spec['name']}:{mid}"
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
            max_ts = max(max_ts, ts)
            # 真名优先：群名表 -> 群名片 -> 昵称 -> 退回 uid
            pretty_group = names.group_name(chat_id) if names else ""
            is_c2c = spec.get("chat_type") == "c2c"
            if pretty_group:
                chat_label = pretty_group
            elif is_c2c:
                # 会话名必须用"对方"（chat_id=40021），绝不能把 sender_uid 传进去：
                # 本人自己发的消息里 sender_uid 就是本人，那样会把会话显示成自己。
                peer = names.person("", chat_id) if names else ""
                if not peer and not (names and names.is_me(sender_uid)):
                    peer = sender_name
                chat_label = f"私聊·{peer}" if peer else _chat_name(spec, chat_id, group_names)
            else:
                chat_label = _chat_name(spec, chat_id, group_names)

            if names and names.is_me(sender_uid):
                pretty_sender = "我（本人）"
            else:
                pretty_sender = names.person(chat_id, sender_uid) if names else ""
            messages.append({
                "source": "QQ",
                "chat_type": spec.get("chat_type", "unknown"),
                "chat_id": chat_id,
                "chat_name": chat_label,
                "sender": pretty_sender or _sender_text(sender_name, sender_uid),
                "timestamp": common.fmt(ts),
                "msg_id": key,
                "content": text,
                "_ts": ts,
            })

    messages.sort(key=lambda m: m["_ts"])
    if len(messages) > MAX_MESSAGES:
        truncated = True
        notes.append(f"命中 {len(messages)} 条，按上限截取最近 {MAX_MESSAGES} 条；游标保持")
        messages = messages[-MAX_MESSAGES:]
    for m in messages:
        m.pop("_ts", None)
    # 只有在"schema 里根本没固化核心消息表"时才提示缺表；
    # 明明有表只是这段时间没消息，属于正常，别误导成故障
    has_core = any(s.get("name") in CORE_MSG_TABLES for s in schema.get("tables", []))
    if not has_core:
        notes.append(
            "表结构里没有可用的消息表（已跳过：" + ", ".join(skipped[:6]) + "），"
            "请重跑 Admin_Init.bat 重新固化表结构"
        )
    stats = {
        "raw": raw_in_window,
        "dropped": dropped,
        "max_ts": max_ts,
        "core_tables": core_tables,
        "successful_queries": successful_queries,
        "query_errors": query_errors + (1 if not has_core else 0),
        "truncated": truncated,
        "incomplete": bool(truncated or query_errors or not has_core or successful_queries == 0),
    }
    return messages, used, stats


# 主入口
def run(
    hours: float | None = None,
    db_path: Path | None = None,
    schema_path: Path | None = None,
    out_json: Path | None = None,
    env_path: Path | None = None,
    now_ts: float | None = None,
    ignore_cursor: bool = False,
) -> Result:
    common.ensure_dirs()
    env = common.load_env(env_path)
    schema_path = schema_path or (common.get_schema_path() if hasattr(common, "get_schema_path") else common.SCHEMA_PATH)
    out_json = out_json or common.MATERIAL_PATH
    # 空值要归一成 None：Path("") == Path(".") 且 exists() 恒真，
    # 会让下面的"找不到就自动定位"分支永远不触发。
    _declared = common.safe_path(env.get("QQ_DB_PATH", ""))
    db_path = db_path or _declared
    key = (env.get("QQ_DBKEY", "") or "").strip()
    uin = env.get("QQ_TARGET_UIN", "").strip()
    if db_path is None or not db_path.exists():
        try:
            from health import locate_qq_db
            auto_uin, auto_path, ok = locate_qq_db(env)
            if ok:
                db_path = Path(auto_path)
                if not uin:
                    uin = auto_uin
        except Exception:
            pass
    if db_path is None:
        # 既没配置也自动定位不到：保持旧行为（后续按"密钥未就绪/无法读取"报错），
        # 但至少不再把当前目录当成数据库路径。
        db_path = Path(env.get("QQ_DB_PATH") or ".")
    if not uin:
        # 从 ...\<QQ号>\nt_qq\nt_db\nt_msg.db 里反推账号
        for parent in db_path.parents:
            if parent.name.isdigit():
                uin = parent.name
                break
    preferred = env.get("QQ_CIPHER_PROFILE") or None

    # --- 红线 2：密钥没就绪，直接拒绝产出 ---
    db_display = str(db_path) if key or env.get("QQ_DB_PATH") else "(未设置，需先运行初始化)"
    if not key:
        path = common.write_exception(
            common.EXC_NO_KEY,
            "QQ 数据库密钥为空，无法读取本地聊天记录。\n"
            "按手册要求，此时禁止生成「待办素材.json」，以免把“没读到”误当成“没待办”。",
            f"QQ_DB_PATH={db_display}\nQQ_TARGET_UIN={uin or '(未设置)'}\n"
            "下一步：运行 1-实施员一键初始化包 里的 Admin_Init.bat，由实施员配合点一次 QQ 登录完成取密钥。",
        )
        return Result(level=2, status="NO_KEY", message="QQ 密钥未就绪（未生成素材）", exception=path)
    if not qqdb.key_is_plausible(key):
        path = common.write_exception(
            common.EXC_NO_KEY,
            f"QQ 密钥格式不对：当前 {len(key)} 位，NTQQ 要求 16 位可打印 ASCII 口令（不是 64 位 hex）。",
            f"QQ_DBKEY 当前值（已脱敏）: {common.mask(key)}",
        )
        return Result(level=2, status="NO_KEY", message="QQ 密钥格式不对（未生成素材）", exception=path)

    # --- 库文件在不在 ---
    if not db_path or not db_path.exists():
        path = common.write_exception(
            common.EXC_NO_DB,
            "找不到 QQ 消息库文件，可能 QQ 被卸载/重装，或聊天数据目录被移动。",
            f"期望路径: {db_path}\n"
            "请确认 QQ 已登录过一次，或联系维护人员重新运行初始化工具。",
        )
        return Result(level=2, status="FAIL", message="QQ 消息库文件丢失", exception=path)

    # --- schema 在不在 ---
    if not schema_path.exists():
        path = common.write_exception(
            common.EXC_NO_SCHEMA,
            "还没有表结构配置 config/schema.json，无法安全查询消息表。\n"
            "盲猜 NTQQ 的数字列名（40001/40800 等）等于编造数据，因此这里直接停止。",
            "下一步：运行初始化工具（Admin_Init.bat）完成表结构探测。",
        )
        return Result(level=2, status="FAIL", message="表结构未适配", exception=path)
    try:
        schema = common.read_json(schema_path)
    except (json.JSONDecodeError, OSError) as exc:
        path = common.write_exception(common.EXC_NO_SCHEMA, f"schema.json 读不出来: {exc}")
        return Result(level=2, status="FAIL", message="schema.json 损坏", exception=path)
    if not schema.get("tables"):
        path = common.write_exception(
            common.EXC_NO_SCHEMA,
            "schema.json 里没有可用消息表（上次探测没识别出任何表）。",
            "请重新运行初始化工具，并查看 work/schema.txt 的样本行。",
        )
        return Result(level=2, status="FAIL", message="表结构里没有可用消息表", exception=path)

    # --- 新鲜度 ---
    fresh = freshness(db_path, now_ts)
    if fresh["level"] == 1:
        common.log(f"  [注意] {fresh['reason']}")

    # --- 拷库 + 解密 ---
    since, until, why = common.window(hours, overlap_sec=600, ignore_cursor=ignore_cursor)
    if now_ts:  # 自测时固定时间基准
        until = now_ts
        since = now_ts - float(hours or common.DEFAULT_HOURS) * 3600
        why = f"测试基准时间往前 {hours or common.DEFAULT_HOURS:g} 小时"
    common.log(f"  时间窗口: {common.since_text(since, until)}（{why}）")

    tmp_dir = common.TMP_DB
    shutil.rmtree(tmp_dir, ignore_errors=True)
    con = None
    try:
        try:
            con, profile, _stripped = qqdb.open_encrypted(db_path, tmp_dir, key, preferred)
            common.log(f"  [OK] 解密成功（{profile}）")
            if profile and profile != preferred:
                common.save_env({"QQ_CIPHER_PROFILE": profile}, env_path)
        except qqdb.DecryptError as exc:
            path = common.write_exception(
                common.EXC_DECRYPT,
                "QQ 本地消息库读取失败（解密不成功）。常见原因：\n"
                "  1. QQ 升级过版本，加密参数或表结构变了；\n"
                "  2. QQ 重新登录后数据库密钥轮换了；\n"
                "  3. 聊天数据目录被移动或重装过。",
                f"技术信息: {exc}\nQQ_DB_PATH={db_path}",
            )
            return Result(level=2, status="FAIL", message="QQ 解密失败", exception=path)

        group_names = _load_group_names(schema)
        notes: list[str] = []
        # 昵称/群名是"锦上添花"：解不开也必须继续，不允许因此让清单为空
        names, nick_note = _load_names(db_path, key, tmp_dir)
        if nick_note:
            notes.append(nick_note)
        messages, used, stats = _collect(con, schema, since, until, group_names, notes, names)
        for n in notes:
            common.log(f"  [注意] {n}")
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass
        # 临时副本即用即删，不在磁盘上留明文/密文残留
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if not stats.get("successful_queries"):
        path = common.write_exception(
            common.EXC_NO_SCHEMA,
            "所有核心消息表都未能成功查询，本轮没有可信采集结果。",
            "\n".join(notes),
        )
        return Result(
            level=2,
            status="FAIL",
            message="核心消息表全部查询失败，游标保持不变",
            exception=path,
            meta={"notes": notes},
            until=until,
            data_state="unavailable",
        )

    stale = fresh["level"] == 1
    coverage_incomplete = bool(stats.get("incomplete"))
    degraded = stale or coverage_incomplete
    status = "STALE" if degraded else "OK"

    raw_count = stats.get("raw", 0)
    dropped = stats.get("dropped", 0)
    data_state = classify_state(degraded, raw_count, len(messages))

    meta = {
        "source": "QQ",
        "target_uin": uin,
        "cutoff_time": datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M:%S"),
        "time_range": common.since_text(since, until),
        "status": status,
        "stale_warning": degraded,
        "coverage_incomplete": coverage_incomplete,
        "message_count": len(messages),
        "data_state": data_state,
        "raw_count": raw_count,
        "dropped_count": dropped,
        "latest_msg_time": (
            datetime.fromtimestamp(stats["max_ts"]).strftime("%Y-%m-%d %H:%M:%S")
            if stats.get("max_ts") else ""
        ),
        # 以下为便于排查的扩展字段，不影响上游读取必需字段
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cipher_profile": preferred or "",
        "tables_used": used,
        "notes": notes,
        "window_reason": why,
        "db_path": str(db_path),
    }
    if stale:
        meta["stale_reason"] = fresh["reason"]

    payload = {"meta": meta, "messages": messages}
    common.write_json(out_json, payload)
    # 告警清理由多平台聚合器按通道统一处理；QQ 成功不得删除其他平台故障说明。
    removed: list[str] = []

    if data_state == "offline":
        msg = (
            f"🟡 QQ 电脑端离线：本地库 {fresh['reason']}，读不到新消息（本轮 {len(messages)} 条）。"
            "请登录电脑 QQ，新消息会自动补齐，不会丢。"
        )
    elif data_state == "no_new":
        msg = f"🟢 QQ 在线（{fresh['reason']}），本时段确实没有新消息（0 条）"
    elif data_state == "only_noise":
        msg = (
            f"🟢 QQ 在线，扫描到 {raw_count} 条，"
            f"其中 {dropped} 条是表情/卡片/系统提示，没有可提炼的内容"
        )
    else:
        msg = f"🟢 QQ 采集正常，共 {len(messages)} 条（窗口 {common.since_text(since, until)}）"
    common.log(f"  {msg}")
    if removed:
        common.log(f"  已清理旧告警 {len(removed)} 份")

    return Result(
        level=1 if degraded else 0,
        status=status,
        count=len(messages),
        stale_warning=degraded,
        message=msg,
        meta=meta,
        messages=messages,
        tables_used=used,
        raw_count=raw_count,
        dropped_count=dropped,
        max_ts=float(stats.get("max_ts") or 0.0),
        until=until,
        data_state=data_state,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="抽取 NTQQ 增量消息，生成 work/待办素材.json")
    ap.add_argument("--hours", type=float, help="回看小时数（默认 4，或按上次游标）")
    ap.add_argument("--db", help="nt_msg.db 路径")
    ap.add_argument("--schema", help="schema.json 路径")
    ap.add_argument("--out", help="素材输出路径")
    ap.add_argument("--env", help=".env 路径（自测用）")
    args = ap.parse_args()

    common.head("QQ 增量抽取")
    res = run(
        hours=args.hours,
        db_path=Path(args.db) if args.db else None,
        schema_path=Path(args.schema) if args.schema else None,
        out_json=Path(args.out) if args.out else None,
        env_path=Path(args.env) if args.env else None,
    )
    if res.exception:
        common.log(f"  已写异常说明: {res.exception}")
    return 0 if res.ok else 3


if __name__ == "__main__":
    sys.exit(main())
