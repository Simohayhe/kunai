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

# "name: ssid" と "value: xxx" が近接して現れる YAML ブロック。
# 実際の Riot Client は name も value も引用符で囲むので、引用符は任意にする。
# 書き戻し (update_cookies) はこの正規表現で行う。ファイルを丸ごと
# 書き直すとフォーマットが変わって Riot 側が読めなくなりかねないため、
# 値の部分だけを差し替える。
_COOKIE_RE = re.compile(
    r"""name:\s*(?P<nq>["']?)(?P<name>[\w-]+)(?P=nq)"""
    r"""(?P<between>(?:.|\n){0,400}?)"""
    r"""value:\s*(?P<vq>["']?)(?P<value>[^"'\s]+)(?P=vq)"""
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


def _walk_cookies(node, found: list[dict]) -> None:
    """入れ子のどこにあっても、name と value を持つ辞書を cookie とみなす。

    Riot Client の実ファイルは
        rso-authenticator:
            tdid:
                name: "tdid"
                value: "..."
                expiryTime: 1820012619
    のような名前付きマッピングだが、リスト形式で持つ版もある。
    構造を決め打ちせずに拾う。
    """
    if isinstance(node, dict):
        name, value = node.get("name"), node.get("value")
        if isinstance(name, str) and isinstance(value, str) and value:
            found.append({
                "name": name,
                "value": value,
                "expiryTime": node.get("expiryTime"),
            })
        for child in node.values():
            _walk_cookies(child, found)
    elif isinstance(node, list):
        for child in node:
            _walk_cookies(child, found)


def _parse_cookies(text: str) -> list[dict]:
    """まず YAML として読む。読めなければ正規表現に落とす。"""
    try:
        import yaml
        data = yaml.safe_load(text)
    except Exception:
        data = None

    if data is not None:
        found: list[dict] = []
        _walk_cookies(data, found)
        if found:
            return found

    # YAML として壊れている場合の保険
    return [
        {"name": m.group("name"), "value": m.group("value"), "expiryTime": None}
        for m in _COOKIE_RE.finditer(text)
    ]


def inspect_blobs(blobs: dict[str, bytes]) -> SessionInfo:
    """保存済みセッション (ファイル名 -> 中身) から puuid と有効期限を読む。"""
    cookies: dict[str, str] = {}
    expiry_times: dict[str, float] = {}
    for data in blobs.values():
        for cookie in _parse_cookies(data.decode("utf-8", errors="replace")):
            name = cookie["name"]
            if name in INTERESTING_COOKIES and name not in cookies:
                cookies[name] = cookie["value"].strip("'\"")
                if cookie.get("expiryTime"):
                    try:
                        expiry_times[name] = float(cookie["expiryTime"])
                    except (TypeError, ValueError):
                        pass

    info = SessionInfo(cookies=cookies)

    # ssid が取れていればそれを使う。
    # 取れなかった場合に全文から JWT を拾うのは、cookie を 1 つも構造的に
    # 読めなかったときだけにする。cookie は読めたが ssid が無い状態は
    # 「ログイン情報を保存していない」であって、そこで別の JWT を ssid に
    # 昇格させると、ログアウト中の端末をログイン済みと誤判定する。
    candidates = [cookies["ssid"]] if cookies.get("ssid") else []
    if not candidates and not cookies:
        for data in blobs.values():
            candidates += _JWT_RE.findall(data.decode("utf-8", errors="replace"))
    for token in candidates:
        payload = decode_jwt_payload(token)
        if payload.get("sub"):
            info.puuid = payload["sub"]
            # cookie 自身が持つ expiryTime を優先する。JWT の exp は
            # トークンの寿命であって、cookie の寿命とは限らない。
            info.expires_at = (expiry_times.get("ssid")
                               or float(payload.get("exp", 0) or 0))
            if not cookies.get("ssid"):
                cookies["ssid"] = token
            break
    return info


_NAME_LINE_RE = re.compile(r'^(?P<indent>\s*)(?:-\s*)?name:\s*["\']?(?P<name>[\w-]+)["\']?\s*$')
_VALUE_LINE_RE = re.compile(r'^(?P<head>\s*(?:-\s*)?value:\s*)(?P<q>["\']?)[^"\'\s]*(?P=q)\s*$')
_EXPIRY_LINE_RE = re.compile(r'^(?P<head>\s*(?:-\s*)?expiryTime:\s*)\S+\s*$')


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def update_cookies(blobs: dict[str, bytes], cookies: dict[str, str],
                   expiries: dict[str, float] | None = None) -> dict[str, bytes]:
    """セッションファイル内の cookie 値を差し替えた blob を返す。

    再認証のたびに Riot は cookie をローテーションして返してくる。
    それを書き戻さないと、最初に取り込んだ cookie の期限が来た時点で切れる。

    値だけを行単位で差し替え、YAML 全体は書き直さない。書き直すと引用符や
    並び順が変わり、Riot Client 側が読めなくなる危険があるため。

    expiryTime も一緒に更新する。実ファイルの cookie は JWT に exp を
    持たないことがあり、その場合 expiryTime だけが寿命の情報源になる。
    """
    expiries = expiries or {}
    out: dict[str, bytes] = {}

    for rel, data in blobs.items():
        lines = data.decode("utf-8", errors="replace").splitlines(keepends=True)

        for i, line in enumerate(lines):
            m = _NAME_LINE_RE.match(line.rstrip("\r\n"))
            if not m:
                continue
            name = m.group("name")
            if name not in cookies and name not in expiries:
                continue

            # この name 行と同じ字下げの範囲を、その cookie のブロックとみなす
            indent = _indent_of(line)
            start = i
            while start > 0 and _indent_of(lines[start - 1]) >= indent \
                    and lines[start - 1].strip():
                start -= 1
            end = i + 1
            while end < len(lines) and _indent_of(lines[end]) >= indent \
                    and lines[end].strip():
                end += 1

            _rewrite_block(lines, start, end,
                           cookies.get(name), expiries.get(name))

        out[rel] = "".join(lines).encode("utf-8")
    return out


def _rewrite_block(lines: list[str], start: int, end: int,
                   value: str | None, expiry: float | None) -> None:
    for j in range(start, end):
        stripped = lines[j].rstrip("\r\n")
        newline = lines[j][len(stripped):]

        if value:
            m = _VALUE_LINE_RE.match(stripped)
            if m:
                q = m.group("q")
                lines[j] = f"{m.group('head')}{q}{value}{q}{newline}"
                continue
        if expiry:
            m = _EXPIRY_LINE_RE.match(stripped)
            if m:
                lines[j] = f"{m.group('head')}{int(expiry)}{newline}"


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
