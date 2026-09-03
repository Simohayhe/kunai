"""起動中の Riot Client が持つローカル API。

Riot Client は起動すると 127.0.0.1 にランダムポートで HTTPS を立て、
接続情報を lockfile に書く。Basic 認証のパスワードもそこに入っている。
ここから今ログインしているアカウントの puuid と、
リモート API を叩くのに要るアクセストークンが取れる。
"""
from __future__ import annotations

import base64
from dataclasses import dataclass

import requests
import urllib3

from .. import paths

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class LocalApiError(Exception):
    pass


@dataclass(frozen=True)
class Lockfile:
    name: str
    pid: int
    port: int
    password: str
    protocol: str

    @property
    def base_url(self) -> str:
        return f"{self.protocol}://127.0.0.1:{self.port}"

    @property
    def auth_header(self) -> str:
        token = base64.b64encode(f"riot:{self.password}".encode()).decode()
        return f"Basic {token}"


def read_lockfile() -> Lockfile | None:
    path = paths.lockfile_path()
    if not path:
        return None
    try:
        parts = path.read_text(encoding="utf-8").strip().split(":")
        if len(parts) < 5:
            return None
        return Lockfile(parts[0], int(parts[1]), int(parts[2]), parts[3], parts[4])
    except (OSError, ValueError):
        return None


@dataclass
class LocalSession:
    """今ログイン中のアカウントと、そのアクセストークン。"""
    puuid: str
    access_token: str
    entitlements_token: str
    game_name: str = ""
    tag_line: str = ""
    region: str = ""

    @property
    def riot_id(self) -> str:
        return f"{self.game_name}#{self.tag_line}" if self.game_name else ""


class LocalClient:
    def __init__(self, lockfile: Lockfile | None = None, timeout: float = 5.0):
        self.lockfile = lockfile or read_lockfile()
        self.timeout = timeout
        self._session = requests.Session()
        self._session.verify = False   # Riot のローカル証明書は自己署名

    @property
    def available(self) -> bool:
        return self.lockfile is not None

    def get(self, path: str) -> dict:
        if not self.lockfile:
            raise LocalApiError("Riot Client が起動していません")
        resp = self._session.get(
            self.lockfile.base_url + path,
            headers={"Authorization": self.lockfile.auth_header},
            timeout=self.timeout,
        )
        if resp.status_code == 401:
            raise LocalApiError("ローカル API の認証に失敗しました。lockfile が古い可能性があります")
        if resp.status_code >= 400:
            raise LocalApiError(f"{path} が {resp.status_code} を返しました")
        return resp.json()

    def entitlements(self) -> dict:
        return self.get("/entitlements/v1/token")

    def chat_session(self) -> dict:
        return self.get("/chat/v1/session")

    def region(self) -> str:
        try:
            return (self.get("/riotclient/region-locale") or {}).get("region", "").lower()
        except LocalApiError:
            return ""

    def session(self) -> LocalSession:
        """リモート API を叩くのに必要な一式をまとめて取る。"""
        ent = self.entitlements()
        s = LocalSession(
            puuid=ent.get("subject", ""),
            access_token=ent.get("accessToken", ""),
            entitlements_token=ent.get("token", ""),
        )
        try:
            chat = self.chat_session()
            s.game_name = chat.get("game_name", "")
            s.tag_line = chat.get("game_tag", "")
            s.region = (chat.get("region") or "").lower()
        except LocalApiError:
            pass
        if not s.region:
            s.region = self.region()
        return s
