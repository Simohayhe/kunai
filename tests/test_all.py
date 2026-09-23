"""通しの動作確認。VALORANT 未インストールでも全部走る。

    python tests/test_all.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, condition, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [ok]   {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


def section(title: str) -> None:
    print(f"\n== {title} " + "=" * max(0, 58 - len(title)))


# ==========================================================================
def test_crypto() -> None:
    section("暗号")
    from vam import crypto

    d = Path(tempfile.mkdtemp())
    ks = crypto.KeyStore(d / "k.json")
    key = ks.create()
    check("DPAPI 鍵の往復", ks.load() == key)
    check("鍵ファイルに平文の鍵が無い", key not in (d / "k.json").read_bytes())

    blob = crypto.encrypt(key, b"secret")
    check("AES-GCM 往復", crypto.decrypt(key, blob) == b"secret")
    check("暗号文に平文が出ない", b"secret" not in blob)

    ks2 = crypto.KeyStore(d / "k2.json")
    key2 = ks2.create("pw")
    check("パスワード保護の判定", ks2.needs_password)
    check("正しいパスワードで開く", ks2.load("pw") == key2)
    try:
        ks2.load("bad")
        check("誤パスワードを拒否", False, "開いてしまった")
    except crypto.VaultLocked:
        check("誤パスワードを拒否", True)
    try:
        ks2.load()
        check("パスワード無しを拒否", False, "開いてしまった")
    except crypto.VaultLocked:
        check("パスワード無しを拒否", True)

    tampered = bytearray(blob)
    tampered[-1] ^= 0xFF
    try:
        crypto.decrypt(key, bytes(tampered))
        check("改ざん検知", False, "復号できてしまった")
    except Exception:
        check("改ざん検知", True)


def test_storage() -> None:
    section("保管庫")
    from vam.models import Account
    from vam.storage import Vault

    d = Path(tempfile.mkdtemp())
    v = Vault(d)
    v.initialize()
    a = v.add(Account(label="テスト", username="u1", password="p@ss!", riot_id="Name#TAG"))
    b = v.add(Account(label="二番目"))
    v.write_session_blob(a.id, "Data/RiotGamesPrivateSettings.yaml", b"cookie-data")

    raw = (d / "accounts.dat").read_bytes()
    check("ディスク上でパスワードが読めない", b"p@ss!" not in raw)
    check("ディスク上で Riot ID が読めない", b"Name#TAG" not in raw)

    v2 = Vault(d)
    v2.open()
    got = v2.get(a.id)
    check("再読込で復元", got is not None and got.password == "p@ss!")
    check("Riot ID の分解", got.game_name == "Name" and got.tag_line == "TAG")
    check("セッション blob の往復",
          v2.read_session_blobs(a.id) == {"Data/RiotGamesPrivateSettings.yaml": b"cookie-data"})

    v2.reorder([b.id, a.id])
    check("並べ替え", [x.id for x in v2.accounts()] == [b.id, a.id])

    v2.remove(a.id)
    check("削除でセッションも消える", not v2.session_dir_for(a.id).exists())
    check("削除後の件数", len(v2.accounts()) == 1)

    v3 = Vault(Path(tempfile.mkdtemp()))
    v3.initialize("master-pw")
    check("パスワード付き保管庫", v3.needs_password)
    v3.close()
    check("close で閉じる", not v3.is_open)


def test_session() -> None:
    section("セッション解析")
    from vam.mock.fake_riot import FakeRiotEnv, make_jwt
    from vam.riot import session

    env = FakeRiotEnv()
    puuid = env.build(puuid="11111111-2222-3333-4444-555555555555")
    with env:
        blobs = session.capture()
        info = session.inspect_blobs(blobs)
        check("puuid を読める", info.puuid == puuid, info.puuid)
        check("現行形式と判定", info.kind == "refresh_token", info.kind)
        check("refresh_token を読める", len(info.refresh_token) > 50)
        check("id_token から Riot ID を読める",
              info.riot_id == "MockPlayer#JP1", info.riot_id)
        check("端末 ID (tdid) も併存する", bool(info.cookies.get("tdid")))
        check("有効判定", info.valid and not info.expired)
        check("残り日数", 29 < info.expires_in_days <= 30, f"{info.expires_in_days}")

        # 期限切れ
        env.write_session("aaaa1111-2222-3333-4444-555555555555", ttl_days=-1)
        expired = session.current_session_info()
        check("期限切れを検出", expired.expired)

        # 復元
        session.restore(blobs)
        check("復元で元の puuid に戻る", session.current_session_info().puuid == puuid)

        # ログアウト
        removed = session.clear_current()
        check("ログアウトでファイルが消える", len(removed) >= 1)
        check("ログアウト後は無効", not session.current_session_info().valid)

        # 保存が無い状態での復元はエラー
        try:
            session.restore({})
            check("空セッションの復元を拒否", False)
        except session.SessionError:
            check("空セッションの復元を拒否", True)

    # 旧 (ssid cookie) 形式も引き続き読めること
    from vam.mock.fake_riot import cookie_session_yaml, session_yaml
    legacy = session.inspect_blobs({
        "x.yaml": cookie_session_yaml("legacy-puuid", 30).encode("utf-8")})
    check("旧形式も読める", legacy.kind == "cookie" and legacy.valid, legacy.kind)
    check("旧形式の ssid", bool(legacy.cookies.get("ssid")))
    check("旧形式の clid/csid/tdid",
          all(legacy.cookies.get(k) for k in ("clid", "csid", "tdid")))
    check("旧形式の puuid", legacy.puuid == "legacy-puuid")

    # 新規ログイン直後は max_duration_between_restores が無い。
    # 値はアカウントで桁違い (実機で 41.27 日 と 3.39 日) なので推測しない。
    fresh = session.inspect_blobs({
        "x.yaml": session_yaml("fresh-puuid", 30, with_window=False).encode("utf-8")})
    check("期限フィールドが無くても有効と判定", fresh.valid)
    check("期限は不明として扱う", fresh.expiry_unknown)
    check("期限不明なら残り日数は 0", fresh.expires_in_days == 0.0)
    check("期限不明を失効扱いにしない", not fresh.expired)
    check("期限不明でも puuid は読める", fresh.puuid == "fresh-puuid")

    check("JWT の中身を読める",
          session.decode_jwt_payload(make_jwt("abc"))["sub"] == "abc")
    check("壊れた JWT は空辞書", session.decode_jwt_payload("not-a-jwt") == {})


def test_localapi() -> None:
    section("ローカル API")
    from vam.mock.fake_riot import FakeRiotEnv
    from vam.riot import localapi

    env = FakeRiotEnv()
    puuid = env.build(puuid="99999999-8888-7777-6666-555555555555")
    with env:
        check("未起動なら lockfile 無し", localapi.read_lockfile() is None)
        env.start_local_api(puuid, "Tester", "JP9")

        lf = localapi.read_lockfile()
        check("lockfile を解析", lf is not None and lf.protocol == "https")

        c = localapi.LocalClient()
        s = c.session()
        check("puuid を取得", s.puuid == puuid)
        check("Riot ID を取得", s.riot_id == "Tester#JP9", s.riot_id)
        check("region を取得", s.region == "ap", s.region)
        check("トークンを取得", bool(s.access_token and s.entitlements_token))

        bad = localapi.LocalClient(
            localapi.Lockfile(lf.name, lf.pid, lf.port, "wrong", lf.protocol)
        )
        try:
            bad.entitlements()
            check("誤パスワードを拒否", False)
        except localapi.LocalApiError:
            check("誤パスワードを拒否", True)


def test_service() -> None:
    section("サービス層（切り替え通し）")
    from vam.mock.fake_riot import FakeRiotEnv
    from vam.models import Account
    from vam.service import AccountService, ServiceError
    from vam.storage import Vault

    env = FakeRiotEnv()
    puuid_a = env.build(puuid="aaaa0000-0000-0000-0000-00000000000a")
    vault = Vault(Path(tempfile.mkdtemp()))
    vault.initialize()

    with env:
        svc = AccountService(vault)
        env.start_local_api(puuid_a, "MainAcc", "JP1")
        a = svc.import_current()
        env.stop_local_api()
        check("取り込み: Riot ID", a.riot_id == "MainAcc#JP1", a.riot_id)
        check("取り込み: puuid", a.puuid == puuid_a)
        check("取り込み: セッション保存済み", a.session_saved)

        puuid_b = env.write_session("bbbb0000-0000-0000-0000-00000000000b")
        b = svc.import_current(label="サブ")
        check("2件目の取り込み", b.puuid == puuid_b)
        check("現在アカウントの判定", svc.current_account().id == b.id)

        try:
            svc.import_current()
            check("重複取り込みを拒否", False)
        except ServiceError:
            check("重複取り込みを拒否", True)

        r = svc.switch(a, launch_game=True)
        check("A へ切り替え", svc.current_account().id == a.id)
        check("切り替え方式はセッション復元", r.method == "session")
        check("警告なし", not r.warnings, str(r.warnings))
        check("起動された", r.launched)
        check("last_used が入る", vault.get(a.id).last_used_at > 0)

        svc.switch(b)
        check("B へ切り替え", svc.current_account().id == b.id)
        check("A のセッションは保持されたまま", svc.session_status(a).valid)

        # セッションもパスワードも無いアカウント
        empty = vault.add(Account(label="空"))
        try:
            svc.switch(empty)
            check("切り替え不能を検出", False)
        except ServiceError:
            check("切り替え不能を検出", True)

        launched = env.launch_log.read_text(encoding="utf-8", errors="replace")
        check("VALORANT 指定で起動している", "--launch-product=valorant" in launched)

        svc.logout_current()
        check("ログアウト後は現在アカウント無し", svc.current_account() is None)


def test_real_file_format() -> None:
    """実機 (Riot Client 134.x) から確認した YAML の形をそのまま固定する。

    ここが崩れると、テストが全部通っても実機では 1 つも cookie を読めない。
    実際に一度そうなった。
    """
    section("実ファイル形式")
    from vam.mock.fake_riot import LIST_FORM_TEMPLATE, make_jwt
    from vam.riot import session

    puuid = "dddd0000-0000-0000-0000-00000000000d"
    expiry = int(time.time() + 20 * 86400)

    # 実機そのままの形。名前も値も引用符付き、名前付きマッピング、
    # tdid は exp クレームを持たず expiryTime だけが寿命を持つ。
    real = f'''psl:
    authorization:
        riot-client: null
riot-login:
    persist: null
rso-authenticator:
    ssid:
        domain: "riotgames.com"
        expiryTime: {expiry}
        hostOnly: false
        httpOnly: true
        name: "ssid"
        path: "/"
        persistent: true
        secureOnly: true
        value: "{make_jwt(puuid, 5)}"
    tdid:
        domain: "riotgames.com"
        expiryTime: {int(time.time() + 365 * 86400)}
        hostOnly: false
        httpOnly: true
        name: "tdid"
        path: "/"
        persistent: true
        secureOnly: true
        value: "{make_jwt(puuid, 0, no_exp=True, no_sub=True, id="x", nonce="y")}"
'''
    blobs = {"Data/RiotGamesPrivateSettings.yaml": real.encode("utf-8")}
    info = session.inspect_blobs(blobs)
    check("引用符付きの cookie 名を読める", "ssid" in info.cookies, str(sorted(info.cookies)))
    check("引用符を値から剥がす", not info.cookies["ssid"].startswith('"'))
    check("puuid を読める", info.puuid == puuid)
    check("有効判定", info.valid and not info.expired)
    # JWT の exp は 5 日だが、cookie の寿命は expiryTime の 20 日
    check("expiryTime を JWT の exp より優先する",
          19 < info.expires_in_days <= 20, f"{info.expires_in_days}")

    # ssid の JWT が exp を持たない場合でも、expiryTime から期限を出せる
    no_exp_ssid = f'''rso-authenticator:
    ssid:
        expiryTime: {expiry}
        name: "ssid"
        value: "{make_jwt(puuid, 0, no_exp=True)}"
'''
    no_exp_info = session.inspect_blobs({"x.yaml": no_exp_ssid.encode("utf-8")})
    check("exp なし JWT でも例外にならない", no_exp_info.puuid == puuid)
    check("exp が無くても expiryTime から期限が出る",
          19 < no_exp_info.expires_in_days <= 20, f"{no_exp_info.expires_in_days}")

    # 「ログイン情報を保存する」が無効なとき。実機で確認した形で、
    # riot-login.persist が null、cookie は tdid だけ。
    logged_out = {"x.yaml": ("rso-authenticator:\n    tdid:\n"
                             + real.split('    tdid:\n')[1]).encode("utf-8")}
    out_info = session.inspect_blobs(logged_out)
    check("ログアウト状態には ssid が無い", "ssid" not in out_info.cookies,
          str(sorted(out_info.cookies)))
    check("ログアウト状態は無効と判定", not out_info.valid)

    # 別形式（リスト）でも読める
    list_form = {"x.yaml": LIST_FORM_TEMPLATE.format(
        ssid=make_jwt(puuid, 12)).encode("utf-8")}
    list_info = session.inspect_blobs(list_form)
    check("リスト形式も読める", list_info.cookies.get("ssid") is not None)
    check("リスト形式でも puuid を読める", list_info.puuid == puuid)
    check("リスト形式は JWT の exp を使う",
          11 < list_info.expires_in_days <= 12, f"{list_info.expires_in_days}")


def test_process_isolation() -> None:
    """モック環境では実プロセスに触らないこと。

    ここが抜けていたせいで、テストが実機の Riot Client を落とした。
    """
    section("プロセス操作の隔離")
    from vam.mock.fake_riot import FakeRiotEnv
    from vam.riot import process

    check("素の状態はモードでない", not process.mock_mode())
    env = FakeRiotEnv()
    env.build()
    with env:
        check("モック中はモックモード", process.mock_mode())
        env.set_running("RiotClientServices.exe", "VALORANT.exe")
        check("模擬プロセスが見える", process.running() == ["RiotClientServices.exe",
                                                            "VALORANT.exe"])
        check("game_running を判定", process.game_running())
        stopped = process.stop_all()
        check("停止できる", set(stopped) == {"RiotClientServices.exe", "VALORANT.exe"})
        check("停止後は空", process.running() == [])
    check("抜けたらモードが戻る", not process.mock_mode())
    check("模擬プロセスは片付く", not process._MOCK_RUNNING)


def test_autologin_geometry() -> None:
    """自動ログインの定数と座標計算。実機で実測した値を固定する。

    実機 (Riot Client v138.0.1) で測ったところ、旧定数は全部外れていた:
      - ウィンドウクラスは RCLIENT ではなく Chrome_WidgetWin_1 (Electron)
      - UI のプロセス名は RiotClientUx.exe ではなく "Riot Client.exe"
    """
    section("自動ログインの座標と定数")
    from vam.riot import autologin, process

    check("Electron のクラスを候補に含む",
          "Chrome_WidgetWin_1" in autologin.LOGIN_WINDOW_CLASSES)
    check("旧版のクラスも残してある", "RCLIENT" in autologin.LOGIN_WINDOW_CLASSES)
    check("UI のプロセス名が実機と一致",
          "Riot Client.exe" in autologin.UX_PROCESSES)
    check("終了対象に UI プロセスが入っている",
          "Riot Client.exe" in process.CLIENT_PROCESSES)
    check("UI をサービスより先に落とす",
          process.CLIENT_PROCESSES.index("Riot Client.exe")
          < process.CLIENT_PROCESSES.index("RiotClientServices.exe"))

    # 実機の実測値: ウィンドウ (78,23) 1536x864、ユーザー名欄の中心 (277,290)
    x, y = autologin.map_fraction(78, 23, 1536, 864, autologin.USERNAME_FIELD)
    check("実測したユーザー名欄の座標を再現する",
          abs(x - 277) <= 2 and abs(y - 290) <= 2, f"({x},{y})")

    # 原点や大きさが変わっても比率どおりに追随する
    x2, y2 = autologin.map_fraction(0, 0, 1000, 1000, (0.5, 0.25))
    check("比率計算が正しい", (x2, y2) == (500, 250), f"({x2},{y2})")
    x3, y3 = autologin.map_fraction(100, 200, 1000, 1000, (0.5, 0.25))
    check("原点のずれを足す", (x3, y3) == (600, 450), f"({x3},{y3})")

    import inspect
    defaults = {name: p.default for name, p
                in inspect.signature(autologin.perform_login).parameters.items()}
    check("既定でサインインまで行う", defaults["submit"] is True)
    # ログインは ID -> Tab -> パスワード -> Enter だけにする。
    # チェックボックスに触ると Tab 走査とフォーカスの巻き戻しが要る。
    check("既定ではチェックボックスに触らない",
          defaults["stay_signed_in"] is False)

    try:
        autologin.perform_login("", "x")
        check("空の入力を拒否", False)
    except autologin.AutoLoginError:
        check("空の入力を拒否", True)

    # チェックボックスの色判定。実機で採った値を使う
    check("未チェック(無彩色)を False",
          autologin.is_checkbox_checked((232, 232, 232)) is False)
    check("チェック済み(赤み)を True",
          autologin.is_checkbox_checked((72, 29, 30)) is True)
    check("非アクティブで暗転した無彩色も False",
          autologin.is_checkbox_checked((82, 82, 82)) is False)
    check("明るい赤も True",
          autologin.is_checkbox_checked((196, 48, 62)) is True)
    check("判別できない色は None",
          autologin.is_checkbox_checked((100, 90, 70)) is None)
    # 座標がずれてボタンや背景に乗ったときは触らせない
    check("フォーム背景(ほぼ白)は None",
          autologin.is_checkbox_checked((252, 252, 252)) is None)
    check("Facebook の青は None",
          autologin.is_checkbox_checked((24, 119, 242)) is None)
    check("Xbox の緑は None",
          autologin.is_checkbox_checked((16, 124, 16)) is None)
    check("Apple の黒は無彩色だが背景ではない",
          autologin.is_checkbox_checked((20, 20, 20)) is False)

    # 「サインイン状態を維持」の設定。既定は有効
    import tempfile as _tf
    from vam.service import AccountService, SETTING_STAY_SIGNED_IN
    from vam.storage import Vault as _Vault
    v = _Vault(Path(_tf.mkdtemp()))
    v.initialize()
    svc = AccountService(v)
    check("既定は触らない", svc.stay_signed_in is False)
    svc.stay_signed_in = True
    check("有効にできる", svc.stay_signed_in is True)
    check("設定ファイルに残る",
          v.settings().get(SETTING_STAY_SIGNED_IN) is True)
    check("読み直しても保たれる", AccountService(v).stay_signed_in is True)
    svc.stay_signed_in = False
    check("戻せる", svc.stay_signed_in is False)


def test_session_renewal() -> None:
    section("セッションの延長（cookie ローテーション）")
    import types
    from vam.mock.fake_riot import FakeRiotEnv, make_jwt
    from vam.models import Account
    from vam.riot import auth, session
    from vam.service import AccountService
    from vam.storage import Vault

    # cookie ローテーションは旧形式の話なので、その形で組み立てる
    from vam.mock.fake_riot import cookie_session_yaml
    puuid = "cccc0000-0000-0000-0000-00000000000c"
    env = FakeRiotEnv()
    env.build()
    blobs = {"Data/RiotGamesPrivateSettings.yaml":
             cookie_session_yaml(puuid, 10).encode("utf-8")}
    before = session.inspect_blobs(blobs)
    check("延長前の残り日数", 9 < before.expires_in_days <= 10,
          f"{before.expires_in_days}")

    # 実機の cookie は寿命を expiryTime にしか持たないことがあるので、
    # value だけ差し替えても期限は動かない。これが正しい挙動。
    fresh = {"ssid": make_jwt(puuid, 30), "clid": make_jwt(puuid, 30, cid="clid")}
    value_only = session.inspect_blobs(session.update_cookies(blobs, fresh))
    check("value だけ差し替えても期限は動かない",
          9 < value_only.expires_in_days <= 10, f"{value_only.expires_in_days}")
    check("value だけでも値は差し替わる", value_only.cookies["ssid"] == fresh["ssid"])

    new_expiry = time.time() + 30 * 86400
    rotated = session.update_cookies(blobs, fresh, {"ssid": new_expiry,
                                                    "clid": new_expiry})
    after = session.inspect_blobs(rotated)
    check("expiryTime も更新すると期限が延びる",
          29 < after.expires_in_days <= 30, f"{after.expires_in_days}")
    check("差し替えても puuid は同じ", after.puuid == puuid)
    check("指定した cookie だけ差し替わる", after.cookies["ssid"] == fresh["ssid"])
    check("指定しない cookie は元のまま",
          after.cookies["csid"] == before.cookies["csid"])

    text_before = blobs[list(blobs)[0]].decode()
    text_after = rotated[list(rotated)[0]].decode()
    check("YAML の構造は壊れない",
          text_after.count("name:") == text_before.count("name:")
          and text_after.count("expiryTime:") == text_before.count("expiryTime:"))
    check("行数が変わらない",
          len(text_after.splitlines()) == len(text_before.splitlines()))
    check("引用符が保たれる", f'value: "{fresh["ssid"]}"' in text_after)
    check("書き戻した YAML が再パースできる",
          __import__("yaml").safe_load(text_after) is not None)

    # --- 同名 cookie は期限が最も先のものを採る ---
    old_ssid, new_ssid = make_jwt(puuid, 5), make_jwt(puuid, 40)
    jar = [types.SimpleNamespace(name="ssid", value=old_ssid, expires=None),
           types.SimpleNamespace(name="ssid", value=new_ssid, expires=None),
           types.SimpleNamespace(name="tdid", value="plain-value",
                                 expires=int(time.time() + 365 * 86400))]
    collected, expiries = auth._collect_cookies(jar)
    check("重複 cookie は新しい方を採る", collected["ssid"] == new_ssid)
    check("JWT でない cookie も拾う", collected["tdid"] == "plain-value")
    check("cookie 自身の expires を拾う", expiries["tdid"] > time.time())
    check("exp しか無い cookie は JWT から期限を採る", expiries["ssid"] > time.time())

    # --- サービス層: 再認証で保管庫の cookie が更新される ---
    vault = Vault(Path(tempfile.mkdtemp()))
    vault.initialize()
    svc = AccountService(vault)
    account = vault.add(Account(label="延長テスト", puuid=puuid, session_saved=True))
    for rel, data in blobs.items():
        vault.write_session_blob(account.id, rel, data)

    def fake_reauth(cookies, **kw):
        # 実機同様、値と期限の両方を返す
        return auth.AuthResult(
            access_token="tok", puuid=puuid,
            cookies={"ssid": make_jwt(puuid, 30)},
            cookie_expiries={"ssid": time.time() + 30 * 86400},
        )

    original = auth.reauth_with_cookies
    from vam import service as service_module
    service_module.auth.reauth_with_cookies = fake_reauth
    try:
        result = svc.renew_session(account)
        check("延長されたと報告する", result["extended"])
        check("延長後の残り日数", 29 < result["after_days"] <= 30,
              f"{result['after_days']}")
        check("保管庫の cookie が更新されている",
              29 < svc.session_status(account).expires_in_days <= 30)
        check("保存時刻が更新される", vault.get(account.id).session_saved_at > 0)

        # 期限が縮む応答は無視する
        def shrinking_reauth(cookies, **kw):
            return auth.AuthResult(
                access_token="tok", puuid=puuid,
                cookies={"ssid": make_jwt(puuid, 1)},
                cookie_expiries={"ssid": time.time() + 86400},
            )
        service_module.auth.reauth_with_cookies = shrinking_reauth
        result = svc.renew_session(account)
        check("期限が縮む応答は書き戻さない", not result["extended"])
        check("縮む応答の後も期限は保たれる",
              29 < svc.session_status(account).expires_in_days <= 30)

        # 壊れた cookie も無視する
        def broken_reauth(cookies, **kw):
            return auth.AuthResult(
                access_token="tok", puuid=puuid,
                cookies={"ssid": "not-a-jwt"},
                cookie_expiries={"ssid": time.time() + 999 * 86400},
            )
        service_module.auth.reauth_with_cookies = broken_reauth
        svc.renew_session(account)
        check("壊れた cookie は書き戻さない",
              29 < svc.session_status(account).expires_in_days <= 30)
    finally:
        service_module.auth.reauth_with_cookies = original


def test_api_parsing() -> None:
    section("API の解析")
    from vam.riot import api
    from vam.riot.auth import AuthResult

    check("tier 名: Unranked", api.tier_name(0) == "Unranked")
    check("tier 名: Iron 1", api.tier_name(3) == "Iron 1")
    check("tier 名: Radiant", api.tier_name(27) == "Radiant")
    check("tier 名: 範囲外", api.tier_name(99) == "Unranked")
    check("未使用 tier は Unranked 扱い", api.tier_name(1) == "Unranked")
    check("シャード対応: latam→na", api.REGION_TO_SHARD["latam"] == "na")
    # 実機のローカル API は LoL 由来のコード (jp1) を返す。
    # そのままホスト名に入れると pd.jp1.a.pvp.net となり名前解決に失敗する。
    check("jp1 は ap シャード", api.shard_for("jp1") == "ap")
    check("na1 は na シャード", api.shard_for("na1") == "na")
    check("euw1 は eu シャード", api.shard_for("euw1") == "eu")
    check("oc1 は ap シャード", api.shard_for("oc1") == "ap")
    check("大文字でも通る", api.shard_for("JP1") == "ap")
    check("未知のリージョンは ap に寄せる", api.shard_for("zzz9") == "ap")
    check("空でも落ちない", api.shard_for("") == "ap")
    check("シャードは 4 種類に収まる",
          set(api.REGION_TO_SHARD.values()) <= {"na", "eu", "ap", "kr", "pbe"})

    season = "sea-1"
    payload = {
        "LatestCompetitiveUpdate": {"SeasonID": season},
        "QueueSkills": {"competitive": {"SeasonalInfoBySeasonID": {
            season: {"CompetitiveTier": 21, "RankedRating": 64,
                     "NumberOfWins": 38, "NumberOfGames": 71,
                     "LeaderboardRank": 0, "WinsByTier": {"20": 3, "21": 5}},
            "sea-0": {"CompetitiveTier": 18, "RankedRating": 10,
                      "NumberOfWins": 20, "NumberOfGames": 44,
                      "WinsByTier": {"24": 1}},
        }}},
    }

    client = api.ValorantApi(AuthResult(access_token="x"), region="ap")
    client._get = lambda path: payload            # ネットワークを差し替える
    snap = client.mmr("puuid")
    check("現在 tier", snap.tier == 21, str(snap.tier))
    check("現在 RR", snap.rr == 64)
    check("勝敗", (snap.wins, snap.games) == (38, 71))
    check("tier 名", snap.tier_label == "Ascendant 1", snap.tier_label)
    check("最高 tier は全シーズンから", snap.peak_tier == 24, str(snap.peak_tier))
    check("最高 tier のシーズン", snap.peak_season == "sea-0")

    client._get = lambda path: {"Balances": {
        api.CURRENCY["vp"]: 1275, api.CURRENCY["rp"]: 40, api.CURRENCY["kc"]: 8600}}
    check("ウォレット", client.wallet("p") == {"vp": 1275, "rp": 40, "kc": 8600})

    client._get = lambda path: {"Entitlements": [{"ItemID": "a"}, {"ItemID": "b"}, {}]}
    check("所持品の ID 抽出", client.entitlements("skins", "p") == ["a", "b"])
    try:
        client.entitlements("nope", "p")
        check("未知の種別を拒否", False)
    except api.ApiError:
        check("未知の種別を拒否", True)

    client._get = lambda path: {"Matches": [
        {"MatchID": "m1", "MapID": "/Game/Maps/Ascent/Ascent",
         "RankedRatingEarned": 18, "TierAfterUpdate": 21,
         "RankedRatingAfterUpdate": 64, "MatchStartTime": 1700000000000},
    ]}
    hist = client.competitive_history("p")
    check("履歴の解析", len(hist) == 1 and hist[0].rr_earned == 18)
    check("履歴の tier 名", hist[0].tier_after_name == "Ascendant 1")

    client._get = lambda path: None               # 404 相当
    check("404 でも落ちない", client.mmr("p").tier == 0)


def test_auth_helpers() -> None:
    section("認証まわり")
    import base64
    import json
    from vam.riot import auth

    header = json.loads(base64.b64decode(auth.client_platform_header()))
    check("ClientPlatform ヘッダ", header["platformOS"] == "Windows")

    tokens = auth._extract_fragment_tokens(
        "https://playvalorant.com/opt_in#access_token=AAA&id_token=BBB&expires_in=3600"
    )
    check("フラグメントから access_token", tokens["access_token"] == "AAA")
    check("フラグメントから id_token", tokens["id_token"] == "BBB")

    r = auth.AuthResult(access_token="t", entitlements_token="e",
                        game_name="N", tag_line="T", expires_at=time.time() + 60)
    check("Riot ID の組み立て", r.riot_id == "N#T")
    check("期限内", not r.expired)
    h = r.headers("1.0")
    check("ヘッダ一式", h["Authorization"] == "Bearer t"
          and h["X-Riot-Entitlements-JWT"] == "e"
          and h["X-Riot-ClientVersion"] == "1.0")

    try:
        auth.reauth_with_cookies({})
        check("cookie 無しを拒否", False)
    except auth.SessionExpired:
        check("cookie 無しを拒否", True)


def test_content() -> None:
    section("コンテンツ API（要ネットワーク）")
    from vam.riot.content import ContentCache, ContentError

    cache = ContentCache(Path(tempfile.mkdtemp()))
    try:
        version = cache.client_version()
    except ContentError as exc:
        print(f"  [skip] ネットワーク不可のため省略: {exc}")
        return

    check("クライアントバージョン", bool(version), version)
    tiers = cache.competitive_tiers()
    check("ランク表", len(tiers) == 28, str(len(tiers)))
    check("ランク名が日本語", tiers.get(27, {}).get("name") == "レディアント")
    check("ランクアイコン URL", str(tiers.get(21, {}).get("icon", "")).startswith("http"))

    skins = cache.skins()
    check("スキン一覧", len(skins) > 500, str(len(skins)))
    mapping = cache.skin_level_to_skin()
    check("skinLevel 逆引き", len(mapping) > len(skins))
    check("キャッシュが効く", cache.client_version() == version)


def test_inventory_summary() -> None:
    section("所持品の集計")
    from vam.models import Account, InventoryInfo
    from vam.riot.content import ContentError
    from vam.service import AccountService
    from vam.storage import Vault

    vault = Vault(Path(tempfile.mkdtemp()))
    vault.initialize()
    svc = AccountService(vault)
    try:
        weapons = svc.content.weapons()
    except ContentError as exc:
        print(f"  [skip] ネットワーク不可のため省略: {exc}")
        return

    # 複数の武器にまたがるよう、武器ごとに数枚ずつ levels を集める
    levels: list[str] = []
    for w in weapons.values():
        for skin in list(w["skins"].values())[:3]:
            levels.extend(skin["levels"][:1])

    account = vault.add(Account(
        label="所持テスト",
        inventory=InventoryInfo(skin_level_ids=levels),
    ))
    groups = svc.weapon_inventory(account)
    check("武器ごとに分かれる", len(groups) > 1, str(len(groups)))
    check("名前が入っている", all(s.name for g in groups for s in g.skins))
    check("所持数の多い順", [g.count for g in groups] == sorted([g.count for g in groups], reverse=True))
    check("武器名にスキンが紐づく", all(g.name for g in groups))


def test_ui() -> None:
    section("UI の構築")
    from PySide6.QtWidgets import QApplication, QDialog
    from vam.mock.demo_data import FIRST_PUUID, seed
    from vam.mock.fake_riot import FakeRiotEnv
    from vam.storage import Vault
    from vam.ui.dialogs import AccountDialog, SetupDialog, UnlockDialog
    from vam.ui.main_window import MainWindow

    QApplication.instance() or QApplication([])

    env = FakeRiotEnv()
    env.build(puuid=FIRST_PUUID)
    with env:
        vault = Vault(Path(tempfile.mkdtemp()))
        vault.initialize()
        accounts = seed(vault)
        check("デモデータ投入", len(accounts) == 4)

        window = MainWindow(vault)
        check("カードが並ぶ", len(window.cards) == 4, str(len(window.cards)))
        check("お気に入りが先頭",
              window.vault.get(list(window.cards)[0]).favorite)
        check("初期選択がある", window.selected_id is not None)

        window.refresh_environment()
        check("現在アカウントを検出", window.current_id is not None)

        # 検索。ウィンドウ自体を表示していないので isHidden で判定する
        shown = lambda: [c for c in window.cards.values() if not c.isHidden()]
        window.search.setText("サブ2")
        check("検索で絞り込む", len(shown()) == 1, str(len(shown())))
        window.search.setText("アセンダント")     # ランク名でも引ける
        check("ランク名で検索", len(shown()) == 1, str(len(shown())))
        window.search.setText("該当なし")
        check("該当なしで 0 件", len(shown()) == 0, str(len(shown())))
        window.search.clear()
        check("検索クリアで全件", len(shown()) == 4, str(len(shown())))

        # 選択と切り替え可否
        target = [a for a in vault.accounts() if a.label == "サブ3"][0]
        window.select_account(target.id)
        check("セッション無しは起動不可", not window.switch_button.isEnabled())
        main_acc = [a for a in vault.accounts() if a.label == "メイン"][0]
        window.select_account(main_acc.id)
        check("セッション有りは起動可", window.switch_button.isEnabled())

        # コンテキストメニュー
        menu = window._build_menu(main_acc.id)
        labels = [a.text() for a in menu.actions() if a.text()]
        check("メニューが作れる", len(labels) >= 5, str(labels))
        check("メニューに削除がある", any("削除" in t for t in labels))
        check("メニューにセッション延長がある", any("延長" in t for t in labels))
        renew = [a for a in menu.actions() if "延長" in a.text()][0]
        check("セッション有りなら延長できる", renew.isEnabled())
        no_session = window._build_menu(target.id)
        renew2 = [a for a in no_session.actions() if "延長" in a.text()][0]
        check("セッション無しは延長不可", not renew2.isEnabled())

        # 残り日数の表示
        card = window.cards[main_acc.id]
        check("カードに残り日数が出る", "残り" in card.status.text(), card.status.text())
        check("カードのツールチップに期限日時", "有効期限" in card.toolTip(), card.toolTip())
        window.select_account(main_acc.id)
        banner = window.detail.overview.session_banner.text()
        check("概要にセッション帯が出る", "残り" in banner and "まで" in banner, banner)
        window.select_account(target.id)
        check("未保存なら帯が警告になる",
              "未保存" in window.detail.overview.session_banner.text())
        window.select_account(main_acc.id)

        # お気に入り切り替え
        before = vault.get(main_acc.id).favorite
        window.toggle_favorite(main_acc.id)
        check("お気に入り切り替え", vault.get(main_acc.id).favorite != before)

        # ランクアイコンを消さずに再描画できるか
        from PySide6.QtGui import QPixmap
        pm = QPixmap(16, 16)
        pm.fill()
        window._rank_icons = {21: "http://example/x.png"}
        window.icons._pixmaps["http://example/x.png"] = pm
        window.reload_accounts()
        card = window.cards[main_acc.id]
        check("再描画してもランクアイコンが残る", card.badge._pixmap is not None)

        # ダイアログ
        dialog = AccountDialog(vault.get(main_acc.id))
        check("編集ダイアログに値が入る", dialog.label.text() == "メイン")
        dialog.label.setText("変更後")
        dialog.riot_id.setText("New#TAG")
        result = dialog.result_account()
        check("ダイアログの結果を取り出せる",
              result.label == "変更後" and result.riot_id == "New#TAG")

        new_dialog = AccountDialog()
        check("新規ダイアログは空", not new_dialog.label.text())

        v2 = Vault(Path(tempfile.mkdtemp()))
        check("初期設定ダイアログ", SetupDialog(v2) is not None)
        v3 = Vault(Path(tempfile.mkdtemp()))
        v3.initialize("pw")
        unlock = UnlockDialog(v3)
        unlock.password.setText("wrong")
        unlock.accept()
        check("誤パスワードでダイアログが閉じない",
              not unlock.error.isHidden() and unlock.result() != QDialog.Accepted)

        window.close()


def main() -> int:
    print("Kunai — 通しテスト")
    for fn in (test_crypto, test_storage, test_session, test_localapi,
               test_service, test_real_file_format, test_process_isolation,
               test_autologin_geometry,
               test_session_renewal, test_api_parsing, test_auth_helpers,
               test_content, test_inventory_summary, test_ui):
        try:
            fn()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            FAILED.append((fn.__name__, f"例外: {exc}"))

    print("\n" + "=" * 64)
    print(f"成功 {len(PASSED)} 件 / 失敗 {len(FAILED)} 件")
    for name, detail in FAILED:
        print(f"  FAILED: {name}  {detail}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
