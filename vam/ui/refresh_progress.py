"""すべて更新の進捗バー。ウィンドウの一番上に出る。

service.refresh_cycle が流すイベント (dict) を受けて、全体の何件目か・
今のアカウント・各ステップ (Riot Client / ランク / 戦績 / スキン) の状態を出す。
キャンセルボタンもここにある。
"""
from __future__ import annotations

from html import escape

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout,
)

from ..service import STEP_LABELS
from . import theme

# 1 アカウントあたりの目盛り: Riot Client の起動 + 4 ステップ
_UNITS = 5
_ORDER = ("client", "rank", "history", "skins", "store")
_LABELS = {"client": "Riot Client", **STEP_LABELS}
_MARKS = {"pending": "・", "running": "…", "ok": "✓", "error": "✕", "skipped": "－"}
_COLORS = {"pending": theme.TEXT_DIM, "running": theme.WARN, "ok": theme.OK,
           "error": theme.ACCENT, "skipped": theme.TEXT_DIM}


class RefreshProgressBar(QFrame):
    cancel_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("RefreshProgress")
        self.setStyleSheet(
            f"QFrame#RefreshProgress {{ background:{theme.BG_CARD};"
            f" border-bottom:1px solid {theme.BORDER}; }}"
            f"QFrame#RefreshProgress QLabel {{ border:none; background:transparent; }}"
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 8, 18, 8)
        outer.setSpacing(5)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.title = QLabel("すべて更新")
        self.title.setStyleSheet("font-weight:700;")
        top.addWidget(self.title)
        self.counter = QLabel()
        self.counter.setStyleSheet(f"color:{theme.TEXT_DIM};")
        top.addWidget(self.counter)
        top.addStretch(1)
        self.tally = QLabel()
        self.tally.setStyleSheet(f"color:{theme.TEXT_DIM}; font-size:11px;")
        top.addWidget(self.tally)
        self.cancel_button = QPushButton("キャンセル")
        self.cancel_button.setObjectName("Danger")
        self.cancel_button.clicked.connect(self._on_cancel)
        top.addWidget(self.cancel_button)
        outer.addLayout(top)

        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        self.bar.setStyleSheet(
            f"QProgressBar {{ background:{theme.BORDER}; border:none; border-radius:3px; }}"
            f"QProgressBar::chunk {{ background:{theme.ACCENT}; border-radius:3px; }}"
        )
        outer.addWidget(self.bar)

        self.detail = QLabel()
        self.detail.setTextFormat(Qt.RichText)
        self.detail.setStyleSheet("font-size:11px;")
        outer.addWidget(self.detail)

        self.hide()
        self._reset()

    # -- 状態 ---------------------------------------------------------------
    def _reset(self) -> None:
        self.total = 0
        self.index = 0
        self.name = ""
        self.steps = {k: "pending" for k in _ORDER}
        self.held = 0
        self.partial = 0
        self.note = ""
        self.cancelling = False

    def begin(self, total: int) -> None:
        self._reset()
        self.total = total
        self.bar.setRange(0, max(total, 1) * _UNITS)
        self.bar.setValue(0)
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("キャンセル")
        self.note = "準備中…"
        self._render()
        self.show()

    def finish(self) -> None:
        self.hide()
        self._reset()

    def _on_cancel(self) -> None:
        if self.cancelling:
            return
        self.cancelling = True
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("キャンセル中…")
        self.note = "実行中の処理が終わるのを待っています"
        self._render()
        self.cancel_requested.emit()

    # -- イベント -------------------------------------------------------------
    def on_event(self, event: dict) -> None:
        kind = event.get("kind")
        if kind == "start":
            self.total = event.get("total", self.total)
            self.bar.setRange(0, max(self.total, 1) * _UNITS)
        elif kind == "account":
            self.index = event["index"]
            self.name = event.get("name", "")
            self.steps = {k: "pending" for k in _ORDER}
            self.note = ""
        elif kind == "client":
            self.steps["client"] = event["state"]
        elif kind == "step":
            self.steps[event["step"]] = event["state"]
        elif kind == "account_done":
            self.held += 1 if event.get("held") else 0
            self.partial += 1 if event.get("partial") else 0
        elif kind == "restoring":
            self.note = f"{event.get('name', '')} のセッションを戻しています…"
        self._render()

    def _render(self) -> None:
        done_units = max(self.index - 1, 0) * _UNITS
        if self.index:
            done_units += sum(1 for s in self.steps.values()
                              if s in ("ok", "error", "skipped"))
        self.bar.setValue(min(done_units, self.bar.maximum()))

        self.counter.setText(f"{self.index}/{self.total} 件" if self.index else "")
        tally = []
        if self.partial:
            tally.append(f"一部失敗 {self.partial}")
        if self.held:
            tally.append(f"保留 {self.held}")
        self.tally.setText("　".join(tally))

        parts = []
        for key in _ORDER:
            state = self.steps[key]
            parts.append(
                f'<span style="color:{_COLORS[state]}">{_LABELS[key]} {_MARKS[state]}</span>'
            )
        text = "　".join(parts)
        if self.name:
            text = f'<b>{escape(self.name)}</b>　{text}'
        if self.note:
            text = (text + "　" if self.name else "") + f'<span style="color:{theme.TEXT_DIM}">{self.note}</span>'
        self.detail.setText(text)
