"""各種ダイアログ。保管庫の開錠、アカウントの追加・編集。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QTextEdit,
    QVBoxLayout,
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
                stay_signed_in: bool, current_version: str, parent=None):
        super().__init__(parent)
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

    def step_delay(self) -> float:
        return self.delay.value()

    def stay_signed_in_enabled(self) -> bool:
        return self.stay_signed_in.isChecked()

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
