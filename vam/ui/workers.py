"""重い処理をバックグラウンドに逃がす。

切り替えも情報取得も数秒〜数十秒かかるので、UI スレッドで走らせると固まる。
"""
from __future__ import annotations

import traceback
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

# 走行中の Task を掴んでおく。ここに残さないと Python 側の参照が消えて
# GC され、スレッドが動いている最中にシグナル送出元が破棄される。
_ALIVE: set["Task"] = set()


class _Signals(QObject):
    progress = Signal(str)
    finished = Signal(object)
    failed = Signal(str)


class Task(QRunnable):
    """関数を 1 個バックグラウンドで走らせる。

    関数の第一引数に進捗コールバックを渡したい場合は wants_progress=True。
    """

    def __init__(self, fn: Callable, *args, wants_progress: bool = False, **kwargs):
        super().__init__()
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

    @Slot()
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
    QThreadPool.globalInstance().start(task)
    return task


def wait_for_all(timeout_ms: int = 5000) -> bool:
    """終了時に走行中のタスクを待つ。"""
    return QThreadPool.globalInstance().waitForDone(timeout_ms)
