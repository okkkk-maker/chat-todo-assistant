"""微信 4.x 数据库密钥：定位、提取、**校验**、落盘。

为什么单独成模块
历史版本把「取密钥」拆在三个地方：vendor 的 extract_raw_key.py（扫描）、
health.trigger_wechat_key_extractor（拉起窗口）、health.handle_wechat_key_sync
（写 .env）。三处彼此不知道对方的结果，于是出现两类难查的假失败：

    1. **写进去的密钥没人验**：只要 key_windows.txt 里躺着一串 64 位 hex
       （哪怕来自旧机器 / 旧账号 / 上一次微信重装前），就直接写进 .env。
       之后采集报「解密失败」，用户以为程序坏了，其实密钥是错的。
    2. **解密调用少传一层目录**：extract_raw_key.auto_decrypt() 与
       channels/wechat._try_decrypt() 都把 WECHAT_DATA_ROOT 指到了
       `<wxid>_xxx` 这一层，而 decrypt_all.py 是按 `<root>/*/db_storage`
       匹配的 → 一个库都找不到 → 「密钥抓到了但没产出镜像」。

本模块的规矩
    * 任何来源的密钥，**必须先过 page-1 校验**才能被采用（不验不写盘）；
    * 校验失败要说清是「格式不对」「和这个库不匹配」还是「压根没找到微信」；
    * 只读出密钥，不做解密（解密在 wxdecrypt），职责单一便于自测。

密钥来源（按优先级，前一个不通过才试下一个）
    1. .env 里的 WX_DBKEY
    2. 密钥文件（vendor/wechat-key/key_windows.txt、work/wx_key.txt）
    3. 内存扫描：
       · 微信 4.1+ 走 Config.Cipher 对象链，拿到「每库一把」raw key（精确、秒级）
       · 微信 4.0.x 退回 `x'<32B key><16B salt>'` 缓存扫描
    4. 人工粘贴（键盘交互时）
"""
from __future__ import annotations

import csv
import ctypes
import io
import json
import os
import re
import struct
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import common
import wxdecrypt

# 状态
OK = "OK"
NOT_CONFIGURED = "NOT_CONFIGURED"   # 没填也没找到
NO_WECHAT = "NO_WECHAT"             # 本机没有微信数据目录/没装客户端
KEY_MISMATCH = "KEY_MISMATCH"       # 找到了但和当前库不匹配
SCAN_FAILED = "SCAN_FAILED"         # 扫了但没抓到

PROCESS_NAMES = ("weixin.exe", "wechat.exe")
HEX64 = re.compile(r"[0-9a-fA-F]{64}")
MAX_REGION = 250 * 1024 * 1024      # 单块上限，防止遇到巨型保留区
CHUNK = 4 * 1024 * 1024
OVERLAP = 256                        # 跨块边界的模式要能被拼起来
TARGET_PROTECT = (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)   # 可读/读写/写时复制/可执行可读


@dataclass
class KeyResult:
    status: str = NOT_CONFIGURED
    key: str = ""
    source: str = ""
    message: str = ""
    notes: list[str] = field(default_factory=list)
    db_path: Path | None = None
    keyring: dict[str, str] = field(default_factory=dict)   # {salt_hex: key_hex}

    @property
    def ok(self) -> bool:
        return self.status == OK and bool(self.key)


# 定位数据目录
def _user_dirs() -> list[Path]:
    home = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    roots = [
        home / "Documents" / "xwechat_files",      # 微信 4.x 默认
        home / "文档" / "xwechat_files",            # 中文系统重定向
        home / "OneDrive" / "Documents" / "xwechat_files",
        home / "Documents" / "WeChat Files",       # 微信 3.x
        home / "文档" / "WeChat Files",
    ]
    for drive in ("C", "D", "E", "F"):
        roots += [Path(f"{drive}:/WeChat Files"), Path(f"{drive}:/xwechat_files")]
    return roots


def find_storage_roots(configured: str = "") -> list[Path]:
    """返回所有可能的 db_storage 目录，按 message_0.db 修改时间倒序。

    configured 允许用户直接填 db_storage、填 <wxid> 目录、或填 xwechat_files 根，
    三种写法都能识别 —— 这三种写法在真实机器上都被填过，不能只认一种。
    """
    explicit: list[Path] = []
    if configured:
        p = Path(configured).expanduser()
        if p.is_dir():
            if p.name.lower() == "db_storage":
                explicit.append(p)
            else:
                explicit += list(p.glob("*/db_storage")) or list(p.glob("*/*/db_storage"))

    hits: list[Path] = list(explicit)
    if not explicit:
        # 只有用户没指定（或指到了空目录）才自动发现。
        # 旧实现把两者混在一起按 mtime 排序：真实微信库一直在写、mtime 最新，
        # 于是「显式配置的 WX_DATA_PATH」被静默忽略，用户以为配了其实没生效，
        # 还可能拿别的号（或自测库）的路径去解密。
        for root in _user_dirs():
            try:
                if root.is_dir():
                    hits += list(root.glob("*/db_storage"))
            except OSError:
                continue
    uniq: dict[Path, float] = {}
    for h in hits:
        db = h / "message" / "message_0.db"
        try:
            uniq[h] = db.stat().st_mtime if db.exists() else h.stat().st_mtime
        except OSError:
            continue
    return sorted(uniq, key=lambda k: uniq[k], reverse=True)


