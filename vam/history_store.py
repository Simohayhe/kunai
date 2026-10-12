"""戦績の保存先。アカウントごとに1つの JSON ファイルを持つ。

    <app_dir>/history/<account.id>.json
    {"version": 1, "saved_at": 1790000000.0, "source": "api",
     "matches": [ {試合1行分}, ... ],
     "details": { "<match_id>": {スコアボード}, ... }}

一度取得した戦績・スコアボードはここに残し、次回以降はこれを先に表示する。
そのアカウントにログインしていなくても (取得に失敗しても) 保存済みの内容は
見られる。中身の (de)serialize は service 側が持ち、ここは JSON の読み書きと
排他だけを受け持つ。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Callable

VERSION = 1


class HistoryStore:
    def __init__(self, directory: Path):
        self.directory = directory
        # 戦績の取得 (一覧) とスコアボードの取得は別スレッドで並走するので、
        # 読み→書きの間に割り込まれて片方の更新が消えないよう直列化する。
        self._lock = threading.Lock()

    def path_for(self, account_id: str) -> Path:
        return self.directory / f"{account_id}.json"

    def load(self, account_id: str) -> dict:
        with self._lock:
            return self._read(account_id)

    def update(self, account_id: str, mutate: Callable[[dict], None]) -> None:
        """読み出して mutate で書き換え、そのまま保存する (原子的)。"""
        with self._lock:
            data = self._read(account_id)
            mutate(data)
            data["version"] = VERSION
            self._write(account_id, data)

    def delete(self, account_id: str) -> None:
        with self._lock:
            try:
                self.path_for(account_id).unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------
    def _read(self, account_id: str) -> dict:
        try:
            data = json.loads(self.path_for(account_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, account_id: str, data: dict) -> None:
        path = self.path_for(account_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
