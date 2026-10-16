<#
.SYNOPSIS
  Quick diagnostics for "MAN_inspection won't start" on an employee machine.
  Run as a normal user (no admin needed) and share the output.
#>

Write-Host "=== 1) Is it already running? ===" -ForegroundColor Cyan
Get-Process -Name "MAN_inspection" -ErrorAction SilentlyContinue |
    Select-Object Id, StartTime, Path | Format-Table -AutoSize
if (-not (Get-Process -Name "MAN_inspection" -ErrorAction SilentlyContinue)) {
    Write-Host "(not running)"
}

Write-Host "`n=== 2) Installed files in C:\ProgramData\MAN_inspection\app ===" -ForegroundColor Cyan
$appDir = "$env:ProgramData\MAN_inspection\app"
if (Test-Path $appDir) {
    Get-ChildItem $appDir -Force | Select-Object Name, Length, LastWriteTime | Format-Table -AutoSize
} else {
    Write-Host "MISSING: $appDir does not exist at all."
}

Write-Host "`n=== 3) Leftover update files anywhere in that folder tree? ===" -ForegroundColor Cyan
Get-ChildItem "$env:ProgramData\MAN_inspection" -Recurse -Filter "*.new" -ErrorAction SilentlyContinue
Get-ChildItem "$env:Temp" -Filter "*_update.bat" -ErrorAction SilentlyContinue |
    Select-Object Name, LastWriteTime

Write-Host "`n=== 4) Scheduled Task status ===" -ForegroundColor Cyan
try {
    $task = Get-ScheduledTask -TaskName "MAN_inspection Activity Logger" -ErrorAction Stop
    $info = $task | Get-ScheduledTaskInfo
    $resultHex = "0x{0:X8}" -f $info.LastTaskResult
    [PSCustomObject]@{
        State          = $task.State
        LastRunTime    = $info.LastRunTime
        LastTaskResult = $resultHex
        NextRunTime    = $info.NextRunTime
    } | Format-List
} catch {
    Write-Host "Task not found (install.ps1 may not have been run, or ran without admin)."
}

Write-Host "`n=== 5) Windows Defender detections for MAN_inspection ===" -ForegroundColor Cyan
try {
    Get-MpThreatDetection -ErrorAction Stop |
        Where-Object { $_.Resources -match "MAN_inspection" -or $_.ThreatID } |
        Select-Object -First 10 DetectionTime, ThreatID, Resources |
        Format-Table -AutoSize
    Write-Host "(if nothing listed above matched MAN_inspection, Defender's own GUI history may show more)"
} catch {
    Write-Host "Could not query Defender detections (module not available)."
}

Write-Host "`n=== 6) Is the exe file actually a valid, complete PE file? ===" -ForegroundColor Cyan
$exe = Join-Path $appDir "MAN_inspection.exe"
if (Test-Path $exe) {
    $bytes = [System.IO.File]::ReadAllBytes($exe) | Select-Object -First 2
    $sizeMB = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    $joined = $bytes -join ","
    Write-Host "Size: $sizeMB MB. First 2 bytes (should be 77,90 which is MZ): $joined"
} else {
    Write-Host "exe not present, skipped"
}

Write-Host "`n=== Done - copy everything above and send it back ===" -ForegroundColor Green
