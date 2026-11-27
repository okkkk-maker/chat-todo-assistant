"""NTQQ nt_msg.db 的只读复制、剥头、解密与参数探测。

三条硬约束（来自手册红线，改代码前先读）：
    1. 绝不碰原位库：先把 db + -wal + -shm 拷到 work/tmp_db/ 再动手，
       原目录里不建任何临时文件、不加锁。
    2. 剥头偏移固定 1024，并校验 offset 1016-1023 是 8 个零字节、
       剥完大小是 4096 的整数倍 —— 校验不过就报错，不猜。
    3. 密钥只接受 16 位可打印 ASCII 口令；探测按三组 PRAGMA 依次试，
       哪组能读出 sqlite_master 就记哪组。
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

import common

QQ_HEADER = 1024
PAGE_SIZE = 4096

# (profile 名, 语句模板) —— {key} 会被替换成转义后的口令
#
#  顺序是硬坑，2026-10-29 在真机 QQ 9.9.36 上实测确认：
#   SQLCipher 的 codec 是惰性的，`PRAGMA key` 只是记下口令，
#   真正派生要等第一次读页（openssl 那句 SELECT）。
#   所以「key 在前、参数在后、不重复喂 key」才能让自定义参数生效；
#   反过来「先参数后 key」会被静默忽略，退回 SQLCipher 4 默认值（256000/SHA512），
#   于是无论口令对不对都报 `file is not a database` —— 这就是之前"抓不到密钥"的真因。
ATTEMPTS: list[tuple[str, list[str]]] = [
    # QQNT 官方组合：page 4096 + kdf_iter 4000 + HMAC_SHA1。首选，一次命中。
    (
        "ntqq_official",
        [
            "PRAGMA key = '{key}';",
            "PRAGMA cipher_page_size = 4096;",
            "PRAGMA kdf_iter = 4000;",
            "PRAGMA cipher_hmac_algorithm = HMAC_SHA1;",
        ],
    ),
    # 同上但补上 kdf 算法，个别版本需要显式写成 SHA1
    (
        "ntqq_official_sa1",
        [
            "PRAGMA key = '{key}';",
            "PRAGMA cipher_page_size = 4096;",
            "PRAGMA kdf_iter = 4000;",
            "PRAGMA cipher_hmac_algorithm = HMAC_SHA1;",
            "PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA1;",
        ],
    ),
    # 旧版 QQNT（9.9 之前）：默认 4096 页 + SQLCipher 4 默认 KDF
    ("ntqq_v4", ["PRAGMA key = '{key}';", "PRAGMA cipher_page_size = 4096;"]),
    # 兜底：SQLCipher 3 兼容档
    ("compat3", ["PRAGMA key = '{key}';", "PRAGMA cipher_compatibility = 3;"]),
    # 最后的默认参数
    ("default", ["PRAGMA key = '{key}';"]),
    # 少数版本把 16 字节直接当裸密钥用（x'hex'），不走 PBKDF2
    (
        "raw_hex",
        ["PRAGMA key = \"x'{hexkey}'\";", "PRAGMA cipher_page_size = 4096;", "PRAGMA kdf_iter = 4000;"],
    ),
]

PROFILE_NAMES = tuple(name for name, _ in ATTEMPTS)


class DecryptError(RuntimeError):
    """解密失败（口令不对或参数不在已探测范围）。"""


def key_is_plausible(key: str) -> bool:
    """NTQQ 的 dbkey 是 16 字节可打印 ASCII 口令，不是 hex。"""
    return len(key) == 16 and all(32 <= ord(c) <= 126 for c in key)


def _sqlcipher():
    try:
        from sqlcipher3 import dbapi2 as sqlcipher  # type: ignore
        return sqlcipher
    except ImportError as exc:  # pragma: no cover
        raise DecryptError(
            "缺少 SQLCipher 组件：执行 .venv\\Scripts\\python.exe -m pip install sqlcipher3-wheels"
        ) from exc


# 1. 拷贝
def copy_db_set(src: Path, dst_dir: Path, stem: str = "raw_nt_msg.db", retries: int = 3) -> dict[str, Path | None]:
    """把 db / -wal 拷到 dst_dir，返回各文件路径（shm 恒为 None，见下）。

    QQ 在跑的时候文件一直在写，可能拷到撕裂的副本，所以拷两遍比对大小，
    不稳定就重试。

     不复制 -shm：它是跨进程共享的内存索引，SQLite 规定不可跨进程共享，
    打开 WAL 库时会根据 -wal 自动重建。强行复制只会把别的进程的锁状态带进来，
    导致 `database disk image is malformed` 或再度踩到 WinError 32。
    """
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / stem

    def _stable_copy(s: Path, d: Path) -> None:
        for i in range(retries):
            before = s.stat().st_size
            shutil.copy2(s, d)
            after = s.stat().st_size
            if abs(d.stat().st_size - min(before, after)) <= PAGE_SIZE:
                return
            time.sleep(0.4)
        # 实在不稳定也保留最后一次，后续解密失败会另给原因

    _stable_copy(src, dst)
    result: dict[str, Path | None] = {"db": dst, "wal": None, "shm": None}
    wal = Path(str(src) + "-wal")
    if wal.exists():
        d = Path(str(dst) + "-wal")
        try:
            shutil.copy2(wal, d)
            result["wal"] = d
        except OSError:
            result["wal"] = None
    return result


def inspect_header(src: Path) -> tuple[bool, str]:
    """校验 1024 字节自定义头。返回 (通过, 说明)。"""
    size = src.stat().st_size
    if size <= QQ_HEADER:
        return False, f"文件只有 {size} 字节，比自定义头还小"
    with open(src, "rb") as f:
        f.seek(1016)
        tail = f.read(8)
    if tail != b"\x00" * 8:
        return False, (
            f"offset 1016-1023 不是 8 个零字节（实际 {tail.hex()}），"
            "自定义头可能不是 1024 字节，不敢猜，请人工核对"
        )
    body = size - QQ_HEADER
    if body % PAGE_SIZE != 0:
        return False, (
            f"剥头后 {body} 字节不是 {PAGE_SIZE} 的整数倍，"
            "说明偏移不对或文件正在被写入，请稍后重试"
        )
    return True, f"头校验通过（{size} -> {body} 字节，{body // PAGE_SIZE} 页）"


def strip_header(src: Path, dst: Path) -> Path:
    """剥离 1024 字节头，产出标准 SQLCipher 文件。校验不过直接抛错。

    src 必须是**副本**（调用方已拷进沙箱）；这里只读 src，写 dst。
    只跟随 -wal（它承载尚未 checkpoint 的新消息），不跟随 -shm（SQLite 自动重建）。
    """
    ok, msg = inspect_header(src)
    if not ok:
        raise DecryptError(f"剥头校验失败：{msg}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        fi.seek(QQ_HEADER)
        shutil.copyfileobj(fi, fo, length=1024 * 1024)
    # WAL 跟着改名，否则读不到未 checkpoint 的新消息
    wal = Path(str(src) + "-wal")
    if wal.exists():
        try:
            shutil.copy2(wal, Path(str(dst) + "-wal"))
        except OSError:
            pass
    return dst


# 2. 解密
def _apply(con, stmts: list[str], key: str) -> None:
    escaped = key.replace("'", "''")
    hexkey = key.encode("utf-8").hex()
    for stmt in stmts:
        con.execute(stmt.format(key=escaped, hexkey=hexkey))


def try_open(path: Path, key: str, stmts: list[str]):
    """按给定语句组合打开并读一次 sqlite_master。成功返回连接（调用方负责关）。"""
    sqlcipher = _sqlcipher()
    con = sqlcipher.connect(str(path))
    try:
        _apply(con, stmts, key)
        n = con.execute("SELECT count(*) FROM sqlite_master;").fetchone()[0]
        if not n:
            con.close()
            return None
    except Exception:  # noqa: BLE001
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass
        return None
    return con


def detect_profile(path: Path, key: str, preferred: str | None = None):
    """依次试三组参数，返回 (profile 名, 已打开的连接)。

    失败抛 DecryptError。preferred 命中的组合会优先试，省时间。
    """
    if not key_is_plausible(key):
        raise DecryptError(
            f"密钥格式不对：长度 {len(key)}，NTQQ 要求 16 位可打印 ASCII 口令"
        )
    order = list(ATTEMPTS)
    if preferred:
        order.sort(key=lambda a: 0 if a[0] == preferred else 1)

    errors: list[str] = []
    for name, stmts in order:
        con = try_open(path, key, stmts)
        if con is not None:
            return name, con
        errors.append(name)
    raise DecryptError(
        "三组参数都打不开：口令不对，或该 QQ 版本用了未知加密参数。\n"
        f"已尝试: {', '.join(dict.fromkeys(errors))}"
    )


# 注意：v1.5.3.2 里这里还有一个 export_plaintext()，把加密库整个导出成明文库，
# 安全性只靠注释里"用完即删"的口头承诺，而且全仓没有任何调用者。已删除：
# 一个没有任何用途、却能把全部聊天记录写成明文的函数，留着就是隐患。
# 若将来排查确实需要，请落到系统临时目录并在同一次调用内删除。


# 3. 便捷：一步到位打开（拷贝 -> 剥头 -> 解密）
def open_encrypted(
    src_db: Path,
    tmp_dir: Path,
    key: str,
    preferred: str | None = None,
    stem: str = "raw_nt_msg.db",
) -> tuple[object, str, Path]:
    """完整流程打开原位库的临时副本。返回 (连接, profile, 剥头文件路径)。

    调用方用完必须 close()，并清理 tmp_dir。
    """
    files = copy_db_set(src_db, tmp_dir, stem=stem)
    raw = files["db"]
    assert raw is not None
    stripped = strip_header(raw, tmp_dir / ("stripped_" + stem))
    profile, con = detect_profile(stripped, key, preferred)
    return con, profile, stripped


def open_plain(path: Path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def table_names(con) -> list[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def columns(con, table: str) -> list[str]:
    return [c[1] for c in con.execute(f'PRAGMA table_info({common.quote_ident(table)})')]


def row_count(con, table: str) -> int:
    try:
        return con.execute(f'SELECT count(*) FROM {common.quote_ident(table)}').fetchone()[0]
    except sqlite3.DatabaseError:
        return -1


def profile_statements(profile: str | None = None) -> list[str]:
    """按 profile 名返回对应的 PRAGMA 语句（已剔除 key 语句，供辅助库复用）。

    辅助库（group_info.db / profile_info.db）与主库用同一套加密参数，
    但不需要重新探测 —— 主库定了哪个，辅助库照抄即可。
    """
    for name, stmts in ATTEMPTS:
        if name == profile:
            rest = [s for s in stmts if "PRAGMA key" not in s]
            if rest:
                return rest
            break
    # 找不到或该 profile 本身不带参数 -> 用 QQNT 官方组合
    return [
        "PRAGMA cipher_page_size = 4096;",
        "PRAGMA kdf_iter = 4000;",
        "PRAGMA cipher_hmac_algorithm = HMAC_SHA1;",
    ]
