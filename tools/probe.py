"""解密 NTQQ nt_msg.db，探测真实表结构，固化 config/schema.json。

严格顺序（对应手册 A3，也是 L3 红线的落地）：
    1. db + -wal + -shm 拷到 work/tmp_db/
    2. 剥 1024 字节头，并做两项校验
    3. 依次试三组 SQLCipher 参数，命中即记 profile
    4. 遍历所有表，按数值分布自动识出
          消息ID / 会话ID / 发送人 / 时间戳 / 正文
       对应的真实列名（NTQQ 是 40001、40800 这类数字列）
    5. 写 config/schema.json + work/schema.txt（人看的版本）

没有密钥 / 解不开 -> 直接退出，绝不写 schema、绝不碰抽取。

用法:
    .venv\\Scripts\\python.exe tools\\probe.py
    .venv\\Scripts\\python.exe tools\\probe.py --db "E:\\Tencent Files\\xxx\\nt_qq\\nt_db\\nt_msg.db" --key abcdefgh12345678
    .venv\\Scripts\\python.exe tools\\probe.py --dump-only     # 只打印现有 schema.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
import msgparse  # noqa: E402
import qqdb  # noqa: E402

SAMPLE_ROWS = 300
PLAUSIBLE_TS_SEC = (1_400_000_000, 2_200_000_000)      # 约 2014 ~ 2039
PLAUSIBLE_TS_MS = (1_400_000_000_000, 2_200_000_000_000)


# 列类型推断
def _as_int(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit() and len(v.strip()) <= 20:
        return int(v.strip())
    return None


def _as_text(v) -> str | None:
    if isinstance(v, str):
        return v
    if isinstance(v, (bytes, bytearray, memoryview)):
        return None
    return None


def _int_stats(values: list[int]) -> dict:
    return {
        "n": len(values),
        "distinct": len(set(values)),
        "min": min(values) if values else 0,
        "max": max(values) if values else 0,
    }


def _unique_ratio(values: list[int | str]) -> float:
    if not values:
        return 0.0
    return len(set(values)) / len(values)


def _ts_hits(values: list[int], lo: int, hi: int) -> float:
    if not values:
        return 0.0
    return sum(1 for v in values if lo <= v <= hi) / len(values)


# NTQQ 的列名是固定数字，社区资料与 2026-10-29 真机实测（QQ 9.9.36）一致。
# 先验命中且通过校验就直接用，未命中再退回下面的数值分布探测。
# 这一层很关键：纯靠"可解码文本长度"打分时，偶尔出现的引用片段列（40900，非空率仅 5%）
# 会因为单条文本很长而挤掉真正的正文列（40800，非空率 99%）。
NTQQ_PRIOR: dict[str, tuple[str, ...]] = {
    "msg_id": ("40001",),
    "timestamp": ("40050",),
    "content": ("40800",),
    "sender": ("40020",),
    "sender_name": ("40093",),
    "chat_id_group": ("40027", "40021"),
    "chat_id_c2c": ("40021", "40027"),
}


def _nonnull_ratio(con, name: str, col: str) -> float:
    """整表非空率（空串算空）。采样有偏差，判断"这列到底有没有内容"必须看全表。"""
    q_table = common.quote_ident(name)
    q_col = common.quote_ident(col)
    try:
        total = con.execute(f"SELECT count(*) FROM {q_table}").fetchone()[0]
        if not total:
            return 0.0
        filled = con.execute(
            f"SELECT count(*) FROM {q_table} "
            f"WHERE {q_col} IS NOT NULL AND CAST({q_col} AS TEXT) <> ''"
        ).fetchone()[0]
        return filled / total
    except Exception:  # noqa: BLE001
        return 0.0


# 单表分析
def analyze_table(con, name: str, rows: int) -> dict | None:
    try:
        cur = con.execute(f"SELECT * FROM {common.quote_ident(name)} LIMIT {SAMPLE_ROWS}")
        heads = [d[0] for d in cur.description]
        data = cur.fetchall()
    except Exception:  # noqa: BLE001
        return None
    if not data:
        return None

    cols: dict[str, list] = {h: [] for h in heads}
    for row in data:
        for h, v in zip(heads, row):
            cols[h].append(v)

    chat_type = (
        "group" if "group" in name.lower()
        else ("c2c" if any(k in name.lower() for k in ("c2c", "buddy", "friend")) else "unknown")
    )

    # ---------- 时间戳 ----------
    ts_col, ts_unit, ts_score = None, "s", 0.0
    for h in NTQQ_PRIOR["timestamp"]:          # 先验优先
        if h not in cols:
            continue
        ints = [i for i in (_as_int(v) for v in cols[h]) if i is not None]
        if len(ints) < len(cols[h]) * 0.7:
            continue
        for unit, (lo, hi) in (("s", PLAUSIBLE_TS_SEC), ("ms", PLAUSIBLE_TS_MS)):
            hit = _ts_hits(ints, lo, hi)
            if hit > 0.8:
                ts_col, ts_unit, ts_score = h, unit, hit
                break
        if ts_col:
            break

    if ts_col is None:                          # 自动探测兜底
        for h in heads:
            ints = [i for i in (_as_int(v) for v in cols[h]) if i is not None]
            # 小表（刚上线的新号）也要能识别：样本 ≤4 条时允许 2 条整数即可
            need = 2 if len(cols[h]) <= 4 else max(3, len(cols[h]) * 0.7)
            if len(ints) < need:
                continue
            for unit, (lo, hi) in (("s", PLAUSIBLE_TS_SEC), ("ms", PLAUSIBLE_TS_MS)):
                hit = _ts_hits(ints, lo, hi)
                if hit > 0.8 and hit > ts_score:
                    ts_col, ts_unit, ts_score = h, unit, hit
    if ts_col is None:
        return None  # 没有时间戳的表对“最近 N 小时”没有意义

    # ---------- 消息 ID ----------
    id_col, id_uniq = None, 0.0
    for h in NTQQ_PRIOR["msg_id"]:              # 先验优先
        if h not in cols or h == ts_col:
            continue
        ints = [i for i in (_as_int(v) for v in cols[h]) if i is not None]
        if len(ints) < len(cols[h]) * 0.7 or not ints:
            continue
        ur = _unique_ratio(ints)
        if ur >= 0.92 and max(ints) > 1000:
            id_col, id_uniq = h, ur

    if id_col is None:
        for h in heads:
            if h == ts_col:
                continue
            ints = [i for i in (_as_int(v) for v in cols[h]) if i is not None]
            if len(ints) < len(cols[h]) * 0.7 or not ints:
                continue
            ur = _unique_ratio(ints)
            if ur >= 0.92 and ur > id_uniq:
                # 越大越像消息号（自增/雪花id），排除小范围序号
                if max(ints) > 1000:
                    id_col, id_uniq = h, ur
    msg_id_source = "column"
    if id_col is None:
        # 退回 rowid：可溯源，但要在 schema 里标明来源
        msg_id_source = "rowid"
        id_col = "rowid"

    # ---------- 正文 ----------
    # 先验列必须通过"整表非空率"校验，否则退回自动探测。
    content_col, content_score, content_mode = None, 0.0, "auto"
    for h in NTQQ_PRIOR["content"]:
        if h not in cols or h in (ts_col, id_col):
            continue
        if _nonnull_ratio(con, name, h) < 0.5:
            continue
        vals = [v for v in cols[h] if v is not None]
        texts = [t for t in (msgparse.decode_content(v) for v in vals[:60]) if t]
        if len(texts) >= max(1, len(vals[:60]) * 0.5):
            content_col = h
            content_score = 5.0
            content_mode = "protobuf" if any(isinstance(v, (bytes, bytearray)) for v in vals[:20]) else "auto"
            break

    if content_col is None:
        for h in heads:
            if h in (ts_col, id_col):
                continue
            vals = [v for v in cols[h] if v is not None]
            if not vals:
                continue
            if all(isinstance(v, int) for v in vals[:20]):
                continue  # 纯整数列不可能是正文
            # 全表非空率太低的多半是"引用/特殊消息"列，不是正文
            full_ratio = _nonnull_ratio(con, name, h)
            if full_ratio < 0.3:
                continue
            texts: list[str] = []
            json_hits = 0
            for v in vals[:60]:
                t = msgparse.decode_content(v)
                if msgparse.looks_like_json(v):
                    json_hits += 1
                if t:
                    texts.append(t)
            if not texts:
                continue
            decodable = len(texts) / max(1, len(vals[:60]))
            avg_len = sum(len(t) for t in texts) / len(texts)
            cjk = sum(1 for t in texts if msgparse.CJK_RE.search(t))
            score = decodable * 2.0 + min(avg_len, 200) / 100.0 + (cjk / len(texts)) * 2.0
            score += 1.5 if json_hits else 0.0
            score += 2.0 * full_ratio          # 覆盖率高才是正文
            if score > content_score:
                content_col, content_score = h, score
                content_mode = "json" if json_hits >= max(1, len(vals[:60]) * 0.5) else "auto"
    if content_col is None:
        return None

    # ---------- 会话 ID ----------
    chat_priors = NTQQ_PRIOR["chat_id_group"] if chat_type != "c2c" else NTQQ_PRIOR["chat_id_c2c"]
    chat_col, chat_score = None, 0.0
    for h in chat_priors:                       # 先验优先
        if h not in cols or h in (ts_col, id_col, content_col):
            continue
        if _nonnull_ratio(con, name, h) < 0.5:
            continue
        chat_col = h
        break

    if chat_col is None:
        for h in heads:
            if h in (ts_col, id_col, content_col):
                continue
            vals = [v for v in cols[h] if v is not None]
            if not vals:
                continue
            ints = [i for i in (_as_int(v) for v in vals) if i is not None]
            if len(ints) >= len(vals) * 0.8:
                ur = _unique_ratio(ints)
                # 会话 ID：会重复、但不会全部相同；群号一般是 6~12 位
                if 0.002 <= ur <= 0.75:
                    score = (1 - abs(ur - 0.1)) + (1.0 if 5 <= len(str(max(ints))) <= 13 else 0)
                    if score > chat_score:
                        chat_col, chat_score = h, score
            else:
                texts = [t for t in (_as_text(v) for v in vals) if t]
                if texts and len(texts) >= len(vals) * 0.8:
                    ur = _unique_ratio(texts)
                    if 0.002 <= ur <= 0.9:
                        score = (1 - abs(ur - 0.1)) + 0.5
                        if score > chat_score:
                            chat_col, chat_score = h, score
    if chat_col is None:
        chat_col = ""

    # ---------- 发送人 ----------
    sender_col, sender_name_col = None, None
    best_sender, best_name = 0.0, 0.0
    for h in NTQQ_PRIOR["sender"]:              # 先验优先
        if h not in cols or h in (ts_col, id_col, content_col, chat_col):
            continue
        vals = [t for t in (_as_text(v) for v in cols[h]) if t]
        if len(vals) < len(cols[h]) * 0.6 or not vals:
            continue
        sender_col = h
        best_sender = 2.0
        break
    for h in NTQQ_PRIOR["sender_name"]:         # 先验优先
        if h not in cols or h in (ts_col, id_col, content_col, chat_col, sender_col):
            continue
        vals = [t for t in (_as_text(v) for v in cols[h]) if t]
        if len(vals) < len(cols[h]) * 0.1 or not vals:
            continue
        cjk_ratio = sum(1 for t in vals if msgparse.CJK_RE.search(t)) / len(vals)
        if cjk_ratio > 0.3:
            sender_name_col = h
            best_name = 2.0
            break

    if sender_col is None:
        for h in heads:
            if h in (ts_col, id_col, content_col, chat_col):
                continue
            vals = [t for t in (_as_text(v) for v in cols[h]) if t]
            if len(vals) < len(cols[h]) * 0.6 or not vals:
                continue
            ur = _unique_ratio(vals)
            cjk_ratio = sum(1 for t in vals if msgparse.CJK_RE.search(t)) / len(vals)
            uid_like = sum(1 for t in vals if t.startswith("u_") or (t.isdigit() and 4 <= len(t) <= 12)) / len(vals)
            if uid_like > 0.6 and ur >= 0.05:
                score = uid_like + min(ur, 1.0)
                if score > best_sender:
                    sender_col, best_sender = h, score
            elif cjk_ratio > 0.5 and ur >= 0.02:
                score = cjk_ratio + min(ur, 1.0)
                if score > best_name:
                    sender_name_col, best_name = h, score

    # 若只找到一个“人名列”，也可当发送人用
    if sender_col is None and sender_name_col is not None:
        sender_col, sender_name_col = sender_name_col, None

    confidence = round(
        min(1.0, 0.3 * ts_score + 0.3 * id_uniq + 0.2 * min(1.0, content_score / 5) + (0.2 if chat_col else 0.0)),
        2,
    )

    return {
        "name": name,
        "rows": rows,
        "chat_type": chat_type,
        "columns": {
            "msg_id": id_col,
            "chat_id": chat_col,
            "sender": sender_col or "",
            "sender_name": sender_name_col or "",
            "timestamp": ts_col,
            "content": content_col,
        },
        "msg_id_source": msg_id_source,
        "timestamp_unit": ts_unit,
        "content_mode": content_mode,
        "confidence": confidence,
        "evidence": {
            "ts_hit_ratio": round(ts_score, 3),
            "msg_id_unique": round(id_uniq, 3),
            "content_score": round(content_score, 2),
            "sampled": len(data),
        },
    }


# 群名映射（可选增强：group_info.db 里通常有群号 -> 群名）
def probe_group_names(key: str, profile: str | None, tmp_dir: Path, qq_db_dir: Path, out_json: Path) -> int:
    src = qq_db_dir / "group_info.db"
    if not src.exists():
        return 0
    sub = tmp_dir / "group_info"
    try:
        con, _prof, _path = qqdb.open_encrypted(src, sub, key, profile, stem="raw_group.db")
    except Exception:  # noqa: BLE001
        return 0

    mapping: dict[str, str] = {}
    try:
        for t in qqdb.table_names(con):
            cols = qqdb.columns(con, t)
            if not cols:
                continue
            try:
                cur = con.execute(f"SELECT * FROM {common.quote_ident(t)} LIMIT 500")
                heads = [d[0] for d in cur.description]
                rows = cur.fetchall()
            except Exception:  # noqa: BLE001
                continue
            if not rows:
                continue
            name_col, id_col = None, None
            for i, h in enumerate(heads):
                vals = [r[i] for r in rows if r[i] is not None]
                if not vals:
                    continue
                texts = [v for v in vals if isinstance(v, str)]
                if texts and name_col is None:
                    cjk = sum(1 for t in texts if msgparse.CJK_RE.search(t)) / len(texts)
                    if cjk > 0.5:
                        name_col = h
                ints = [i2 for i2 in (_as_int(v) for v in vals) if i2 is not None]
                if ints and id_col is None and _unique_ratio(ints) > 0.9:
                    if 5 <= len(str(max(ints))) <= 13:
                        id_col = h
            if name_col and id_col:
                for r in rows:
                    gid = _as_int(r[heads.index(id_col)])
                    gname = r[heads.index(name_col)]
                    if gid and isinstance(gname, str) and gname.strip():
                        mapping[str(gid)] = gname.strip()
    finally:
        con.close()
        shutil.rmtree(sub, ignore_errors=True)

    if mapping:
        common.write_json(out_json, mapping)
    return len(mapping)


# 主流程
def verify_decrypt_and_dump_schema(
    db_path: Path,
    key: str,
    preferred_profile: str | None = None,
    out_schema: Path | None = None,
    dump_txt: Path | None = None,
    save_profile_cb=None,
) -> dict:
    """拷库 -> 剥头 -> 试三组参数 -> 识别列 -> 写 schema.json。

    失败抛 DecryptError（由调用方决定写哪份异常说明）。
    """
    out_schema = out_schema or common.SCHEMA_PATH
    dump_txt = dump_txt or (common.WORK / "schema.txt")

    if not db_path.exists():
        raise qqdb.DecryptError(f"找不到数据库文件: {db_path}")
    if not qqdb.key_is_plausible(key):
        raise qqdb.DecryptError(f"密钥格式不对（{len(key)} 位，应为 16 位 ASCII 口令）")

    common.ensure_dirs()
    tmp_dir = common.TMP_DB
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    con = None
    try:
        con, profile, stripped = qqdb.open_encrypted(db_path, tmp_dir, key, preferred_profile)
        common.log(f"  [OK] 解密成功，参数组合 = {profile}")
        if save_profile_cb:
            save_profile_cb(profile)

        tables = qqdb.table_names(con)
        common.log(f"       可见表 {len(tables)} 张")

        found: list[dict] = []
        dump_lines: list[str] = [
            f"# NTQQ 表结构探测报告",
            f"时间: {datetime.now():%Y-%m-%d %H:%M:%S}",
            f"源库: {db_path}",
            f"参数: {profile}",
            f"表数: {len(tables)}",
            "",
        ]
        for t in tables:
            rows = qqdb.row_count(con, t)
            cols = qqdb.columns(con, t)
            dump_lines.append(f"### {t}   rows={rows}")
            dump_lines.append("    cols: " + ", ".join(cols))
            info = analyze_table(con, t, rows)
            if info:
                found.append(info)
                dump_lines.append(f"    识别: {json.dumps(info['columns'], ensure_ascii=False)}")
                dump_lines.append(f"    chat_type={info['chat_type']} unit={info['timestamp_unit']} conf={info['confidence']}")
            if 0 < rows <= 3000:
                try:
                    cur = con.execute(f"SELECT * FROM {common.quote_ident(t)} LIMIT 2")
                    heads = [d[0] for d in cur.description]
                    for r in cur.fetchall():
                        pairs = []
                        for h, v in zip(heads, r):
                            rep = repr(v)
                            pairs.append(f"{h}={rep[:70]}")
                        dump_lines.append("    row: " + " | ".join(pairs))
                except Exception:  # noqa: BLE001
                    pass
            dump_lines.append("")

        found.sort(key=lambda x: (-x["rows"], -x["confidence"]))
        group_count = 0
        if found:
            try:
                group_count = probe_group_names(
                    key, profile, tmp_dir, db_path.parent, common.WORK / "group_names.json"
                )
            except Exception as exc:  # noqa: BLE001
                dump_lines.append(f"# 群名映射探测失败（不影响抽取）: {exc}")

        schema = {
            "version": 1,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "source": {
                "db": str(db_path),
                "size": db_path.stat().st_size,
                "profile": profile,
                "key_masked": common.mask(key),
            },
            "tables": found,
            "group_names": {
                "count": group_count,
                "file": "work/group_names.json" if group_count else "",
            },
            "diagnostics": [
                f"共扫描 {len(tables)} 张表，识别出 {len(found)} 张可用消息表",
                "列名由数值分布自动识别，未经人工核对前请先看 work/schema.txt 的样本行",
            ],
        }
        common.write_json(out_schema, schema)
        dump_txt.parent.mkdir(parents=True, exist_ok=True)
        dump_txt.write_text("\n".join(dump_lines), encoding="utf-8")

        common.log(f"       schema 已写入 {out_schema}")
        common.log(f"       人看的版本 {dump_txt}")
        for info in found:
            common.log(
                f"       - {info['name']}: {info['rows']} 行, 列映射 "
                + ", ".join(f"{k}={v}" for k, v in info["columns"].items() if v)
            )
        if not found:
            common.log("  [注意] 没识别出任何可用消息表，抽取会拒绝运行（宁可不产出，也不编造）")
        return schema
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass


def print_schema(path: Path) -> None:
    if not path.exists():
        common.log(f"还没有 {path}，请先运行本脚本探测。")
        return
    schema = common.read_json(path)
    common.head("当前 schema.json")
    common.log(json.dumps(schema, ensure_ascii=False, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser(description="NTQQ nt_msg.db 解密与表结构探测")
    ap.add_argument("--db", help="nt_msg.db 路径（默认读 .env 的 QQ_DB_PATH）")
    ap.add_argument("--key", help="16 位 ASCII 密钥（默认读 .env 的 QQ_DBKEY）")
    ap.add_argument("--profile", help="指定参数组合 profile_1/2/3，默认自动试")
    ap.add_argument("--out", help="schema.json 输出路径")
    ap.add_argument("--dump", help="人看的 schema 报告路径")
    ap.add_argument("--dump-only", action="store_true", help="只打印现有 schema.json")
    args = ap.parse_args()

    if args.dump_only:
        print_schema(common.SCHEMA_PATH)
        return 0

    env = common.load_env()
    db_path = Path(args.db or env.get("QQ_DB_PATH", ""))
    key = (args.key or env.get("QQ_DBKEY", "")).strip()
    preferred = args.profile or env.get("QQ_CIPHER_PROFILE") or None

    common.head("NTQQ 表结构探测")
    if not key:
        common.log("  [失败] 没有密钥：先跑 vendor\\1-get-qq-key.bat 拿到 16 位口令，")
        common.log("         或运行 Admin_Init.bat 让向导自动抓取并回填。")
        return 2
    if not db_path or not db_path.exists():
        common.log(f"  [失败] 数据库路径无效: {db_path}")
        return 3

    try:
        schema = verify_decrypt_and_dump_schema(
            db_path,
            key,
            preferred,
            Path(args.out) if args.out else None,
            Path(args.dump) if args.dump else None,
            save_profile_cb=lambda p: common.save_env({"QQ_CIPHER_PROFILE": p}),
        )
    except qqdb.DecryptError as exc:
        common.log(f"\n  [失败] {exc}")
        common.log("         请确认 QQ 处于登录状态后重新提取密钥。")
        return 4
    finally:
        shutil.rmtree(common.TMP_DB, ignore_errors=True)

    return 0 if schema.get("tables") else 5


if __name__ == "__main__":
    sys.exit(main())
