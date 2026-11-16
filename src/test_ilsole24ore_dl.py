"""Offline check of stitch() and crop(): python src/test_ilsole24ore_dl.py"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ilsole24ore_dl as m  # noqa: E402

BG = (250, 240, 220)
tiles = [Image.new("P", (100, 50)) for _ in range(m.TILES)]
for t in tiles:
    t.putpalette([*BG, 0, 0, 0])  # index 0 = background, 1 = ink
ImageDraw.Draw(tiles[0]).rectangle((40, 30, 99, 49), fill=1)  # ink across tiles 0, 1 and 2
ImageDraw.Draw(tiles[1]).rectangle((0, 30, 9, 49), fill=1)
ImageDraw.Draw(tiles[2]).rectangle((40, 0, 99, 9), fill=1)

page = m.stitch(tiles)
assert page.size == (200, 200), page.size
assert page.getpixel((45, 35)) == (0, 0, 0) and page.getpixel((105, 35)) == (0, 0, 0)
assert page.getpixel((45, 55)) == (0, 0, 0) and page.getpixel((0, 0)) == BG

box = (40, 30, 110, 60)  # ink bounding box, right/bottom exclusive
cropped = m.crop(page)
assert cropped.size == (box[2] - box[0] + 2 * m.MARGIN, box[3] - box[1] + 2 * m.MARGIN), cropped.size
assert m.crop(Image.new("RGB", (10, 10), BG)).size == (10, 10)  # blank page: unchanged
print("ok")
