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

    # 途中からのパスワード付け外し
    d4 = Path(tempfile.mkdtemp())
    v4 = Vault(d4)
    v4.initialize()  # 最初はパスワード無し
    v4.add(Account(label="残る子"))
    check("最初はパスワード無し", not v4.needs_password)

    v4.change_password("new-pw")
    check("途中からパスワードを付けられる", v4.needs_password)
    v4.close()
    from vam.crypto import VaultLocked
    try:
        Vault(d4).open()
        check("パスワード無しでは開けなくなる", False, "例外が出なかった")
    except VaultLocked:
        check("パスワード無しでは開けなくなる", True)

    v5 = Vault(d4)
    v5.open("new-pw")
    check("正しいパスワードでは開ける", len(v5.accounts()) == 1)
    v5.change_password(None)
    check("途中からパスワードを外せる", not v5.needs_password)
    v5.close()
    v6 = Vault(d4)
    v6.open()  # パスワード無しで開けるはず
    check("外した後はパスワード無しで開ける", len(v6.accounts()) == 1)


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

        before_used = vault.get(a.id).last_used_at
        svc.switch(a, launch_game=False, mark_used=False)
        check("mark_used=False なら使用順に反映しない", vault.get(a.id).last_used_at == before_used)

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
    for g in groups:
        ranks = [s.tier_rank for s in g.skins]
        check("スキンがエディション昇順", ranks == sorted(ranks), str(ranks))


def test_match_cache() -> None:
    section("戦績キャッシュ")
    from vam.match_cache import MatchCache
    from vam.riot.api import MatchSummary

    cache = MatchCache(Path(tempfile.mkdtemp()))
    check("未取得はNone", cache.get("puuid1", "match1") is None)

    summary = MatchSummary(match_id="match1", map_id="map1", character_id="agent1",
                           won=True, kills=20, deaths=10, headshots=5,
                           bodyshots=10, legshots=1, party=[])
    cache.put("puuid1", summary)
    check("保存前はファイル未作成", not cache.path.exists())
    cache.save()
    check("保存後はファイルができる", cache.path.exists())

    reloaded = MatchCache(cache.path.parent)
    got = reloaded.get("puuid1", "match1")
    check("読み直しても取れる", got is not None and got.match_id == "match1")
    check("内容が一致する", got.kills == 20 and got.won is True)
    check("他アカウントには漏れない", reloaded.get("puuid2", "match1") is None)
    check("別試合は別物", reloaded.get("puuid1", "match2") is None)
    cache.put("puuid1", MatchSummary(match_id="old", won=True))   # party 情報の無い古い形
    check("パーティー情報の無い古いキャッシュは取り直す", cache.get("puuid1", "old") is None)


def test_match_stats_api_mode() -> None:
    section("戦績 (APIモード)")
    from vam.models import Account
    from vam.riot import henrik
    from vam.service import AccountService
    from vam.storage import Vault

    vault = Vault(Path(tempfile.mkdtemp()))
    vault.initialize()
    svc = AccountService(vault)
    svc.henrik_api_key = "dummy-key"

    def _p(puuid, team, name, tag, party, score, kills=0, deaths=0, assists=0):
        return {"puuid": puuid, "team_id": team, "name": name, "tag": tag,
               "party_id": party, "agent": {"id": "a1", "name": "ジェット"},
               "stats": {"score": score, "kills": kills, "deaths": deaths,
                        "assists": assists, "headshots": 8, "bodyshots": 10,
                        "legshots": 2, "damage": {"dealt": 2000, "received": 1500}}}

    fake_matches = [{
        "metadata": {"match_id": "m1", "map": {"id": "map1", "name": "アセント"},
                     "started_at": "2026-01-01T00:00:00.000Z"},
        "players": [
            _p("puuid1", "Red", "Self", "1234", "party-a", 6000, 20, 10, 2),
            _p("puuid2", "Red", "Duo", "5678", "party-a", 5000, 15, 12, 3),
            _p("puuid3", "Blue", "Enemy1", "1111", "party-b", 5500, 18, 11, 1),
            _p("puuid4", "Blue", "Enemy2", "2222", "party-b", 3000, 8, 15, 4),
        ],
        "teams": [{"team_id": "Red", "won": True, "rounds": {"won": 13, "lost": 8}},
                 {"team_id": "Blue", "won": False, "rounds": {"won": 8, "lost": 13}}],
    }]
    fake_history = [{"match_id": "m1", "tier": {"id": 21, "name": "アセンダント1"},
                     "rr": 50, "last_change": 12, "date": "2026-01-01T00:00:00.000Z"}]

    original_find_matches = henrik.find_matches
    original_find_mmr_history = henrik.find_mmr_history
    henrik.find_matches = lambda puuid, region, api_key, count=20: fake_matches
    henrik.find_mmr_history = lambda puuid, region, api_key: fake_history
    try:
        account = Account(label="api-test", puuid="puuid1", region="ap")
        stats = svc.match_stats(account, count=5)
    finally:
        henrik.find_matches = original_find_matches
        henrik.find_mmr_history = original_find_mmr_history

    check("APIキーがあれば使う", stats.source == "api")
    check("試合数が正しい", stats.games == 1, str(stats.games))
    check("勝敗が正しい", stats.wins == 1)
    check("HS率が計算される", round(stats.headshot_pct) == 40, str(stats.headshot_pct))
    check("試合詳細が入る", stats.matches[0].match_id == "m1")
    check("RR増減が入る", stats.matches[0].rr_earned == 12, str(stats.matches[0].rr_earned))
    check("エージェント別に出る", stats.by_agent and stats.by_agent[0].name == "ジェット")

    # スコアボード (キャッシュ済みの生データを使うので通信は発生しない)
    detail = svc.match_detail(account, "m1")
    check("味方2人・敵2人", len(detail.my_team) == 2 and len(detail.enemy_team) == 2)
    check("スコア降順", [p.score for p in detail.my_team] == [6000, 5000])
    check("自分にis_selfが立つ", detail.my_team[0].is_self and not detail.my_team[1].is_self)
    check("デュオのparty_idが一致", detail.my_team[0].party_id == detail.my_team[1].party_id)
    check("味方と敵のparty_idは別", detail.my_team[0].party_id != detail.enemy_team[0].party_id)
    check("チームスコアが入る", detail.my_team_score == 13 and detail.enemy_team_score == 8,
          f"{detail.my_team_score}-{detail.enemy_team_score}")
    check("勝敗が入る", detail.won is True)

    # account.region がローカル API 由来の不正な値 (例: "jp1") でも、
    # puuid があれば Riot ID から HenrikDev 自身の region に引き直す。
    original_find_account = henrik.find_account
    henrik.find_account = lambda name, tag, api_key: henrik.PlayerAccount(
        puuid="puuid1", name=name, tag=tag, region="ap",
    )
    try:
        bad_region_account = Account(label="bad-region", puuid="puuid1",
                                     region="jp1", riot_id="Self#1234")
        resolved_puuid, resolved_region = svc._resolve_henrik_identity(bad_region_account)
    finally:
        henrik.find_account = original_find_account
    check("不正なregionはHenrikDev自身の値に引き直す", resolved_region == "ap",
          resolved_region)
    check("puuidは維持される", resolved_puuid == "puuid1")

    # キー未設定ならローカル API 経路に落ちる (セッション未保存で弾かれる)
    vault2 = Vault(Path(tempfile.mkdtemp()))
    vault2.initialize()
    svc2 = AccountService(vault2)
    from vam.service import ServiceError
    try:
        svc2.match_stats(Account(label="no-key-test"))
        check("キー未設定ならローカルAPI経路", False, "例外が出なかった")
    except ServiceError:
        check("キー未設定ならローカルAPI経路", True)


