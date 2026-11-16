@echo off
rem Download the latest issue of Il Sole 24 Ore into data\final.
rem An optional argument YYYY-MM-DD downloads the issue of that date instead.
uv run --project "%~dp0." python "%~dp0src\ilsole24ore_dl.py" %*
if errorlevel 1 pause
