"""偽の Riot 環境。

VALORANT が入っていない PC でも切替ロジックと API 層を通しで試せるようにする。
本物と同じディレクトリ構造・同じ YAML 形状・同じローカル API を用意し、
paths.py の環境変数フックで差し替える。
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import ssl
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .. import paths
from ..riot import process as riot_process

# 実機 (Riot Client 134.x) の RiotGamesPrivateSettings.yaml と同じ形。
# 実物は cookie 名も値も引用符で囲み、名前付きマッピングで持ち、
# 寿命は JWT の exp ではなく expiryTime フィールドに入っている。
# ここを本物に合わせておかないと、テストが通っても実機で動かない。
_COOKIE_BLOCK = """\
    {name}:
        domain: "riotgames.com"
        expiryTime: {expiry}
        hostOnly: false
        httpOnly: true
        name: "{name}"
        path: "/"
        persistent: true
        secureOnly: true
        value: "{value}"
"""

PRIVATE_SETTINGS_TEMPLATE = """\
psl:
    authorization:
        riot-client: null
riot-login:
    persist: null
rso-authenticator:
"""

# 「ログイン情報を保存する」が無効なとき。tdid だけがあって ssid は無い
LOGGED_OUT_TEMPLATE = PRIVATE_SETTINGS_TEMPLATE + _COOKIE_BLOCK.format(
    name="tdid", expiry=0, value="PLACEHOLDER"
)

# 一部の版はリスト形式で持つ。パーサがどちらでも読めることを確かめる用
LIST_FORM_TEMPLATE = """\
riot-login:
  persist:
    session:
      cookies:
      - domain: auth.riotgames.com
        name: ssid
        path: /
        value: {ssid}
"""

CLIENT_SETTINGS = """\
install:
  globals:
    region: AP
    locale: ja_JP
"""


def _b64(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def make_jwt(puuid: str, ttl_days: int = 30, no_exp: bool = False,
             no_sub: bool = False, **extra) -> str:
    """署名は飾り。中身のクレームが本物と同じ形になっていればよい。

    実機を見て分かったこと（モックが違っているとテストが嘘をつく）:
      - tdid は exp も sub も持たない。クレームは iat / id / nonce だけ
      - したがって tdid の寿命は YAML の expiryTime にしか無い
    no_exp / no_sub でその状況を再現する。
    """
    header = _b64({"alg": "RS256", "kid": "mock", "typ": "JWT"})
    claims = {
        "iss": "https://auth.riotgames.com",
        "iat": int(time.time()),
        **extra,
    }
    if not no_sub:
        claims["sub"] = puuid
    if not no_exp:
        claims["exp"] = int(time.time()) + ttl_days * 86400
    payload = _b64(claims)
    sig = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    return f"{header}.{payload}.{sig}"


def cookie_session_yaml(puuid: str, ttl_days: int = 30) -> str:
    """旧 (ssid cookie) 形式のログイン済み状態。後方互換の確認用。"""
    expiry = int(time.time() + ttl_days * 86400)
    blocks = "".join(
        _COOKIE_BLOCK.format(name=name, expiry=expiry, value=value)
        for name, value in (
            ("clid", make_jwt(puuid, ttl_days, cid="clid")),
            ("csid", make_jwt(puuid, ttl_days, cid="csid")),
            ("ssid", make_jwt(puuid, ttl_days)),
            ("tdid", _tdid_jwt()),
        )
    )
    return PRIVATE_SETTINGS_TEMPLATE + blocks


def _tdid_jwt() -> str:
    """端末 ID。実物と同じく sub も exp も持たない。"""
    return make_jwt("", 0, no_exp=True, no_sub=True,
                    id=secrets.token_hex(8), nonce=secrets.token_hex(8))


# 現行 (Riot Client v138) の形。セッションは ssid cookie ではなく
# psl.authorization.riot-client の OAuth refresh_token として保存される。
# 期限は last_token_creation_time + max_duration_between_restores。
REFRESH_SESSION_TEMPLATE = """\
psl:
    authorization:
        riot-client:
            claims: []
            id_token: "{id_token}"
            is_dpop_bound: false
            last_token_creation_time: {last_ms}
            max_duration_between_restores: {window}
            original_token_creation_time: {original_ms}
            refresh_token: "{refresh_token}"
            refresh_token_write_count: {writes}
            refresh_tokens_session_id: "{session_id}"
            scopes:
            - "openid"
            - "link"
            - "ban"
            - "lol_region"
            - "lol"
            - "account"
riot-login:
    persist: null
