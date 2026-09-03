"""ログイン済みセッションの保存と復元。

Riot Client は「ログインしたまま」の状態を %LOCALAPPDATA%\\Riot Games\\Riot Client
配下の PrivateSettings.yaml に cookie として持っている。
これをアカウントごとに退避しておき、起動前に書き戻すことで、
パスワードを一切扱わずにアカウントを切り替えられる。
"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .. import paths

# cookie の value に入っている JWT
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+")
# "name: ssid" と "value: xxx" が近接して現れる YAML ブロック
_COOKIE_RE = re.compile(
    r"name:\s*(?P<name>[\w-]+)(?P<between>(?:.|\n){0,400}?)value:\s*(?P<value>\S+)"
)

INTERESTING_COOKIES = ("ssid", "clid", "csid", "tdid", "sub")


class SessionError(Exception):
    pass


def decode_jwt_payload(token: str) -> dict:
    """署名検証はしない。中身の sub / exp を読むだけの用途。"""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except (IndexError, ValueError, json.JSONDecodeError):
        return {}


@dataclass
class SessionInfo:
    """セッションファイル群から読み取れた素性。"""
    puuid: str = ""
    expires_at: float = 0.0
    cookies: dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.cookies is None:
            self.cookies = {}

    @property
    def valid(self) -> bool:
        return bool(self.cookies.get("ssid"))

    @property
    def expired(self) -> bool:
        return bool(self.expires_at) and self.expires_at < time.time()

    @property
    def expires_in_days(self) -> float:
        if not self.expires_at:
            return 0.0
        return max(0.0, (self.expires_at - time.time()) / 86400)


def inspect_blobs(blobs: dict[str, bytes]) -> SessionInfo:
    """保存済みセッション (ファイル名 -> 中身) から puuid と有効期限を読む。"""
    cookies: dict[str, str] = {}
    for data in blobs.values():
        text = data.decode("utf-8", errors="replace")
        for m in _COOKIE_RE.finditer(text):
            name = m.group("name")
            if name in INTERESTING_COOKIES and name not in cookies:
                cookies[name] = m.group("value").strip("'\"")

    info = SessionInfo(cookies=cookies)

    # ssid が取れていればそれを、駄目なら全文から JWT を拾って sub を持つものを使う
    candidates = [cookies["ssid"]] if cookies.get("ssid") else []
    if not candidates:
        for data in blobs.values():
            candidates += _JWT_RE.findall(data.decode("utf-8", errors="replace"))
    for token in candidates:
        payload = decode_jwt_payload(token)
        if payload.get("sub"):
            info.puuid = payload["sub"]
            info.expires_at = float(payload.get("exp", 0) or 0)
            if not cookies.get("ssid"):
                cookies["ssid"] = token
            break
    return info


def update_cookies(blobs: dict[str, bytes],
                   cookies: dict[str, str]) -> dict[str, bytes]:
    """セッションファイル内の cookie 値を差し替えた blob を返す。

    再認証のたびに Riot は cookie をローテーションして返してくる。
    それを書き戻さないと、最初に取り込んだ cookie の期限が来た時点で切れる。
    書き戻せば、使うたびに有効期限が延びていく。
    """
    def replace(match: "re.Match[str]") -> str:
        name = match.group("name")
        new = cookies.get(name)
        if not new:
            return match.group(0)
        return f"name: {name}{match.group('between')}value: {new}"

    return {
        rel: _COOKIE_RE.sub(replace, data.decode("utf-8", errors="replace")).encode("utf-8")
        for rel, data in blobs.items()
    }


def capture() -> dict[str, bytes]:
    """今 Riot Client に入っているセッションを読み出す。"""
    root = paths.riot_client_data_root()
    blobs: dict[str, bytes] = {}
    for rel in paths.SESSION_FILE_CANDIDATES:
        f = root / rel
        if f.is_file():
            blobs[rel] = f.read_bytes()
    if not blobs:
        raise SessionError(
            "セッションファイルが見つかりません。"
            "一度 Riot Client でログインしてから保存してください。"
        )
    return blobs


def current_session_info() -> SessionInfo:
    try:
        return inspect_blobs(capture())
    except SessionError:
        return SessionInfo()


def restore(blobs: dict[str, bytes], backup_dir: Path | None = None) -> list[str]:
    """保存しておいたセッションを Riot Client の置き場に書き戻す。

    Riot Client が起動中だと終了時に上書きされるので、呼ぶ前に必ず落としておくこと。
    書き戻したファイルの相対パス一覧を返す。
    """
    if not blobs:
        raise SessionError("保存されたセッションがありません")

    root = paths.riot_client_data_root()
    written: list[str] = []
    for rel, data in blobs.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if backup_dir and target.is_file():
            b = backup_dir / rel.replace("/", "@@")
            b.parent.mkdir(parents=True, exist_ok=True)
            b.write_bytes(target.read_bytes())
        target.write_bytes(data)
        written.append(rel)
    return written


def clear_current() -> list[str]:
    """今のログイン状態を消す。ログアウト相当。"""
    root = paths.riot_client_data_root()
    removed = []
    for rel in paths.SESSION_FILE_CANDIDATES:
        f = root / rel
        if f.is_file():
            f.unlink()
            removed.append(rel)
    return removed
