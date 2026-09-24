"""試合ごとの詳細 (MatchSummary) をアカウント (puuid) ごとにキャッシュする。

戦績の集計は「直近の試合一覧 (安い、1回の通信)」+「試合ごとの詳細
(高い、件数分だけ通信が増える)」でできている。詳細は試合が終われば
内容が変わらないので、一度取れた分はディスクに残しておき、次回は
一覧に新しく増えた試合の分だけ取りに行けば済む。
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .riot.api import MatchSummary


class MatchCache:
    def __init__(self, cache_dir: Path):
        self.path = cache_dir / "match_summaries.json"
        self._data: dict[str, dict[str, dict]] = {}
        self._loaded = False
        self._dirty = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._data = {}

    def get(self, puuid: str, match_id: str) -> MatchSummary | None:
        self._load()
        raw = self._data.get(puuid, {}).get(match_id)
        if raw is None:
            return None
        try:
            return MatchSummary(**raw)
        except TypeError:
            return None  # 古い形式が残っていたら無視して取り直す

    def put(self, puuid: str, summary: MatchSummary) -> None:
        self._load()
        self._data.setdefault(puuid, {})[summary.match_id] = asdict(summary)
        self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
        self._dirty = False
