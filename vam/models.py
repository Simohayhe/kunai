from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict


@dataclass
class RankInfo:
    """直近に取得したランク情報のキャッシュ。"""
    tier: int = 0                 # 0=Unranked, 3=Iron1 ... 27=Radiant
    tier_name: str = "Unranked"
    rr: int = 0                   # ランクレーティング 0-100
    peak_tier: int = 0
    peak_tier_name: str = ""
    peak_season: str = ""
    leaderboard_rank: int = 0
    wins: int = 0
    games: int = 0
    updated_at: float = 0.0

    @property
    def is_stale(self) -> bool:
        return time.time() - self.updated_at > 3600


@dataclass
class WalletInfo:
    vp: int = 0        # VALORANT Point
    rp: int = 0        # Radianite Point
    kc: int = 0        # Kingdom Credit
    updated_at: float = 0.0


@dataclass
class InventoryInfo:
    skin_level_ids: list[str] = field(default_factory=list)
    agent_ids: list[str] = field(default_factory=list)
    buddy_ids: list[str] = field(default_factory=list)
    card_ids: list[str] = field(default_factory=list)
    title_ids: list[str] = field(default_factory=list)
    spray_ids: list[str] = field(default_factory=list)
    updated_at: float = 0.0

    @property
    def skin_count(self) -> int:
        return len(self.skin_level_ids)


@dataclass
class Account:
    """管理対象のアカウント 1 件。

    password は保管庫ごと暗号化されて保存される。session_saved が True なら
    Riot Client のログイン済みセッションのコピーを持っているので、
    パスワードなしで切り替えできる。
    """
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    label: str = ""                     # 表示名。ユーザーが自由に付ける
    username: str = ""                  # Riot ログイン ID
    password: str = ""                  # 自動入力用。任意
    riot_id: str = ""                   # ゲーム内 ID  Name#TAG
    puuid: str = ""
    region: str = "ap"                  # ap / na / eu / kr / latam / br
    note: str = ""
    tags: list[str] = field(default_factory=list)
    color: str = "#ff4655"
    favorite: bool = False
    session_saved: bool = False
    session_saved_at: float = 0.0
    last_used_at: float = 0.0
    created_at: float = field(default_factory=time.time)
    rank: RankInfo = field(default_factory=RankInfo)
    wallet: WalletInfo = field(default_factory=WalletInfo)
    inventory: InventoryInfo = field(default_factory=InventoryInfo)

    @property
    def display_name(self) -> str:
        return self.label or self.riot_id or self.username or self.id

    @property
    def game_name(self) -> str:
        return self.riot_id.split("#", 1)[0] if "#" in self.riot_id else self.riot_id

    @property
    def tag_line(self) -> str:
        return self.riot_id.split("#", 1)[1] if "#" in self.riot_id else ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Account":
        data = dict(data)
        data["rank"] = RankInfo(**(data.get("rank") or {}))
        data["wallet"] = WalletInfo(**(data.get("wallet") or {}))
        data["inventory"] = InventoryInfo(**(data.get("inventory") or {}))
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})
