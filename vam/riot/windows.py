"""Riot Client のウィンドウを、画面に出さない (最小化する) ための小道具。

「すべて更新」やストアの自動取得は、Riot Client を起動してトークンを借りるだけで、
画面を見る必要はない。起動したウィンドウを、フォーカスを奪わずに最小化しておく
(最小化なら Riot Client 側の動作に影響しない。完全に隠す方法は、実機で確かめて
いないので採らない)。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time


from . import autologin

user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None

SW_SHOWMINNOACTIVE = 7   # 最小化する。アクティブにはしない


def riot_window_handles() -> list[int]:
    """Riot Client の UI プロセスが持つ、見えていて最小化されていない最上位ウィンドウ。"""
    if user32 is None:
        return []
    pids = autologin._ux_pids()
    if not pids:
        return []
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            rect = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            # 画面に出ているウィンドウだけ (幅・高さが無い内部ウィンドウは触らない)
            if rect.right - rect.left > 100 and rect.bottom - rect.top > 100:
                found.append(hwnd)
        return True

    user32.EnumWindows(callback, 0)
    return found


def minimize_riot_windows(enumerate_fn=riot_window_handles, show_fn=None) -> int:
    """見つかった Riot Client のウィンドウを最小化する。最小化した数を返す。"""
    show = show_fn or (lambda hwnd: user32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE))
    count = 0
    for hwnd in enumerate_fn():
        show(hwnd)
        count += 1
    return count


def keep_minimized(stop: threading.Event, poll: float = 0.05) -> threading.Thread:
    """stop が立つまで、Riot Client のウィンドウが出てくるたびに最小化し続ける。"""
    def run() -> None:
        while not stop.is_set():
            try:
                minimize_riot_windows()
            except Exception:
                pass  # 窓の列挙に失敗しても、更新そのものは止めない
            time.sleep(poll)

    thread = threading.Thread(target=run, name="riot-window-minimizer", daemon=True)
    thread.start()
    return thread
