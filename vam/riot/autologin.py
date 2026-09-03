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

# ログイン画面のウィンドウ。実機 (Riot Client 134.x) で実測した値。
# UI は Electron なのでウィンドウクラスは Chrome_WidgetWin_1 になる。
# これは Chromium 系アプリなら何でも名乗る汎用クラスなので、
# クラスだけで判定してはいけない。所有プロセスが Riot のものかどうかが本命。
LOGIN_WINDOW_CLASSES = ("Chrome_WidgetWin_1", "RCLIENT")
LOGIN_WINDOW_TITLES = ("Riot Client", "リオットクライアント")
UX_PROCESSES = ("Riot Client.exe", "RiotClientUx.exe")

# ウィンドウとして小さすぎるものは通知やスプラッシュなので除く
MIN_WINDOW_WIDTH = 200

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


class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KeyBdInput), ("mi", _MouseInput)]


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


INPUT_MOUSE = 0
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


def click_at(x: int, y: int, settle: float = 0.25) -> None:
    """画面座標をクリックする。

    Electron のログイン画面は、ウィンドウを前面にしただけでは
    入力欄が DOM フォーカスを持たない。実際に押さないと文字が入らない。
    """
    user32.SetProcessDPIAware()
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    down = _Input(INPUT_MOUSE, _InputUnion(
        mi=_MouseInput(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, None)))
    up = _Input(INPUT_MOUSE, _InputUnion(
        mi=_MouseInput(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, None)))
    _send([down, up])
    time.sleep(settle)


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
    cls: str = ""
    width: int = 0
    height: int = 0
    owned_by_riot: bool = False


def _ux_pids() -> set[int]:
    wanted = {n.lower() for n in UX_PROCESSES}
    pids = set()
    for p in psutil.process_iter(["name", "pid"]):
        try:
            if (p.info["name"] or "").lower() in wanted:
                pids.add(p.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


def enumerate_candidates() -> list[Window]:
    """ログイン画面になりうる可視ウィンドウを列挙する。

    Riot のプロセスが持つものを優先する。Chrome_WidgetWin_1 は
    Chromium 系アプリの汎用クラスなので、クラス一致だけで飛びつくと
    無関係なアプリにキー入力を送りかねない。
    """
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

        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width < MIN_WINDOW_WIDTH:
            return True

        owned = pid.value in pids
        matches_class = cls.value in LOGIN_WINDOW_CLASSES
        matches_title = any(t in title for t in LOGIN_WINDOW_TITLES)
        if owned and (matches_class or matches_title):
            found.append(Window(hwnd, title, pid.value, cls.value,
                                width, height, owned_by_riot=True))
        elif not pids and matches_title:
            # プロセスを列挙できなかったときの保険。タイトル一致だけで拾う
            found.append(Window(hwnd, title, pid.value, cls.value,
                                width, height, owned_by_riot=False))
        return True

    user32.EnumWindows(callback, 0)
    # Riot 所有のものを優先し、その中では大きいものを先に
    found.sort(key=lambda w: (not w.owned_by_riot, -(w.width * w.height)))
    return found


def find_login_window() -> Window | None:
    """Riot Client のログインウィンドウを探す。"""
    candidates = enumerate_candidates()
    return candidates[0] if candidates else None


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


# ログイン画面のユーザー名欄の位置。ウィンドウ左上からの比率。
# 実機 (Riot Client v138.0.1、ウィンドウ 1536x864) を実測した値。
# ログインウィンドウはユーザーがリサイズできない固定サイズなので、
# この比率で足りる。Riot が画面構成を変えたらここを測り直すこと。
USERNAME_FIELD = (0.130, 0.310)


def map_fraction(left: int, top: int, width: int, height: int,
                 fraction: tuple[float, float]) -> tuple[int, int]:
    """ウィンドウ矩形と相対位置から画面座標を出す。座標計算だけを分けてある。"""
    return int(left + width * fraction[0]), int(top + height * fraction[1])


def window_rect(window: Window) -> tuple[int, int, int, int]:
    """(left, top, width, height) を返す。"""
    user32.SetProcessDPIAware()
    rect = wt.RECT()
    user32.GetWindowRect(window.hwnd, ctypes.byref(rect))
    return (rect.left, rect.top,
            rect.right - rect.left, rect.bottom - rect.top)


def field_position(window: Window, fraction: tuple[float, float]) -> tuple[int, int]:
    """ウィンドウ内の相対位置を画面座標に直す。"""
    return map_fraction(*window_rect(window), fraction)


def perform_login(username: str, password: str, window: Window | None = None,
                  submit: bool = False, settle: float = 1.2) -> None:
    """ログイン画面にユーザー名とパスワードを打ち込む。

    既定では送信しない (submit=False)。Riot のログイン画面は hCaptcha で
    保護されており、勝手に送信すると人手での確認を挟む余地がなくなる。
    また「サインイン状態を維持」を有効にしてもらう必要があるので、
    最後の一押しは利用者に任せる。

    ログイン画面は Electron なので、ウィンドウを前面にしただけでは
    入力欄がフォーカスを持たない。必ずクリックしてから打ち込む。
    """
    if not username or not password:
        raise AutoLoginError("ユーザー名とパスワードの両方が必要です")

    w = window or wait_for_login_window()
    focus(w)
    time.sleep(settle)

    x, y = field_position(w, USERNAME_FIELD)
    click_at(x, y)

    # 既存の入力を消してから打つ
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
