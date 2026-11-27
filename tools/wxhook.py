#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信 4.1+ 启动瞬间抓密钥（只读挂载）。

为什么需要这个模块
微信 4.0 时代，WCDB 会把 ``x'<64hex key><32hex salt>'`` 缓存在进程内存里，
直接扫内存就能拿到。**微信 4.1 起这条路径失效**：SQLCipher 派生完页密钥后
会把 raw key 清零（cipher_memory_security），静态内存里只剩：

  * 明文页缓存（能看到 sqlite_master 的 schema 文本），
  * 以及被 AES-NI 展开成轮密钥的 enc_key（不是连续 32 字节，扫不出来）。

因此唯一可靠的时机是**微信启动、打开数据库、跑 PBKDF2 的那一瞬间**：
PBKDF2-HMAC-SHA512 初始化 HMAC 时会构造 ipad block::

    ipad = (key XOR 0x36)[32] || 0x36 * 96      # key 是 32 字节时

这个 128 字节块里前 32 字节就是 key 的明文 XOR 形式，且不受后续清零影响。
本模块在 ``sha512_block_data_order`` 入口挂载，只对 ``num == 1``（单块）
的调用读 128 字节，命中后 XOR 回来，再用当前数据库 page1 校验，通过才返回。

地址不写死
不同微信版本的 RVA 不同，所以每次从 ``Weixin.dll`` 里现找：

  1. 找字符串 ``SHA512 block transform for x86_64``（OpenSSL 版权串，唯一）；
  2. 往后扫到函数入口特征
     ``48 89 7C 24 08 48 89 74 24 10 48 89 E0 48 89 CF 48 89 D6 4C 89 C2``；
  3. 用 PE 节表把文件偏移换算成 RVA。

实测 4.1.15.56: 字符串 @0x5a18f1c → 入口 RVA 0x5a18f80，与本机进程一致。

用法
----
    python tools/wxhook.py status          # 环境自检（不挂载）
    python tools/wxhook.py capture         # 等待微信重启并抓取
    python tools/wxhook.py capture --timeout 240
"""

from __future__ import annotations

import json
import mmap
import os
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
import wxdecrypt  # noqa: E402
import wxkey  # noqa: E402

# OpenSSL 写在函数前面的版权串，4.0 / 4.1 都有
_OPENSSL_MARK = b"SHA512 block transform for x86_64"
# sha512_block_data_order 的入口特征（x86_64, CRYPTOGAMS）
_ENTRY_SIG = bytes.fromhex(
    "48897c240848897424104889e04889cf4889d64c89c2"
)
_CACHE_PATH = common.WORK / "wxhook_cache.json"
_ROOT = Path(__file__).resolve().parent.parent
_FRIDA_VER = "16.7.19"   # 17.x 在 Win10 上报 "Unable to locate the ntdll loader TLS routines"

JS_TEMPLATE = r"""
var RVA = %RVA%;
var moduleName = "Weixin.dll";
var installed = false;
var hits = {};
var sent = 0;

function hexOf(bytes, xor) {
  var s = "";
  for (var i = 0; i < 32; i++) {
    var v = (bytes[i] ^ xor) & 0xff;
    s += ("0" + v.toString(16)).slice(-2);
  }
  return s;
}

function looksLikePad(bytes, pad) {
  for (var i = 32; i < 128; i++) {
    if (bytes[i] !== pad) return false;
  }
  return true;
}

function install() {
  var m = Process.findModuleByName(moduleName);
  if (!m) { setTimeout(install, 150); return; }
  var entry = m.base.add(RVA);
  send({ type: "module", base: m.base.toString(), entry: entry.toString() });
  Interceptor.attach(entry, {
    onEnter: function () {
      // Windows x64: rcx=ctx, rdx=data, r8=num_blocks
      var num = 0;
      try { num = this.context.r8.toInt32(); } catch (e) { num = 0; }
      if (num !== 1) return;                 // 批量哈希不可能是 HMAC ipad
      var p = this.context.rdx;
      if (p.isNull()) return;
      var raw;
      try { raw = new Uint8Array(p.readByteArray(128)); } catch (e) { return; }
      if (!looksLikePad(raw, 0x36)) return;
      var k = hexOf(raw, 0x36);
      if (hits[k]) return;
      hits[k] = 1;
      if (sent < 6) { sent += 1; send({ type: "key", hex: k, source: "ipad" }); }
    }
  });
  installed = true;
  send({ type: "hooked" });
}

