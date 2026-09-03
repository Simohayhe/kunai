"""自動ログインの「入力が届くか」だけを、認証情報なしで検証する。

パスワードは扱わない。送信もしない。
無害な文字列をユーザー名欄に入れて、届いたかを画面で確かめ、最後に消す。

    python tools/check_autologin.py

前後のスクリーンショットを保存するので、文字が入ったか目で確認できる。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from vam.riot import autologin  # noqa: E402

PROBE_TEXT = "vam-input-test"


def shot(app: QApplication, window: autologin.Window, out: Path, tag: str) -> Path:
    screen = QGuiApplication.primaryScreen()
    pixmap = screen.grabWindow(0)   # 画面全体。ウィンドウ単体だと Electron が黒くなる
    target = out / f"autologin-{tag}.png"
    pixmap.save(str(target))
    return target


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    app = QApplication(sys.argv)

    print("=" * 66)
    print("自動ログインの入力経路チェック (パスワードは扱わない)")
    print("=" * 66)

    window = autologin.find_login_window()
    if not window:
        print("\nログインウィンドウが見つかりません。Riot Client を起動してください。")
        return 1
    print(f"\n対象ウィンドウ: {window.title!r}")
    print(f"  class = {window.cls}")
    print(f"  pid   = {window.pid}")
    print(f"  size  = {window.width}x{window.height}")
    print(f"  riot所有 = {window.owned_by_riot}")

    print("\n[1] 前面化を試みます")
    try:
        autologin.focus(window)
        print("    成功: 前面に出ました")
    except autologin.AutoLoginError as exc:
        print(f"    失敗: {exc}")
        return 2

    before = shot(app, window, out, "1-before")
    print(f"    入力前のスクリーンショット: {before}")

    print(f"\n[2] 無害な文字列 {PROBE_TEXT!r} を送ります (送信はしない)")
    time.sleep(0.8)
    autologin.press(autologin.VK_A, modifiers=(autologin.VK_CONTROL,))
    autologin.press(autologin.VK_BACK)
    autologin.type_text(PROBE_TEXT)
    time.sleep(1.0)
    after = shot(app, window, out, "2-after")
    print(f"    入力後のスクリーンショット: {after}")

    print("\n[3] 入れた文字を消します")
    autologin.press(autologin.VK_A, modifiers=(autologin.VK_CONTROL,))
    autologin.press(autologin.VK_BACK)
    time.sleep(0.8)
    cleared = shot(app, window, out, "3-cleared")
    print(f"    消去後のスクリーンショット: {cleared}")

    print("\n" + "=" * 66)
    print("2 枚目に文字が入っていれば、入力経路は生きている。")
    print("Enter は一度も押していないので、ログインは試行されていない。")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
