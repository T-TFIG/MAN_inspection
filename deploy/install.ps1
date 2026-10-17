#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Installs MAN_inspection so it runs automatically for any user who logs on.

.DESCRIPTION
  - Downloads the latest MAN_inspection.exe from GitHub Releases (or uses
    -ExePath if you already have a copy), installs it to
    C:\ProgramData\MAN_inspection\app\MAN_inspection.exe, and registers a
    Scheduled Task that starts it at logon for any user - no console window,
    no time limit, restarts a few times if it crashes.
  - Run this ONCE per employee machine, as Administrator (e.g. via GPO startup
    script, Intune, PDQ Deploy, or manually).

.PARAMETER ExePath
  Path to an already-downloaded MAN_inspection.exe. If omitted, the script
  downloads the latest release from GitHub automatically.

.EXAMPLE
  # One-liner from an elevated PowerShell prompt:
  irm https://raw.githubusercontent.com/T-TFIG/MAN_inspection/main/deploy/install.ps1 | iex

.EXAMPLE
  # Using a copy you already downloaded:
  .\install.ps1 -ExePath C:\Downloads\MAN_inspection.exe
#>
param(
    [string]$ExePath
)

$ErrorActionPreference = "Stop"

$InstallDir = "$env:ProgramData\MAN_inspection\app"
$TargetExe  = Join-Path $InstallDir "MAN_inspection.exe"
$TaskName   = "MAN_inspection Activity Logger"
# A direct download, not the api.github.com JSON lookup: that REST endpoint
# rate-limits unauthenticated requests to 60/hour PER IP ADDRESS, which an
# office full of machines sharing one public IP can exhaust easily (this is
# exactly the error this script used to hit: "API rate limit exceeded for
# <office IP>"). This URL redirects through github.com itself (not the API
# host) straight to whatever the current latest release's asset is, so it
# isn't subject to that cap at all.
$LatestDownloadUrl = "https://github.com/T-TFIG/MAN_inspection/releases/latest/download/MAN_inspection.exe"

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

# Stop any already-running copy FIRST: downloading/copying over $TargetExe
# while the exe is running fails with "file is being used by another
# process" (Windows locks a running executable's image). A short pause after
# Stop-Process gives the OS a moment to actually release the file handle,
# since -Force kills it but the handle doesn't always free instantly.
$running = Get-Process -Name "MAN_inspection" -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "Stopping the currently running copy..."
    $running | Stop-Process -Force
    Start-Sleep -Milliseconds 500
}

# A few retries with a short wait: even after Stop-Process, something else
# (antivirus briefly scanning the just-terminated exe, Explorer, etc.) can
# hold the file handle a little longer.
$attempts = 5
for ($i = 1; $i -le $attempts; $i++) {
    try {
        if (-not $ExePath) {
            Write-Host "Downloading the latest release..."
            Invoke-WebRequest -Uri $LatestDownloadUrl -OutFile $TargetExe
        } else {
            if (-not (Test-Path $ExePath)) { throw "ExePath not found: $ExePath" }
            Copy-Item -Path $ExePath -Destination $TargetExe -Force
        }
        break
    } catch {
        if ($i -ge $attempts) { throw }
        Write-Host "  File still locked, retrying ($i/$attempts)..."
        Start-Sleep -Seconds 2
    }
}

Write-Host "Installed to $TargetExe"

# Remove a previous install of this task, if any, then register it fresh.
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$action      = New-ScheduledTaskAction -Execute $TargetExe
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
# A second trigger that re-fires every 30 minutes, indefinitely: if the app
# ever exits on its own (crash, a stuck update, anything) nobody has to
# notice and relaunch it by hand -- Task Scheduler does it, and every launch
# already checks for an update on its own. MultipleInstances IgnoreNew below
# means this is a no-op while a copy is already running, so it's harmless
# the other 29 times out of 30.
$repeatTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration ([TimeSpan]::MaxValue)
# BUILTIN\Users + "Limited" run level: starts for whichever user logs on,
# with no admin rights (this is a plain tray app, it doesn't need any).
$principal = New-ScheduledTaskPrincipal -GroupId "BUILTIN\Users" -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action `
    -Trigger @($logonTrigger, $repeatTrigger) `
    -Principal $principal -Settings $settings `
    -Description "Runs the MAN_inspection activity logger at logon for any user, and re-launches it every 30 minutes if it isn't already running." | Out-Null

Write-Host "Scheduled task '$TaskName' registered: starts at logon, and every 30 min if not already running."

# Start it right now too, for the currently logged-on user, so there's no
# need to log off/on to see it working.
Start-ScheduledTask -TaskName $TaskName
Write-Host "Started now. Look for the icon in the system tray."