def test_history_persistence() -> None:
    section("戦績のアカウント別保存 (JSON)")
    from vam.models import Account
    from vam.riot import henrik
    from vam.service import AccountService, ServiceError
    from vam.storage import Vault

    root = Path(tempfile.mkdtemp())
    vault = Vault(root)
    vault.initialize()
    svc = AccountService(vault)
    svc.henrik_api_key = "dummy-key"

    def match(mid, started, won=True, rr=0):
        return {
            "metadata": {"match_id": mid, "map": {"id": "map1", "name": "アセント"},
                         "started_at": started},
            "players": [{"puuid": "puuid1", "team_id": "Red", "name": "Self", "tag": "1",
                         "party_id": "p", "agent": {"id": "a1", "name": "ジェット"},
                         "stats": {"score": 5000, "kills": 10, "deaths": 5, "assists": 1,
                                   "headshots": 4, "bodyshots": 4, "legshots": 2,
                                   "damage": {"dealt": 1500, "received": 1000}}},
                        {"puuid": "puuid9", "team_id": "Blue", "name": "E", "tag": "2",
                         "party_id": "q", "agent": {"id": "a2", "name": "ネオン"},
                         "stats": {"score": 3000}}],
            "teams": [{"team_id": "Red", "won": won, "rounds": {"won": 13, "lost": 5}},
                      {"team_id": "Blue", "won": not won, "rounds": {"won": 5, "lost": 13}}],
        }

    state = {"matches": [match("m1", "2026-01-01T00:00:00.000Z")], "fail": False}
    hist = [{"match_id": "m1", "tier": {"id": 21}, "rr": 50, "last_change": 12}]

    def fake_find_matches(puuid, region, api_key, count=20):
        if state["fail"]:
            raise henrik.HenrikError("ネットワークが無い")
        return state["matches"]

    orig = (henrik.find_matches, henrik.find_mmr_history)
    henrik.find_matches = fake_find_matches
    henrik.find_mmr_history = lambda puuid, region, api_key: hist
    try:
        account = Account(label="hist-test", puuid="puuid1", region="ap")
        check("保存前は無い", svc.saved_match_stats(account) is None)

        stats = svc.match_stats(account)
        path = svc.history.path_for(account.id)
        check("取得するとJSONができる", path.is_file())
        check("アカウントIDごとのファイル", path.name == f"{account.id}.json")
        saved = svc.saved_match_stats(account)
        check("通信せず保存済みが読める", saved is not None and saved.games == 1)
        check("保存済みの印が立つ", saved.from_saved and saved.saved_at > 0)
        check("集計が保存行から再現される",
              round(saved.headshot_pct) == 40 and saved.by_agent[0].name == "ジェット"
              and saved.by_map[0].name == "アセント")
        check("RRも残る", saved.matches[0].rr_earned == 12 and saved.matches[0].rr_after == 50)

        # 取得に失敗しても保存済みが出る (ログインしていない状況)
        state["fail"] = True
        offline = svc.match_stats(account)
        check("取得失敗でも戦績を返す", offline.games == 1)
        check("失敗理由が入る", "ネットワークが無い" in offline.fetch_error, offline.fetch_error)

        # 新しい試合は足され、古い分は消えない (20件を超えて貯まる)
        state["fail"] = False
        state["matches"] = [match("m2", "2026-01-02T00:00:00.000Z", won=False)]
        merged = svc.match_stats(account)
        check("保存分に足される", merged.games == 2 and merged.wins == 1, str(merged.games))
        check("新しい順", [m.match_id for m in merged.matches] == ["m2", "m1"])
        check("取得成功なら失敗理由は空", merged.fetch_error == "")
        check("古い試合のRRが残る", merged.matches[1].rr_earned == 12)

        # スコアボードは一度取れたら保存される
        d1 = svc.match_detail(account, "m2")
        svc._henrik_raw_matches.clear()
        orig_find_match = henrik.find_match
        henrik.find_match = lambda *a, **k: (_ for _ in ()).throw(AssertionError("通信した"))
        try:
            d2 = svc.match_detail(account, "m2")
        finally:
            henrik.find_match = orig_find_match
        check("保存済みスコアボードは通信しない", d2.match_id == "m2" and len(d2.my_team) == 1)
        check("内容が復元される", d2.my_team[0].is_self and d2.my_team[0].kills == 10
              and d2.enemy_team[0].agent_name == "ネオン" and d2.my_team_score == 13)

        # 保存が壊れていても落ちない
        path.write_text("{not json", encoding="utf-8")
        check("壊れたJSONは空扱い", svc.saved_match_stats(account) is None)
        stats = svc.match_stats(account)
        check("壊れていても取り直して復旧", stats.games == 1)

        # アカウントごとに別ファイル
        other = Account(label="other", puuid="puuid1", region="ap")
        check("他アカウントには見えない", svc.saved_match_stats(other) is None)

        svc.history.delete(account.id)
        check("削除でJSONも消える", not path.exists())
    finally:
        henrik.find_matches, henrik.find_mmr_history = orig

    # 保存が一つも無く、取得も失敗したときだけ例外
    svc.henrik_api_key = ""
    try:
        svc.match_stats(Account(label="nothing"))
        check("保存も取得も無ければ例外", False, "例外が出なかった")
    except ServiceError:
        check("保存も取得も無ければ例外", True)


def test_bulk_refresh() -> None:
    section("一括更新 (ランク・戦績・スキンを独立に)")
    import time as _time
    from vam.models import Account
    from vam.riot import api, auth, henrik
    from vam.service import AccountService, ServiceError
    from vam.storage import Vault

    vault = Vault(Path(tempfile.mkdtemp()))
    vault.initialize()
    svc = AccountService(vault)
    svc.henrik_api_key = "dummy-key"

    now = _time.time()
    day = 86400
    acts = [{"uuid": "actA", "name": "ACT III", "start": now - 120 * day},
            {"uuid": "actB", "name": "ACT IV", "start": now - 60 * day},
            {"uuid": "actC", "name": "ACT V", "start": now - 1 * day}]
    svc.content.acts = lambda: acts
    svc.content.tier_names = lambda: {16: "ゴールド2", 0: "アンランク"}
    svc.content.client_version = lambda: "v"

    snap = api.MmrSnapshot(tier=0, rr=0, season_id="actC",
                           seasons={"actB": {"tier": 16, "rr": 40, "wins": 9, "games": 15,
                                             "leaderboard": 0},
                                    "actC": {"tier": 0, "rr": 0, "wins": 1, "games": 2,
                                             "leaderboard": 0}})

    class FakeClient:
        mmr_error = None
        ent_error = None

        def __init__(self, *a, **k):
            pass

        def mmr(self):
            if FakeClient.mmr_error:
                raise api.ApiError(FakeClient.mmr_error)
            return snap

        def wallet(self):
            return {"vp": 100, "rp": 5, "kc": 0}

        def entitlements(self, kind):
            if FakeClient.ent_error:
                raise api.ApiError(FakeClient.ent_error)
            return [f"{kind}-1", f"{kind}-2"]

    auth_calls = {"n": 0, "fail": False}

    def fake_authenticate(account):
        auth_calls["n"] += 1
        if auth_calls["fail"]:
            raise ServiceError("セッションが保存されていません")
        return auth.AuthResult(access_token="t", entitlements_token="e", puuid="puuid1",
                               region="ap", game_name="Self", tag_line="1234")

    svc.authenticate = fake_authenticate

    match = {
        "metadata": {"match_id": "m1", "map": {"id": "map1", "name": "アセント"},
                     "started_at": "2026-01-01T00:00:00.000Z"},
        "players": [{"puuid": "puuid1", "team_id": "Red", "name": "Self", "tag": "1",
                     "party_id": "p", "agent": {"id": "a1", "name": "ジェット"},
                     "stats": {"score": 5000, "kills": 10, "deaths": 5, "assists": 1,
                               "headshots": 4, "bodyshots": 4, "legshots": 2,
                               "damage": {"dealt": 1500, "received": 1000}}}],
        "teams": [{"team_id": "Red", "won": True, "rounds": {"won": 13, "lost": 5}}],
    }
    hstate = {"fail": False}

    def fake_find_matches(puuid, region, api_key, count=20):
        if hstate["fail"]:
            raise henrik.HenrikError("API が落ちている")
        return [match]

    orig = (api.ValorantApi, henrik.find_matches, henrik.find_mmr_history)
    api.ValorantApi = FakeClient
    henrik.find_matches = fake_find_matches
    henrik.find_mmr_history = lambda puuid, region, api_key: []
    try:
        # 1. 全部成功。今のアクトにランクが無いので前アクトの最終ランクが入る
        acc = Account(label="bulk", puuid="puuid1", region="ap", session_saved=True)
        vault.add(acc)
        report = svc.refresh_account(acc)
        check("全部成功なら errors が空", report.ok and not report.held, str(report.errors))
        check("認証は 1 回で使い回す", auth_calls["n"] == 1, str(auth_calls["n"]))
        got = vault.get(acc.id)
        check("今のアクトが未ランクなら前アクトの最終ランクを出す",
              got.rank.tier == 16 and got.rank.tier_name == "ゴールド2", str(got.rank))
        check("どのアクトか分かる", got.rank.carried_from == "ACT IV", got.rank.carried_from)
        check("今アクトの戦績は今アクトのもの", got.rank.games == 2 and got.rank.wins == 1)
        check("スキンが入る", got.inventory.skin_level_ids == ["skins-1", "skins-2"])
        check("ウォレットが入る", got.wallet.vp == 100)
        check("戦績の JSON もできる", svc.history.path_for(acc.id).is_file())
        check("スコアボードも先に保存される (ログインなしで開ける)",
              "m1" in (svc.history.load(acc.id).get("details") or {}))

        # 2. 今のアクトにランクがあれば、それを使う
        snap.seasons["actC"] = {"tier": 16, "rr": 77, "wins": 5, "games": 8, "leaderboard": 0}
        svc.refresh_account(acc)
        got = vault.get(acc.id)
        check("今のアクトのランクを優先", got.rank.rr == 77 and got.rank.carried_from == "")
        snap.seasons["actC"] = {"tier": 0, "rr": 0, "wins": 1, "games": 2, "leaderboard": 0}

        # 3. ランクだけ失敗 -> 戦績とスキンは取れる
        FakeClient.mmr_error = "mmr が 500"
        hstate["fail"] = False
        acc2 = Account(label="bulk2", puuid="puuid1", region="ap", session_saved=True)
        vault.add(acc2)
        report = svc.refresh_account(acc2)
        check("失敗したのはランクだけ", list(report.errors) == ["rank"], str(report.errors))
        check("保留にはならない", not report.held)
        check("失敗の理由が入る", "mmr が 500" in report.errors["rank"])
        got2 = vault.get(acc2.id)
        check("ランクが失敗しても所持品は更新される", len(got2.inventory.skin_level_ids) == 2)
        check("ランクが失敗しても戦績は取れる", svc.history.path_for(acc2.id).is_file())
        FakeClient.mmr_error = None

        # 4. スキンが全種類失敗 -> 前回の所持品を消さない
        FakeClient.ent_error = "entitlements が 403"
        report = svc.refresh_account(acc)
        check("スキンだけ失敗", list(report.errors) == ["skins"], str(report.errors))
        check("前回の所持品が残る",
              vault.get(acc.id).inventory.skin_level_ids == ["skins-1", "skins-2"])
        FakeClient.ent_error = None

        # 5. 3 つとも失敗 -> 保留
        auth_calls.update(n=0, fail=True)
        hstate["fail"] = True
        acc3 = Account(label="bulk3", puuid="puuid1", region="ap", session_saved=True)
        vault.add(acc3)
        report = svc.refresh_account(acc3)
        check("3 つとも失敗なら保留", report.held and len(report.errors) == 3,
              str(report.errors))
        check("認証失敗は 1 回だけ試す", auth_calls["n"] == 1, str(auth_calls["n"]))
        check("理由が各ステップに入る",
              all(report.errors.get(k) for k in ("rank", "history", "skins")))

        # 6. まとめて: 失敗が混ざっても全員分回る
        auth_calls.update(n=0, fail=False)
        hstate["fail"] = False
        seen = []
        reports = svc.refresh_many([acc, acc2], progress=seen.append)
        check("全員分の結果が返る", set(reports) == {acc.id, acc2.id})
        check("進捗に [1/2] が付く", any(m.startswith("[1/2]") for m in seen))
    finally:
        api.ValorantApi, henrik.find_matches, henrik.find_mmr_history = orig


