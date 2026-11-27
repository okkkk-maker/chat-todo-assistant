"""微信 4.x 本地库（SQLCipher 4）解密内核 → 生成明文镜像。

为什么需要这一层，而不是直接调 vendor/wechat-key/scripts/windows/decrypt_all.py：
    1. 那个脚本要求 WECHAT_DATA_ROOT 指到 xwechat_files 这一层（它内部按
       `<root>/*/db_storage` 匹配）。而两个调用方（extract_raw_key.auto_decrypt、
       channels/wechat._try_decrypt）都多传了一层，指成了 `<root>/<wxid>_xxx`。
       结果就是「密钥抓到了、解密却一条也没产出」——这是历史上最难查的一类假失败。
    2. 它把所有 *.db 一视同仁地解密，但真实机器上还有 -wal/-shm、临时文件、
       以及部分非 SQLCipher 的库；失败原因全被压成一句 "key/format mismatch"。
    3. 它不区分「密钥不对」和「库正在被写」两类完全不同的错误，排障没有抓手。

本模块的约定：
    * 只读源目录（微信正在运行时也安全），把结果写到自己的镜像目录；
    * 每个库单独 try，逐库回报 OK / KEY_MISMATCH / BUSY / CORRUPT / EMPTY；
    * message_0.db 失败视为整体失败（消息主库都读不了，不能假装成功）；
    * 解出来的明文库与源库同名同相对路径，供 sqlite3 只读查询。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import struct
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from wxcrypto import aes_cbc_decrypt, backend_name

PAGE = 4096
RESERVE = 80          # SQLCipher 4: IV(16) + HMAC-SHA512(64)
SALT_SZ = 16
KDF_ITER = 256000
SQLITE_MAGIC = b"SQLite format 3\x00"

# 结果分类。排障时一眼能看出是「密钥错」还是「库在写」还是「文件坏了」。
OK = "OK"
NO_KEY = "NO_KEY"
KEY_MISMATCH = "KEY_MISMATCH"
BUSY = "BUSY"
CORRUPT = "CORRUPT"
EMPTY = "EMPTY"
NOT_CIPHER = "NOT_CIPHER"

REASON_CN = {
    OK: "解密成功",
    NO_KEY: "这个库的密钥还没抓到（每库一把，需在微信运行且打开过该库时抓取）",
    KEY_MISMATCH: "密钥与这个库的 salt 不匹配（多半是换了微信号/微信重装后密钥变了）",
    BUSY: "微信正在写这个库，本次快照重试失败（下次采集会自动重试，游标不会推进）",
    CORRUPT: "库文件长度或页结构异常，可能是未完成的写入或磁盘错误",
    EMPTY: "空文件（微信尚未写入这个库）",
    NOT_CIPHER: "不是 SQLCipher 加密库（可能是明文或非微信文件），已跳过",
}


@dataclass
class DbResult:
    name: str
    status: str
    pages: int = 0
    out_bytes: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == OK


@dataclass
class MirrorResult:
    ok: bool = False
    storage_out: Path | None = None
    dbs: list[DbResult] = field(default_factory=list)
    message: str = ""
    notes: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    def summary(self) -> str:
        ok = sum(1 for d in self.dbs if d.ok)
        return f"{ok}/{len(self.dbs)} 个库解密成功"


# WAL 快照：WAL 里存的是「加密后的页」，所以必须先按 SQLite 语义合并，
# 再整库解密。直接读 -wal 会读到未提交的半页，解出来就是坏库。
# 逻辑与 vendor/wechat-key/scripts/common/wal_snapshot.py 等价（含校验和）。
def _wal_checksum(data: bytes, byteorder: str, state: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    if len(data) % 8:
        raise ValueError("WAL 校验输入必须是 8 字节对齐")
    first, second = state
    for left, right in struct.iter_unpack(byteorder + "II", data):
        first = (first + left + second) & 0xFFFFFFFF
        second = (second + right + first) & 0xFFFFFFFF
    return first, second


def merge_committed_pages(main: bytes, wal: bytes, page_size: int = PAGE) -> bytes:
    """只保留最后一个已提交事务的页，丢弃写了一半的帧。"""
    if not wal:
        return main
    if len(main) < page_size or len(main) % page_size or len(wal) < 32:
        raise ValueError("主库或 WAL 头不完整")
    magic, version, size = struct.unpack(">III", wal[:12])
    if magic not in (0x377F0682, 0x377F0683) or version != 3007000 or size != page_size:
        raise ValueError("WAL 格式或页大小不受支持")
    byteorder = "<" if magic == 0x377F0682 else ">"
    state = _wal_checksum(wal[:24], byteorder)
    if state != struct.unpack(">II", wal[24:32]):
        raise ValueError("WAL 头校验和不匹配")
    generation = wal[16:24]
    pending: dict[int, bytes] = {}
    committed: dict[int, bytes] = {}
    committed_size: int | None = None
    main_available = len(main) // page_size
    frame_size = page_size + 24
    for offset in range(32, len(wal) - frame_size + 1, frame_size):
        header = wal[offset:offset + 24]
        if header[8:16] != generation:
            break                      # 上一代残留帧，不能算数
        page_number, commit_size = struct.unpack(">II", header[:8])
        if not 1 <= page_number <= 0xFFFFFFFE:
            raise ValueError("WAL 页号非法")
        page = wal[offset + 24:offset + frame_size]
        state = _wal_checksum(header[:8] + page, byteorder, state)
        if state != struct.unpack(">II", header[16:24]):
            raise ValueError("WAL 帧校验和不匹配")
        pending[page_number] = page
        if commit_size:
            committed.update(pending)
            pending.clear()
            committed = {n: p for n, p in committed.items() if n <= commit_size}
            committed_size = commit_size
            main_available = min(main_available, commit_size)
    if committed_size is None:
        return main                    # 没有提交点：只有主库内容可信
    if committed_size > main_available + len(committed):
        raise ValueError("WAL 提交跨过了缺失的页")
    out = bytearray()
    for number in range(1, committed_size + 1):
        page = committed.get(number)
        if page is None:
            if number > main_available:
                raise ValueError("WAL 提交里缺新页")
            page = main[(number - 1) * page_size:number * page_size]
        out.extend(page)
    return bytes(out)


def _meta(path: str) -> tuple[int, int, int] | None:
    try:
        st = os.stat(path)
        return st.st_size, st.st_mtime_ns, st.st_ino
    except OSError:
        return None


def read_snapshot(source: Path, attempts: int = 3) -> bytes:
    """读一份「主库 + 已提交 WAL」的一致快照；期间文件被改动就重试。"""
    wal_path = str(source) + "-wal"
    for _ in range(attempts):
        before = (_meta(str(source)), _meta(wal_path))
        try:
            main = source.read_bytes()
        except OSError as exc:
            raise ValueError(f"读主库失败: {exc}") from exc
        try:
            wal = Path(wal_path).read_bytes() if os.path.exists(wal_path) else b""
        except OSError:
            wal = b""
        after = (_meta(str(source)), _meta(wal_path))
        if before != after:
            time.sleep(0.15)
            continue
        return merge_committed_pages(main, wal)
    raise _BusyError("主库/WAL 在读取过程中持续变化")


class _BusyError(RuntimeError):
    pass


# 两代密钥形态（这是历史上一直取不到密钥的技术根因）
#
#   4.0.x：`WX_DBKEY` 是 passphrase，SQLCipher 自己再跑 25.6 万轮 PBKDF2；
#   4.1+ ：微信只把「每库一把」的 32 字节 raw key 放在 WCDB 配置对象里，
#          这个值**直接**就是 AES-256 的 enc_key，只再用 2 轮 PBKDF2 派生 HMAC key。
#
# 老代码只实现了第一种，于是 4.1 上即使拿到正确的密钥，也会被判成
# 「密钥与数据库不匹配」——这是反复修却修不好的核心原因。
def derive_keys(raw_key: bytes, salt: bytes) -> tuple[bytes, bytes]:
    """4.0.x 的 passphrase 模式：PBKDF2-SHA512 25.6 万轮。"""
    enc = hashlib.pbkdf2_hmac("sha512", raw_key, salt, KDF_ITER, 32)
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac = hashlib.pbkdf2_hmac("sha512", enc, mac_salt, 2, 32)
    return enc, mac


def derive_keys_direct(enc_key: bytes, salt: bytes) -> tuple[bytes, bytes]:
    """4.1+ 的 raw key 模式：密钥本身就是 enc_key，只派生 HMAC key（2 轮）。"""
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, 32)
    return enc_key, mac


def _key_modes(key: bytes, salt: bytes):
    """按「先快后慢」给出两种候选：direct 约 1 毫秒，passphrase 约 200 毫秒。"""
    yield "direct", *derive_keys_direct(key, salt)
    yield "passphrase", *derive_keys(key, salt)


def _page1_hmac_ok(mac: bytes, head: bytes) -> bool:
    """page 1 的 HMAC 是密钥正确性的**精确**判据（宏块校验，误判率 2^-512）。"""
    if len(head) < PAGE:
        return False
    calc = hmac.new(mac, head[SALT_SZ:PAGE - 64] + struct.pack("<I", 1), hashlib.sha512).digest()
    return hmac.compare_digest(calc, head[PAGE - 64:PAGE])


def _page_hmac(mac: bytes, page: bytes, number: int, offset: int) -> bytes:
    return hmac.new(
        mac, page[offset:PAGE - 64] + struct.pack("<I", number), hashlib.sha512
    ).digest()


def looks_like_sqlcipher(data: bytes) -> bool:
    """加密库的第一个 16 字节是随机 salt；明文库以 'SQLite format 3' 开头。"""
    if len(data) < PAGE:
        return False
    if data.startswith(SQLITE_MAGIC):
        return False
    return len(data) % PAGE == 0


def page1_header_ok(pt: bytes) -> bool:
    """校验 page 1 解密出来的「页头正文」（即明文第 16 字节起的内容）。

    这里**不**死磕 reserve==80。SQLCipher 默认 80（IV16+HMAC64），但 reserve
    是可以配的；把它写成硬条件，会在某些版本上把**正确**的密钥判成错密钥
    —— 历史版本正是用 `pt[4]==0x50` 卡死了这一条，导致「密钥明明对却报错」。

    改用 SQLite 头里真正的常量做指纹，误判率仍然极低（2^-40 量级）：
        pt[0:2]  页大小（4096 = 0x1000）
        pt[2:4]  写/读格式版本，只会是 1（旧版）或 2（WAL）
        pt[5:8]  负载分配比例，固定 64 / 32 / 32
    """
    if len(pt) < 8:
        return False
    if int.from_bytes(pt[0:2], "big") != PAGE:
        return False
    if pt[2] not in (1, 2) or pt[3] not in (1, 2):
        return False
    return pt[5] == 64 and pt[6] == 32 and pt[7] == 32


def verify_direct_key(key: bytes, head: bytes) -> bool:
    """4.1+ 每库 raw key 的精确校验（只跑 2 轮 PBKDF2，微秒级）。"""
    if len(key) != 32 or len(head) < PAGE:
        return False
    _enc, mac = derive_keys_direct(key, head[:SALT_SZ])
    return _page1_hmac_ok(mac, head)


def key_matches_db(raw_key: bytes, db_path: Path) -> tuple[bool, str]:
    """用 page 1 验证密钥。返回 (是否匹配, 说明)。

    判据从「猜页头字节」升级为「校验 page 1 的 HMAC」：
      * direct 模式只要 2 轮 PBKDF2，约 1 毫秒；
      * passphrase 模式 25.6 万轮，约 200 毫秒（只在 direct 不通过时才跑）；
      * HMAC 对不上时，再退回页头指纹兜底（兼容 reserve 非 80 的变体）。
    """
    try:
        with open(db_path, "rb") as fh:
            head = fh.read(PAGE)
    except OSError as exc:
        return False, f"读库失败: {exc}"
    if len(head) < PAGE:
        return False, "库文件不足一页（微信还没写入数据）"
    if head.startswith(SQLITE_MAGIC):
        return False, "这是明文库，不需要密钥"
    if len(raw_key) != 32:
        return False, f"密钥应为 32 字节（64 位 hex），实际 {len(raw_key)} 字节"
    salt = head[:SALT_SZ]
    for mode, _enc, mac in _key_modes(raw_key, salt):
        if _page1_hmac_ok(mac, head):
            return True, f"密钥与数据库匹配（{mode} 模式，page 1 HMAC 校验通过）"
    for mode, enc, _mac in _key_modes(raw_key, salt):
        pt = aes_cbc_decrypt(enc, head[PAGE - RESERVE:PAGE - RESERVE + 16], head[SALT_SZ:PAGE - RESERVE])
        if page1_header_ok(pt):
            return True, f"密钥与数据库匹配（{mode} 模式，页头指纹校验通过）"
    return False, (
        "密钥与当前数据库不匹配（page 1 HMAC 未通过）。"
        "通常是因为这个库的 salt 变了：换过微信号、微信重装/升级，或填的是别的机器上的密钥"
    )


def detect_mode(raw_key: bytes, head: bytes) -> str:
    """判断这把密钥属于 direct（4.1+ 每库 raw key）还是 passphrase（4.0.x）。"""
    salt = head[:SALT_SZ]
    for mode, _enc, mac in _key_modes(raw_key, salt):
        if _page1_hmac_ok(mac, head):
            return mode
    return ""


def page_size_of(data: bytes) -> int:
    return PAGE


def decrypt_db_bytes(data: bytes, raw_key: bytes, verify_all: bool = True,
                     mode: str = "auto") -> tuple[bytes, int]:
    """把一个加密库的完整字节解成明文 sqlite 字节。失败抛 ValueError。

    mode="auto" 时先按 direct（4.1+ 每库 raw key）验 page1 HMAC，不通过再试
    passphrase（4.0.x）。两条都不通过才报错，且报错信息能区分「密钥不对」。
    """
    if len(data) < PAGE or len(data) % PAGE:
        raise ValueError("文件长度不是页大小的整数倍")
    salt = data[:SALT_SZ]
    head = data[:PAGE]
    enc = mac = b""
    if mode == "auto":
        for name, e, m in _key_modes(raw_key, salt):
            if _page1_hmac_ok(m, head):
                enc, mac, mode = e, m, name
                break
        if not enc:
            for name, e, m in _key_modes(raw_key, salt):
                pt = aes_cbc_decrypt(e, head[PAGE - RESERVE:PAGE - RESERVE + 16],
                                     head[SALT_SZ:PAGE - RESERVE])
                if page1_header_ok(pt):
                    enc, mac, mode = e, m, name
                    break
        if not enc:
            raise ValueError("第 1 页校验失败（密钥不对，或这个库不是 SQLCipher 格式）")
    elif mode == "direct":
        enc, mac = derive_keys_direct(raw_key, salt)
    else:
        enc, mac = derive_keys(raw_key, salt)
    total = len(data) // PAGE
    rstart = PAGE - RESERVE

    def _page(number: int) -> bytes:
        raw = data[(number - 1) * PAGE:number * PAGE]
        offset = SALT_SZ if number == 1 else 0
        if verify_all:
            expect = _page_hmac(mac, raw, number, offset)
            if not hmac.compare_digest(expect, raw[PAGE - 64:PAGE]):
                raise KeyError(number)
        iv = raw[rstart:rstart + 16]
        body = aes_cbc_decrypt(enc, iv, raw[offset:rstart])
        return (SQLITE_MAGIC + body + raw[rstart:]) if number == 1 else (body + raw[rstart:])

    # 先只解第一页：密钥错的话这里立刻失败，不必解整个库。
    # 统一抛 ValueError —— 上层只需认一种异常类型，不会漏掉 KeyError。
    try:
        first = _page(1)
    except KeyError:
        raise ValueError("第 1 页校验失败（密钥不对，或这个库不是 SQLCipher 格式）") from None
    if not first.startswith(SQLITE_MAGIC):
        raise ValueError("解密结果不是 SQLite 头（密钥或参数不匹配）")
    out = bytearray(first)
    for number in range(2, total + 1):
        try:
            out.extend(_page(number))
        except KeyError:
            # 单页 HMAC 不过 = 该页正在被写或损坏。整体判失败，避免产出坏库。
            raise ValueError(f"第 {number} 页校验失败（库可能正在写入）") from None
    return bytes(out), total


def decrypt_db(src: Path, dst: Path, raw_key: bytes, verify_all: bool = True) -> DbResult:
    name = src.name
    if not raw_key:
        return DbResult(name, NO_KEY, detail=REASON_CN[NO_KEY])
    try:
        data = read_snapshot(src)
    except _BusyError as exc:
        return DbResult(name, BUSY, detail=str(exc))
    except ValueError as exc:
        return DbResult(name, CORRUPT, detail=str(exc))

    if not data:
        return DbResult(name, EMPTY)
    if data.startswith(SQLITE_MAGIC):
        # 明文库：直接复制，保持镜像结构一致（不影响使用）
        return _write(dst, data, name, len(data) // PAGE, NOT_CIPHER, "明文库，已直接复制")
    if not looks_like_sqlcipher(data):
        return DbResult(name, CORRUPT, detail=f"长度 {len(data)} 不是 {PAGE} 的整数倍")

    try:
        plain, pages = decrypt_db_bytes(data, raw_key, verify_all=verify_all)
    except KeyError:
        return DbResult(name, KEY_MISMATCH, detail=REASON_CN[KEY_MISMATCH])
    except ValueError as exc:
        msg = str(exc)
        status = BUSY if "正在写入" in msg else CORRUPT
        return DbResult(name, status, detail=msg)
    except Exception as exc:  # noqa: BLE001  解密后端异常也要归类，不能把上层炸掉
        return DbResult(name, CORRUPT, detail=f"解密异常: {exc}")
    return _write(dst, plain, name, pages, OK, "")


def _write(dst: Path, data: bytes, name: str, pages: int, status: str, detail: str) -> DbResult:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=dst.name + ".", dir=str(dst.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dst)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return DbResult(name, status, pages=pages, out_bytes=len(data), detail=detail)


def db_salt(path: Path) -> str:
    """读一个库前 16 字节的 salt（hex）。读不到返回空串。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(SALT_SZ)
        return head.hex() if len(head) == SALT_SZ else ""
    except OSError:
        return ""


