"""配布用のインストーラー (setup.exe) をビルドする。

    python tools/build_installer.py

先に単体 exe (dist/ValorantAccountManager.exe) を build_exe.py でビルドし、
それを installer/setup.iss (Inno Setup) でラップする。Inno Setup 本体が
要る (winget install JRSoftware.InnoSetup で入る)。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ISS_FILE = ROOT / "installer" / "setup.iss"

# よくある置き場所を順に探す。PATH に無いことが多いため。
ISCC_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    Path.home() / "AppData" / "Local" / "Programs" / "Inno Setup 6" / "ISCC.exe",
]


def find_iscc() -> Path:
    which = shutil.which("iscc") or shutil.which("ISCC")
    if which:
        return Path(which)
    for candidate in ISCC_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise SystemExit(
        "Inno Setup (ISCC.exe) が見つかりません。"
        "winget install --id JRSoftware.InnoSetup -e でインストールしてください。"
    )


def build() -> Path:
    from vam.version import __version__
    from build_exe import build as build_exe

    exe = ROOT / "dist" / "ValorantAccountManager.exe"
    if not exe.is_file():
        print("先に単体 exe をビルドします…")
        build_exe()

    iscc = find_iscc()
    print(f"インストーラーをビルド中… (Inno Setup: {iscc})")
    subprocess.run(
        [str(iscc), f"/DMyAppVersion={__version__}", str(ISS_FILE)],
        check=True, cwd=ROOT,
    )

    installer = ROOT / "dist" / "ValorantAccountManagerSetup.exe"
    if not installer.is_file():
        raise SystemExit("インストーラーが生成されませんでした")
    print(f"\n完成: {installer}  ({installer.stat().st_size / 1024 / 1024:.1f} MB)")
    return installer


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(ROOT))
    build()