def pick_storage(configured: str = "") -> tuple[Path | None, list[Path]]:
    roots = find_storage_roots(configured)
    return (roots[0] if roots else None), roots


def message_db(storage: Path) -> Path:
    return storage / "message" / "message_0.db"


def account_of(storage: Path) -> str:
    """`<...>/<wxid>_xxxx/db_storage` → `wxid_xxxx`（用于镜像目录命名）。"""
    try:
        return storage.parent.name
    except Exception:  # noqa: BLE001
        return "default"


# 密钥格式 / 来源
def normalize_key(text: str) -> str:
    if not text:
        return ""
    m = HEX64.search(str(text).strip())
    return m.group(0).lower() if m else ""


def key_file_candidates(env: dict | None = None) -> list[Path]:
    """密钥可能落盘的位置（vendor 的脚本历史版本写过好几个路径）。"""
    home = common.HOME
    cands = []
    vdir = (env or {}).get("WECHAT_VENDOR_DIR", "")
    if vdir:
        cands.append(Path(vdir) / "key_windows.txt")
    cands += [
        home / "vendor" / "wechat-key" / "key_windows.txt",
        home.parent / "1-实施员一键初始化包" / "vendor" / "wechat-key" / "key_windows.txt",
        home.parent / "2-用户日常全自动运行包" / "vendor" / "wechat-key" / "key_windows.txt",
        common.WORK / "wx_key.txt",
        home / "key_windows.txt",
    ]
    return cands


def read_key_file(env: dict | None = None) -> tuple[str, Path | None]:
    for path in key_file_candidates(env):
        try:
            if path.is_file():
                key = normalize_key(path.read_text(encoding="ascii", errors="ignore"))
                if key:
                    return key, path
        except OSError:
            continue
    return "", None


def validate(key_hex: str, db: Path) -> tuple[bool, str]:
    try:
        raw = bytes.fromhex(key_hex)
    except ValueError:
        return False, "密钥不是合法的十六进制"
    if len(raw) != 32:
        return False, f"密钥应为 64 位 hex（32 字节），实际 {len(key_hex)} 位"
    return wxdecrypt.key_matches_db(raw, db)


# 内存扫描
def _wechat_pids() -> list[tuple[int, int]]:
    """返回 [(pid, 内存KB)]，只认微信主进程，排除 WeChatAppEx/WeChatPlayer。

    WeChatAppEx.exe 是小程序/网页引擎（Chromium 堆，几百 MB 却与数据库无关），
    扫它既慢又永远扫不到 —— 历史版本曾因为它把整轮扫描拖到超时。
    """
    out: list[tuple[int, int]] = []
    try:
        flags = common.CREATE_NO_WINDOW
        raw = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"], errors="replace", timeout=8, creationflags=flags
        )
    except (OSError, subprocess.SubprocessError):
        return out
    for row in csv.reader(io.StringIO(raw)):
        if len(row) < 5:
            continue
        name = row[0].strip().lower()
        if name not in PROCESS_NAMES:
            continue
        if not row[1].strip().isdigit():
            continue
        mem = re.sub(r"[^0-9]", "", row[4]) or "0"
        out.append((int(row[1]), int(mem)))
    out.sort(key=lambda x: -x[1])
    return out


class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", ctypes.c_uint32),
        ("_pad1", ctypes.c_uint32),
        ("RegionSize", ctypes.c_size_t),
        ("State", ctypes.c_uint32),
        ("Protect", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("_pad2", ctypes.c_uint32),
    ]


def _regions(k32, handle) -> list[tuple[int, int]]:
    regions: list[tuple[int, int]] = []
    mbi = _MBI()
    addr = 0
    limit = 0x7FFFFFFFFFFF
    while addr < limit:
        got = k32.VirtualQueryEx(
            handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)
        )
        if not got:
            break
        base = mbi.BaseAddress or 0
        size = mbi.RegionSize or 0
        if mbi.State == 0x1000 and mbi.Protect in TARGET_PROTECT and 0 < size <= MAX_REGION:
            regions.append((base, size))
        nxt = base + size
        if nxt <= addr:
            break
        addr = nxt
    return regions


