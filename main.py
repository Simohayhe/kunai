"""Kunai — VALORANT アカウント切り替えツール。エントリポイント。

  python main.py             通常起動
  python main.py --demo      モック環境で起動（VALORANT 未インストールでも動く）
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QDialog

from vam import diagnostics
from vam.storage import Vault
from vam.ui.dialogs import SetupDialog, UnlockDialog
from vam.ui.main_window import MainWindow


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kunai — VALORANT アカウント切り替えツール")
    parser.add_argument("--demo", action="store_true",
                        help="モック Riot 環境で起動する（動作確認用）")
    parser.add_argument("--app-dir", type=Path, default=None,
                        help="保管庫の置き場を指定する")
    return parser.parse_args(argv)


def start_demo_environment():
    """VALORANT が無い環境でも UI を触れるようにする。"""
    from vam.mock.fake_riot import FakeRiotEnv

    env = FakeRiotEnv()
    env.build(puuid="11111111-1111-1111-1111-111111111111")
    env.activate()
    env.start_local_api("11111111-1111-1111-1111-111111111111", "DemoPlayer", "JP1")
    return env


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    demo_env = None
    app_dir = args.app_dir
    if args.demo:
        demo_env = start_demo_environment()
        if app_dir is None:
            app_dir = Path(tempfile.gettempdir()) / "vam-demo"

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("Kunai")
    icon_path = Path(__file__).parent / "assets" / "icon.ico"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))

    # exe だと標準エラーがどこにも出ないので、落ちた理由をログに残して知らせる
    def on_crash(log_file, exc):
        from PySide6.QtWidgets import QMessageBox
        where = f"\n\nログ: {log_file}" if log_file else ""
        QMessageBox.critical(
            None, "エラー",
            f"予期しないエラーが発生しました。\n\n{exc}{where}",
        )

    diagnostics.install_excepthook(on_crash)

    vault = Vault(app_dir)

    if demo_env:
        # デモは使い捨ての保管庫。開錠ダイアログを挟まず、サンプルを入れて開く
        from vam.mock.demo_data import seed
        if not vault.initialized:
            vault.initialize()
        else:
            vault.open()
        seed(vault)
    elif not vault.initialized:
        setup = SetupDialog(vault)
        if setup.exec() != QDialog.Accepted:
            return 0
    elif vault.needs_password:
        unlock = UnlockDialog(vault)
        if unlock.exec() != QDialog.Accepted:
            return 0
    else:
        vault.open()

    window = MainWindow(vault)
    if demo_env:
        window.setWindowTitle(window.windowTitle() + "  —  デモモード（モック環境）")
    window.show()

    try:
        return app.exec()
    finally:
        if demo_env:
            demo_env.deactivate()


if __name__ == "__main__":
    sys.exit(main())