def test_refresh_cycle() -> None:
    section("一括更新の入れ替え (起動 → 取得 → 停止)")
    import threading
    from vam.models import Account
    from vam.riot import process
    from vam.service import AccountService, GameRunning, RefreshReport, ServiceError
    from vam.storage import Vault

    vault = Vault(Path(tempfile.mkdtemp()))
    vault.initialize()
    svc = AccountService(vault)
    a, b, c, orig_acc = (Account(label=n, session_saved=True) for n in "abco")
    for acc in (a, b, c, orig_acc):
        vault.add(acc)

    log: list[str] = []

    def fake_switch(account, **kw):
        log.append(f"switch:{account.label}")
        check(f"{account.label}: 更新の入れ替えは使用順に反映しない", kw.get("mark_used") is False)
        if account.label == "o":
            check("元のアカウントはクライアントを起動せず戻す",
                  kw.get("launch_game") is False and kw.get("client_only") is False)
        else:
            check(f"{account.label}: ゲームは起動せずクライアントだけ",
                  kw.get("launch_game") is False and kw.get("client_only") is True)
        check(f"{account.label}: パスワード自動入力には頼らない",
              kw.get("allow_autologin") is False)
        if account.label == "b":
            raise ServiceError("セッションが失効しています")

    def fake_refresh(account, progress=None, auth_error=None, **kw):
        log.append(f"refresh:{account.label}:{'err' if auth_error else 'ok'}")
        return RefreshReport(account.id)

    saved = (process.game_running, process.stop_all)
    saved_refresh = svc.refresh_account  # 実物 (進捗イベントのテストで戻す)
    svc._refresh_rank = lambda *a, **k: None
    svc._refresh_history = lambda *a, **k: None
    svc._refresh_skins = lambda *a, **k: None
    svc.switch = fake_switch
    svc.refresh_account = fake_refresh
    svc.current_account = lambda: orig_acc
    process.stop_all = lambda *a, **k: log.append("stop") or []
    try:
        process.game_running = lambda: True
        try:
            svc.refresh_cycle([a, b, c])
            check("VALORANT 起動中は始めない", False, "例外が出なかった")
        except GameRunning:
            check("VALORANT 起動中は始めない", True)
        check("起動中は何も触らない", log == [], str(log))

        process.game_running = lambda: False
        reports = svc.refresh_cycle([a, b, c])
        check("全員分の結果", set(reports) == {a.id, b.id, c.id})
        order = [x for x in log if x != "stop"]
        check("1 件ずつ 切り替え → 取得", order[:6] == [
            "switch:a", "refresh:a:ok", "switch:b", "refresh:b:err",
            "switch:c", "refresh:c:ok"], str(order))
        check("切り替えに失敗しても次のアカウントへ進む", "refresh:c:ok" in log)
        check("切り替え失敗は認証エラーとして渡る", "refresh:b:err" in log)
        check("毎回停止する", log.count("stop") == 3, str(log.count("stop")))
        check("最後に元のアカウントへ戻す", order[-1] == "switch:o", str(order[-1]))

        # キャンセルしたら続けない・元に戻さない
        log.clear()
        ev = threading.Event()
        ev.set()
        reports = svc.refresh_cycle([a, b], cancel_event=ev)
        check("キャンセル済みなら取得は始めない", reports == {}
              and not any(x.startswith("refresh:") for x in log), str(log))
        check("キャンセルでも元のアカウントのセッションは戻す", log == ["switch:o"], str(log))

        # 進捗イベント
        log.clear()
        events: list[dict] = []
        svc.refresh_account = saved_refresh
        reports = svc.refresh_cycle([a], events=events.append)
        kinds = [e["kind"] for e in events]
        check("進捗: start→account→client", kinds[:3] == ["start", "account", "client"], str(kinds))
        check("進捗: 件数と名前が入る", events[0]["total"] == 1 and events[1]["name"] == "a")
        check("進捗: 3 ステップ分", [e["step"] for e in events if e["kind"] == "step"
              and e["state"] == "running"] == ["rank", "history", "skins"])
        check("進捗: 完了と復元", "account_done" in kinds and kinds[-1] == "restoring")
    finally:
        process.game_running, process.stop_all = saved


