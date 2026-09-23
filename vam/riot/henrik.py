"""HenrikDev API (https://api.henrikdev.xyz) — 他プレイヤーの検索用。

自分のセッションのアクセストークンでは、他プレイヤーの Riot ID
(name#tag) から puuid への変換ができないことを実機で確認した
(pd.*.a.pvp.net の name-service は puuid→name の逆引きにしか使えない)。
公式の Riot Developer API キーでも同様に、VALORANT の試合/ランクデータ
自体は提供されていない。そのため、この用途では第三者の公開 API である
HenrikDev API を使う。

要 API キー (https://api.henrikdev.xyz/dashboard/ で発行)。
エンドポイントとヘッダ形式は同じ作者の別アプリ HiyokoSwitcher の実装
(electron/main.ts) を参考にしている。
"""
from __future__ import annotations

from dataclasses import dataclass

import requests

BASE = "https://api.henrikdev.xyz/valorant"


class HenrikError(Exception):
    pass


@dataclass
class PlayerAccount:
    puuid: str
    name: str
    tag: str
    region: str
    account_level: int = 0
    card_icon: str = ""

    @property
    def riot_id(self) -> str:
        return f"{self.name}#{self.tag}"


@dataclass
class PlayerRank:
    tier_name: str = "Unranked"
    rr: int = 0
    elo: int = 0
    icon: str = ""


def _headers(api_key: str) -> dict[str, str]:
    # HenrikDev はキーをそのまま Authorization ヘッダに入れる (Bearer 無し)
    return {"Authorization": api_key, "Accept": "application/json"}


def _get(path: str, api_key: str, timeout: float = 15.0) -> dict:
    if not api_key:
        raise HenrikError(
            "HenrikDev API キーが設定されていません。設定画面で登録してください "
            "(https://api.henrikdev.xyz/dashboard/ で発行できます)。"
        )
    try:
        resp = requests.get(f"{BASE}{path}", headers=_headers(api_key), timeout=timeout)
    except requests.RequestException as exc:
        raise HenrikError(f"HenrikDev API に接続できませんでした: {exc}") from exc

    if resp.status_code == 404:
        raise HenrikError("そのプレイヤーが見つかりませんでした")
    if resp.status_code in (401, 403):
        raise HenrikError("HenrikDev API キーが無効です")
    if resp.status_code == 429:
        raise HenrikError("HenrikDev API のレート制限に達しました。しばらく待ってから試してください")
    if resp.status_code >= 400:
        raise HenrikError(f"HenrikDev API が {resp.status_code} を返しました")

    try:
        body = resp.json()
    except ValueError as exc:
        raise HenrikError("HenrikDev API の応答が JSON ではありません") from exc
    return body.get("data") or {}


def find_account(name: str, tag: str, api_key: str) -> PlayerAccount:
    data = _get(f"/v1/account/{name}/{tag}", api_key)
    if not data:
        raise HenrikError("そのプレイヤーが見つかりませんでした")
    card = data.get("card") or {}
    return PlayerAccount(
        puuid=data.get("puuid", ""),
        name=data.get("name", name),
        tag=data.get("tag", tag),
        region=data.get("region", ""),
        account_level=data.get("account_level", 0),
        card_icon=card.get("small", ""),
    )


def find_rank(name: str, tag: str, region: str, api_key: str) -> PlayerRank:
    data = _get(f"/v1/mmr/{region}/{name}/{tag}", api_key)
    images = data.get("images") or {}
    return PlayerRank(
        tier_name=data.get("currenttierpatched") or "Unranked",
        rr=data.get("ranking_in_tier", 0),
        elo=data.get("elo", 0),
        icon=images.get("small") or images.get("large") or "",
    )


def search(riot_id: str, api_key: str) -> tuple[PlayerAccount, PlayerRank]:
    """"Name#Tag" を渡して、アカウント情報とランクをまとめて引く。"""
    name, _, tag = riot_id.partition("#")
    name, tag = name.strip(), tag.strip()
    if not name or not tag:
        raise HenrikError("Riot ID は Name#TAG の形式で入力してください")
    account = find_account(name, tag, api_key)
    rank = find_rank(name, tag, account.region or "ap", api_key)
    return account, rank
