"""Download an issue of Il Sole 24 Ore as a single PDF.

    python src/ilsole24ore_dl.py              latest issue
    python src/ilsole24ore_dl.py 2026-11-16   issue of that date
    python src/ilsole24ore_dl.py --keep-days 7   first delete the issues downloaded over 7 days ago
    python src/ilsole24ore_dl.py --outdir DIR    save the issues in DIR (default: data/final)

The viewer's issue index (get_timone.php) lists the pages. Each page is served as a
PDF (one 200 dpi JPEG, no text layer): all of them are downloaded at once (cached in
data/download/, so an interrupted run resumes) and merged into data/final/IlSole24Ore_<date>.pdf.
If a page PDF is missing or fails, the issue is built from the page images instead:
8 GIF tiles per page (2 columns x 4 rows), stitched at full resolution, cropped to the
printed area, JPEG-encoded and merged without re-encoding.
"""

import argparse
import datetime as dt
import io
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import img2pdf
import requests
from PIL import Image, ImageChops
from pypdf import PdfWriter
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from urllib3.util.retry import Retry

INDEX_URL = "https://www.quotidiano.ilsole24ore.com/sfoglio/_resources/php/get_timone.php"
CDN = "https://mobapp2.ilsole24ore.com/_deploy/"
TILES = 8  # per page, 2 columns x 4 rows
LOOKBACK_DAYS = 7  # latest issue: from tomorrow (published the evening before) back this many days
DOWNLOAD_THREADS = 32  # 192 tiles: 1.0 s with 8 threads, 0.6-0.8 s with 32, no gain beyond
PAGE_THREADS = 8  # stitch/crop/encode; threads beat processes: spawning 12 processes on Windows takes 1.3 s
DPI = 200  # stitched pages are ~2,750 px wide for a 35 cm page
MARGIN = 20  # px kept around the printed area
CROP_SCALE = 4  # the blank border is measured on the page reduced 4 times: 4 px accuracy, 16x fewer pixels
JPEG_QUALITY = 85  # same look as 92 at 200 % zoom (PSNR 40 dB), 49 MB instead of 63 MB per issue
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
DATA = Path(__file__).resolve().parent.parent / "data"


def session():
    s = requests.Session()
    s.headers["User-Agent"] = UA
    retry = Retry(total=4, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))
    s.mount("https://", HTTPAdapter(pool_maxsize=DOWNLOAD_THREADS, max_retries=retry))
    return s


def issue_index(s, day, edition):
    """The index of an issue, or None when that edition of that day does not exist."""
    params = {"newspaper": "S24", "issue": f"{day:%Y%m%d}", "edition": edition, "issue_date": ""}
    r = s.get(INDEX_URL, params=params, timeout=(10, 30))  # (connect, read): an unreachable host fails fast
    r.raise_for_status()
    j = r.json()
    return j["result"]["data"] if j["status"] == "OK" else None


def find_issue(s, day=None):
    """(date, index) of the issue of DAY or, without DAY, of the latest issue.
    Sequential on one kept-alive connection: 18 parallel requests took 1.1 s, 4 in a row 0.3 s."""
    days = [day] if day else [dt.date.today() + dt.timedelta(days=1 - k) for k in range(LOOKBACK_DAYS + 2)]
    for d in days:
        for edition in ("LUNEDI", "SOLE") if d.weekday() == 0 else ("SOLE", "LUNEDI"):  # likely one first
            if index := issue_index(s, d, edition):
                return d, index
    sys.exit(f"No issue found for {days[-1]}" + (f" to {days[0]}" if len(days) > 1 else ""))


def fetch(s, url, path, magic=b"GIF"):
    """Download a tile (or, with magic=b"%PDF", a page PDF), unless it is already cached."""
    if path.is_file():
        return
    r = s.get(url, timeout=(10, 60))
    r.raise_for_status()
    if not r.content.startswith(magic):
        raise RuntimeError(f"not a {magic.decode().strip('%')}: {url}")
    part = path.with_suffix(".part")
    part.write_bytes(r.content)
    part.replace(path)


def stitch(tiles):
    """Tiles in reading order, two per row, into one RGB page."""
    rows = list(zip(tiles[::2], tiles[1::2], strict=True))
    page = Image.new("RGB", (rows[0][0].width + rows[0][1].width, sum(left.height for left, _ in rows)))
    y = 0
    for left, right in rows:
        page.paste(left, (0, y))
        page.paste(right, (left.width, y))
        y += left.height
    return page


def crop(page):
    """Cut the blank border (the colour of the top-left pixel), keeping MARGIN px or slightly more."""
    small = page.reduce(CROP_SCALE)
    bg = Image.new(small.mode, small.size, page.getpixel((0, 0)))
    box = ImageChops.difference(small, bg).convert("L").point(lambda v: 255 if v > 32 else 0).getbbox()
    if not box:
        return page
    left, top, right, bottom = (v * CROP_SCALE for v in box)
    return page.crop((max(left - MARGIN, 0), max(top - MARGIN, 0),
                      min(right + MARGIN, page.width), min(bottom + MARGIN, page.height)))


