"""UI の組み立て検証とスクリーンショット撮影。

実機なしで見た目と初期化経路を確認するための開発用スクリプト。
    python tools/ui_shot.py out.png
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from vam.mock.demo_data import FIRST_PUUID, sample_matches, seed
from vam.mock.fake_riot import FakeRiotEnv
from vam.storage import Vault
from vam.ui.main_window import MainWindow


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("ui.png")

    env = FakeRiotEnv()
    env.build(puuid=FIRST_PUUID)
    env.activate()

    vault = Vault(Path(tempfile.mkdtemp(prefix="vam-shot-")))
    vault.initialize()
    seed(vault)

    app = QApplication(sys.argv)
    window = MainWindow(vault)
    window.resize(1180, 740)
    window.show()

    def capture():
        # 戦績と所持品を実際に埋めてから撮る
        from vam.service import MatchStats
        window._on_history(MatchStats(matches=sample_matches()))
        groups = window.service.weapon_inventory(
            window.vault.get(window.selected_id)
        )
        window.detail.inventory.set_groups(groups)

        # 概要・戦績・所持品を順に撮る
        for index, suffix in enumerate(("overview", "history", "inventory")):
            window.detail.setCurrentIndex(index)
            app.processEvents()
            target = out if index == 0 else out.with_name(f"{out.stem}-{suffix}{out.suffix}")
            window.grab().save(str(target))
            print("saved:", target)
        app.quit()

    QTimer.singleShot(2500, capture)   # アイコン取得を少し待つ
    code = app.exec()
    env.deactivate()
    return code


if __name__ == "__main__":
    sys.exit(main())
