# FORTNITE-OP worker setup script
# Run this LOCALLY on FORTNITE-OP (not via remoting).
#
# Copy this to FORTNITE-OP first:
#   copy \\192.168.86.152\training\deploy\fortnite_setup.ps1 C:\soccer-cam-label\
#   cd C:\soccer-cam-label
#   powershell -ExecutionPolicy Bypass -File fortnite_setup.ps1

$ErrorActionPreference = "Stop"
$WorkDir = "C:\soccer-cam-label"
$ProjectDir = "$WorkDir\project"
$ServerIP = "192.168.86.152"

Write-Host "=== FORTNITE-OP Pipeline Worker Setup ==="
Write-Host ""

# Step 1: Map network shares
Write-Host "[1/5] Mapping network shares..."
net use \\$ServerIP\training /user:trainer amy4ever /persistent:yes 2>$null
net use \\$ServerIP\video /user:trainer amy4ever /persistent:yes 2>$null

if (Test-Path "\\$ServerIP\training\work_queue.db") {
    Write-Host "  Training share: OK"
} else {
    Write-Host "  Training share: FAILED - check credentials" -ForegroundColor Red
    exit 1
}
if (Test-Path "\\$ServerIP\video") {
    Write-Host "  Video share: OK"
} else {
    Write-Host "  Video share: FAILED" -ForegroundColor Red
    exit 1
}

# Step 2: Create directories
Write-Host "`n[2/5] Creating directories..."
New-Item -ItemType Directory -Force -Path "$WorkDir\work" | Out-Null
New-Item -ItemType Directory -Force -Path "$WorkDir\models" | Out-Null
New-Item -ItemType Directory -Force -Path "$ProjectDir\training\worker" | Out-Null
New-Item -ItemType Directory -Force -Path "$ProjectDir\training\tasks" | Out-Null
New-Item -ItemType Directory -Force -Path "$ProjectDir\training\pipeline" | Out-Null
New-Item -ItemType Directory -Force -Path "$ProjectDir\training\data_prep" | Out-Null
Write-Host "  Done"

# Step 3: Copy worker code from server share
Write-Host "`n[3/5] Copying worker code from server..."
$ShareDeploy = "\\$ServerIP\training\deploy"

# Copy all Python files
$FilePairs = @(
    @("training\__init__.py", "$ProjectDir\training\__init__.py"),
    @("training\worker\__init__.py", "$ProjectDir\training\worker\__init__.py"),
    @("training\worker\__main__.py", "$ProjectDir\training\worker\__main__.py"),
    @("training\worker\worker.py", "$ProjectDir\training\worker\worker.py"),
    @("training\worker\resources.py", "$ProjectDir\training\worker\resources.py"),
    @("training\tasks\__init__.py", "$ProjectDir\training\tasks\__init__.py"),
    @("training\tasks\io.py", "$ProjectDir\training\tasks\io.py"),
    @("training\tasks\stage.py", "$ProjectDir\training\tasks\stage.py"),
    @("training\tasks\tile.py", "$ProjectDir\training\tasks\tile.py"),
    @("training\tasks\label.py", "$ProjectDir\training\tasks\label.py"),
    @("training\tasks\train.py", "$ProjectDir\training\tasks\train.py"),
    @("training\tasks\sonnet_qa.py", "$ProjectDir\training\tasks\sonnet_qa.py"),
    @("training\tasks\generate_review.py", "$ProjectDir\training\tasks\generate_review.py"),
    @("training\tasks\ingest_reviews.py", "$ProjectDir\training\tasks\ingest_reviews.py"),
    @("training\pipeline\__init__.py", "$ProjectDir\training\pipeline\__init__.py"),
    @("training\pipeline\config.py", "$ProjectDir\training\pipeline\config.py"),
    @("training\pipeline\config.toml", "$ProjectDir\training\pipeline\config.toml"),
    @("training\pipeline\queue.py", "$ProjectDir\training\pipeline\queue.py"),
    @("training\pipeline\registry.py", "$ProjectDir\training\pipeline\registry.py"),
    @("training\pipeline\state_machine.py", "$ProjectDir\training\pipeline\state_machine.py"),
    @("training\data_prep\__init__.py", "$ProjectDir\training\data_prep\__init__.py"),
    @("training\data_prep\game_manifest.py", "$ProjectDir\training\data_prep\game_manifest.py")
)

foreach ($Pair in $FilePairs) {
    $Src = Join-Path $ShareDeploy $Pair[0]
    $Dst = $Pair[1]
    if (Test-Path $Src) {
        Copy-Item $Src $Dst -Force
    } else {
        Write-Warning "  Missing: $($Pair[0])"
    }
}
Write-Host "  Copied $($FilePairs.Count) files"

# Step 4: Write worker config
Write-Host "`n[4/5] Writing worker config..."
$ConfigContent = @"
[worker]
hostname = "FORTNITE-OP"
capabilities = ["label", "tile"]
server_share = "\\\\$ServerIP\\training"
queue_db = "\\\\$ServerIP\\training\\work_queue.db"
local_work_dir = "C:/soccer-cam-label/work"
local_models_dir = "C:/soccer-cam-label/models"

[resources]
max_gpu_temp = 85
min_disk_free_gb = 20
gpu_device = 0
idle_games = ["FortniteClient-Win64-Shipping", "RobloxPlayerBeta", "RocketLeague"]

[heartbeat]
interval = 30
"@
Set-Content -Path "$ProjectDir\worker_config.toml" -Value $ConfigContent
Write-Host "  Config written"

# Step 5: Register scheduled task
Write-Host "`n[5/5] Registering scheduled task..."
$PythonExe = "C:\Python313\python.exe"

$Action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "-m training.worker run --config worker_config.toml" `
    -WorkingDirectory $ProjectDir

$Trigger = New-ScheduledTaskTrigger -AtLogon
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -RestartCount 999 `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Days 365)

Unregister-ScheduledTask -TaskName "PipelineWorker" -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName "PipelineWorker" `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Soccer-cam pipeline worker (pauses for games)"

Write-Host "  Task registered"

# Start it now
Write-Host "`nStarting worker..."
Start-ScheduledTask -TaskName "PipelineWorker"
Start-Sleep -Seconds 3

$State = (Get-ScheduledTask -TaskName "PipelineWorker").State
Write-Host "Worker state: $State"

Write-Host "`n=== Setup complete ==="
Write-Host "Worker will:"
Write-Host "  - Pull label and tile tasks from the server queue"
Write-Host "  - Pause when Fortnite/Roblox/RocketLeague is running"
Write-Host "  - Resume automatically when games exit"
Write-Host "  - Auto-start on login"
Write-Host ""
Write-Host "Check status from server:"
Write-Host "  uv run python -m training.pipeline machines"
