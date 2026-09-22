"""アプリの中核。保管庫と Riot 連携層をつなぐ。

UI からはここだけを呼ぶ。進捗はコールバックで文字列を流す。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable

from . import diagnostics
from .models import Account, InventoryInfo, RankInfo, WalletInfo
from .storage import Vault
from . import paths
from .riot import api, auth, autologin, content, launcher, localapi, process, session, status

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    pass


class ServiceError(Exception):
    pass


class GameRunning(ServiceError):
    """VALORANT が起動中。切り替えるなら落とす必要がある。"""


@dataclass
class SwitchResult:
    account_id: str
    method: str            # "session" か "autologin"
    launched: bool
    warnings: list[str]


# 設定キー。settings.json に平文で置く (機密ではない)
SETTING_STAY_SIGNED_IN = "stay_signed_in"
SETTING_DISCORD_WEBHOOK = "discord_webhook_url"


class AccountService:
    def __init__(self, vault: Vault):
        self.vault = vault
        self.content = content.ContentCache(vault.app_dir / "cache")

    @property
    def stay_signed_in(self) -> bool:
        """自動ログイン時に「サインイン状態を維持」をアプリが操作するか。

        既定は False。ログインは ユーザー名 → Tab → パスワード → Enter だけに
        する。チェックボックスは Riot 側が前回の状態を覚えているので、
        一度入れておけば維持される。アプリが操作しようとすると Tab 走査と
        フォーカスの巻き戻しが要り、挙動が読みにくくなる。

        なお、このチェックが入っていないと Riot はセッションを保存しないため、
        次回もパスワード入力になる。有効にしたい場合は settings.json に
        {"stay_signed_in": true} を書く。
        """
        return bool(self.vault.settings().get(SETTING_STAY_SIGNED_IN, False))

    @stay_signed_in.setter
    def stay_signed_in(self, value: bool) -> None:
        settings = self.vault.settings()
        settings[SETTING_STAY_SIGNED_IN] = bool(value)
        self.vault.save_settings(settings)

    @property
    def discord_webhook_url(self) -> str:
        return str(self.vault.settings().get(SETTING_DISCORD_WEBHOOK, ""))

    @discord_webhook_url.setter
    def discord_webhook_url(self, value: str) -> None:
        settings = self.vault.settings()
        settings[SETTING_DISCORD_WEBHOOK] = value.strip()
        self.vault.save_settings(settings)

    # -- VALORANT のメンテナンス・障害ステータス ----------------------------
    def check_status(self, progress: Progress = _noop) -> tuple[bool, str]:
        """状態を取得し、前回との差分を Discord に通知する。

        返り値は (アクティブな案件があるか, バナー用の要約文)。
        初回実行時は「今すでに起きていること」を通知しない
        (起動のたびに既知の案件をスパム通知しないため)。
        """
        state_path = self.vault.app_dir / "status_state.json"
        previous = status.load_snapshot(state_path)
        current = status.fetch()

        if previous is not None:
            webhook = self.discord_webhook_url
            for event in status.diff(previous, current):
                if webhook:
                    try:
                        status.notify_discord(webhook, event)
                    except status.StatusError as exc:
                        progress(str(exc))

        status.save_snapshot(state_path, current)
        return current.active, status.summarize(current)

    # -- 環境 ---------------------------------------------------------------
    def environment(self) -> paths.Environment:
        return paths.detect()

    def running_processes(self) -> list[str]:
        return process.running()

    # -- 現在ログイン中のアカウント ----------------------------------------
    def current_session_info(self) -> session.SessionInfo:
        return session.current_session_info()

    def current_account(self) -> Account | None:
        """今 Riot Client に入っているセッションが、保管庫のどれかを puuid で照合する。"""
        info = self.current_session_info()
        if not info.puuid:
            return None
        for acc in self.vault.accounts():
            if acc.puuid and acc.puuid == info.puuid:
                return acc
        return None

    # -- セッションの取り込み ----------------------------------------------
    def capture_into(self, account: Account, progress: Progress = _noop) -> Account:
        """今ログインしている状態を、指定アカウントのセッションとして保存する。"""
        progress("現在のセッションを読み取り中…")
        blobs = session.capture()
        info = session.inspect_blobs(blobs)
        if not info.valid:
            raise ServiceError(self._no_session_reason())
        if info.expired:
            raise ServiceError("このセッションは既に失効しています")

        self.vault.clear_session(account.id)
        for rel, data in blobs.items():
            self.vault.write_session_blob(account.id, rel, data)

        account.session_saved = True
        account.session_saved_at = time.time()
        account.session_rejected = False
        if info.puuid:
            account.puuid = info.puuid
        self.vault.update(account)
        progress(f"{account.display_name} のセッションを保存しました")
        return account

    def _no_session_reason(self) -> str:
        """セッションが保存されていない理由を、できるだけ具体的に説明する。

        ローカル API ではログインが見えているのにセッションが無い、という
        状態がありうる。「サインイン状態を維持」を入れずにログインした場合で、
        このときクライアントは refresh_token をディスクに残さない。
        単に「見つかりません」と言われても原因が分からないので切り分ける。
        """
        client = localapi.LocalClient()
        if client.available:
            try:
                live = client.session()
            except localapi.LocalApiError:
                live = None
            if live and live.puuid:
                who = live.riot_id or live.puuid[:8]
                return (
                    f"{who} でログインしていますが、セッションが保存されていません。\n"
                    "ログイン時に「サインイン状態を維持」を有効にしていないと、"
                    "Riot Client はログイン状態をディスクに残しません。\n"
                    "一度サインアウトし、「サインイン状態を維持」にチェックを入れて"
                    "ログインし直してから、もう一度取り込んでください。"
                )
        return (
            "ログイン中のアカウントが見つかりません。\n"
            "Riot Client でログインし、そのとき「サインイン状態を維持」を"
            "有効にしてください。"
        )

    @staticmethod
    def _missing_credentials_reason(account: Account, outcome: str) -> str:
        """自動ログインに回せなかった理由を、何が足りないかまで書く。

        「登録してください」だけだと、片方だけ入っている場合に
        「登録したのに何も起きない」と見える。
        """
        head = ("保存されていたセッションは Riot 側で失効していました。"
                if outcome == "rejected" else
                "ログイン状態を確認できませんでした。")
        if not account.username and not account.password:
            missing = "ユーザー名とパスワードが未登録です"
        elif not account.password:
            missing = "パスワードが未登録です（ユーザー名だけでは自動ログインできません）"
        elif not account.username:
            missing = "ユーザー名が未登録です（パスワードだけでは自動ログインできません）"
        else:
            missing = "自動ログインが無効です"
        return (f"{head}\n{missing}。\n"
                "「···」→「編集」から登録するか、"
                "このアカウントでログインし直して取り込み直してください。")

    def import_current(self, label: str = "", progress: Progress = _noop) -> Account:
        """今ログイン中のアカウントを新規登録する。"""
        info = self.current_session_info()
        if not info.valid:
            raise ServiceError(self._no_session_reason())

        for acc in self.vault.accounts():
            if acc.puuid and acc.puuid == info.puuid:
                raise ServiceError(f"このアカウントは「{acc.display_name}」として登録済みです")

        account = Account(label=label or "取り込んだアカウント", puuid=info.puuid)

        # 現行のセッションは id_token に Riot ID を持っている。
        # クライアントが起動していなくてもここから取れる。
        if info.riot_id:
            account.riot_id = info.riot_id
            if not label:
                account.label = info.riot_id.split("#", 1)[0]

        # クライアントが起動中なら region も取れる
        client = localapi.LocalClient()
        if client.available:
            try:
                s = client.session()
                if s.riot_id:
                    account.riot_id = s.riot_id
                if s.region:
                    account.region = s.region
                if not label and s.game_name:
                    account.label = s.game_name
            except localapi.LocalApiError:
                pass

        self.vault.add(account)
        return self.capture_into(account, progress)

    def session_status(self, account: Account) -> session.SessionInfo:
        return session.inspect_blobs(self.vault.read_session_blobs(account.id))

    # -- 保存済みセッションの追従 ------------------------------------------
    def sync_current_session(self) -> Account | None:
        """今ログイン中のアカウントの保存済みセッションを最新に保つ。

        refresh_token は使うたびにローテーションする。クライアントが起動して
        トークンを更新すると、保管庫に持っている古いコピーはその時点で失効する。
        そのまま次の切り替えで書き戻しても Riot に拒否され、クライアントは
        セッションファイルを消してログイン画面に戻る。

        なので「今ディスクにあるもの」と「保管庫にあるもの」が食い違ったら、
        黙って追従させる。これを怠ると、1 回使ったアカウントには
        二度と切り替えられなくなる。

        更新したアカウントを返す。何もしなければ None。
        """
        try:
            blobs = session.capture()
        except session.SessionError:
            return None

        info = session.inspect_blobs(blobs)
        if not info.valid or not info.puuid:
            return None

        account = next((a for a in self.vault.accounts()
                        if a.puuid and a.puuid == info.puuid), None)
        if account is None:
            return None

        if self.vault.read_session_blobs(account.id) == blobs:
            return None                      # 既に最新

        self.vault.clear_session(account.id)
        for rel, data in blobs.items():
            self.vault.write_session_blob(account.id, rel, data)
        account.session_saved = True
        account.session_saved_at = time.time()
        self.vault.update(account)
        return account

    def wait_for_login(self, account: Account, timeout: float = 90.0,
                       progress: Progress = _noop) -> str:
        """復元したセッションが通るかを見届ける。

        返り値は "ok" / "rejected" / "timeout"。

        成功を待つだけだと、失効していた場合に必ずタイムアウト分だけ
        待たされる。クライアントはトークンを拒否するとセッションファイルを
        消すので、それを監視すれば失敗は数十秒で分かる。
        """
        if process.mock_mode():
            return "ok"

        progress(f"{account.display_name}: ログインを確認しています…")
        deadline = time.time() + timeout
        gone = 0
        while time.time() < deadline:
            client = localapi.LocalClient()
            if client.available:
                try:
                    live = client.session()
                except localapi.LocalApiError:
                    live = None
                if live and live.puuid == account.puuid:
                    return "ok"

            # 拒否されるとクライアントがセッションファイルを消す。
            # 書き換え途中の一瞬を拾わないよう、続けて空だったときだけ確定する。
            gone = gone + 1 if not session.current_session_info().valid else 0
            if gone >= 3:
                return "rejected"

            time.sleep(1.5)
        return "timeout"

    def wait_and_capture(self, account: Account, timeout: float = 60.0,
                         progress: Progress = _noop) -> bool:
        """切り替え後、クライアントがログインし終えてから取り込み直す。

        起動時にトークンが更新されるので、そのあとの状態を保存しないと
        保管庫のコピーが一世代古いままになる。
        """
        if process.mock_mode():
            # モック環境には待つ相手がいない。復元済みの内容をそのまま取り込む
            try:
                self.capture_into(account)
                return True
            except ServiceError:
                return False

        deadline = time.time() + timeout
        progress(f"{account.display_name}: ログイン完了を待っています…")
        while time.time() < deadline:
            client = localapi.LocalClient()
            if client.available:
                try:
                    live = client.session()
                except localapi.LocalApiError:
                    live = None
                if live and live.puuid == account.puuid:
                    # 書き込みが落ち着くまで少し待ってから取り込む
                    time.sleep(3)
                    try:
                        self.capture_into(account)
                        progress(f"{account.display_name}: セッションを最新に更新しました")
                        return True
                    except ServiceError:
                        return False
            time.sleep(2)
        progress(f"{account.display_name}: ログイン完了を確認できませんでした")
        return False

    # -- 切り替え -----------------------------------------------------------
    def switch(self, account: Account, launch_game: bool = True,
               force: bool = False, allow_autologin: bool = True,
               submit_login: bool = True, recapture: bool = True,
               progress: Progress = _noop) -> SwitchResult:
        # 進捗はログにも残す。固まったときにどこまで来たか分かるように。
        _ui_progress = progress

        def progress(message: str) -> None:
            diagnostics.log(f"switch({account.display_name}): {message}")
            _ui_progress(message)

        """指定アカウントに切り替える。

        セッションが保存されていればそれを書き戻す (パスワード不要)。
        無ければ、パスワードが登録されていれば自動入力にフォールバックする。
        """
        env = self.environment()
        if not env.installed:
            raise ServiceError(
                "Riot Client が見つかりません。VALORANT をインストールするか、"
                "設定で RiotClientServices.exe の場所を指定してください。"
            )

        if process.game_running() and not force:
            raise GameRunning("VALORANT が起動中です。終了してから切り替えてください。")

        warnings: list[str] = []
        blobs = self.vault.read_session_blobs(account.id)
        info = session.inspect_blobs(blobs) if blobs else session.SessionInfo()

        method = "session"
        can_autologin = allow_autologin and account.username and account.password
        if account.session_rejected and can_autologin:
            # 前回この保存セッションは Riot に拒否された。試すだけ時間の無駄。
            progress("前回失効していたセッションなので、自動ログインで入ります")
            method = "autologin"
        elif not info.valid or info.expired:
            if info.expired:
                warnings.append("保存されたセッションが失効していました")
            if can_autologin:
                method = "autologin"
            else:
                raise ServiceError(
                    "このアカウントには有効なセッションがありません。"
                    "一度ログインして「セッションを保存」するか、"
                    "ユーザー名とパスワードを登録してください。"
                )

        # 切り替えで今のログイン状態が消えるので、登録済みなら退避しておく
        self._preserve_current(progress, warnings)

        progress("Riot Client を終了中…")
        process.stop_all()

        if method == "session":
            progress("セッションを復元中…")
            backup = self.vault.app_dir / "backup" / "last"
            session.restore(blobs, backup_dir=backup)
        else:
            progress("ログアウト状態にしています…")
            session.clear_current()

        launched = False
        if launch_game or method == "autologin":
            progress("Riot Client を起動中…")
            launcher.launch(launcher.PRODUCT_VALORANT if launch_game else None)
            launched = True

        if method == "session" and launched and recapture:
            # 復元したトークンが Riot 側で失効していることがある。
            # ファイル上は正しく見えるので、実際に通るか試すまで分からない。
            # 通らなければクライアントはセッションを消してログイン画面に戻る。
            outcome = self.wait_for_login(account, progress=progress)
            if outcome == "ok":
                progress(f"{account.display_name}: ログインを確認しました")
                self.capture_into(account)
                recapture = False               # 取り込み済み
            elif can_autologin:
                # rejected でも timeout でも、入り直せるなら入り直す。
                # timeout は「拒否は見えなかったがログインもできていない」状態で、
                # 放置すると何も起きないまま終わってしまう。
                if outcome == "rejected":
                    account.session_rejected = True
                    self.vault.update(account)
                    warnings.append(
                        "保存されていたセッションは Riot 側で失効していました。"
                        "登録済みのログイン情報で入り直します。"
                    )
                else:
                    warnings.append(
                        "ログイン状態を確認できなかったので、"
                        "登録済みのログイン情報で入り直します。"
                    )
                progress("自動ログインに切り替えます…")
                method = "autologin"
            else:
                if outcome == "rejected":
                    account.session_rejected = True
                    self.vault.update(account)
                warnings.append(
                    self._missing_credentials_reason(account, outcome)
                )

        if method == "autologin":
            progress("ログイン画面を待っています…")
            try:
                window = autologin.wait_for_login_window(timeout=120)
                progress("ログイン情報を入力中…")
                outcome = autologin.perform_login(
                    account.username, account.password,
                    window=window, submit=submit_login,
                    stay_signed_in=self.stay_signed_in,
                )
                # stay_signed_in=None は「触っていない」という既定の状態。
                # 毎回警告に出すとノイズにしかならないので黙っておく。
                if outcome["stay_signed_in"] is False:
                    warnings.append(
                        "「サインイン状態を維持」の状態を判別できませんでした。"
                        "ログイン画面で有効になっているか確認してください。"
                        "これが無効だとセッションを保存できず、次回から切り替えできません。"
                    )
                if outcome["submitted"]:
                    progress("サインインしました")
                    warnings.append(
                        "サインインを実行しました。hCaptcha や 2 段階認証が出た場合は、"
                        "画面に従って対応してください。"
                    )
                else:
                    warnings.append(
                        "ユーザー名とパスワードを入力しました。"
                        "サインインはご自身で押してください。"
                    )
            except autologin.AutoLoginError as exc:
                warnings.append(f"自動入力に失敗しました: {exc}")

        account.last_used_at = time.time()
        self.vault.update(account)
        progress(f"{account.display_name} に切り替えました")

        # 自動ログインを行った場合は、その後のセッションを取り込む。
        # クライアントは起動やログインのたび refresh_token を更新するので、
        # ここで取り込まないと保管庫のコピーが一世代古いままになり、
        # 次回の切り替えで Riot に拒否される。
        if launched and recapture:
            if not self.wait_and_capture(account, progress=progress):
                warnings.append(
                    "ログイン後のセッションを取り込めませんでした。"
                    "次回このアカウントに切り替えられない可能性があります。"
                    "ログインが済んだら「現在のログインをこのアカウントに保存」を実行してください。"
                )

        return SwitchResult(account.id, method, launched, warnings)

    def _preserve_current(self, progress: Progress, warnings: list[str]) -> None:
        """切り替え前に、今のログイン状態を該当アカウントへ保存し直す。"""
        try:
            current = self.current_account()
            if not current:
                return
            blobs = session.capture()
            info = session.inspect_blobs(blobs)
            if not info.valid or info.expired:
                return
            self.vault.clear_session(current.id)
            for rel, data in blobs.items():
                self.vault.write_session_blob(current.id, rel, data)
            current.session_saved = True
            current.session_saved_at = time.time()
            self.vault.update(current)
            progress(f"{current.display_name} のセッションを退避しました")
        except (session.SessionError, OSError) as exc:
            warnings.append(f"現在のセッションを退避できませんでした: {exc}")

    def logout_current(self) -> None:
        process.stop_all()
        session.clear_current()

    # -- 情報の更新 ---------------------------------------------------------
    def authenticate(self, account: Account) -> auth.AuthResult:
        """保存済み cookie からトークンを取り直す。切り替え不要。

        Riot は再認証のたびに cookie をローテーションして返すので、
        それを保管庫に書き戻して有効期限を延ばす。
        """
        blobs = self.vault.read_session_blobs(account.id)
        if not blobs:
            raise ServiceError("セッションが保存されていません")
        info = session.inspect_blobs(blobs)
        if not info.valid:
            raise ServiceError("保存されたセッションから cookie を読めませんでした")
        if info.expired:
            raise auth.SessionExpired("セッションが失効しています。ログインし直してください")

        if info.cookies.get("ssid"):
            result = auth.reauth_with_cookies(info.cookies)
            self._persist_rotated_cookies(account, blobs, info, result)
            return result

        # 現行 (refresh_token) 形式には ssid が無いので cookie 再認証は使えない。
        # 起動中のクライアントからトークンを借りる。refresh_token を消費しないので、
        # ローテーションで元のセッションを失効させる心配がない。
        return self._authenticate_via_local_api(account)

    def _authenticate_via_local_api(self, account: Account) -> auth.AuthResult:
        client = localapi.LocalClient()
        if not client.available:
            raise ServiceError(
                "Riot Client が起動していないため、このアカウントの情報を取得できません。"
                "現行の Riot Client はセッションを refresh_token で持っており、"
                "情報の取得には起動中のクライアントが要ります。"
                "このアカウントに切り替えてから実行してください。"
            )
        try:
            local = client.session()
        except localapi.LocalApiError as exc:
            raise ServiceError(f"ローカル API から取得できませんでした: {exc}") from exc

        if account.puuid and local.puuid and local.puuid != account.puuid:
            raise ServiceError(
                f"今ログインしているのは別のアカウントです"
                f"（{local.riot_id or local.puuid[:8]}）。"
                "情報を取りたいアカウントに切り替えてから実行してください。"
            )

        return auth.AuthResult(
            access_token=local.access_token,
            entitlements_token=local.entitlements_token,
            puuid=local.puuid or account.puuid,
            game_name=local.game_name,
            tag_line=local.tag_line,
            region=local.region or account.region,
        )

    def _persist_rotated_cookies(self, account: Account, blobs: dict[str, bytes],
                                 old: session.SessionInfo,
                                 result: auth.AuthResult) -> float:
        """更新された cookie を保管庫に書き戻す。延びた場合だけ触る。

        延長後の残り日数を返す。延長されなければ 0。
        """
        new_ssid = result.cookies.get("ssid")
        if not new_ssid or new_ssid == old.cookies.get("ssid"):
            return 0.0

        updated = session.update_cookies(blobs, result.cookies,
                                         result.cookie_expiries)
        new_info = session.inspect_blobs(updated)
        # 読み直して、確かに有効かつ期限が延びていることを確かめてから書く。
        # ここを確認しないと、壊れた応答で使えるセッションを潰しかねない。
        if not new_info.valid or new_info.expired:
            return 0.0
        if new_info.expires_at <= old.expires_at:
            return 0.0

        for rel, data in updated.items():
            self.vault.write_session_blob(account.id, rel, data)
        account.session_saved_at = time.time()
        self.vault.update(account)
        return new_info.expires_in_days

    def renew_session(self, account: Account, progress: Progress = _noop) -> dict:
        """セッションの有効期限を延ばすためだけの再認証。

        延長前後の残り日数を返す。UI の「セッションを延長」から呼ぶ。

        現行 (refresh_token) 形式では、期限を延ばすのは Riot Client 自身の
        仕事なので、ここでは何も起きない。そのアカウントで一度起動すれば
        トークンが更新され、期限が先に延びる。
        """
        before = self.session_status(account)
        if before.kind == "refresh_token":
            progress(f"{account.display_name}: 現行形式のため延長操作は不要です")
            return {
                "account_id": account.id,
                "extended": False,
                "not_applicable": True,
                "before_days": before.expires_in_days,
                "after_days": before.expires_in_days,
                "expires_at": before.expires_at,
            }

        progress(f"{account.display_name}: セッションを延長中…")
        self.authenticate(account)
        after = self.session_status(account)
        extended = after.expires_at > before.expires_at
        progress(
            f"{account.display_name}: 残り {after.expires_in_days:.0f} 日に延長しました"
            if extended else
            f"{account.display_name}: 有効期限は変わりませんでした（残り {after.expires_in_days:.0f} 日）"
        )
        return {
            "account_id": account.id,
            "extended": extended,
            "not_applicable": False,
            "before_days": before.expires_in_days,
            "after_days": after.expires_in_days,
            "expires_at": after.expires_at,
        }

    def refresh(self, account: Account, fetch_inventory: bool = True,
                progress: Progress = _noop) -> Account:
        """ランク・ウォレット・所持品を取得してアカウントに書き戻す。"""
        progress(f"{account.display_name}: 認証中…")
        result = self.authenticate(account)

        if result.puuid:
            account.puuid = result.puuid
        if result.riot_id:
            account.riot_id = result.riot_id
        region = result.region or account.region

        try:
            version = self.content.client_version()
        except content.ContentError:
            version = ""
        client = api.ValorantApi(result, region=region, client_version=version)

        progress(f"{account.display_name}: ランクを取得中…")
        mmr = client.mmr()
        # ランク名は valorant-api の表示名を優先する（日本語になる）
        names = self.content.tier_names()
        account.rank = RankInfo(
            tier=mmr.tier, tier_name=names.get(mmr.tier, mmr.tier_label), rr=mmr.rr,
            peak_tier=mmr.peak_tier,
            peak_tier_name=names.get(mmr.peak_tier, mmr.peak_tier_label),
            peak_season=mmr.peak_season, leaderboard_rank=mmr.leaderboard_rank,
            wins=mmr.wins, games=mmr.games, updated_at=time.time(),
        )

        progress(f"{account.display_name}: ウォレットを取得中…")
        try:
            w = client.wallet()
            account.wallet = WalletInfo(vp=w["vp"], rp=w["rp"], kc=w["kc"],
                                        updated_at=time.time())
        except api.ApiError:
            pass

        if fetch_inventory:
            progress(f"{account.display_name}: 所持品を取得中…")
            inv = InventoryInfo(updated_at=time.time())
            for kind, attr in (("skins", "skin_level_ids"), ("agents", "agent_ids"),
                               ("buddies", "buddy_ids"), ("cards", "card_ids"),
                               ("titles", "title_ids"), ("sprays", "spray_ids")):
                try:
                    setattr(inv, attr, client.entitlements(kind))
                except api.ApiError:
                    pass
            account.inventory = inv

        account.region = region
        self.vault.update(account)
        progress(f"{account.display_name}: 更新しました")
        return account

    def refresh_many(self, accounts: Iterable[Account], fetch_inventory: bool = True,
                     progress: Progress = _noop) -> dict[str, str]:
        """複数アカウントをまとめて更新し、失敗したものの理由を返す。"""
        errors: dict[str, str] = {}
        for acc in accounts:
            try:
                self.refresh(acc, fetch_inventory=fetch_inventory, progress=progress)
            except (ServiceError, auth.AuthError, api.ApiError) as exc:
                errors[acc.id] = str(exc)
                progress(f"{acc.display_name}: 失敗 — {exc}")
        return errors

    def match_history(self, account: Account, count: int = 20) -> list[api.CompetitiveUpdate]:
        result = self.authenticate(account)
        try:
            version = self.content.client_version()
        except content.ContentError:
            version = ""
        client = api.ValorantApi(result, region=result.region or account.region,
                                 client_version=version)
        return client.competitive_history(count=count)

    # -- 所持品の集計 -------------------------------------------------------
    def summarize_inventory(self, account: Account) -> dict:
        """所持スキンをレア度ごとに数え、名前付きの一覧を作る。"""
        try:
            levels = self.content.skin_levels()
            skins = self.content.skins()
            tiers = self.content.content_tiers()
            level_to_skin = self.content.skin_level_to_skin()
        except content.ContentError as exc:
            raise ServiceError(str(exc)) from exc

        owned_skins: dict[str, dict] = {}
        for level_id in account.inventory.skin_level_ids:
            skin_id = level_to_skin.get(level_id)
            if not skin_id or skin_id in owned_skins:
                continue
            skin = skins.get(skin_id)
            if not skin:
                continue
            tier = tiers.get(skin["tier"], {})
            owned_skins[skin_id] = {
                "name": skin["name"],
                "icon": levels.get(level_id, {}).get("icon") or skin["icon"],
                "tier_name": tier.get("name", "スタンダード"),
                "tier_rank": tier.get("rank", -1),
                "tier_color": tier.get("color", "#8b8b8b"),
            }

        by_tier: dict[str, int] = {}
        for s in owned_skins.values():
            by_tier[s["tier_name"]] = by_tier.get(s["tier_name"], 0) + 1

        agents = self.content.agents()
        return {
            "skins": sorted(owned_skins.values(),
                            key=lambda s: (-s["tier_rank"], s["name"])),
            "skin_count": len(owned_skins),
            "by_tier": by_tier,
            "agent_count": len(account.inventory.agent_ids),
            "agent_total": len(agents),
            "buddy_count": len(account.inventory.buddy_ids),
            "card_count": len(account.inventory.card_ids),
            "title_count": len(account.inventory.title_ids),
            "spray_count": len(account.inventory.spray_ids),
        }
