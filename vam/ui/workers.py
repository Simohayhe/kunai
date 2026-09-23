"""重い処理をバックグラウンドに逃がす。

切り替えも情報取得も数秒〜数十秒かかるので、UI スレッドで走らせると固まる。
QThreadPool ではなく素の threading.Thread + Qt シグナルの組み合わせで動かす
(QThreadPool の QRunnable が理由不明のまま完了シグナルを返さないことが
実機であったため)。

signal.connect() は必ず Qt.QueuedConnection を明示する。また _ALIVE から
の後始末は、emit() 直後 (バックグラウンドスレッド側) ではなく、
finished/failed が実際に配送された後 (メインスレッド側) でやる。
キュー接続の emit() は「積むだけ」で即座に返るので、その直後に
_ALIVE.discard() すると、メインスレッドがまだキューを処理していない
うちに Task (と Qt 側の _Signals) への唯一の強参照が消え、GC で回収されて
しまうことがあった。実機のビルドで、渡した完了コールバックが一度も呼ばれず
「確認中…」のまま固まる形で発現し、再現率はまちまちだった。
"""
from __future__ import annotations

import threading
import time
import traceback
from typing import Callable

from PySide6.QtCore import QObject, Qt, Signal

# 走行中の Task を掴んでおく。ここに残さないと Python 側の参照が消えて
# GC され、スレッドが動いている最中にシグナル送出元が破棄される。
_ALIVE: set["Task"] = set()


class _Signals(QObject):
    progress = Signal(str)
    finished = Signal(object)
    failed = Signal(str)


class Task:
    """関数を 1 個バックグラウンドで走らせる。

    関数の第一引数に進捗コールバックを渡したい場合は wants_progress=True。
    """

    def __init__(self, fn: Callable, *args, wants_progress: bool = False, **kwargs):
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.wants_progress = wants_progress
        self.signals = _Signals()

    def _emit(self, signal, payload) -> None:
        # ウィンドウが閉じた後などは受け手が消えている。落とさずに黙って捨てる。
        try:
            signal.emit(payload)
        except RuntimeError:
            pass

    def run(self) -> None:
        try:
            if self.wants_progress:
                self.kwargs["progress"] = self._progress
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:  # UI を落とさないため広く捕まえる
            traceback.print_exc()
            self._emit(self.signals.failed, str(exc) or exc.__class__.__name__)
        else:
            self._emit(self.signals.finished, result)
        # _ALIVE の後始末はメインスレッド側 (finished/failed 配送後) でやる。
        # ここではまだ触らない (モジュール先頭のコメント参照)。

    def _progress(self, message: str) -> None:
        self._emit(self.signals.progress, message)


def run(fn: Callable, *args, on_done: Callable | None = None,
        on_error: Callable | None = None, on_progress: Callable | None = None,
        wants_progress: bool = False, **kwargs) -> Task:
    task = Task(fn, *args, wants_progress=wants_progress or on_progress is not None,
                **kwargs)
    if on_done:
        task.signals.finished.connect(on_done, Qt.QueuedConnection)
    if on_error:
        task.signals.failed.connect(on_error, Qt.QueuedConnection)
    if on_progress:
        task.signals.progress.connect(on_progress, Qt.QueuedConnection)
    # ユーザーのコールバックより後に繋いでおく。同じシグナルへのキュー接続は
    # 繋いだ順に配送されるので、後始末は必ず on_done/on_error の後に走る。
    task.signals.finished.connect(lambda _r: _ALIVE.discard(task), Qt.QueuedConnection)
    task.signals.failed.connect(lambda _m: _ALIVE.discard(task), Qt.QueuedConnection)
    _ALIVE.add(task)
    threading.Thread(target=task.run, daemon=True).start()
    return task


def wait_for_all(timeout_ms: int = 5000) -> bool:
    """終了時に走行中のタスクを待つ。"""
    deadline = time.time() + timeout_ms / 1000
    while _ALIVE and time.time() < deadline:
        time.sleep(0.05)
    return not _ALIVE
