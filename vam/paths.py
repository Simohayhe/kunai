"""Riot Client / VALORANT のインストール先とデータ置き場を探す。

実機がない環境でも動くように、すべての探索は「見つからなければ None」を返し、
VAM_RIOT_ROOT 環境変数でモック環境に丸ごと差し替えられるようにしてある。
"""
from __future__ import annotations

import os
import winreg
from dataclasses import dataclass
from pathlib import Path

# モック環境から差し込むための環境変数
ENV_OVERRIDE_LOCALAPPDATA = "VAM_LOCALAPPDATA"
ENV_OVERRIDE_CLIENT_EXE = "VAM_RIOT_CLIENT_EXE"

# Riot Client がログイン状態を保持しているファイル群。
# ファイル名は Riot 側の都合で増減するので「あるものだけ」を対象にする。
SESSION_FILE_CANDIDATES = (
    "Data/RiotGamesPrivateSettings.yaml",
    "Data/RiotClientPrivateSettings.yaml",
    "Config/RiotGamesPrivateSettings.yaml",
    "Config/RiotClientPrivateSettings.yaml",
    "Beta/Data/RiotGamesPrivateSettings.yaml",
    "Beta/Data/RiotClientPrivateSettings.yaml",
)

LOCKFILE_CANDIDATES = (
    "Config/lockfile",
    "Beta/Config/lockfile",
)


def safe_is_file(path: Path) -> bool:
    """path.is_file() だが、通常と違う OSError で丸ごと落ちないようにする。

    実機で報告された不具合: ユーザーの環境では Riot Client のデータ置き場
    (%LOCALAPPDATA%\\Riot Games\\...) の経路上に「信頼されていない
    マウントポイント」(WinError 448) があり、素の is_file() がそこで
    未捕捉の OSError を投げてアプリごと落ちていた。Path.is_file() は
    ENOENT 等ごく一部の OSError しか黙って False にしないため、これは
    素通りする。ここで広く OSError を捕まえ、判定不能なら「無い」扱いにする。
    """
    try:
        return path.is_file()
    except OSError:
        return False


def local_appdata() -> Path:
    override = os.environ.get(ENV_OVERRIDE_LOCALAPPDATA)
    if override:
        return Path(override)
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))


def riot_client_data_root() -> Path:
    r"""%LOCALAPPDATA%\Riot Games\Riot Client"""
    return local_appdata() / "Riot Games" / "Riot Client"


def valorant_log_dir() -> Path:
    return local_appdata() / "VALORANT" / "Saved" / "Logs"


def session_files() -> list[Path]:
    """現在存在するセッション関連ファイルの実パス一覧。"""
    root = riot_client_data_root()
    return [root / rel for rel in SESSION_FILE_CANDIDATES if safe_is_file(root / rel)]


def lockfile_path() -> Path | None:
    root = riot_client_data_root()
    for rel in LOCKFILE_CANDIDATES:
        p = root / rel
        if safe_is_file(p):
            return p
    return None


def _registry_riot_client_exe() -> Path | None:
    keys = (
        (winreg.HKEY_CURRENT_USER, r"Software\Riot Games\RiotClientInstalls"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Riot Games\RiotClientInstalls"),
    )
    for hive, sub in keys:
        try:
            with winreg.OpenKey(hive, sub) as k:
                for name in ("rc_live", "rc_default", "rc_beta"):
                    try:
                        value, _ = winreg.QueryValueEx(k, name)
                    except FileNotFoundError:
                        continue
                    p = Path(value)
                    if safe_is_file(p):
                        return p
        except OSError:
            continue
    return None


def riot_client_exe() -> Path | None:
    """RiotClientServices.exe を探す。見つからなければ None。"""
    override = os.environ.get(ENV_OVERRIDE_CLIENT_EXE)
    if override:
        p = Path(override)
        return p if safe_is_file(p) else None

    # 1) Riot 公式の install マニフェスト
    manifest = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Riot Games" / "RiotClientInstalls.json"
    if safe_is_file(manifest):
        import json
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        for key in ("rc_live", "rc_default", "rc_beta"):
            value = data.get(key)
            if value and safe_is_file(Path(value)):
                return Path(value)

    # 2) レジストリ
    from_reg = _registry_riot_client_exe()
    if from_reg:
        return from_reg

    # 3) 既定のインストール先
    for base in (r"C:\Riot Games", os.environ.get("PROGRAMFILES", r"C:\Program Files")):
        p = Path(base) / "Riot Client" / "RiotClientServices.exe"
        if safe_is_file(p):
            return p
    return None


@dataclass(frozen=True)
class Environment:
    """今この PC で Riot 環境がどこまで揃っているかのスナップショット。"""

    client_exe: Path | None
    data_root: Path
    found_session_files: tuple[Path, ...]
    lockfile: Path | None

    @property
    def installed(self) -> bool:
        return self.client_exe is not None

    @property
    def client_running(self) -> bool:
        return self.lockfile is not None

    def describe(self) -> str:
        if not self.installed:
            return "Riot Client が見つかりません（未インストール）"
        if self.client_running:
            return "Riot Client 起動中"
        return "Riot Client インストール済み・未起動"


def detect() -> Environment:
    return Environment(
        client_exe=riot_client_exe(),
        data_root=riot_client_data_root(),
        found_session_files=tuple(session_files()),
        lockfile=lockfile_path(),
    )
