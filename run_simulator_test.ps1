<#
.SYNOPSIS
    Start a real E2E test: Docker camera simulator + installed Windows service + tray.

.DESCRIPTION
    Rebuilds executables, starts the Docker Reolink simulator with 2 real game clips
    (July 22, 2025), sets VIDEOGROUPER_CONFIG env var to point at shared_data_simulator/,
    deploys the new executables, and starts the service + tray.

    Everything is real except the camera. YouTube uploads go to private visibility,
    NTFY auto-responds, TeamSnap/PlayMetrics make real API calls.

    Uses VIDEOGROUPER_CONFIG environment variable (no registry changes needed).
    Run stop_simulator_test.ps1 to stop and clean up.
#>

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$SharedDataSim = Join-Path $ProjectRoot "shared_data_simulator"
$SharedDataProd = Join-Path $ProjectRoot "shared_data"
$InstallDir = "C:\Program Files\VideoGrouper"
$ServiceName = "VideoGrouperService"
$TrayProcess = "VideoGrouperTray"
$SimConfigPath = Join-Path $SharedDataSim "config.ini"

# Source clip directory (first 2 clips from July 22 game)
$GameDir = Join-Path $SharedDataProd "2025.07.22-18.08.14"
$Clip1 = "RecM09_DST20250722_180814_181313_0_9D28DB80000000_1B8D8384.mp4"
$Clip2 = "RecM09_DST20250722_181314_181814_0_9D28D380000000_1BA42229.mp4"

Write-Host "=== Simulator E2E Test ===" -ForegroundColor Cyan
Write-Host ""

# ---------------------------------------------------------------
# Step 1: Rebuild executables
# ---------------------------------------------------------------
Write-Host "[1/7] Rebuilding executables..." -ForegroundColor Yellow
Push-Location $ProjectRoot
try {
    & uv sync --extra dev --extra tray --extra service
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }

    & uv run pyinstaller --noconfirm VideoGrouperService.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller (service) failed" }

    & uv run pyinstaller --noconfirm VideoGrouperTray.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller (tray) failed" }

    Write-Host "  Built dist\VideoGrouperService.exe and dist\VideoGrouperTray.exe" -ForegroundColor Green
} finally {
    Pop-Location
}

# ---------------------------------------------------------------
# Step 2: Stage clips for Docker mount
# ---------------------------------------------------------------
Write-Host "[2/7] Staging clips for Docker mount..." -ForegroundColor Yellow
$StagingDir = Join-Path $ProjectRoot "simulator_clips_staging"
if (Test-Path $StagingDir) { Remove-Item $StagingDir -Recurse -Force }
New-Item -ItemType Directory -Path $StagingDir -Force | Out-Null

Copy-Item (Join-Path $GameDir $Clip1) $StagingDir
Copy-Item (Join-Path $GameDir $Clip2) $StagingDir
Write-Host "  Staged 2 clips (~920 MB) to $StagingDir" -ForegroundColor Green

# ---------------------------------------------------------------
# Step 3: Start Docker simulator
# ---------------------------------------------------------------
Write-Host "[3/7] Starting Docker simulator..." -ForegroundColor Yellow
Push-Location $ProjectRoot
try {
    # Stop any existing simulator
    & docker compose --profile reolink down -v 2>$null

    # Start with override that sets seed time and mounts real clips
    $env:SIMULATOR_CLIPS_DIR = $StagingDir.Replace('\', '/')
    & docker compose -f docker-compose.yaml -f docker-compose.simulator-test.yaml --profile reolink up -d --build
    if ($LASTEXITCODE -ne 0) { throw "Docker compose up failed" }

    # Wait for simulator to be ready
    Write-Host "  Waiting for simulator health check..." -ForegroundColor Gray
    $maxWait = 60
    $waited = 0
    while ($waited -lt $maxWait) {
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:8180" -TimeoutSec 2 -ErrorAction SilentlyContinue
            if ($response.StatusCode -ge 200) {
                break
            }
        } catch { }
        Start-Sleep -Seconds 2
        $waited += 2
    }
    if ($waited -ge $maxWait) {
        Write-Warning "Simulator did not respond within ${maxWait}s -- check docker logs"
    }

    # Verify recordings are seeded
    try {
        $dashboard = Invoke-RestMethod -Uri "http://127.0.0.1:8080/api/recordings" -TimeoutSec 5
        $recCount = ($dashboard | Measure-Object).Count
        Write-Host "  Simulator ready: $recCount recordings seeded" -ForegroundColor Green
    } catch {
        Write-Host "  Simulator started (could not verify recordings via dashboard)" -ForegroundColor DarkYellow
    }
} finally {
    Pop-Location
}