class _Verifier:
    """候选密钥的校验器：先用 AES 试 K1（微秒级），再做 PBKDF2（约 0.2 秒）。

    这个顺序很重要：内存里 32 字节的高熵窗口成千上万，如果每个都跑
    PBKDF2，一轮扫描要几十分钟。用 AES 先筛掉绝大多数，只对「salt 附近」
    和「x'...' 结构化位置」的候选花 PBKDF2。
    """

    def __init__(self, db: Path, budget: int = 45):
        self.db = db
        head = db.read_bytes()[:wxdecrypt.PAGE]
        self.head = head
        self.salt = head[:16]
        self.iv = head[wxdecrypt.PAGE - wxdecrypt.RESERVE:wxdecrypt.PAGE - wxdecrypt.RESERVE + 16]
        self.ct = head[16:32]
        self.budget = budget
        self.tried: set[bytes] = set()
        self.pbkdf2_used = 0

    def _hdr_ok(self, pt: bytes) -> bool:
        return wxdecrypt.page1_header_ok(pt)

    def as_k1(self, cand: bytes) -> bool:
        """K1 是「已派生的页密钥」，直接解 page 1。"""
        if len(cand) != 32 or len(set(cand)) < 5:
            return False
        try:
            return self._hdr_ok(wxcrypto_decrypt(cand, self.iv, self.ct))
        except Exception:  # noqa: BLE001
            return False

    def as_raw(self, cand: bytes) -> bool:
        if len(cand) != 32 or len(set(cand)) < 5 or self.pbkdf2_used >= self.budget:
            return False
        if cand in self.tried:
            return False
        self.tried.add(cand)
        self.pbkdf2_used += 1
        import hashlib

        enc = hashlib.pbkdf2_hmac("sha512", cand, self.salt, wxdecrypt.KDF_ITER, 32)
        try:
            return self._hdr_ok(wxcrypto_decrypt(enc, self.iv, self.ct))
        except Exception:  # noqa: BLE001
            return False


def wxcrypto_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """薄封装：让 wxkey 不必在导入期就绑定 AES 后端（后端缺失时也能给出人话）。"""
    from wxcrypto import aes_cbc_decrypt

    return aes_cbc_decrypt(key, iv, data)


def _candidates_in_chunk(chunk: bytes, salt: bytes, salt_hex: bytes) -> tuple[list[bytes], list[bytes]]:
    """从一个内存块里挑候选。返回 (高优先=结构化位置, 低优先=salt 邻域窗口)。

    结构化位置（WCDB 缓存 raw key 的真实格式，命中率最高）：
        x' <32字节密钥 hex=64> <16字节 salt hex=32> '   → 共 98 字节
    """
    hi: list[bytes] = []
    lo: list[bytes] = []

    # 1) x'<96 hex>'
    start = 0
    marker = b"x'"
    while True:
        idx = chunk.find(marker, start)
        if idx < 0:
            break
        start = idx + 2
        seg = chunk[idx + 2:idx + 2 + 96]
        if len(seg) < 96 or chunk[idx + 98:idx + 99] not in (b"'", b"\x00"):
            continue
        try:
            blob = bytes.fromhex(seg.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            # 大写 hex 也允许
            try:
                blob = bytes.fromhex(seg.decode("ascii").lower())
            except (ValueError, UnicodeDecodeError):
                continue
        if len(blob) != 48:
            continue
        key_part, salt_part = blob[:32], blob[32:]
        hi.append(key_part)
        # 同一处还可能是 K1+另一段，交给校验器判断

    # 2) 二进制 salt 邻域
    pos = chunk.find(salt)
    while pos >= 0:
        for off in (-32, -16, 16, 32, 48, 64):
            p = pos + off
            if 0 <= p and p + 32 <= len(chunk):
                lo.append(chunk[p:p + 32])
        pos = chunk.find(salt, pos + 1)

    # 3) ASCII salt 前后夹着的 64 位 hex
    p = chunk.find(salt_hex)
    while p >= 0:
        for seg in (chunk[max(0, p - 64):p], chunk[p + 32:p + 96]):
            if len(seg) == 64:
                try:
                    lo.append(bytes.fromhex(seg.decode("ascii")))
                except (ValueError, UnicodeDecodeError):
                    pass
        p = chunk.find(salt_hex, p + 1)

    return hi, lo


def _scan_legacy(
    db_path: Path,
    timeout: float = 25.0,
    pids: list[int] | None = None,
    budget: int = 45,
    progress=None,
) -> KeyResult:
    """扫描微信进程内存里的密钥缓存。只读，不修改目标进程，不影响微信使用。

    db_path: 用来校验候选密钥的加密库（通常是 message/message_0.db）
    timeout: 总时长上限（秒）。超时返回带诊断的 SCAN_FAILED，绝不静默。
    """
    res = KeyResult(status=SCAN_FAILED, db_path=db_path)
    if os.name != "nt":
        res.message = "内存扫描仅在 Windows 上可用"
        return res
    try:
        db_path.read_bytes()[:wxdecrypt.PAGE]
    except OSError as exc:
        res.status = NO_WECHAT
        res.message = f"读不到微信数据库: {exc}"
        return res

    targets = pids if pids is not None else [pid for pid, _ in _wechat_pids()]
    if not targets:
        res.status = NO_WECHAT
        res.message = "没有检测到正在运行的微信主进程（Weixin.exe）。请先登录电脑版微信再试"
        return res

    verifier = _Verifier(db_path, budget=budget)
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.VirtualQueryEx.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t
    ]
    k32.ReadProcessMemory.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]

    buf = ctypes.create_string_buffer(CHUNK)
    read = ctypes.c_size_t(0)
    started = time.time()
    scanned = 0
    seen_regions = 0
    errors = 0
    # 诊断计数：区分「内存里根本没有密钥结构」和「有结构但没验过」。
    # 微信 4.1 起静态内存里不再保留裸 key / hex 缓存，这两个数会一直是 0，
    # 靠它们才能在报告里说清"该换启动钩子方案"，而不是笼统说"没找到"。
    hex_cache_hits = 0
    salt_window_hits = 0

    for pid in targets:
        if time.time() - started > timeout:
            break
        handle = k32.OpenProcess(0x0010 | 0x0400, False, pid)   # VM_READ | QUERY_INFORMATION
        if not handle:
            errors += 1
            res.notes.append(
                f"PID {pid} 打不开（错误码 {ctypes.GetLastError()}）："
                "请以管理员身份运行，或确认微信以同一用户身份启动"
            )
            continue
        try:
            for base, size in _regions(k32, handle):
                if time.time() - started > timeout:
                    break
                seen_regions += 1
                offset = 0
                while offset < size:
                    if time.time() - started > timeout:
                        break
                    want = min(CHUNK, size - offset)
                    ok = k32.ReadProcessMemory(
                        handle, ctypes.c_void_p(base + offset), buf, want,
                        ctypes.byref(read),
                    )
                    got = read.value if ok else 0
                    if got:
                        chunk = buf.raw[:got]
                        scanned += got
                        hi, lo = _candidates_in_chunk(
                            chunk, verifier.salt, verifier.salt.hex().encode()
                        )
                        hex_cache_hits += len(hi)
                        salt_window_hits += len(lo)
                        for cand in hi:
                            if verifier.as_raw(cand):
                                res.status = OK
                                res.key = cand.hex()
                                res.source = "memory"
                                res.message = "已从微信进程内存中捕获并与数据库校验通过"
                                res.notes.append(f"命中位置: 结构化缓存 x'<key><salt>'")
                                return _finish(res, started, scanned)
                        for cand in lo:
                            if verifier.as_raw(cand):
                                res.status = OK
                                res.key = cand.hex()
                                res.source = "memory"
                                res.message = "已从微信进程内存 salt 邻域捕获并校验通过"
                                return _finish(res, started, scanned)
                    if progress:
                        progress(scanned, seen_regions, verifier.pbkdf2_used)
                    if offset + want >= size:
                        break
                    offset += max(1, want - OVERLAP)
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))

    res.notes.append(
        f"扫描 {scanned // 1024 // 1024} MB / {seen_regions} 个内存块，"
        f"PBKDF2 校验 {verifier.pbkdf2_used} 个候选"
    )
    res.notes.append(
        f"结构化缓存 x'<key><salt>' {hex_cache_hits} 处，salt 邻域候选 {salt_window_hits} 个"
    )
    if hex_cache_hits == 0 and salt_window_hits <= 4:
        res.notes.append(
            "内存里没有密钥缓存结构：微信 4.1+ 在派生完页密钥后就清掉了 raw key。"
            "此类版本必须用「启动时挂载」方式抓取（见 tools/wxhook.py）"
        )
    if errors and not scanned:
        res.status = NO_WECHAT
        res.message = "无法读取微信进程内存（权限不足）"
    elif time.time() - started > timeout:
        res.message = (
            f"扫描 {timeout:.0f} 秒未捕获密钥。微信在「刚启动、刚打开过某个聊天」时才把密钥"
            "留在内存里；请在微信里点开任一聊天后重试，或用「重启微信」方式再抓一次"
        )
    else:
        res.message = (
            "扫描完成但没有找到可用的密钥。典型原因是微信把密钥清掉了"
            "（长时间运行且没有新开数据库）。解决办法：重启微信后立刻再抓一次，"
            "或手工粘贴密钥"
        )
    return _finish(res, started, scanned)


