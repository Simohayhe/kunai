"""各種ダイアログ。保管庫の開錠、アカウントの追加・編集。"""
from __future__ import annotations

import time
from collections import Counter

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QScrollArea, QTextEdit, QVBoxLayout, QWidget,
)

from ..crypto import VaultLocked
from ..models import Account
from ..riot.api import REGIONS
from ..storage import Vault
from . import theme

REGION_LABELS = {
    "ap": "AP  アジア太平洋", "na": "NA  北米", "eu": "EU  ヨーロッパ",
    "kr": "KR  韓国", "latam": "LATAM  中南米", "br": "BR  ブラジル",
}


class UnlockDialog(QDialog):
    """マスターパスワードを聞いて保管庫を開ける。"""

    def __init__(self, vault: Vault, parent=None):
        super().__init__(parent)
        self.vault = vault
        self.setWindowTitle("保管庫を開く")
        self.setMinimumWidth(360)
        self.setStyleSheet(theme.STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(22, 20, 22, 20)

        title = QLabel("Kunai")
        title.setObjectName("Title")
        layout.addWidget(title)

        self.hint = QLabel("マスターパスワードを入力してください")
        self.hint.setObjectName("SubTitle")
        layout.addWidget(self.hint)

        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("マスターパスワード")
        self.password.returnPressed.connect(self.accept)
        layout.addWidget(self.password)

        self.error = QLabel()
        self.error.setStyleSheet(f"color:{theme.ACCENT}; font-size:12px;")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setObjectName("Primary")
        buttons.button(QDialogButtonBox.Ok).setText("開く")
        buttons.button(QDialogButtonBox.Cancel).setText("終了")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self) -> None:
        try:
            self.vault.open(self.password.text() or None)
        except VaultLocked as exc:
            self.error.setText(str(exc))
            self.error.show()
            self.password.selectAll()
            self.password.setFocus()
            return
        except OSError:
            self.error.setText(
                "保管庫を復号できませんでした。"
                "別の Windows ユーザーで作成された保管庫の可能性があります。"
            )
            self.error.show()
            return
        super().accept()


