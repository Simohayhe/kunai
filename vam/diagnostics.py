"""クラッシュログと診断情報。

exe 配布だと標準エラーがどこにも出ないので、
落ちた理由と環境の素性を後から追えるようにしておく。
"""
from __future__ import annotations

import platform
import sys
import time
import traceback
from pathlib import Path

from . import paths
from .storage import default_app_dir


def log_path() -> Path:
    return default_app_dir() / "crash.log"


def write_crash(exc_type, exc_value, exc_tb) -> Path | None:
    """未捕捉例外をログに追記する。書けなければ None。"""
    try:
        target = log_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            f.write(environment_report())
            f.write("\n")
            traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
        return target
    except OSError:
        return None


def environment_report() -> str:
    """不具合報告に貼れる環境情報。個人を特定する値は入れない。"""
    env = paths.detect()
    try:
        from .riot import process
        running = ", ".join(process.running()) or "なし"
    except Exception as exc:
        running = f"取得失敗: {exc}"

    lines = [
        f"アプリ            : VALORANT Account Manager {version()}",
        f"実行形態          : {'exe (PyInstaller)' if frozen() else 'ソース'}",
        f"Python            : {platform.python_version()}",
        f"OS                : {platform.platform()}",
        f"Riot Client       : {env.client_exe or '見つからない'}",
        f"データ置き場      : {env.data_root}",
        f"データ置き場の有無: {env.data_root.is_dir()}",
        f"セッションファイル: {[p.name for p in env.found_session_files] or 'なし'}",
        f"lockfile          : {'あり' if env.lockfile else 'なし'}",
        f"起動中プロセス    : {running}",
        f"保管庫            : {default_app_dir()}",
    ]
    try:
        from PySide6 import __version__ as pyside_version
        lines.insert(3, f"PySide6           : {pyside_version}")
    except ImportError:
        pass
    return "\n".join(lines)


def frozen() -> bool:
    return getattr(sys, "frozen", False)


def version() -> str:
    return "0.2.0"


def install_excepthook(on_crash=None) -> None:
    """未捕捉例外をログに残す。on_crash があればログの場所を渡して呼ぶ。"""
    previous = sys.excepthook

    def hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            previous(exc_type, exc_value, exc_tb)
            return
        target = write_crash(exc_type, exc_value, exc_tb)
        if on_crash:
            try:
                on_crash(target, exc_value)
            except Exception:
                pass
        previous(exc_type, exc_value, exc_tb)

    sys.excepthook = hook
