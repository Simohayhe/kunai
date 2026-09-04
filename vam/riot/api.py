"""VALORANT 本体の API (pd.*.a.pvp.net)。

ローカル API か cookie 再認証で得たトークンを使って、
ランク・ウォレット・所持品・試合履歴を引く。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import requests

from .auth import AuthResult, client_platform_header

# リージョン -> シャード。
# VALORANT のシャードは na / eu / ap / kr の 4 つしかない。
# 一方 Riot Client のローカル API は LoL 由来のリージョン コード (jp1 など) を
# 返してくる。実機で確認したところ日本は "jp1" で、これは ap シャードに乗る。
# そのままホスト名に埋めると pd.jp1.a.pvp.net となり名前解決に失敗する。
REGION_TO_SHARD = {
    # VALORANT のリージョン表記
    "na": "na", "latam": "na", "br": "na",
    "eu": "eu", "ap": "ap", "kr": "kr", "pbe": "pbe",
    # Riot Client / LoL 由来のコード
    "na1": "na", "br1": "na", "la1": "na", "la2": "na",
    "euw1": "eu", "eun1": "eu", "tr1": "eu", "ru": "eu", "me1": "eu",
    "jp1": "ap", "oc1": "ap", "ph2": "ap", "sg2": "ap",
    "th2": "ap", "tw2": "ap", "vn2": "ap", "sea": "ap",
    # 数字なしで返ってくることもある。実機で "jp1" と "jp" の両方を観測した
    "jp": "ap", "oc": "ap", "ph": "ap", "sg": "ap", "th": "ap",
    "tw": "ap", "vn": "ap", "euw": "eu", "eun": "eu", "tr": "eu",
    "la": "na",
}
REGIONS = ("ap", "na", "eu", "kr", "latam", "br")


def shard_for(region: str) -> str:
    """リージョン表記からシャードを決める。未知なら ap に寄せる。"""
    region = (region or "").lower()
    if region in REGION_TO_SHARD:
        return REGION_TO_SHARD[region]
    # 末尾の数字を落として再照合 (jp1 -> jp のような表記ゆれ向け)
    trimmed = region.rstrip("0123456789")
    return REGION_TO_SHARD.get(trimmed, "ap")

# 所持品の種別 ID
ITEM_TYPE = {
    "agents": "01bb38e1-da47-4e6a-9b3d-945fe4655707",
    "contracts": "f85cb6f7-33e5-4dc8-b609-ec7212301948",
    "sprays": "d5f120f8-ff8c-4aac-92ea-f2b5acbe9475",
    "buddies": "dd3bf334-87f3-40bd-b043-682a57a8dc3a",
    "cards": "3f296c07-64c3-494c-923b-fe692a4fa1bd",
    "skins": "e7c63390-eda7-46e0-bb7a-a6abdacd2433",
    "skin_variants": "3ad1b2b2-acdb-4524-852f-954a76ddae0a",
    "titles": "de7caa6b-adf7-4588-bbd1-143831e786c6",
}

# ウォレットの通貨 ID
CURRENCY = {
    "vp": "85ad13f7-3d1b-5128-9eb2-7cd8ee0b5741",
    "rp": "e59aa87c-4cbf-517a-5983-6e81511be9b7",
    "kc": "85ca954a-41f2-ce94-9b45-8ca3dd39a00d",
}

TIER_NAMES = [
    "Unranked", "Unused1", "Unused2",
    "Iron 1", "Iron 2", "Iron 3",
    "Bronze 1", "Bronze 2", "Bronze 3",
    "Silver 1", "Silver 2", "Silver 3",
    "Gold 1", "Gold 2", "Gold 3",
    "Platinum 1", "Platinum 2", "Platinum 3",
    "Diamond 1", "Diamond 2", "Diamond 3",
    "Ascendant 1", "Ascendant 2", "Ascendant 3",
    "Immortal 1", "Immortal 2", "Immortal 3",
    "Radiant",
]


def tier_name(tier: int) -> str:
    if 0 <= tier < len(TIER_NAMES):
        name = TIER_NAMES[tier]
        return "Unranked" if name.startswith("Unused") else name
    return "Unranked"


class ApiError(Exception):
    pass


@dataclass
class CompetitiveUpdate:
    match_id: str = ""
    map_id: str = ""
    season_id: str = ""
    started_at: int = 0
    tier_before: int = 0
    tier_after: int = 0
    rr_before: int = 0
    rr_after: int = 0
    rr_earned: int = 0

    @property
    def tier_after_name(self) -> str:
        return tier_name(self.tier_after)


@dataclass
class MmrSnapshot:
    tier: int = 0
    rr: int = 0
    wins: int = 0
    games: int = 0
    leaderboard_rank: int = 0
    peak_tier: int = 0
    peak_season: str = ""
    season_id: str = ""
    history: list[CompetitiveUpdate] = field(default_factory=list)

    @property
    def tier_label(self) -> str:
        return tier_name(self.tier)

    @property
    def peak_tier_label(self) -> str:
        return tier_name(self.peak_tier)


class ValorantApi:
    def __init__(self, auth: AuthResult, region: str = "ap",
                 client_version: str = "", session: requests.Session | None = None,
                 timeout: float = 15.0):
        self.auth = auth
        self.region = (region or "ap").lower()
        self.shard = shard_for(self.region)
        self.client_version = client_version
        self.timeout = timeout
        self._session = session or requests.Session()

    # -- 低レベル -----------------------------------------------------------
    @property
    def pd(self) -> str:
        return f"https://pd.{self.shard}.a.pvp.net"

    def _headers(self) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.auth.access_token}",
            "X-Riot-Entitlements-JWT": self.auth.entitlements_token,
            "X-Riot-ClientPlatform": client_platform_header(),
        }
        if self.client_version:
            h["X-Riot-ClientVersion"] = self.client_version
        return h

    def _request(self, method: str, url: str, **kw):
        resp = self._session.request(
            method, url, headers=self._headers(), timeout=self.timeout, **kw
        )
        if resp.status_code == 400:
            raise ApiError("トークンが無効です。セッションを取り直してください")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise ApiError(f"{url.split('.net')[-1]} が {resp.status_code} を返しました")
        try:
            return resp.json()
        except ValueError:
            raise ApiError("応答が JSON ではありません")

    def _get(self, path: str):
        return self._request("GET", self.pd + path)

    # -- ランク -------------------------------------------------------------
    def mmr(self, puuid: str | None = None) -> MmrSnapshot:
        puuid = puuid or self.auth.puuid
        data = self._get(f"/mmr/v1/players/{puuid}") or {}
        snap = MmrSnapshot()

        comp = ((data.get("QueueSkills") or {}).get("competitive") or {})
        seasons = comp.get("SeasonalInfoBySeasonID") or {}

        latest = data.get("LatestCompetitiveUpdate") or {}
        snap.season_id = latest.get("SeasonID", "")

        current = seasons.get(snap.season_id) if snap.season_id else None
        if current is None and seasons:
            # 最新シーズンが特定できなければ、試合数が最も多いものを現在として扱う
            current = max(seasons.values(), key=lambda s: s.get("NumberOfGames", 0))
        if current:
            snap.tier = current.get("CompetitiveTier", 0)
            snap.rr = current.get("RankedRating", 0)
            snap.wins = current.get("NumberOfWins", 0)
            snap.games = current.get("NumberOfGames", 0)
            snap.leaderboard_rank = current.get("LeaderboardRank", 0) or 0

        for season_id, info in seasons.items():
            tiers = info.get("WinsByTier") or {}
            best = max((int(t) for t in tiers), default=info.get("CompetitiveTier", 0))
            if best > snap.peak_tier:
                snap.peak_tier = best
                snap.peak_season = season_id
        return snap

    def competitive_history(self, puuid: str | None = None,
                            count: int = 20) -> list[CompetitiveUpdate]:
        puuid = puuid or self.auth.puuid
        data = self._get(
            f"/mmr/v1/players/{puuid}/competitiveupdates"
            f"?startIndex=0&endIndex={count}&queue=competitive"
        ) or {}
        out = []
        for m in data.get("Matches", []):
            out.append(CompetitiveUpdate(
                match_id=m.get("MatchID", ""),
                map_id=m.get("MapID", ""),
                season_id=m.get("SeasonID", ""),
                started_at=m.get("MatchStartTime", 0),
                tier_before=m.get("TierBeforeUpdate", 0),
                tier_after=m.get("TierAfterUpdate", 0),
                rr_before=m.get("RankedRatingBeforeUpdate", 0),
                rr_after=m.get("RankedRatingAfterUpdate", 0),
                rr_earned=m.get("RankedRatingEarned", 0),
            ))
        return out

    # -- ウォレット / 所持品 -----------------------------------------------
    def wallet(self, puuid: str | None = None) -> dict[str, int]:
        puuid = puuid or self.auth.puuid
        data = self._get(f"/store/v1/wallet/{puuid}") or {}
        balances = data.get("Balances") or {}
        return {name: balances.get(cid, 0) for name, cid in CURRENCY.items()}

    def entitlements(self, kind: str, puuid: str | None = None) -> list[str]:
        puuid = puuid or self.auth.puuid
        type_id = ITEM_TYPE.get(kind)
        if not type_id:
            raise ApiError(f"未知の所持品種別: {kind}")
        data = self._get(f"/store/v1/entitlements/{puuid}/{type_id}") or {}
        return [e.get("ItemID", "") for e in data.get("Entitlements", []) if e.get("ItemID")]

    def storefront(self, puuid: str | None = None) -> dict:
        puuid = puuid or self.auth.puuid
        return self._request("POST", f"{self.pd}/store/v3/storefront/{puuid}", json={}) or {}

    # -- 名前解決 -----------------------------------------------------------
    def names(self, puuids: list[str]) -> dict[str, str]:
        data = self._request("PUT", f"{self.pd}/name-service/v2/players", json=puuids) or []
        return {
            e.get("Subject", ""): f"{e.get('GameName','')}#{e.get('TagLine','')}"
            for e in data
        }