def test_sort_modes() -> None:
    section("アカウントの並び順")
    from vam.models import Account
    from vam.service import AccountService
    from vam.storage import Vault

    vault = Vault(Path(tempfile.mkdtemp()))
    vault.initialize()
    svc = AccountService(vault)

    def acc(label, tier, rr, used=0.0, fav=False):
        a = Account(label=label, favorite=fav, last_used_at=used)
        a.rank.tier, a.rank.rr = tier, rr
        vault.add(a)
        return a

    low = acc("low", 12, 10, used=1)
    high = acc("high", 24, 50, used=2)
    unranked = acc("unranked", 0, 0, used=3, fav=True)
    mid_hi = acc("mid_hi", 18, 80, used=4)
    mid_lo = acc("mid_lo", 18, 20, used=5)
    names = lambda: [a.label for a in svc.sorted_accounts()]

    check("既定は お気に入り優先・使った順", names()[0] == "unranked" and names()[1] == "mid_lo", str(names()))
    svc.sort_mode = "rank"
    check("ランク順 (高い順・同ランクはRR)。お気に入りは先頭",
          names() == ["unranked", "high", "mid_hi", "mid_lo", "low"], str(names()))
    low.rank.tier = 27
    vault.update(low)
    check("ランクが変われば並びも変わる", names()[:2] == ["unranked", "low"], str(names()))
    mid_hi.favorite = True
    vault.update(mid_hi)
    check("お気に入りはランクが低くても上 (その中はランク順)",
          names()[:3] == ["mid_hi", "unranked", "low"], str(names()))
    mid_hi.favorite = False
    vault.update(mid_hi)
    check("設定は保存される", AccountService(vault).sort_mode == "rank")

    svc.sort_mode = "manual"
    vault.reorder([unranked.id, low.id, high.id, mid_lo.id, mid_hi.id])
    check("手動は保管庫の順", names() == ["unranked", "low", "high", "mid_lo", "mid_hi"], str(names()))
    high.favorite = True
    vault.update(high)
    check("手動でもお気に入りは先頭", names()[:2] == ["unranked", "high"], str(names()))
    high.favorite = False
    vault.update(high)
    check("手動の順は再読込しても残る", [a.label for a in AccountService(vault).sorted_accounts()][:2]
          == ["unranked", "low"])
    try:
        svc.sort_mode = "bogus"
        check("不正な並び順は拒否", False, "例外が出なかった")
    except ValueError:
        check("不正な並び順は拒否", True)


def test_act_status() -> None:
    section("アクトの残り日数")
    from vam.service import AccountService
    from vam.storage import Vault
    vault = Vault(Path(tempfile.mkdtemp()))
    vault.initialize()
    svc = AccountService(vault)
    acts = [{"uuid": "a", "name": "ACT IV", "start": 1000.0, "end": 5000.0},
            {"uuid": "b", "name": "ACT V", "start": 5000.0, "end": 9000.0},
            {"uuid": "c", "name": "ACT VI", "start": 9000.0, "end": 13000.0}]
    svc.content.acts = lambda: acts
    st = svc.act_status(now=6000.0)
    check("今のアクトが分かる", st["name"] == "ACT V", str(st))
    check("終了までの秒数", st["seconds_left"] == 3000.0)
    check("次のアクトに入ったら切り替わる", svc.act_status(now=9500.0)["name"] == "ACT VI")
    check("全部終わっていれば None", svc.act_status(now=20000.0) is None)
    check("始まる前なら None", svc.act_status(now=10.0) is None)
    from vam.riot import content as _content

    def boom():
        raise _content.ContentError("offline")
    svc.content.acts = boom
    check("取得できなければ None", svc.act_status(now=6000.0) is None)

    from vam.ui.main_window import format_remaining
    check("1 日以上は D 表記", format_remaining(3 * 86400 + 5000) == "3D")
    check("ちょうど 1 日も D で統一", format_remaining(86400) == "1D")
    check("1 日を切ったら h 表記", format_remaining(86400 - 1) == "23h")
    check("数時間", format_remaining(5 * 3600 + 10) == "5h")
    check("1 時間を切ったら <1h", format_remaining(3599) == "<1h" and format_remaining(0) == "<1h")


def test_browser_open() -> None:
    section("Riot アカウントページを開く")
    from vam import browser

    fake_exe = Path(tempfile.mkdtemp()) / "chrome.exe"
    fake_exe.write_text("", encoding="utf-8")
    popen_calls, web_calls = [], []
    saved = (browser._candidates, browser.subprocess.Popen, browser.webbrowser.open)
    browser.subprocess.Popen = lambda args, **k: popen_calls.append(args)
    browser.webbrowser.open = lambda url: web_calls.append(url)
    try:
        browser._candidates = lambda: [("Chrome", fake_exe, "--incognito")]
        how = browser.open_private()
        check("Chrome があればシークレットで開く",
              popen_calls and popen_calls[0][1:] == ["--incognito", browser.ACCOUNT_PAGE_URL], str(popen_calls))
        check("開き方を返す", "シークレット" in how, how)
        check("普通のブラウザは使わない", not web_calls)

        popen_calls.clear()
        browser._candidates = lambda: [("Edge", fake_exe.parent / "none.exe", "--inprivate")]
        how = browser.open_private()
        check("見つからなければ既定のブラウザ", web_calls == [browser.ACCOUNT_PAGE_URL] and "既定" in how, how)
        check("URL は Riot のアカウントページ", browser.ACCOUNT_PAGE_URL.startswith("https://account.riotgames.com"))
    finally:
        browser._candidates, browser.subprocess.Popen, browser.webbrowser.open = saved


def test_borrowed_history() -> None:
    section("ログイン中でないアカウントの戦績 (他アカウントのトークンを借りる)")
    from vam.models import Account
    from vam.riot import api, auth
    from vam.service import AccountService, ServiceError
    from vam.storage import Vault

    vault = Vault(Path(tempfile.mkdtemp()))
    vault.initialize()
    svc = AccountService(vault)  # API キー無し = サインインモード
    svc.content.client_version = lambda: "v"
    svc.content.agents = lambda: {"ag1": {"name": "ジェット", "icon": ""}}
    svc.content.maps = lambda: {"map1": {"name": "アセント", "icon": ""}}
    svc.content.tier_names = lambda: {}
    sub = Account(label="sub", puuid="SUB-PUUID", region="ap", session_saved=True)
    vault.add(sub)
    state = {"borrow": True, "borrow_calls": 0}
    seen = {"history": [], "summary": [], "details": []}

    def own_auth(account):
        raise ServiceError("今ログインしているのは別のアカウントです")

    def borrow(account, allow_other=False):
        state["borrow_calls"] += 1
        if not state["borrow"]:
            raise ServiceError("Riot Client が起動していません")
        check("借りるときは allow_other", allow_other is True)
        return auth.AuthResult(access_token="t", entitlements_token="e", puuid="MAIN-PUUID",
                               region="ap")

    class FakeClient:
        def __init__(self, result, **k):
            seen["token_owner"] = result.puuid

        def competitive_history(self, puuid=None, count=20):
            seen["history"].append(puuid)
            return [api.CompetitiveUpdate(match_id="mS", map_id="map1", season_id="x",
                    started_at=1000, tier_before=12, tier_after=13, rr_before=10,
                    rr_after=30, rr_earned=20)]

        def match_summary(self, match_id, puuid=None):
            seen["summary"].append(puuid)
            return api.MatchSummary(match_id=match_id, map_id="map1", character_id="ag1",
                                    won=True, kills=20, deaths=10, assists=3, score=5000,
                                    team_score=13, enemy_score=7, headshots=5, bodyshots=10,
                                    legshots=1)

        def match_details(self, match_id):
            seen["details"].append(match_id)
            return {"players": [
                {"subject": "SUB-PUUID", "teamId": "Red", "gameName": "Sub", "tagLine": "1",
                 "characterId": "ag1", "stats": {"score": 5000, "kills": 20, "deaths": 10,
                                                 "assists": 3}},
                {"subject": "x", "teamId": "Blue", "gameName": "E", "tagLine": "2",
                 "characterId": "ag1", "stats": {"score": 3000}}],
                "teams": [{"teamId": "Red", "won": True, "numPoints": 13},
                          {"teamId": "Blue", "won": False, "numPoints": 7}],
                "matchInfo": {"matchId": match_id, "mapId": "map1", "gameStartMillis": 1000},
                "roundResults": []}

    saved = (api.ValorantApi, svc.authenticate, svc._authenticate_via_local_api)
    api.ValorantApi = FakeClient
    svc.authenticate = own_auth
    svc._authenticate_via_local_api = borrow
    try:
        stats = svc.match_stats(sub)
        check("借りたトークンで本人の戦績を取れる", stats.games == 1 and stats.fetch_error == "")
        check("対象は本人の puuid", seen["history"] == ["SUB-PUUID"], str(seen["history"]))
        check("トークンの持ち主は別アカウント", seen["token_owner"] == "MAIN-PUUID")
        check("試合の集計も本人の puuid", seen["summary"] == ["SUB-PUUID"], str(seen["summary"]))
        check("JSON に保存される", svc.history.path_for(sub.id).is_file())

        detail = svc.match_detail(sub, "mS")
        check("試合詳細も借りて取れる", detail.match_id == "mS" and len(detail.my_team) == 1)
        check("自分の行が本人になる", detail.my_team[0].is_self and detail.my_team[0].riot_id == "Sub#1")
        check("試合詳細も JSON に保存される",
              "mS" in (svc.history.load(sub.id).get("details") or {}))
        seen["details"].clear()
        svc.match_detail(sub, "mS")
        check("保存済みなら通信しない", seen["details"] == [])

        # 誰もログインしていない: 保存済みは見られ、取得失敗として知らせる
        state["borrow"] = False
        offline = svc.match_stats(sub)
        check("借りられなくても保存済みを返す", offline.games == 1 and "起動していません" in offline.fetch_error,
              offline.fetch_error)

        # puuid が無いと誰の戦績か分からない
        anon = Account(label="anon", session_saved=True)
        vault.add(anon)
        state["borrow"] = True
        try:
            svc.match_stats(anon)
            check("puuid 不明は取得できない", False, "例外が出なかった")
        except ServiceError:
            check("puuid 不明は取得できない", True)
    finally:
        api.ValorantApi, svc.authenticate, svc._authenticate_via_local_api = saved


