"""アプリの中核。保管庫と Riot 連携層をつなぐ。

UI からはここだけを呼ぶ。進捗はコールバックで文字列を流す。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable

from .models import Account, InventoryInfo, RankInfo, WalletInfo
from .storage import Vault
from . import paths
from .riot import api, auth, autologin, content, launcher, localapi, process, session

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


class AccountService:
    def __init__(self, vault: Vault):
        self.vault = vault
        self.content = content.ContentCache(vault.app_dir / "cache")

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
            raise ServiceError(
                "ログイン状態が見つかりません。Riot Client でログインし、"
                "「ログイン情報を保存する」を有効にしてから取り込んでください。"
            )
        if info.expired:
            raise ServiceError("このセッションは既に失効しています")

        self.vault.clear_session(account.id)
        for rel, data in blobs.items():
            self.vault.write_session_blob(account.id, rel, data)

        account.session_saved = True
        account.session_saved_at = time.time()
        if info.puuid:
            account.puuid = info.puuid
        self.vault.update(account)
        progress(f"{account.display_name} のセッションを保存しました")
        return account

    def import_current(self, label: str = "", progress: Progress = _noop) -> Account:
        """今ログイン中のアカウントを新規登録する。"""
        info = self.current_session_info()
        if not info.valid:
            raise ServiceError("ログイン中のアカウントが見つかりません")

        for acc in self.vault.accounts():
            if acc.puuid and acc.puuid == info.puuid:
                raise ServiceError(f"このアカウントは「{acc.display_name}」として登録済みです")

        account = Account(label=label or "取り込んだアカウント", puuid=info.puuid)

        # クライアントが起動中なら Riot ID まで取れる
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

    # -- 切り替え -----------------------------------------------------------
    def switch(self, account: Account, launch_game: bool = True,
               force: bool = False, allow_autologin: bool = True,
               submit_login: bool = True,
               progress: Progress = _noop) -> SwitchResult:
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
        if not info.valid or info.expired:
            if info.expired:
                warnings.append("保存されたセッションが失効していました")
            if allow_autologin and account.username and account.password:
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

        if method == "autologin":
            progress("ログイン画面を待っています…")
            try:
                window = autologin.wait_for_login_window(timeout=90)
                progress("ログイン情報を入力中…")
                autologin.perform_login(
                    account.username, account.password,
                    window=window, submit=submit_login,
                )
            except autologin.AutoLoginError as exc:
                warnings.append(f"自動入力に失敗しました: {exc}")

        account.last_used_at = time.time()
        self.vault.update(account)
        progress(f"{account.display_name} に切り替えました")
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

        result = auth.reauth_with_cookies(info.cookies)
        self._persist_rotated_cookies(account, blobs, info, result)
        return result

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
        """
        before = self.session_status(account)
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