# 微信 4.1+：从 WCDB 的 Config.Cipher 对象里取「每库一密钥」
#
# 4.1 起内存里既没有明文密钥，也没有 salt 相邻的 32 字节；只有一个名为
# com.Tencent.WCDB.Config.Cipher 的配置对象，它的 blob 用固定掩码 XOR 过，
# 解开后是若干 x'<64hex key><32hex salt>' 字面量 —— 每个库一把。
# 所以在 4.1 上「搜 x'...' 明文 / 搜 salt 邻域」必然 0 命中，这不是扫描姿势问题。
CONFIG_CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"
CONFIG_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)
CONFIG_LITERAL = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
MAX_USER_ADDR = 0x0000800000000000
CONFIG_BLOB_MAX = 1024


def _read_mem(k32, handle, addr: int, size: int) -> bytes:
    if addr <= 0 or size <= 0:
        return b""
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    if k32.ReadProcessMemory(handle, ctypes.c_void_p(addr), buf, size, ctypes.byref(got)):
        return buf.raw[: got.value]
    return b""


def _iter_chunks(k32, handle, regions, chunk: int = CHUNK, overlap: int = 0):
    for base, size in regions:
        offset = 0
        tail, tail_base = b"", base
        while offset < size:
            cur = min(chunk, size - offset)
            data = _read_mem(k32, handle, base + offset, cur)
            data_base = tail_base if tail else base + offset
            whole = tail + data
            if whole:
                yield data_base, whole
                if overlap:
                    tail = whole[-overlap:]
                    tail_base = data_base + max(0, len(whole) - len(tail))
                else:
                    tail, tail_base = b"", base + offset + cur
            else:
                tail, tail_base = b"", base + offset + cur
            offset += cur