def test_match_extras() -> None:
    section("ACS・パーティー勝率・アイコン")
    from vam.riot.api import ValorantApi, AuthResult
    from vam.service import (
        MatchDetail, MatchListRow, MatchPlayerStat, _aggregate_rows,
    )

    # ACS = 総スコア ÷ ラウンド数 (総スコアそのものではない)
    row = MatchListRow(score=5734, team_score=13, enemy_score=9)
    check("ACS は総スコア÷ラウンド数", row.acs == 261 and row.rounds == 22, f"{row.acs}")
    check("ラウンド 0 でも落ちない", MatchListRow(score=100).acs == 0)
    check("ACS が現実的な範囲 (500 前後まで)", MatchListRow(score=9000, team_score=13, enemy_score=12).acs < 500)
    detail = MatchDetail(my_team_score=13, enemy_team_score=9,
                         my_team=[MatchPlayerStat(score=4622)])
    check("スコアボードの ACS", detail.acs_of(detail.my_team[0]) == 210, str(detail.acs_of(detail.my_team[0])))

    # パーティーの相手ごとの勝率
    def r(i, won, party):
        return MatchListRow(match_id=str(i), won=won, started_at=1000 - i, party=party)
    A = {"puuid": "A", "name": "Alice", "tag": "1"}
    B = {"puuid": "B", "name": "Bob", "tag": "2"}
    C = {"puuid": "C", "name": "", "tag": ""}
    rows = [r(1, True, [A]), r(2, False, [A, B]), r(3, True, [A]), r(4, True, []),
            r(5, False, [B]), r(6, True, None)]       # None = パーティー情報なし
    stats = _aggregate_rows(rows, "api")
    names = [(p.name, p.games, p.wins) for p in stats.party_stats]
    check("一緒に遊んだ多い順", names == [("Alice#1", 3, 2), ("Bob#2", 2, 0)], str(names))
    check("情報のある試合だけで集計", stats.party_known_games == 5, str(stats.party_known_games))
    check("勝率", round(stats.party_stats[0].win_rate) == 67)
    many = [r(i, True, [{"puuid": f"P{i % 7}", "name": f"P{i % 7}", "tag": "x"}]) for i in range(30)]
    check("最大 5 人まで", len(_aggregate_rows(many, "api").party_stats) == 5)
    check("名前が非公開でも puuid で数える",
          _aggregate_rows([r(1, True, [C]), r(2, True, [C])], "api").party_stats[0].games == 2)

    # 試合詳細から party を取り出す
    class Fake(ValorantApi):
        def match_details(self, match_id):
            return {"players": [
                {"subject": "me", "teamId": "Red", "partyId": "p1", "characterId": "x", "stats": {}},
                {"subject": "mate", "teamId": "Red", "partyId": "p1", "gameName": "Mate",
                 "tagLine": "7", "characterId": "x", "stats": {}},
                {"subject": "solo", "teamId": "Red", "partyId": "p2", "characterId": "x", "stats": {}},
                {"subject": "enemy", "teamId": "Blue", "partyId": "p1", "characterId": "x", "stats": {}}],
                "teams": [{"teamId": "Red", "won": True, "numPoints": 13},
                          {"teamId": "Blue", "won": False, "numPoints": 5}],
                "matchInfo": {"mapId": "m", "gameStartMillis": 1}, "roundResults": []}
    fake = Fake(AuthResult(access_token="t", puuid="me"))
    summary = fake.match_summary("mid", puuid="me")
    check("パーティーの味方だけが入る (自分・ソロ・敵は入らない)",
          [m["puuid"] for m in summary.party] == ["mate"], str(summary.party))
    check("名前とタグが入る", summary.party[0]["name"] == "Mate" and summary.party[0]["tag"] == "7")

    # アイコン: 未取得なら待ち、取れたら自動で入る
    import tempfile as _tf
    from PySide6.QtWidgets import QApplication, QLabel
    from PySide6.QtGui import QPixmap, QColor
    from vam.ui.icons import IconLoader
    QApplication.instance() or QApplication([])
    d = Path(_tf.mkdtemp())
    loader = IconLoader(d)
    pm = QPixmap(10, 10); pm.fill(QColor("red"))
    url = "https://example.invalid/a.png"
    path = loader._path_for(url)
    label = QLabel()
    loader._pending.add(url)          # 取得中の体 (通信はしない)
    loader._waiting.setdefault(url, []).append((label, 20))
    path.parent.mkdir(parents=True, exist_ok=True)
    pm.save(str(path), "PNG")
    loader.loaded.emit(url, str(path))
    check("取れたらラベルに絵が入る", label.pixmap() is not None and not label.pixmap().isNull())
    label2 = QLabel()
    loader.attach(label2, url, 20)
    check("取得済みならその場で入る", label2.pixmap() is not None and not label2.pixmap().isNull())
    loader.attach(QLabel(), None, 20)
    check("URL 無しは何もしない", True)


def test_custom_launch() -> None:
    section("起動コマンド (自己責任の上級者向け設定)")
    from vam.riot import launcher
    from vam.service import AccountService
    from vam.storage import Vault

    vault = Vault(Path(tempfile.mkdtemp()))
    vault.initialize()
    svc = AccountService(vault)
    check("既定は空", svc.custom_launch_path == "")
    svc.custom_launch_path = '  "C:/x/a.lnk"  '
    check("前後の空白と引用符を外して保存", svc.custom_launch_path == "C:/x/a.lnk", svc.custom_launch_path)
    check("再読込しても残る", AccountService(vault).custom_launch_path == "C:/x/a.lnk")

    d = Path(tempfile.mkdtemp())
    exe, bat, lnk = d / "a.exe", d / "b.bat", d / "c.lnk"
    for f in (exe, bat, lnk):
        f.write_text("", encoding="utf-8")
    popen, started = [], []
    saved = (launcher.subprocess.Popen, getattr(launcher.os, "startfile", None))
    launcher.subprocess.Popen = lambda args, **k: popen.append((args, k))
    launcher.os.startfile = lambda path: started.append(path)
    try:
        launcher.launch_custom(str(exe))
        check(".exe は直接起動", popen and popen[-1][0] == [str(exe)])
        launcher.launch_custom(f'"{bat}"')
        check(".bat は shell 経由 (引用符付きでも可)", popen[-1][1].get("shell") is True)
        launcher.launch_custom(str(lnk))
        check(".lnk は関連付けで開く", started == [str(lnk)], str(started))
        try:
            launcher.launch_custom(str(d / "none.lnk"))
            check("無いパスはエラー", False, "例外が出なかった")
        except launcher.LaunchError:
            check("無いパスはエラー", True)
    finally:
        launcher.subprocess.Popen = saved[0]
        if saved[1] is not None:
            launcher.os.startfile = saved[1]

    # 設定画面と、保存時の確認
    from PySide6.QtWidgets import QApplication, QMessageBox
    from vam.ui import main_window as mw
    from vam.ui.dialogs import LAUNCH_DISCLAIMER, SettingsDialog
    QApplication.instance() or QApplication([])
    dlg = SettingsDialog(webhook_url="", mention="", step_delay=0.5, stay_signed_in=False,
                         henrik_api_key="", current_version="0", vault=vault,
                         custom_launch_path="C:/x/a.lnk")
    check("設定画面に現在の値が入る", dlg.custom_launch_text() == "C:/x/a.lnk")
    check("警告文に自己責任・BANが書かれている",
          "自己責任" in LAUNCH_DISCLAIMER and "BAN" in LAUNCH_DISCLAIMER)

    class Fake:
        status = type("S", (), {"showMessage": lambda self, *a, **k: None})()
        service = svc
    asked = []
    saved_warn = QMessageBox.warning
    QMessageBox.warning = staticmethod(lambda *a, **k: asked.append(a[2]) or QMessageBox.No)
    try:
        mw.MainWindow._save_custom_launch(Fake, "C:/new/b.lnk")
        check("新しく指定するときは確認が出る", len(asked) == 1 and "自己責任" in asked[0])
        check("断れば保存されない", svc.custom_launch_path == "C:/x/a.lnk")
        QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Yes)
        mw.MainWindow._save_custom_launch(Fake, "C:/new/b.lnk")
        check("了承すれば保存される", svc.custom_launch_path == "C:/new/b.lnk")
        asked.clear()
        QMessageBox.warning = staticmethod(lambda *a, **k: asked.append(1) or QMessageBox.Yes)
        mw.MainWindow._save_custom_launch(Fake, "C:/new/b.lnk")
        check("変えていなければ確認しない", asked == [])
        mw.MainWindow._save_custom_launch(Fake, "")
        check("空にするのは確認なしで解除できる", svc.custom_launch_path == "" and asked == [])
    finally:
        QMessageBox.warning = saved_warn


