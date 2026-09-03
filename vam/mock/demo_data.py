"""デモ用のサンプルアカウント。

VALORANT が無い環境で UI の見え方と操作を確認するためだけのもの。
実運用の保管庫には入らない (--demo は専用の保管庫を使う)。
"""
from __future__ import annotations

import time

from ..models import Account, InventoryInfo, RankInfo, WalletInfo
from ..riot.api import CompetitiveUpdate
from ..riot.content import ContentCache, ContentError
from ..storage import Vault
from .fake_riot import session_yaml

SAMPLES = [
    dict(label="メイン", riot_id="Simohaya#JP1", region="ap", color="#ff4655",
         favorite=True, tier=21, tier_name="アセンダント 1", rr=64, wins=38, games=71,
         peak=22, peak_name="アセンダント 2", vp=1275, rp=40, kc=8600,
         skins=142, agents=24, used_ago=1800, session_days=30),
    dict(label="サブ（練習用）", riot_id="Sub#0001", region="ap", color="#0fd8c2",
         tier=15, tier_name="プラチナ 1", rr=31, wins=12, games=27,
         peak=16, peak_name="プラチナ 2", vp=0, rp=10, kc=1200,
         skins=23, agents=19, used_ago=86400 * 3, session_days=30),
    dict(label="スマーフ", riot_id="Smurf#JP2", region="ap", color="#a05fd0",
         tier=9, tier_name="シルバー 1", rr=88, wins=5, games=8,
         peak=12, peak_name="ゴールド 1", vp=350, rp=0, kc=400,
         skins=4, agents=11, used_ago=86400 * 12, session_days=2),
    dict(label="NA 検証用", riot_id="NaTest#NA1", region="na", color="#f0a53a",
         tier=0, tier_name="Unranked", rr=0, wins=0, games=0,
         peak=0, peak_name="", vp=0, rp=0, kc=0,
         skins=0, agents=6, used_ago=0, session_days=0),
]

# 先頭アカウントの puuid。モック環境の「今ログイン中」と一致させる
FIRST_PUUID = "00000000-0000-0000-0000-000000000000"


def _puuid(index: int) -> str:
    return f"{index}0000000-0000-0000-0000-00000000000{index}"


def _skin_pool(vault: Vault) -> list[str]:
    """所持品タブが実際に集計できるよう、実在の skinLevel ID を使う。

    valorant-api.com は認証不要の公開データなので、デモでも本物を引ける。
    通信できないときは架空の ID にフォールバックする（集計結果は空になる）。
    """
    try:
        return list(ContentCache(vault.app_dir / "cache").skin_levels().keys())
    except ContentError:
        return []


def seed(vault: Vault) -> list[Account]:
    """サンプルアカウントを保管庫に入れる。既に何か入っていれば何もしない。"""
    if vault.accounts():
        return vault.accounts()

    pool = _skin_pool(vault)
    now = time.time()
    created = []
    for i, d in enumerate(SAMPLES):
        account = Account(
            label=d["label"], riot_id=d["riot_id"], region=d["region"],
            color=d["color"], favorite=d.get("favorite", False),
            puuid=FIRST_PUUID if i == 0 else _puuid(i),
            username=f"demo_user{i}",
            session_saved=bool(d["session_days"]),
            session_saved_at=now if d["session_days"] else 0,
            last_used_at=now - d["used_ago"] if d["used_ago"] else 0,
            rank=RankInfo(tier=d["tier"], tier_name=d["tier_name"], rr=d["rr"],
                          peak_tier=d["peak"], peak_tier_name=d["peak_name"],
                          wins=d["wins"], games=d["games"], updated_at=now),
            wallet=WalletInfo(vp=d["vp"], rp=d["rp"], kc=d["kc"], updated_at=now),
            inventory=InventoryInfo(
                skin_level_ids=(pool[i * 40:i * 40 + d["skins"]] if pool
                                else [f"skin-{i}-{n}" for n in range(d["skins"])]),
                agent_ids=[f"agent-{i}-{n}" for n in range(d["agents"])],
                updated_at=now,
            ),
        )
        vault.add(account)
        if d["session_days"]:
            vault.write_session_blob(
                account.id, "Data/RiotGamesPrivateSettings.yaml",
                session_yaml(account.puuid, d["session_days"]).encode("utf-8"),
            )
        created.append(account)
    return created


# 戦績タブの見た目確認用。実データは認証が要るのでデモでは使えない
SAMPLE_MATCHES = [
    ("/Game/Maps/Ascent/Ascent", 18, 21, 64, 0),
    ("/Game/Maps/Bonsai/Bonsai", -14, 21, 46, 1),
    ("/Game/Maps/Triad/Triad", 21, 21, 60, 2),
    ("/Game/Maps/Port/Port", 16, 20, 39, 3),
    ("/Game/Maps/Ascent/Ascent", -17, 20, 23, 4),
    ("/Game/Maps/Canyon/Canyon", 24, 20, 40, 5),
]


def sample_matches() -> list[CompetitiveUpdate]:
    now_ms = int(time.time() * 1000)
    return [
        CompetitiveUpdate(
            match_id=f"demo-{i}", map_id=map_id, season_id="demo",
            started_at=now_ms - days_ago * 86400_000,
            tier_after=tier, rr_after=rr, rr_earned=earned,
        )
        for i, (map_id, earned, tier, rr, days_ago) in enumerate(SAMPLE_MATCHES)
    ]
