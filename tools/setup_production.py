"""本番の保管庫を用意する。検証用保管庫のアカウントを引き継げる。

    python tools/setup_production.py

マスターパスワードはこのスクリプトの中では扱わない。アプリと同じ
初期設定ダイアログを出し、利用者が直接入力する。

検証用保管庫 (%LOCALAPPDATA%\\ValorantAccountManager-verify) に
アカウントがあれば、セッションごと本番へ複製する。再ログインは要らない。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from vam.models import Account  # noqa: E402
from vam.riot import session  # noqa: E402
from vam.service import AccountService  # noqa: E402
from vam.storage import Vault, default_app_dir  # noqa: E402
from vam.ui.dialogs import SetupDialog, UnlockDialog  # noqa: E402


def verify_vault_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA", str(Path.home()))
    return Path(base) / "ValorantAccountManager-verify"


def open_production(app: QApplication) -> Vault | None:
    """本番の保管庫を開く。無ければ初期設定から。"""
    vault = Vault(default_app_dir())
    if not vault.initialized:
        print("本番の保管庫がありません。初期設定ダイアログを出します。")
        dialog = SetupDialog(vault)
        if dialog.exec() != QDialog.Accepted:
            print("中止しました。")
            return None
        print("保管庫を作成しました。")
        return vault

    if vault.needs_password:
        print("マスターパスワードの入力を求めます。")
        dialog = UnlockDialog(vault)
        if dialog.exec() != QDialog.Accepted:
            print("中止しました。")
            return None
    else:
        vault.open()
    print("保管庫を開きました。")
    return vault


def migrate(source: Vault, target: Vault) -> tuple[int, int]:
    """source のアカウントを target へ複製する。既存は飛ばす。"""
    copied = skipped = 0
    for account in source.accounts():
        if any(a.puuid and a.puuid == account.puuid for a in target.accounts()):
            print(f"  - {account.display_name}: 登録済みなので飛ばす")
            skipped += 1
            continue

        blobs = source.read_session_blobs(account.id)
        info = session.inspect_blobs(blobs) if blobs else session.SessionInfo()
        if not info.valid:
            print(f"  - {account.display_name}: セッションが無いので飛ばす")
            skipped += 1
            continue

        # id は付け直す。保管庫ごとに独立させておく
        fresh = Account(
            label=account.label, riot_id=account.riot_id, puuid=account.puuid,
            region=account.region, color=account.color, note=account.note,
            favorite=account.favorite, username=account.username,
            password=account.password,
            session_saved=True, session_saved_at=account.session_saved_at,
            rank=account.rank, wallet=account.wallet, inventory=account.inventory,
        )
        target.add(fresh)
        for rel, data in blobs.items():
            target.write_session_blob(fresh.id, rel, data)
        target.update(fresh)
        print(f"  + {fresh.display_name} / {fresh.riot_id} "
              f"(残り {info.expires_in_days:.0f} 日)")
        copied += 1
    return copied, skipped


def main() -> int:
    app = QApplication(sys.argv)

    print("=" * 62)
    print("本番の保管庫を用意します")
    print("=" * 62)
    print(f"置き場: {default_app_dir()}")

    target = open_production(app)
    if target is None:
        return 1

    source_dir = verify_vault_dir()
    if source_dir.is_dir():
        source = Vault(source_dir)
        if source.initialized and not source.needs_password:
            source.open()
            print(f"\n検証用保管庫から引き継ぎます ({source_dir}):")
            copied, skipped = migrate(source, target)
            print(f"\n  複製 {copied} 件 / 飛ばし {skipped} 件")
        else:
            print("\n検証用保管庫は開けませんでした（パスワード付き？）。引き継ぎは省略します。")
    else:
        print("\n検証用保管庫はありません。引き継ぎは省略します。")

    svc = AccountService(target)
    print("\n=== 本番の保管庫の中身 ===")
    accounts = target.accounts()
    if not accounts:
        print("  (空) アプリの「現在のを取り込む」で登録してください")
    for a in accounts:
        s = svc.session_status(a)
        print(f"  - {a.display_name:18} {a.riot_id:24} "
              f"残り {s.expires_in_days:5.1f} 日")

    QMessageBox.information(
        None, "セットアップ完了",
        f"本番の保管庫を用意しました。\n\n"
        f"置き場: {default_app_dir()}\n"
        f"アカウント: {len(accounts)} 件\n\n"
        "アプリを起動すると、この内容で使えます。",
    )
    print("\n完了しました。アプリを起動すれば使えます。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