# ---------------------------------------------------------------
# Step 4: Set up shared_data_simulator directory
# ---------------------------------------------------------------
Write-Host "[4/7] Setting up shared_data_simulator/..." -ForegroundColor Yellow

# Create subdirectories
New-Item -ItemType Directory -Path (Join-Path $SharedDataSim "youtube") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $SharedDataSim "logs") -Force | Out-Null

# Copy YouTube credentials
Copy-Item (Join-Path $SharedDataProd "youtube\client_secret.json") (Join-Path $SharedDataSim "youtube\") -Force
Copy-Item (Join-Path $SharedDataProd "youtube\token.json") (Join-Path $SharedDataSim "youtube\") -Force

Write-Host "  YouTube creds copied" -ForegroundColor Green

# ---------------------------------------------------------------
# Step 5: Set VIDEOGROUPER_CONFIG env var (system-level for service)
# ---------------------------------------------------------------
Write-Host "[5/7] Setting VIDEOGROUPER_CONFIG environment variable..." -ForegroundColor Yellow

# Set system-level env var so the service (running as SYSTEM) can read it
[System.Environment]::SetEnvironmentVariable("VIDEOGROUPER_CONFIG", $SimConfigPath, "Machine")
# Also set for current process so the tray picks it up
$env:VIDEOGROUPER_CONFIG = $SimConfigPath

Write-Host "  VIDEOGROUPER_CONFIG = $SimConfigPath" -ForegroundColor Green

# ---------------------------------------------------------------
# Step 6: Stop existing service and tray, deploy executables
# ---------------------------------------------------------------
Write-Host "[6/7] Stopping existing instances and deploying..." -ForegroundColor Yellow

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Running") {
    Stop-Service -Name $ServiceName -Force
    Write-Host "  Service stopped" -ForegroundColor Gray
}

Get-Process -Name $TrayProcess -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1

# Remove stale lock file
$lockFile = Join-Path $SharedDataSim "tray_agent.lock"
if (Test-Path $lockFile) { Remove-Item $lockFile -Force }

# Deploy
Copy-Item (Join-Path $ProjectRoot "dist\VideoGrouperService.exe") $InstallDir -Force
Copy-Item (Join-Path $ProjectRoot "dist\VideoGrouperTray.exe") $InstallDir -Force
Write-Host "  Executables deployed" -ForegroundColor Green

# ---------------------------------------------------------------
# Step 7: Start service and tray
# ---------------------------------------------------------------
Write-Host "[7/7] Starting service and tray..." -ForegroundColor Yellow

Start-Service -Name $ServiceName
Write-Host "  Service started" -ForegroundColor Green

Start-Process -FilePath (Join-Path $InstallDir "VideoGrouperTray.exe") -ArgumentList "`"$SimConfigPath`""
Write-Host "  Tray launched" -ForegroundColor Green

# ---------------------------------------------------------------
# Summary
# ---------------------------------------------------------------
Write-Host ""
Write-Host "=== Simulator E2E Test Running ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Monitor:" -ForegroundColor White
Write-Host "  Logs:      $SharedDataSim\logs\video_grouper.log"
Write-Host "  Dashboard: http://127.0.0.1:8080"
Write-Host "  Service:   sc query $ServiceName"
Write-Host "  Tray:      System tray icon (double-click for status)"
Write-Host ""
Write-Host "Expected pipeline:" -ForegroundColor White
Write-Host "  1. Camera poll discovers 2 files            (~30s)"
Write-Host "  2. Baichuan downloads 2x ~460MB clips       (~60s)"
Write-Host "  3. Combine into ~10 min video               (~30s)"
Write-Host "  4. TeamSnap finds matching game              (~5s)"
Write-Host "  5. NTFY game start (auto-respond)            (~10s)"
Write-Host "  6. Trim video                                (~15s)"
Write-Host "  7. Autocam processing                        (~30 min)"
Write-Host "  8. YouTube upload (private)                  (~5 min)"
Write-Host ""
Write-Host "Total estimated runtime: ~40 minutes" -ForegroundColor Gray
Write-Host ""
Write-Host "Run stop_simulator_test.ps1 to stop and clean up." -ForegroundColor DarkYellow
