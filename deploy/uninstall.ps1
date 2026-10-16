#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Removes the MAN_inspection scheduled task and installed files from this machine.
#>
$ErrorActionPreference = "Stop"
$TaskName = "MAN_inspection Activity Logger"
$InstallDir = "$env:ProgramData\MAN_inspection"

Get-Process -Name "MAN_inspection" -ErrorAction SilentlyContinue | Stop-Process -Force
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "Scheduled task removed (if it existed)."

if (Test-Path $InstallDir) {
    # Keep email_config.json / admin_settings.json out of this by default -
    # remove just the app binary, not the whole config folder, unless asked.
    $appDir = Join-Path $InstallDir "app"
    if (Test-Path $appDir) {
        Remove-Item -Recurse -Force $appDir
        Write-Host "Removed $appDir"
    }
}
Write-Host "Done. Config/logs under $InstallDir were left in place; delete manually if you want those gone too."