def _u64(buf: bytes, off: int) -> int:
    if off < 0 or off + 8 > len(buf):
        return 0
    return struct.unpack_from("<Q", buf, off)[0]


def _xor_repeat(data: bytes) -> bytes:
    mask = CONFIG_XOR_MASK
    return bytes(v ^ mask[i % len(mask)] for i, v in enumerate(data))


def _config_candidates(blob: bytes) -> list[tuple[str, str]]:
    decoded = _xor_repeat(blob)
    out: list[tuple[str, str]] = []
    for m in CONFIG_LITERAL.finditer(decoded):
        run = m.group(1).decode("ascii").lower()
        starts = [0]
        if len(run) > 96:
            starts += list(range(0, len(run) - 63, 32))
            starts.append(len(run) - 64)
        for st in dict.fromkeys(starts):
            if st < 0 or st + 64 > len(run):
                continue
            key_hex = run[st:st + 64]
            try:
                raw = bytes.fromhex(key_hex)
            except ValueError:
                continue
            if len(set(raw)) < 15:
                continue
            salt = run[st + 64:st + 96] if st + 96 <= len(run) else ""
            if (key_hex, salt) not in out:
                out.append((key_hex, salt))
    return out


def scan_config_cipher(targets: list[Path], pids: list[int], progress=None):
    """扫 Config.Cipher 对象链。返回 (keyring, notes)。

    keyring 为 {salt_hex: key_hex}，只收录 page 1 HMAC 校验通过的密钥。
    """
    notes: list[str] = []
    heads: dict[str, bytes] = {}
    for path in targets:
        try:
            with open(path, "rb") as fh:
                head = fh.read(wxdecrypt.PAGE)
        except OSError:
            continue
        if len(head) >= wxdecrypt.PAGE and not head.startswith(wxdecrypt.SQLITE_MAGIC):
            heads[head[:16].hex()] = head
    if not heads:
        notes.append("没有可校验的加密库")
        return {}, notes

    if not pids:
        procs = _wechat_pids()
        pids = [p for p, m in procs if m >= 20 * 1024] or [p for p, _m in procs]

    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.VirtualQueryEx.argtypes = [ctypes.c_void_p] * 3 + [ctypes.c_size_t]
    k32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]

    keyring: dict[str, str] = {}
    hit_nodes = 0
    hit_blobs = 0
    for pid in pids:
        if len(keyring) >= len(heads):
            break
        handle = k32.OpenProcess(0x0010 | 0x0400, False, pid)
        if not handle:
            notes.append(f"PID {pid}: 打开失败（权限不足或进程已退出）")
            continue
        try:
            regions = _regions(k32, handle)
            addrs: set[int] = set()
            for base, data in _iter_chunks(
                k32, handle, regions, overlap=len(CONFIG_CIPHER_NAME) - 1
            ):
                pos = data.find(CONFIG_CIPHER_NAME)
                while pos >= 0:
                    addrs.add(base + pos)
                    pos = data.find(CONFIG_CIPHER_NAME, pos + 1)
            if not addrs:
                notes.append(f"PID {pid}: 没找到 {CONFIG_CIPHER_NAME.decode()} 标记")
                continue
            pairs = [
                struct.pack("<Q", a) + struct.pack("<Q", len(CONFIG_CIPHER_NAME))
                for a in addrs
            ]
            for base, data in _iter_chunks(k32, handle, regions, overlap=0x80):
                for pat in pairs:
                    pos = data.find(pat)
                    while pos >= 0:
                        node = _read_mem(k32, handle, base + pos - 0x10, 0x50)
                        if (len(node) >= 0x40
                                and _u64(node, 0x10) in addrs
                                and _u64(node, 0x18) == len(CONFIG_CIPHER_NAME)):
                            cfg = _u64(node, 0x28)
                            if 0x10000 <= cfg < MAX_USER_ADDR:
                                hit_nodes += 1
                                obj = _read_mem(k32, handle, cfg + 0x88, 0x28)
                                if len(obj) >= 0x18:
                                    dptr, dlen = _u64(obj, 8), _u64(obj, 0x10)
                                    if 0 < dlen <= CONFIG_BLOB_MAX and 0x10000 <= dptr < MAX_USER_ADDR:
                                        blob = _read_mem(k32, handle, dptr, int(dlen))
                                        if blob and len(blob) == dlen:
                                            hit_blobs += 1
                                            for key_hex, salt in _config_candidates(blob):
                                                raw = bytes.fromhex(key_hex)
                                                if salt and salt in heads:
                                                    if salt not in keyring and wxdecrypt.verify_direct_key(raw, heads[salt]):
                                                        keyring[salt] = key_hex
                                                else:
                                                    for sh, head in heads.items():
                                                        if sh in keyring:
                                                            continue
                                                        if wxdecrypt.verify_direct_key(raw, head):
                                                            keyring[sh] = key_hex
                        pos = data.find(pat, pos + 1)
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))

    notes.append(
        f"Config.Cipher 对象 {hit_nodes} 个、blob {hit_blobs} 个，"
        f"命中 {len(keyring)}/{len(heads)} 个库的密钥"
    )
    return keyring, notes


