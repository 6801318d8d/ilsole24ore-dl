"""Offline check of stitch(), crop(), prune(), find_issue() and main(): python src/test_ilsole24ore_dl.py"""

import datetime as dt
import io
import os
import sys
import tempfile
import time
import types
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter

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
exact = (box[2] - box[0] + 2 * m.MARGIN, box[3] - box[1] + 2 * m.MARGIN)  # border measured to CROP_SCALE px
assert all(0 <= c - e < 2 * m.CROP_SCALE for c, e in zip(cropped.size, exact, strict=True)), (cropped.size, exact)
assert cropped.convert("L").histogram()[0] == page.convert("L").histogram()[0]  # no ink cut
assert m.crop(Image.new("RGB", (10, 10), BG)).size == (10, 10)  # blank page: unchanged

# prune: deletes what was last modified before KEEP_DAYS days ago, only issue PDFs and tile folders
def noon(days_ago):
    return dt.datetime.combine(dt.date.today() - dt.timedelta(days=days_ago), dt.time(12)).timestamp()


with tempfile.TemporaryDirectory() as tmp:
    m.DATA = Path(tmp)
    files = {"final/IlSole24Ore_2026-11-01.pdf": (8, False), "final/IlSole24Ore_2026-11-02.pdf": (7, True),
             "final/notes.pdf": (30, True), "download/20261101_SOLE/x_0.gif": (8, True)}
    for name, (age, _) in files.items():
        (m.DATA / name).parent.mkdir(parents=True, exist_ok=True)
        (m.DATA / name).write_bytes(b"")
        os.utime(m.DATA / name, (noon(age), noon(age)))
    os.utime(m.DATA / "download/20261101_SOLE", (noon(8), noon(8)))  # the folder of a run failed 8 days ago
    m.prune(7, m.DATA / "final")
    assert not (m.DATA / "download/20261101_SOLE").exists()
    for name, (_age, kept) in files.items():
        if not name.startswith("download/"):
            assert (m.DATA / name).exists() == kept, name

# find_issue: from tomorrow backwards, Monday's edition first on Mondays, one clear message on failure
m.dt = types.SimpleNamespace(date=type("Today", (dt.date,), {"today": classmethod(lambda c: dt.date(2026, 11, 15))}),
                             timedelta=dt.timedelta)  # a Sunday
calls = []


def fake_index(s, d, edition, found=(dt.date(2026, 11, 14), "SOLE")):
    calls.append((d.isoformat(), edition))
    return {"pages": []} if (d, edition) == found else None


m.issue_index = fake_index
assert m.find_issue(None) == (dt.date(2026, 11, 14), {"pages": []})
assert calls == [("2026-11-16", "LUNEDI"), ("2026-11-16", "SOLE"), ("2026-11-15", "SOLE"), ("2026-11-15", "LUNEDI"),
                 ("2026-11-14", "SOLE")], calls
m.issue_index = lambda s, d, edition: None
for day, msg in ((None, "No issue found for 2026-11-08 to 2026-11-16"), (dt.date(2026, 11, 10), "No issue found for 2026-11-10")):
    try:
        m.find_issue(None, day)
        raise AssertionError("no SystemExit")
    except SystemExit as e:
        assert str(e) == msg, e

# main: an index without page PDFs falls back to the tiles; a failed tile stops the run at once,
# without downloading the rest of the queue
def fake_fetch(s, url, path):
    started.append(url)
    if url.endswith("p0/0.gif"):
        raise RuntimeError("tile missing")
    time.sleep(0.2)


def no_login(s):
    raise RuntimeError("no login in the test")


# the default mode (full) fails without a login and falls back to pages, then to tiles; the real .env is not read
m.load_env, m.sso_login = (lambda: None), no_login
pages = [{"page": n, "secret": f"s{n}", "splitted": f"p{n}"} for n in range(40)]  # 320 tiles
index = {"pages": pages, "edition": {"description": "E", "code": "SOLE"}, "issue": {"description": "I"}}
m.find_issue, m.fetch, sys.argv, started = (lambda s, day: (dt.date(2026, 11, 15), index)), fake_fetch, ["test"], []
with tempfile.TemporaryDirectory() as tmp:
    m.DATA = Path(tmp)
    try:
        m.main()
        raise AssertionError("no error")
    except RuntimeError as e:
        assert str(e) == "tile missing", e
assert len(started) < 2 * m.DOWNLOAD_THREADS, len(started)


# main: the page PDFs are merged in page order, and the downloads folder is removed
def pdf_fetch(s, url, path, magic=b"GIF"):
    assert magic == b"%PDF", url
    w, buf = PdfWriter(), io.BytesIO()
    w.add_blank_page(width=100 + int(url[len(m.CDN) + 1:-4]), height=100)  # the width tells the page apart
    w.write(buf)
    path.write_bytes(buf.getvalue())


index["pages"] = [{"page": n, "secret": f"s{n}", "pdf": f"p{n}.pdf", "splitted": f"p{n}"} for n in (2, 0, 1)]
m.fetch = pdf_fetch
with tempfile.TemporaryDirectory() as tmp:
    m.DATA = Path(tmp)
    m.main()
    widths = [float(p.mediabox.width) for p in PdfReader(m.DATA / "final/IlSole24Ore_2026-11-15.pdf").pages]
    assert widths == [100, 101, 102], widths
    assert not (m.DATA / "download/20261115_SOLE").exists()


# full_issue: the PDF inside the server's ZIP; an answer that is not a ZIP raises
class FakeResponse:
    def __init__(self, body):
        self.body, self.headers = body, {"content-length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        yield self.body


zbuf = io.BytesIO()
with zipfile.ZipFile(zbuf, "w") as z:
    z.writestr("S24-20261115-SOLE.pdf", b"%PDF-1.7 whole")
full_index = {"newspaper": {"code": "S24"}, "edition": {"code": "SOLE", "id": 1}, "issue": {"code": "20261115"}, "version": 1}
m.sso_login = lambda s: None
for body, expected in ((zbuf.getvalue(), b"%PDF-1.7 whole"), (b"DOWNLOAD_KO_USER_INVALID", None)):
    fake = types.SimpleNamespace(post=lambda url, **kw: FakeResponse(body))
    try:
        assert m.full_issue(fake, full_index, None) == expected
    except RuntimeError as e:
        assert expected is None and "no ZIP" in str(e), e

# FirstForm: only the first form, without checkboxes and buttons
form = m.FirstForm('<form action="/a"><input type="hidden" name="h" value="1"><input name="t">'
                   '<input type="checkbox" name="c" value="x"><input type="submit" name="s"></form>'
                   '<form action="/b"><input name="other"></form>')
assert (form.action, form.fields) == ("/a", [("h", "1"), ("t", "")]), (form.action, form.fields)
print("ok")
