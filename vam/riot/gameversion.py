"""VALORANT が最新版か (アップデートが済んでいるか) を調べる。

古いままだと、ランクなどが正しく取れないことがある (実機で確認: アップデートを
済ませたら取れるようになった)。インストールされている版は、Riot が置いている
製品のマニフェスト (valorant.live.manifest) の先頭にある「マニフェスト ID」で分かる。
最新の版は、Riot の公開設定 (clientconfig) の patch_url にある同じ ID。両方を比べる。
"""
from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path

import requests

CONFIG_URL = ("https://clientconfig.rpg.riotgames.com/api/v1/config/public"
              "?namespace=keystone.products.valorant.patchlines")
_ID_IN_URL = re.compile(r"/releases/([0-9A-Fa-f]{16})\.manifest")


def product_manifest_path() -> Path:
    base = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    return base / "Riot Games" / "Metadata" / "valorant.live" / "valorant.live.manifest"


def installed_manifest_id() -> str | None:
    """いまインストールされている VALORANT のマニフェスト ID。読めなければ None。"""
    try:
        with open(product_manifest_path(), "rb") as f:
            head = f.read(32)
    except OSError:
        return None
    if len(head) < 24 or head[:4] != b"RMAN":
        return None
    return format(struct.unpack_from("<Q", head, 16)[0], "016X")


def latest_manifest_id(region: str = "ap", timeout: float = 15.0) -> str | None:
    """いまの VALORANT (live) のマニフェスト ID。取れなければ None。"""
    try:
        resp = requests.get(CONFIG_URL, timeout=timeout)
        resp.raise_for_status()
        configs = (resp.json()["keystone.products.valorant.patchlines.live"]
                   ["platforms"]["win"]["configurations"])
    except (requests.RequestException, ValueError, KeyError):
        return None
    ordered = ([c for c in configs if c.get("id") == region]
               + [c for c in configs if c.get("id") != region])   # 地域が無くても同じ版のはず
    for config in ordered:
        m = _ID_IN_URL.search(config.get("patch_url") or "")
        if m:
            return m.group(1).upper()
    return None


@dataclass
class GameVersion:
    installed: str | None = None
    latest: str | None = None

    @property
    def outdated(self) -> bool:
        """両方分かっていて、違うときだけ「古い」(分からないときは警告しない)。"""
        return bool(self.installed and self.latest and self.installed != self.latest)


def check(region: str = "ap") -> GameVersion:
    return GameVersion(installed=installed_manifest_id(), latest=latest_manifest_id(region))
