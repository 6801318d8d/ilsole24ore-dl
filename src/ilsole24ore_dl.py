"""Download an issue of Il Sole 24 Ore as a single PDF.

    python src/ilsole24ore_dl.py              latest issue
    python src/ilsole24ore_dl.py 2026-11-16   issue of that date
    python src/ilsole24ore_dl.py --keep-days 7   first delete the issues downloaded over 7 days ago
    python src/ilsole24ore_dl.py --outdir DIR    save the issues in DIR (default: data/final)
    python src/ilsole24ore_dl.py --mode pages    how to get the issue: full (default), pages or tiles

The viewer's issue index (get_timone.php) lists the pages. The issue is saved as
data/final/IlSole24Ore_<date>.pdf in one of three modes; a mode that fails falls back to the next:
- full: the publisher's PDF of the whole edition (vector text), after the institutional
  Shibboleth login set in .env (SOLE_LOGIN_URL, SOLE_IDP, SOLE_USERNAME, SOLE_PASSWORD);
- pages: the PDF of each page (one 200 dpi JPEG, no text layer), no login;
- tiles: 8 GIF tiles per page (2 columns x 4 rows), stitched at full resolution, cropped to
  the printed area, JPEG-encoded and merged without re-encoding; no login.
Pages and tiles are downloaded at once and cached in data/download/, so an interrupted run resumes.
"""

import argparse
import datetime as dt
import io
import os
import shutil
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlparse

import img2pdf
import requests
from PIL import Image, ImageChops
from pypdf import PdfWriter
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from urllib3.util.retry import Retry

INDEX_URL = "https://www.quotidiano.ilsole24ore.com/sfoglio/_resources/php/get_timone.php"
PDF_URL = "https://www.quotidiano.ilsole24ore.com/sfoglio/_resources/php/get_pdf.php"  # the viewer's "save the whole issue"
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
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def load_env(path=ROOT / ".env"):
    """KEY=value lines of .env into os.environ; a variable already set wins."""
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.lstrip().startswith("#"):
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))


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


class FirstForm(HTMLParser):
    """The action and the (name, value) fields of a page's first form; checkboxes, radios and buttons left out."""

    def __init__(self, html):
        super().__init__()
        self.action, self.fields, self.done = None, [], False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and self.action is None:
            self.action = a.get("action") or ""
        elif tag == "input" and self.action is not None and not self.done and a.get("name") \
                and (a.get("type") or "text").lower() in ("hidden", "text", "password", "email"):
            self.fields.append((a["name"], a.get("value") or ""))

    def handle_endtag(self, tag):
        self.done = self.done or (tag == "form" and self.action is not None)


def sso_login(s):
    """Log session S in through the institutional Shibboleth login set in .env."""
    cfg = {k: os.environ.get(f"SOLE_{k}") for k in ("LOGIN_URL", "IDP", "USERNAME", "PASSWORD")}
    if missing := [f"SOLE_{k}" for k, v in cfg.items() if not v]:
        raise RuntimeError(f"missing in .env: {', '.join(missing)}")
    r = s.get(cfg["LOGIN_URL"], timeout=(10, 30))  # ends on the federation's page that picks the institution
    if not (ret := parse_qs(urlparse(r.url).query).get("return")):
        raise RuntimeError(f"no institution picker after SOLE_LOGIN_URL: {r.url}")
    r = s.get(f"{ret[0]}&entityID={quote(cfg['IDP'], safe='')}", timeout=(10, 30))
    for _ in range(10):  # the identity provider's pages, then the forms that post the login back to the publisher
        form = FirstForm(r.text)
        if form.action is None:
            break
        names = {n for n, _ in form.fields}
        fields = form.fields
        if "j_username" in names:
            fields = [(n, v) for n, v in fields if n not in ("j_username", "j_password")]
            fields += [("j_username", cfg["USERNAME"]), ("j_password", cfg["PASSWORD"]), ("_eventId_proceed", "")]
        elif "shib_idp_ls_supported" in names:  # the page that tests the browser's local storage
            fields = [(n, "false" if n == "shib_idp_ls_supported" else v) for n, v in fields]
        elif "_shib_idp_consentIds" in names:  # release of the attributes to the publisher
            fields += [("_shib_idp_consentOptions", "_shib_idp_rememberConsent"), ("_eventId_proceed", "Accept")]
        r = s.post(urljoin(r.url, form.action), data=fields, timeout=(10, 60))
        if "j_username" in names and 'name="j_password"' in r.text:
            raise RuntimeError("login refused: check SOLE_USERNAME and SOLE_PASSWORD in .env")
    if "SoleAuth" not in s.cookies:
        raise RuntimeError(f"no session after the login (last page: {r.url})")


