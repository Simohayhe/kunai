"""保存済み cookie からのアクセストークン再取得 (RSO reauth)。

これがあると「アカウントを切り替えずに、保管庫の全アカウントのランクや所持品を引く」
ことができる。ssid cookie を持った状態で authorize に投げると、
ログイン画面を出さずに access_token が返ってくる仕組み。

パスワードは一切送らない。手元にある cookie を使い回すだけ。
"""
from __future__ import annotations

import base64
import json
import ssl
import time
import urllib.parse
from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager

from .session import decode_jwt_payload

AUTH_BASE = "https://auth.riotgames.com"
ENTITLEMENTS_URL = "https://entitlements.auth.riotgames.com/api/token/v1"
USERINFO_URL = f"{AUTH_BASE}/userinfo"
GEO_URL = "https://riot-geo.pas.si.riotgames.com/pas/v1/product/valorant"

AUTHORIZE_PARAMS = {
    "client_id": "play-valorant-web-prod",
    "nonce": "1",
    "redirect_uri": "https://playvalorant.com/opt_in",
    "response_type": "token id_token",
    "scope": "account openid",
}

# Riot の認証エンドポイントは TLS の ClientHello を見ている。
# requests の既定の並びだと Cloudflare に 403 で弾かれるため、
# ブラウザ相当の cipher 並びを明示する。
_CIPHERS = ":".join([
    "TLS_CHACHA20_POLY1305_SHA256", "TLS_AES_128_GCM_SHA256", "TLS_AES_256_GCM_SHA384",
    "ECDHE-ECDSA-CHACHA20-POLY1305", "ECDHE-RSA-CHACHA20-POLY1305",
    "ECDHE-ECDSA-AES128-GCM-SHA256", "ECDHE-RSA-AES128-GCM-SHA256",
    "ECDHE-ECDSA-AES256-GCM-SHA384", "ECDHE-RSA-AES256-GCM-SHA384",
    "ECDHE-ECDSA-AES128-SHA", "ECDHE-RSA-AES128-SHA",
    "ECDHE-ECDSA-AES256-SHA", "ECDHE-RSA-AES256-SHA",
    "AES128-GCM-SHA256", "AES256-GCM-SHA384", "AES128-SHA", "AES256-SHA",
])

USER_AGENT = "RiotClient/63.0.9.4909983.4789131 rso-auth (Windows; 10;;Professional, x64)"


class AuthError(Exception):
    pass


class SessionExpired(AuthError):
    """cookie が失効している。再ログインが必要。"""


class _RiotTLSAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False, **kw):
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_ciphers(_CIPHERS)
        # Riot 側が対応していない拡張を落とす
        ctx.options |= 1 << 19  # SSL_OP_NO_ENCRYPT_THEN_MAC
        self.poolmanager = PoolManager(
            num_pools=connections, maxsize=maxsize, block=block,
            ssl_context=ctx, **kw,
        )


def client_platform_header() -> str:
    payload = {
        "platformType": "PC",
        "platformOS": "Windows",
        "platformOSVersion": "10.0.19042.1.256.64bit",
        "platformChipset": "Unknown",
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


@dataclass
class AuthResult:
    """再認証の結果。リモート API を叩くのに必要な一式。"""
    access_token: str
    entitlements_token: str = ""
    id_token: str = ""
    puuid: str = ""
    game_name: str = ""
    tag_line: str = ""
    region: str = ""
    expires_at: float = 0.0
    cookies: dict[str, str] = field(default_factory=dict)

    @property
    def riot_id(self) -> str:
        return f"{self.game_name}#{self.tag_line}" if self.game_name else ""

    @property
    def expired(self) -> bool:
        return bool(self.expires_at) and self.expires_at <= time.time()

    def headers(self, client_version: str = "") -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.access_token}",
            "X-Riot-Entitlements-JWT": self.entitlements_token,
            "X-Riot-ClientPlatform": client_platform_header(),
        }
        if client_version:
            h["X-Riot-ClientVersion"] = client_version
        return h


def _collect_cookies(jar) -> dict[str, str]:
    """cookie jar を name -> value に畳む。

    元の cookie とレスポンスの Set-Cookie が domain 違いで併存することがあるので、
    同名なら JWT の exp が最も先のものを採る（＝いちばん新しい方）。
    """
    best: dict[str, tuple[float, str]] = {}
    for c in jar:
        exp = float(decode_jwt_payload(c.value).get("exp", 0) or 0)
        current = best.get(c.name)
        if current is None or exp >= current[0]:
            best[c.name] = (exp, c.value)
    return {name: value for name, (_, value) in best.items()}


def _extract_fragment_tokens(uri: str) -> dict[str, str]:
    fragment = urllib.parse.urlparse(uri).fragment
    return dict(urllib.parse.parse_qsl(fragment))


def new_session() -> requests.Session:
    s = requests.Session()
    s.mount("https://", _RiotTLSAdapter())
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


def reauth_with_cookies(cookies: dict[str, str],
                        session: requests.Session | None = None,
                        timeout: float = 15.0) -> AuthResult:
    """ssid cookie からアクセストークンを取り直す。"""
    if not cookies.get("ssid"):
        raise SessionExpired("ssid cookie がありません")

    s = session or new_session()
    for name, value in cookies.items():
        s.cookies.set(name, value, domain="auth.riotgames.com")

    url = f"{AUTH_BASE}/authorize?" + urllib.parse.urlencode(AUTHORIZE_PARAMS)
    resp = s.get(url, allow_redirects=False, timeout=timeout)

    location = resp.headers.get("Location", "")
    if not location:
        # JSON で返ってくる場合もある
        try:
            body = resp.json()
        except ValueError:
            body = {}
        location = (body.get("response", {}) or {}).get("parameters", {}).get("uri", "")

    tokens = _extract_fragment_tokens(location) if location else {}
    if not tokens.get("access_token"):
        raise SessionExpired(
            "セッションが失効しています。このアカウントで一度ログインし直して、"
            "セッションを保存し直してください。"
        )

    result = AuthResult(
        access_token=tokens["access_token"],
        id_token=tokens.get("id_token", ""),
        expires_at=time.time() + float(tokens.get("expires_in", 3600)),
        cookies=_collect_cookies(s.cookies),
    )
    _fill_details(s, result, timeout)
    return result


def _fill_details(s: requests.Session, result: AuthResult, timeout: float) -> None:
    bearer = {"Authorization": f"Bearer {result.access_token}"}

    ent = s.post(ENTITLEMENTS_URL, json={}, headers=bearer, timeout=timeout)
    if ent.ok:
        result.entitlements_token = ent.json().get("entitlements_token", "")

    info = s.post(USERINFO_URL, json={}, headers=bearer, timeout=timeout)
    if info.ok:
        data = info.json()
        result.puuid = data.get("sub", "")
        acct = data.get("acct") or {}
        result.game_name = acct.get("game_name", "")
        result.tag_line = acct.get("tag_line", "")

    if result.id_token:
        geo = s.put(GEO_URL, json={"id_token": result.id_token},
                    headers=bearer, timeout=timeout)
        if geo.ok:
            affinities = (geo.json().get("affinities") or {})
            result.region = affinities.get("live", "") or affinities.get("pbe", "")
