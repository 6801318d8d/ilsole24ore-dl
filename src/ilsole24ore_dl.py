"""Download an issue of Il Sole 24 Ore as a single PDF.

    python src/ilsole24ore_dl.py              latest issue
    python src/ilsole24ore_dl.py 2026-11-16   issue of that date
    python src/ilsole24ore_dl.py --keep-days 7   first delete the issues downloaded over 7 days ago
    python src/ilsole24ore_dl.py --outdir DIR    save the issues in DIR (default: data/final)
    python src/ilsole24ore_dl.py --mode pages    how to get the issue: full (default), pages or tiles
    python src/ilsole24ore_dl.py --edition PLUS  a supplement (--list-editions shows the codes; 'all': every edition)

Next to each PDF the articles are saved too: the viewer's database (.db, SQLite with the full
text of every article and a search index) and the articles as Markdown (.md).

The viewer's issue index (get_timone.php) lists the pages. The issue is saved as
data/final/IlSole24Ore_<date>.pdf in one of three modes; a mode that fails falls back to the next:
- full: the publisher's PDF of the whole edition (vector text), after the institutional
  Shibboleth login set in .env (SOLE_LOGIN_URL, SOLE_IDP, SOLE_USERNAME, SOLE_PASSWORD), without
  the watermark, the download code and the metadata that identify the subscriber (scrub());
- pages: the PDF of each page (one 200 dpi JPEG, no text layer), no login;
- tiles: 8 GIF tiles per page (2 columns x 4 rows), stitched at full resolution, cropped to
  the printed area, JPEG-encoded and merged without re-encoding; no login.
Pages and tiles are downloaded at once and cached in data/download/, so an interrupted run resumes.
"""

import argparse
import datetime as dt
import html
import io
import os
import re
import shutil
import sqlite3
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlparse

import img2pdf
import requests
from PIL import Image, ImageChops
from pypdf import PdfReader, PdfWriter
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from urllib3.util.retry import Retry

INDEX_URL = "https://www.quotidiano.ilsole24ore.com/sfoglio/_resources/php/get_timone.php"
PDF_URL = "https://www.quotidiano.ilsole24ore.com/sfoglio/_resources/php/get_pdf.php"  # the viewer's "save the whole issue"
NEWSSTAND_URL = "https://www.quotidiano.ilsole24ore.com/"  # lists the newspaper's editions and supplements
DAILY = ("SOLE", "LUNEDI")  # the daily newspaper, saved as IlSole24Ore_<date>.pdf; other editions get _<EDITION>
WATERMARK = b"@ Gruppo Il Sole 24 ORE".hex().upper().encode()  # its text as pdf-lib writes it: a hex string
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
    if j["result"].get("code") == "PDF_AVAILABLE":  # 2012-2013: an archive PDF that the site no longer serves (404)
        print(f"{day} {edition}: listed only as an archive PDF, which the site no longer serves")
    return j["result"]["data"] if j["status"] == "OK" and "pages" in (j["result"]["data"] or {}) else None


def find_issue(s, day=None, editions=None):
    """(date, index) of the issue of DAY or, without DAY, of the latest issue, in the first of EDITIONS that has
    one (default: the daily newspaper, whose Monday edition is LUNEDI).
    Sequential on one kept-alive connection: 18 parallel requests took 1.1 s, 4 in a row 0.3 s."""
    days = [day] if day else [dt.date.today() + dt.timedelta(days=1 - k) for k in range(LOOKBACK_DAYS + 2)]
    for d in days:
        for edition in editions or (("LUNEDI", "SOLE") if d.weekday() == 0 else ("SOLE", "LUNEDI")):  # likely one first
            if index := issue_index(s, d, edition):
                return d, index
    sys.exit(f"No issue found for {days[-1]}" + (f" to {days[0]}" if len(days) > 1 else "")
             + (f" ({', '.join(editions)})" if editions else ""))


