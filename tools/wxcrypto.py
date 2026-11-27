"""AES-256-CBC 加解密后端：三档自动降级，保证「零 pip 依赖也能跑」。

为什么要自己写一层：
    1. 用户包（2-包）通常没有 .venv，解释器是 外部协作引擎 自带的 Python，
       装不了 pycryptodome；而 Windows 系统自带 CNG（bcrypt.dll）一定能用。
    2. vendor/scripts/common/crypto_backend.py 里 `os` 没有 import，
       走到 bcrypt 兜底那一支会 NameError；且它把解密函数当加密函数返回。
       本模块把这两处一并修好，并给出 backend 名字供排障打印。

优先级：pycryptodome → cryptography → Windows CNG(bcrypt.dll) → 报错。
"""
from __future__ import annotations

import ctypes
import os
from functools import lru_cache

BACKEND = ""


def _try_pycryptodome():
    try:
        from Crypto.Cipher import AES  # noqa: PLC0415
    except ImportError:
        return None

    def dec(key: bytes, iv: bytes, data: bytes) -> bytes:
        return AES.new(key, AES.MODE_CBC, iv).decrypt(data)

    def enc(key: bytes, iv: bytes, data: bytes) -> bytes:
        return AES.new(key, AES.MODE_CBC, iv).encrypt(data)

    return dec, enc


def _try_cryptography():
    try:
        from cryptography.hazmat.primitives.ciphers import (  # noqa: PLC0415
            Cipher,
            algorithms,
            modes,
        )
    except ImportError:
        return None

    def dec(key: bytes, iv: bytes, data: bytes) -> bytes:
        ctx = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        return ctx.update(data) + ctx.finalize()

    def enc(key: bytes, iv: bytes, data: bytes) -> bytes:
        ctx = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return ctx.update(data) + ctx.finalize()

    return dec, enc


def _try_windows_cng():
    """Windows 系统自带 CNG。每次调用新建 alg/key 句柄，避免跨线程共享。"""
    if os.name != "nt":
        return None
    try:
        import ctypes.wintypes as wt  # noqa: PLC0415

        bcrypt = ctypes.windll.bcrypt  # type: ignore[attr-defined]
        bcrypt.BCryptOpenAlgorithmProvider.restype = ctypes.c_long
        bcrypt.BCryptGenerateSymmetricKey.restype = ctypes.c_long
        bcrypt.BCryptDecrypt.restype = ctypes.c_long
        bcrypt.BCryptEncrypt.restype = ctypes.c_long
    except Exception:  # noqa: BLE001
        return None

    def _run(key: bytes, iv: bytes, data: bytes, decrypt: bool) -> bytes:
        h_alg = ctypes.c_void_p()
        if bcrypt.BCryptOpenAlgorithmProvider(
            ctypes.byref(h_alg), ctypes.c_wchar_p("AES"), None, 0
        ) != 0:
            raise RuntimeError("BCryptOpenAlgorithmProvider 失败")
        h_key = ctypes.c_void_p()
        try:
            mode = ctypes.c_wchar_p("ChainingModeCBC")
            bcrypt.BCryptSetProperty(
                h_alg, ctypes.c_wchar_p("ChainingMode"), mode, ctypes.sizeof(mode), 0
            )
            kbuf = (ctypes.c_ubyte * len(key)).from_buffer_copy(key)
            if bcrypt.BCryptGenerateSymmetricKey(
                h_alg, ctypes.byref(h_key), None, 0, kbuf, len(key), 0
            ) != 0:
                raise RuntimeError("BCryptGenerateSymmetricKey 失败")
            iv_buf = (ctypes.c_ubyte * len(iv)).from_buffer_copy(iv)
            in_buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            out_buf = (ctypes.c_ubyte * len(data))()
            done = wt.DWORD(0)
            fn = bcrypt.BCryptDecrypt if decrypt else bcrypt.BCryptEncrypt
            status = fn(
                h_key, in_buf, len(data), None, iv_buf, len(iv),
                out_buf, len(data), ctypes.byref(done), 0,
            )
            if status != 0:
                raise RuntimeError(f"BCrypt{'Decrypt' if decrypt else 'Encrypt'} 失败: {status}")
            return bytes(out_buf[: done.value])
        finally:
            if h_key:
                bcrypt.BCryptDestroyKey(h_key)
            bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)

    def dec(key: bytes, iv: bytes, data: bytes) -> bytes:
        return _run(key, iv, data, True)

    def enc(key: bytes, iv: bytes, data: bytes) -> bytes:
        return _run(key, iv, data, False)

    return dec, enc


@lru_cache(maxsize=1)
def _backend():
    global BACKEND
    for name, factory in (
        ("pycryptodome", _try_pycryptodome),
        ("cryptography", _try_cryptography),
        ("windows-cng", _try_windows_cng),
    ):
        pair = factory()
        if pair is not None:
            BACKEND = name
            return pair
    raise RuntimeError(
        "找不到可用的 AES 后端（已尝试 pycryptodome / cryptography / Windows CNG）。"
        "请安装 pycryptodome，或在 Windows 上运行。"
    )


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    fn = _backend()[0]
    return fn(key, iv, data)


def aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    fn = _backend()[1]
    return fn(key, iv, data)


def backend_name() -> str:
    try:
        _backend()
    except RuntimeError:
        return "unavailable"
    return BACKEND


__all__ = ["aes_cbc_decrypt", "aes_cbc_encrypt", "backend_name", "BACKEND"]
