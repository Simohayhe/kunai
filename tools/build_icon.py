"""アプリアイコンを生成する。

    python tools/build_icon.py

オリジナルのクナイ (くない) シルエットを描く。Riot の公式アートは
一切使わない。配色はアプリのテーマ (vam/ui/theme.py) の TEAL/BG に揃えた、
ジェットを連想させるシアン系。

assets/icon.ico (Windows 用マルチサイズ) と assets/icon.png (参考用) を書き出す。
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

BG = (15, 25, 35, 255)        # theme.BG 相当
BG_EDGE = (25, 38, 52, 255)
BLADE = (15, 216, 194, 255)   # theme.TEAL
BLADE_LIGHT = (170, 245, 235, 255)
BLADE_DARK = (9, 140, 126, 255)
HANDLE = (40, 52, 64, 255)
WIND = (15, 216, 194, 140)

CANVAS = 1024


def _kunai_points(cx: float, cy: float, scale: float) -> list[tuple[float, float]]:
    """クナイの刃 (先端を上に向けた状態) の輪郭。原点中心、後で回転する。

    小さいサイズ (16px タスクバー等) でも潰れないよう、太めのシルエットにする。
    """
    pts = [
        (0, -430), (78, -220), (46, -170), (46, 10),
        (-46, 10), (-46, -170), (-78, -220),
    ]
    return [(cx + x * scale, cy + y * scale) for x, y in pts]


def _rotate(points, cx, cy, degrees):
    rad = math.radians(degrees)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    out = []
    for x, y in points:
        dx, dy = x - cx, y - cy
        out.append((cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a))
    return out


def draw_icon() -> Image.Image:
    img = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 背景: 角丸正方形。16px でも視認できるよう、飾りは付けずベタ塗りにする
    margin = 32
    radius = 200
    draw.rounded_rectangle(
        [margin, margin, CANVAS - margin, CANVAS - margin],
        radius=radius, fill=BG,
    )

    cx, cy = CANVAS / 2, CANVAS / 2
    angle = -38  # 右上に切っ先が向くように回転
    scale = 0.86  # 小さいサイズでも潰れないよう、枠いっぱいに大きく描く

    # 刃本体 (太めのシルエット。細部よりコントラストと大きさを優先)
    blade = _rotate(_kunai_points(cx, cy, scale), cx, cy, angle)
    draw.polygon(blade, fill=BLADE)

    # 刃の中心ハイライト (縮小しても残る、太い 1 本の筋)
    highlight = _rotate(
        [
            (cx, cy - 430 * scale + 20), (cx + 20, cy - 190 * scale),
            (cx + 20, cy + 10 * scale), (cx - 20, cy + 10 * scale),
            (cx - 20, cy - 190 * scale),
        ],
        cx, cy, angle,
    )
    draw.polygon(highlight, fill=BLADE_LIGHT)

    # 柄 (ハンドル): 刃と同じ太さで一体感を出す、装飾は最小限
    handle_pts = _rotate(
        [
            (cx - 46 * scale, cy + 10 * scale), (cx + 46 * scale, cy + 10 * scale),
            (cx + 52 * scale, cy + 300 * scale), (cx - 52 * scale, cy + 300 * scale),
        ],
        cx, cy, angle,
    )
    draw.polygon(handle_pts, fill=HANDLE)
    # 柄の区切り線 (1 本だけ、太く)
    p1 = _rotate([(cx - 52 * scale, cy + 150 * scale)], cx, cy, angle)[0]
    p2 = _rotate([(cx + 52 * scale, cy + 150 * scale)], cx, cy, angle)[0]
    draw.line([p1, p2], fill=BLADE_DARK, width=int(14 * scale))

    return img


def build() -> Path:
    ASSETS.mkdir(parents=True, exist_ok=True)
    img = draw_icon()

    png_path = ASSETS / "icon.png"
    img.save(png_path)

    ico_path = ASSETS / "icon.ico"
    sizes = [16, 24, 32, 48, 64, 128, 256]
    img.save(ico_path, sizes=[(s, s) for s in sizes])

    print(f"完成: {ico_path}")
    print(f"参考用: {png_path}")
    return ico_path


if __name__ == "__main__":
    build()
