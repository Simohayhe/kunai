"""重い処理をバックグラウンドに逃がす。

切り替えも情報取得も数秒〜数十秒かかるので、UI スレッドで走らせると固まる。

QThreadPool ではなく素の threading.Thread を使う。実機で、QThreadPool に
渡した QRunnable が理由不明のまま完了シグナルを返さないことがある
(再現率はまちまちだが、更新確認が「確認中…」のまま固まる形で発現した)
のを確認したため。同じ処理を素の threading.Thread + Qt シグナルの
組み合わせで動かすと安定して完了する。
"""
from __future__ import annotations

import threading
import time
import traceback
from typing import Callable

from PySide6.QtCore import QObject, Signal

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
        finally:
            _ALIVE.discard(self)

    def _progress(self, message: str) -> None:
        self._emit(self.signals.progress, message)


def run(fn: Callable, *args, on_done: Callable | None = None,
        on_error: Callable | None = None, on_progress: Callable | None = None,
        wants_progress: bool = False, **kwargs) -> Task:
    task = Task(fn, *args, wants_progress=wants_progress or on_progress is not None,
                **kwargs)
    if on_done:
        task.signals.finished.connect(on_done)
    if on_error:
        task.signals.failed.connect(on_error)
    if on_progress:
        task.signals.progress.connect(on_progress)
    _ALIVE.add(task)
    threading.Thread(target=task.run, daemon=True).start()
    return task


def wait_for_all(timeout_ms: int = 5000) -> bool:
    """終了時に走行中のタスクを待つ。"""
    deadline = time.time() + timeout_ms / 1000
    while _ALIVE and time.time() < deadline:
        time.sleep(0.05)
    return not _ALIVE
