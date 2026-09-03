"""配色とスタイルシート。VALORANT の UI 配色に寄せた暗色テーマ。"""
from __future__ import annotations

BG = "#0f1923"          # ベース背景
BG_ALT = "#18232e"      # パネル
BG_CARD = "#1b2733"
BG_HOVER = "#22303d"
BORDER = "#2b3a47"
ACCENT = "#ff4655"      # VALORANT レッド
ACCENT_DIM = "#c4303c"
TEAL = "#0fd8c2"
TEXT = "#ece8e1"
TEXT_DIM = "#8b9bab"
WARN = "#f0a53a"
OK = "#3ad29f"

# ランクのおおまかな色。tier//3 で束ねる
RANK_COLORS = {
    0: "#5a6672",   # Unranked
    1: "#63594f",   # Iron
    2: "#9d6b4b",   # Bronze
    3: "#c3ccd4",   # Silver
    4: "#e5c76b",   # Gold
    5: "#39a0a8",   # Platinum
    6: "#a05fd0",   # Diamond
    7: "#3ad29f",   # Ascendant
    8: "#e0577f",   # Immortal
    9: "#ffedb0",   # Radiant
}


def rank_color(tier: int) -> str:
    if tier <= 2:
        return RANK_COLORS[0]
    if tier >= 27:
        return RANK_COLORS[9]
    return RANK_COLORS.get(tier // 3, RANK_COLORS[0])


STYLESHEET = f"""
QWidget {{
    background: {BG};
    color: {TEXT};
    font-family: "Segoe UI", "Yu Gothic UI", "Meiryo", sans-serif;
    font-size: 13px;
}}
QMainWindow, QDialog {{ background: {BG}; }}

/* ラベルは親の背景を透かす。透明にしないとカード上に矩形が浮く */
QLabel {{ background: transparent; border: none; }}

QLabel#Title {{ font-size: 20px; font-weight: 700; letter-spacing: 1px; }}
QLabel#SubTitle {{ color: {TEXT_DIM}; font-size: 12px; }}
QLabel#SectionTitle {{
    font-size: 12px; font-weight: 700; color: {TEXT_DIM};
    letter-spacing: 2px; padding-top: 4px;
}}
QLabel#Dim {{ color: {TEXT_DIM}; }}
QLabel#BigStat {{ font-size: 26px; font-weight: 700; }}

QPushButton {{
    background: {BG_ALT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 7px 14px;
    color: {TEXT};
}}
QPushButton:hover {{ background: {BG_HOVER}; border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: {TEXT_DIM}; border-color: {BORDER}; background: {BG}; }}

QPushButton#Primary {{
    background: {ACCENT}; border: none; color: #ffffff; font-weight: 700;
    padding: 9px 18px;
}}
QPushButton#Primary:hover {{ background: #ff5c69; }}
QPushButton#Primary:pressed {{ background: {ACCENT_DIM}; }}
QPushButton#Primary:disabled {{ background: {BORDER}; color: {TEXT_DIM}; }}

QPushButton#Ghost {{ background: transparent; border: 1px solid {BORDER}; }}
QPushButton#Ghost:hover {{ background: {BG_HOVER}; }}
QPushButton#Danger:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}

QPushButton#Star {{
    background: transparent; border: none; font-size: 16px; padding: 0px;
    max-width: 24px; min-width: 24px;
}}

QLineEdit, QTextEdit, QComboBox, QSpinBox {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 7px 9px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QLineEdit[readOnly="true"] {{ color: {TEXT_DIM}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {BG_ALT}; border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
}}

QScrollArea {{ border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {TEXT_DIM}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 5px; min-width: 30px; }}

QTabWidget::pane {{ border: none; border-top: 1px solid {BORDER}; }}
QTabBar::tab {{
    background: transparent; padding: 9px 18px; color: {TEXT_DIM};
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:hover {{ color: {TEXT}; }}

QStatusBar {{ background: {BG_ALT}; color: {TEXT_DIM}; }}
QStatusBar::item {{ border: none; }}

QToolTip {{
    background: {BG_ALT}; color: {TEXT};
    border: 1px solid {BORDER}; padding: 5px;
}}

QCheckBox::indicator {{
    width: 15px; height: 15px; border: 1px solid {BORDER};
    border-radius: 3px; background: {BG};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

QProgressBar {{
    background: {BG_ALT}; border: none; border-radius: 2px;
    height: 4px; text-align: center;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 2px; }}

QMenu {{ background: {BG_ALT}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 7px 22px; border-radius: 3px; }}
QMenu::item:selected {{ background: {ACCENT}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
"""
