"""Riot Client / VALORANT の起動。"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from .. import paths
from . import process

PRODUCT_VALORANT = ("valorant", "live")
PRODUCT_LOL = ("league_of_legends", "live")


class LaunchError(Exception):
    pass


def launch(product: tuple[str, str] | None = PRODUCT_VALORANT,
           exe: Path | None = None) -> subprocess.Popen:
    """Riot Client を起動する。product を渡すとそのゲームまで直行する。

    product=None なら Riot Client のログイン画面だけを出す。
    """
    exe = exe or paths.riot_client_exe()
    if not exe:
        raise LaunchError(
            "RiotClientServices.exe が見つかりません。VALORANT がインストールされていないか、"
            "インストール先を設定で指定してください。"
        )

    args = [str(exe)]
    if product:
        name, patchline = product
        args += [f"--launch-product={name}", f"--launch-patchline={patchline}"]

    # .cmd (モック) は shell 経由でないと起動できない
    if exe.suffix.lower() in (".cmd", ".bat"):
        return subprocess.Popen(args, shell=True, cwd=str(exe.parent))

    return subprocess.Popen(
        args, cwd=str(exe.parent),
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
    )


def launch_custom(path: str) -> None:
    """利用者が指定したショートカット / 実行ファイルで VALORANT を起動する。

    Riot Client の代わりに使う。Kunai はそれが何をするかには関与しない
    (どんなツールを通すかは利用者の自己責任)。
    """
    target = Path(path.strip().strip('"'))
    if not target.exists():
        raise LaunchError(f"指定された起動コマンドが見つかりません: {target}")
    suffix = target.suffix.lower()
    if suffix == ".exe":
        subprocess.Popen([str(target)], cwd=str(target.parent),
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    elif suffix in (".bat", ".cmd"):
        subprocess.Popen([str(target)], shell=True, cwd=str(target.parent))
    else:
        os.startfile(str(target))   # ショートカット (.lnk) など。関連付けで開く


def wait_for_client(timeout: float = 60.0) -> bool:
    """Riot Client のローカル API が応答し始めるまで待つ。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if paths.lockfile_path():
            return True
        time.sleep(0.5)
    return False


def restart_into(product: tuple[str, str] | None = PRODUCT_VALORANT,
                 stop_timeout: float = 12.0) -> subprocess.Popen:
    """今動いているクライアントを落としてから起動し直す。"""
    process.stop_all(timeout=stop_timeout)
    return launch(product)
