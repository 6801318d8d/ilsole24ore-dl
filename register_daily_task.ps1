# Register (or update) the Windows scheduled task that every day at 07:00 deletes the issues downloaded
# over 7 days ago and downloads the latest one. Output and errors go to data\download_latest_issue.log.
#     powershell -ExecutionPolicy Bypass -File register_daily_task.ps1
$ErrorActionPreference = "Stop"
$name = "ilsole24ore-dl daily"
$proj = $PSScriptRoot
$uv = (Get-Command uv).Source
$log = "$proj\data\download_latest_issue.log"
New-Item -ItemType Directory -Force "$proj\data" | Out-Null
# cmd /c strips the outer quotes; the window is headless, so everything goes to the log
$cmdline = "/c `"(echo ===== %date% %time% & `"$uv`" run --directory `"$proj`" python src\ilsole24ore_dl.py --keep-days 7) >> `"$log`" 2>&1`""
$action = New-ScheduledTaskAction -Execute "conhost.exe" -Argument "--headless cmd.exe $cmdline" -WorkingDirectory $proj
$trigger = New-ScheduledTaskTrigger -Daily -At "07:00"
# A run missed while the PC was off starts when it is back on, only once a network is up, also on battery
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Download the latest issue of Il Sole 24 Ore into $proj\data\final (log: $log)" -Force | Out-Null

# Test run
Start-ScheduledTask -TaskName $name
$deadline = (Get-Date).AddMinutes(3)
do { Start-Sleep -Seconds 3; $state = (Get-ScheduledTask -TaskName $name).State } while ($state -eq "Running" -and (Get-Date) -lt $deadline)
$info = Get-ScheduledTaskInfo -TaskName $name
"{0}: state={1} lastResult={2} nextRun={3}" -f $name, $state, $info.LastTaskResult, $info.NextRunTime.ToString("yyyy-MM-dd HH:mm")
