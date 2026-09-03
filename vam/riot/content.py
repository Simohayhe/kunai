"""valorant-api.com からのゲーム内容データ。

ID しか返ってこない所持品 API の結果を、人が読める名前と画像に変換するために使う。
認証不要・公開データなので、取得したものはディスクにキャッシュして使い回す。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

BASE = "https://valorant-api.com/v1"
CACHE_TTL = 7 * 86400  # コンテンツは頻繁には変わらない


class ContentError(Exception):
    pass


class ContentCache:
    def __init__(self, cache_dir: Path, language: str = "ja-JP", timeout: float = 20.0):
        self.cache_dir = Path(cache_dir)
        self.language = language
        self.timeout = timeout
        self._memory: dict[str, object] = {}

    # -- 取得とキャッシュ ---------------------------------------------------
    def _cache_file(self, key: str) -> Path:
        return self.cache_dir / f"content-{key}-{self.language}.json"

    def fetch(self, key: str, endpoint: str, params: dict | None = None,
              force: bool = False) -> object:
        if not force and key in self._memory:
            return self._memory[key]

        f = self._cache_file(key)
        if not force and f.is_file() and time.time() - f.stat().st_mtime < CACHE_TTL:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                self._memory[key] = data
                return data
            except ValueError:
                pass

        query = {"language": self.language, **(params or {})}
        try:
            resp = requests.get(f"{BASE}{endpoint}", params=query, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json().get("data")
        except (requests.RequestException, ValueError) as exc:
            # 通信できないときは期限切れキャッシュでも使う
            if f.is_file():
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    self._memory[key] = data
                    return data
                except ValueError:
                    pass
            raise ContentError(f"コンテンツ取得に失敗しました: {exc}") from exc

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self._memory[key] = data
        return data

    # -- 個別データ ---------------------------------------------------------
    def client_version(self) -> str:
        data = self.fetch("version", "/version")
        return (data or {}).get("riotClientVersion", "")

    def skin_levels(self) -> dict[str, dict]:
        """skinLevel の uuid -> {name, icon}"""
        data = self.fetch("skinlevels", "/weapons/skinlevels") or []
        return {
            s["uuid"]: {"name": s.get("displayName", ""), "icon": s.get("displayIcon")}
            for s in data
        }

    def skins(self) -> dict[str, dict]:
        """skin uuid -> {name, tier, levels[]}。レア度の集計に使う。"""
        data = self.fetch("skins", "/weapons/skins") or []
        out = {}
        for s in data:
            out[s["uuid"]] = {
                "name": s.get("displayName", ""),
                "tier": s.get("contentTierUuid") or "",
                "icon": s.get("displayIcon"),
                "levels": [lv["uuid"] for lv in (s.get("levels") or [])],
            }
        return out

    def content_tiers(self) -> dict[str, dict]:
        data = self.fetch("contenttiers", "/contenttiers") or []
        return {
            t["uuid"]: {
                "name": t.get("displayName", ""),
                "rank": t.get("rank", 0),
                "color": "#" + (t.get("highlightColor") or "ffffffff")[:6],
                "icon": t.get("displayIcon"),
            }
            for t in data
        }

    def agents(self) -> dict[str, dict]:
        data = self.fetch("agents", "/agents", {"isPlayableCharacter": "true"}) or []
        return {
            a["uuid"]: {"name": a.get("displayName", ""), "icon": a.get("displayIconSmall")}
            for a in data
        }

    def maps(self) -> dict[str, dict]:
        data = self.fetch("maps", "/maps") or []
        return {
            m["mapUrl"]: {"name": m.get("displayName", ""), "icon": m.get("splash")}
            for m in data
        }

    def competitive_tiers(self) -> dict[int, dict]:
        """tier 番号 -> {name, icon, color}。最新のティア表のみ使う。"""
        data = self.fetch("competitivetiers", "/competitivetiers") or []
        if not data:
            return {}
        latest = data[-1]
        out = {}
        for t in latest.get("tiers", []):
            out[t["tier"]] = {
                "name": t.get("tierName", ""),
                "icon": t.get("smallIcon") or t.get("largeIcon"),
                "color": "#" + (t.get("color") or "ffffffff")[:6],
            }
        return out

    def tier_names(self) -> dict[int, str]:
        """tier 番号 -> 表示名。取れなければ空 dict（呼び側が英語表記に落とす）。"""
        try:
            return {t: info["name"] for t, info in self.competitive_tiers().items()
                    if info.get("name")}
        except ContentError:
            return {}

    def skin_level_to_skin(self) -> dict[str, str]:
        """skinLevel uuid -> skin uuid。所持品からレア度を数えるための逆引き。"""
        mapping = {}
        for skin_id, info in self.skins().items():
            for level_id in info["levels"]:
                mapping[level_id] = skin_id
        return mapping
