Downloads the daily newspaper "Il Sole 24 Ore" as a single PDF. Use it only if you have a subscription to the digital edition (e.g. through your university).

```
download_latest_issue.bat              # the latest issue
download_latest_issue.bat 2026-11-16   # the issue of a given date
```

or `uv run python src/ilsole24ore_dl.py [YYYY-MM-DD]`. The PDF is saved as `data/final/IlSole24Ore_<date>.pdf`; an issue already downloaded is skipped. `--keep-days N` first deletes the issues (and the tiles of failed runs) last modified over N days ago.

Daily download: `powershell -ExecutionPolicy Bypass -File register_daily_task.ps1` registers a Windows scheduled task that every day at 07:00 (or as soon as the PC is back on, once a network is up, also on battery) deletes the issues downloaded over 7 days ago and downloads the latest one; its output goes to `data/download_latest_issue.log`.

How it works: the viewer's issue index lists the pages; each page is downloaded as 8 image tiles (in parallel, cached in `data/download/` so an interrupted run resumes), stitched at full resolution, cropped to the printed area and merged into the PDF without re-encoding. No browser and no ImageMagick are needed. `python src/test_ilsole24ore_dl.py` checks the stitching, the cropping, the cleanup and the choice of the issue offline.
