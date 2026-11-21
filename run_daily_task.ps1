# Run the scheduled task "ilsole24ore-dl daily" now, with the same arguments as its 07:00 run,
# and show its log as it grows until the task ends.
#     powershell -ExecutionPolicy Bypass -File run_daily_task.ps1     (or double-click run_daily_task.bat)
$ErrorActionPreference = "Stop"
$name = "ilsole24ore-dl daily"
$log = Join-Path $PSScriptRoot "data\download_latest_issue.log"
if (-not (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)) {
    throw "No scheduled task '$name': register it first with register_daily_task.ps1"
}
$offset = if (Test-Path $log) { (Get-Item $log).Length } else { 0 }

function Show-NewLog {
    # Print what the task appended to the log since the last call
    if (-not (Test-Path $log)) { return }
    $fs = [IO.File]::Open($log, "Open", "Read", "ReadWrite")  # the task keeps the file open for writing
    try {
        $length = $fs.Length
        if ($length -le $script:offset) { return }
        $fs.Seek($script:offset, "Begin") | Out-Null
        $bytes = New-Object byte[] ($length - $script:offset)
        $read = $fs.Read($bytes, 0, $bytes.Length)
        $script:offset += $read
        Write-Host -NoNewline ([Text.Encoding]::Default.GetString($bytes, 0, $read))
    } finally { $fs.Close() }
}

$before = (Get-ScheduledTaskInfo -TaskName $name).LastRunTime
Start-ScheduledTask -TaskName $name
Write-Host "Started '$name'; its log ($log) follows."
# Wait for the task to start (its last run time changes), then follow the log while it runs
$deadline = (Get-Date).AddSeconds(30)
while ((Get-ScheduledTaskInfo -TaskName $name).LastRunTime -eq $before -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
$deadline = (Get-Date).AddMinutes(35)  # the task itself stops after 30 minutes
do {
    Start-Sleep -Seconds 2
    Show-NewLog
    $state = (Get-ScheduledTask -TaskName $name).State
} while ($state -eq "Running" -and (Get-Date) -lt $deadline)
Show-NewLog
$info = Get-ScheduledTaskInfo -TaskName $name
Write-Host ("`n{0}: state={1}, result={2} (0 = success), next run {3}" -f $name, $state, $info.LastTaskResult,
    $info.NextRunTime.ToString("yyyy-MM-dd HH:mm"))
