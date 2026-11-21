Downloads the daily newspaper "Il Sole 24 Ore" as a single PDF. Use it only if you have a subscription to the digital edition (e.g. through your university).

```
download_latest_issue.bat              # the latest issue
download_latest_issue.bat 2026-11-16   # the issue of a given date
```

or `uv run python src/ilsole24ore_dl.py [YYYY-MM-DD] [--mode full|pages|tiles] [--edition CODE|all]` (see "Modes" and "Editions and supplements"). The PDF is saved as `data/final/IlSole24Ore_<date>.pdf` (another folder with `--outdir DIR`), with the articles next to it (see "Articles"); an issue already downloaded is skipped. `--keep-days N` first deletes the issues in that folder (PDF, `.db` and `.md`, and the tiles of failed runs) last modified over N days ago.

Daily download: `powershell -ExecutionPolicy Bypass -File register_daily_task.ps1` registers a Windows scheduled task that every day at 07:00 (or as soon as the PC is back on, once a network is up, also on battery) deletes the issues downloaded over 7 days ago and downloads every edition of the latest issue (`--edition all`: the daily newspaper and the supplements of that day); its output goes to `data/download_latest_issue.log`. To run it now, with the same arguments, double-click `run_daily_task.bat` (or `powershell -ExecutionPolicy Bypass -File run_daily_task.ps1`): it starts the task and shows its log until the task ends.

## Modes

`--mode` chooses how the issue is obtained; a mode that fails (no login settings, login refused, a file missing) falls back to the next one, in this order:

| `--mode` | What you get | Login | Time and size (32 pages) |
|---|---|---|---|
| `full` (default) | the publisher's PDF of the whole edition: vector text you can search and copy, embedded fonts, pictures at 150 dpi; every page carries a line with the institutional account and a code | institutional (Shibboleth) login, from `.env` | about 8 s, 34 MB |
| `pages` | the PDF the viewer serves for each page, merged: one 200 dpi image per page, no text | none | about 7 s, 75 MB |
| `tiles` | the page images, 8 tiles per page, stitched at full resolution, cropped to the printed area and merged without re-encoding | none | about 50 MB |

`pages` and `tiles` are downloaded in parallel and cached in `data/download/`, so an interrupted run resumes. No browser and no ImageMagick are needed.

Login for `full`: copy `.env.example` to `.env` and fill it in. `.env` is in `.gitignore`: never commit it.

- `SOLE_LOGIN_URL`: the institutional access link to the newspaper that your library catalogue gives (it leads to the page where you pick your institution);
- `SOLE_IDP`: the entityID of your institution's Shibboleth identity provider (the `entityID` parameter of your institution's link on that page);
- `SOLE_USERNAME`, `SOLE_PASSWORD`: your institutional credentials.

The script logs in like a browser: it picks the institution, fills the identity provider's login form, accepts the release of the attributes to the publisher, and posts the answer back to the publisher's site, which opens the session; then it asks for the whole edition, as the viewer's "save" button does, and extracts the PDF from the ZIP the server sends.

The server has the PDF of the whole edition for issues from 2023 on (checked: 2023-06-15 yes, 2022-06-15 no); for older issues, and for supplements without one (e.g. `EBOOK`), it sends a one-page placeholder and `full` falls back to `pages`. The page PDFs of `EBOOK` are encrypted, so it ends in `tiles`.

## Editions and supplements

`--list-editions` lists the editions and supplements of the last weeks, from the site's newsstand page: `SOLE` (the daily, `LUNEDI` on Mondays), `DOMENICA`, `PLUS` (Plus24), `RAPPORTO`, `HOWTOSPENDIT`, `ULTIMAPAGINA`, the regional ones (`RCENTRO`, `RLOMBARDIA`, `RNORDEST`, `RNORDOVEST`, `RSUD`) and others. `--edition PLUS` downloads that edition (without a date: its latest issue, also when it is weekly or monthly); `--edition all` downloads every edition of that date (default: the latest issue of the daily). The daily is saved as `IlSole24Ore_<date>.pdf`, the other editions as `IlSole24Ore_<date>_<EDITION>.pdf`.

Archive: issues are found from about 2014 on. The site lists 2012 and 2013 issues only as archive PDFs that it no longer serves (404), and finds none for 2010 and 2011.

## Articles

Next to each PDF the script saves, with no login:

- `IlSole24Ore_<date>.db`: the viewer's own database of the issue (SQLite, about 3.5 MB for 32 pages), with the full text of every article (headline, subhead, abstract, text, signature, captions, page, section) and a full-text index (`article_fts`). For example, `SELECT page, headline FROM article_fts WHERE article_fts MATCH 'inflazione'`;
- `IlSole24Ore_<date>.md`: the same articles as Markdown, in page order under their sections, with the tables as Markdown tables (about 130,000 words for 32 pages). Editions made only of pictures (e.g. `ULTIMAPAGINA`) have no articles.

If the articles cannot be saved, the run only warns and keeps the PDF.

`python src/test_ilsole24ore_dl.py` checks offline the stitching, the cropping, the cleanup, the choice of the issue and of the edition, the reading of the newsstand page, the merging of the page PDFs, the extraction of the full PDF, the fallback from one mode to the next, and the conversion of the articles to Markdown.

## Show the viewer's PDF save button

With institutional (SSO) access the viewer hides its "Salvataggio PDF" button (the download icon at the right end of the bottom bar). `ilsole24ore_save_button.user.css` shows it again. It only changes how the page looks: whether the PDF can be downloaded is still decided by the site and by the terms of your subscription.

Install it in one of these ways; the first two need no restart and are edited from the browser.

- **uBlock Origin** (already installed in many browsers): Dashboard → "My filters", add this line and click "Apply changes":
  ```
  quotidiano.ilsole24ore.com##body.classe_sso a#saveButton:style(display: block !important)
  ```
- **Stylus** (Firefox, Chrome, Edge): install it from the browser's add-on store, open "Manage" → "Write new style", paste the content of `ilsole24ore_save_button.user.css` and save.
- **Firefox, no extension** (`userContent.css`):
  1. In `about:config` set `toolkit.legacyUserProfileCustomizations.stylesheets` to `true`.
  2. In `about:support`, next to "Profile Folder", click "Open Folder"; create a subfolder `chrome` if there is none.
  3. Copy `ilsole24ore_save_button.user.css` into `chrome` as `userContent.css` (if that file already exists, paste the content at its end).
  4. Restart Firefox, which reads `userContent.css` only at startup. Every later change needs the file edited and another restart.

Check: open an issue in the viewer (`quotidiano.ilsole24ore.com/sfoglio/...`) and reload it; the download icon appears at the right end of the bottom bar.
