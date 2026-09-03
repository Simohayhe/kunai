"""実機の Riot Client を起動して、プロセスとウィンドウの素性を実測する。

自動ログインは「どのプロセスの、どのクラスのウィンドウか」を当てにしている。
そこが実機とずれていると、入力の送り先を間違える。推測せずに測る。

    python tools/probe_client.py            # 起動して調べる
    python tools/probe_client.py --no-launch  # 既に起動しているものを調べる

パスワードは扱わない。入力も送らない。読むだけ。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psutil

from vam.riot import autologin, launcher, process

user32 = ctypes.windll.user32

RIOT_NAME_HINT = ("riot", "valorant", "vgc", "vgtray")


def riot_processes() -> dict[int, str]:
    out = {}
    for p in psutil.process_iter(["pid", "name"]):
        try:
            name = p.info["name"] or ""
            if any(h in name.lower() for h in RIOT_NAME_HINT):
                out[p.info["pid"]] = name
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


def visible_windows() -> list[dict]:
    found: list[dict] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)

        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)

        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))

        found.append({
            "hwnd": hwnd,
            "title": title.value,
            "class": cls.value,
            "pid": pid.value,
            "size": (rect.right - rect.left, rect.bottom - rect.top),
        })
        return True

    user32.EnumWindows(callback, 0)
    return found


def main() -> int:
    launch = "--no-launch" not in sys.argv

    print("=" * 70)
    print("Riot Client 実測")
    print("=" * 70)

    if launch and not process.client_running():
        print("\n[起動] Riot Client を起動します (ログイン画面のみ、ゲームは起動しない)")
        launcher.launch(product=None)
    else:
        print("\n[起動] 省略")

    print("\n[待機] ウィンドウが出るまで最大 90 秒待ちます")
    deadline = time.time() + 90
    procs: dict[int, str] = {}
    while time.time() < deadline:
        procs = riot_processes()
        wins = [w for w in visible_windows() if w["pid"] in procs and w["size"][0] > 200]
        if wins:
            break
        time.sleep(1)

    print("\n--- Riot 系プロセス " + "-" * 48)
    for pid, name in sorted(procs.items(), key=lambda kv: kv[1]):
        print(f"  {name:34} pid={pid}")

    print("\n--- Riot 系プロセスが持つ可視ウィンドウ " + "-" * 30)
    riot_windows = [w for w in visible_windows() if w["pid"] in procs]
    if not riot_windows:
        print("  (なし)")
    for w in riot_windows:
        print(f"  class={w['class']!r}")
        print(f"    title = {w['title']!r}")
        print(f"    pid   = {w['pid']}  ({procs.get(w['pid'], '?')})")
        print(f"    size  = {w['size'][0]}x{w['size'][1]}")

    print("\n--- 現在のコードの想定と実測の突き合わせ " + "-" * 29)
    print(f"  autologin.LOGIN_WINDOW_CLASSES = {list(autologin.LOGIN_WINDOW_CLASSES)}")
    actual_classes = sorted({w["class"] for w in riot_windows})
    print(f"  実測のクラス                 = {actual_classes}")
    print(f"  一致するか                   = "
          f"{any(c in actual_classes for c in autologin.LOGIN_WINDOW_CLASSES)}")

    print(f"\n  autologin.UX_PROCESSES       = {list(autologin.UX_PROCESSES)}")
    actual_names = sorted(set(procs.values()))
    print(f"  実測のプロセス名             = {actual_names}")
    print(f"  一致するか                   = "
          f"{any(n in actual_names for n in autologin.UX_PROCESSES)}")

    print(f"\n  process.CLIENT_PROCESSES     = {list(process.CLIENT_PROCESSES)}")
    missing = [n for n in actual_names if n not in process.CLIENT_PROCESSES
               and n not in process.GAME_PROCESSES and "vgc" not in n.lower()
               and "vgtray" not in n.lower()]
    print(f"  一覧に無い実在プロセス       = {missing or 'なし'}")

    print("\n--- ログイン画面の候補 " + "-" * 45)
    for w in autologin.enumerate_candidates():
        print(f"  {w.title!r} class={w.cls} pid={w.pid} "
              f"{w.width}x{w.height} riot所有={w.owned_by_riot}")
    window = autologin.find_login_window()
    if window:
        print(f"\n  find_login_window() -> {window.title!r} pid={window.pid} "
              f"{window.width}x{window.height}")
    else:
        print("\n  find_login_window() -> なし")

    print("\n" + "=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
