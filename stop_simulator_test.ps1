<#
.SYNOPSIS
    Stop the simulator E2E test and clean up.

.DESCRIPTION
    Stops the service and tray, removes the VIDEOGROUPER_CONFIG environment
    variable, and stops the Docker simulator.

    Does NOT delete shared_data_simulator/ -- preserves results for inspection.
#>

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$ServiceName = "VideoGrouperService"
$TrayProcess = "VideoGrouperTray"

Write-Host "=== Stopping Simulator E2E Test ===" -ForegroundColor Cyan
Write-Host ""

# ---------------------------------------------------------------
# Step 1: Stop service and tray
# ---------------------------------------------------------------
Write-Host "[1/3] Stopping service and tray..." -ForegroundColor Yellow

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Running") {
    Stop-Service -Name $ServiceName -Force
    Write-Host "  Service stopped" -ForegroundColor Green
} else {
    Write-Host "  Service was not running" -ForegroundColor Gray
}

$trayProcs = Get-Process -Name $TrayProcess -ErrorAction SilentlyContinue
if ($trayProcs) {
    $trayProcs | Stop-Process -Force
    Write-Host "  Tray stopped" -ForegroundColor Green
} else {
    Write-Host "  Tray was not running" -ForegroundColor Gray
}

# ---------------------------------------------------------------
# Step 2: Remove VIDEOGROUPER_CONFIG env var
# ---------------------------------------------------------------
Write-Host "[2/3] Removing VIDEOGROUPER_CONFIG environment variable..." -ForegroundColor Yellow

[System.Environment]::SetEnvironmentVariable("VIDEOGROUPER_CONFIG", $null, "Machine")
$env:VIDEOGROUPER_CONFIG = $null

Write-Host "  Environment variable removed" -ForegroundColor Green

# ---------------------------------------------------------------
# Step 3: Stop Docker simulator
# ---------------------------------------------------------------
Write-Host "[3/3] Stopping Docker simulator..." -ForegroundColor Yellow

Push-Location $ProjectRoot
try {
    & docker compose --profile reolink down -v 2>$null
    Write-Host "  Docker simulator stopped" -ForegroundColor Green
} catch {
    Write-Host "  Failed to stop Docker simulator (may already be stopped)" -ForegroundColor DarkYellow
} finally {
    Pop-Location
}

# Cleanup staging dir
$stagingDir = Join-Path $ProjectRoot "simulator_clips_staging"
if (Test-Path $stagingDir) {
    Remove-Item $stagingDir -Recurse -Force
    Write-Host "  Cleaned up staging dir" -ForegroundColor Gray
}

# ---------------------------------------------------------------
# Summary
# ---------------------------------------------------------------
Write-Host ""
Write-Host "=== Simulator Test Stopped ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Results preserved at:" -ForegroundColor White
Write-Host "  Logs:    $ProjectRoot\shared_data_simulator\logs\video_grouper.log"
Write-Host "  Videos:  $ProjectRoot\shared_data_simulator\2025.07.22-18.08.14\"
Write-Host ""
Write-Host "To restart production service:" -ForegroundColor White
Write-Host "  Start-Service $ServiceName"
Write-Host ""