def scan_memory_for_key(
    db_path: Path,
    timeout: float = 25.0,
    pids: list[int] | None = None,
    budget: int = 45,
    progress=None,
) -> KeyResult:
    """取密钥：先走 4.1+ 的 Config.Cipher（快且精确），不中再退回 4.0.x 老扫描。"""
    started = time.time()
    storage = db_path.parent.parent
    targets = sorted(p for p in storage.rglob("*.db") if p.is_file()) if storage.is_dir() else [db_path]
    if progress:
        try:
            progress(0, 0)
        except Exception:  # noqa: BLE001
            pass

    keyring, notes = scan_config_cipher(targets, pids or [], progress)
    if keyring:
        res = KeyResult(status=OK, db_path=db_path, keyring=keyring)
        res.source = "config_cipher"
        res.key = keyring.get(wxdecrypt.db_salt(db_path), "") or next(iter(keyring.values()))
        res.message = (
            f"已从微信进程内存取到 {len(keyring)}/{len(targets)} 个库的密钥"
            "（page 1 HMAC 校验通过）"
        )
        res.notes = notes
        return _finish(res, started, 0)

    legacy = _scan_legacy(db_path, timeout=timeout, pids=pids, budget=budget, progress=progress)
    legacy.notes = notes + legacy.notes
    return legacy


def _finish(res: KeyResult, started: float, scanned: int) -> KeyResult:
    res.notes.append(f"耗时 {time.time() - started:.1f} 秒")
    return res


# 编排：多来源取密钥，全部必须过校验
def acquire(
    env: dict,
    db_path: Path,
    allow_scan: bool = True,
    manual_key: str = "",
    timeout: float = 25.0,
) -> KeyResult:
    """按优先级取一个**已验证**的密钥。任何一步失败都要留下人能看懂的原因。"""
    trace: list[str] = []
    ring = load_keyring(env)
    if ring:
        trace.append(f"已缓存 {len(ring)} 个库的密钥（work/wx_keys.json）")

    # 只拿到一把钥匙时（老 .env / 手工粘贴），顺手把整环补齐：
    # 4.1+ 每个 message_N.db 密钥都不同，缺一把就少一个分片。
    storage_dir = db_path.parent.parent
    if allow_scan and ring_gap(storage_dir, ring):
        extra, scan_notes = scan_config_cipher(
            sorted(p for p in storage_dir.rglob("*.db") if p.is_file())
            if storage_dir.is_dir() else [db_path],
            [],
        )
        fresh = {k: v for k, v in extra.items() if k not in ring}
        if fresh:
            ring.update(fresh)
            trace.append(f"补齐密钥环：新增 {len(fresh)} 个库")
        trace.extend(scan_notes[:2])

    # 1. .env
    raw_env = normalize_key(env.get("WX_DBKEY", ""))
    if raw_env:
        ok, why = validate(raw_env, db_path)
        trace.append(f".env WX_DBKEY: {'通过' if ok else '不通过'}（{why}）")
        if ok:
            ring.setdefault(wxdecrypt.db_salt(db_path), raw_env)
            return KeyResult(OK, raw_env, "env", "使用 .env 中的 WX_DBKEY（已校验）", trace, db_path, ring)

    # 2. 密钥文件
    file_key, file_path = read_key_file(env)
    if file_key:
        ok, why = validate(file_key, db_path)
        trace.append(f"密钥文件 {file_path.name}: {'通过' if ok else '不通过'}（{why}）")
        if ok:
            ring.setdefault(wxdecrypt.db_salt(db_path), file_key)
            return KeyResult(OK, file_key, "keyfile", f"使用 {file_path.name} 中的密钥（已校验）", trace, db_path, ring)

    # 3. 人工粘贴
    if manual_key:
        mk = normalize_key(manual_key)
        if mk:
            ok, why = validate(mk, db_path)
            trace.append(f"人工粘贴: {'通过' if ok else '不通过'}（{why}）")
            if ok:
                ring.setdefault(wxdecrypt.db_salt(db_path), mk)
                return KeyResult(OK, mk, "manual", "使用人工粘贴的密钥（已校验）", trace, db_path, ring)

    # 4. 内存扫描
    if allow_scan:
        scan = scan_memory_for_key(db_path, timeout=timeout)
        trace.extend(scan.notes)
        if scan.ok:
            scan.notes = trace + scan.notes
            if ring:
                merged = dict(scan.keyring)
                for k, v in ring.items():
                    merged.setdefault(k, v)
                scan.keyring = merged
            scan.keyring.setdefault(wxdecrypt.db_salt(db_path), scan.key)
            return scan
        last = scan
    else:
        last = KeyResult(SCAN_FAILED, message="已禁用内存扫描", notes=trace)

    # 全部失败：给出最有信息量的那一条
    status = last.status
    if raw_env or file_key:
        # 找到了密钥但对不上库 —— 这是最需要说清的情况
        if status != NO_WECHAT:
            status = KEY_MISMATCH
    msg = last.message or "没有找到微信数据库密钥"
    if (raw_env or file_key) and status == KEY_MISMATCH:
        msg = (
            "已找到密钥，但与当前微信数据库不匹配（salt 校验失败）。"
            "通常是换了微信号、微信重装/升级，或填的是别的机器上的密钥。"
            "请重新提取一次。"
        )
    return KeyResult(status, last.key, last.source, msg, trace, db_path)


