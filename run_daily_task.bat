@echo off
rem Run the daily scheduled task now (same arguments as its 07:00 run) and show its log until it ends.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_daily_task.ps1"
pause
