"""GitHub Releases を見て、新しい exe に入れ替える。

Meridian (ark-breeding-timer) の updater.py と同じ方式に揃えてある。
以前は requests ライブラリ + gh CLI/トークンのフォールバックを使っていたが、
ビルドした exe だと確認処理が「確認中…」のまま返ってこないことが実機で
確認された (公開リポジトリなので認証は本来不要なのに、gh CLI 呼び出しの
フォールバック経路まで用意していた分、複雑で切り分けづらかった)。
Meridian は標準ライブラリの urllib.request だけで同じ処理を安定して
動かせているため、同じ作りに寄せてある。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .version import __version__

REPO = "Simohayhe/kunai"
ASSET_NAME = "Kunai.exe"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"
USER_AGENT = "Kunai-Updater"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --------------------------------------------------------------------------
# バージョン比較
# --------------------------------------------------------------------------
def parse_version(text: str) -> tuple[int, ...]:
    """'v1.2.3' や '1.2' を (1,2,3) / (1,2) にする。数字以外は無視する。"""
    numbers = re.findall(r"\d+", str(text or ""))
    return tuple(int(n) for n in numbers) or (0,)


def is_newer(candidate: str, current: str = __version__) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    # (1,2) と (1,2,0) を同じ扱いにするため長さを揃える
    length = max(len(a), len(b))
    a += (0,) * (length - len(a))
    b += (0,) * (length - len(b))
    return a > b


# --------------------------------------------------------------------------
# リリースを見る
# --------------------------------------------------------------------------
@dataclass
class Release:
    tag: str
    name: str
    notes: str
    asset_url: str | None  # ダウンロード先 (browser_download_url)。無ければ添付なし


class UpdateUnavailable(Exception):
    """更新の確認に失敗した。"""


def check_latest(timeout: float = 10.0) -> Release:
    """最新リリースを見に行く。公開リポジトリなので認証は要らない。

    Meridian の check() と同じく、例外は種類を選ばず全部まとめて
    UpdateUnavailable にする (呼び側は「確認できなかった」とだけ扱えばよい)。
    """
    req = urllib.request.Request(
        API_URL, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except Exception as exc:
        raise UpdateUnavailable(
            f"リリース情報を確認できませんでした ({exc.__class__.__name__})。"
            "ネットワーク接続を確認してください。"
        ) from exc

    asset = next(
        (a for a in data.get("assets") or [] if a.get("name") == ASSET_NAME), None
    )
    return Release(
        tag=data.get("tag_name") or "",
        name=data.get("name") or "",
        notes=data.get("body") or "",
        asset_url=asset.get("browser_download_url") if asset else None,
    )


# --------------------------------------------------------------------------
# ダウンロード
# --------------------------------------------------------------------------
def download(release: Release, dest_dir: Path, timeout: float = 60.0) -> Path:
    """新しい exe を落として、その置き場所を返す。"""
    if not release.asset_url:
        raise RuntimeError(f"リリース {release.tag} に {ASSET_NAME} が添付されていません。")

    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"{ASSET_NAME}.new"
    target.unlink(missing_ok=True)

    req = urllib.request.Request(release.asset_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp, target.open("wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            f.write(chunk)
        _ = total  # 進捗表示は今のところ使っていない

    if target.stat().st_size < 1_000_000:
        raise RuntimeError("落としたファイルが小さすぎます。中断しました。")
    return target


# --------------------------------------------------------------------------
# 入れ替え
# --------------------------------------------------------------------------
# Meridian と同じ、バッチ + tasklist によるポーリング待ちにしてある。
# PowerShell の Wait-Process 版よりシンプルで、実機で安定して動くことが確認済み。
BAT_TEMPLATE = """@echo off
chcp 65001 >nul
rem Kunai の更新用。終わったら自分を消す。
:wait
tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul
if not errorlevel 1 (
  ping -n 2 127.0.0.1 >nul
  goto wait
)
move /y "{new_file}" "{target}" >nul
start "" "{target}"
del "%~f0"
"""


def apply_update(new_file: Path) -> None:
    """GUI を終了させてから入れ替えるためのバッチを起動する。

    Windows は実行中の exe を上書きできないので、「こちらが終わるのを待ってから
    置き換えて起動し直す」係を別プロセスで走らせる。
    呼び出した側は、これを呼んだらすぐ終了すること。
    """
    if not getattr(sys, "frozen", False):
        raise RuntimeError("ソースから動かしているときは入れ替えできません。")

    target = Path(sys.executable).resolve()
    bat_path = Path(tempfile.gettempdir()) / "kunai_update.bat"
    bat_path.write_text(
        BAT_TEMPLATE.format(pid=os.getpid(), new_file=str(new_file), target=str(target)),
        encoding="utf-8",
    )

    subprocess.Popen(
        ["cmd", "/c", str(bat_path)],
        creationflags=_NO_WINDOW, close_fds=True,
    )
