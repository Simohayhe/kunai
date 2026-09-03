"""ランクアイコンやスキン画像の遅延読み込み。

valorant-api.com の画像 URL をディスクにキャッシュし、
読み込めたらシグナルで通知して差し替える。UI は待たない。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import requests
from PySide6.QtCore import QObject, QThreadPool, QRunnable, Signal, Slot
from PySide6.QtGui import QPixmap

# workers.py と同じ理由で走行中の取得タスクを保持する
_ALIVE: set["_Fetch"] = set()


class _Fetch(QRunnable):
    def __init__(self, url: str, target: Path, done: Signal):
        super().__init__()
        self.url = url
        self.target = target
        self.done = done

    @Slot()
    def run(self) -> None:
        try:
            resp = requests.get(self.url, timeout=20)
            resp.raise_for_status()
            self.target.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.target.with_suffix(".part")
            tmp.write_bytes(resp.content)
            tmp.replace(self.target)
        except (requests.RequestException, OSError):
            return
        finally:
            _ALIVE.discard(self)
        try:
            self.done.emit(self.url, str(self.target))
        except RuntimeError:
            pass   # ウィンドウが閉じた後


class IconLoader(QObject):
    """URL -> QPixmap。取得済みならその場で返し、未取得なら loaded シグナルで後追い。"""

    loaded = Signal(str, str)   # url, ローカルパス

    def __init__(self, cache_dir: Path):
        super().__init__()
        self.cache_dir = Path(cache_dir)
        self._pending: set[str] = set()
        self._pixmaps: dict[str, QPixmap] = {}

    def _path_for(self, url: str) -> Path:
        digest = hashlib.sha1(url.encode()).hexdigest()[:20]
        suffix = ".png" if ".png" in url.lower() else ".img"
        return self.cache_dir / f"{digest}{suffix}"

    def pixmap(self, url: str | None) -> QPixmap | None:
        """キャッシュにあれば QPixmap を返す。無ければ取得を始めて None を返す。"""
        if not url:
            return None
        if url in self._pixmaps:
            return self._pixmaps[url]

        path = self._path_for(url)
        if path.is_file():
            pm = QPixmap(str(path))
            if not pm.isNull():
                self._pixmaps[url] = pm
                return pm

        if url not in self._pending:
            self._pending.add(url)
            fetch = _Fetch(url, path, self.loaded)
            _ALIVE.add(fetch)
            QThreadPool.globalInstance().start(fetch)
        return None

    def take(self, url: str, path: str) -> QPixmap | None:
        """loaded シグナル受信後に呼ぶ。"""
        self._pending.discard(url)
        pm = QPixmap(path)
        if pm.isNull():
            return None
        self._pixmaps[url] = pm
        return pm