def decrypt_storage(src_storage: Path, dst_storage: Path,
                    key_or_keys, only_message: bool = False) -> MirrorResult:
    """把一个 db_storage 下的加密库整体解成明文镜像。

    key_or_keys 支持两种形态：
        * bytes           —— 单一密钥（老调用方式，所有库共用，4.0.x 仍然适用）
        * {salt_hex: key} —— 每库一密钥（微信 4.1+ 的真实形态）

    微信 4.1 起每个库有各自独立的密钥，所以 keyring 缺失的库会明确标成 NO_KEY，
    不再用一把密钥硬套所有库、也不再笼统报「解密失败」。
    """
    started = time.time()
    res = MirrorResult(storage_out=dst_storage)
    if not src_storage.is_dir():
        res.message = f"原始微信库目录不存在: {src_storage}"
        return res

    dbs = sorted(p for p in src_storage.rglob("*.db") if p.is_file())
    if only_message:
        dbs = [p for p in dbs if p.parent.name == "message"]
    if not dbs:
        res.message = f"{src_storage} 下没有找到任何 .db 文件"
        return res

    keyring = key_or_keys if isinstance(key_or_keys, dict) else None
    for src in dbs:
        rel = src.relative_to(src_storage)
        if keyring is None:
            key = key_or_keys if isinstance(key_or_keys, (bytes, bytearray)) else b""
        else:
            key = keyring.get(db_salt(src), b"")
            if isinstance(key, str):
                try:
                    key = bytes.fromhex(key)
                except ValueError:
                    key = b""
        res.dbs.append(decrypt_db(src, dst_storage / rel, bytes(key)))

    msg_db = next((d for d in res.dbs if d.name == "message_0.db"), None)
    fatal = [d for d in res.dbs if not d.ok and d.status in (KEY_MISMATCH, CORRUPT)]
    missing = [d for d in res.dbs if d.status == NO_KEY]
    if msg_db is not None and not msg_db.ok:
        res.ok = False
        res.message = f"消息主库 message_0.db 解密失败：{msg_db.detail}"
    elif fatal and len(fatal) == len(res.dbs):
        res.ok = False
        res.message = "所有库都解密失败：" + "；".join(f"{d.name}={d.status}" for d in fatal[:3])
    else:
        res.ok = any(d.ok for d in res.dbs)
        res.message = res.summary()
        if missing:
            res.message += f"，{len(missing)} 个库缺密钥"

    for d in res.dbs:
        if not d.ok:
            res.notes.append(f"{d.name}: {d.status} {d.detail}".strip())
    res.notes.append(f"AES 后端: {backend_name()}")
    res.elapsed_ms = int((time.time() - started) * 1000)
    return res


__all__ = [
    "PAGE", "RESERVE", "OK", "NO_KEY", "KEY_MISMATCH", "BUSY", "CORRUPT", "EMPTY", "NOT_CIPHER",
    "REASON_CN", "DbResult", "MirrorResult",
    "read_snapshot", "merge_committed_pages", "derive_keys", "derive_keys_direct",
    "key_matches_db", "detect_mode", "db_salt",
    "decrypt_db_bytes", "decrypt_db", "decrypt_storage", "looks_like_sqlcipher",
]