def newsstand(s):
    """{edition: [issue dates]} that the newsstand page lists (supplements included, about the last 40 days)."""
    html = s.get(NEWSSTAND_URL, timeout=(10, 30)).text
    found = {}
    for tag in re.findall(r"<[^>]*data-edizione=[^>]*>", html):
        edition, issue = re.search(r'data-edizione="([^"]+)"', tag), re.search(r'data-issue="(\d{8})"', tag)
        if edition and issue:
            found.setdefault(edition[1], set()).add(dt.datetime.strptime(issue[1], "%Y%m%d").date())
    return {e: sorted(d) for e, d in found.items()}


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
    """Log session S in through the institutional Shibboleth login set in .env (once per session)."""
    if "SoleAuth" in s.cookies:
        return
    cfg ={k: os.environ.get(f"SOLE_{k}") for k in ("LOGIN_URL", "IDP", "USERNAME", "PASSWORD")}
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
    if body.startswith(b"%PDF"):  # an 844-byte one-page placeholder (EBOOK; issues of 2022 and earlier)
        raise RuntimeError("no PDF of the whole edition on the server (it sent a one-page placeholder)")
    if not body.startswith(b"PK"):  # the server answers e.g. DOWNLOAD_KO_USER_INVALID
        raise RuntimeError(f"no ZIP from the server: {body[:60]!r}")
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        pdfs = [n for n in z.namelist() if n.lower().endswith(".pdf")]
        if len(pdfs) != 1:
            raise RuntimeError(f"expected one PDF in the ZIP, found {z.namelist()}")
        return scrub(z.read(pdfs[0]))


def scrub(pdf):
    """The publisher's PDF without what identifies the subscriber: the watermark the server adds to each page with
    pdf-lib (a line with the institutional account and the download code, then the whole code in black under a
    paper-coloured box), the code in the Subject and in the Exif of a few pictures, and the download time."""
    r = PdfReader(io.BytesIO(pdf))
    code = str((r.metadata or {}).get("/Subject", "")).encode()  # 64 hex digits, the same for each account and issue
    w = PdfWriter(clone_from=r)
    w.metadata = None  # Subject, creation and modification time, producer
    stamped = 0
    for page in (p for p in w.pages if "/Contents" in p):  # a blank page may have none
        parts = page["/Contents"].get_object()
        parts = [p.get_object() for p in parts] if isinstance(parts, list) else [parts]
        for part in parts:
            if WATERMARK in part.get_data():
                if len(parts) == 1:  # pdf-lib appends its own stream: a single one is the page itself
                    raise RuntimeError("watermark inside the page's own content: scrub() would blank the page")
                part.set_data(b"")
                stamped += 1
    if code and not stamped:
        raise RuntimeError("the PDF carries a download code but no known watermark: its format changed")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue().replace(code, b" " * len(code)) if code else buf.getvalue()  # same length: JPEGs stay valid


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


