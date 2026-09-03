"""パスワード自動入力によるログイン (セッション復元が使えないときのフォールバック)。

Riot Client のログイン画面にキー入力を送り込む。
Riot が画面構成を変えると壊れる方式なので、あくまで保険。
通常は session.py のセッション復元を使うこと。

キーは KEYEVENTF_UNICODE で送るので、日本語キーボードでも記号が化けない。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time
from dataclasses import dataclass

import psutil

user32 = ctypes.windll.user32

# ログイン画面のウィンドウ
LOGIN_WINDOW_CLASS = "RCLIENT"
LOGIN_WINDOW_TITLES = ("Riot Client", "リオットクライアント")
UX_PROCESS = "RiotClientUx.exe"

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
INPUT_KEYBOARD = 1
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_BACK = 0x08
VK_CONTROL = 0x11
VK_A = 0x41


class AutoLoginError(Exception):
    pass


# --------------------------------------------------------------------------
# SendInput
# --------------------------------------------------------------------------
class _KeyBdInput(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KeyBdInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("union", _InputUnion)]


def _send(inputs: list[_Input]) -> None:
    if not inputs:
        return
    arr = (_Input * len(inputs))(*inputs)
    user32.SendInput(len(inputs), ctypes.byref(arr), ctypes.sizeof(_Input))


def _key_event(vk: int = 0, scan: int = 0, flags: int = 0) -> _Input:
    return _Input(INPUT_KEYBOARD, _InputUnion(_KeyBdInput(vk, scan, flags, 0, None)))


def press(vk: int, modifiers: tuple[int, ...] = ()) -> None:
    seq = [_key_event(vk=m) for m in modifiers]
    seq.append(_key_event(vk=vk))
    seq.append(_key_event(vk=vk, flags=KEYEVENTF_KEYUP))
    seq += [_key_event(vk=m, flags=KEYEVENTF_KEYUP) for m in reversed(modifiers)]
    _send(seq)


def type_text(text: str, delay: float = 0.012) -> None:
    """1 文字ずつ Unicode で送る。IME やキーボード配列の影響を受けない。"""
    for ch in text:
        for code in _utf16_units(ch):
            _send([_key_event(scan=code, flags=KEYEVENTF_UNICODE),
                   _key_event(scan=code, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)])
        time.sleep(delay)


def _utf16_units(ch: str) -> list[int]:
    encoded = ch.encode("utf-16-le")
    return [int.from_bytes(encoded[i:i + 2], "little") for i in range(0, len(encoded), 2)]


# --------------------------------------------------------------------------
# ウィンドウ探索
# --------------------------------------------------------------------------
@dataclass
class Window:
    hwnd: int
    title: str
    pid: int


def _ux_pids() -> set[int]:
    pids = set()
    for p in psutil.process_iter(["name", "pid"]):
        try:
            if (p.info["name"] or "").lower() == UX_PROCESS.lower():
                pids.add(p.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


def find_login_window() -> Window | None:
    """Riot Client のログインウィンドウを探す。"""
    found: list[Window] = []
    pids = _ux_pids()

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value

        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)

        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

        matches_class = cls.value == LOGIN_WINDOW_CLASS
        matches_title = any(t in title for t in LOGIN_WINDOW_TITLES)
        if (matches_class or matches_title) and (not pids or pid.value in pids):
            found.append(Window(hwnd, title, pid.value))
        return True

    user32.EnumWindows(callback, 0)
    return found[0] if found else None


def wait_for_login_window(timeout: float = 60.0) -> Window:
    deadline = time.time() + timeout
    while time.time() < deadline:
        w = find_login_window()
        if w:
            return w
        time.sleep(0.5)
    raise AutoLoginError("Riot Client のログイン画面が見つかりませんでした")


def focus(window: Window) -> None:
    user32.ShowWindow(window.hwnd, 9)          # SW_RESTORE
    user32.SetForegroundWindow(window.hwnd)
    time.sleep(0.4)
    if user32.GetForegroundWindow() != window.hwnd:
        raise AutoLoginError(
            "Riot Client を前面にできませんでした。"
            "他のアプリが前面を掴んでいる可能性があります"
        )


def perform_login(username: str, password: str, window: Window | None = None,
                  submit: bool = True, settle: float = 1.2) -> None:
    """ログイン画面にユーザー名とパスワードを打ち込む。

    submit=False なら Enter を押さずに止める。2 段階認証がある場合や、
    最後は自分で確認したい場合に使う。
    """
    if not username or not password:
        raise AutoLoginError("ユーザー名とパスワードの両方が必要です")

    w = window or wait_for_login_window()
    focus(w)
    time.sleep(settle)

    # ユーザー名欄にフォーカスが当たっている前提。念のため既存入力を消す
    press(VK_A, modifiers=(VK_CONTROL,))
    press(VK_BACK)
    type_text(username)
    press(VK_TAB)
    time.sleep(0.15)
    press(VK_A, modifiers=(VK_CONTROL,))
    press(VK_BACK)
    type_text(password)
    if submit:
        time.sleep(0.2)
        press(VK_RETURN)