def _fb_write_table(buf: bytearray, spec: dict) -> int:
    """テスト用の最小 FlatBuffers 書き出し。spec は {フィールド番号: (種類, 値)}。
    種類: Q(u64) I(u32) B(u8) str  tables(子テーブルの配列)  u64s(u64 の配列)。"""
    import struct
    n = max(spec) + 1
    sizes = {"Q": 8, "I": 4, "B": 1, "str": 4, "tables": 4, "u64s": 4}
    offsets, pos = [0] * n, 4
    for idx in sorted(spec):
        size = sizes[spec[idx][0]]
        pos = (pos + size - 1) // size * size
        offsets[idx] = pos
        pos += size
    obj_size = pos
    vt_pos = len(buf)
    buf += struct.pack("<HH", 4 + 2 * n, obj_size) + b"".join(struct.pack("<H", o) for o in offsets)
    if len(buf) % 4:
        buf += bytes(4 - len(buf) % 4)
    table_pos = len(buf)
    buf += struct.pack("<i", table_pos - vt_pos) + bytes(obj_size - 4)
    pending = []
    for idx, (kind, value) in spec.items():
        fpos = table_pos + offsets[idx]
        if kind == "Q":
            struct.pack_into("<Q", buf, fpos, value)
        elif kind == "I":
            struct.pack_into("<I", buf, fpos, value)
        elif kind == "B":
            struct.pack_into("<B", buf, fpos, value)
        else:
            pending.append((fpos, kind, value))
    for fpos, kind, value in pending:
        while len(buf) % 4:
            buf += b"\0"
        child = len(buf)
        if kind == "str":
            data = value.encode("utf-8")
            buf += struct.pack("<I", len(data)) + data + b"\0"
        elif kind == "u64s":
            buf += struct.pack("<I", len(value)) + b"".join(struct.pack("<Q", v) for v in value)
        else:
            buf += struct.pack("<I", len(value)) + bytes(4 * len(value))
            for i, sub in enumerate(value):
                while len(buf) % 4:
                    buf += b"\0"
                sub_pos = _fb_write_table(buf, sub)
                struct.pack_into("<I", buf, child + 4 + 4 * i, sub_pos - (child + 4 + 4 * i))
        struct.pack_into("<I", buf, fpos, child - fpos)
    return table_pos