class SetupDialog(QDialog):
    """初回起動時。マスターパスワードを設定するか決める。"""

    def __init__(self, vault: Vault, parent=None):
        super().__init__(parent)
        self.vault = vault
        self.setWindowTitle("初期設定")
        self.setMinimumWidth(430)
        self.setStyleSheet(theme.STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setSpacing(13)
        layout.setContentsMargins(22, 20, 22, 20)

        title = QLabel("ようこそ")
        title.setObjectName("Title")
        layout.addWidget(title)

        desc = QLabel(
            "アカウント情報は Windows の資格情報保護 (DPAPI) で暗号化され、"
            "このユーザーアカウントでのみ復号できます。\n"
            "さらにマスターパスワードを設定すると、同じ Windows ユーザーでも"
            "パスワードなしでは開けなくなります。"
        )
        desc.setObjectName("SubTitle")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        self.use_password = QCheckBox("マスターパスワードを設定する（推奨）")
        self.use_password.setChecked(True)
        self.use_password.toggled.connect(self._toggle)
        layout.addWidget(self.use_password)

        form = QFormLayout()
        form.setSpacing(9)
        self.password = QLineEdit(); self.password.setEchoMode(QLineEdit.Password)
        self.confirm = QLineEdit(); self.confirm.setEchoMode(QLineEdit.Password)
        form.addRow("パスワード", self.password)
        form.addRow("確認", self.confirm)
        layout.addLayout(form)

        self.warn = QLabel(
            "忘れると保管庫は開けません。復旧手段はありません。"
        )
        self.warn.setStyleSheet(f"color:{theme.WARN}; font-size:12px;")
        self.warn.setWordWrap(True)
        layout.addWidget(self.warn)

        self.error = QLabel()
        self.error.setStyleSheet(f"color:{theme.ACCENT}; font-size:12px;")
        self.error.hide()
        layout.addWidget(self.error)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setObjectName("Primary")
        buttons.button(QDialogButtonBox.Ok).setText("はじめる")
        buttons.button(QDialogButtonBox.Cancel).setText("終了")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _toggle(self, on: bool) -> None:
        for w in (self.password, self.confirm, self.warn):
            w.setEnabled(on)
            w.setVisible(on)

    def accept(self) -> None:
        password = None
        if self.use_password.isChecked():
            password = self.password.text()
            if len(password) < 4:
                self.error.setText("パスワードは 4 文字以上にしてください")
                self.error.show()
                return
            if password != self.confirm.text():
                self.error.setText("確認用パスワードが一致しません")
                self.error.show()
                return
        self.vault.initialize(password)
        super().accept()


class AccountDialog(QDialog):
    """アカウントの追加・編集。"""

    def __init__(self, account: Account | None = None, parent=None):
        super().__init__(parent)
        self.account = account or Account()
        self.is_new = account is None
        self.setWindowTitle("アカウントを追加" if self.is_new else "アカウントを編集")
        self.setMinimumWidth(440)
        self.setStyleSheet(theme.STYLESHEET)
        self._color = self.account.color
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(13)
        layout.setContentsMargins(22, 20, 22, 20)

        form = QFormLayout()
        form.setSpacing(9)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        label_row = QHBoxLayout()
        self.label = QLineEdit(self.account.label)
        self.label.setPlaceholderText("メイン / サブ / 弟用 など")
        label_row.addWidget(self.label, 1)
        self.label_linked = QCheckBox("Riot IDと同じ")
        self.label_linked.setToolTip(
            "チェックを入れると、表示名は常に Riot ID と同じになります。"
            "外すと、この欄で自由に名前を付けられます。"
        )
        self.label_linked.setChecked(self.account.label_linked)
        self.label_linked.toggled.connect(self.label.setDisabled)
        self.label.setDisabled(self.account.label_linked)
        label_row.addWidget(self.label_linked)
        form.addRow("表示名", label_row)

        self.riot_id = QLineEdit(self.account.riot_id)
        self.riot_id.setPlaceholderText("Name#TAG")
        form.addRow("Riot ID", self.riot_id)

        self.region = QComboBox()
        for r in REGIONS:
            self.region.addItem(REGION_LABELS.get(r, r.upper()), r)
        idx = self.region.findData(self.account.region)
        self.region.setCurrentIndex(idx if idx >= 0 else 0)
        form.addRow("リージョン", self.region)

        color_row = QHBoxLayout()
        self.color_button = QPushButton()
        self.color_button.setFixedSize(34, 26)
        self.color_button.clicked.connect(self._pick_color)
        self._paint_color()
        color_row.addWidget(self.color_button)
        color_row.addStretch(1)
        self.favorite = QCheckBox("お気に入り")
        self.favorite.setChecked(self.account.favorite)
        color_row.addWidget(self.favorite)
        form.addRow("色", color_row)

        layout.addLayout(form)

        cred_title = QLabel("ログイン情報（自動入力用・任意）")
        cred_title.setObjectName("SectionTitle")
        layout.addWidget(cred_title)

        note = QLabel(
            "通常はセッション保存だけで切り替えられます。ここは、"
            "セッションが失効したときの自動入力にのみ使われます。"
        )
        note.setObjectName("SubTitle")
        note.setWordWrap(True)
        layout.addWidget(note)

        cred = QFormLayout()
        cred.setSpacing(9)
        cred.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.username = QLineEdit(self.account.username)
        cred.addRow("ユーザー名", self.username)

        pw_row = QHBoxLayout()
        self.password = QLineEdit(self.account.password)
        self.password.setEchoMode(QLineEdit.Password)
        pw_row.addWidget(self.password, 1)
        self.reveal = QPushButton("表示")
        self.reveal.setCheckable(True)
        self.reveal.setFixedWidth(52)
        self.reveal.toggled.connect(
            lambda on: self.password.setEchoMode(
                QLineEdit.Normal if on else QLineEdit.Password
            )
        )
        pw_row.addWidget(self.reveal)
        cred.addRow("パスワード", pw_row)
        layout.addLayout(cred)

        self.note = QTextEdit(self.account.note)
        self.note.setPlaceholderText("メモ")
        self.note.setMaximumHeight(68)
        layout.addWidget(self.note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setObjectName("Primary")
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("キャンセル")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _paint_color(self) -> None:
        self.color_button.setStyleSheet(
            f"background:{self._color}; border:1px solid {theme.BORDER}; border-radius:4px;"
        )

    def _pick_color(self) -> None:
        color = QColorDialog.getColor(QColor(self._color), self, "カードの色")
        if color.isValid():
            self._color = color.name()
            self._paint_color()

    def accept(self) -> None:
        riot_id = self.riot_id.text().strip()
        if riot_id and "#" not in riot_id:
            QMessageBox.warning(self, "確認", "Riot ID は Name#TAG の形式で入力してください")
            return
        if not (self.label.text().strip() or riot_id or self.username.text().strip()):
            QMessageBox.warning(self, "確認", "表示名・Riot ID・ユーザー名のいずれかは必要です")
            return
        super().accept()

    def result_account(self) -> Account:
        a = self.account
        a.label = self.label.text().strip()
        a.label_linked = self.label_linked.isChecked()
        a.riot_id = self.riot_id.text().strip()
        a.region = self.region.currentData()
        a.username = self.username.text().strip()
        a.password = self.password.text()
        a.note = self.note.toPlainText().strip()
        a.color = self._color
        a.favorite = self.favorite.isChecked()
        return a


class SettingsDialog(QDialog):
    """通知・ログイン・更新をまとめた設定画面。

    更新の確認/適用そのものは MainWindow が持つ (バックグラウンド実行や
    終了処理が絡むため)。このダイアログはボタンと表示だけを持ち、
    MainWindow がそれらに配線する。
    """

    def __init__(self, webhook_url: str, mention: str, step_delay: float,
                stay_signed_in: bool, henrik_api_key: str, current_version: str,
                vault: Vault, parent=None):
        super().__init__(parent)
        self.vault = vault
        self.setWindowTitle("設定")
        self.setMinimumWidth(460)
        self.setStyleSheet(theme.STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(22, 20, 22, 20)

        title = QLabel("設定")
        title.setObjectName("Title")
        layout.addWidget(title)

        # -- 通知 -----------------------------------------------------
        notify_title = QLabel("メンテナンス・障害通知")
        notify_title.setObjectName("SectionTitle")
        layout.addWidget(notify_title)

        notify_desc = QLabel(
            "VALORANT のメンテナンス・障害情報を定期的に確認し、開始/終了"
            "したときに Discord へ通知します。空欄にすると、アプリ内の"
            "表示だけになり通知は行いません。"
        )
        notify_desc.setObjectName("SubTitle")
        notify_desc.setWordWrap(True)
        layout.addWidget(notify_desc)

        notify_form = QFormLayout()
        notify_form.setSpacing(9)
        self.webhook = QLineEdit(webhook_url)
        self.webhook.setPlaceholderText("https://discord.com/api/webhooks/...")
        notify_form.addRow("Webhook URL", self.webhook)
        self.mention = QLineEdit(mention)
        self.mention.setPlaceholderText("@everyone / <@ユーザーID> / <@&ロールID> など (任意)")
        notify_form.addRow("メンション", self.mention)
        layout.addLayout(notify_form)

        # -- ログイン ---------------------------------------------------
        login_title = QLabel("自動ログイン")
        login_title.setObjectName("SectionTitle")
        layout.addWidget(login_title)

        login_desc = QLabel(
            "ログイン画面が表示されてから入力を始めるまでの待機秒数です。"
            "短くすると速くなりますが、短すぎると画面の描画が間に合わず"
            "入力を取りこぼすことがあります。"
        )
        login_desc.setObjectName("SubTitle")
        login_desc.setWordWrap(True)
        layout.addWidget(login_desc)

        login_form = QFormLayout()
        login_form.setSpacing(9)
        self.delay = QDoubleSpinBox()
        self.delay.setDecimals(1)
        self.delay.setRange(0.1, 5.0)
        self.delay.setSingleStep(0.1)
        self.delay.setSuffix(" 秒")
        self.delay.setValue(step_delay)
        login_form.addRow("待機時間", self.delay)
        layout.addLayout(login_form)

        self.stay_signed_in = QCheckBox("「サインイン状態を維持」を自動で有効にする")
        self.stay_signed_in.setToolTip(
            "自動ログイン時、パスワード入力の後にこのチェックボックスも"
            "自動で入れます。無効だとセッションが保存されず、次回から"
            "切り替えにパスワードが毎回要ります。"
        )
        self.stay_signed_in.setChecked(stay_signed_in)
        layout.addWidget(self.stay_signed_in)

        # -- プレイヤー検索 ---------------------------------------------
        search_title = QLabel("プレイヤー検索 / 戦績のAPIモード")
        search_title.setObjectName("SectionTitle")
        layout.addWidget(search_title)

        search_desc = QLabel(
            "他プレイヤーを Riot ID で検索するには、外部の HenrikDev API の"
            "キーが要ります。設定すると、戦績タブもサインイン不要の"
            "APIモードになり、今ログインしていないアカウントの戦績も"
            "見られるようになります。未設定ならこれまで通りサインイン"
            "モード (ログイン中のアカウントのみ) で動きます。"
            "https://api.henrikdev.xyz/dashboard/ で無料で発行できます。"
        )
        search_desc.setObjectName("SubTitle")
        search_desc.setWordWrap(True)
        layout.addWidget(search_desc)

        search_form = QFormLayout()
        search_form.setSpacing(9)
        self.henrik_key = QLineEdit(henrik_api_key)
        self.henrik_key.setPlaceholderText("HenrikDev API キー (任意)")
        self.henrik_key.setEchoMode(QLineEdit.Password)
        search_form.addRow("APIキー", self.henrik_key)
        layout.addLayout(search_form)

        # -- マスターパスワード -----------------------------------------
        pw_title = QLabel("マスターパスワード")
        pw_title.setObjectName("SectionTitle")
        layout.addWidget(pw_title)

        pw_row = QHBoxLayout()
        pw_row.setSpacing(9)
        self.password_state_label = QLabel(
            "現在: パスワード設定済み" if vault.needs_password else "現在: パスワードなし"
        )
        self.password_state_label.setObjectName("SubTitle")
        pw_row.addWidget(self.password_state_label, 1)
        self.password_button = QPushButton("外す" if vault.needs_password else "設定する")
        self.password_button.clicked.connect(self._open_change_password)
        pw_row.addWidget(self.password_button)
        layout.addLayout(pw_row)

        # -- 更新 ---------------------------------------------------
        update_title = QLabel("ソフトの更新")
        update_title.setObjectName("SectionTitle")
        layout.addWidget(update_title)

        update_row = QHBoxLayout()
        update_row.setSpacing(9)
        self.version_label = QLabel(f"現在のバージョン: v{current_version}")
        self.version_label.setObjectName("SubTitle")
        update_row.addWidget(self.version_label, 1)
        self.check_button = QPushButton("今すぐ確認")
        update_row.addWidget(self.check_button)
        layout.addLayout(update_row)

        self.update_status = QLabel()
        self.update_status.setObjectName("SubTitle")
        self.update_status.setWordWrap(True)
        layout.addWidget(self.update_status)

        self.update_button = QPushButton()
        self.update_button.setObjectName("Primary")
        self.update_button.setVisible(False)
        layout.addWidget(self.update_button)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setObjectName("Primary")
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("キャンセル")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def webhook_url(self) -> str:
        return self.webhook.text().strip()

    def mention_text(self) -> str:
        return self.mention.text().strip()

    def henrik_api_key(self) -> str:
        return self.henrik_key.text().strip()

    def step_delay(self) -> float:
        return self.delay.value()

    def stay_signed_in_enabled(self) -> bool:
        return self.stay_signed_in.isChecked()

    # -- マスターパスワードの付け外し -----------------------------------------
    def _open_change_password(self) -> None:
        if self.vault.needs_password:
            confirm = QMessageBox.question(
                self, "パスワードを外しますか？",
                "パスワードを外すと、この Windows アカウントでログインして"
                "いれば誰でも保管庫を開けるようになります。\n続けますか？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return
            self.vault.change_password(None)
            self._refresh_password_state()
            return

        dialog = SetPasswordDialog(parent=self)
        if dialog.exec():
            self.vault.change_password(dialog.new_password())
            self._refresh_password_state()

    def _refresh_password_state(self) -> None:
        protected = self.vault.needs_password
        self.password_state_label.setText(
            "現在: パスワード設定済み" if protected else "現在: パスワードなし"
        )
        self.password_button.setText("外す" if protected else "設定する")

    # -- 更新確認の表示 (MainWindow から呼ばれる) ----------------------------
    def set_checking(self) -> None:
        self.check_button.setEnabled(False)
        self.check_button.setText("確認中…")
        self.update_status.setText("")

    def set_up_to_date(self) -> None:
        self.check_button.setEnabled(True)
        self.check_button.setText("今すぐ確認")
        self.update_status.setText("最新の状態です。")
        self.update_button.setVisible(False)

    def set_check_failed(self, message: str) -> None:
        self.check_button.setEnabled(True)
        self.check_button.setText("今すぐ確認")
        self.update_status.setText(message)
        self.update_button.setVisible(False)

    def set_update_available(self, tag: str) -> None:
        self.check_button.setEnabled(True)
        self.check_button.setText("今すぐ確認")
        self.update_status.setText(f"{tag} が利用できます。")
        self.update_button.setText(f"{tag} に更新")
        self.update_button.setVisible(True)


class SetPasswordDialog(QDialog):
    """途中からマスターパスワードを新しく設定する。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("マスターパスワードを設定")
        self.setMinimumWidth(360)
        self.setStyleSheet(theme.STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setSpacing(13)
        layout.setContentsMargins(22, 20, 22, 20)

        title = QLabel("マスターパスワードを設定")
        title.setObjectName("Title")
        layout.addWidget(title)

        form = QFormLayout()
        form.setSpacing(9)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.confirm = QLineEdit()
        self.confirm.setEchoMode(QLineEdit.Password)
        form.addRow("パスワード", self.password)
        form.addRow("確認", self.confirm)
        layout.addLayout(form)

        warn = QLabel("忘れると保管庫は開けません。復旧手段はありません。")
        warn.setStyleSheet(f"color:{theme.WARN}; font-size:12px;")
        warn.setWordWrap(True)
        layout.addWidget(warn)

        self.error = QLabel()
        self.error.setStyleSheet(f"color:{theme.ACCENT}; font-size:12px;")
        self.error.hide()
        layout.addWidget(self.error)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setObjectName("Primary")
        buttons.button(QDialogButtonBox.Ok).setText("設定")
        buttons.button(QDialogButtonBox.Cancel).setText("キャンセル")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def new_password(self) -> str:
        return self.password.text()

    def accept(self) -> None:
        password = self.password.text()
        if len(password) < 4:
            self.error.setText("パスワードは 4 文字以上にしてください")
            self.error.show()
            return
        if password != self.confirm.text():
            self.error.setText("確認用パスワードが一致しません")
            self.error.show()
            return
        super().accept()


class PlayerSearchDialog(QDialog):
    """Riot ID (Name#TAG) で他プレイヤーのレベル・ランクを検索する (HenrikDev API)。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("プレイヤー検索")
        self.setMinimumWidth(420)
        self.setStyleSheet(theme.STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setSpacing(13)
        layout.setContentsMargins(22, 20, 22, 20)

        title = QLabel("プレイヤー検索")
        title.setObjectName("Title")
        layout.addWidget(title)

        row = QHBoxLayout()
        row.setSpacing(9)
        self.riot_id = QLineEdit()
        self.riot_id.setPlaceholderText("Name#TAG")
        row.addWidget(self.riot_id, 1)
        self.search_button = QPushButton("検索")
        self.search_button.setObjectName("Primary")
        row.addWidget(self.search_button)
        layout.addLayout(row)
        self.riot_id.returnPressed.connect(self.search_button.click)

        self.result = QLabel()
        self.result.setObjectName("SubTitle")
        self.result.setWordWrap(True)
        self.result.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.result.setTextFormat(Qt.RichText)
        layout.addWidget(self.result, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def query(self) -> str:
        return self.riot_id.text().strip()

    def set_searching(self) -> None:
        self.search_button.setEnabled(False)
        self.search_button.setText("検索中…")
        self.result.setText("")

    def set_result(self, account, rank) -> None:
        self.search_button.setEnabled(True)
        self.search_button.setText("検索")
        self.result.setText(
            f"<span style='font-size:16px; font-weight:700;'>{account.riot_id}</span><br>"
            f"レベル {account.account_level}　・　リージョン {account.region.upper()}"
            "<br><br>"
            f"<span style='font-size:15px; font-weight:700;'>{rank.tier_name}</span>"
            f"　{rank.rr} RR"
        )

    def set_error(self, message: str) -> None:
        self.search_button.setEnabled(True)
        self.search_button.setText("検索")
        self.result.setText(message)


# デュオ/トリオ (同じパーティ) を色分けするときの配色。チーム内で使い切ったら
# 循環する (10人中、同じ色が複数パーティに割り当たることは稀だが許容する)。
PARTY_COLORS = ["#00d4ff", "#ffb020", "#ff5c8a", "#7c5cff", "#22c55e"]


class MatchDetailDialog(QDialog):
    """1 試合分のスコアボード。tracker.gg を参考に、味方/敵を分けて
    スコア順に並べ、デュオ/トリオ (同じパーティ) を色分けする。"""

    def __init__(self, detail, parent=None):
        super().__init__(parent)
        self.setWindowTitle("試合詳細")
        self.setMinimumSize(640, 540)
        self.setStyleSheet(theme.STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setSpacing(13)
        layout.setContentsMargins(22, 20, 22, 20)

        header = QHBoxLayout()
        title = QLabel(detail.map_name or "不明なマップ")
        title.setObjectName("Title")
        header.addWidget(title)
        header.addStretch(1)
        result = QLabel("勝利" if detail.won else "敗北")
        result.setStyleSheet(
            f"color:{theme.OK if detail.won else theme.ACCENT}; "
            "font-weight:700; font-size:16px;"
        )
        header.addWidget(result)
        score = QLabel(f"{detail.my_team_score} - {detail.enemy_team_score}")
        score.setObjectName("SubTitle")
        header.addWidget(score)
        layout.addLayout(header)

        when = QLabel(
            time.strftime("%Y/%m/%d %H:%M", time.localtime(detail.started_at / 1000))
            if detail.started_at else ""
        )
        when.setObjectName("SubTitle")
        layout.addWidget(when)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        container = QWidget()
        inner = QVBoxLayout(container)
        inner.setSpacing(16)
        inner.setContentsMargins(0, 0, 6, 0)

        party_colors = self._assign_party_colors(detail.my_team + detail.enemy_team)
        inner.addWidget(self._team_section("味方", detail.my_team, theme.OK, party_colors))
        inner.addWidget(self._team_section("敵", detail.enemy_team, theme.ACCENT, party_colors))
        inner.addStretch(1)
        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _assign_party_colors(players) -> dict[str, str]:
        """2 人以上いる party_id にだけ色を割り当てる (ソロは無色)。"""
        counts = Counter(p.party_id for p in players if p.party_id)
        colors: dict[str, str] = {}
        i = 0
        for party_id, n in counts.items():
            if n >= 2:
                colors[party_id] = PARTY_COLORS[i % len(PARTY_COLORS)]
                i += 1
        return colors

    def _team_section(self, title: str, players: list, accent: str,
                      party_colors: dict[str, str]) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setSpacing(6)
        layout.setContentsMargins(0, 0, 0, 0)

        head = QLabel(title)
        head.setStyleSheet(f"color:{accent}; font-weight:700; font-size:13px;")
        layout.addWidget(head)

        header_row = QHBoxLayout()
        header_row.setSpacing(9)
        header_row.addWidget(self._col_label("", 4))
        header_row.addWidget(self._col_label("プレイヤー", 0), 1)
        header_row.addWidget(self._col_label("K/D/A", 88))
        header_row.addWidget(self._col_label("ACS", 55))
        header_row.addWidget(self._col_label("HS%", 50))
        header_row.addWidget(self._col_label("DDΔ", 65))
        layout.addLayout(header_row)

        for p in players:
            layout.addWidget(self._player_row(p, party_colors.get(p.party_id, "")))
        return box

    @staticmethod
    def _col_label(text: str, width: int) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color:{theme.TEXT_DIM}; font-size:10px; border:none;")
        if width:
            lbl.setFixedWidth(width)
        return lbl

    def _player_row(self, p, party_color: str) -> QFrame:
        row = QFrame()
        bg = theme.BG_HOVER if p.is_self else theme.BG_CARD
        row.setStyleSheet(
            f"QFrame {{ background:{bg}; border:1px solid {theme.BORDER};"
            f" border-radius:4px; }} QLabel {{ border:none; }}"
        )
        layout = QHBoxLayout(row)
        layout.setContentsMargins(9, 6, 9, 6)
        layout.setSpacing(9)

        bar = QLabel()
        bar.setFixedWidth(4)
        bar.setStyleSheet(
            f"background:{party_color or 'transparent'}; border-radius:2px;"
        )
        layout.addWidget(bar)

        name_col = QVBoxLayout()
        name_col.setSpacing(0)
        name = QLabel(p.riot_id + ("  (自分)" if p.is_self else ""))
        name.setStyleSheet(
            f"font-size:12px; font-weight:{700 if p.is_self else 400};"
        )
        name_col.addWidget(name)
        agent = QLabel(p.agent_name or "?")
        agent.setStyleSheet(f"color:{theme.TEXT_DIM}; font-size:10px;")
        name_col.addWidget(agent)
        layout.addLayout(name_col, 1)

        kda = QLabel(f"{p.kills}/{p.deaths}/{p.assists}")
        kda.setStyleSheet("font-size:11px;")
        kda.setFixedWidth(88)
        layout.addWidget(kda)

        acs = QLabel(str(p.score))
        acs.setStyleSheet("font-size:11px;")
        acs.setFixedWidth(55)
        layout.addWidget(acs)

        hs = QLabel(f"{p.headshot_pct:.0f}%")
        hs.setStyleSheet("font-size:11px;")
        hs.setFixedWidth(50)
        layout.addWidget(hs)

        dd = p.damage_delta
        dd_color = theme.OK if dd > 0 else (theme.ACCENT if dd < 0 else theme.TEXT_DIM)
        dd_label = QLabel(f"{dd:+d}")
        dd_label.setStyleSheet(f"color:{dd_color}; font-size:11px;")
        dd_label.setFixedWidth(65)
        layout.addWidget(dd_label)

        return row
