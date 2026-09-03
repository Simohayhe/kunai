"""アカウント 1 件分のカード。左の一覧に並ぶ。"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from ..models import Account
from . import theme


class RankBadge(QLabel):
    """ランク名と RR を出す小さなバッジ。アイコンが取れたら差し替わる。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(40, 40)
        self.setAlignment(Qt.AlignCenter)
        self._tier = 0
        self._pixmap = None

    def set_rank(self, tier: int, pixmap=None) -> None:
        self._tier = tier
        self._pixmap = pixmap
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        color = QColor(theme.rank_color(self._tier))

        p.setPen(QPen(color, 2))
        p.setBrush(QColor(theme.BG))
        p.drawEllipse(1, 1, self.width() - 2, self.height() - 2)

        if self._pixmap and not self._pixmap.isNull():
            scaled = self._pixmap.scaled(30, 30, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            p.drawPixmap((self.width() - scaled.width()) // 2,
                         (self.height() - scaled.height()) // 2, scaled)
        else:
            p.setPen(color)
            f = p.font(); f.setBold(True); f.setPointSize(11)
            p.setFont(f)
            label = "-" if self._tier <= 2 else str(self._tier)
            p.drawText(self.rect(), Qt.AlignCenter, label)
        p.end()


class AccountCard(QFrame):
    clicked = Signal(str)
    switch_requested = Signal(str)
    favorite_toggled = Signal(str)
    context_requested = Signal(str, object)

    def __init__(self, account: Account, parent=None):
        super().__init__(parent)
        self.account = account
        self._selected = False
        self._current = False
        self.setObjectName("AccountCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda pos: self.context_requested.emit(self.account.id, self.mapToGlobal(pos))
        )
        self._build()
        self.refresh(account)

    # -- 組み立て -----------------------------------------------------------
    def _build(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 10, 0)
        outer.setSpacing(0)

        self.stripe = QWidget()
        self.stripe.setFixedWidth(4)
        outer.addWidget(self.stripe)

        inner = QHBoxLayout()
        inner.setContentsMargins(11, 10, 0, 10)
        inner.setSpacing(11)
        outer.addLayout(inner, 1)

        self.badge = RankBadge()
        inner.addWidget(self.badge)

        text = QVBoxLayout()
        text.setSpacing(2)
        inner.addLayout(text, 1)

        top = QHBoxLayout()
        top.setSpacing(6)
        self.name = QLabel()
        f = self.name.font(); f.setBold(True); f.setPointSize(10)
        self.name.setFont(f)
        top.addWidget(self.name)
        self.current_pill = QLabel("使用中")
        self.current_pill.setStyleSheet(
            f"color:{theme.OK}; border:1px solid {theme.OK}; border-radius:3px;"
            "padding:0px 5px; font-size:10px;"
        )
        self.current_pill.hide()
        top.addWidget(self.current_pill)
        top.addStretch(1)
        text.addLayout(top)

        self.riot_id = QLabel()
        self.riot_id.setObjectName("Dim")
        self.riot_id.setStyleSheet(f"color:{theme.TEXT_DIM}; font-size:11px;")
        text.addWidget(self.riot_id)

        self.status = QLabel()
        self.status.setStyleSheet("font-size:10px;")
        text.addWidget(self.status)

        right = QVBoxLayout()
        right.setSpacing(2)
        right.setAlignment(Qt.AlignTop | Qt.AlignRight)
        inner.addLayout(right)

        self.star = QPushButton()
        self.star.setObjectName("Star")
        self.star.setCursor(Qt.PointingHandCursor)
        self.star.clicked.connect(lambda: self.favorite_toggled.emit(self.account.id))
        right.addWidget(self.star, 0, Qt.AlignRight)

        self.rr = QLabel()
        self.rr.setAlignment(Qt.AlignRight)
        self.rr.setStyleSheet(f"color:{theme.TEXT_DIM}; font-size:11px;")
        right.addWidget(self.rr)

    # -- 表示更新 -----------------------------------------------------------
    def refresh(self, account: Account, session_ok: bool | None = None,
                session_days: float = 0.0) -> None:
        self.account = account
        self.name.setText(account.display_name)
        self.riot_id.setText(account.riot_id or account.username or "未設定")
        self.stripe.setStyleSheet(f"background:{account.color};")
        self.star.setText("★" if account.favorite else "☆")
        self.star.setStyleSheet(
            f"color:{theme.WARN};" if account.favorite else f"color:{theme.BORDER};"
        )

        rank = account.rank
        self.badge.set_rank(rank.tier)
        if rank.tier > 2:
            self.rr.setText(f"{rank.rr} RR")
            self.rr.show()
        else:
            self.rr.hide()

        self.status.setText(self._status_text(account, session_ok, session_days))
        self._apply_style()

    # 残りがこれを切ったら警告色にする
    EXPIRY_WARN_DAYS = 7

    def _status_text(self, account: Account, session_ok: bool | None,
                     session_days: float) -> str:
        """状態行。左に近況、右にセッションの残り日数を必ず出す。"""
        self.status.setStyleSheet("font-size:10px;")

        parts = []
        if account.rank.tier > 2:
            parts.append(account.rank.tier_name)
        if account.last_used_at:
            parts.append(self._ago(account.last_used_at))
        elif account.session_saved:
            parts.append("未使用")
        left = "  ·  ".join(parts)

        session_html, tooltip = self._session_html(account, session_ok, session_days)
        self.setToolTip(tooltip)

        if not left:
            return session_html
        return (f'<span style="color:{theme.TEXT_DIM}">{left}</span>'
                f'<span style="color:{theme.BORDER}">　·　</span>{session_html}')

    def _session_html(self, account: Account, session_ok: bool | None,
                      session_days: float) -> tuple[str, str]:
        if session_ok is False:
            if not account.session_saved:
                return (f'<span style="color:{theme.ACCENT}">セッション未保存</span>',
                        "このアカウントはセッションが保存されていません。\n"
                        "Riot Client でログインしてから取り込んでください。")
            return (f'<span style="color:{theme.ACCENT}">失効 — 要再ログイン</span>',
                    "セッションの有効期限が切れています。\n"
                    "ログインし直して取り込み直してください。")

        if not session_days:
            return (f'<span style="color:{theme.TEXT_DIM}">残り不明</span>',
                    "セッションの有効期限を読み取れませんでした。")

        color = theme.WARN if session_days < self.EXPIRY_WARN_DAYS else theme.TEXT_DIM
        expires = time.strftime("%Y/%m/%d %H:%M",
                                time.localtime(time.time() + session_days * 86400))
        tooltip = (f"セッション有効期限: {expires}\n"
                   "更新するたびに期限は延長されます。")
        return f'<span style="color:{color}">残り {session_days:.0f} 日</span>', tooltip

    @staticmethod
    def _ago(ts: float) -> str:
        delta = time.time() - ts
        if delta < 3600:
            return f"{int(delta // 60)} 分前"
        if delta < 86400:
            return f"{int(delta // 3600)} 時間前"
        return f"{int(delta // 86400)} 日前"

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self._apply_style()

    def set_current(self, current: bool) -> None:
        self._current = current
        self.current_pill.setVisible(current)
        self._apply_style()

    def _apply_style(self) -> None:
        border = theme.ACCENT if self._selected else (
            theme.OK if self._current else theme.BORDER
        )
        bg = theme.BG_HOVER if self._selected else theme.BG_CARD
        self.setStyleSheet(
            f"QFrame#AccountCard {{ background:{bg}; border:1px solid {border};"
            f" border-radius:5px; }}"
            f"QFrame#AccountCard:hover {{ background:{theme.BG_HOVER}; }}"
        )

    # -- 入力 ---------------------------------------------------------------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.account.id)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.switch_requested.emit(self.account.id)
        super().mouseDoubleClickEvent(event)