def test_langpack() -> None:
    section("言語ファイルの差し替え (音声とテキストの言語を別々に)")
    import hashlib
    import struct
    import threading
    import zstandard
    from vam.riot import langpack as lp
    from vam.service import AccountService, ServiceError
    from vam.storage import Vault

    # ---- 合成した RMAN マニフェストを読めるか / 取得できるか
    comp = zstandard.ZstdCompressor()
    ja_text = bytes(range(256)) * 40 + b"JAPANESE-TEXT"
    ja_sig = b"sig-ja" * 7
    en_text = b"ENGLISH-TEXT" * 900
    en_sig = b"sig-en" * 7

    def sha_id(data: bytes) -> int:
        return struct.unpack("<Q", hashlib.sha256(data).digest()[:8])[0]

    # ja の本文は 2 チャンクに分ける
    pieces = {"ja_pak": [ja_text[:6000], ja_text[6000:]], "ja_sig": [ja_sig],
              "en_pak": [en_text], "en_sig": [en_sig]}
    bundle_chunks, bundle = {}, bytearray()
    for key, chunk_list in pieces.items():
        bundle_chunks[key] = []
        for piece in chunk_list:
            blob = comp.compress(piece)
            bundle_chunks[key].append((sha_id(piece), len(blob), len(piece), len(bundle)))
            bundle += blob
    BUNDLE_ID = 0xABCDEF0123456789
    chunk_specs = []
    for key in pieces:
        for cid, c, u, _off in bundle_chunks[key]:
            chunk_specs.append({0: ("Q", cid), 1: ("I", c), 2: ("I", u)})
    names = {"ja_pak": "ja_JP_Text-WindowsClient.pak", "ja_sig": "ja_JP_Text-WindowsClient.sig",
             "en_pak": "en_US_Text-WindowsClient.pak", "en_sig": "en_US_Text-WindowsClient.sig"}
    sizes = {k: sum(len(x) for x in v) for k, v in pieces.items()}
    entries = []
    for i, key in enumerate(pieces):
        entries.append({0: ("Q", 100 + i), 1: ("Q", 12), 2: ("Q", sizes[key]),
                        3: ("str", names[key]), 7: ("u64s", [c[0] for c in bundle_chunks[key]]),
                        11: ("B", 0)})
    root_spec = {
        0: ("tables", [{0: ("Q", BUNDLE_ID), 1: ("tables", chunk_specs)}]),
        2: ("tables", entries),
        3: ("tables", [{0: ("Q", 10), 2: ("str", "ShooterGame")},
                       {0: ("Q", 11), 1: ("Q", 10), 2: ("str", "Content")},
                       {0: ("Q", 12), 1: ("Q", 11), 2: ("str", "Paks")}]),
        5: ("tables", [{1: ("B", lp.HASH_SHA256)}]),
    }
    body = bytearray(4)
    struct.pack_into("<I", body, 0, _fb_write_table(body, root_spec))
    half = len(body) // 2   # 本文を 2 つの zstd フレームに分けて入れる (実物と同じ形)
    packed = comp.compress(bytes(body[:half])) + comp.compress(bytes(body[half:]))
    manifest = (b"RMAN" + bytes([2, 1, 0, 2]) + struct.pack("<IIQI", 28, len(packed), 0x1234, len(body))
                + packed + bytes(256))
    files = lp.parse_manifest(manifest)
    key = "shootergame/content/paks/ja_jp_text-windowsclient.pak"
    check("ファイル名がディレクトリから組み立てられる", key in files, str(list(files)))
    check("サイズとチャンク数", files[key].size == len(ja_text) and len(files[key].chunks) == 2)
    check("ハッシュ種類を読む", files[key].hash_type == lp.HASH_SHA256)
    try:
        lp.parse_manifest(b"XXXX" + manifest[4:])
        check("RMAN 以外は拒否", False, "例外が出なかった")
    except lp.LangPackError:
        check("RMAN 以外は拒否", True)

    # 取得 (Range 取得を偽サーバーで再現)
    ranges = []

    class FakeResp:
        def __init__(self, content):
            self.content = content

    def fake_get(url, headers=None, timeout=30.0):
        assert url.endswith(f"{BUNDLE_ID:016X}.bundle"), url
        a, b = headers["Range"].split("=")[1].split("-")
        ranges.append((int(a), int(b)))
        return FakeResp(bytes(bundle[int(a):int(b) + 1]))

    store = Path(tempfile.mkdtemp())
    saved_get = lp._get
    lp._get = fake_get
    try:
        lp.download_file(files[key], store / "ja.pak")
        check("取得した内容が一致する", (store / "ja.pak").read_bytes() == ja_text)
        check("隣り合うチャンクは 1 回の Range にまとまる", len(ranges) == 1, str(ranges))
        bundle[bundle_chunks["en_pak"][0][3] + 5] ^= 0xFF   # 壊れたチャンク
        try:
            lp.download_file(files["shootergame/content/paks/en_us_text-windowsclient.pak"],
                             store / "en.pak")
            check("壊れたチャンクは拒否", False, "例外が出なかった")
        except (lp.LangPackError, zstandard.ZstdError):
            check("壊れたチャンクは拒否", True)
        check("壊れたら書き出さない", not (store / "en.pak").exists())
    finally:
        lp._get = saved_get

    # ---- 差し替え
    paks = Path(tempfile.mkdtemp())
    store2 = Path(tempfile.mkdtemp())
    for loc, (pak, sig) in {"en_US": (b"EN-PAK", b"EN-SIG"), "ja_JP": (b"JA-PAK", b"JA-SIG")}.items():
        (store2 / f"{loc}_Text-WindowsClient.pak").write_bytes(pak)
        (store2 / f"{loc}_Text-WindowsClient.sig").write_bytes(sig)
    (paks / "en_US_Text-WindowsClient.pak").write_bytes(b"DIRTY")
    (paks / "en_US_Text-WindowsClient.sig").write_bytes(b"DIRTY")
    (paks / "en_US_Audio-WindowsClient.pak").write_bytes(b"AUDIO")
    check("voice の言語が入っているか判定", lp.voice_installed(paks, "en_US") and not lp.voice_installed(paks, "ja_JP"))

    saved = (lp.find_paks_dir, lp.ensure_packs)
    lp.find_paks_dir = lambda: paks
    lp.ensure_packs = lambda *a, **k: "url"
    try:
        got = lp.prepare_launch(store2, "en_US", "ja_JP")
        check("起動前に voice のテキストを元に戻す",
              got == paks and (paks / "en_US_Text-WindowsClient.pak").read_bytes() == b"EN-PAK")
        try:
            lp.prepare_launch(store2, "ja_JP", "en_US")
            check("ゲーム言語が voice でなければ案内して止める", False, "例外が出なかった")
        except lp.LangPackError as exc:
            check("ゲーム言語が voice でなければ案内して止める", "ゲーム言語" in str(exc), str(exc))

        # ゲームの起動を待って差し替える
        state = {"n": 0}

        def running():
            state["n"] += 1
            return state["n"] >= 4

        ok = lp.swap_when_game_starts(store2, paks, "en_US", "ja_JP", poll=0.001, is_running=running)
        check("起動を検知したら text で上書きする",
              ok and (paks / "en_US_Text-WindowsClient.pak").read_bytes() == b"JA-PAK"
              and (paks / "en_US_Text-WindowsClient.sig").read_bytes() == b"JA-SIG")
        check("音声ファイルには触らない", (paks / "en_US_Audio-WindowsClient.pak").read_bytes() == b"AUDIO")
        lp.install_text(store2, paks, "en_US", "en_US")
        ev = threading.Event()
        ev.set()
        check("キャンセルなら差し替えない",
              lp.swap_when_game_starts(store2, paks, "en_US", "ja_JP", cancel_event=ev,
                                       poll=0.001, is_running=lambda: True) is False
              and (paks / "en_US_Text-WindowsClient.pak").read_bytes() == b"EN-PAK")
        check("起動しなければタイムアウトで差し替えない",
              lp.swap_when_game_starts(store2, paks, "en_US", "ja_JP", timeout=0.05, poll=0.01,
                                       is_running=lambda: False) is False)
        lp.install_text(store2, paks, "ja_JP", "en_US")
        lp.restore_original(store2, "en_US")
        check("元に戻せる", (paks / "en_US_Text-WindowsClient.pak").read_bytes() == b"EN-PAK")
    finally:
        lp.find_paks_dir, lp.ensure_packs = saved

    # ---- 設定とサービス
    vault = Vault(Path(tempfile.mkdtemp()))
    vault.initialize()
    svc = AccountService(vault)
    check("既定は何もしない", svc.language_split() is None)
    svc.set_languages("en_US", "")
    check("片方だけなら無効", svc.language_split() is None)
    svc.set_languages("en_US", "en_US")
    check("同じ言語なら無効", svc.language_split() is None)
    svc.set_languages("en_US", "ja_JP")
    check("音声と文字が違えば有効", svc.language_split() == ("en_US", "ja_JP"))
    check("設定は保存される", AccountService(vault).language_split() == ("en_US", "ja_JP"))

    warnings: list[str] = []
    check("モック環境では何もしない", svc._prepare_language_swap(lambda m: None, warnings) is None)
    from vam.riot import process as _process
    saved_mock, saved_prepare = _process.mock_mode, lp.prepare_launch
    _process.mock_mode = lambda: False
    try:
        lp.prepare_launch = lambda *a, **k: paks
        plan = svc._prepare_language_swap(lambda m: None, warnings)
        check("準備できたら (paks, 音声, テキスト) を返す", plan == (paks, "en_US", "ja_JP"), str(plan))

        def boom(*a, **k):
            raise lp.LangPackError("ゲーム言語を en_US にしてください")
        lp.prepare_launch = boom
        plan = svc._prepare_language_swap(lambda m: None, warnings)
        check("準備に失敗したら警告して通常どおり起動", plan is None and "スキップ" in warnings[-1], str(warnings))
    finally:
        _process.mock_mode, lp.prepare_launch = saved_mock, saved_prepare
    try:
        AccountService(Vault(Path(tempfile.mkdtemp()))).prepare_language_packs()
        check("言語未設定では準備できない", False, "例外が出なかった")
    except ServiceError:
        check("言語未設定では準備できない", True)

    # ---- 設定画面と確認ダイアログ
    from PySide6.QtWidgets import QApplication, QMessageBox
    from vam.ui import main_window as mw
    from vam.ui.dialogs import LANGUAGE_DISCLAIMER, SettingsDialog
    QApplication.instance() or QApplication([])
    dlg = SettingsDialog(webhook_url="", mention="", step_delay=0.5, stay_signed_in=False,
                         henrik_api_key="", current_version="0", vault=vault,
                         lang_voice="en_US", lang_text="ja_JP", locales=["en_US", "ja_JP", "ko_KR"])
    check("設定画面に現在の言語が入る", dlg.language_voice() == "en_US" and dlg.language_text() == "ja_JP")
    check("「変更しない」を選べる", dlg.lang_voice.itemData(0) == "")
    check("警告に自己責任・BAN が書かれている", "自己責任" in LANGUAGE_DISCLAIMER and "BAN" in LANGUAGE_DISCLAIMER)

    class Fake:
        status = type("S", (), {"showMessage": lambda self, *a, **k: None})()
        service = svc
    asked = []
    saved_warn = QMessageBox.warning
    QMessageBox.warning = staticmethod(lambda *a, **k: asked.append(a[2]) or QMessageBox.No)
    try:
        mw.MainWindow._save_languages(Fake, "en_US", "ko_KR")
        check("新しく有効にするときは確認が出る", len(asked) == 1 and "自己責任" in asked[0])
        check("断れば変わらない", svc.language_split() == ("en_US", "ja_JP"))
        QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Yes)
        mw.MainWindow._save_languages(Fake, "en_US", "ko_KR")
        check("了承すれば保存される", svc.language_split() == ("en_US", "ko_KR"))
        asked.clear()
        QMessageBox.warning = staticmethod(lambda *a, **k: asked.append(1) or QMessageBox.Yes)
        mw.MainWindow._save_languages(Fake, "", "")
        check("無効にするのは確認なし", svc.language_split() is None and asked == [])
    finally:
        QMessageBox.warning = saved_warn


