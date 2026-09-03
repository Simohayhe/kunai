"""保管庫の暗号化。

鍵は 32byte のランダム値で、これを Windows DPAPI (CurrentUser スコープ) で包んで
ディスクに置く。つまり「このユーザーでログインしている状態でのみ復号できる」。
任意でマスターパスワードを掛けると、DPAPI に加えて PBKDF2 由来の鍵でも包む
（パスワードを知らない限り、同じ Windows ユーザーでも開けない）。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

PBKDF2_ITERATIONS = 600_000


class VaultLocked(Exception):
    """マスターパスワードが必要 / 間違っている。"""


# --------------------------------------------------------------------------
# DPAPI (CryptProtectData / CryptUnprotectData) を ctypes で直接叩く
# --------------------------------------------------------------------------
class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _DataBlob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _blob_bytes(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


_crypt32 = ctypes.windll.crypt32
_kernel32 = ctypes.windll.kernel32
CRYPTPROTECT_UI_FORBIDDEN = 0x01


def dpapi_protect(data: bytes, entropy: bytes = b"vam-v1") -> bytes:
    out = _DataBlob()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(_blob(data)), "vam", ctypes.byref(_blob(entropy)),
        None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
    )
    if not ok:
        raise OSError(ctypes.GetLastError(), "CryptProtectData failed")
    try:
        return _blob_bytes(out)
    finally:
        _kernel32.LocalFree(out.pbData)


def dpapi_unprotect(data: bytes, entropy: bytes = b"vam-v1") -> bytes:
    out = _DataBlob()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(_blob(data)), None, ctypes.byref(_blob(entropy)),
        None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
    )
    if not ok:
        raise OSError(ctypes.GetLastError(), "CryptUnprotectData failed")
    try:
        return _blob_bytes(out)
    finally:
        _kernel32.LocalFree(out.pbData)


def _derive(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=PBKDF2_ITERATIONS)
    return kdf.derive(password.encode("utf-8"))


class KeyStore:
    """データ鍵の生成・保存・読み出しを受け持つ。"""

    def __init__(self, key_file: Path):
        self.key_file = key_file

    def exists(self) -> bool:
        return self.key_file.is_file()

    @property
    def needs_password(self) -> bool:
        if not self.exists():
            return False
        meta = json.loads(self.key_file.read_text(encoding="utf-8"))
        return bool(meta.get("password_protected"))

    def create(self, password: str | None = None) -> bytes:
        data_key = secrets.token_bytes(32)
        self._write(data_key, password)
        return data_key

    def _write(self, data_key: bytes, password: str | None) -> None:
        meta: dict[str, object] = {"version": 1, "password_protected": bool(password)}
        payload = data_key
        if password:
            salt = secrets.token_bytes(16)
            nonce = secrets.token_bytes(12)
            payload = AESGCM(_derive(password, salt)).encrypt(nonce, payload, None)
            meta["salt"] = salt.hex()
            meta["nonce"] = nonce.hex()
        meta["blob"] = dpapi_protect(payload).hex()
        self.key_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.key_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta), encoding="utf-8")
        os.replace(tmp, self.key_file)

    def load(self, password: str | None = None) -> bytes:
        meta = json.loads(self.key_file.read_text(encoding="utf-8"))
        payload = dpapi_unprotect(bytes.fromhex(meta["blob"]))
        if meta.get("password_protected"):
            if not password:
                raise VaultLocked("マスターパスワードが必要です")
            key = _derive(password, bytes.fromhex(meta["salt"]))
            try:
                payload = AESGCM(key).decrypt(bytes.fromhex(meta["nonce"]), payload, None)
            except Exception as exc:
                raise VaultLocked("マスターパスワードが違います") from exc
        return payload

    def change_password(self, data_key: bytes, new_password: str | None) -> None:
        self._write(data_key, new_password)


def encrypt(data_key: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(data_key).encrypt(nonce, plaintext, aad or None)


def decrypt(data_key: bytes, blob: bytes, aad: bytes = b"") -> bytes:
    return AESGCM(data_key).decrypt(blob[:12], blob[12:], aad or None)