install();
"""


@dataclass
class HookResult:
    ok: bool = False
    key: str = ""
    status: str = ""
    message: str = ""
    notes: list[str] = field(default_factory=list)
    rva: int = 0


# PE / DLL 定位
def _sections(path: Path):
    """返回 [(name, va, vsize, rawptr, rawsize)]，用 mmap 避免把 200MB 读进内存。"""
    with open(path, "rb") as f:
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            if mm[:2] != b"MZ":
                raise ValueError("不是 PE 文件")
            pe = struct.unpack_from("<I", mm, 0x3C)[0]
            if mm[pe:pe + 4] != b"PE\0\0":
                raise ValueError("PE 签名缺失")
            nsec = struct.unpack_from("<H", mm, pe + 6)[0]
            optsz = struct.unpack_from("<H", mm, pe + 20)[0]
            off = pe + 24 + optsz
            out = []
            for i in range(nsec):
                s = mm[off + i * 40: off + (i + 1) * 40]
                name = s[:8].rstrip(b"\0").decode(errors="replace")
                vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", s, 8)
                out.append((name, va, vsize, rawptr, rawsize))
            return out


def _file_off_to_rva(path: Path, offset: int) -> int:
    for _name, va, _vsize, rawptr, rawsize in _sections(path):
        if rawptr <= offset < rawptr + rawsize:
            return va + (offset - rawptr)
    raise ValueError(f"偏移 {offset:#x} 不在任何节内")


def find_sha512_block_rva(dll_path: Path) -> int:
    """从 Weixin.dll 现找 sha512_block_data_order 的 RVA（带磁盘缓存）。"""
    stat = dll_path.stat()
    cache_key = f"{dll_path}|{stat.st_size}|{int(stat.st_mtime)}"
    try:
        saved = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        if saved.get("key") == cache_key and saved.get("rva"):
            return int(saved["rva"])
    except (OSError, ValueError):
        saved = {}

    with open(dll_path, "rb") as f:
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            pos = mm.find(_OPENSSL_MARK)
            if pos < 0:
                raise ValueError("Weixin.dll 里找不到 OpenSSL SHA512 标记，可能是未知版本")
            entry = mm.find(_ENTRY_SIG, pos, min(len(mm), pos + 512))
            if entry < 0:
                # 版权串后可能还有对齐/其他字符串，放宽到 4KB 再找一次
                entry = mm.find(_ENTRY_SIG, pos, min(len(mm), pos + 4096))
            if entry < 0:
                raise ValueError("找到 SHA512 标记但定位不到函数入口（版本可能改了编译器）")
            rva = _file_off_to_rva(dll_path, entry)

    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(
            json.dumps({"key": cache_key, "rva": rva}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass
    return rva


def _version_key(path: Path) -> tuple:
    parts = []
    for p in path.parent.parts[::-1]:
        if p and p[0].isdigit():
            parts = [int(x) if x.isdigit() else 0 for x in p.split(".")]
            break
    return tuple(parts)


def find_weixin_dll() -> Path | None:
    """按「正在运行的进程 → 注册表安装目录 → 常见目录」找 Weixin.dll。"""
    candidates: list[Path] = []
    try:
        import winreg  # noqa: PLC0415

        for hive, key in (
            (winreg.HKEY_CURRENT_USER, r"Software\Tencent\Weixin"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Tencent\Weixin"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Tencent\Weixin"),
        ):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for value in ("InstallPath", "InstallDir", "Path"):
                        try:
                            p = Path(winreg.QueryValueEx(k, value)[0])
                            candidates.append(p / "Weixin.dll")
                        except OSError:
                            pass
            except OSError:
                pass
    except ImportError:
        pass

    for root in (r"C:\Program Files\Tencent\Weixin", r"C:\Program Files (x86)\Tencent\Weixin"):
        base = Path(root)
        if base.is_dir():
            candidates.extend(base.glob("*/Weixin.dll"))
            candidates.append(base / "Weixin.dll")

    existing = [p for p in candidates if p.is_file()]
    if not existing:
        return None
    existing.sort(key=_version_key, reverse=True)
    return existing[0]


# 等待重启 + 挂载
def _main_pid(min_mb: int = 20) -> int | None:
    procs = wxkey._wechat_pids()
    procs = [(p, m) for p, m in procs if m >= min_mb * 1024]
    if not procs:
        return None
    return max(procs, key=lambda x: x[1])[0]


def _frida():
    try:
        import frida  # noqa: PLC0415
    except ImportError:
        return None
    return frida


def _install_frida_from_wheels() -> object | None:
    """首次用启动抓取时，从 vendor/wheels 离线装 frida（不联网、不改系统环境）。"""
    wheels = _ROOT / "vendor" / "wheels"
    if not any(wheels.glob("frida-*win_amd64.whl")):
        return None
    cmd = [
        sys.executable, "-m", "pip", "install", "--no-index",
        "--find-links", str(wheels), f"frida=={_FRIDA_VER}",
        "--disable-pip-version-check", "-q",
    ]
    try:
        subprocess.run(cmd, timeout=240, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return _frida()


def status() -> dict:
    dll = find_weixin_dll()
    frida = _frida()
    info = {
        "frida": bool(frida),
        "frida_version": getattr(frida, "__version__", "") if frida else "",
        "weixin_dll": str(dll) if dll else "",
        "wechat_pids": [p for p, _ in wxkey._wechat_pids()],
        "rva": 0,
        "wheel": any((_ROOT / "vendor" / "wheels").glob("frida-*win_amd64.whl")),
        "error": "",
    }
    if dll:
        try:
            info["rva"] = find_sha512_block_rva(dll)
        except Exception as exc:  # noqa: BLE001
            info["error"] = str(exc)
    return info


def _pids_now() -> dict[int, int]:
    return {p: m for p, m in wxkey._wechat_pids() if m >= 20 * 1024}


def _pid_alive(pid: int) -> bool:
    return pid in {p for p, _ in wxkey._wechat_pids()}


def _wait_for_new_pid(baseline: set[int], deadline: float, emit) -> int | None:
    started = time.time()
    while time.time() < deadline:
        procs = _pids_now()
        fresh = [(p, m) for p, m in procs.items() if p not in baseline]
        if fresh:
            return max(fresh, key=lambda x: x[1])[0]
        if not baseline and procs:
            return max(procs.items(), key=lambda x: x[1])[0]
        emit(
            "等待微信重启" if baseline else "等待微信启动",
            hint="请完全退出微信后重新打开" if baseline else "请从桌面打开微信",
            elapsed=int(time.time() - started),
        )
        time.sleep(0.4)
    return None


def _hook_one(pid: int, rva: int, db_path: Path, deadline: float, emit, res: HookResult) -> str | None:
    """挂一个进程，等它启动时派生的密钥。返回已校验的 key hex 或 None。"""
    frida = _frida()
    if frida is None:
        return None
    t0 = time.time()

    session = None
    try:
        last_exc = None
        for _ in range(12):
            try:
                session = frida.attach(pid)
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                time.sleep(0.25)
        if session is None:
            res.notes.append(f"PID {pid} 挂载失败: {last_exc}")
            return None

        found: list[str] = []
        state = {"hooked": False, "error": ""}

        def on_message(message, data):
            if message.get("type") == "error":
                state["error"] = str(message.get("stack") or message.get("description"))
                return
            payload = message.get("payload") or {}
            kind = payload.get("type")
            if kind == "hooked":
                state["hooked"] = True
                emit("已挂载，等待启动时 PBKDF2", pid=pid)
            elif kind == "module":
                res.notes.append(
                    f"模块基址 {payload.get('base')} → 入口 {payload.get('entry')}"
                )
            elif kind == "key":
                found.append(payload.get("hex") or "")

        try:
            script = session.create_script(JS_TEMPLATE.replace("%RVA%", str(rva)))
            script.on("message", on_message)
            script.load()
        except Exception as exc:  # noqa: BLE001
            res.notes.append(f"PID {pid} 挂载脚本失败: {exc}")
            return None

        # DLL 可能还没加载完，JS 会自己轮询；这里等它确认挂上
        wait_until = min(deadline, time.time() + 25)
        while time.time() < wait_until and not state["hooked"] and not state["error"]:
            if found:
                break
            if not _pid_alive(pid):
                res.notes.append(f"PID {pid} 在挂载过程中退出")
                return None
            time.sleep(0.2)
        if state["error"]:
            res.notes.append(f"挂载脚本出错: {state['error']}")
            return None

        while time.time() < deadline:
            if found:
                cand = found.pop(0)
                if len(cand) == 64:
                    emit("已捕获候选密钥，正在校验", pid=pid)
                    try:
                        ok, why = wxdecrypt.key_matches_db(bytes.fromhex(cand), db_path)
                    except Exception as exc:  # noqa: BLE001
                        ok, why = False, str(exc)
                    res.notes.append(f"候选 {cand[:8]}… 校验: {'通过' if ok else why}")
                    if ok:
                        return cand
            if not _pid_alive(pid):
                res.notes.append(f"PID {pid} 已退出（可能是非桌面会话拉起的空壳，不读数据库）")
                return None
            emit("已挂载，等待启动时 PBKDF2", pid=pid,
                 elapsed=int(time.time() - t0))
            time.sleep(0.2)
        return None
    finally:
        try:
            if session is not None:
                session.detach()
        except Exception:  # noqa: BLE001
            pass


def capture(db_path: Path, timeout: float = 180.0, progress=None) -> HookResult:
    """等待用户重启微信，在启动瞬间挂载并抓取 raw key（只读）。"""
    emit = progress or (lambda *_a, **_k: None)
    res = HookResult()

    frida = _frida()
    if frida is None:
        emit("首次使用，正在离线安装 hook 组件")
        frida = _install_frida_from_wheels()
    if frida is None:
        res.status = "NO_FRIDA"
        res.message = (
            "缺少 frida 组件，无法在启动瞬间挂载。请把 frida 的 wheel 放进 vendor/wheels，"
            "或执行: python -m pip install frida==16.7.19"
        )
        return res

    try:
        head = db_path.read_bytes()[:wxdecrypt.PAGE]
    except OSError as exc:
        res.status = wxkey.NO_WECHAT
        res.message = f"读不到微信数据库: {exc}"
        return res
    if len(head) < wxdecrypt.PAGE:
        res.message = "数据库文件不完整"
        return res

    dll = find_weixin_dll()
    if not dll:
        res.status = "NO_DLL"
        res.message = "找不到 Weixin.dll，请确认电脑上装了微信 4.x"
        return res

    emit("定位微信主模块", dll=str(dll))
    try:
        rva = find_sha512_block_rva(dll)
    except Exception as exc:  # noqa: BLE001
        res.status = "NO_SYMBOL"
        res.message = f"定位 SHA512 入口失败: {exc}"
        return res
    res.rva = rva
    res.notes.append(f"sha512_block RVA = {rva:#x}")
    emit("定位微信主模块", dll=str(dll), rva=hex(rva))

    deadline = time.time() + timeout
    baseline = set(_pids_now())
    if baseline:
        emit("等待微信重启", hint="请完全退出微信后重新打开", elapsed=0)
    else:
        emit("等待微信启动", hint="请从桌面打开微信并登录", elapsed=0)

    early_exits = 0
    while time.time() < deadline:
        pid = _wait_for_new_pid(baseline, deadline, emit)
        if pid is None:
            break
        emit("已发现微信进程，正在挂载", pid=pid)
        key = _hook_one(pid, rva, db_path, deadline, emit, res)
        if key:
            res.ok = True
            res.key = key
            res.status = wxkey.OK
            res.message = "已在微信启动瞬间捕获数据库密钥并通过 page1 校验"
            return res
        baseline.add(pid)   # 这个进程已试过，等下一个新进程
        if not _pid_alive(pid):
            early_exits += 1
            if early_exits >= 2:
                res.status = "HOOK_BLOCKED"
                res.message = (
                    "连续两个微信进程在挂载后很快退出。两种可能：\n"
                    "1) 微信不是从桌面手动启动的（空壳，本就不会读库）；\n"
                    "2) 当前微信版本对进程挂载有安全校验。\n"
                    "请先从桌面双击启动并登录微信再试；若仍退出，请改用粘贴密钥。"
                )
                return res

    if not res.notes:
        res.status = "NO_RESTART"
        res.message = (
            f"等待 {int(timeout)} 秒没有检测到微信重启。请完全退出微信（托盘也要退出）"
            "后重新打开；如果只关了窗口、托盘还在，密钥不会重新派生。"
        )
    else:
        res.status = "NO_KEY"
        res.message = (
            "已挂载微信，但没有捕获到启动密钥。常见原因：\n"
            "· 微信不是从桌面手动启动的（脚本/后台拉起的空壳不读数据库）\n"
            "· 挂载晚于 PBKDF2（电脑很快）——再试一次即可\n"
            "· 登录的是另一个微信号（确认窗口标题是本人昵称）"
        )
    return res


# CLI
def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "status"
    env = common.load_env()
    storage, _roots = wxkey.pick_storage((env.get("WX_DATA_PATH") or "").strip())

    if cmd == "status":
        info = status()
        print("frida      :", "OK " + info["frida_version"] if info["frida"] else "未安装")
        print("Weixin.dll :", info["weixin_dll"] or "未找到")
        print("SHA512 RVA :", hex(info["rva"]) if info["rva"] else (info["error"] or "未定位"))
        print("微信进程   :", info["wechat_pids"] or "未运行")
        return 0 if info["frida"] and info["rva"] else 1

    if cmd == "capture":
        timeout = 180.0
        for i, a in enumerate(argv):
            if a == "--timeout" and i + 1 < len(argv):
                timeout = float(argv[i + 1])
        if storage is None:
            print("找不到微信数据目录，请先登录电脑版微信 4.x")
            return 2
        db = wxkey.message_db(storage)
        if not db.exists():
            print(f"{storage} 下没有 message/message_0.db")
            return 2

        def show(stage, **kw):
            extra = " ".join(f"{k}={v}" for k, v in kw.items() if v not in ("", None))
            print(f"  [..] {stage} {extra}".rstrip(), flush=True)

        res = capture(db, timeout=timeout, progress=show)
        print()
        print(("OK  " if res.ok else "FAIL ") + res.message)
        for n in res.notes:
            print("  ·", n)
        if res.ok:
            written = wxkey.store_key(res.key, env=env)
            print("  已写入:", ", ".join(written))
        return 0 if res.ok else 1

    print(__doc__)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
