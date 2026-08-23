# Register the weekly end-to-end run as a Windows scheduled task.
#
# INTERACTIVE on purpose. The suite drives the tray and the AutoCam desktop
# GUI, so it needs a real desktop session -- a task configured to "run whether
# the user is logged on or not" lands in Session 0, where AutoCam's window
# never appears and every run fails for the wrong reason.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_weekly_e2e_task.ps1
#
# Remove with:
#   Unregister-ScheduledTask -TaskName "SoccerCamWeeklyE2E" -Confirm:$false

param(
    [string]$TaskName = "SoccerCamWeeklyE2E",
    # Sunday early morning: the machine is idle, and a failure is waiting to be
    # read before the week's games.
    [string]$DayOfWeek = "Sunday",
    [string]$At = "03:00"
)

$ErrorActionPreference = 'Stop'

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$UvPath = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $UvPath) { throw "uv is not on PATH; install it or run from a shell that has it." }

Write-Host "Project:  $ProjectDir"
Write-Host "uv:       $UvPath"
Write-Host "Schedule: $DayOfWeek at $At"

# Fail early and loudly rather than registering a task that can never pass.
& $UvPath run python -m scripts.run_weekly_e2e --dry-run --no-notify
if ($LASTEXITCODE -ne 0) {
    throw "Prerequisites are not met (see above). Fix these before scheduling, or the weekly run will just report the same thing every week."
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$Action = New-ScheduledTaskAction `
    -Execute $UvPath `
    -Argument "run python -m scripts.run_weekly_e2e" `
    -WorkingDirectory $ProjectDir

$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DayOfWeek -At $At

# Interactive: needs the desktop for the tray + AutoCam GUI.
$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Highest

# A run that hangs must not still be holding the machine a week later, when the
# next one starts.
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Weekly soccer-cam end-to-end pipeline test. Reports over NTFY, and calls out AutoCam needing an update by name." | Out-Null

Write-Host ""
Write-Host "Registered '$TaskName'." -ForegroundColor Green
Write-Host "Run it now with:  Start-ScheduledTask -TaskName $TaskName"
