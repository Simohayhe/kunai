"""ブラウザでページを開く。

Riot のアカウントページ (Riot ID の変更など) は、ブラウザのログイン状態で
決まる。別のアカウントのログインが残っていると使いにくいので、できれば
シークレット (InPrivate) ウィンドウで開く。見つからなければ普通に開く。
"""
from __future__ import annotations

import os
import subprocess
import webbrowser
from pathlib import Path

ACCOUNT_PAGE_URL = "https://account.riotgames.com/"


def _candidates() -> list[tuple[str, Path, str]]:
    """(表示名, 実行ファイル, シークレットのオプション) の候補。Chrome を優先。"""
    out = []
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if not base:
            continue
        root = Path(base)
        out.append(("Chrome", root / "Google/Chrome/Application/chrome.exe", "--incognito"))
        out.append(("Edge", root / "Microsoft/Edge/Application/msedge.exe", "--inprivate"))
    return out


def find_private_browser() -> tuple[str, Path, str] | None:
    for name, exe, flag in _candidates():
        if exe.is_file():
            return name, exe, flag
    return None


def open_private(url: str = ACCOUNT_PAGE_URL) -> str:
    """シークレットで開く。開いた方法を文字列で返す (画面の案内用)。"""
    found = find_private_browser()
    if found:
        name, exe, flag = found
        try:
            subprocess.Popen([str(exe), flag, url])
            return f"{name} のシークレットウィンドウ"
        except OSError:
            pass
    webbrowser.open(url)
    return "既定のブラウザ"