rso-authenticator:
"""

# 実機の値。約 41 日
DEFAULT_RESTORE_WINDOW = 3566083


def session_yaml(puuid: str, ttl_days: float = DEFAULT_RESTORE_WINDOW / 86400,
                 game_name: str = "MockPlayer", tag_line: str = "JP1",
                 writes: int = 3) -> str:
    """ログイン済み状態の RiotGamesPrivateSettings.yaml (現行形式)。

    ttl_days は「残り日数」。最後の発行時刻から逆算して組み立てる。
    """
    window = DEFAULT_RESTORE_WINDOW
    now = time.time()
    last_ms = int((now + ttl_days * 86400 - window) * 1000)
    id_token = make_jwt(puuid, 1, aud="riot-client",
                        acct={"game_name": game_name, "tag_line": tag_line},
                        player_locale="ja-JP")
    return REFRESH_SESSION_TEMPLATE.format(
        id_token=id_token,
        refresh_token="eyJlbmMiOiJtb2NrIn0." + secrets.token_urlsafe(96),
        last_ms=last_ms,
        original_ms=last_ms - 7_200_000,
        window=window,
        writes=writes,
        session_id=str(uuid.uuid4()),
    ) + _COOKIE_BLOCK.format(name="tdid", expiry=int(now + 365 * 86400),
                             value=_tdid_jwt())


class FakeRiotEnv:
    """偽 Riot 環境ひとそろい。with 文で使う。"""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else Path(tempfile.mkdtemp(prefix="vam-mock-"))
        self.local_appdata = self.root / "LocalAppData"
        self.client_dir = self.local_appdata / "Riot Games" / "Riot Client"
        self.exe = self.root / "RiotClientServices.exe.cmd"
        self.launch_log = self.root / "launch.log"
        self._saved_env: dict[str, str | None] = {}
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- 環境の組み立て -----------------------------------------------------
    def build(self, puuid: str | None = None, ttl_days: int = 30) -> str:
        puuid = puuid or str(uuid.uuid4())
        (self.client_dir / "Data").mkdir(parents=True, exist_ok=True)
        (self.client_dir / "Config").mkdir(parents=True, exist_ok=True)
        self.write_session(puuid, ttl_days)
        (self.client_dir / "Config" / "RiotClientSettings.yaml").write_text(
            CLIENT_SETTINGS, encoding="utf-8"
        )
        # 起動されたら引数をログに書くだけの偽 exe
        self.exe.write_text(
            "@echo off\r\n"
            f'echo %DATE% %TIME% %* >> "{self.launch_log}"\r\n',
            encoding="utf-8",
        )
        return puuid

    def write_session(self, puuid: str, ttl_days: int = 30) -> str:
        (self.client_dir / "Data" / "RiotGamesPrivateSettings.yaml").write_text(
            session_yaml(puuid, ttl_days), encoding="utf-8"
        )
        (self.client_dir / "Data" / "RiotClientPrivateSettings.yaml").write_text(
            "private:\n  settings: {}\n", encoding="utf-8"
        )
        return puuid

    def logged_out(self) -> None:
        for name in ("RiotGamesPrivateSettings.yaml", "RiotClientPrivateSettings.yaml"):
            f = self.client_dir / "Data" / name
            if f.is_file():
                f.unlink()

    # -- ローカル API (lockfile + HTTPS) -----------------------------------
    def start_local_api(self, puuid: str, game_name: str = "MockPlayer",
                        tag_line: str = "JP1") -> int:
        password = secrets.token_hex(16)
        handler = _make_handler(password, puuid, game_name, tag_line)
        ctx = _self_signed_context(self.root)
        self._server = HTTPServer(("127.0.0.1", 0), handler)
        self._server.socket = ctx.wrap_socket(self._server.socket, server_side=True)
        port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        (self.client_dir / "Config").mkdir(parents=True, exist_ok=True)
        (self.client_dir / "Config" / "lockfile").write_text(
            f"Riot Client:{os.getpid()}:{port}:{password}:https", encoding="utf-8"
        )
        return port

    def stop_local_api(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        lock = self.client_dir / "Config" / "lockfile"
        if lock.is_file():
            lock.unlink()

    # -- paths.py の差し替え ------------------------------------------------
    # -- 起動中プロセスの模擬 ----------------------------------------------
    def set_running(self, *names: str) -> None:
        """モック環境で「起動している」ことにするプロセス名。"""
        riot_process._MOCK_RUNNING.clear()
        riot_process._MOCK_RUNNING.update(names)

    def running(self) -> set[str]:
        return set(riot_process._MOCK_RUNNING)

    def activate(self) -> "FakeRiotEnv":
        for key, value in (
            (paths.ENV_OVERRIDE_LOCALAPPDATA, str(self.local_appdata)),
            (paths.ENV_OVERRIDE_CLIENT_EXE, str(self.exe)),
        ):
            self._saved_env[key] = os.environ.get(key)
            os.environ[key] = value
        return self

    def deactivate(self) -> None:
        riot_process._MOCK_RUNNING.clear()
        for key, old in self._saved_env.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        self._saved_env.clear()
        self.stop_local_api()

    def __enter__(self) -> "FakeRiotEnv":
        return self.activate()

    def __exit__(self, *exc) -> None:
        self.deactivate()


def _self_signed_context(workdir: Path) -> ssl.SSLContext:
    """本物の Riot ローカル API も自己署名証明書なので、それに合わせる。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
        .sign(key, hashes.SHA256())
    )
    cert_file = workdir / "mock-cert.pem"
    cert_file.write_bytes(
        cert.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file)
    return ctx


def _make_handler(password: str, puuid: str, game_name: str, tag_line: str):
    expected = "Basic " + base64.b64encode(f"riot:{password}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # 標準エラーを汚さない
            pass

        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.headers.get("Authorization") != expected:
                return self._send(401, {"errorCode": "UNAUTHORIZED"})
            if self.path.startswith("/entitlements/v1/token"):
                return self._send(200, {
                    "accessToken": make_jwt(puuid, 1, aud="valorant"),
                    "token": make_jwt(puuid, 1, aud="entitlements"),
                    "subject": puuid,
                })
            if self.path.startswith("/chat/v1/session"):
                return self._send(200, {
                    "puuid": puuid, "game_name": game_name, "game_tag": tag_line,
                    "loaded": True, "region": "ap", "state": "connected",
                })
            if self.path.startswith("/riotclient/region-locale"):
                return self._send(200, {"region": "AP", "webLanguage": "ja-JP"})
            return self._send(404, {"errorCode": "NOT_FOUND", "path": self.path})

    return Handler