def keyring_path() -> Path:
    """密钥环落盘位置。WX_KEYRING_PATH 可重定向（自测/多账号隔离用）。"""
    custom = (os.environ.get("WX_KEYRING_PATH") or "").strip()
    if custom:
        return Path(custom).expanduser()
    return common.WORK / "wx_keys.json"


def ring_gap(storage: Path, ring: dict[str, str]) -> list[Path]:
    """返回「有加密库、但密钥环里没有对应 salt」的库。"""
    if not storage.is_dir():
        return []
    out = []
    # 整个 db_storage 都算：镜像会把 contact/session 一起解，缺一把就少一批数据
    for src in storage.rglob("*.db"):
        salt = wxdecrypt.db_salt(src)
        if salt and salt not in ring:
            out.append(src)
    return out


def load_keyring(env: dict | None = None) -> dict[str, str]:
    """读「每库一密钥」映射。微信 4.1+ 必须用它，单把密钥只能开一个库。"""
    out: dict[str, str] = {}
    try:
        raw = keyring_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            for salt, key in (data.get("keys") or data).items():
                k = normalize_key(str(key))
                s = str(salt).strip().lower()
                if k and len(s) == 32:
                    out[s] = k
    except (OSError, ValueError, AttributeError):
        pass
    # .env 里的 WX_DBKEY 只对应 message_0.db，兜底补进去
    single = normalize_key((env or {}).get("WX_DBKEY", ""))
    if single and out:
        try:
            storage, _roots = pick_storage(((env or {}).get("WX_DATA_PATH") or "").strip())
            if storage:
                salt = wxdecrypt.db_salt(message_db(storage))
                if salt and salt not in out:
                    out[salt] = single
        except OSError:
            pass
    return out


def save_keyring(keyring: dict[str, str]) -> str:
    if not keyring:
        return ""
    path = keyring_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        common.atomic_write_text(
            path,
            json.dumps({"updated_at": int(time.time()), "keys": keyring},
                       ensure_ascii=False, indent=2) + "\n",
        )
        return str(path)
    except OSError:
        return ""


def store_key(key_hex: str, env_path: Path | None = None, env: dict | None = None,
              keyring: dict[str, str] | None = None) -> list[str]:
    """把已验证的密钥写进 .env 与 vendor/key_windows.txt（原子写）。返回写到的位置。

    keyring 非空时另写 work/wx_keys.json —— 微信 4.1+ 每个库密钥不同，
    只有这一个文件能让所有 message_N.db 都解得开。
    """
    written: list[str] = []
    key_hex = normalize_key(key_hex)
    if keyring:
        where = save_keyring({k: normalize_key(v) for k, v in keyring.items() if normalize_key(v)})
        if where:
            written.append(where)
    if not key_hex:
        return written
    try:
        common.save_env({"WX_DBKEY": key_hex}, env_path or common.ENV_PATH)
        written.append(str(env_path or common.ENV_PATH))
    except Exception as exc:  # noqa: BLE001
        written.append(f"(写入 .env 失败: {exc})")
    for path in key_file_candidates(env):
        if "vendor" in path.parts or path == common.WORK / "wx_key.txt":
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                common.atomic_write_text(path, key_hex + "\n")
                written.append(str(path))
                break
            except OSError:
                continue
    os.environ["WX_DBKEY"] = key_hex
    return written