def table_md(table):
    """An HTML table as a Markdown table (cell text only; the first row is the header)."""
    rows = [[re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", cell))).strip().replace("|", "\\|")
             for cell in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", tr, flags=re.S | re.I)]
            for tr in re.findall(r"<tr\b.*?</tr>", table, flags=re.S | re.I)]
    rows = [r for r in rows if any(r)]
    if not rows:
        return ""
    width = max(map(len, rows))
    lines = ["| " + " | ".join(r + [""] * (width - len(r))) + " |" for r in rows]
    return "\n".join([lines[0], "|" + " --- |" * width, *lines[1:]])


def html_to_md(text):
    """The publisher's article HTML as Markdown: paragraphs, line breaks, bold, italics and tables; other tags dropped."""
    text = re.sub(r"<table\b.*?</table>", lambda m: "\n\n" + table_md(m[0]) + "\n\n", text or "", flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.I)
    text = re.sub(r"</?b>", "**", text, flags=re.I)
    text = re.sub(r"</?i>", "*", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))  # <p>, <firma>, <citta>, <fonte> ...
    text = re.sub(r" *\n *", "\n", re.sub(r"[ \t]+", " ", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def articles_markdown(db, title):
    """The articles of the viewer's issue database DB as one Markdown document, in page order, under their sections."""
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT s.name, f.page, f.headline, f.subhead, f.subheading, f.abstract, f.text, f.signature, f.caption"
            " FROM article a JOIN article_fts f ON f.article_id = a.id"
            " LEFT JOIN section s ON s.id = a.section_id"
            " LEFT JOIN article2firstpage p ON p.article_id = a.id"
            " ORDER BY COALESCE(p.first_page, f.page), a.priority").fetchall()
    finally:
        con.close()  # an open handle would block the file on Windows
    out, section = [f"# {title}"], None
    for name, page, headline, subhead, subheading, abstract, text, signature, caption in rows:
        if name != section:
            section = name
            out.append(f"## {name or 'Senza sezione'}")
        out.append("### " + (html_to_md(headline).replace("\n", " ") or "(senza titolo)"))
        out += [f"*{x}*" for x in map(html_to_md, (subhead, subheading)) if x]
        if x := html_to_md(abstract):
            out.append(f"**{x}**")
        if x := html_to_md(text):
            out.append(x)
        if x := html_to_md(caption):
            out.append(f"*{x}*")
        out.append(" · ".join(filter(None, (html_to_md(signature), f"pagina {page}"))))
    return "\n\n".join(out) + "\n"


def save_articles(s, index, out):
    """Save the articles of the issue next to OUT: the viewer's database (OUT.db: SQLite, full text of every article
    with a search index) and the articles as Markdown (OUT.md). No login; a failure only warns, the PDF stays."""
    try:
        r = s.get(CDN + index["sqLite"], timeout=(10, 120))
        r.raise_for_status()
        if not r.content.startswith(b"SQLite format 3"):
            raise RuntimeError("not an SQLite database")
        db, md = out.with_suffix(".db"), out.with_suffix(".md")
        db.write_bytes(r.content)
        md.write_text(articles_markdown(db, f"{index['edition']['description']}, {index['issue']['description']}"),
                      encoding="utf-8")
        print(f"Saved the articles: {db.name} and {md.name}")
    except Exception as e:  # the PDF is the main product: the articles must not fail the run
        print(f"Articles not saved ({type(e).__name__}: {e})")


MODES = {  # in fallback order
    "full": (full_issue, "logging in and downloading the PDF of the whole edition"),
    "pages": (pdf_issue, "downloading the PDF of each page"),
    "tiles": (tile_issue, "downloading the page images"),
}


def prune(keep_days, outdir):
    """Delete the issues in OUTDIR (PDF, articles database and Markdown), and the tile folders of failed runs,
    last modified over KEEP_DAYS days ago."""
    oldest = dt.date.today() - dt.timedelta(days=keep_days)
    issues = [p for p in outdir.glob("IlSole24Ore_*") if p.suffix in (".pdf", ".db", ".md")]
    for p in [*issues, *(DATA / "download").glob("*_*")]:
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
    ap.add_argument("--edition", metavar="CODE",
                    help="an edition or supplement instead of the daily newspaper (e.g. PLUS, DOMENICA, RAPPORTO), "
                         "or 'all' for every edition of that date; --list-editions shows the codes")
    ap.add_argument("--list-editions", action="store_true",
                    help="list the editions and supplements of the last weeks, then exit")
    args = ap.parse_args()
    load_env()
    if args.keep_days is not None:
        prune(args.keep_days, args.outdir)

    s = session()
    if args.list_editions:
        for edition, days in sorted(newsstand(s).items()):
            print(f"{edition:22} {len(days):3} issues, latest {days[-1]}")
        return
    print("Looking for the issue...", flush=True)
    if not args.edition:
        targets = [find_issue(s, args.date)]
    elif args.edition.lower() == "all":
        day = args.date or find_issue(s)[0]
        targets = [(day, index) for edition in sorted(newsstand(s)) if (index := issue_index(s, day, edition))]
        if not targets:
            sys.exit(f"No edition found for {day}")
    else:  # without a date: the latest issue the newsstand lists (supplements can be monthly), else the last days
        edition = args.edition.upper()
        listed = [] if args.date else newsstand(s).get(edition, [])
        targets = [find_issue(s, args.date or (listed[-1] if listed else None), (edition,))]
    for day, index in targets:
        download_issue(s, day, index, args)


def download_issue(s, day, index, args):
    """Save one issue as a PDF, in the first mode that works, and its articles."""
    edition = index["edition"]["code"]
    out = args.outdir / (f"IlSole24Ore_{day:%Y-%m-%d}.pdf" if edition in DAILY else f"IlSole24Ore_{day:%Y-%m-%d}_{edition}.pdf")
    if out.is_file():
        print(f"Already downloaded: {out}")
        if not out.with_suffix(".db").is_file():  # an issue saved before the articles were saved too
            save_articles(s, index, out)
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
    save_articles(s, index, out)


if __name__ == "__main__":
    main()
