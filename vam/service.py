"""アプリの中核。保管庫と Riot 連携層をつなぐ。

UI からはここだけを呼ぶ。進捗はコールバックで文字列を流す。
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Callable, Iterable

from . import diagnostics
from .history_store import HistoryStore
from .match_cache import MatchCache
from .models import Account, InventoryInfo, RankInfo, WalletInfo
from .storage import Vault
from . import paths
from .riot import (
    api, auth, autologin, content, gameversion, henrik, launcher, localapi, process, session,
    status, windows,
)

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    pass


def _merge_rows(saved: list[MatchListRow], fresh: list[MatchListRow]) -> list[MatchListRow]:
    """保存済みの行に新しく取れた行を重ねる (同じ試合は新しい方で置き換え)。
    新しい方に RR の情報が無いとき (RR 履歴に載っていない) は、保存済みの
    RR を引き継ぐ。開始時刻の新しい順に返す。"""
    by_id = {r.match_id: r for r in saved}
    for row in fresh:
        old = by_id.get(row.match_id)
        if old and not row.tier_after and old.tier_after:
            row.tier_after, row.rr_after, row.rr_earned = (
                old.tier_after, old.rr_after, old.rr_earned)
        by_id[row.match_id] = row
    return sorted(by_id.values(), key=lambda r: -r.started_at)


def _aggregate_rows(rows: list[MatchListRow], source: str) -> MatchStats:
    """行から HS 率・エージェント別/マップ別勝率を集計する。"""
    stats = MatchStats(matches=list(rows), source=source)
    agent_buckets: dict[str, AgentStat] = {}
    map_buckets: dict[str, MapStat] = {}
    headshots = bodyshots = legshots = 0
    for r in rows:
        stats.games += 1
        stats.wins += int(r.won)
        headshots += r.headshots
        bodyshots += r.bodyshots
        legshots += r.legshots
        agent_name = r.agent_name or "不明なエージェント"
        agent = agent_buckets.setdefault(agent_name, AgentStat(name=agent_name))
        agent.games += 1
        agent.wins += int(r.won)
        map_name = r.map_name or "不明なマップ"
        map_stat = map_buckets.setdefault(map_name, MapStat(name=map_name))
        map_stat.games += 1
        map_stat.wins += int(r.won)
    total_shots = headshots + bodyshots + legshots
    stats.headshot_pct = (headshots / total_shots * 100) if total_shots else 0.0

    # パーティーの相手ごとの勝率 (情報のある試合だけ)。名前は新しい試合のものを使う
    mates: dict[str, PartyStat] = {}
    for r in rows:   # rows は新しい順
        if r.party is None:
            continue
        stats.party_known_games += 1
        for m in r.party:
            key = m.get("puuid") or m.get("name", "")
            if not key:
                continue
            stat = mates.setdefault(key, PartyStat(puuid=key))
            if not stat.name and m.get("name"):
                stat.name = f"{m['name']}#{m['tag']}" if m.get("tag") else m["name"]
            stat.games += 1
            stat.wins += int(r.won)
    stats.party_stats = sorted(mates.values(), key=lambda s: (-s.games, -s.wins))[:5]
    stats.by_agent = sorted(agent_buckets.values(), key=lambda a: -a.games)
    stats.by_map = sorted(map_buckets.values(), key=lambda m: -m.games)
    return stats


def _shots_to_pct(stats: dict) -> float:
    """HenrikDev の stats {headshots, bodyshots, legshots} から HS% を出す。"""
    hs = stats.get("headshots", 0)
    bs = stats.get("bodyshots", 0)
    ls = stats.get("legshots", 0)
    total = hs + bs + ls
    return (hs / total * 100) if total else 0.0


def _parse_iso_to_ms(value: str | None) -> int:
    """HenrikDev の ISO8601 文字列 ('...Z' 含む) をエポック ms にする。"""
    if not value:
        return 0
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


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


@dataclass
class AgentStat:
    name: str
    games: int = 0
    wins: int = 0

    @property
    def win_rate(self) -> float:
        return (self.wins / self.games * 100) if self.games else 0.0


@dataclass
class PartyStat:
    puuid: str
    name: str = ""
    games: int = 0
    wins: int = 0

    @property
    def win_rate(self) -> float:
        return (self.wins / self.games * 100) if self.games else 0.0


@dataclass
class MapStat:
    name: str
    games: int = 0
    wins: int = 0

    @property
    def win_rate(self) -> float:
        return (self.wins / self.games * 100) if self.games else 0.0


@dataclass
class MatchListRow:
    """戦績一覧の1行分。RR 変動と、自分のその試合の成績をまとめて持つ。"""
    match_id: str = ""
    map_id: str = ""
    started_at: int = 0
    tier_after: int = 0
    rr_after: int = 0
    rr_earned: int = 0
    won: bool = False
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    score: int = 0
    team_score: int = 0
    enemy_score: int = 0
    placement: int = 0
    headshot_pct: float = 0.0
    damage_delta: int = 0
    # 保存した行だけで集計・表示できるよう、名前と弾数も持っておく
    agent_name: str = ""
    map_name: str = ""
    headshots: int = 0
    bodyshots: int = 0
    legshots: int = 0
    # 同じパーティーだった味方 [{"puuid","name","tag"}]。None は情報なし (古い保存)
    party: list | None = None

    @property
    def tier_after_name(self) -> str:
        return api.tier_name(self.tier_after)

    @property
    def kd(self) -> float:
        return self.kills / self.deaths if self.deaths else float(self.kills)

    @property
    def rounds(self) -> int:
        return self.team_score + self.enemy_score

    @property
    def acs(self) -> int:
        """ACS (平均戦闘スコア) = 総スコア ÷ ラウンド数。"""
        return round(self.score / self.rounds) if self.rounds else 0


@dataclass
class MatchPlayerStat:
    """スコアボード1行分 (自分・味方・敵、全員共通の形)。"""
    puuid: str = ""
    name: str = ""
    tag: str = ""
    party_id: str = ""
    agent_id: str = ""
    agent_name: str = ""
    agent_icon: str = ""
    tier_name: str = ""
    tier_icon: str = ""
    score: int = 0
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    headshots: int = 0
    bodyshots: int = 0
    legshots: int = 0
    damage_dealt: int = 0
    damage_received: int = 0
    is_self: bool = False

    @property
    def riot_id(self) -> str:
        return f"{self.name}#{self.tag}" if self.tag else self.name

    @property
    def kd(self) -> float:
        return self.kills / self.deaths if self.deaths else float(self.kills)

    @property
    def headshot_pct(self) -> float:
        total = self.headshots + self.bodyshots + self.legshots
        return (self.headshots / total * 100) if total else 0.0

    @property
    def damage_delta(self) -> int:
        return self.damage_dealt - self.damage_received


@dataclass
class MatchDetail:
    """1 試合のスコアボード。味方/敵チームに分けて、スコア順で持つ。"""
    match_id: str = ""
    map_name: str = ""
    started_at: int = 0
    won: bool = False
    my_team_score: int = 0
    enemy_team_score: int = 0
    my_team: list[MatchPlayerStat] = field(default_factory=list)
    enemy_team: list[MatchPlayerStat] = field(default_factory=list)

    @property
    def rounds(self) -> int:
        return self.my_team_score + self.enemy_team_score

    def acs_of(self, player: "MatchPlayerStat") -> int:
        """そのプレイヤーの ACS (総スコア ÷ ラウンド数)。"""
        return round(player.score / self.rounds) if self.rounds else 0


@dataclass
class MatchStats:
    """直近の試合から集計した HS 率・エージェント別/マップ別勝率。"""
    matches: list[MatchListRow] = field(default_factory=list)
    games: int = 0
    wins: int = 0
    headshot_pct: float = 0.0
    by_agent: list[AgentStat] = field(default_factory=list)
    by_map: list[MapStat] = field(default_factory=list)
    # 一緒に遊んだ回数の多い順 (最大 5 人)。パーティー情報のある試合だけで集計
    party_stats: list["PartyStat"] = field(default_factory=list)
    party_known_games: int = 0
    # "api" (HenrikDev、サインイン不要) / "local" (ローカル API、要サインイン)
    source: str = "local"
    # 保存済みの戦績を出しているとき: 最後に取得できた時刻 (epoch 秒)。
    # 最新の取得に失敗したときは、その理由が fetch_error に入る。
    saved_at: float = 0.0
    fetch_error: str = ""
    from_saved: bool = False

    @property
    def win_rate(self) -> float:
        return (self.wins / self.games * 100) if self.games else 0.0


VP_CURRENCY_ID = "85ad13f7-3d1b-5128-9eb2-7cd8ee0b5741"   # VALORANT Points


@dataclass
class StoreItem:
    """ストアの商品 1 点。表示に要る名前・画像・価格を持ち、保存した内容だけで描ける。"""
    offer_id: str = ""
    name: str = ""
    icon: str | None = None
    cost: int = 0               # 今の価格 (VP)。ナイトマーケットでは割引後
    base_cost: int = 0          # 割引前
    discount_percent: int = 0
    tier_name: str = ""
    tier_color: str = "#8b8b8b"


@dataclass
class StoreBundle:
    name: str = ""
    icon: str | None = None
    base_cost: int = 0
    cost: int = 0
    discount_percent: int = 0
    ends_at: float = 0.0


@dataclass
class StoreData:
    saved_at: float = 0.0
    daily: list[StoreItem] = field(default_factory=list)
    daily_ends_at: float = 0.0          # 日替わりが入れ替わる時刻 (epoch 秒)
    night: list[StoreItem] = field(default_factory=list)
    night_ends_at: float = 0.0
    bundles: list[StoreBundle] = field(default_factory=list)
    from_saved: bool = False
    fetch_error: str = ""

    def expired(self, now: float | None = None) -> bool:
        """日替わりの入れ替え時刻を過ぎている (＝今のストアとは違う)。"""
        now = time.time() if now is None else now
        if self.daily_ends_at:
            return now >= self.daily_ends_at
        if self.saved_at:
            # 入れ替わり時刻が分からないときは、取得が直近の入れ替わり (00:00 UTC) より前か
            return self.saved_at < now - (now % 86400)
        return False


@dataclass
class WeaponSkin:
    name: str
    icon: str | None = None
    tier_name: str = "スタンダード"
    tier_color: str = "#8b8b8b"
    tier_rank: int = 0


@dataclass
class WeaponGroup:
    weapon_id: str
    name: str
    icon: str | None = None
    category: str = ""
    skins: list[WeaponSkin] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.skins)


# 設定キー。settings.json に平文で置く (機密ではない)
SETTING_STAY_SIGNED_IN = "stay_signed_in"
SETTING_DISCORD_WEBHOOK = "discord_webhook_url"
SETTING_DISCORD_MENTION = "discord_mention"
SETTING_LOGIN_STEP_DELAY = "login_step_delay"
DEFAULT_LOGIN_STEP_DELAY = 0.5
MIN_LOGIN_STEP_DELAY = 0.1
SETTING_HENRIK_API_KEY = "henrik_api_key"
SETTING_SORT_MODE = "sort_mode"
SETTING_AUTO_STORE = "auto_store"
SETTING_REFRESH_MODE = "refresh_mode"
SETTING_REFRESH_SELECTION = "refresh_selection"
SETTING_REFRESH_SKIP_UPDATED = "refresh_skip_updated"
# 「すべて更新」の動作。choose=押すと更新するアカウントを選ぶ (既定) / all=ワンクリックで全アカウント
REFRESH_MODES = ("all", "choose")
SETTING_AUTO_STORE_LAST = "auto_store_last"
# アカウント一覧の並び順。recent=お気に入り優先・使った順 (既定) / manual=手動 / rank=ランク順 (自動)
SORT_MODES = ("recent", "manual", "rank")


# 進捗を UI に伝えるコールバック (イベントは dict)。None なら何もしない
Events = Callable[[dict], None] | None

REFRESH_STEPS = ("rank", "history", "skins", "store")
# 保留 (⚠) の判定に使う、中心の 3 つ。ストアが取れなくても保留にはしない
CORE_STEPS = ("rank", "history", "skins")
STEP_LABELS = {"rank": "ランク", "history": "戦績", "skins": "スキン", "store": "ストア"}


@dataclass
class RefreshReport:
    """1 アカウント分の更新結果。errors は失敗したステップ -> 理由。"""
    account_id: str
    errors: dict[str, str] = field(default_factory=dict)

    cancelled: bool = False   # キャンセルで一部のステップを飛ばした

    @property
    def ok(self) -> bool:
        return not self.errors and not self.cancelled

    @property
    def held(self) -> bool:
        """ランク・戦績・スキンの 3 つとも失敗した (＝保留にするアカウント)。"""
        return all(step in self.errors for step in CORE_STEPS)


class _RefreshContext:
    """1 アカウントの更新中に、認証とクライアントを使い回す。認証に失敗した
    ときは同じ例外を使い回し、ステップごとに認証をやり直さない。"""

    def __init__(self, service: "AccountService", account: Account):
        self.service = service
        self.account = account
        self.authenticated = False
        self._result = None
        self._client = None
        self._error: Exception | None = None

    def client(self) -> "api.ValorantApi":
        if self._error is not None:
            raise self._error
        if self._client is None:
            try:
                self._result = self.service.authenticate(self.account)
                try:
                    version = self.service.content.client_version()
                except content.ContentError:
                    version = ""
                region = self._result.region or self.account.region
                self._client = api.ValorantApi(self._result, region=region,
                                               client_version=version)
                self.authenticated = True
                self.apply_identity()  # 戦績の取得が puuid を使えるように、早めに反映
            except Exception as exc:
                self._error = exc
                raise
        return self._client

    @property
    def puuid(self) -> str:
        return self._result.puuid if self._result is not None else ""

    def apply_identity(self) -> None:
        """認証で分かった puuid / Riot ID / リージョンをアカウントに反映する。"""
        if self._result is None:
            return
        if self._result.puuid:
            self.account.puuid = self._result.puuid
        if self._result.riot_id:
            self.account.riot_id = self._result.riot_id
        self.account.region = self._result.region or self.account.region


class AccountService:
    def __init__(self, vault: Vault):
        self.vault = vault
        self.content = content.ContentCache(vault.app_dir / "cache")
        self.match_cache = MatchCache(vault.app_dir / "cache")
        # アカウントごとの戦績 JSON (history/<account.id>.json)
        self.history = HistoryStore(vault.app_dir / "history")
        # アカウントごとのストア JSON (store/<account.id>.json)
        self.store_store = HistoryStore(vault.app_dir / "store")
        # HenrikDev モードで戦績一覧を取ったときの生データ。スコアボードを
        # 開くときに (アプリを再起動していなければ) 取り直さずに使う。
        self._henrik_raw_matches: dict[str, dict] = {}

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

    @property
    def discord_mention(self) -> str:
        """Discord 通知の先頭に添えるメンション文字列。例: @everyone, <@ユーザーID>"""
        return str(self.vault.settings().get(SETTING_DISCORD_MENTION, ""))

    @discord_mention.setter
    def discord_mention(self, value: str) -> None:
        settings = self.vault.settings()
        settings[SETTING_DISCORD_MENTION] = value.strip()
        self.vault.save_settings(settings)

    @property
    def sort_mode(self) -> str:
        mode = str(self.vault.settings().get(SETTING_SORT_MODE, "recent"))
        return mode if mode in SORT_MODES else "recent"

    @sort_mode.setter
    def sort_mode(self, value: str) -> None:
        if value not in SORT_MODES:
            raise ValueError(value)
        settings = self.vault.settings()
        settings[SETTING_SORT_MODE] = value
        self.vault.save_settings(settings)

    def sorted_accounts(self) -> list[Account]:
        """設定された並び順でアカウントを返す。お気に入りは、どの並び順でも
        必ず先頭にまとめる (お気に入りの中・それ以外の中で、並び順に従う)。"""
        accounts = self.vault.accounts()  # 保管庫の順 = 手動で並べた順
        mode = self.sort_mode
        if mode == "rank":
            # ランクの高い順。ランク無し (未ランク) は最後。同ランクは RR、次に使った順
            accounts = sorted(accounts, key=lambda a: (
                -(a.rank.tier if a.rank.tier > 2 else 0),
                -(a.rank.rr if a.rank.tier > 2 else 0),
                -a.last_used_at,
            ))
        elif mode == "recent":
            accounts = sorted(accounts, key=lambda a: -a.last_used_at)
        # 安定ソートなので、お気に入りの中の順は上で決めた順のまま
        return sorted(accounts, key=lambda a: not a.favorite)

    @property
    def henrik_api_key(self) -> str:
        """他プレイヤー検索 (HenrikDev API) に使うキー。無ければ検索不可。"""
        return str(self.vault.settings().get(SETTING_HENRIK_API_KEY, ""))

    @henrik_api_key.setter
    def henrik_api_key(self, value: str) -> None:
        settings = self.vault.settings()
        settings[SETTING_HENRIK_API_KEY] = value.strip()
        self.vault.save_settings(settings)

    def search_player(self, riot_id: str) -> tuple[henrik.PlayerAccount, henrik.PlayerRank]:
        """Name#TAG で任意のプレイヤーのレベル・ランクを検索する (HenrikDev API)。"""
        return henrik.search(riot_id, self.henrik_api_key)

    @property
    def login_step_delay(self) -> float:
        """自動ログインで、フォームが出てから入力を始めるまでの待機秒数。

        短すぎると Electron 側の描画が間に合わず入力を取りこぼす
        (実機で確認済み)。下限は 0.1 秒。
        """
        value = self.vault.settings().get(SETTING_LOGIN_STEP_DELAY, DEFAULT_LOGIN_STEP_DELAY)
        try:
            return max(MIN_LOGIN_STEP_DELAY, float(value))
        except (TypeError, ValueError):
            return DEFAULT_LOGIN_STEP_DELAY

    @login_step_delay.setter
    def login_step_delay(self, value: float) -> None:
        settings = self.vault.settings()
        settings[SETTING_LOGIN_STEP_DELAY] = max(MIN_LOGIN_STEP_DELAY, float(value))
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
            mention = self.discord_mention
            for event in status.diff(previous, current):
                if webhook:
                    try:
                        # 重大 (critical) なときだけメンションを付ける。
                        # info/warning やメンテナンス (severity 無し) は
                        # 毎回メンションされるとうるさいので付けない。
                        event_mention = mention if event.severity == "critical" else ""
                        status.notify_discord(webhook, event, mention=event_mention)
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
                       progress: Progress = _noop, cancel_event=None) -> str:
        """復元したセッションが通るかを見届ける。

        返り値は "ok" / "rejected" / "timeout" / "cancelled"。

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
            if cancel_event is not None and cancel_event.is_set():
                return "cancelled"
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
                         progress: Progress = _noop, cancel_event=None) -> bool:
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
            if cancel_event is not None and cancel_event.is_set():
                return False
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
               client_only: bool = False, mark_used: bool = True,
               progress: Progress = _noop, cancel_event=None) -> SwitchResult:
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
            if paths.session_files():
                # 消せていないと、Riot Client は前のアカウントのまま自動ログインして、
                # ログイン画面が出ない (＝自動入力する相手がいない) まま待たされる。
                raise ServiceError(
                    "Riot Client のログイン情報を消せなかったため、自動入力に進めません。"
                    "Riot Client のデータフォルダ (Data など) がリンクになっている場合は、"
                    "右上の「?」の診断情報を確認してください。"
                )

        launched = False
        if launch_game or method == "autologin" or client_only:
            # client_only: ゲームは起動せず、Riot Client にログインさせるだけ
            progress("Riot Client を起動中…")
            launcher.launch(launcher.PRODUCT_VALORANT if launch_game else None)
            launched = True

        if method == "session" and launched and recapture:
            # 復元したトークンが Riot 側で失効していることがある。
            # ファイル上は正しく見えるので、実際に通るか試すまで分からない。
            # 通らなければクライアントはセッションを消してログイン画面に戻る。
            outcome = self.wait_for_login(account, progress=progress, cancel_event=cancel_event)
            if outcome == "cancelled":
                raise ServiceError("キャンセルされました")
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
                window = autologin.wait_for_login_window(timeout=120, cancel_event=cancel_event)
                progress("ログイン情報を入力中…")
                outcome = autologin.perform_login(
                    account.username, account.password,
                    window=window, submit=submit_login,
                    stay_signed_in=self.stay_signed_in,
                    settle=self.login_step_delay,
                    cancel_event=cancel_event,
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
                if cancel_event is not None and cancel_event.is_set():
                    raise ServiceError("キャンセルされました") from exc
                warnings.append(f"自動入力に失敗しました: {exc}")

        if mark_used:
            # 一括更新の入れ替えは「使った」ことにしない (使った順の並びを乱さない)
            account.last_used_at = time.time()
        self.vault.update(account)
        progress(f"{account.display_name} に切り替えました")

        # 自動ログインを行った場合は、その後のセッションを取り込む。
        # クライアントは起動やログインのたび refresh_token を更新するので、
        # ここで取り込まないと保管庫のコピーが一世代古いままになり、
        # 次回の切り替えで Riot に拒否される。
        if cancel_event is not None and cancel_event.is_set():
            raise ServiceError("キャンセルされました")

        if launched and recapture:
            if not self.wait_and_capture(account, progress=progress, cancel_event=cancel_event):
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

    def _authenticate_via_local_api(self, account: Account,
                                    allow_other: bool = False) -> auth.AuthResult:
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

        if (not allow_other and account.puuid and local.puuid
                and local.puuid != account.puuid):
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
        """ランク・ウォレット・所持品を取得してアカウントに書き戻す。
        どれかが失敗したら例外にする (単体の更新用。まとめて更新は
        refresh_account を使う)。"""
        ctx = _RefreshContext(self, account)
        progress(f"{account.display_name}: 認証中…")
        ctx.client()  # 認証できなければここで例外
        self._refresh_rank(account, ctx, progress)
        if fetch_inventory:
            self._refresh_skins(account, ctx, progress)
        ctx.apply_identity()
        self.vault.update(account)
        progress(f"{account.display_name}: 更新しました")
        return account

    def refresh_account(self, account: Account, progress: Progress = _noop,
                        steps: Iterable[str] = REFRESH_STEPS,
                        auth_error: Exception | None = None,
                        cancel_event=None, events: Events = None) -> RefreshReport:
        """ランク・戦績・スキンを、互いに独立に取りにいく。

        どれかが失敗しても次へ進み、失敗の理由を report に集める。全部
        (3 つ) 失敗したときは report.held が True になる (呼び出し側が保留扱いにする)。
        """
        steps = tuple(steps)
        ctx = _RefreshContext(self, account)
        if auth_error is not None:
            ctx._error = auth_error  # 認証が要るステップは、この理由で失敗扱いにする
        report = RefreshReport(account.id)
        runners = {
            "rank": lambda: self._refresh_rank(account, ctx, progress),
            "history": lambda: self._refresh_history(account, progress),
            "skins": lambda: self._refresh_skins(account, ctx, progress),
            "store": lambda: self._refresh_store(account, ctx, progress),
        }
        emit = events or (lambda _e: None)
        for step in steps:
            if cancel_event is not None and cancel_event.is_set():
                report.cancelled = True
                emit({"kind": "step", "step": step, "state": "skipped"})
                continue
            emit({"kind": "step", "step": step, "state": "running"})
            try:
                runners[step]()
                emit({"kind": "step", "step": step, "state": "ok"})
            except Exception as exc:  # 1 つの失敗で他を止めない
                report.errors[step] = str(exc) or type(exc).__name__
                progress(f"{account.display_name}: {STEP_LABELS[step]}に失敗 — {report.errors[step]}")
                emit({"kind": "step", "step": step, "state": "error",
                      "error": report.errors[step]})
        ctx.apply_identity()
        if set(REFRESH_STEPS) <= set(steps) and not report.errors and not report.cancelled:
            account.last_refreshed_at = time.time()   # 全部取れた = 更新済み
        if len(report.errors) < len(steps) or ctx.authenticated:
            self.vault.update(account)  # 取れた分 (ランク・所持品・ID) を書き戻す
        return report

    def refresh_many(self, accounts: Iterable[Account],
                     progress: Progress = _noop) -> dict[str, RefreshReport]:
        """複数アカウントをまとめて更新する。アカウント id -> 結果。
        (全部成功したものも含めて返すので、呼び側は report.errors で判断する。)"""
        reports: dict[str, RefreshReport] = {}
        accounts = list(accounts)
        for i, acc in enumerate(accounts, 1):
            def tagged(msg: str, i=i) -> None:
                progress(f"[{i}/{len(accounts)}] {msg}")
            reports[acc.id] = self.refresh_account(acc, progress=tagged)
        return reports

    # -- ストア -------------------------------------------------------------
    def saved_store(self, account: Account) -> StoreData | None:
        """保存済みのストア。通信しない。無ければ None。"""
        data = self.store_store.load(account.id)
        if not data.get("daily") and not data.get("bundles") and not data.get("night"):
            return None

        def items(key: str) -> list[StoreItem]:
            names = {f.name for f in fields(StoreItem)}
            return [StoreItem(**{k: v for k, v in raw.items() if k in names})
                    for raw in data.get(key) or [] if isinstance(raw, dict)]

        bundle_names = {f.name for f in fields(StoreBundle)}
        return StoreData(
            saved_at=float(data.get("saved_at") or 0.0),
            daily=items("daily"), daily_ends_at=float(data.get("daily_ends_at") or 0.0),
            night=items("night"), night_ends_at=float(data.get("night_ends_at") or 0.0),
            bundles=[StoreBundle(**{k: v for k, v in b.items() if k in bundle_names})
                     for b in data.get("bundles") or [] if isinstance(b, dict)],
            from_saved=True,
        )

    def fetch_store(self, account: Account, progress: Progress = _noop,
                    ctx: "_RefreshContext | None" = None) -> StoreData:
        """今日のストアを取得して保存する。そのアカウント自身のトークンが要る
        (ログインしている / 認証できるアカウントだけ)。"""
        progress(f"{account.display_name}: ストアを取得中…")
        ctx = ctx or _RefreshContext(self, account)
        client = ctx.client()
        raw = client.storefront(ctx.puuid or account.puuid)
        ctx.apply_identity()
        data = self._build_store(raw)
        data.saved_at = time.time()
        payload = asdict(data)
        for key in ("from_saved", "fetch_error"):
            payload.pop(key, None)
        self.store_store.update(account.id, lambda d: (d.clear(), d.update(payload)))
        return data

    def store(self, account: Account, progress: Progress = _noop) -> StoreData:
        """ストアを返す。取得できなければ保存済みを返し、理由を fetch_error に入れる。
        保存も無ければ例外。"""
        try:
            return self.fetch_store(account, progress)
        except Exception as exc:
            saved = self.saved_store(account)
            if saved is None:
                raise
            saved.fetch_error = str(exc) or type(exc).__name__
            return saved

    def _build_store(self, raw: dict) -> StoreData:
        """API の応答 (SkinsPanelLayout 等) を、表示用の StoreData にする。"""
        now = time.time()
        try:
            weapons = self.content.weapons()
            tiers = self.content.content_tiers()
        except content.ContentError:
            weapons, tiers = {}, {}
        try:
            bundles_meta = self.content.bundles()
        except content.ContentError:
            bundles_meta = {}

        level_to_skin: dict[str, dict] = {}
        for w in weapons.values():
            for skin in w["skins"].values():
                for level_id in skin["levels"]:
                    level_to_skin[level_id] = skin

        def cost_of(offer: dict, key: str = "Cost") -> int:
            costs = offer.get(key) or {}
            if VP_CURRENCY_ID in costs:
                return int(costs[VP_CURRENCY_ID])
            return int(next(iter(costs.values()), 0))

        def make_item(offer_id: str, cost: int, base: int = 0, discount: int = 0) -> StoreItem:
            skin = level_to_skin.get(offer_id) or {}
            tier = tiers.get(skin.get("tier", ""), {})
            return StoreItem(
                offer_id=offer_id, name=skin.get("name") or "不明なスキン",
                icon=skin.get("icon"), cost=cost, base_cost=base or cost,
                discount_percent=discount,
                tier_name=tier.get("name", ""), tier_color=tier.get("color", "#8b8b8b"),
            )

        panel = raw.get("SkinsPanelLayout") or {}
        offers_by_id = {o.get("OfferID"): o for o in panel.get("SingleItemStoreOffers") or []}
        daily = []
        for offer_id in panel.get("SingleItemOffers") or []:
            offer = offers_by_id.get(offer_id) or {}
            daily.append(make_item(offer_id, cost_of(offer)))
        daily_left = float(panel.get("SingleItemOffersRemainingDurationInSeconds") or 0)

        night = []
        bonus = raw.get("BonusStore") or {}
        for entry in bonus.get("BonusStoreOffers") or []:
            offer = entry.get("Offer") or {}
            rewards = offer.get("Rewards") or [{}]
            offer_id = rewards[0].get("ItemID") or offer.get("OfferID", "")
            night.append(make_item(
                offer_id, cost_of(entry, "DiscountCosts"), cost_of(offer),
                int(entry.get("DiscountPercent") or 0)))
        night_left = float(bonus.get("BonusStoreRemainingDurationInSeconds") or 0)

        bundles = []
        featured = raw.get("FeaturedBundle") or {}
        listed = featured.get("Bundles") or ([featured["Bundle"]] if featured.get("Bundle") else [])
        for bundle in listed:
            meta = bundles_meta.get(bundle.get("DataAssetID", ""), {})

            def total(key: str) -> int:
                value = bundle.get(key)
                if isinstance(value, dict):
                    return int(value.get(VP_CURRENCY_ID, next(iter(value.values()), 0)))
                return int(value or 0)

            base, now_cost = total("TotalBaseCost"), total("TotalDiscountedCost")
            bundles.append(StoreBundle(
                name=meta.get("name") or "バンドル", icon=meta.get("icon"),
                base_cost=base, cost=now_cost or base,
                discount_percent=round(100 * (1 - now_cost / base)) if base and now_cost else 0,
                ends_at=now + float(bundle.get("DurationRemainingInSeconds") or 0),
            ))
        return StoreData(
            daily=daily, daily_ends_at=(now + daily_left) if daily_left else 0.0,
            night=night, night_ends_at=(now + night_left) if night_left else 0.0,
            bundles=bundles,
        )

    def _refresh_store(self, account: Account, ctx: "_RefreshContext",
                       progress: Progress) -> None:
        self.fetch_store(account, progress, ctx)

    def act_status(self, now: float | None = None) -> dict | None:
        """今のアクト (コンペのシーズン) の名前と終了までの残り。取れなければ None。
        {"name": "ACT V", "end": epoch 秒, "seconds_left": float}"""
        now = time.time() if now is None else now
        try:
            acts = self.content.acts()
        except content.ContentError:
            return None
        current = [a for a in acts if a["start"] <= now]
        if not current:
            return None
        act = current[-1]
        if act["end"] <= now:
            return None   # 終了日が過ぎている (次のアクトの情報がまだ無い)
        return {"name": act["name"], "end": act["end"], "seconds_left": act["end"] - now}

    # -- 「すべて更新」の動作 ----------------------------------------------------
    @property
    def refresh_mode(self) -> str:
        mode = str(self.vault.settings().get(SETTING_REFRESH_MODE, "choose"))
        return mode if mode in REFRESH_MODES else "choose"

    @refresh_mode.setter
    def refresh_mode(self, value: str) -> None:
        if value not in REFRESH_MODES:
            raise ValueError(value)
        settings = self.vault.settings()
        settings[SETTING_REFRESH_MODE] = value
        self.vault.save_settings(settings)

    @property
    def refresh_selection(self) -> list[str] | None:
        """前回選んだアカウント (id)。選んだことが無ければ None (＝全員を選んだ状態から)。"""
        raw = self.vault.settings().get(SETTING_REFRESH_SELECTION)
        return [str(x) for x in raw] if isinstance(raw, list) else None

    @refresh_selection.setter
    def refresh_selection(self, ids: list[str]) -> None:
        settings = self.vault.settings()
        settings[SETTING_REFRESH_SELECTION] = list(ids)
        self.vault.save_settings(settings)

    @property
    def refresh_skip_updated(self) -> bool:
        """選択画面で、今日更新済みのアカウントをスキップする (既定は入)。"""
        return bool(self.vault.settings().get(SETTING_REFRESH_SKIP_UPDATED, True))

    @refresh_skip_updated.setter
    def refresh_skip_updated(self, value: bool) -> None:
        settings = self.vault.settings()
        settings[SETTING_REFRESH_SKIP_UPDATED] = bool(value)
        self.vault.save_settings(settings)

    def refreshed_today(self, account: Account, now: float | None = None) -> bool:
        """今日 (ストアが入れ替わる日本時間 9:00 = 00:00 UTC 以降) に、全部取れた更新をしたか。"""
        now = time.time() if now is None else now
        return account.last_refreshed_at >= now - (now % 86400)

    # -- ストアの自動取得 ------------------------------------------------------
    @property
    def auto_store(self) -> bool:
        """ストアを毎日自動で取得するか (既定は切)。"""
        return bool(self.vault.settings().get(SETTING_AUTO_STORE, False))

    @auto_store.setter
    def auto_store(self, value: bool) -> None:
        settings = self.vault.settings()
        settings[SETTING_AUTO_STORE] = bool(value)
        self.vault.save_settings(settings)

    @property
    def auto_store_last(self) -> float:
        return float(self.vault.settings().get(SETTING_AUTO_STORE_LAST, 0.0))

    def mark_auto_store_run(self, when: float | None = None) -> None:
        settings = self.vault.settings()
        settings[SETTING_AUTO_STORE_LAST] = time.time() if when is None else when
        self.vault.save_settings(settings)

    # ストアが入れ替わるのは毎日 00:00 UTC (日本時間 9:00)
    STORE_RESET_SECONDS = 86400

    # -- VALORANT のアップデート確認 ---------------------------------------------
    def _patch_region(self) -> str:
        """パッチ情報を引く地域。登録アカウントで一番多い地域 (既定 ap)。"""
        counts: dict[str, int] = {}
        for acc in self.vault.accounts():
            if acc.region in api.REGIONS:
                counts[acc.region] = counts.get(acc.region, 0) + 1
        return max(counts, key=counts.get) if counts else "ap"

    def game_version(self) -> "gameversion.GameVersion":
        """インストール済みの VALORANT の版と、最新の版。モック環境では比べない。"""
        if process.mock_mode():
            return gameversion.GameVersion()
        return gameversion.check(self._patch_region())

    def store_stale_accounts(self, now: float | None = None) -> list[Account]:
        """今日のストアをまだ持っていないアカウント (保存が無い / 入れ替わり済み)。
        セッションが保存されているものだけ。"""
        now = time.time() if now is None else now
        out = []
        for acc in self.vault.accounts():
            if not acc.session_saved:
                continue
            saved = self.saved_store(acc)
            if saved is None or saved.expired(now) or not saved.daily_ends_at:
                out.append(acc)
        return out

    def auto_store_due(self, now: float | None = None) -> bool:
        """自動取得をいま始めてよいか。切のとき / Riot Client かゲームが動いているとき /
        直近 (6 時間以内) に試したばかりのとき / 取るものが無いときは False。"""
        now = time.time() if now is None else now
        if not self.auto_store or process.mock_mode():
            return False
        if process.running():       # Riot Client もゲームも止まっているときだけ
            return False
        if now - self.auto_store_last < 6 * 3600:
            return False
        return bool(self.store_stale_accounts(now))

    def refresh_cycle(self, accounts: Iterable[Account], progress: Progress = _noop,
                      cancel_event=None, events: Events = None,
                      steps: Iterable[str] = REFRESH_STEPS,
                      hide_windows: bool = True) -> dict[str, RefreshReport]:
        """入れ替え更新。Riot Client の画面は、出てきたそばから最小化して見せない。"""
        stop_hide = threading.Event()
        if hide_windows and not process.mock_mode():
            windows.keep_minimized(stop_hide)
        try:
            return self._run_refresh_cycle(accounts, progress, cancel_event, events, tuple(steps))
        finally:
            stop_hide.set()

    def _run_refresh_cycle(self, accounts: Iterable[Account], progress: Progress,
                           cancel_event, events: Events,
                           steps: tuple[str, ...]) -> dict[str, RefreshReport]:
        """アカウントを 1 つずつ Riot Client で起動 → 取得 → 停止、を繰り返す。

        現行の Riot Client のセッションは起動中のクライアントからしかトークンを
        借りられないので、まとめて更新するには 1 アカウントずつ入れ替えるしかない。
        VALORANT が起動していたら何もしない (勝手に落とさない)。ゲームは起動せず
        Riot Client だけ使う。パスワードの自動入力には頼らない (失効していたら
        そのアカウントは失敗として次へ)。終わったら、最初に入っていたアカウントを
        元に戻す。
        """
        if process.game_running():
            raise GameRunning("VALORANT が起動中です。終了してから一括更新してください。")
        accounts = list(accounts)
        original = None
        try:
            original = self.current_account()
        except Exception:
            pass

        def cancelled() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        emit = events or (lambda _e: None)
        reports: dict[str, RefreshReport] = {}
        total = len(accounts)
        emit({"kind": "start", "total": total})
        for i, acc in enumerate(accounts, 1):
            if cancelled():
                break

            def tagged(msg: str, i=i) -> None:
                progress(f"[{i}/{total}] {msg}")

            emit({"kind": "account", "index": i, "total": total, "id": acc.id,
                  "name": acc.display_name})
            switch_error: Exception | None = None
            emit({"kind": "client", "state": "running"})
            try:
                tagged(f"{acc.display_name}: Riot Client を起動中…")
                self.switch(acc, launch_game=False, client_only=True, allow_autologin=False,
                            recapture=True, mark_used=False, progress=tagged,
                            cancel_event=cancel_event)
                emit({"kind": "client", "state": "ok"})
            except Exception as exc:
                switch_error = exc
                emit({"kind": "client", "state": "error", "error": str(exc)})
            if cancelled():
                try:
                    process.stop_all()
                except Exception as exc:
                    diagnostics.log(f"refresh_cycle: 停止に失敗: {exc}")
                break
            report = self.refresh_account(acc, progress=tagged, auth_error=switch_error,
                                          cancel_event=cancel_event, events=emit, steps=steps)
            reports[acc.id] = report
            emit({"kind": "account_done", "id": acc.id, "held": report.held,
                  "partial": bool(report.errors) and not report.held})
            try:
                process.stop_all()  # 次のアカウントの前に落とす
            except Exception as exc:
                diagnostics.log(f"refresh_cycle: 停止に失敗: {exc}")

        # 一括更新の前に入っていたアカウントに戻す (切り替えで消えた状態の復元)。
        # キャンセルしたときも、途中のアカウントのまま放置しないよう戻す。
        if original is not None:
            emit({"kind": "restoring", "name": original.display_name})
            try:
                progress(f"{original.display_name} に戻しています…")
                # クライアントは起動しない (画面を残さない)。次に Riot Client を
                # 立ち上げたとき、このアカウントでログインした状態になる。
                self.switch(original, launch_game=False, client_only=False,
                            allow_autologin=False, recapture=False, mark_used=False,
                            progress=progress)
            except Exception as exc:
                diagnostics.log(f"refresh_cycle: 元のアカウントに戻せなかった: {exc}")
        return reports

    # -- 更新の各ステップ -----------------------------------------------------
    def _refresh_rank(self, account: Account, ctx: "_RefreshContext",
                      progress: Progress) -> None:
        client = ctx.client()
        progress(f"{account.display_name}: ランクを取得中…")
        mmr = client.mmr()
        names = self.content.tier_names()  # 日本語の表示名を優先
        legacy = (mmr.tier, mmr.rr, mmr.wins, mmr.games, mmr.leaderboard_rank)
        cur_tier, cur_rr, cur_wins, cur_games, cur_lb = legacy
        carried_from = ""

        def final_tier(info: dict) -> int:
            """そのシーズンの最終ランク。CompetitiveTier が 0 でも Rank に入っていればそれ。"""
            return max(info.get("tier", 0) or 0, info.get("rank", 0) or 0)

        def reached_tier(info: dict) -> int:
            """最終ランクが分からないとき、そのシーズンで届いた最高ランク (代わりに使う)。"""
            return max(final_tier(info), info.get("best", 0) or 0)

        acts = []
        try:
            acts = self.content.acts()
        except content.ContentError:
            pass
        act_ids = {a["uuid"] for a in acts}
        if acts:
            # 「今のアクト」はカレンダーで決める。mmr の LatestCompetitiveUpdate は
            # 最後に遊んだ試合のシーズンなので、新アクトの開始直後は古いまま。
            now = time.time()
            current_idx = max((i for i, a in enumerate(acts) if a["start"] <= now), default=-1)
            if current_idx >= 0:
                this = mmr.seasons.get(acts[current_idx]["uuid"]) or {}
                cur_tier, cur_rr = final_tier(this), this.get("rr", 0)
                cur_wins, cur_games = this.get("wins", 0), this.get("games", 0)
                cur_lb = this.get("leaderboard", 0)
            if cur_tier <= 2:
                previous = list(reversed(acts[:max(current_idx, 0)]))
                # 1) 直近のランク付きアクトの最終ランク  2) 最終が分からなければ届いた最高
                for measure in (final_tier, reached_tier):
                    for a in previous:
                        value = measure(mmr.seasons.get(a["uuid"]) or {})
                        if value > 2:
                            cur_tier, cur_rr, carried_from = value, 0, a["name"]
                            break
                    if carried_from:
                        break
        if cur_tier <= 2:
            # アクト表に無いシーズンや、アクト表が取れないとき: ランク付きの過去シーズンのどれか
            for info in reversed(list(mmr.seasons.values())):
                value = reached_tier(info)
                if value > 2:
                    cur_tier, cur_rr, carried_from = value, 0, "前アクト"
                    break
        if cur_tier <= 2 and legacy[0] > 2:
            # 以前の決め方 (最後に遊んだシーズンのランク) なら取れる
            cur_tier, cur_rr, cur_wins, cur_games, cur_lb = legacy
            carried_from = carried_from or "前アクト"
        if cur_tier <= 2 and mmr.latest_tier > 2:
            # シーズンごとの記録が空でも、最後に遊んだ競技試合の直後のランクは取れる
            cur_tier, cur_rr, carried_from = mmr.latest_tier, 0, "前アクト"
        if cur_tier <= 2:
            # さらに、このアプリが保存してある戦績 (試合ごとのランク) からも探す
            saved_tier = self._rank_from_saved_matches(account)
            if saved_tier > 2:
                cur_tier, cur_rr, carried_from = saved_tier, 0, "前回の試合"
        if cur_tier <= 2:
            # 原因を調べられるよう、判定の材料をログに残す (シーズン: (最終, Rank, 届いた最高))
            diagnostics.log(
                f"ランクが出ない: {account.display_name} 前回={account.rank.tier} "
                f"seasons={ {k[:8]: (v.get('tier'), v.get('rank'), v.get('best')) for k, v in mmr.seasons.items()} }")
        if cur_tier <= 2 and account.rank.tier > 2:
            # どう調べても出ないとき、前に取れていたランクを「？」で潰さない
            cur_tier, cur_rr = account.rank.tier, 0
            carried_from = account.rank.carried_from or "前回"
        account.rank = RankInfo(
            tier=cur_tier, tier_name=names.get(cur_tier, api.tier_name(cur_tier)), rr=cur_rr,
            peak_tier=mmr.peak_tier,
            peak_tier_name=names.get(mmr.peak_tier, mmr.peak_tier_label),
            peak_season=mmr.peak_season, leaderboard_rank=cur_lb,
            wins=cur_wins, games=cur_games, updated_at=time.time(),
            carried_from=carried_from,
        )

        progress(f"{account.display_name}: ウォレットを取得中…")
        try:
            w = client.wallet()
            account.wallet = WalletInfo(vp=w["vp"], rp=w["rp"], kc=w["kc"],
                                        updated_at=time.time())
        except api.ApiError:
            pass  # ウォレットだけの失敗はランク取得の失敗にしない

    def _rank_from_saved_matches(self, account: Account) -> int:
        """保存してある戦績の、いちばん新しい「ランクが付いていた試合」の直後のランク。"""
        try:
            rows, _saved_at, _source = self._load_saved(account)
        except Exception:
            return 0
        for row in rows:     # 新しい順
            if row.tier_after > 2:
                return row.tier_after
        return 0

    def _refresh_history(self, account: Account, progress: Progress) -> None:
        stats = self.match_stats(account, progress=progress)
        if stats.fetch_error:
            # 保存済みは見られるが、今回の取得は失敗している
            raise ServiceError(stats.fetch_error)
        self._prefetch_details(account, stats.matches)

    def _prefetch_details(self, account: Account, rows: list[MatchListRow],
                          limit: int = 10) -> None:
        """直近の試合のスコアボードを保存しておく。ログインしていない
        (最新を取れない) 状態でも、サブアカウントの試合詳細を開けるように。
        失敗しても更新全体は失敗にしない。"""
        saved = self.history.load(account.id).get("details") or {}
        for row in rows[:limit]:
            if row.match_id in saved:
                continue
            try:
                self.match_detail(account, row.match_id)
            except Exception as exc:
                diagnostics.log(f"スコアボードの先読みに失敗: {exc}")
                break  # 認証できない等。残りも同じなので打ち切る

    def _refresh_skins(self, account: Account, ctx: "_RefreshContext",
                       progress: Progress) -> None:
        client = ctx.client()
        progress(f"{account.display_name}: 所持品を取得中…")
        old = account.inventory
        inv = InventoryInfo(
            skin_level_ids=list(old.skin_level_ids), agent_ids=list(old.agent_ids),
            buddy_ids=list(old.buddy_ids), card_ids=list(old.card_ids),
            title_ids=list(old.title_ids), spray_ids=list(old.spray_ids),
            updated_at=old.updated_at,
        )
        first_error: api.ApiError | None = None
        got_any = False
        for kind, attr in (("skins", "skin_level_ids"), ("agents", "agent_ids"),
                           ("buddies", "buddy_ids"), ("cards", "card_ids"),
                           ("titles", "title_ids"), ("sprays", "spray_ids")):
            try:
                setattr(inv, attr, client.entitlements(kind))
                got_any = True
            except api.ApiError as exc:
                first_error = first_error or exc
        if not got_any and first_error:
            raise first_error  # 全部失敗なら前回の所持品を残して失敗扱い
        inv.updated_at = time.time()
        account.inventory = inv

    def match_history(self, account: Account, count: int = 20) -> list[api.CompetitiveUpdate]:
        result = self.authenticate(account)
        try:
            version = self.content.client_version()
        except content.ContentError:
            version = ""
        client = api.ValorantApi(result, region=result.region or account.region,
                                 client_version=version)
        return client.competitive_history(count=count)

    # 保存する戦績の上限。古いものから捨てる。
    MAX_SAVED_MATCHES = 100

    def match_stats(self, account: Account, count: int = 20,
                    progress: Progress = _noop) -> MatchStats:
        """HS 率・エージェント別/マップ別勝率と試合一覧を返す。

        取得できた試合は、アカウントごとの JSON (history/<id>.json) に
        足していく。取得に失敗しても (そのアカウントにログインしていない、
        ネットワークが無い等)、保存済みの分があればそれを返し、失敗した
        理由は MatchStats.fetch_error に入れる。保存が一つも無いときだけ
        例外にする。

        取得元: HenrikDev API キーがあれば API モード (サインイン不要)、
        無ければローカル API のサインインモード (ログイン中のアカウントのみ)。
        """
        source = "api" if self.henrik_api_key else "local"
        fresh: list[MatchListRow] = []
        error: Exception | None = None
        try:
            if source == "api":
                fresh = self._fetch_rows_via_henrik(account, count, progress)
            else:
                fresh = self._fetch_rows_via_local_api(account, count, progress)
        except Exception as exc:  # 取れなくても保存済みを見せたいので広く受ける
            error = exc

        saved_rows, saved_at, _ = self._load_saved(account)
        if fresh:
            merged = _merge_rows(saved_rows, fresh)[: self.MAX_SAVED_MATCHES]
            saved_at = self._save_rows(account, merged, source)
        else:
            merged = saved_rows
        if not merged and error is not None:
            raise error

        stats = _aggregate_rows(merged, source)
        stats.saved_at = saved_at
        stats.fetch_error = str(error) if error is not None else ""
        return stats

    def saved_match_stats(self, account: Account) -> MatchStats | None:
        """保存済みの戦績だけを返す (通信しない)。無ければ None。
        画面を開いた瞬間に出して、最新の取得は裏で走らせるために使う。"""
        rows, saved_at, source = self._load_saved(account)
        if not rows:
            return None
        stats = _aggregate_rows(rows, source or "local")
        stats.saved_at = saved_at
        stats.from_saved = True
        return stats

    def _load_saved(self, account: Account) -> tuple[list[MatchListRow], float, str]:
        data = self.history.load(account.id)
        names = {f.name for f in fields(MatchListRow)}
        rows: list[MatchListRow] = []
        for raw in data.get("matches") or []:
            if isinstance(raw, dict):
                rows.append(MatchListRow(**{k: v for k, v in raw.items() if k in names}))
        rows.sort(key=lambda r: -r.started_at)
        return rows, float(data.get("saved_at") or 0.0), str(data.get("source") or "")

    def _save_rows(self, account: Account, rows: list[MatchListRow], source: str) -> float:
        now = time.time()
        keep = {r.match_id for r in rows}

        def mutate(data: dict) -> None:
            data["saved_at"] = now
            data["source"] = source
            data["matches"] = [asdict(r) for r in rows]
            # 一覧から落ちた試合のスコアボードも一緒に捨てる
            details = data.get("details") or {}
            data["details"] = {k: v for k, v in details.items() if k in keep}

        self.history.update(account.id, mutate)
        return now

    def _resolve_henrik_identity(self, account: Account) -> tuple[str, str]:
        """HenrikDev 向けの (puuid, region) を用意する。

        account.region はローカル API (Riot Client の chat/presence) 由来の
        値で、"jp1" のような HenrikDev が受け付けない形式のことがある
        (実機で確認済み: HenrikDev はリクエストを 400 で弾く)。
        api.REGIONS ("ap"/"na"/"eu"/"kr"/"latam"/"br") に無ければ、
        puuid が既に分かっていても Riot ID から HenrikDev 自身の account
        検索に投げ直して、HenrikDev が認識する正しい region を取り直す。
        """
        api_key = self.henrik_api_key
        puuid = account.puuid
        region = account.region or "ap"
        if not puuid or region not in api.REGIONS:
            if not account.riot_id:
                if puuid and region:
                    return puuid, region  # Riot ID が無ければ手も足も出ないのでそのまま試す
                raise ServiceError(
                    "Riot ID が未登録のため、API モードでは取得できません。"
                )
            name, _, tag = account.riot_id.partition("#")
            try:
                found = henrik.find_account(name.strip(), tag.strip(), api_key)
            except henrik.HenrikError as exc:
                raise ServiceError(str(exc)) from exc
            puuid = found.puuid or puuid
            region = found.region or region
        return puuid, region

    def _fetch_rows_via_henrik(self, account: Account, count: int,
                               progress: Progress) -> list[MatchListRow]:
        api_key = self.henrik_api_key
        puuid, region = self._resolve_henrik_identity(account)

        progress(f"{account.display_name}: 試合一覧を取得中… (API)")
        try:
            matches_raw = henrik.find_matches(puuid, region, api_key, count=count)
            history_raw = henrik.find_mmr_history(puuid, region, api_key)
        except henrik.HenrikError as exc:
            raise ServiceError(str(exc)) from exc
        rr_by_match = {h.get("match_id"): h for h in history_raw}

        try:
            agents = self.content.agents()
        except content.ContentError:
            agents = {}
        try:
            maps = self.content.maps()
        except content.ContentError:
            maps = {}

        rows: list[MatchListRow] = []
        for m in matches_raw:
            meta = m.get("metadata") or {}
            match_id = meta.get("match_id", "")
            map_info = meta.get("map") or {}
            all_players = m.get("players") or []
            player = next((p for p in all_players if p.get("puuid") == puuid), None)
            if not player:
                continue
            # 詳細スコアボード (クリックしたときに使う) を、取り直さずに
            # 使えるようセッション中だけ覚えておく。
            self._henrik_raw_matches[match_id] = m

            team_id = player.get("team_id")
            teams = m.get("teams") or []
            team = next((t for t in teams if t.get("team_id") == team_id), None)
            enemy_team = next((t for t in teams if t.get("team_id") != team_id), None)
            pstats = player.get("stats") or {}
            agent_info = player.get("agent") or {}
            damage = pstats.get("damage") or {}

            ranked = sorted(
                all_players, key=lambda p: (p.get("stats") or {}).get("score", 0),
                reverse=True,
            )
            placement = next(
                (i + 1 for i, p in enumerate(ranked) if p.get("puuid") == puuid), 0
            )

            rr_info = rr_by_match.get(match_id) or {}
            tier_info = rr_info.get("tier") or {}
            rows.append(MatchListRow(
                match_id=match_id, map_id=map_info.get("id", ""),
                started_at=_parse_iso_to_ms(meta.get("started_at")),
                tier_after=tier_info.get("id", 0),
                rr_after=rr_info.get("rr", 0),
                rr_earned=rr_info.get("last_change", 0),
                won=bool(team and team.get("won")),
                kills=pstats.get("kills", 0), deaths=pstats.get("deaths", 0),
                assists=pstats.get("assists", 0), score=pstats.get("score", 0),
                team_score=(team or {}).get("rounds", {}).get("won", 0),
                enemy_score=(enemy_team or {}).get("rounds", {}).get("won", 0),
                placement=placement,
                headshot_pct=_shots_to_pct(pstats),
                damage_delta=damage.get("dealt", 0) - damage.get("received", 0),
                agent_name=(agent_info.get("name")
                            or agents.get(agent_info.get("id", ""), {}).get("name")
                            or ""),
                map_name=(map_info.get("name")
                          or maps.get(map_info.get("id", ""), {}).get("name") or ""),
                headshots=pstats.get("headshots", 0),
                bodyshots=pstats.get("bodyshots", 0),
                legshots=pstats.get("legshots", 0),
                party=[
                    {"puuid": p.get("puuid", ""), "name": p.get("name", ""),
                     "tag": p.get("tag", "")}
                    for p in all_players
                    if player.get("party_id") and p.get("party_id") == player.get("party_id")
                    and p.get("team_id") == player.get("team_id") and p.get("puuid") != puuid
                ],
            ))
        return rows

    def _history_reader(self, account: Account) -> tuple["api.ValorantApi", str]:
        """戦績を読むための (クライアント, 対象アカウントの puuid) を返す。

        本人としてログインできるなら本人のトークンを使う。できなければ
        (別のアカウントがログイン中、セッション失効など)、いまログイン中の
        アカウントのトークンを借りる。ランク・試合一覧・試合詳細は、トークンの
        持ち主と関係なく、puuid / 試合 ID を指定すれば誰の分でも引けるため。
        """
        try:
            version = self.content.client_version()
        except content.ContentError:
            version = ""
        try:
            result = self.authenticate(account)
            target = result.puuid or account.puuid
        except Exception as own_error:
            if not account.puuid:
                raise  # 誰の戦績か特定できない (一度更新して puuid を取る必要がある)
            try:
                result = self._authenticate_via_local_api(account, allow_other=True)
            except Exception as borrow_error:
                # 借りられる人もいない (Riot Client が止まっている・ログアウト中)。
                # 「別のアカウントです」より、こちらの理由の方が実情に合う
                raise borrow_error from own_error
            target = account.puuid
        client = api.ValorantApi(result, region=account.region or result.region,
                                 client_version=version)
        return client, target

    def _fetch_rows_via_local_api(self, account: Account, count: int,
                                  progress: Progress) -> list[MatchListRow]:
        """競技試合一覧は毎回取り直す (安い、1 回の通信)。試合ごとの詳細は
        終わった試合の内容が変わることは無いので、一度取れた分はディスクの
        キャッシュに残し、次回はまだ持っていない (＝新しく増えた) 試合の
        分だけ通信する。
        """
        client, target = self._history_reader(account)
        matches = client.competitive_history(puuid=target, count=count)

        try:
            agents = self.content.agents()
        except content.ContentError:
            agents = {}
        try:
            maps = self.content.maps()
        except content.ContentError:
            maps = {}

        rows: list[MatchListRow] = []
        for i, m in enumerate(matches):
            summary = self.match_cache.get(target, m.match_id)
            if summary is None:
                progress(f"{account.display_name}: 試合 {i + 1}/{len(matches)} の詳細を取得中…")
                try:
                    summary = client.match_summary(m.match_id, puuid=target)
                except api.ApiError:
                    summary = None
                if summary:
                    self.match_cache.put(target, summary)
            if not summary:
                continue

            rows.append(MatchListRow(
                match_id=m.match_id, map_id=summary.map_id, started_at=m.started_at,
                tier_after=m.tier_after, rr_after=m.rr_after, rr_earned=m.rr_earned,
                won=summary.won, kills=summary.kills, deaths=summary.deaths,
                assists=summary.assists, score=summary.score,
                team_score=summary.team_score, enemy_score=summary.enemy_score,
                placement=summary.placement, headshot_pct=summary.headshot_pct,
                damage_delta=summary.damage_delta,
                agent_name=agents.get(summary.character_id, {}).get("name", ""),
                map_name=maps.get(summary.map_id, {}).get("name", ""),
                headshots=summary.headshots, bodyshots=summary.bodyshots,
                legshots=summary.legshots, party=summary.party,
            ))
        self.match_cache.save()
        return rows

    # -- 試合詳細 (スコアボード) ---------------------------------------------
    def match_detail(self, account: Account, match_id: str) -> MatchDetail:
        """1 試合分の全員のスコアボードを返す。

        保存済み (history/<id>.json) があればそれを返し、通信しない。終わった
        試合の内容は変わらないため。無ければ取得して保存する。モードの選び方は
        match_stats() と同じ (API キーがあれば API モード)。
        """
        saved = self._load_saved_detail(account, match_id)
        if saved is not None:
            return saved
        if self.henrik_api_key:
            detail = self._match_detail_via_henrik(account, match_id)
        else:
            detail = self._match_detail_via_local_api(account, match_id)
        self._save_detail(account, match_id, detail)
        return detail

    def _load_saved_detail(self, account: Account, match_id: str) -> MatchDetail | None:
        raw = (self.history.load(account.id).get("details") or {}).get(match_id)
        if not isinstance(raw, dict):
            return None
        try:
            names = {f.name for f in fields(MatchPlayerStat)}

            def players(key: str) -> list[MatchPlayerStat]:
                return [MatchPlayerStat(**{k: v for k, v in p.items() if k in names})
                        for p in raw.get(key) or []]

            detail_names = {f.name for f in fields(MatchDetail)}
            base = {k: v for k, v in raw.items()
                    if k in detail_names and k not in ("my_team", "enemy_team")}
            return MatchDetail(**base, my_team=players("my_team"),
                               enemy_team=players("enemy_team"))
        except (TypeError, AttributeError):
            return None  # 壊れた保存は無かったことにして取り直す

    def _save_detail(self, account: Account, match_id: str, detail: MatchDetail) -> None:
        def mutate(data: dict) -> None:
            details = data.get("details")
            if not isinstance(details, dict):
                details = data["details"] = {}
            details[match_id] = asdict(detail)

        try:
            self.history.update(account.id, mutate)
        except OSError:
            pass  # 保存できなくても表示はできる

    def _match_detail_via_henrik(self, account: Account, match_id: str) -> MatchDetail:
        api_key = self.henrik_api_key
        puuid, region = self._resolve_henrik_identity(account)

        m = self._henrik_raw_matches.get(match_id)
        if m is None:
            try:
                m = henrik.find_match(match_id, region, api_key)
            except henrik.HenrikError as exc:
                raise ServiceError(str(exc)) from exc
        if not m:
            raise ServiceError("試合データが見つかりませんでした。")

        try:
            agents = self.content.agents()
        except content.ContentError:
            agents = {}
        try:
            tiers = self.content.competitive_tiers()
        except content.ContentError:
            tiers = {}

        meta = m.get("metadata") or {}
        map_info = meta.get("map") or {}
        all_players = m.get("players") or []
        me = next((p for p in all_players if p.get("puuid") == puuid), None)
        my_team_id = me.get("team_id") if me else None
        teams = m.get("teams") or []
        my_team_info = next((t for t in teams if t.get("team_id") == my_team_id), None)
        enemy_team_info = next((t for t in teams if t.get("team_id") != my_team_id), None)

        def build(p: dict) -> MatchPlayerStat:
            pstats = p.get("stats") or {}
            damage = pstats.get("damage") or {}
            agent_info = p.get("agent") or {}
            tier_info = p.get("tier") or {}
            tier_id = tier_info.get("id", 0)
            tier_meta = tiers.get(tier_id, {})
            return MatchPlayerStat(
                puuid=p.get("puuid", ""), name=p.get("name", ""), tag=p.get("tag", ""),
                party_id=p.get("party_id", ""),
                agent_id=agent_info.get("id", ""),
                agent_name=(agent_info.get("name")
                           or agents.get(agent_info.get("id", ""), {}).get("name", "")),
                agent_icon=agents.get(agent_info.get("id", ""), {}).get("icon", ""),
                tier_name=tier_info.get("name") or tier_meta.get("name", ""),
                tier_icon=tier_meta.get("icon", ""),
                score=pstats.get("score", 0), kills=pstats.get("kills", 0),
                deaths=pstats.get("deaths", 0), assists=pstats.get("assists", 0),
                headshots=pstats.get("headshots", 0), bodyshots=pstats.get("bodyshots", 0),
                legshots=pstats.get("legshots", 0),
                damage_dealt=damage.get("dealt", 0), damage_received=damage.get("received", 0),
                is_self=p.get("puuid") == puuid,
            )

        my_team = sorted(
            (build(p) for p in all_players if p.get("team_id") == my_team_id),
            key=lambda ps: -ps.score,
        )
        enemy_team = sorted(
            (build(p) for p in all_players if p.get("team_id") != my_team_id),
            key=lambda ps: -ps.score,
        )
        return MatchDetail(
            match_id=meta.get("match_id", ""), map_name=map_info.get("name", ""),
            started_at=_parse_iso_to_ms(meta.get("started_at")),
            won=bool(my_team_info and my_team_info.get("won")),
            my_team_score=(my_team_info or {}).get("rounds", {}).get("won", 0),
            enemy_team_score=(enemy_team_info or {}).get("rounds", {}).get("won", 0),
            my_team=my_team, enemy_team=enemy_team,
        )

    def _match_detail_via_local_api(self, account: Account, match_id: str) -> MatchDetail:
        client, puuid = self._history_reader(account)
        data = client.match_details(match_id)
        if not data.get("players"):
            raise ServiceError("試合データが見つかりませんでした。")

        all_players = data.get("players") or []
        me = next((p for p in all_players if p.get("subject") == puuid), None)
        my_team_id = me.get("teamId") if me else None
        teams = data.get("teams") or []
        my_team_info = next((t for t in teams if t.get("teamId") == my_team_id), None)
        enemy_team_info = next((t for t in teams if t.get("teamId") != my_team_id), None)

        # ラウンドごとのダメージ内訳 (与/被とも) を全員分まとめて集計する。
        damage_dealt: dict[str, int] = {}
        damage_received: dict[str, int] = {}
        hs: dict[str, int] = {}
        bs: dict[str, int] = {}
        ls: dict[str, int] = {}
        for round_result in data.get("roundResults") or []:
            for player_stats in round_result.get("playerStats") or []:
                subject = player_stats.get("subject")
                for dmg in player_stats.get("damage") or []:
                    if subject:
                        damage_dealt[subject] = damage_dealt.get(subject, 0) + dmg.get("damage", 0)
                        hs[subject] = hs.get(subject, 0) + dmg.get("headshots", 0)
                        bs[subject] = bs.get(subject, 0) + dmg.get("bodyshots", 0)
                        ls[subject] = ls.get(subject, 0) + dmg.get("legshots", 0)
                    receiver = dmg.get("receiver")
                    if receiver:
                        damage_received[receiver] = damage_received.get(receiver, 0) + dmg.get("damage", 0)

        try:
            agents = self.content.agents()
        except content.ContentError:
            agents = {}
        try:
            tiers = self.content.competitive_tiers()
        except content.ContentError:
            tiers = {}
        try:
            maps = self.content.maps()
        except content.ContentError:
            maps = {}

        def build(p: dict) -> MatchPlayerStat:
            subject = p.get("subject", "")
            pstats = p.get("stats") or {}
            agent_id = p.get("characterId", "")
            agent_info = agents.get(agent_id, {})
            tier_info = tiers.get(p.get("competitiveTier", 0), {})
            return MatchPlayerStat(
                puuid=subject, name=p.get("gameName", ""), tag=p.get("tagLine", ""),
                party_id=p.get("partyId", ""),
                agent_id=agent_id, agent_name=agent_info.get("name", ""),
                agent_icon=agent_info.get("icon", ""),
                tier_name=tier_info.get("name", ""), tier_icon=tier_info.get("icon", ""),
                score=pstats.get("score", 0), kills=pstats.get("kills", 0),
                deaths=pstats.get("deaths", 0), assists=pstats.get("assists", 0),
                headshots=hs.get(subject, 0), bodyshots=bs.get(subject, 0),
                legshots=ls.get(subject, 0),
                damage_dealt=damage_dealt.get(subject, 0),
                damage_received=damage_received.get(subject, 0),
                is_self=subject == puuid,
            )

        my_team = sorted(
            (build(p) for p in all_players if p.get("teamId") == my_team_id),
            key=lambda ps: -ps.score,
        )
        enemy_team = sorted(
            (build(p) for p in all_players if p.get("teamId") != my_team_id),
            key=lambda ps: -ps.score,
        )
        match_info = data.get("matchInfo") or {}
        return MatchDetail(
            match_id=match_info.get("matchId", match_id),
            map_name=maps.get(match_info.get("mapId", ""), {}).get("name", ""),
            started_at=match_info.get("gameStartMillis", 0),
            won=bool(my_team_info and my_team_info.get("won")),
            my_team_score=(my_team_info or {}).get("numPoints", 0),
            enemy_team_score=(enemy_team_info or {}).get("numPoints", 0),
            my_team=my_team, enemy_team=enemy_team,
        )

    # -- 所持品の集計 -------------------------------------------------------
    def weapon_inventory(self, account: Account) -> list[WeaponGroup]:
        """所持スキンを武器 (ナイフ含む) ごとにまとめる。

        /weapons はスキンを武器ごとに内包して返すので、それをそのまま
        「この武器にはこのスキンがある」の一覧として使う。スキンを1つも
        持っていない武器は出さない。表示順は所持数が多い武器から。
        """
        try:
            weapons = self.content.weapons()
            tiers = self.content.content_tiers()
        except content.ContentError as exc:
            raise ServiceError(str(exc)) from exc

        level_to_ref: dict[str, tuple[str, str]] = {}
        for weapon_id, w in weapons.items():
            for skin_id, skin in w["skins"].items():
                for level_id in skin["levels"]:
                    level_to_ref[level_id] = (weapon_id, skin_id)

        groups = {
            weapon_id: WeaponGroup(weapon_id=weapon_id, name=w["name"],
                                   icon=w["icon"], category=w["category"])
            for weapon_id, w in weapons.items()
        }

        seen: set[tuple[str, str]] = set()
        for level_id in account.inventory.skin_level_ids:
            ref = level_to_ref.get(level_id)
            if not ref or ref in seen:
                continue
            seen.add(ref)
            weapon_id, skin_id = ref
            skin = weapons[weapon_id]["skins"][skin_id]
            tier = tiers.get(skin["tier"], {})
            groups[weapon_id].skins.append(WeaponSkin(
                name=skin["name"], icon=skin["icon"],
                tier_name=tier.get("name", "スタンダード"),
                tier_color=tier.get("color", "#8b8b8b"),
                tier_rank=tier.get("rank", 0),
            ))

        result = [g for g in groups.values() if g.skins]
        result.sort(key=lambda g: (-g.count, g.name))
        # エディション (contenttiers の rank) の低い順。同ランクなら名前順。
        for g in result:
            g.skins.sort(key=lambda s: (s.tier_rank, s.name))
        return result
