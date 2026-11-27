"""群名 / 昵称映射：让"谁在哪个群说了什么"变得可读。

为什么需要它：nt_msg.db 里发送人只有 uid（`u_zmzcVrhp-zhUXQu9mY7l-Q` 这种），
群只有群号（1073282949）。院长看到这些等于没看到。

同一目录下的两个辅助库用同一把密钥、同样的 1024 字节头，实测（QQ 9.9.36）
可得下列映射，本模块就是把它们读出来：

    group_info.db    group_list      60001=群号  60007=群名
                     group_member3   60001=群号  1000=uid  1002=uin
                                     64003=群名片  20002=昵称
    profile_info.db  profile_info_v6 1000=uid  1002=uin  20002=昵称  20009=群昵称

设计原则：**任何一步失败都静默降级**。昵称只是锦上添花，
解不开辅助库绝不能让主流程（抽消息）挂掉或误报。
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import common
import qqdb

GROUP_DB = "group_info.db"
PROFILE_DB = "profile_info.db"

# 实测固化的列号（schema.json 的 aux 段可覆盖）
COLS_GROUP_LIST = ("60001", "60007")
COLS_GROUP_MEMBER = ("60001", "1000", "1002", "64003", "20002")
COLS_PROFILE = ("1000", "1002", "20002", "20009")


@dataclass
class Names:
    groups: dict[str, str] = field(default_factory=dict)          # 群号 -> 群名
    member_uin: dict[tuple[str, str], str] = field(default_factory=dict)  # (群号, uid) -> 群名片
    nick_by_uid: dict[str, str] = field(default_factory=dict)     # uid -> 昵称
    nick_by_uin: dict[str, str] = field(default_factory=dict)     # uin -> 昵称
    self_uids: set[str] = field(default_factory=set)              # 本人 uid（多设备可能不止一个）
    ok: bool = False
    note: str = ""

    # ---- 对外查询 ----
    def group_name(self, chat_id: str) -> str:
        cid = (chat_id or "").strip()
        if not cid:
            return ""
        if cid in self.groups:
            return self.groups[cid]
        # 有些版本消息里存的是群 uin，群名表里按群号存 —— 只做一次宽松匹配
        for k, v in self.groups.items():
            if k.endswith(cid) or cid.endswith(k):
                return v
        return ""

    def is_me(self, uid: str) -> bool:
        """这条消息是不是本人自己发的。

        c2c 表的 40020 是发送人；若它等于本人 uid，说明是"我发出去"的消息。
        以前没做这个区分，会把本人昵称当成对方名字，导致"我私聊老师"被读成"老师私聊我"。
        """
        return bool(uid) and (uid or "").strip() in self.self_uids

    def person(self, chat_id: str, uid: str, uin: str = "") -> str:
        """优先群名片，其次昵称，再退回 uin/uid。"""
        cid, uid = (chat_id or "").strip(), (uid or "").strip()
        uin = (uin or "").strip()

        name = self.member_uin.get((cid, uid), "")
        if name:
            return name
        if uid and uid in self.nick_by_uid:
            return self.nick_by_uid[uid]
        if uin and uin in self.nick_by_uin:
            return self.nick_by_uin[uin]
        return ""


def _open_aux(path: Path, key: str, tmp_dir: Path, profile: str | None):
    """辅助库同样是「1024 头 + SQLCipher」，剥完头按已知参数打开。

     红线：绝不对原位库做任何读写。
    以前这里直接 `qqdb.strip_header(path, plain)`，而 strip_header 内部会对
    **原位目录**执行 copy2(src+"-wal"/"-shm")；QQ 在线时这些文件被独占持有，
    Windows 上直接抛 PermissionError(WinError 32)，于是昵称/群名全部降级为空，
    清单退化成数字 UID。现在改成：先 copy_db_set 到沙箱目录，再对副本剥头解密。
    """
    if not path.exists():
        return None

    # 1) 先把辅助库整体拷进沙箱（只拷 db + -wal，-shm 由 SQLite 自行重建）
    #    QQ 正在高频写群消息时，copy2 仍可能撞上瞬时独占，重试几次再放弃；
    #    放弃只是"没有真名"，绝不影响主流程。
    aux_copy_dir = tmp_dir / f"aux_raw_{path.stem}"
    files: dict = {}
    for attempt in range(3):
        try:
            files = qqdb.copy_db_set(path, aux_copy_dir, stem=path.name)
            break
        except OSError:
            if attempt == 2:
                return None
            time.sleep(0.4)
    raw_copy = files.get("db")
    if not raw_copy or not Path(raw_copy).exists():
        return None

    # 2) 只对副本剥头，原位文件一个字节都不碰
    tmp_dir.mkdir(parents=True, exist_ok=True)
    plain = tmp_dir / ("aux_" + path.name)
    try:
        qqdb.strip_header(Path(raw_copy), plain)
    except Exception:  # noqa: BLE001
        return None

    sqlcipher = qqdb._sqlcipher()  # noqa: SLF001 —— 复用同一份绑定，避免重复 import 分支
    con = sqlcipher.connect(str(plain))
    try:
        # 与主库完全相同的参数顺序（key 先、参数后），这里不探测，直接照主库 profile 来
        con.execute(f"PRAGMA key = '{key.replace(chr(39), chr(39) * 2)}';")
        stmts = qqdb.profile_statements(profile)
        for stmt in stmts:
            con.execute(stmt)
        con.execute("SELECT count(*) FROM sqlite_master;").fetchone()
        return con
    except Exception:  # noqa: BLE001
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass
        return None


def _has_col(con, table: str, col: str) -> bool:
    try:
        return any(r[1] == col for r in con.execute(f'PRAGMA table_info({common.quote_ident(table)})'))
    except Exception:  # noqa: BLE001
        return False


def load(db_dir: Path, key: str, tmp_dir: Path, profile: str | None = None,
         self_uin: str = "") -> Names:
    """读两个辅助库。全部失败就返回空 Names（ok=False），调用方无感降级。"""
    out = Names()
    if not key:
        out.note = "无密钥，跳过昵称库"
        return out

    # ---------- group_info.db：群名 + 群成员名片 ----------
    try:
        con = _open_aux(db_dir / GROUP_DB, key, tmp_dir, profile)
        if con is not None:
            try:
                c_id, c_name = COLS_GROUP_LIST
                if _has_col(con, "group_list", c_id) and _has_col(con, "group_list", c_name):
                    for cid, name in con.execute(
                        f'SELECT "{c_id}", "{c_name}" FROM "group_list"'
                    ):
                        cid, name = str(cid or "").strip(), str(name or "").strip()
                        if cid and name:
                            out.groups[cid] = name

                if _has_col(con, "group_member3", "1000"):
                    g_col, uid_col, uin_col, card_col, nick_col = COLS_GROUP_MEMBER
                    sql = (
                        f'SELECT "{g_col}", "{uid_col}", "{uin_col}", "{card_col}", "{nick_col}" '
                        f'FROM "group_member3"'
                    )
                    for gid, uid, uin, card, nick in con.execute(sql):
                        gid = str(gid or "").strip()
                        uid = str(uid or "").strip()
                        uin = str(uin or "").strip()
                        name = str(card or "").strip() or str(nick or "").strip()
                        if not name:
                            continue
                        if gid and uid:
                            out.member_uin[(gid, uid)] = name
                        if uid and name:
                            out.nick_by_uid.setdefault(uid, name)
                        if uin and name:
                            out.nick_by_uin.setdefault(uin, name)
                out.ok = True
            finally:
                con.close()
    except Exception as exc:  # noqa: BLE001
        out.note = f"群资料库读取失败：{exc}"

    # ---------- profile_info.db：好友/联系人昵称 ----------
    try:
        con = _open_aux(db_dir / PROFILE_DB, key, tmp_dir, profile)
        if con is not None:
            try:
                if _has_col(con, "profile_info_v6", "1000"):
                    uid_col, uin_col, nick_col, gnick_col = COLS_PROFILE
                    sql = (
                        f'SELECT "{uid_col}", "{uin_col}", "{nick_col}", "{gnick_col}" '
                        f'FROM "profile_info_v6"'
                    )
                    want_uin = str(self_uin or "").strip()
                    for uid, uin, nick, gnick in con.execute(sql):
                        uid, uin = str(uid or "").strip(), str(uin or "").strip()
                        # 本人 uid 要在 name 判空之前收集：本机实测本人有一条记录昵称为空
                        if want_uin and uin == want_uin and uid:
                            out.self_uids.add(uid)
                        name = str(nick or "").strip() or str(gnick or "").strip()
                        if not name:
                            continue
                        if uid:
                            out.nick_by_uid.setdefault(uid, name)
                        if uin:
                            out.nick_by_uin.setdefault(uin, name)
                out.ok = True
            finally:
                con.close()
    except Exception as exc:  # noqa: BLE001
        if not out.note:
            out.note = f"联系人库读取失败：{exc}"

    return out


def cleanup(tmp_dir: Path) -> None:
    shutil.rmtree(tmp_dir, ignore_errors=True)