def diagnose(env: dict) -> dict:
    """给控制台/排障用的一份体检报告（不含明文密钥）。"""
    storage, roots = pick_storage((env.get("WX_DATA_PATH") or "").strip())
    pids = _wechat_pids()
    report = {
        "wechat_installed": bool(roots),
        "storage": str(storage) if storage else "",
        "storage_candidates": [str(r) for r in roots[:5]],
        "account": account_of(storage) if storage else "",
        "running": bool(pids),
        "processes": [{"pid": p, "mb": m // 1024} for p, m in pids],
        "key_in_env": bool(normalize_key(env.get("WX_DBKEY", ""))),
        "key_file": "",
        "key_state": "",
        "mirror": "",
    }
    key, kf = read_key_file(env)
    report["key_file"] = str(kf) if kf else ""
    if storage:
        db = message_db(storage)
        report["db"] = str(db)
        report["db_exists"] = db.exists()
        if db.exists():
            if key:
                ok, why = validate(key, db)
                report["key_state"] = "OK" if ok else f"MISMATCH: {why}"
            else:
                report["key_state"] = "NOT_FOUND"
        mirror = mirror_root(env) / account_of(storage) / "db_storage"
        report["mirror"] = str(mirror)
        report["mirror_ready"] = (mirror / "message" / "message_0.db").exists()
    else:
        report["key_state"] = "NO_STORAGE"
    return report


def mirror_root(env: dict | None = None) -> Path:
    """明文镜像放程序自己的 work/ 下，不放 vendor（用户包可能没有 vendor）。"""
    custom = (env or {}).get("WX_MIRROR_DIR", "").strip()
    if custom:
        return Path(custom).expanduser()
    return common.WORK / "wx_mirror"


# 命令行：不打开控制台也能取密钥 / 排障
def _cli(argv: list[str]) -> int:
    import argparse  # noqa: PLC0415

    ap = argparse.ArgumentParser(description="微信 4.x 数据库密钥工具（只读、先校验后落盘）")
    ap.add_argument("action", nargs="?", default="status",
                    choices=["status", "scan", "acquire", "paste", "decrypt"])
    ap.add_argument("key", nargs="?", default="", help="paste 动作要写入的 64 位密钥")
    ap.add_argument("--seconds", type=float, default=25.0, help="内存扫描时长上限")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（给脚本/定时任务用）")
    args = ap.parse_args(argv)

    env = common.load_env()
    storage, roots = pick_storage((env.get("WX_DATA_PATH") or "").strip())
    if storage is None:
        msg = "没找到微信数据目录（xwechat_files/<wxid>/db_storage）。请先登录电脑版微信 4.x。"
        print(json_dumps({"ok": False, "message": msg}) if args.json else msg)
        return 2
    db = message_db(storage)

    if args.action == "status":
        rep = diagnose(env)
        if args.json:
            print(json_dumps(rep))
        else:
            print("=" * 62)
            print("  微信通道体检")
            print("=" * 62)
            print(f"  数据目录 : {rep.get('storage')}")
            print(f"  账号     : {rep.get('account')}")
            print(f"  微信进程 : {'运行中 ' + str(rep.get('processes')) if rep.get('running') else '未运行'}")
            print(f"  密钥状态 : {rep.get('key_state')}")
            print(f"  密钥文件 : {rep.get('key_file') or '—'}")
            print(f"  密钥环   : {len(load_keyring(env))} 个库（work\\wx_keys.json）")
            print(f"  明文镜像 : {rep.get('mirror')} {'(就绪)' if rep.get('mirror_ready') else '(未生成)'}")
            print(f"  AES 后端 : {wxdecrypt.backend_name()}")
            print("=" * 62)
        return 0

    if args.action == "scan":
        res = scan_memory_for_key(db, timeout=args.seconds)
        out = {"ok": res.ok, "status": res.status, "message": res.message,
               "key": res.key, "notes": res.notes}
        print(json_dumps(out) if args.json else
              f"{'✅' if res.ok else '❌'} {res.message}\n" + "\n".join(f"  · {n}" for n in res.notes))
        return 0 if res.ok else 1

    if args.action == "paste":
        key = normalize_key(args.key)
        if not key:
            print("请给出 64 位十六进制密钥：python tools\\wxkey.py paste <key>")
            return 2
        ok, why = validate(key, db)
        print(("✅ " if ok else "❌ ") + why)
        if ok:
            print("已写入: " + ", ".join(store_key(key, env=env)))
        return 0 if ok else 1

    if args.action == "acquire":
        res = acquire(env, db, allow_scan=True, timeout=args.seconds)
        if not res.ok:
            print(f"❌ {res.message}")
            for n in res.notes:
                print(f"  · {n}")
            return 1
        written = store_key(res.key, env=env, keyring=res.keyring)
        mirror = mirror_root(env) / account_of(storage) / "db_storage"
        dres = wxdecrypt.decrypt_storage(storage, mirror, res.keyring or bytes.fromhex(res.key))
        print(f"✅ {res.message}（来源 {res.source}）")
        print(f"   已写入: {', '.join(written[:2])}")
        print(f"   镜像: {dres.message} → {mirror}")
        return 0 if dres.ok else 1

    # decrypt：用已配置的密钥重解一次镜像
    key, _kf = read_key_file(env)
    key = normalize_key(env.get("WX_DBKEY", "")) or key
    if not key:
        print("❌ 还没有可用密钥，先执行 acquire")
        return 1
    ok, why = validate(key, db)
    if not ok:
        print(f"❌ {why}")
        return 1
    mirror = mirror_root(env) / account_of(storage) / "db_storage"
    dres = wxdecrypt.decrypt_storage(storage, mirror, bytes.fromhex(key))
    print(f"{'✅' if dres.ok else '❌'} {dres.message}（{dres.elapsed_ms} ms）→ {mirror}")
    for n in dres.notes:
        print(f"  · {n}")
    return 0 if dres.ok else 1


def json_dumps(obj) -> str:
    import json  # noqa: PLC0415

    return json.dumps(obj, ensure_ascii=False)


__all__ = [
    "OK", "NOT_CONFIGURED", "NO_WECHAT", "KEY_MISMATCH", "SCAN_FAILED",
    "KeyResult", "normalize_key", "find_storage_roots", "pick_storage", "message_db",
    "account_of", "read_key_file", "key_file_candidates", "validate",
    "scan_memory_for_key", "acquire", "store_key", "diagnose", "mirror_root",
]


if __name__ == "__main__":
    import sys  # noqa: PLC0415

    sys.exit(_cli(sys.argv[1:]))
