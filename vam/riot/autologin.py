"""パスワード自動入力によるログイン (セッション復元が使えないときのフォールバック)。

Riot Client のログイン画面にキー入力を送り込む。
Riot が画面構成を変えると壊れる方式なので、あくまで保険。
通常は session.py のセッション復元を使うこと。

流れは ユーザー名 → Tab → パスワード → 「サインイン状態を維持」 → Enter。
すべてキーボードで行う。座標クリックには頼らない。

実機で確かめた要点:
  - ウィンドウを正しくアクティブ化できていれば、起動直後のログイン画面は
    ユーザー名欄にフォーカスが載っているので、クリックは要らない
  - ただし SetForegroundWindow を呼ぶだけでは「最前面に出るがアクティブでない」
    状態になることがあり、その場合キー入力が 1 文字も届かない (focus を参照)
  - 「サインイン状態を維持」へは Tab 7 回。位置を決め打ちせず、
    フォーカスリングを見て到達を判定する

キーは KEYEVENTF_UNICODE で送るので、日本語キーボードでも記号が化けない。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time
from dataclasses import dataclass

import psutil

user32 = ctypes.windll.user32

# ログイン画面のウィンドウ。実機 (Riot Client v138.0.1) で実測した値。
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
VK_SPACE = 0x20


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

        owned = pid.value in pids
        matches_class = cls.value in LOGIN_WINDOW_CLASSES
        matches_title = any(t in title for t in LOGIN_WINDOW_TITLES)
        if owned and (matches_class or matches_title):
            # 最小化されていると 160x28 のような小ささで報告される。
            # Riot 所有と分かっているものは大きさで弾かない。復元してから測る。
            found.append(Window(hwnd, title, pid.value, cls.value,
                                width, height, owned_by_riot=True))
        elif not pids and matches_title and width >= MIN_WINDOW_WIDTH:
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


# 読み込み中のスプラッシュは 600x600 程度。これを超えたらログイン画面とみなす
LOADED_MIN_WIDTH = 800


def wait_for_login_window(timeout: float = 120.0,
                          min_width: int = LOADED_MIN_WIDTH,
                          stable_for: float = 1.5) -> Window:
    """ログイン画面が「出来上がる」まで待つ。

    起動直後はスプラッシュ (600x600 程度) が出て、読み込みが終わると
    ウィンドウが作り直される。古いハンドルを掴んだままだと無効になるので、
    毎回取り直し、十分な大きさで一定時間安定してから返す。
    """
    deadline = time.time() + timeout
    stable_since: float | None = None
    last_size: tuple[int, int] | None = None

    while time.time() < deadline:
        w = find_login_window()
        if w and w.width >= min_width:
            size = (w.width, w.height)
            if size == last_size:
                if stable_since and time.time() - stable_since >= stable_for:
                    return w
            else:
                last_size = size
                stable_since = time.time()
        else:
            stable_since, last_size = None, None
        time.sleep(0.5)

    # 大きさの条件を満たさなくても、見つかっているなら返す
    w = find_login_window()
    if w:
        return w
    raise AutoLoginError("Riot Client のログイン画面が見つかりませんでした")


_kernel32 = ctypes.windll.kernel32
SW_RESTORE = 9
VK_MENU = 0x12


def focus(window: Window, settle: float = 0.6) -> None:
    """ウィンドウを本当にアクティブにする。

    SetForegroundWindow を単に呼ぶだけだと、最前面に出はするが
    アクティブにはならないことがある。その状態だとキー入力が届かず、
    Riot Client は画面を暗転させたままになる。

    Windows がフォーカス奪取を許すよう、ALT を軽く叩いてから、
    相手スレッドに入力状態を接続して活性化する。
    """
    user32.ShowWindow(window.hwnd, SW_RESTORE)

    # ALT の空打ち。SetForegroundWindow の制限を外すための作法
    press(VK_MENU)

    target_thread = user32.GetWindowThreadProcessId(window.hwnd, None)
    current_thread = _kernel32.GetCurrentThreadId()
    attached = user32.AttachThreadInput(current_thread, target_thread, True)
    try:
        user32.BringWindowToTop(window.hwnd)
        user32.SetForegroundWindow(window.hwnd)
        user32.SetActiveWindow(window.hwnd)
        user32.SetFocus(window.hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, target_thread, False)

    time.sleep(settle)
    if user32.GetForegroundWindow() != window.hwnd:
        raise AutoLoginError(
            "Riot Client を前面にできませんでした。"
            "他のアプリが前面を掴んでいる可能性があります"
        )


def is_active(window: Window) -> bool:
    return user32.GetForegroundWindow() == window.hwnd


# ログイン画面のユーザー名欄の位置。ウィンドウ左上からの比率。
# 実機 (Riot Client v138.0.1、ウィンドウ 1536x864) を実測した値。
# ログインウィンドウはユーザーがリサイズできない固定サイズなので、
# この比率で足りる。Riot が画面構成を変えたらここを測り直すこと。
USERNAME_FIELD = (0.130, 0.310)
# 「サインイン状態を維持」のチェックボックス。
# これを有効にしないと riot-login.persist が null のままで、
# セッションが保存されず切り替えに使えない。ログインより大事。
STAY_SIGNED_IN = (0.0417, 0.5023)


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


_gdi32 = ctypes.windll.gdi32


def get_pixel(x: int, y: int) -> tuple[int, int, int]:
    """画面上の 1 点の色を RGB で返す。"""
    dc = user32.GetDC(0)
    try:
        value = _gdi32.GetPixel(dc, int(x), int(y))
    finally:
        user32.ReleaseDC(0, dc)
    return (value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF)


# ほぼ白ならフォーム背景。チェックボックスではない
BACKGROUND_MIN = 246


def is_checkbox_checked(rgb: tuple[int, int, int]) -> bool | None:
    """チェックボックスの色から状態を判定する。

    未チェックは無彩色の枠 (背景より少し暗い)、チェック済みは Riot の赤。
    ウィンドウが非アクティブだと全体が暗転して色が沈むので、
    明るさではなく「赤みがあるか」で見る。

    ほぼ白、あるいは赤以外の有彩色なら、そこはチェックボックスではない。
    その場合は None を返す。ボタンの上で Space を押してしまうと
    ソーシャルログインが起動しかねないので、判別できないときは触らない。
    """
    r, g, b = rgb
    if r - max(g, b) >= 15:
        return True                       # 赤み = チェック済み
    if abs(r - g) <= 12 and abs(g - b) <= 12:
        if max(rgb) >= BACKGROUND_MIN:
            return None                   # ほぼ白 = 背景
        return False                      # 無彩色 = 未チェック
    return None                           # 赤以外の有彩色 = 別の部品


def stay_signed_in_state(window: Window) -> bool | None:
    """チェックボックスの状態。読めないときは None。

    最小化されていたり読み込み途中だと、画面の関係ない場所を読んでしまう。
    そのまま False を返すと、誤って触りにいく判断につながる。
    """
    left, top, width, height = window_rect(window)
    if width < LOADED_MIN_WIDTH:
        return None
    return is_checkbox_checked(
        get_pixel(*map_fraction(left, top, width, height, STAY_SIGNED_IN))
    )


def _has_focus_ring(window: Window, required: int = 2) -> bool:
    """チェックボックスにフォーカスリングが出ているか。

    Tab で回ってきたかを判定するのに使う。周囲 4 点のうち、
    他より明らかに暗い点が required 個以上あればリングとみなす。
    明るさの絶対値ではなく相対差で見るので、画面の暗転に影響されない。

    実測ではリングは角丸で、4 点のうち 2 点に乗った。1 点だけで
    判定すると、たまたま暗い何かに反応してボタン上で Space を
    押しかねないので 2 点以上を要求する。
    """
    x, y = field_position(window, STAY_SIGNED_IN)
    points = [(x - 12, y), (x + 12, y), (x, y - 12), (x, y + 12)]
    lums = sorted(sum(get_pixel(px, py)) for px, py in points)
    threshold = lums[-1] - 200
    return sum(1 for value in lums if value < threshold) >= required


def ensure_stay_signed_in(window: Window, max_tabs: int = 25) -> bool:
    """「サインイン状態を維持」を有効にする。キーボードだけで行う。

    Tab を送りながらフォーカスリングを見て、チェックボックスに
    到達したら Space で入れる。座標クリックに頼らないので、
    画面配置が多少変わっても追随する。

    既に有効なら何もしない。判定できないときも触らない。
    誤って外すと、ログインできてもセッションが保存されず、
    切り替えに使えなくなるため。

    実測 (Riot Client v138.0.1): ユーザー名欄から Tab 7 回で到達する。
    ただし開始位置は状況で変わるので、フォームを一周できるだけの
    回数を回す。フォーカスが既に先へ行っていても拾えるようにするため。
    """
    if stay_signed_in_state(window) is True:
        return True

    for _ in range(max_tabs):
        press(VK_TAB)
        time.sleep(0.35)
        if not _has_focus_ring(window):
            continue

        # リングが出ていても、そこに「未チェックのチェックボックス」が
        # 見えていなければ押さない。ソーシャルログインのボタン上で
        # Space を押すと OAuth が始まってしまう。
        state = stay_signed_in_state(window)
        if state is True:
            return True
        if state is not False:
            return False

        press(VK_SPACE)
        time.sleep(0.4)
        return stay_signed_in_state(window) is True
    return False


def perform_login(username: str, password: str, window: Window | None = None,
                  submit: bool = True, settle: float = 1.2,
                  stay_signed_in: bool = True) -> dict:
    """ログイン画面にユーザー名とパスワードを打ち込み、サインインする。

    ユーザー名 → Tab → パスワード → (「サインイン状態を維持」) → Enter。
    すべてキーボードで行い、座標クリックには頼らない。

    「サインイン状態を維持」も有効にする。これが無いとログインできても
    セッションが保存されず、このアプリの切り替えに使えない。

    Riot のログイン画面は hCaptcha で保護されている。captcha が出た場合は
    アプリ側では何もできないので、利用者が対応する必要がある。
    結果を dict で返すので、呼び側で状況を伝えること。
    """
    if not username or not password:
        raise AutoLoginError("ユーザー名とパスワードの両方が必要です")

    w = window or wait_for_login_window()
    focus(w)
    time.sleep(settle)

    # 起動直後のログイン画面はユーザー名欄にフォーカスが載っている。
    # ウィンドウを正しくアクティブ化できていれば、クリックは要らない。
    # 既存の入力を消してから打つ
    press(VK_A, modifiers=(VK_CONTROL,))
    press(VK_BACK)
    type_text(username)

    press(VK_TAB)
    time.sleep(0.15)
    press(VK_A, modifiers=(VK_CONTROL,))
    press(VK_BACK)
    type_text(password)

    # stay_signed_in=False なら触らない。結果は None (未操作) になる
    kept = ensure_stay_signed_in(w) if stay_signed_in else None

    submitted = False
    if submit:
        time.sleep(0.3)
        press(VK_RETURN)
        submitted = True

    return {"submitted": submitted, "stay_signed_in": kept, "window": w}
