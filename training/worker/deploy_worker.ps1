# Deploy pipeline worker to a remote machine (laptop or FORTNITE-OP).
# Run from the SERVER in an elevated PowerShell prompt.
#
# Usage:
#   .\training\worker\deploy_worker.ps1 -Machine laptop
#   .\training\worker\deploy_worker.ps1 -Machine fortnite
#
# What it does:
#   1. Creates C:\soccer-cam-label\work and C:\soccer-cam-label\models on the remote
#   2. Copies worker scripts + config
#   3. Copies ONNX model if not present
#   4. Registers a Scheduled Task to auto-start the worker at login
#   5. Starts the worker

param(
    [Parameter(Mandatory=$true)]
    [ValidateSet("laptop", "fortnite")]
    [string]$Machine
)

$ErrorActionPreference = "Stop"

# Machine config
$Machines = @{
    "laptop" = @{
        Hostname = "jared-laptop"
        Capabilities = @("tile", "label", "train")
        GPU = "RTX 4070"
    }
    "fortnite" = @{
        Hostname = "FORTNITE-OP"
        Capabilities = @("label", "tile")
        GPU = "RTX 3060 Ti"
    }
}

$Config = $Machines[$Machine]
$RemoteHost = $Config.Hostname
$ServerShare = "\\192.168.86.152\training"
$QueueDB = "$ServerShare\work_queue.db"
$RemoteDir = "C:\soccer-cam-label"
$RemoteWork = "$RemoteDir\work"
$RemoteModels = "$RemoteDir\models"
$ProjectDir = "C:\Users\jared\projects\soccer-cam-annotation"

Write-Host "Deploying worker to $RemoteHost ($Machine)..."
Write-Host "  Capabilities: $($Config.Capabilities -join ', ')"

# 1. Create directories
Write-Host "`n[1/5] Creating directories on $RemoteHost..."
$ScriptBlock = {
    param($RemoteDir, $RemoteWork, $RemoteModels)
    New-Item -ItemType Directory -Force -Path $RemoteDir | Out-Null
    New-Item -ItemType Directory -Force -Path $RemoteWork | Out-Null
    New-Item -ItemType Directory -Force -Path $RemoteModels | Out-Null
    Write-Output "Directories created"
}
Invoke-Command -ComputerName $RemoteHost -ScriptBlock $ScriptBlock -ArgumentList $RemoteDir, $RemoteWork, $RemoteModels

# 2. Copy worker scripts
Write-Host "`n[2/5] Copying worker scripts..."
$FilesToCopy = @(
    "training\worker\worker.py",
    "training\worker\resources.py",
    "training\worker\__init__.py",
    "training\worker\__main__.py",
    "training\tasks\__init__.py",
    "training\tasks\io.py",
    "training\tasks\stage.py",
    "training\tasks\tile.py",
    "training\tasks\label.py",
    "training\tasks\train.py",
    "training\tasks\sonnet_qa.py",
    "training\tasks\generate_review.py",
    "training\tasks\ingest_reviews.py",
    "training\pipeline\config.py",
    "training\pipeline\config.toml",
    "training\pipeline\queue.py",
    "training\pipeline\registry.py",
    "training\pipeline\state_machine.py",
    "training\pipeline\__init__.py",
    "training\data_prep\game_manifest.py",
    "training\__init__.py"
)

# Create directory structure on remote
$RemoteProjDir = "\\$RemoteHost\c$\soccer-cam-label\project"
$Dirs = @(
    "$RemoteProjDir\training\worker",
    "$RemoteProjDir\training\tasks",
    "$RemoteProjDir\training\pipeline",
    "$RemoteProjDir\training\data_prep"
)
foreach ($Dir in $Dirs) {
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
}

foreach ($File in $FilesToCopy) {
    $Src = Join-Path $ProjectDir $File
    $Dst = Join-Path $RemoteProjDir $File
    if (Test-Path $Src) {
        Copy-Item $Src $Dst -Force
    } else {
        Write-Warning "Not found: $Src"
    }
}
Write-Host "  Copied $($FilesToCopy.Count) files"

# 3. Generate worker config
Write-Host "`n[3/5] Writing worker config..."
$Capabilities = ($Config.Capabilities | ForEach-Object { "`"$_`"" }) -join ", "
$ConfigContent = @"
[worker]
hostname = "$RemoteHost"
capabilities = [$Capabilities]
server_share = "$ServerShare"
queue_db = "$QueueDB"
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
Set-Content -Path "$RemoteProjDir\worker_config.toml" -Value $ConfigContent
Write-Host "  Config written with capabilities: $Capabilities"

# 4. Copy ONNX model if needed
Write-Host "`n[4/5] Checking ONNX model..."
$ModelSrc = "F:\test\***REDACTED***\model.onnx"
$ModelDst = "\\$RemoteHost\c$\soccer-cam-label\models\model.onnx"
if (-not (Test-Path $ModelDst)) {
    if (Test-Path $ModelSrc) {
        Write-Host "  Copying ONNX model (this may take a minute)..."
        Copy-Item $ModelSrc $ModelDst
        Write-Host "  Model copied"
    } else {
        Write-Warning "  ONNX model not found at $ModelSrc"
    }
} else {
    Write-Host "  Model already present"
}

# 5. Register scheduled task
Write-Host "`n[5/5] Registering scheduled task on $RemoteHost..."
$TaskScript = {
    param($RemoteProjDir)

    $PythonExe = "C:\Python313\python.exe"
    if (-not (Test-Path $PythonExe)) {
        # Try other locations
        $PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
    }

    $Action = New-ScheduledTaskAction `
        -Execute $PythonExe `
        -Argument "-m training.worker run --config worker_config.toml" `
        -WorkingDirectory $RemoteProjDir

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
        -Description "Soccer-cam pipeline worker" `
        -RunLevel Highest

    Start-ScheduledTask -TaskName "PipelineWorker"
    Write-Output "Task registered and started"
}
Invoke-Command -ComputerName $RemoteHost -ScriptBlock $TaskScript -ArgumentList $RemoteProjDir

Write-Host "`nDeploy complete! Worker should be running on $RemoteHost."
Write-Host "Check status: uv run python -m training.pipeline machines"
