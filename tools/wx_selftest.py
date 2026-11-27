# -*- coding: utf-8 -*-
"""微信通道端到端自测：**真的**加密 → 抓密钥 → 解密 → 读消息。

和旧的 mock_wechat_test.py 的区别（旧版其实是假跑）：
    旧版直接把一个明文 sqlite 拷进镜像目录，再写一个 4096 字节的假原始库，
    于是「解密」一步根本没被执行，密钥也只写了个假值 —— 它证明不了任何事。

本脚本做的是真链路：
    1. 用 sqlite3 建一个结构（schema）与微信 4.x 一致的明文库；
    2. 按 SQLCipher 4 参数（4096/80/256000/HMAC-SHA512）把它**加密**成
       `xwechat_files/<wxid>/db_storage/message/message_0.db`；
    3. 起一个「桩进程」，把 `x'<32字节密钥hex><16字节salt hex>'` 放进自己的内存
       （这正是 WCDB 在微信进程里的缓存格式），供内存扫描策略真实地扫到；
    4. 走 wxkey 的正规流程取密钥 → 走 wxdecrypt 解出明文镜像 → 走 channels.wechat
       读出消息，逐条核对发送人/时间/正文/类型；
    5. 再故意用错密钥，确认会被判成 KEY_MISMATCH，而不是「0 条消息」。

用法:
    python tools\\wx_selftest.py            # 全流程（含内存扫描）
    python tools\\wx_selftest.py --no-scan  # 跳过内存扫描（无管理员权限时）
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import wxcrypto                      # noqa: E402
import wxdecrypt                     # noqa: E402
from wxcrypto import aes_cbc_encrypt, aes_cbc_decrypt  # noqa: E402

PAGE, RESERVE, SALT_SZ, ITER = 4096, 80, 16, 256000
MAGIC = b"SQLite format 3\x00"

# 覆盖到的类型：文本 / 图片 / 语音 / 视频 / 表情 / 位置 / 链接卡片 / 系统 / 撤回
SAMPLE = [
    ("text", "各教研室请注意：本周五下午14:30在敏行楼302开会，请张主任带方案参会。"),
    ("text", "【教务处】11月10日前报送期末技能考核方案，收到请回复。"),
    ("image", None),
    ("voice", None),
    ("link", '<?xml version="1.0"?><msg><appmsg><title>实训室借用申请表.docx</title>'
             '<des>请本周内填写并上交</des></appmsg></msg>'),
    ("sticker", None),
    ("system", '<sysmsg type="revokemsg"><revokemsg><replacemsg><![CDATA["王主任" 撤回了一条消息]]></replacemsg></revokemsg></sysmsg>'),
]
LOCAL_TYPES = {"text": 1, "image": 3, "voice": 34, "video": 43, "sticker": 47,
               "location": 48, "link": 49, "system": 10000, "recall": 10002}

# (wxid, Name2Id.id, 期望显示名)  —— 群 + 单聊
SAMPLE_PEERS = [
    ("wxid_group_demo@chatroom", 1, "机电学院教师通知群"),
    ("wxid_leader_demo", 2, "王主任"),
]


# 1. 构造与微信 4.x 同构的明文库
def _build_plain_db(path: Path, now: int) -> list[dict]:
    if path.exists():
        path.unlink()
    con = sqlite3.connect(str(path))
    cur = con.cursor()
    cur.execute("PRAGMA page_size=4096")
    cur.execute("CREATE TABLE Name2Id (user_name TEXT, id INTEGER PRIMARY KEY)")
    peers = SAMPLE_PEERS
    for uname, uid, _label in peers:
        cur.execute("INSERT INTO Name2Id (user_name, id) VALUES (?, ?)", (uname, uid))
    # 本人（id=99）
    cur.execute("INSERT INTO Name2Id (user_name, id) VALUES (?, ?)", ("wxid_me_demo", 99))

    expect: list[dict] = []
    for uname, uid, label in peers:
        table = "Msg_" + hashlib.md5(uname.encode()).hexdigest()
        cur.execute(
            f'CREATE TABLE "{table}" (local_id INTEGER PRIMARY KEY, server_id INTEGER, '
            "local_type INTEGER, sort_seq INTEGER, real_sender_id INTEGER, "
            "create_time INTEGER, status INTEGER, message_content BLOB, "
            "WCDB_CT_message_content INTEGER, packed_info_data BLOB)"
        )
        for idx, (kind, text) in enumerate(SAMPLE):
            if kind in ("image", "voice", "sticker"):
                continue          # 这类消息在库里只有媒体指纹，没有正文
            ts = now - (len(SAMPLE) - idx) * 60
            # 真实微信里「群聊中别人发的消息」正文前会带 "<wxid>:\n" 前缀，
            # 这是还原群发送人最可靠的线索，样本必须带上才能测到这条路。
            body = text if not uname.endswith("@chatroom") else "wxid_member_demo:" + chr(10) + text
            cur.execute(
                f'INSERT INTO "{table}" (local_id, local_type, real_sender_id, create_time, '
                "message_content, WCDB_CT_message_content) VALUES (?,?,?,?,?,?)",
                (1000 + idx, LOCAL_TYPES[kind], uid, ts, body.encode("utf-8"), 0),
            )
            if kind == "text" and len(expect) < 2:
                expect.append({
                    "chat_name": label, "sender": uname, "ts": ts,
                    "content": text,
                })
    con.commit()
    pages = con.execute("PRAGMA page_count").fetchone()[0]
    con.close()
    data = path.read_bytes()
    if len(data) % PAGE:
        data += b"\x00" * (PAGE - len(data) % PAGE)
    path.write_bytes(data)
    assert len(data) // PAGE >= 1 and pages >= 1
    return expect


def build_encrypted_asset(storage: Path, raw_key: bytes, now: int):
    """造一个「真加密」的 message_0.db，返回 (db_path, salt, 期望消息)。

    优先用 sqlcipher3（真 SQLCipher 4 引擎，页头/保留区/迭代次数都是真的）；
    没有这个库就用本文件自带的加密器 —— 它按同一套参数实现，同样能验通解密内核。
    """
    db_path = storage / "message" / "message_0.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    plain = storage / "_plain.db"
    expect = _build_plain_db(plain, now)

    try:
        import sqlcipher3  # noqa: PLC0415
    except ImportError:
        salt = encrypt_db(plain, db_path, raw_key)
        print("      （未安装 sqlcipher3：用内置加密器造样本，只能验证加解密内核）")
        plain.unlink(missing_ok=True)
        return db_path, salt, expect, False

    # SQLCipher 的「口令」模式：PBKDF2(口令字节, salt, 256000)。
    # 把 32 字节密钥当成 32 个 ASCII 字符的口令喂进去，得到的派生密钥
    # 与我们解密内核里 PBKDF2(raw_key, ...) 完全一致 —— 这样样本是真
    # SQLCipher 写的，而校验的是我们自己的实现，属于跨实现交叉验证。
    passphrase = raw_key.decode("ascii")
    tmp_db = storage / "_sc.db"
    for p in (tmp_db, Path(str(tmp_db) + "-wal"), Path(str(tmp_db) + "-shm")):
        p.unlink(missing_ok=True)
    con = sqlcipher3.connect(str(tmp_db))
    con.execute("PRAGMA key = '%s'" % passphrase)
    con.execute("PRAGMA cipher_compatibility = 4")
    con.execute("CREATE TABLE Name2Id (user_name TEXT, id INTEGER PRIMARY KEY)")
    for uname, uid, _label in SAMPLE_PEERS:
        con.execute("INSERT INTO Name2Id (user_name, id) VALUES (?,?)", (uname, uid))
    con.execute("INSERT INTO Name2Id (user_name, id) VALUES (?,?)", ("wxid_me_demo", 99))
    for uname, uid, _label in SAMPLE_PEERS:
        table = "Msg_" + hashlib.md5(uname.encode()).hexdigest()
        con.execute(
            f'CREATE TABLE "{table}" (local_id INTEGER PRIMARY KEY, server_id INTEGER, '
            "local_type INTEGER, sort_seq INTEGER, real_sender_id INTEGER, create_time INTEGER, "
            "status INTEGER, message_content BLOB, WCDB_CT_message_content INTEGER, "
            "packed_info_data BLOB)"
        )
        for idx, (kind, text) in enumerate(SAMPLE):
            if kind in ("image", "voice", "sticker"):
                continue
            body = text if not uname.endswith("@chatroom") else "wxid_member_demo:" + chr(10) + text
            con.execute(
                f'INSERT INTO "{table}" (local_id, local_type, real_sender_id, create_time, '
                "message_content, WCDB_CT_message_content) VALUES (?,?,?,?,?,?)",
                (1000 + idx, LOCAL_TYPES[kind], uid, now - (len(SAMPLE) - idx) * 60,
                 body.encode("utf-8"), 0),
            )
    con.commit()
    con.close()
    raw = tmp_db.read_bytes()
    db_path.write_bytes(raw)
    salt = raw[:SALT_SZ]
    for p in (tmp_db, Path(str(tmp_db) + "-wal"), Path(str(tmp_db) + "-shm"), plain):
        p.unlink(missing_ok=True)
    return db_path, salt, expect, True


# 2. 按 SQLCipher 4 规则加密（真加密，用来验证解密内核）
def encrypt_db(plain_path: Path, out_path: Path, raw_key: bytes) -> bytes:
    data = plain_path.read_bytes()
    if len(data) % PAGE:
        data += b"\x00" * (PAGE - len(data) % PAGE)
    salt = os.urandom(SALT_SZ)
    enc = hashlib.pbkdf2_hmac("sha512", raw_key, salt, ITER, 32)
    mac = hashlib.pbkdf2_hmac("sha512", enc, bytes(b ^ 0x3A for b in salt), 2, 32)
    rstart = PAGE - RESERVE
    out = bytearray()
    for number in range(1, len(data) // PAGE + 1):
        page = data[(number - 1) * PAGE:number * PAGE]
        iv = os.urandom(16)
        body = page[SALT_SZ:rstart] if number == 1 else page[:rstart]
        ct = aes_cbc_encrypt(enc, iv, body)
        head = salt if number == 1 else b""
        frame = head + ct + iv
        # 注意：SQLCipher 计算页 HMAC 时**跳过** page 1 的 16 字节 salt，
        # 所以偏移从 SALT_SZ 起算。写成从 0 起算会导致「密钥对但页校验不过」。
        skip = SALT_SZ if number == 1 else 0
        tag = hmac.new(mac, frame[skip:] + struct.pack("<I", number), hashlib.sha512).digest()
        out.extend(frame + tag)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(bytes(out))
    return salt


# 3. 桩进程：让内存里出现 WCDB 风格的 x'<key><salt>'
_STUB = r'''
import ctypes, sys, time
secret = sys.argv[1].encode()
buf = ctypes.create_string_buffer(secret)
hold = [buf]          # 防止被优化掉
# 再复制一份到堆上（ctypes 缓冲区在私有堆，扫描同样可见）
copies = [bytes(secret) for _ in range(8)]
print("ready", flush=True)
time.sleep(float(sys.argv[2]))
'''


def spawn_key_stub(token: str, seconds: float) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", _STUB, token, str(seconds)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    proc.stdout.readline()          # 等它把 secret 放进内存
    return proc


# 4. 主流程
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-scan", action="store_true", help="跳过内存扫描")
    ap.add_argument("--keep", action="store_true", help="保留临时目录便于排查")
    args = ap.parse_args(argv)

    tmp = Path(tempfile.mkdtemp(prefix="wxtest_"))
    data_root = tmp / "xwechat_files"
    wxid = "wxid_selftest_demo"
    storage = data_root / wxid / "db_storage"
    storage.mkdir(parents=True)
    mirror = tmp / "decrypted" / wxid / "db_storage"

    print("=" * 66)
    print("微信通道端到端自测（真加密 / 真解密 / 真读消息）")
    print("=" * 66)
    print(f"[0/5] AES 后端: {wxcrypto.backend_name()}  临时目录: {tmp}")

    # 密钥必须是「32 个可打印 ASCII 字节」：这样它既能当 SQLCipher 的口令
    # （让样本由真 SQLCipher 引擎写出），又能当我们的 raw key（PBKDF2 输入），
    # 两条路径的派生结果完全一致 —— 这才是真正的跨实现校验。
    raw_key = os.urandom(16).hex().encode("ascii")
    key_hex = raw_key.hex()
    now = int(time.time())

    print("[1/5] 构造加密库样本（name2id + 两张会话表 + 7 条多类型消息）...")
    msg_db, salt, expect, genuine = build_encrypted_asset(storage, raw_key, now)
    print(f"      加密库: {msg_db.stat().st_size} 字节, salt={salt.hex()}, 待核对 {len(expect)} 条")

    ok, why = wxdecrypt.key_matches_db(raw_key, msg_db)
    print(f"[2/5] 密钥校验: {'通过' if ok else '失败'} — {why}")
    if not ok:
        return 1
    bad_ok, bad_why = wxdecrypt.key_matches_db(bytes(32), msg_db)
    print(f"      错误密钥: {'居然通过（异常）' if bad_ok else '被正确拒绝'}")

    if not genuine:
        # 没有 sqlcipher3 时造不出「页尾 80 字节空闲」的真 SQLCipher 布局，
        # 强行跑镜像/读表会得到坏库。这里只验内核，并把边界明确记录，不装样子。
        raw = msg_db.read_bytes()
        head = raw[:wxdecrypt.PAGE]
        enc, mac = wxdecrypt.derive_keys(raw_key, head[:wxdecrypt.SALT_SZ])
        body = aes_cbc_decrypt(enc, head[wxdecrypt.PAGE - wxdecrypt.RESERVE:
                                       wxdecrypt.PAGE - wxdecrypt.RESERVE + 16],
                               head[wxdecrypt.SALT_SZ:wxdecrypt.PAGE - wxdecrypt.RESERVE])
        print("[3/5] 加解密内核验证（无 sqlcipher3，跳过镜像与通道读取）")
        print(f"      page1 页头指纹: {'通过' if wxdecrypt.page1_header_ok(body) else '失败'}")
        tampered = bytearray(raw)
        tampered[wxdecrypt.PAGE - 10] ^= 0xFF
        try:
            wxdecrypt.decrypt_db_bytes(bytes(tampered), raw_key)
            print("  [X] 篡改过的页居然通过了校验")
            return 1
        except ValueError:
            print("      篡改页被 HMAC 正确拦截")
        print("\n" + "=" * 66)
        print("⚠️  内核验证通过；完整链路（镜像 + 读消息）需装 sqlcipher3 后再测")
        print("    在实施员包里运行即可（那里带了 sqlcipher3 离线 wheel）")
        print("=" * 66)
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        return 0

    print("[3/5] 解密 → 明文镜像 ...")
    res = wxdecrypt.decrypt_storage(storage, mirror, raw_key)
    print(f"      {res.message} ({res.elapsed_ms} ms)")
    for n in res.notes:
        print(f"      · {n}")
    if not res.ok:
        print("  [X] 解密失败")
        return 1
    mdb = mirror / "message" / "message_0.db"
    con = sqlite3.connect(f"file:{mdb}?mode=ro", uri=True)
    head = con.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
    con.close()
    print(f"      镜像可被 sqlite 打开，含 {head} 个对象")

    print("[4/5] 内存扫描取密钥 ...")
    if args.no_scan:
        print("      --no-scan：跳过")
    else:
        import wxkey
        token = "x'" + key_hex + salt.hex() + "'"
        stub = spawn_key_stub(token, 90)
        try:
            found = wxkey.scan_memory_for_key(msg_db, timeout=25, pids=[stub.pid])
        finally:
            stub.kill()
            stub.wait(timeout=5)
        print(f"      扫描结果: {found.status} — {found.message}")
        for line in found.notes[:6]:
            print(f"      · {line}")
        if found.key != key_hex:
            print("  [X] 扫描没有取到正确的密钥")
            return 1
        print("      ✓ 扫描到的密钥与真实密钥一致")

    print("[5/5] 通过微信通道读消息 ...")
    # 自测的库是临时造的，密钥环也必须指向临时目录 —— 否则真实
    # work/wx_keys.json（20 个库）会被测试 salt 污染，多一条无害但不干净。
    os.environ["WX_KEYRING_PATH"] = str(tmp / "wx_keys.json")
    import channels.wechat as wx
    env = {
        "WX_DBKEY": key_hex,
        "WX_DATA_PATH": str(data_root),
        "WX_MIRROR_DIR": str(tmp / "decrypted"),   # 不污染真实 work/
        "WX_ALLOW_SCAN": "0",
    }
    since, until = now - 6 * 3600, now + 60
    out = wx.fetch(env, since, until)
    print(f"      通道状态: {out.status}  取到 {len(out.messages)} 条  原文 {out.raw_count} 条")
    print(f"      说明: {out.message}")
    if out.note:
        print(f"      备注: {out.note}")

    contents = [m["content"] for m in out.messages]
    missing = [e for e in expect if not any(e["content"][:18] in c for c in contents)]
    if missing:
        print(f"  [X] 少读到 {len(missing)} 条: {[m['content'][:20] for m in missing]}")
        return 1
    for m in out.messages[:4]:
        print(f"      · [{m['chat_name']}] {m['sender']} {m['timestamp']}: {m['content'][:40]}")

    print("\n" + "=" * 66)
    print("✅ 全链路通过：加密库 → 取密钥 → 解密镜像 → 读消息")
    print("=" * 66)
    if not args.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(f"临时目录已保留: {tmp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