def full_issue(s, index, folder):
    """The issue as PDF bytes: the publisher's PDF of the whole edition, after the SSO login."""
    sso_login(s)
    code, issue = index["edition"]["code"], index["issue"]["code"]
    data = {"pdf_files": f"{index['newspaper']['code']}-{issue}-{code}-whole.pdf", "edition_id": index["edition"]["id"],
            "newspaper": index["newspaper"]["code"], "edition": code, "issue": issue, "version": index["version"]}
    with s.post(PDF_URL, data=data, timeout=(10, 120), stream=True) as r:
        r.raise_for_status()
        buf = io.BytesIO()
        with tqdm(total=int(r.headers.get("content-length", 0)) or None, unit="B", unit_scale=True) as bar:
            for chunk in r.iter_content(1 << 20):
                buf.write(chunk)
                bar.update(len(chunk))
    body = buf.getvalue()
    if not body.startswith(b"PK"):  # the server answers e.g. DOWNLOAD_KO_USER_INVALID
        raise RuntimeError(f"no ZIP from the server: {body[:60]!r}")
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        pdfs = [n for n in z.namelist() if n.lower().endswith(".pdf")]
        if len(pdfs) != 1:
            raise RuntimeError(f"expected one PDF in the ZIP, found {z.namelist()}")
        return z.read(pdfs[0])


def pdf_issue(s, index, folder):
    """The issue as PDF bytes, merged from the PDF of each page (cached in FOLDER)."""
    pages = index["pages"]
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


def tile_issue(s, index, folder):
    """The issue as PDF bytes, built from the image tiles of each page (cached in FOLDER)."""
    pages = index["pages"]
    paths =[[folder / f"{p['secret']}_{i}.gif" for i in range(TILES)] for p in pages]
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


MODES = {  # in fallback order
    "full": (full_issue, "logging in and downloading the PDF of the whole edition"),
    "pages": (pdf_issue, "downloading the PDF of each page"),
    "tiles": (tile_issue, "downloading the page images"),
}


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
    ap.add_argument("--mode", choices=MODES, default="full",
                    help="full (default): the publisher's PDF of the whole edition, with text, after the SSO login "
                         "set in .env; pages: the PDF of each page (images), no login; tiles: the page images in "
                         "tiles, no login. A mode that fails falls back to the next ones")
    args = ap.parse_args()
    load_env()
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
    index["pages"] = sorted(index["pages"], key=lambda p: p["page"])
    print(f"{index['edition']['description']}, {index['issue']['description']}: {len(index['pages'])} pages", flush=True)

    tiledir = DATA / "download" / f"{day:%Y%m%d}_{index['edition']['code']}"
    tiledir.mkdir(parents=True, exist_ok=True)
    modes = list(MODES)[list(MODES).index(args.mode):]
    for i, mode in enumerate(modes):
        print(f"Mode {mode}: {MODES[mode][1]}...", flush=True)
        try:
            pdf = MODES[mode][0](s, index, tiledir)
            break
        except Exception as e:  # any failure (not Ctrl+C) falls back to the next mode; the last one raises
            if mode == modes[-1]:
                raise
            print(f"Mode {mode} failed ({type(e).__name__}: {e}); trying mode {modes[i + 1]}", flush=True)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".part").write_bytes(pdf)
    out.with_suffix(".part").replace(out)
    shutil.rmtree(tiledir, ignore_errors=True)  # a file held open (antivirus) must not fail a saved issue
    print(f"Saved {out} ({out.stat().st_size / 1e6:,.0f} MB)")


if __name__ == "__main__":
    main()
