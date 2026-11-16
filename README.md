Downloads the daily newspaper "Il Sole 24 Ore" as a single PDF. Use it only if you have a subscription to the digital edition (e.g. through your university).

```
download_latest_issue.bat              # the latest issue
download_latest_issue.bat 2026-11-16   # the issue of a given date
```

or `uv run python src/ilsole24ore_dl.py [YYYY-MM-DD]`. The PDF is saved as `data/final/IlSole24Ore_<date>.pdf`; an issue already downloaded is skipped.

How it works: the viewer's issue index lists the pages; each page is downloaded as 8 image tiles (in parallel, cached in `data/download/` so an interrupted run resumes), stitched at full resolution, cropped to the printed area and merged into the PDF without re-encoding. No browser and no ImageMagick are needed. `python src/test_ilsole24ore_dl.py` checks the stitching and cropping offline.
