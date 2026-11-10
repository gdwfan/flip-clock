"""生成应用图标（多尺寸 ICO）。无 ImageMagick，用 Pillow 直接画。

跑法：py -3.10 -X utf8 tools/gen_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "icon.ico"
OUT.parent.mkdir(parents=True, exist_ok=True)

S = 256
BG = (16, 16, 22, 255)
CARD_TOP = (44, 44, 51, 255)
CARD_BOT = (27, 27, 32, 255)
DIGIT = (246, 246, 248, 255)

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 底板：深色圆角方
d.rounded_rectangle((6, 6, S - 6, S - 6), radius=54, fill=BG)

# 两张翻页卡
CARD_W, CARD_H = 56, 116
GAP = 18
x0 = (S - (CARD_W * 2 + GAP)) // 2
y0 = (S - CARD_H) // 2
for i in range(2):
    x = x0 + i * (CARD_W + GAP)
    mid = y0 + CARD_H // 2
    d.rounded_rectangle((x, y0, x + CARD_W, mid + 8), radius=11, fill=CARD_TOP)
    d.rounded_rectangle((x, mid - 8, x + CARD_W, y0 + CARD_H), radius=11, fill=CARD_BOT)
    d.line((x + 2, mid, x + CARD_W - 2, mid), fill=(0, 0, 0, 190), width=3)

# 卡面数字
try:
    font = ImageFont.truetype("C:/Windows/Fonts/bahnschrift.ttf", 84)
except OSError:
    font = ImageFont.load_default()
for i, ch in enumerate("12"):
    cx = x0 + i * (CARD_W + GAP) + CARD_W // 2
    d.text((cx, y0 + CARD_H // 2 + 2), ch, font=font, fill=DIGIT, anchor="mm")

img.save(OUT, format="ICO",
         sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.save(ROOT / "assets" / "icon.png")
print(f"icon written: {OUT} ({OUT.stat().st_size} bytes)")
