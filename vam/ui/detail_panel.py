"""右側の詳細パネル。概要・戦績・所持品のタブ。"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QTabWidget, QVBoxLayout, QWidget,
)

from ..models import Account
from ..riot.api import CompetitiveUpdate
from . import theme
from .account_card import RankBadge


def _stat_tile(caption: str, value: str, color: str = theme.TEXT) -> QFrame:
    tile = QFrame()
    tile.setStyleSheet(
        f"QFrame {{ background:{theme.BG_CARD}; border:1px solid {theme.BORDER};"
        f" border-radius:5px; }}"
    )
    layout = QVBoxLayout(tile)
    layout.setContentsMargins(13, 10, 13, 11)
    layout.setSpacing(1)

    cap = QLabel(caption)
    cap.setStyleSheet(f"color:{theme.TEXT_DIM}; font-size:11px; border:none;")
    layout.addWidget(cap)

    val = QLabel(value)
    val.setStyleSheet(f"color:{color}; font-size:19px; font-weight:700; border:none;")
    layout.addWidget(val)
    return tile


# 残りがこれを切ったら警告色にする
EXPIRY_WARN_DAYS = 7


def _session_summary(info) -> str:
    """セッションの状態を 1 行で表す。"""
    if not info.valid:
        return "未保存"
    if info.expired:
        return "失効 — 要再ログイン"
    if not info.expires_at:
        return ("有効 — 期限不明（ログイン直後は Riot 側が期限を書き込まないため。"
                "一度ゲームを起動すると入る）")
    expires = time.strftime("%Y/%m/%d %H:%M", time.localtime(info.expires_at))
    return f"有効 — 残り {info.expires_in_days:.0f} 日（{expires} まで）"


def _session_banner_style(info) -> tuple[str, str]:
    """セッション帯の文言と色。"""
    if not info.valid:
        return ("セッション未保存 — このアカウントには切り替えられません",
                theme.ACCENT)
    if info.expired:
        return ("セッション失効 — ログインし直して取り込み直してください",
                theme.ACCENT)
    days = info.expires_in_days
    if not info.expires_at:
        return ("セッション有効 — 期限は未記載（ログイン直後は Riot 側が書き込まない）",
                theme.TEXT_DIM)
    expires = time.strftime("%Y/%m/%d %H:%M", time.localtime(info.expires_at))
    text = f"セッション残り {days:.0f} 日  ·  {expires} まで  ·  更新のたびに延長されます"
    return text, (theme.WARN if days < EXPIRY_WARN_DAYS else theme.OK)


class OverviewTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(15)

        header = QHBoxLayout()
        header.setSpacing(14)
        self.badge = RankBadge()
        self.badge.setFixedSize(58, 58)
        header.addWidget(self.badge)

        names = QVBoxLayout()
        names.setSpacing(2)
        self.rank_name = QLabel("—")
        self.rank_name.setStyleSheet("font-size:20px; font-weight:700;")
        names.addWidget(self.rank_name)
        self.rank_sub = QLabel()
        self.rank_sub.setObjectName("SubTitle")
        names.addWidget(self.rank_sub)
        header.addLayout(names)
        header.addStretch(1)
        layout.addLayout(header)

        # 残り日数はここで必ず見えるようにする
        self.session_banner = QLabel()
        self.session_banner.setWordWrap(True)
        layout.addWidget(self.session_banner)

        self.tiles = QGridLayout()
        self.tiles.setSpacing(9)
        layout.addLayout(self.tiles)

        info_title = QLabel("アカウント情報")
        info_title.setObjectName("SectionTitle")
        layout.addWidget(info_title)

        self.info = QGridLayout()
        self.info.setSpacing(7)
        self.info.setColumnStretch(1, 1)
        layout.addLayout(self.info)

        layout.addStretch(1)

    def _clear(self, grid) -> None:
        while grid.count():
            item = grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def update_account(self, account: Account, session_info=None, icon=None) -> None:
        rank = account.rank
        self.badge.set_rank(rank.tier, icon)
        self.rank_name.setText(rank.tier_name or "Unranked")
        self.rank_name.setStyleSheet(
            f"font-size:20px; font-weight:700; color:{theme.rank_color(rank.tier)};"
        )
        sub = []
        if rank.tier > 2:
            sub.append(f"{rank.rr} RR")
        if rank.leaderboard_rank:
            sub.append(f"ランキング {rank.leaderboard_rank} 位")
        if rank.updated_at:
            sub.append("更新 " + time.strftime("%m/%d %H:%M", time.localtime(rank.updated_at)))
        else:
            sub.append("未取得")
        self.rank_sub.setText("  ·  ".join(sub))

        if session_info is not None:
            text, color = _session_banner_style(session_info)
            self.session_banner.setText(text)
            self.session_banner.setStyleSheet(
                f"color:{color}; font-size:11px; border:1px solid {color};"
                "border-radius:4px; padding:6px 10px;"
            )
            self.session_banner.show()
        else:
            self.session_banner.hide()

        self._clear(self.tiles)
        winrate = f"{rank.wins / rank.games * 100:.0f}%" if rank.games else "—"
        tiles = [
            ("今シーズン戦績", f"{rank.wins}勝 / {rank.games}戦"),
            ("勝率", winrate),
            ("最高ランク", rank.peak_tier_name or "—", theme.rank_color(rank.peak_tier)),
            ("VP", f"{account.wallet.vp:,}", theme.WARN),
            ("RP", f"{account.wallet.rp:,}", theme.TEAL),
            ("KC", f"{account.wallet.kc:,}", theme.TEXT_DIM),
        ]
        for i, t in enumerate(tiles):
            self.tiles.addWidget(_stat_tile(*t), i // 3, i % 3)

        self._clear(self.info)
        rows = [
            ("Riot ID", account.riot_id or "—"),
            ("リージョン", account.region.upper()),
            ("ユーザー名", account.username or "未登録"),
            # 自動ログインは両方揃っていないと動かない。片方だけだと
            # 「押しても何も入力されない」ことになるので、一目で分かるようにする
            ("パスワード", "登録済み" if account.password else "未登録"),
            ("PUUID", account.puuid or "—"),
        ]
        if session_info is not None:
            rows.append(("セッション", _session_summary(session_info)))
        if account.session_saved_at:
            rows.append(("セッション保存",
                         time.strftime("%Y/%m/%d %H:%M",
                                       time.localtime(account.session_saved_at))))
        if account.last_used_at:
            rows.append(("最終使用",
                         time.strftime("%Y/%m/%d %H:%M", time.localtime(account.last_used_at))))
        if account.note:
            rows.append(("メモ", account.note))

        for r, (key, value) in enumerate(rows):
            k = QLabel(key)
            k.setStyleSheet(f"color:{theme.TEXT_DIM}; font-size:12px;")
            k.setFixedWidth(84)
            self.info.addWidget(k, r, 0, Qt.AlignTop)
            v = QLabel(value)
            v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.setStyleSheet("font-size:12px;")
            self.info.addWidget(v, r, 1)


class HistoryTab(QWidget):
    reload_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(10)

        top = QHBoxLayout()
        title = QLabel("コンペティティブ履歴")
        title.setObjectName("SectionTitle")
        top.addWidget(title)
        top.addStretch(1)
        self.reload = QPushButton("読み込む")
        self.reload.clicked.connect(self.reload_requested.emit)
        top.addWidget(self.reload)
        layout.addLayout(top)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.rows = QVBoxLayout(self.container)
        self.rows.setSpacing(5)
        self.rows.setContentsMargins(0, 0, 6, 0)
        self.rows.addStretch(1)
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll, 1)

        self.empty = QLabel("「読み込む」で直近のランク変動を取得します")
        self.empty.setObjectName("SubTitle")
        self.empty.setAlignment(Qt.AlignCenter)
        self.rows.insertWidget(0, self.empty)

    def clear(self) -> None:
        while self.rows.count() > 1:
            item = self.rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def set_message(self, text: str) -> None:
        self.clear()
        msg = QLabel(text)
        msg.setObjectName("SubTitle")
        msg.setAlignment(Qt.AlignCenter)
        msg.setWordWrap(True)
        self.rows.insertWidget(0, msg)

    def set_matches(self, matches: list[CompetitiveUpdate], map_names: dict,
                    tier_names: dict | None = None) -> None:
        self.clear()
        if not matches:
            self.set_message("コンペティティブの記録がありません")
            return
        for m in matches:
            self.rows.insertWidget(self.rows.count() - 1,
                                   self._row(m, map_names, tier_names or {}))

    def _row(self, m: CompetitiveUpdate, map_names: dict, tier_names: dict) -> QFrame:
        row = QFrame()
        row.setStyleSheet(
            f"QFrame {{ background:{theme.BG_CARD}; border:1px solid {theme.BORDER};"
            f" border-radius:4px; }} QLabel {{ border:none; }}"
        )
        layout = QHBoxLayout(row)
        layout.setContentsMargins(11, 8, 11, 8)
        layout.setSpacing(11)

        rr = m.rr_earned
        color = theme.OK if rr > 0 else (theme.ACCENT if rr < 0 else theme.TEXT_DIM)
        delta = QLabel(f"{rr:+d}" if rr else "±0")
        delta.setStyleSheet(f"color:{color}; font-weight:700; font-size:14px;")
        delta.setFixedWidth(46)
        layout.addWidget(delta)

        info = QVBoxLayout()
        info.setSpacing(1)
        map_name = map_names.get(m.map_id, {}).get("name") or "不明なマップ"
        name = QLabel(map_name)
        name.setStyleSheet("font-size:12px;")
        info.addWidget(name)
        when = QLabel(
            time.strftime("%Y/%m/%d %H:%M", time.localtime(m.started_at / 1000))
            if m.started_at else ""
        )
        when.setStyleSheet(f"color:{theme.TEXT_DIM}; font-size:10px;")
        info.addWidget(when)
        layout.addLayout(info, 1)

        label = tier_names.get(m.tier_after, m.tier_after_name)
        tier = QLabel(f"{label}  {m.rr_after} RR")
        tier.setStyleSheet(f"color:{theme.rank_color(m.tier_after)}; font-size:12px;")
        layout.addWidget(tier)
        return row


class InventoryTab(QWidget):
    reload_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(10)

        top = QHBoxLayout()
        title = QLabel("所持品")
        title.setObjectName("SectionTitle")
        top.addWidget(title)
        top.addStretch(1)
        self.reload = QPushButton("集計する")
        self.reload.clicked.connect(self.reload_requested.emit)
        top.addWidget(self.reload)
        layout.addLayout(top)

        self.tiles = QGridLayout()
        self.tiles.setSpacing(9)
        layout.addLayout(self.tiles)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.rows = QVBoxLayout(self.container)
        self.rows.setSpacing(3)
        self.rows.setContentsMargins(0, 0, 6, 0)
        self.rows.addStretch(1)
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll, 1)

        self.set_message("「集計する」で所持スキンを一覧にします")

    def _clear_grid(self) -> None:
        while self.tiles.count():
            item = self.tiles.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def clear(self) -> None:
        while self.rows.count() > 1:
            item = self.rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def set_message(self, text: str) -> None:
        self.clear()
        msg = QLabel(text)
        msg.setObjectName("SubTitle")
        msg.setAlignment(Qt.AlignCenter)
        msg.setWordWrap(True)
        self.rows.insertWidget(0, msg)

    def set_summary(self, summary: dict) -> None:
        self._clear_grid()
        tiles = [
            ("所持スキン", str(summary["skin_count"])),
            ("エージェント", f"{summary['agent_count']} / {summary['agent_total']}"),
            ("ガンバディー", str(summary["buddy_count"])),
            ("カード", str(summary["card_count"])),
            ("タイトル", str(summary["title_count"])),
            ("スプレー", str(summary["spray_count"])),
        ]
        for i, t in enumerate(tiles):
            self.tiles.addWidget(_stat_tile(*t), i // 3, i % 3)

        self.clear()
        by_tier = summary["by_tier"]
        if by_tier:
            chips = QFrame()
            chips.setStyleSheet("border:none;")
            row = QHBoxLayout(chips)
            row.setContentsMargins(0, 4, 0, 4)
            row.setSpacing(7)
            for name, count in sorted(by_tier.items(), key=lambda kv: -kv[1]):
                chip = QLabel(f"{name}  {count}")
                chip.setStyleSheet(
                    f"color:{theme.TEXT_DIM}; border:1px solid {theme.BORDER};"
                    "border-radius:10px; padding:3px 10px; font-size:11px;"
                )
                row.addWidget(chip)
            row.addStretch(1)
            self.rows.insertWidget(0, chips)

        for skin in summary["skins"]:
            self.rows.insertWidget(self.rows.count() - 1, self._skin_row(skin))

    def _skin_row(self, skin: dict) -> QFrame:
        row = QFrame()
        row.setStyleSheet("QFrame { border:none; } QLabel { border:none; }")
        row.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(2, 3, 2, 3)
        layout.setSpacing(9)

        dot = QLabel("●")
        dot.setStyleSheet(f"color:{skin['tier_color']}; font-size:11px;")
        dot.setFixedWidth(14)
        layout.addWidget(dot)

        name = QLabel(skin["name"])
        name.setStyleSheet("font-size:12px;")
        layout.addWidget(name, 1)

        tier = QLabel(skin["tier_name"])
        tier.setStyleSheet(f"color:{theme.TEXT_DIM}; font-size:11px;")
        layout.addWidget(tier)
        return row


class DetailPanel(QTabWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.overview = OverviewTab()
        self.history = HistoryTab()
        self.inventory = InventoryTab()
        self.addTab(self.overview, "概要")
        self.addTab(self.history, "戦績")
        self.addTab(self.inventory, "所持品")