def test_status_notify() -> None:
    section("障害通知")
    from vam.models import Account
    from vam.riot import status
    from vam.service import AccountService
    from vam.storage import Vault

    # diff(): 新規発生・解消の判定
    prev = status.StatusSnapshot(incidents={"1": {"id": 1, "incident_severity": "critical"}})
    cur = status.StatusSnapshot(incidents={
        "1": {"id": 1, "incident_severity": "critical"},
        "2": {"id": 2, "incident_severity": "info"},
    })
    events = status.diff(prev, cur)
    check("新規発生を検知", len(events) == 1 and events[0].id == "2" and events[0].action == "started")

    prev2 = status.StatusSnapshot(incidents={"1": {"id": 1, "incident_severity": "critical"}})
    cur2 = status.StatusSnapshot()
    events2 = status.diff(prev2, cur2)
    check("解消を検知", len(events2) == 1 and events2[0].action == "resolved")

    # 同じ前回/今回なら何も検知しない (重複通知の防止の土台)
    events3 = status.diff(prev, prev)
    check("差分が無ければ何も検知しない", events3 == [])

    # メンテナンスは予定/実施中を開始時刻で判定する (Riot の status は
    # 予定の告知時点から in_progress になるため当てにならない)
    import datetime as _dt
    start = _dt.datetime(2026, 10, 14, 6, 0, tzinfo=status.JST).timestamp()
    maint = {"id": 1, "maintenance_status": "in_progress",
             "titles": [{"locale": "ja_JP", "content": "メンテナンス：2026/10/14 06:00 JST"}]}
    check("開始前は予定扱い", status.is_upcoming(maint, start - 3600))
    check("開始後は実施中扱い", not status.is_upcoming(maint, start + 60))
    snap_m = status.StatusSnapshot(maintenances={"1": maint})
    banner = status.summarize(snap_m, now=start - 3600)
    check("バナーは「予定」", banner.startswith("メンテナンス予定:") and "メンテナンス中" not in banner, banner)
    check("バナー: 開始後は「中」", status.summarize(snap_m, now=start + 60).startswith("メンテナンス中:"))
    ev = status.diff(status.StatusSnapshot(), snap_m, now=start - 3600)
    check("予定の発表を検知", len(ev) == 1 and ev[0].action == "scheduled")
    before = status.StatusSnapshot(maintenances={"1": maint}, taken_at=start - 3600)
    ev2 = status.diff(before, snap_m, now=start + 60)
    check("予定→開始の遷移を検知", len(ev2) == 1 and ev2[0].action == "started")
    ev3 = status.diff(before, snap_m, now=start - 60)
    check("開始前は遷移しない", ev3 == [])
    no_time = {"id": 2, "maintenance_status": "scheduled", "titles": []}
    check("時刻が読めなければ status に従う", status.is_upcoming(no_time))

    # check_status(): critical だけメンション付きで通知する
    vault = Vault(Path(tempfile.mkdtemp()))
    vault.initialize()
    svc = AccountService(vault)
    svc.discord_webhook_url = "https://discord.example/webhook"
    svc.discord_mention = "@everyone"

    state_path = vault.app_dir / "status_state.json"
    status.save_snapshot(state_path, status.StatusSnapshot())

    calls = []
    original_notify = status.notify_discord
    original_fetch = status.fetch
    status.notify_discord = lambda webhook_url, event, timeout=10.0, mention="": calls.append(
        (event.severity, mention)
    )
    status.fetch = lambda region="ap", timeout=10.0: status.StatusSnapshot(incidents={
        "10": {"id": 10, "incident_severity": "critical", "titles": []},
        "11": {"id": 11, "incident_severity": "info", "titles": []},
        "12": {"id": 12, "incident_severity": "warning", "titles": []},
    })
    try:
        svc.check_status()
    finally:
        status.notify_discord = original_notify
        status.fetch = original_fetch

    by_severity = {sev: mention for sev, mention in calls}
    check("3件とも通知される", len(calls) == 3, str(calls))
    check("criticalはメンション付き", by_severity.get("critical") == "@everyone", str(by_severity))
    check("infoはメンション無し", by_severity.get("info") == "")
    check("warningもメンション無し", by_severity.get("warning") == "")


def test_ui() -> None:
    section("UI の構築")
    from PySide6.QtWidgets import QApplication, QDialog
    from vam.mock.demo_data import FIRST_PUUID, seed
    from vam.mock.fake_riot import FakeRiotEnv
    from vam.storage import Vault
    from vam.ui.dialogs import (
        AccountDialog, SettingsDialog, SetPasswordDialog, SetupDialog, UnlockDialog,
    )
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

        # 並べ替え: ドロップで手動順になり、順が保存される。ランク順は自動で並ぶ
        order = lambda: window._card_order()
        first, second, third, fourth = order()
        nfav = sum(1 for i in order() if window.vault.get(i).favorite)
        window._on_card_dropped(fourth, 0)      # 末尾のカードを先頭へ
        check("お気に入りより上には行かない", order()[nfav] == fourth and
              all(window.vault.get(i).favorite for i in order()[:nfav]), str(order()))
        check("ドロップで手動順に切り替わる", window.service.sort_mode == "manual")
        check("並びが保存される", [a.id for a in window.service.sorted_accounts()] == order())
        window._on_card_dropped(fourth, 4)      # 末尾へ
        check("末尾へ移せる", order()[-1] == fourth, str(order()))
        before = order()
        window._on_card_dropped(second, 99)
        check("範囲外は末尾", order()[-1] == second and len(order()) == 4, str(order()))
        window.set_sort_mode("rank")
        ranked = [window.vault.get(i).rank.tier for i in order()]
        check("ランク順 (高い順)", ranked == sorted(ranked, reverse=True), str(ranked))
        window.service.sort_mode = "recent"

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

        # マスターパスワードの付け外し UI
        v4 = Vault(Path(tempfile.mkdtemp()))
        v4.initialize()  # パスワード無しで開始
        settings1 = SettingsDialog(
            webhook_url="", mention="", step_delay=0.5, stay_signed_in=False,
            henrik_api_key="", current_version="0.0.0", vault=v4,
        )
        check("パスワード無しなら「設定する」",
              settings1.password_button.text() == "設定する")
        check("状態表示もパスワード無し",
              "パスワードなし" in settings1.password_state_label.text())

        # 使い方ノート
        from vam.ui.guide import SECTIONS, GuideDialog, build_html
        check("設定に使い方ノートのボタンがある", settings1.guide_button.text() == "使い方ノート")
        guide = GuideDialog()
        check("目次と節の数が一致", guide.toc.count() == len(SECTIONS) and len(SECTIONS) >= 10)
        html = build_html()
        check("全ての節にアンカーがある", all(f'name="{a}"' in html for a, _, _ in SECTIONS))
        check("アンカー名が重複しない", len({a for a, _, _ in SECTIONS}) == len(SECTIONS))
        check("主要機能が載っている",
              all(w in html for w in ("すべて更新", "並び順", "戦績", "セッション", "メンテナンス")))
        guide.toc.setCurrentRow(3)
        check("目次を選んでも落ちない", guide.toc.currentRow() == 3)

        set_pw = SetPasswordDialog()
        set_pw.password.setText("ab")
        set_pw.accept()
        check("4文字未満は拒否", not set_pw.error.isHidden())
        set_pw.error.hide()
        set_pw.password.setText("new-master-pw")
        set_pw.confirm.setText("違う値")
        set_pw.accept()
        check("確認が不一致なら拒否", not set_pw.error.isHidden())
        set_pw.confirm.setText("new-master-pw")
        check("一致すれば通る", set_pw.new_password() == "new-master-pw")

        v4.change_password("new-master-pw")
        settings2 = SettingsDialog(
            webhook_url="", mention="", step_delay=0.5, stay_signed_in=False,
            henrik_api_key="", current_version="0.0.0", vault=v4,
        )
        check("設定後は「外す」", settings2.password_button.text() == "外す")
        check("状態表示も更新", "設定済み" in settings2.password_state_label.text())

        window.close()


def main() -> int:
    print("Kunai — 通しテスト")
    for fn in (test_crypto, test_storage, test_session, test_localapi,
               test_service, test_real_file_format, test_process_isolation,
               test_autologin_geometry,
               test_session_renewal, test_api_parsing, test_auth_helpers,
               test_content, test_inventory_summary, test_match_cache,
               test_match_stats_api_mode, test_history_persistence, test_bulk_refresh, test_refresh_cycle, test_sort_modes, test_act_status, test_browser_open, test_borrowed_history, test_match_extras, test_custom_launch, test_langpack,
               test_status_notify, test_ui):
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
