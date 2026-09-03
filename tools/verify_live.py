"""ログイン済みの実アカウントで、通しの動作を検証する。

ログインした直後にこれを一度流せば、未検証だった部分が全部確かめられる。

    python tools/verify_live.py

やること (読み取りと再認証だけ):
  1. セッションファイルの解析
  2. 起動中クライアントのローカル API
  3. 一時保管庫への取り込み
  4. cookie 再認証 (RSO reauth)
  5. cookie ローテーションによるセッション延長  ← 最大の未検証点
  6. ランク・ウォレット・所持品・戦績の取得
  7. 所持品の名前解決

やらないこと:
  - プロセスの終了、アカウントの切り替え、Riot 側ファイルへの書き込み
  - 本番の保管庫への書き込み (一時ディレクトリを使う)
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vam.riot import api, localapi, process, session  # noqa: E402
from vam.service import AccountService  # noqa: E402
from vam.storage import Vault  # noqa: E402

OK, NG, WARN = "[ok]  ", "[NG]  ", "[--]  "


def mask(value: str, keep: int = 8) -> str:
    return f"{value[:keep]}…({len(value)}文字)" if value else "(なし)"


def step(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 58 - len(title)))


def _forbid(*_a, **_kw):
    raise AssertionError("この検証では呼んではいけない関数が呼ばれました")


def main() -> int:
    # 安全装置。Riot を壊しうる経路を物理的に塞ぐ
    process.stop_all = _forbid
    session.restore = _forbid
    session.clear_current = _forbid

    print("=" * 66)
    print("実アカウントでの通し検証 (切り替えは行わない)")
    print("=" * 66)

    # ---------------------------------------------------------------- 1
    step("1. セッションファイル")
    info = session.current_session_info()
    print(f"  読めた cookie : {sorted(info.cookies)}")
    print(f"  puuid         : {mask(info.puuid)}")
    if not info.valid:
        print(f"\n{NG}ログイン済みセッションがありません。")
        print("      Riot Client で「サインイン状態を維持」を有効にしてログインしてください。")
        return 1
    print(f"{OK}有効。残り {info.expires_in_days:.1f} 日 "
          f"({time.strftime('%Y/%m/%d %H:%M', time.localtime(info.expires_at))} まで)")

    # ---------------------------------------------------------------- 2
    step("2. ローカル API")
    client = localapi.LocalClient()
    if not client.available:
        print(f"{WARN}Riot Client が起動していないので省略")
    else:
        try:
            s = client.session()
            print(f"{OK}Riot ID {s.riot_id or '(取得できず)'} / region {s.region or '?'}")
            print(f"      puuid 一致: {s.puuid == info.puuid}")
        except localapi.LocalApiError as exc:
            print(f"{NG}{exc}")

    # ---------------------------------------------------------------- 3
    step("3. 一時保管庫への取り込み")
    tmp = Path(tempfile.mkdtemp(prefix="vam-live-"))
    vault = Vault(tmp)
    vault.initialize()
    svc = AccountService(vault)
    account = svc.import_current(label="実機検証")
    print(f"{OK}{account.display_name} / {account.riot_id or '(未取得)'} / {account.region}")
    stored = svc.session_status(account)
    print(f"      保存後の解析: 有効={stored.valid} 残り={stored.expires_in_days:.1f}日")
    print(f"      保管庫: {tmp}")

    # ---------------------------------------------------------------- 4
    step("4. cookie 再認証 (RSO reauth)")
    before = svc.session_status(account)
    try:
        result = svc.authenticate(account)
    except Exception as exc:
        print(f"{NG}{type(exc).__name__}: {exc}")
        print("\n      ここで失敗する場合、TLS の cipher 並びか cookie の扱いが原因。")
        print(f"      保管庫を残してあります: {tmp}")
        return 2
    print(f"{OK}accessToken {mask(result.access_token, 12)}")
    print(f"      entitlement {mask(result.entitlements_token, 12)}")
    print(f"      Riot ID {result.riot_id or '(取得できず)'} / region {result.region or '?'}")
    print(f"      返ってきた cookie: {sorted(result.cookies)}")

    # ---------------------------------------------------------------- 5
    step("5. セッション延長 (cookie ローテーション)")
    after = svc.session_status(account)
    rotated = result.cookies.get("ssid") and \
        result.cookies["ssid"] != before.cookies.get("ssid")
    print(f"      ssid が更新された    : {bool(rotated)}")
    print(f"      期限に付いてきた情報 : {sorted(result.cookie_expiries) or 'なし'}")
    print(f"      残り日数 {before.expires_in_days:.2f} -> {after.expires_in_days:.2f}")
    if after.expires_at > before.expires_at:
        print(f"{OK}延長された")
    elif rotated:
        print(f"{WARN}cookie は更新されたが期限は延びなかった")
    else:
        print(f"{WARN}Riot は cookie を更新しなかった (延長は起きない)")

    # ---------------------------------------------------------------- 6
    step("6. リモート API")
    try:
        version = svc.content.client_version()
    except Exception:
        version = ""
    remote = api.ValorantApi(result, region=result.region or account.region,
                             client_version=version)
    print(f"      shard = {remote.shard}")

    names = {}
    try:
        names = svc.content.tier_names()
    except Exception:
        pass

    for label, fn in (
        ("ランク", lambda: _rank(remote, names)),
        ("ウォレット", lambda: _wallet(remote)),
        ("所持品", lambda: _entitlements(remote)),
        ("戦績", lambda: _history(remote)),
    ):
        try:
            print(f"{OK}{label}: {fn()}")
        except Exception as exc:
            print(f"{NG}{label}: {type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- 7
    step("7. 所持品の名前解決")
    try:
        account = svc.refresh(account, fetch_inventory=True)
        summary = svc.summarize_inventory(account)
        print(f"{OK}所持スキン {summary['skin_count']} 種類 / "
              f"エージェント {summary['agent_count']}/{summary['agent_total']}")
        print(f"      レア度別: {summary['by_tier']}")
        for s in summary["skins"][:5]:
            print(f"        {s['tier_name']:16} {s['name']}")
    except Exception as exc:
        print(f"{NG}{type(exc).__name__}: {exc}")

    print("\n" + "=" * 66)
    print(f"一時保管庫: {tmp}  (不要なら削除してよい)")
    print("Riot 側のファイルとプロセスには一切触れていない")
    print("=" * 66)
    return 0


def _rank(remote: api.ValorantApi, names: dict) -> str:
    mmr = remote.mmr()
    return (f"{names.get(mmr.tier, mmr.tier_label)} {mmr.rr} RR / "
            f"{mmr.wins}勝{mmr.games}戦 / 最高 "
            f"{names.get(mmr.peak_tier, mmr.peak_tier_label)}")


def _wallet(remote: api.ValorantApi) -> str:
    w = remote.wallet()
    return f"VP {w['vp']:,} / RP {w['rp']:,} / KC {w['kc']:,}"


def _entitlements(remote: api.ValorantApi) -> str:
    return (f"skinLevel {len(remote.entitlements('skins'))} / "
            f"エージェント {len(remote.entitlements('agents'))}")


def _history(remote: api.ValorantApi) -> str:
    hist = remote.competitive_history(count=5)
    if not hist:
        return "コンペの記録なし"
    latest = hist[0]
    return f"{len(hist)} 件、直近 {latest.rr_earned:+d} RR → {latest.rr_after} RR"


if __name__ == "__main__":
    sys.exit(main())
