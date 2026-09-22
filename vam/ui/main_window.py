"""メインウィンドウ。"""
from __future__ import annotations

import sys
import time
import webbrowser

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QPushButton, QScrollArea, QSplitter, QVBoxLayout, QWidget,
)

from .. import diagnostics, updater
from ..models import Account
from ..service import AccountService
from ..storage import Vault
from ..version import __version__
from . import theme, workers
from .account_card import AccountCard
from .detail_panel import DetailPanel
from .dialogs import AccountDialog, SettingsDialog
from .icons import IconLoader

STATUS_CHECK_INTERVAL_MS = 120_000


class MainWindow(QMainWindow):
    def __init__(self, vault: Vault):
        super().__init__()
        self.vault = vault
        self.service = AccountService(vault)
        self.icons = IconLoader(vault.app_dir / "cache" / "icons")
        self.icons.loaded.connect(self._on_icon_loaded)

        self.cards: dict[str, AccountCard] = {}
        self.selected_id: str | None = None
        self.current_id: str | None = None
        self._rank_icons: dict[int, str] = {}
        self._busy = False
        self._busy_since = 0.0
        self._pending_release: updater.Release | None = None
        self._update_check_token: object | None = None

        self.setWindowTitle("Kunai")
        self.resize(1080, 700)
        self.setStyleSheet(theme.STYLESHEET)

        self._build()
        self._load_rank_icons()
        self.reload_accounts()
        self.refresh_environment()

        self._env_timer = QTimer(self)
        self._env_timer.timeout.connect(self.refresh_environment)
        self._env_timer.start(5000)

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self.check_status)
        self._status_timer.start(STATUS_CHECK_INTERVAL_MS)
        self.check_status()

        self.check_for_update()

    # ==================================================================
    # 組み立て
    # ==================================================================
    def _build(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_header())
        layout.addWidget(self._build_status_banner())

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setStyleSheet(f"QSplitter::handle {{ background:{theme.BORDER}; }}")
        splitter.addWidget(self._build_sidebar())
        splitter.addWidget(self._build_detail())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([340, 740])
        layout.addWidget(splitter, 1)

        self.status = self.statusBar()
        self.status.showMessage("準備完了")

        for key, slot in (
            ("Ctrl+N", self.add_account),
            ("Ctrl+R", self.refresh_selected),
            ("Ctrl+Shift+R", self.refresh_all),
            ("F5", self.reload_accounts),
        ):
            action = QAction(self)
            action.setShortcut(QKeySequence(key))
            action.triggered.connect(slot)
            self.addAction(action)

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setStyleSheet(
            f"QFrame {{ background:{theme.BG_ALT}; border-bottom:1px solid {theme.BORDER}; }}"
        )
        layout = QHBoxLayout(header)
        layout.setContentsMargins(18, 11, 18, 11)
        layout.setSpacing(11)

        mark = QLabel("KUNAI")
        mark.setStyleSheet(
            f"color:{theme.TEAL}; font-size:16px; font-weight:800; letter-spacing:2px;"
            "border:none;"
        )
        layout.addWidget(mark)
        sub = QLabel("VALORANT ACCOUNT MANAGER")
        sub.setStyleSheet(
            f"color:{theme.TEXT_DIM}; font-size:11px; letter-spacing:2px; border:none;"
        )
        layout.addWidget(sub)
        layout.addStretch(1)

        self.env_label = QLabel()
        self.env_label.setStyleSheet(f"color:{theme.TEXT_DIM}; font-size:12px; border:none;")
        layout.addWidget(self.env_label)

        version_label = QLabel(f"v{__version__}")
        version_label.setStyleSheet(f"color:{theme.TEXT_DIM}; font-size:11px; border:none;")
        layout.addWidget(version_label)

        self.update_button = QPushButton()
        self.update_button.setObjectName("Ghost")
        self.update_button.clicked.connect(self.on_update)
        self.update_button.setVisible(False)
        layout.addWidget(self.update_button)

        self.refresh_all_button = QPushButton("すべて更新")
        self.refresh_all_button.setObjectName("Ghost")
        self.refresh_all_button.clicked.connect(self.refresh_all)
        layout.addWidget(self.refresh_all_button)

        self.settings_button = QPushButton("設定")
        self.settings_button.setObjectName("Ghost")
        self.settings_button.setToolTip("通知・ログイン・更新の設定")
        self.settings_button.clicked.connect(self.open_settings)
        layout.addWidget(self.settings_button)

        self.diag_button = QPushButton("?")
        self.diag_button.setObjectName("Ghost")
        self.diag_button.setFixedWidth(32)
        self.diag_button.setToolTip("診断情報を表示（不具合報告用）")
        self.diag_button.clicked.connect(self.show_diagnostics)
        layout.addWidget(self.diag_button)
        return header

    def _build_status_banner(self) -> QWidget:
        self.status_banner = QFrame()
        self.status_banner.setStyleSheet(
            f"QFrame {{ background:{theme.WARN}; }}"
        )
        layout = QHBoxLayout(self.status_banner)
        layout.setContentsMargins(18, 7, 18, 7)
        self.status_banner_label = QLabel()
        self.status_banner_label.setStyleSheet(
            "color:#1a1408; font-size:12px; font-weight:700; border:none;"
        )
        self.status_banner_label.setWordWrap(True)
        layout.addWidget(self.status_banner_label, 1)
        self.status_banner.setVisible(False)
        return self.status_banner

    def _build_sidebar(self) -> QWidget:
        side = QWidget()
        side.setMinimumWidth(300)
        layout = QVBoxLayout(side)
        layout.setContentsMargins(13, 13, 8, 13)
        layout.setSpacing(9)

        self.search = QLineEdit()
        self.search.setPlaceholderText("アカウントを検索")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.import_button = QPushButton("現在のを取り込む")
        self.import_button.setToolTip(
            "いま Riot Client にログインしているアカウントを登録します"
        )
        self.import_button.clicked.connect(self.import_current)
        buttons.addWidget(self.import_button, 1)

        add = QPushButton("＋")
        add.setFixedWidth(38)
        add.setToolTip("手動で追加  (Ctrl+N)")
        add.clicked.connect(self.add_account)
        buttons.addWidget(add)
        layout.addLayout(buttons)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.list_container = QWidget()
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(0, 0, 6, 0)
        self.list_layout.setSpacing(6)
        self.list_layout.addStretch(1)
        scroll.setWidget(self.list_container)
        layout.addWidget(scroll, 1)

        self.empty_label = QLabel(
            "アカウントがありません。\n\n"
            "Riot Client でログインしてから\n「現在のを取り込む」を押すと登録できます。"
        )
        self.empty_label.setObjectName("SubTitle")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.list_layout.insertWidget(0, self.empty_label)
        return side

    def _build_detail(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 15, 20, 15)
        layout.setSpacing(0)

        top = QHBoxLayout()
        top.setSpacing(9)

        titles = QVBoxLayout()
        titles.setSpacing(1)
        self.detail_title = QLabel("アカウントを選択してください")
        self.detail_title.setObjectName("Title")
        titles.addWidget(self.detail_title)
        self.detail_sub = QLabel()
        self.detail_sub.setObjectName("SubTitle")
        titles.addWidget(self.detail_sub)
        top.addLayout(titles, 1)

        self.switch_button = QPushButton("このアカウントで起動")
        self.switch_button.setObjectName("Primary")
        self.switch_button.clicked.connect(self.switch_selected)
        top.addWidget(self.switch_button)

        self.refresh_button = QPushButton("更新")
        self.refresh_button.clicked.connect(self.refresh_selected)
        top.addWidget(self.refresh_button)

        self.more_button = QPushButton("···")
        self.more_button.setFixedWidth(38)
        self.more_button.clicked.connect(self._show_more_menu)
        top.addWidget(self.more_button)
        layout.addLayout(top)

        self.detail = DetailPanel()
        self.detail.history.reload_requested.connect(self.load_history)
        self.detail.inventory.reload_requested.connect(self.load_inventory)
        layout.addWidget(self.detail, 1)

        self.placeholder = QLabel(
            "左の一覧からアカウントを選ぶと、ランク・戦績・所持品を表示します。"
        )
        self.placeholder.setObjectName("SubTitle")
        self.placeholder.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.placeholder, 1)

        self._set_detail_visible(False)
        return panel

    def _set_detail_visible(self, visible: bool) -> None:
        self.detail.setVisible(visible)
        self.placeholder.setVisible(not visible)
        for b in (self.switch_button, self.refresh_button, self.more_button):
            b.setVisible(visible)

    # ==================================================================
    # 一覧
    # ==================================================================
    def reload_accounts(self) -> None:
        for card in self.cards.values():
            card.setParent(None)
            card.deleteLater()
        self.cards.clear()

        accounts = self.vault.accounts()
        accounts.sort(key=lambda a: (not a.favorite, -a.last_used_at))

        for account in accounts:
            card = AccountCard(account)
            card.clicked.connect(self.select_account)
            card.switch_requested.connect(self.switch_account)
            card.favorite_toggled.connect(self.toggle_favorite)
            card.context_requested.connect(self._show_card_menu)
            self.cards[account.id] = card
            self.list_layout.insertWidget(self.list_layout.count() - 1, card)

        self.empty_label.setVisible(not accounts)
        self._update_session_badges()

        if self.selected_id in self.cards:
            self.select_account(self.selected_id)
        elif accounts:
            self.select_account(accounts[0].id)
        else:
            self.selected_id = None
            self._set_detail_visible(False)
            self.detail_title.setText("アカウントを選択してください")
            self.detail_sub.clear()
        self._apply_filter(self.search.text())

    def _update_session_badges(self) -> None:
        for account_id, card in self.cards.items():
            account = self.vault.get(account_id)
            if not account:
                continue
            info = self.service.session_status(account)
            card.refresh(
                account,
                session_ok=info.valid and not info.expired,
                session_days=info.expires_in_days,
            )
            # refresh はアイコンを消すので、取得済みのものを貼り直す
            card.badge.set_rank(account.rank.tier, self._rank_pixmap(account.rank.tier))
            card.set_current(account_id == self.current_id)
            card.set_selected(account_id == self.selected_id)

    def _apply_filter(self, text: str) -> None:
        query = text.strip().lower()
        visible = 0
        for account_id, card in self.cards.items():
            account = self.vault.get(account_id)
            haystack = " ".join([
                account.label, account.riot_id, account.username, account.note,
                " ".join(account.tags), account.rank.tier_name,
            ]).lower() if account else ""
            match = not query or query in haystack
            card.setVisible(match)
            visible += match
        self.empty_label.setVisible(not visible and bool(query or not self.cards))
        if query and not visible:
            self.empty_label.setText("該当するアカウントがありません")
        elif not self.cards:
            self.empty_label.setText(
                "アカウントがありません。\n\n"
                "Riot Client でログインしてから\n「現在のを取り込む」を押すと登録できます。"
            )

    def select_account(self, account_id: str) -> None:
        self.selected_id = account_id
        for cid, card in self.cards.items():
            card.set_selected(cid == account_id)

        account = self.vault.get(account_id)
        if not account:
            return
        self._set_detail_visible(True)
        self.detail_title.setText(account.display_name)
        self.detail_sub.setText(
            f"{account.riot_id or account.username or '—'}   ·   {account.region.upper()}"
        )
        info = self.service.session_status(account)
        icon = self._rank_pixmap(account.rank.tier)
        self.detail.overview.update_account(account, info, icon)
        self.detail.history.set_message("「読み込む」で直近のランク変動を取得します")
        self.detail.inventory.set_message("「集計する」で所持スキンを一覧にします")

        can_switch = (info.valid and not info.expired) or bool(
            account.username and account.password
        )
        self.switch_button.setEnabled(can_switch and not self._busy)
        self.switch_button.setToolTip(
            "" if can_switch else
            "セッションが未保存で、パスワードも未登録のため切り替えられません"
        )

    # ==================================================================
    # 環境監視
    # ==================================================================
    def refresh_environment(self) -> None:
        env = self.service.environment()

        # 保存済みセッションを最新に保つ。refresh_token はクライアントが
        # 起動するたび更新され、古いコピーはその時点で失効するため。
        self._check_stuck()
        try:
            if self.service.sync_current_session() is not None:
                self._update_session_badges()
        except Exception:
            pass

        info = self.service.current_session_info()

        current = None
        if info.puuid:
            for account in self.vault.accounts():
                if account.puuid == info.puuid:
                    current = account
                    break

        new_id = current.id if current else None
        if new_id != self.current_id:
            self.current_id = new_id
            for cid, card in self.cards.items():
                card.set_current(cid == new_id)

        bits = [env.describe()]
        if current:
            bits.append(f"ログイン中: {current.display_name}")
        elif info.valid:
            bits.append("ログイン中: 未登録のアカウント")
        procs = self.service.running_processes()
        if any("VALORANT" in p for p in procs):
            bits.append("ゲーム起動中")

        self.env_label.setText("   ·   ".join(bits))
        color = theme.OK if env.installed else theme.WARN
        self.env_label.setStyleSheet(f"color:{color}; font-size:12px; border:none;")

    # ==================================================================
    # VALORANT のステータス (メンテナンス・障害)
    # ==================================================================
    def check_status(self) -> None:
        workers.run(
            self.service.check_status,
            on_done=self._on_status_checked,
            on_error=lambda m: diagnostics.log(f"ステータス確認に失敗: {m}"),
        )

    def _on_status_checked(self, result: tuple[bool, str]) -> None:
        active, summary = result
        self.status_banner_label.setText(summary)
        self.status_banner.setVisible(active)

    def open_settings(self) -> None:
        dialog = SettingsDialog(
            webhook_url=self.service.discord_webhook_url,
            step_delay=self.service.login_step_delay,
            current_version=__version__,
            parent=self,
        )
        dialog.check_button.clicked.connect(lambda: self._run_update_check(dialog))
        dialog.update_button.clicked.connect(lambda: self._start_update(dialog))
        if self._pending_release:
            dialog.set_update_available(self._pending_release.tag)
        if dialog.exec():
            self.service.discord_webhook_url = dialog.webhook_url()
            self.service.login_step_delay = dialog.step_delay()
            self.status.showMessage("設定を保存しました", 4000)

    # ==================================================================
    # 自動更新
    # ==================================================================
    UPDATE_CHECK_TIMEOUT_MS = 25_000

    def check_for_update(self) -> None:
        """起動時にそっと確認する。失敗しても黙っている (毎回警告を出さない)。"""
        self._run_update_check(dialog=None)

    def _run_update_check(self, dialog: SettingsDialog | None) -> None:
        if dialog:
            dialog.set_checking()

        # gh CLI が稀に応答を返さないことがある (実機で確認済み)。
        # updater 側にもタイムアウトは入れてあるが、万一それをすり抜けても
        # 設定画面が「確認中…」のまま固まって見えることがないよう、
        # ここでも一定時間で諦めて UI を戻す。
        token = object()
        self._update_check_token = token
        workers.run(
            updater.check_latest,
            on_done=lambda release: self._on_update_checked(release, dialog, token),
            on_error=lambda m: self._on_update_check_failed(m, dialog, token),
        )
        QTimer.singleShot(
            self.UPDATE_CHECK_TIMEOUT_MS,
            lambda: self._on_update_check_timeout(dialog, token),
        )

    def _on_update_checked(self, release: updater.Release,
                           dialog: SettingsDialog | None, token: object) -> None:
        if self._update_check_token is not token:
            return  # タイムアウトで既に諦めた後の遅れて来た結果
        self._update_check_token = None
        if not updater.is_newer(release.tag):
            self._pending_release = None
            self.update_button.setVisible(False)
            if dialog:
                dialog.set_up_to_date()
            return
        self._pending_release = release
        self.update_button.setText(f"更新: {release.tag}")
        self.update_button.setVisible(True)
        if dialog:
            dialog.set_update_available(release.tag)

    def _on_update_check_timeout(self, dialog: SettingsDialog | None, token: object) -> None:
        if self._update_check_token is not token:
            return  # 既に結果が来ている
        self._update_check_token = None
        diagnostics.log("更新確認がタイムアウトしました")
        if dialog:
            try:
                dialog.set_check_failed(
                    "確認がタイムアウトしました。ネットワークや gh CLI の状態を確認してください。"
                )
            except RuntimeError:
                pass  # ダイアログが既に閉じられている

    def _on_update_check_failed(self, message: str, dialog: SettingsDialog | None,
                                token: object) -> None:
        if self._update_check_token is not token:
            return  # タイムアウトで既に諦めた後の遅れて来た結果
        self._update_check_token = None
        diagnostics.log(f"更新確認に失敗/確認手段なし: {message}")
        if dialog:
            dialog.set_check_failed(message)

    def _start_update(self, dialog: SettingsDialog) -> None:
        dialog.close()
        self.on_update()

    def on_update(self) -> None:
        release = self._pending_release
        if not release:
            return

        if not getattr(sys, "frozen", False):
            # ソースから動かしているときは入れ替えようがないので、リリースページを見てもらう
            webbrowser.open(updater.RELEASES_URL)
            return

        answer = QMessageBox.question(
            self, "更新",
            f"{release.tag} に更新します。\n"
            "ダウンロードのあと、この画面はいったん閉じて起動し直します。\n"
            "続けますか？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        self.update_button.setEnabled(False)
        self.update_button.setText("更新中…")
        self.status.showMessage("新しいバージョンをダウンロードしています…")
        workers.run(
            self._download_and_apply_update, release,
            on_done=self._on_update_downloaded,
            on_error=self._on_update_failed,
        )

    def _download_and_apply_update(self, release: updater.Release) -> None:
        path = updater.download(release, self.vault.app_dir / "update")
        updater.apply_update(path)

    def _on_update_downloaded(self, _result) -> None:
        self.status.showMessage("入れ替えのため終了します…")
        # 入れ替え役が動き出したので、掴んでいる exe を手放すために終了する
        QTimer.singleShot(600, self.close)

    def _on_update_failed(self, message: str) -> None:
        release = self._pending_release
        self.update_button.setEnabled(True)
        if release:
            self.update_button.setText(f"更新: {release.tag}")
        self.status.showMessage("")
        QMessageBox.warning(self, "更新できませんでした", message)

    # ==================================================================
    # 操作
    # ==================================================================
    # 処理がこれ以上かかったら、何かが詰まったとみなして操作を戻す
    BUSY_LIMIT_SECONDS = 240

    def _check_stuck(self) -> None:
        """処理中のまま戻ってこない状態から復帰する。

        切り替えは待ち時間の長い処理なので、どこかで詰まるとボタンが
        押せないままになる。利用者からは「押しても何も起きない」に見えるので、
        上限を超えたら操作を戻して、何が起きたか伝える。
        """
        if not self._busy or not self._busy_since:
            return
        if time.time() - self._busy_since < self.BUSY_LIMIT_SECONDS:
            return
        diagnostics.log("処理が長すぎるので操作を戻した")
        self._set_busy(False)
        self.status.showMessage(
            "処理が終わらなかったので操作を戻しました。もう一度お試しください。"
        )

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        self._busy_since = time.time() if busy else 0.0
        for b in (self.switch_button, self.refresh_button,
                  self.refresh_all_button, self.import_button):
            b.setEnabled(not busy)
        if message:
            self.status.showMessage(message)

    def _error(self, title: str, message: str) -> None:
        self._set_busy(False)
        self.status.showMessage(message)
        QMessageBox.warning(self, title, message)

    def add_account(self) -> None:
        dialog = AccountDialog(parent=self)
        if dialog.exec():
            self.vault.add(dialog.result_account())
            self.reload_accounts()
            self.status.showMessage("アカウントを追加しました")

    def edit_account(self, account_id: str) -> None:
        account = self.vault.get(account_id)
        if not account:
            return
        dialog = AccountDialog(account, parent=self)
        if dialog.exec():
            self.vault.update(dialog.result_account())
            self.reload_accounts()
            self.status.showMessage("保存しました")

    def delete_account(self, account_id: str) -> None:
        account = self.vault.get(account_id)
        if not account:
            return
        answer = QMessageBox.question(
            self, "削除の確認",
            f"「{account.display_name}」を削除します。\n"
            "保存済みのセッションとログイン情報も消えます。よろしいですか？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.vault.remove(account_id)
            if self.selected_id == account_id:
                self.selected_id = None
            self.reload_accounts()
            self.status.showMessage("削除しました")

    def toggle_favorite(self, account_id: str) -> None:
        account = self.vault.get(account_id)
        if not account:
            return
        account.favorite = not account.favorite
        self.vault.update(account)
        self.reload_accounts()

    def import_current(self) -> None:
        self._set_busy(True, "現在のアカウントを取り込んでいます…")
        workers.run(
            self.service.import_current,
            on_done=self._on_imported,
            on_error=lambda m: self._error("取り込みに失敗しました", m),
            on_progress=self.status.showMessage,
        )

    def _on_imported(self, account: Account) -> None:
        self._set_busy(False, f"{account.display_name} を登録しました")
        self.selected_id = account.id
        self.reload_accounts()
        self.refresh_environment()
        answer = QMessageBox.question(
            self, "情報を取得しますか",
            f"「{account.display_name}」のランクと所持品を今すぐ取得しますか？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            self.refresh_selected()

    def capture_session(self, account_id: str) -> None:
        account = self.vault.get(account_id)
        if not account:
            return
        self._set_busy(True, "セッションを保存しています…")
        workers.run(
            self.service.capture_into, account,
            on_done=lambda _: (self._set_busy(False, "セッションを保存しました"),
                               self.reload_accounts()),
            on_error=lambda m: self._error("保存に失敗しました", m),
            on_progress=self.status.showMessage,
        )

    def renew_session(self, account_id: str) -> None:
        """再認証してセッションの有効期限を延ばす。切り替えは起きない。"""
        account = self.vault.get(account_id)
        if not account or self._busy:
            return
        self._set_busy(True, f"{account.display_name} のセッションを延長中…")
        workers.run(
            self.service.renew_session, account,
            on_done=self._on_renewed,
            on_error=lambda m: self._error("延長に失敗しました", m),
            on_progress=self.status.showMessage,
        )

    def _on_renewed(self, result: dict) -> None:
        account = self.vault.get(result["account_id"])
        name = account.display_name if account else "アカウント"
        self._set_busy(False)
        self.reload_accounts()
        if result.get("not_applicable"):
            message = (f"{name} は現行のセッション形式なので、延長操作は要りません。\n"
                       "このアカウントで Riot Client を起動すれば、"
                       "期限は自動で先に延びます。\n"
                       f"残り {result['after_days']:.0f} 日")
        elif result["extended"]:
            message = (f"{name} のセッションを延長しました。\n"
                       f"残り {result['before_days']:.0f} 日 → "
                       f"{result['after_days']:.0f} 日")
        else:
            message = (f"{name} のセッションは Riot 側で更新されませんでした。\n"
                       f"残り {result['after_days']:.0f} 日のままです。")
        self.status.showMessage(message.replace("\n", "  "))
        QMessageBox.information(self, "セッションの延長", message)

    # -- 切り替え -----------------------------------------------------------
    def switch_selected(self) -> None:
        if self.selected_id:
            self.switch_account(self.selected_id)

    def switch_account(self, account_id: str, force: bool = False) -> None:
        account = self.vault.get(account_id)
        if not account or self._busy:
            return
        self._set_busy(True, f"{account.display_name} に切り替えています…")
        workers.run(
            self.service.switch, account,
            launch_game=True, force=force,
            on_done=self._on_switched,
            on_error=lambda m: self._on_switch_failed(account_id, m),
            on_progress=self.status.showMessage,
        )

    def _on_switch_failed(self, account_id: str, message: str) -> None:
        self._set_busy(False)
        if "起動中" in message:
            answer = QMessageBox.question(
                self, "VALORANT が起動中",
                f"{message}\n\n強制的に終了して切り替えますか？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer == QMessageBox.Yes:
                self.switch_account(account_id, force=True)
            return
        self._error("切り替えに失敗しました", message)

    def _on_switched(self, result) -> None:
        account = self.vault.get(result.account_id)
        name = account.display_name if account else "アカウント"
        method = "セッション復元" if result.method == "session" else "パスワード自動入力"
        self._set_busy(False, f"{name} に切り替えました（{method}）")
        self.reload_accounts()
        self.refresh_environment()
        if result.warnings:
            QMessageBox.information(self, "切り替え完了", "\n".join(result.warnings))

    # -- 情報更新 -----------------------------------------------------------
    def refresh_selected(self) -> None:
        account = self.vault.get(self.selected_id) if self.selected_id else None
        if not account or self._busy:
            return
        self._set_busy(True, f"{account.display_name} を更新中…")
        workers.run(
            self.service.refresh, account,
            on_done=self._on_refreshed,
            on_error=lambda m: self._error("更新に失敗しました", m),
            on_progress=self.status.showMessage,
        )

    def _on_refreshed(self, account: Account) -> None:
        self._set_busy(False, f"{account.display_name} を更新しました")
        self.reload_accounts()
        if self.selected_id == account.id:
            self.select_account(account.id)

    def refresh_all(self) -> None:
        accounts = [a for a in self.vault.accounts() if a.session_saved]
        if not accounts:
            QMessageBox.information(
                self, "更新できません",
                "セッションが保存されたアカウントがありません。",
            )
            return
        self._set_busy(True, f"{len(accounts)} 件を更新中…")
        workers.run(
            self.service.refresh_many, accounts,
            on_done=self._on_refresh_all_done,
            on_error=lambda m: self._error("更新に失敗しました", m),
            on_progress=self.status.showMessage,
        )

    def _on_refresh_all_done(self, errors: dict) -> None:
        self._set_busy(False, "更新が完了しました" if not errors
                       else f"{len(errors)} 件が失敗しました")
        self.reload_accounts()
        if errors:
            lines = []
            for account_id, message in errors.items():
                account = self.vault.get(account_id)
                lines.append(f"・{account.display_name if account else account_id}: {message}")
            QMessageBox.warning(self, "一部が失敗しました", "\n".join(lines))

    # -- 戦績と所持品 -------------------------------------------------------
    def load_history(self) -> None:
        account = self.vault.get(self.selected_id) if self.selected_id else None
        if not account:
            return
        self.detail.history.set_message("読み込み中…")
        workers.run(
            self.service.match_history, account,
            on_done=self._on_history,
            on_error=lambda m: self.detail.history.set_message(m),
        )

    def _on_history(self, matches) -> None:
        try:
            maps = self.service.content.maps()
        except Exception:
            maps = {}
        self.detail.history.set_matches(matches, maps, self.service.content.tier_names())

    def load_inventory(self) -> None:
        account = self.vault.get(self.selected_id) if self.selected_id else None
        if not account:
            return
        if not account.inventory.skin_level_ids:
            self.detail.inventory.set_message(
                "所持品が未取得です。先に「更新」を実行してください。"
            )
            return
        self.detail.inventory.set_message("集計中…")
        workers.run(
            self.service.summarize_inventory, account,
            on_done=self.detail.inventory.set_summary,
            on_error=lambda m: self.detail.inventory.set_message(m),
        )

    # ==================================================================
    # メニュー
    # ==================================================================
    def _show_card_menu(self, account_id: str, position) -> None:
        self.select_account(account_id)
        self._build_menu(account_id).exec(position)

    def _show_more_menu(self) -> None:
        if self.selected_id:
            self._build_menu(self.selected_id).exec(
                self.more_button.mapToGlobal(self.more_button.rect().bottomLeft())
            )

    def _build_menu(self, account_id: str) -> QMenu:
        account = self.vault.get(account_id)
        menu = QMenu(self)
        menu.setStyleSheet(theme.STYLESHEET)

        menu.addAction("このアカウントで起動", lambda: self.switch_account(account_id))
        menu.addAction("情報を更新", lambda: (self.select_account(account_id),
                                              self.refresh_selected()))
        menu.addSeparator()
        capture = menu.addAction(
            "現在のログインをこのアカウントに保存",
            lambda: self.capture_session(account_id),
        )
        capture.setToolTip("いま Riot Client にログインしている状態を、このアカウントの枠に入れます")

        renew = menu.addAction("セッションを延長",
                               lambda: self.renew_session(account_id))
        renew.setToolTip("再認証して有効期限を延ばします。切り替えは起きません")
        renew.setEnabled(bool(account and account.session_saved))
        menu.addSeparator()

        if account and account.riot_id:
            menu.addAction("Riot ID をコピー",
                           lambda: self._copy(account.riot_id))
        if account and account.username:
            menu.addAction("ユーザー名をコピー",
                           lambda: self._copy(account.username))
        if account and account.password:
            menu.addAction("パスワードをコピー",
                           lambda: self._copy(account.password, "パスワードをコピーしました"))
        menu.addSeparator()
        menu.addAction("編集", lambda: self.edit_account(account_id))
        delete = menu.addAction("削除", lambda: self.delete_account(account_id))
        delete.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_TrashIcon))
        return menu

    def show_diagnostics(self) -> None:
        """環境情報を出す。不具合報告のときにそのまま貼れる。"""
        report = diagnostics.environment_report()
        log = diagnostics.log_path()
        if log.is_file():
            report += f"\n\nクラッシュログ: {log}"

        box = QMessageBox(self)
        box.setWindowTitle("診断情報")
        box.setText("この内容をそのまま不具合報告に貼れます。")
        box.setDetailedText(report)
        copy = box.addButton("コピー", QMessageBox.ActionRole)
        box.addButton("閉じる", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is copy:
            self._copy(report, "診断情報をコピーしました")

    def _copy(self, text: str, message: str = "コピーしました") -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(text)
        self.status.showMessage(message, 4000)

    def closeEvent(self, event):
        # 走行中のバックグラウンド処理が終わってから閉じる
        self._env_timer.stop()
        self._status_timer.stop()
        workers.wait_for_all(3000)
        super().closeEvent(event)

    # ==================================================================
    # ランクアイコン
    # ==================================================================
    def _load_rank_icons(self) -> None:
        workers.run(
            self.service.content.competitive_tiers,
            on_done=self._on_rank_icons,
            on_error=lambda _: None,
        )

    def _on_rank_icons(self, tiers: dict) -> None:
        self._rank_icons = {t: info.get("icon") for t, info in tiers.items()
                            if info.get("icon")}
        if self.selected_id:
            self.select_account(self.selected_id)
        for account_id, card in self.cards.items():
            account = self.vault.get(account_id)
            if account:
                card.badge.set_rank(account.rank.tier, self._rank_pixmap(account.rank.tier))

    def _rank_pixmap(self, tier: int):
        url = self._rank_icons.get(tier)
        return self.icons.pixmap(url) if url else None

    def _on_icon_loaded(self, url: str, path: str) -> None:
        pixmap = self.icons.take(url, path)
        if not pixmap:
            return
        for tier, tier_url in self._rank_icons.items():
            if tier_url != url:
                continue
            for account_id, card in self.cards.items():
                account = self.vault.get(account_id)
                if account and account.rank.tier == tier:
                    card.badge.set_rank(tier, pixmap)
            selected = self.vault.get(self.selected_id) if self.selected_id else None
            if selected and selected.rank.tier == tier:
                self.detail.overview.badge.set_rank(tier, pixmap)
