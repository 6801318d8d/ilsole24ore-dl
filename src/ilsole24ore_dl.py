"""Download an issue of Il Sole 24 Ore as a single PDF.

    python src/ilsole24ore_dl.py              latest issue
    python src/ilsole24ore_dl.py 2026-11-16   issue of that date

The viewer's issue index (get_timone.php) lists the pages; each page is served as
8 GIF tiles (2 columns x 4 rows), which are stitched at full resolution, cropped
to the printed area and written to data/final/IlSole24Ore_<date>.pdf. Tiles are
cached in data/download/, so an interrupted run resumes where it stopped.
"""

import argparse
import datetime as dt
import io
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import img2pdf
import requests
from PIL import Image, ImageChops
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from urllib3.util.retry import Retry

INDEX_URL = "https://www.quotidiano.ilsole24ore.com/sfoglio/_resources/php/get_timone.php"
CDN = "https://mobapp2.ilsole24ore.com/_deploy/"
EDITIONS = ("SOLE", "LUNEDI")  # Monday's issue is the LUNEDI edition
TILES = 8  # per page, 2 columns x 4 rows
LOOKBACK_DAYS = 7  # latest issue: from tomorrow (published the evening before) back this many days
WORKERS = 8
DPI = 200  # stitched pages are ~2,750 px wide for a 35 cm page
MARGIN = 20  # px kept around the printed area
JPEG_QUALITY = 85
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
DATA = Path(__file__).resolve().parent.parent / "data"


def session():
    s = requests.Session()
    s.headers["User-Agent"] = UA
    retry = Retry(total=4, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))
    s.mount("https://", HTTPAdapter(pool_maxsize=WORKERS, max_retries=retry))
    return s


def issue_index(s, day, edition):
    """The index of an issue, or None when that edition of that day does not exist."""
    params = {"newspaper": "S24", "issue": f"{day:%Y%m%d}", "edition": edition, "issue_date": ""}
    r = s.get(INDEX_URL, params=params, timeout=30)
    r.raise_for_status()
    j = r.json()
    return j["result"]["data"] if j["status"] == "OK" else None


def find_issue(s, day=None):
    """(date, index) of the issue of DAY or, without DAY, of the latest issue."""
    days = [day] if day else [dt.date.today() + dt.timedelta(days=1 - k) for k in range(LOOKBACK_DAYS + 2)]
    for d in days:
        for edition in EDITIONS:
            if index := issue_index(s, d, edition):
                return d, index
    sys.exit(f"No issue found for {days[-1]}" + (f" to {days[0]}" if len(days) > 1 else ""))


def tile(s, url, path):
    if not path.is_file():
        r = s.get(url, timeout=60)
        r.raise_for_status()
        if not r.content.startswith(b"GIF"):
            raise RuntimeError(f"not a GIF: {url}")
        path.with_suffix(".part").write_bytes(r.content)
        path.with_suffix(".part").replace(path)
    return Image.open(io.BytesIO(path.read_bytes()))  # from memory: no open handle blocks the cleanup


def stitch(tiles):
    """Tiles in reading order, two per row, into one RGB page."""
    rows = list(zip(tiles[::2], tiles[1::2]))
    page = Image.new("RGB", (rows[0][0].width + rows[0][1].width, sum(left.height for left, _ in rows)))
    y = 0
    for left, right in rows:
        page.paste(left, (0, y))
        page.paste(right, (left.width, y))
        y += left.height
    return page


def crop(page):
    """Cut the blank border (the colour of the top-left pixel), keeping MARGIN px."""
    bg = Image.new(page.mode, page.size, page.getpixel((0, 0)))
    box = ImageChops.difference(page, bg).convert("L").point(lambda v: 255 if v > 32 else 0).getbbox()
    if not box:
        return page
    left, top, right, bottom = box
    return page.crop((max(left - MARGIN, 0), max(top - MARGIN, 0),
                      min(right + MARGIN, page.width), min(bottom + MARGIN, page.height)))


def page_jpeg(s, page, tiledir):
    tiles = [tile(s, f"{CDN}{page['splitted']}/{i}.gif", tiledir / f"{page['secret']}_{i}.gif")
             for i in range(TILES)]
    buf = io.BytesIO()
    crop(stitch(tiles)).save(buf, "JPEG", quality=JPEG_QUALITY, dpi=(DPI, DPI))
    return buf.getvalue()


def main():
    ap = argparse.ArgumentParser(description="Download an issue of Il Sole 24 Ore as a PDF.")
    ap.add_argument("date", nargs="?", type=dt.date.fromisoformat, help="YYYY-MM-DD (default: the latest issue)")
    day = ap.parse_args().date

    s = session()
    print("Looking for the issue...", flush=True)
    day, index = find_issue(s, day)
    out = DATA / "final" / f"IlSole24Ore_{day:%Y-%m-%d}.pdf"
    if out.is_file():
        print(f"Already downloaded: {out}")
        return
    pages = sorted(index["pages"], key=lambda p: p["page"])
    print(f"{index['edition']['description']}, {index['issue']['description']}: {len(pages)} pages", flush=True)

    tiledir = DATA / "download" / f"{day:%Y%m%d}_{index['edition']['code']}"
    tiledir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(WORKERS) as pool:
        jpegs = list(tqdm(pool.map(lambda p: page_jpeg(s, p, tiledir), pages), total=len(pages), unit="page"))

    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".part").write_bytes(img2pdf.convert(jpegs))
    out.with_suffix(".part").replace(out)
    shutil.rmtree(tiledir)
    print(f"Saved {out} ({out.stat().st_size / 1e6:,.0f} MB)")


if __name__ == "__main__":
    main()
