"""配布用の単一 exe をビルドする。

    python tools/build_exe.py

PySide6 は使っていないモジュールまで巻き込むと 300MB 級になるので、
このアプリが触らない Qt モジュールは明示的に外している。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "Kunai"
ICON = ROOT / "assets" / "icon.ico"

# 使っていない Qt モジュール。特に WebEngine は単体で 100MB を超える
EXCLUDES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets", "PySide6.QtQml",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtHelp", "PySide6.QtDesigner",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtSerialPort", "PySide6.QtSensors", "PySide6.QtSpatialAudio",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "PySide6.QtTextToSpeech", "PySide6.QtUiTools", "PySide6.QtXml",
    "tkinter", "matplotlib", "numpy", "PIL", "pytest",
]


def build() -> Path:
    try:
        __import__("PyInstaller")
    except ImportError:
        print("PyInstaller がありません。インストールします…")
        subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"], check=True)

    for d in ("build", "dist"):
        shutil.rmtree(ROOT / d, ignore_errors=True)

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",
        "--windowed",                 # コンソールを出さない。エラーは crash.log へ
        "--name", NAME,
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        "--collect-submodules", "vam",
    ]
    if ICON.is_file():
        args += ["--icon", str(ICON), "--add-data", f"{ICON};assets"]
    for module in EXCLUDES:
        args += ["--exclude-module", module]
    args.append(str(ROOT / "main.py"))

    print("ビルド中… 数分かかります")
    subprocess.run(args, check=True, cwd=ROOT)

    exe = ROOT / "dist" / f"{NAME}.exe"
    if not exe.is_file():
        raise SystemExit("exe が生成されませんでした")
    print(f"\n完成: {exe}  ({exe.stat().st_size / 1024 / 1024:.1f} MB)")
    return exe


if __name__ == "__main__":
    build()
