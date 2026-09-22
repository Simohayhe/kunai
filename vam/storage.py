"""アカウント保管庫。JSON を丸ごと AES-GCM で暗号化して 1 ファイルに置く。"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from . import crypto
from .models import Account


def default_app_dir() -> Path:
    base = os.environ.get("VAM_APP_DIR")
    if base:
        return Path(base)
    root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    new_dir = root / "Kunai"
    old_dir = root / "ValorantAccountManager"
    if not new_dir.exists() and old_dir.is_dir():
        # 「VALORANT Account Manager」からの改名に伴う一度きりの移行。
        # コピーではなく rename にして、保管庫を失わないようにする。
        try:
            old_dir.rename(new_dir)
        except OSError:
            return old_dir
    return new_dir


class Vault:
    def __init__(self, app_dir: Path | None = None):
        self.app_dir = Path(app_dir) if app_dir else default_app_dir()
        self.accounts_file = self.app_dir / "accounts.dat"
        self.sessions_dir = self.app_dir / "sessions"
        self.settings_file = self.app_dir / "settings.json"
        self.keystore = crypto.KeyStore(self.app_dir / "key.json")
        self._key: bytes | None = None
        self._accounts: dict[str, Account] = {}
        self._order: list[str] = []

    # -- 開閉 ---------------------------------------------------------------
    @property
    def initialized(self) -> bool:
        return self.keystore.exists()

    @property
    def needs_password(self) -> bool:
        return self.keystore.needs_password

    @property
    def is_open(self) -> bool:
        return self._key is not None

    def initialize(self, password: str | None = None) -> None:
        self.app_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._key = self.keystore.create(password)
        self._accounts, self._order = {}, []
        self.save()

    def open(self, password: str | None = None) -> None:
        self._key = self.keystore.load(password)
        self.load()

    def close(self) -> None:
        self._key = None
        self._accounts, self._order = {}, []

    def change_password(self, new_password: str | None) -> None:
        key = self._require_open()
        self.keystore.change_password(key, new_password)

    def _require_open(self) -> bytes:
        if self._key is None:
            raise crypto.VaultLocked("保管庫が開かれていません")
        return self._key

    # -- 読み書き -----------------------------------------------------------
    def load(self) -> None:
        key = self._require_open()
        if not self.accounts_file.is_file():
            self._accounts, self._order = {}, []
            return
        raw = crypto.decrypt(key, self.accounts_file.read_bytes()).decode("utf-8")
        data = json.loads(raw)
        self._accounts = {a["id"]: Account.from_dict(a) for a in data.get("accounts", [])}
        self._order = [i for i in data.get("order", []) if i in self._accounts]
        self._order += [i for i in self._accounts if i not in self._order]

    def save(self) -> None:
        key = self._require_open()
        payload = {
            "version": 1,
            "order": self._order,
            "accounts": [a.to_dict() for a in self._accounts.values()],
        }
        blob = crypto.encrypt(key, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        self.accounts_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.accounts_file.with_suffix(".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, self.accounts_file)

    # -- アカウント操作 -----------------------------------------------------
    def accounts(self) -> list[Account]:
        return [self._accounts[i] for i in self._order]

    def get(self, account_id: str) -> Account | None:
        return self._accounts.get(account_id)

    def add(self, account: Account) -> Account:
        self._require_open()
        self._accounts[account.id] = account
        if account.id not in self._order:
            self._order.append(account.id)
        self.save()
        return account

    def update(self, account: Account) -> None:
        self._require_open()
        self._accounts[account.id] = account
        self.save()

    def remove(self, account_id: str) -> None:
        self._require_open()
        self._accounts.pop(account_id, None)
        if account_id in self._order:
            self._order.remove(account_id)
        shutil.rmtree(self.session_dir_for(account_id), ignore_errors=True)
        self.save()

    def reorder(self, ordered_ids: list[str]) -> None:
        self._require_open()
        self._order = [i for i in ordered_ids if i in self._accounts]
        self._order += [i for i in self._accounts if i not in self._order]
        self.save()

    # -- セッション保管 -----------------------------------------------------
    def session_dir_for(self, account_id: str) -> Path:
        return self.sessions_dir / account_id

    def write_session_blob(self, account_id: str, rel_name: str, data: bytes) -> None:
        key = self._require_open()
        d = self.session_dir_for(account_id)
        d.mkdir(parents=True, exist_ok=True)
        name = rel_name.replace("/", "@@") + ".enc"
        blob = crypto.encrypt(key, data, aad=rel_name.encode("utf-8"))
        (d / name).write_bytes(blob)

    def read_session_blobs(self, account_id: str) -> dict[str, bytes]:
        key = self._require_open()
        d = self.session_dir_for(account_id)
        if not d.is_dir():
            return {}
        out: dict[str, bytes] = {}
        for f in sorted(d.glob("*.enc")):
            rel = f.name[:-4].replace("@@", "/")
            out[rel] = crypto.decrypt(key, f.read_bytes(), aad=rel.encode("utf-8"))
        return out

    def clear_session(self, account_id: str) -> None:
        shutil.rmtree(self.session_dir_for(account_id), ignore_errors=True)

    # -- 設定。非機密なので平文 JSON --------------------------------------
    def settings(self) -> dict:
        if self.settings_file.is_file():
            try:
                return json.loads(self.settings_file.read_text(encoding="utf-8"))
            except ValueError:
                pass
        return {}

    def save_settings(self, data: dict) -> None:
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
