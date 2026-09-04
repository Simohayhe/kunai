"""2 アカウント間の切り替えを実機で検証する。

1 アカウントだけでは確かめられない部分 (切り替え前の退避、puuid による
現在アカウントの判定、往復してもセッションが壊れないこと) を通しで見る。

使い方:

    # A でログインした状態で
    python tools/verify_switch.py add

    # Riot Client で B にログインし直してから
    python tools/verify_switch.py add

    # 2 件揃ったら
    python tools/verify_switch.py run

保管庫は %LOCALAPPDATA%\\ValorantAccountManager-verify に作る。
本番の保管庫には触らない。list で中身、clear で消せる。
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vam.riot import launcher, localapi, process, session  # noqa: E402
from vam.service import AccountService, ServiceError  # noqa: E402
from vam.storage import Vault  # noqa: E402

OK, NG, WARN = "[ok]  ", "[NG]  ", "[--]  "


def vault_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA", str(Path.home()))
    return Path(base) / "ValorantAccountManager-verify"


def open_vault() -> Vault:
    vault = Vault(vault_dir())
    if vault.initialized:
        vault.open()
    else:
        vault.initialize()
    return vault


def step(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 56 - len(title)))


def cmd_add() -> int:
    """今ログインしているアカウントを検証用の保管庫に取り込む。"""
    vault = open_vault()
    svc = AccountService(vault)

    info = session.current_session_info()
    if not info.valid:
        print(f"{NG}ログイン済みセッションがありません。")
        print("      Riot Client でログインしてから、もう一度実行してください。")
        return 1

    try:
        account = svc.import_current()
    except ServiceError as exc:
        print(f"{WARN}{exc}")
        return 0

    print(f"{OK}取り込みました: {account.display_name} / "
          f"{account.riot_id or '(Riot ID 不明)'} / 残り "
          f"{svc.session_status(account).expires_in_days:.0f} 日")
    cmd_list()
    return 0


def cmd_list() -> int:
    vault = open_vault()
    svc = AccountService(vault)
    accounts = vault.accounts()
    print(f"\n保管庫 ({vault_dir()}): {len(accounts)} 件")
    for a in accounts:
        s = svc.session_status(a)
        print(f"  - {a.display_name:20} {a.riot_id or '(不明)':22} "
              f"残り {s.expires_in_days:5.1f} 日  puuid={a.puuid[:8]}…")
    if len(accounts) < 2:
        print("\n  2 件揃ったら `python tools/verify_switch.py run` で切り替えを検証できます。")
    return 0


def cmd_clear() -> int:
    shutil.rmtree(vault_dir(), ignore_errors=True)
    print(f"{OK}検証用の保管庫を削除しました: {vault_dir()}")
    return 0


def _wait_for_client(timeout: float = 180) -> localapi.LocalSession | None:
    """クライアントが起動してログイン状態になるまで待つ。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        client = localapi.LocalClient()
        if client.available:
            try:
                return client.session()
            except localapi.LocalApiError:
                pass
        time.sleep(2)
    return None


def cmd_run() -> int:
    vault = open_vault()
    svc = AccountService(vault)
    accounts = vault.accounts()
    if len(accounts) < 2:
        print(f"{NG}アカウントが {len(accounts)} 件しかありません。")
        print("      それぞれでログインして `add` を 2 回実行してください。")
        return 1

    a, b = accounts[0], accounts[1]
    print("=" * 64)
    print(f"切り替え検証: {a.display_name} <-> {b.display_name}")
    print("=" * 64)

    if process.game_running():
        print(f"{NG}VALORANT が起動中です。終了してから実行してください。")
        return 1

    failures = 0
    for source, target in ((a, b), (b, a)):
        step(f"{target.display_name} へ切り替え")
        before = svc.session_status(source)
        try:
            result = svc.switch(target, launch_game=False,
                                progress=lambda m: print("   ", m))
        except ServiceError as exc:
            print(f"{NG}{exc}")
            failures += 1
            continue

        print(f"      方式={result.method} 警告={result.warnings or 'なし'}")

        now = session.current_session_info()
        if now.puuid == target.puuid:
            print(f"{OK}セッションが {target.display_name} になった "
                  f"({now.riot_id or now.puuid[:8]})")
        else:
            print(f"{NG}切り替わっていない (puuid={now.puuid[:8]}…)")
            failures += 1

        # 切り替え元のセッションが保管庫に残っているか
        kept = svc.session_status(source)
        if kept.valid and not kept.expired:
            print(f"{OK}{source.display_name} のセッションも保持されている "
                  f"(残り {kept.expires_in_days:.0f} 日)")
        else:
            print(f"{NG}{source.display_name} のセッションが失われた "
                  f"(切り替え前は残り {before.expires_in_days:.0f} 日)")
            failures += 1

        # 実際にクライアントを起動して、そのアカウントで入れるか
        step(f"{target.display_name} でクライアントを起動")
        launcher.launch(product=None)
        live = _wait_for_client()
        if live is None:
            print(f"{NG}クライアントがログイン状態になりませんでした")
            failures += 1
        elif live.puuid == target.puuid:
            print(f"{OK}ログイン済みで起動 ({live.riot_id or live.puuid[:8]})")
        else:
            print(f"{NG}別のアカウントで起動した ({live.riot_id or live.puuid[:8]})")
            failures += 1

    step("結果")
    if failures:
        print(f"{NG}{failures} 件の問題がありました")
    else:
        print(f"{OK}往復とも成功。両方のセッションが保たれています")
    print(f"\n検証用の保管庫: {vault_dir()}")
    print("不要になったら `python tools/verify_switch.py clear` で消せます")
    return 1 if failures else 0


COMMANDS = {"add": cmd_add, "list": cmd_list, "run": cmd_run, "clear": cmd_clear}


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "list"
    if command not in COMMANDS:
        print(__doc__)
        return 2
    return COMMANDS[command]()


if __name__ == "__main__":
    sys.exit(main())