def page_jpeg(tile_paths):
    """A page from its cached tiles, as JPEG bytes."""
    tiles = [Image.open(io.BytesIO(p.read_bytes())) for p in tile_paths]  # no open handle blocks the cleanup
    buf = io.BytesIO()
    crop(stitch(tiles)).save(buf, "JPEG", quality=JPEG_QUALITY, dpi=(DPI, DPI))
    return buf.getvalue()


def pdf_issue(s, pages, folder):
    """The issue as PDF bytes, merged from the PDF of each page (cached in FOLDER)."""
    paths = [folder / f"{p['secret']}.pdf" for p in pages]
    urls = [CDN + p["pdf"].strip() for p in pages]  # KeyError on an index without page PDFs: the caller falls back
    with ThreadPoolExecutor(DOWNLOAD_THREADS) as dl:
        jobs = [dl.submit(fetch, s, url, path, b"%PDF") for url, path in zip(urls, paths, strict=True)]
        try:
            for f in tqdm(as_completed(jobs), total=len(jobs), unit="page"):
                f.result()  # re-raises a failed download
        except BaseException:  # a failed page or Ctrl+C: drop the queued downloads instead of waiting for them
            dl.shutdown(cancel_futures=True)
            raise
    w = PdfWriter()
    for path in paths:
        w.append(io.BytesIO(path.read_bytes()))  # no open handle blocks the cleanup
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def tile_issue(s, pages, folder):
    """The issue as PDF bytes, built from the image tiles of each page (cached in FOLDER)."""
    paths = [[folder / f"{p['secret']}_{i}.gif" for i in range(TILES)] for p in pages]
    # All tiles are queued at once, in page order; each page is built as soon as its tiles are in
    with ThreadPoolExecutor(DOWNLOAD_THREADS) as dl, ThreadPoolExecutor(PAGE_THREADS) as cpu:
        fetched = [[dl.submit(fetch, s, f"{CDN}{p['splitted']}/{i}.gif", path) for i, path in enumerate(pp)]
                   for p, pp in zip(pages, paths, strict=True)]

        def build(n):
            for f in fetched[n]:
                f.result()  # re-raises a failed download
            return page_jpeg(paths[n])

        try:
            jpegs = list(tqdm(cpu.map(build, range(len(pages))), total=len(pages), unit="page"))
        except BaseException:  # a failed tile or Ctrl+C: drop the queued downloads instead of waiting for them
            dl.shutdown(cancel_futures=True)
            raise
    return img2pdf.convert(jpegs)


def prune(keep_days, outdir):
    """Delete the issue PDFs in OUTDIR, and the tile folders of failed runs, last modified over KEEP_DAYS days ago."""
    oldest = dt.date.today() - dt.timedelta(days=keep_days)
    for p in [*outdir.glob("IlSole24Ore_*.pdf"), *(DATA / "download").glob("*_*")]:
        if dt.date.fromtimestamp(p.stat().st_mtime) < oldest:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink()
            print(f"Deleted {p}")


def main():
    ap = argparse.ArgumentParser(description="Download an issue of Il Sole 24 Ore as a PDF.")
    ap.add_argument("date", nargs="?", type=dt.date.fromisoformat, help="YYYY-MM-DD (default: the latest issue)")
    ap.add_argument("--keep-days", type=int, metavar="N",
                    help="first delete the issues (and leftover tiles) last modified over N days ago")
    ap.add_argument("--outdir", type=Path, default=DATA / "final", metavar="DIR",
                    help="folder of the issue PDFs (default: data/final)")
    args = ap.parse_args()
    if args.keep_days is not None:
        prune(args.keep_days, args.outdir)
    day = args.date

    s = session()
    print("Looking for the issue...", flush=True)
    day, index = find_issue(s, day)
    out = args.outdir / f"IlSole24Ore_{day:%Y-%m-%d}.pdf"
    if out.is_file():
        print(f"Already downloaded: {out}")
        return
    pages = sorted(index["pages"], key=lambda p: p["page"])
    print(f"{index['edition']['description']}, {index['issue']['description']}: {len(pages)} pages", flush=True)

    tiledir = DATA / "download" / f"{day:%Y%m%d}_{index['edition']['code']}"
    tiledir.mkdir(parents=True, exist_ok=True)
    print("Downloading the page PDFs...", flush=True)
    try:
        pdf = pdf_issue(s, pages, tiledir)
    except Exception as e:  # any failure of the PDF route (not Ctrl+C): build the issue from the page images
        print(f"Page PDFs not available ({type(e).__name__}: {e}); downloading the page images instead", flush=True)
        pdf = tile_issue(s, pages, tiledir)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".part").write_bytes(pdf)
    out.with_suffix(".part").replace(out)
    shutil.rmtree(tiledir, ignore_errors=True)  # a file held open (antivirus) must not fail a saved issue
    print(f"Saved {out} ({out.stat().st_size / 1e6:,.0f} MB)")


if __name__ == "__main__":
    main()
