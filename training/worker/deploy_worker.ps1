# Deploy pipeline worker to a remote machine (laptop or FORTNITE-OP).
# Run from the SERVER in an elevated PowerShell prompt.
#
# Usage:
#   .\training\worker\deploy_worker.ps1 -Machine laptop
#   .\training\worker\deploy_worker.ps1 -Machine fortnite
#
# What it does:
#   1. Creates directories on the remote machine
#   2. Syncs worker code via robocopy
#   3. Writes worker config + startup script
#   4. Copies ONNX model if not present
#   5. Registers a single Scheduled Task (cleans up old ones)
#   6. Starts the worker and verifies it's running

param(
    [Parameter(Mandatory=$true)]
    [ValidateSet("laptop", "fortnite")]
    [string]$Machine
)

$ErrorActionPreference = "Stop"

# ── Machine definitions ──────────────────────────────────────────────
$ServerIP = "192.168.86.152"
$ServerShare = "\\$ServerIP\training"
$ApiUrl = "http://${ServerIP}:8643"
$RemoteUser = "training"
$RemotePass = "amy4ever"

$Machines = @{
    "laptop" = @{
        Hostname     = "jared-laptop"
        IP           = "192.168.86.24"
        Capabilities = @("tile", "label", "train")
        GPU          = "RTX 4070"
    }
    "fortnite" = @{
        Hostname     = "FORTNITE-OP"
        IP           = ""  # uses hostname
        Capabilities = @("label", "tile")
        GPU          = "RTX 3060 Ti"
    }
}

$Config = $Machines[$Machine]
$RemoteHost = if ($Config.IP) { $Config.IP } else { $Config.Hostname }
$RemoteHostname = $Config.Hostname
$ProjectDir = "C:\Users\jared\projects\soccer-cam-annotation"
$RemoteBase = "C:\soccer-cam-label"
$RemoteProjDir = "$RemoteBase\project"

$Cred = New-Object PSCredential($RemoteUser, (ConvertTo-SecureString $RemotePass -AsPlainText -Force))

Write-Host "Deploying worker to $RemoteHostname ($Machine) at $RemoteHost..."
Write-Host "  Capabilities: $($Config.Capabilities -join ', ')"
Write-Host "  GPU: $($Config.GPU)"

# ── 1. Create directories ────────────────────────────────────────────
Write-Host "`n[1/6] Creating directories on $RemoteHostname..."
Invoke-Command -ComputerName $RemoteHost -Credential $Cred -ScriptBlock {
    param($Base)
    @("$Base\project", "$Base\work", "$Base\models", "$Base\logs") | ForEach-Object {
        New-Item -ItemType Directory -Force -Path $_ | Out-Null
    }
    Write-Output "  Directories ready"

    # Ensure required pip packages are installed in system Python
    $PythonExe = "C:\Python313\python.exe"
    if (Test-Path $PythonExe) {
        $installed = & $PythonExe -m pip list --format=columns 2>&1
        $missing = @()
        foreach ($pkg in @("httpx", "psutil")) {
            if ($installed -notmatch $pkg) { $missing += $pkg }
        }
        if ($missing.Count -gt 0) {
            Write-Output "  Installing missing packages: $($missing -join ', ')"
            & $PythonExe -m pip install @missing --quiet 2>&1 | Out-Null
        } else {
            Write-Output "  Python packages OK"
        }
    }
} -ArgumentList $RemoteBase

# ── 2. Sync worker code ──────────────────────────────────────────────
Write-Host "`n[2/6] Syncing worker code..."

$Session = New-PSSession -ComputerName $RemoteHost -Credential $Cred

# Ensure remote directory structure exists
Invoke-Command -Session $Session -ScriptBlock {
    param($ProjDir)
    @(
        "$ProjDir\training\worker",
        "$ProjDir\training\tasks",
        "$ProjDir\training\pipeline",
        "$ProjDir\training\data_prep",
        "$ProjDir\training\inference",
        "$ProjDir\training\distributed",
        "$ProjDir\training\flywheel"
    ) | ForEach-Object { New-Item -ItemType Directory -Force -Path $_ | Out-Null }
} -ArgumentList $RemoteProjDir

# Copy all .py and .toml files from training/ subpackages
$CopyDirs = @("worker", "tasks", "pipeline", "data_prep", "inference", "distributed", "flywheel")
$FileCount = 0
foreach ($SubDir in $CopyDirs) {
    $LocalDir = "$ProjectDir\training\$SubDir"
    if (-not (Test-Path $LocalDir)) { continue }
    Get-ChildItem $LocalDir -Filter "*.py" | ForEach-Object {
        Copy-Item -ToSession $Session -Path $_.FullName -Destination "$RemoteProjDir\training\$SubDir\$($_.Name)" -Force
        $FileCount++
    }
    Get-ChildItem $LocalDir -Filter "*.toml" -ErrorAction SilentlyContinue | ForEach-Object {
        Copy-Item -ToSession $Session -Path $_.FullName -Destination "$RemoteProjDir\training\$SubDir\$($_.Name)" -Force
        $FileCount++
    }
}
# Copy training/__init__.py
Copy-Item -ToSession $Session -Path "$ProjectDir\training\__init__.py" -Destination "$RemoteProjDir\training\__init__.py" -Force
$FileCount++
# Copy pyproject.toml
Copy-Item -ToSession $Session -Path "$ProjectDir\pyproject.toml" -Destination "$RemoteProjDir\pyproject.toml" -Force
$FileCount++

Remove-PSSession $Session
Write-Host "  Synced $FileCount files"

# ── 3. Write worker config + startup script ──────────────────────────
Write-Host "`n[3/6] Writing worker config..."
$Capabilities = ($Config.Capabilities | ForEach-Object { "`"$_`"" }) -join ", "

# Generate config locally, then copy via PS session.
# TOML single-quoted strings are literal (no escape processing), which is
# critical for the server_share UNC path (\\server\share).
$TempConfig = [System.IO.Path]::GetTempFileName()
# Build the content line-by-line to avoid PowerShell mangling backslashes
# Use single-quoted strings where PowerShell would mangle backslashes
$ShareLine = 'server_share = ' + "'" + '\' + '\' + $ServerIP + '\training' + "'"
$Lines = @(
    "[worker]",
    "hostname = `"$RemoteHostname`"",
    "capabilities = [$Capabilities]",
    "api_url = `"$ApiUrl`"",
    $ShareLine,
    "local_work_dir = `"C:/soccer-cam-label/work`"",
    "local_models_dir = `"C:/soccer-cam-label/models`"",
    "",
    "[resources]",
    "max_gpu_temp = 85",
    "min_disk_free_gb = 20",
    "gpu_device = 0",
    'idle_games = ["FortniteClient-Win64-Shipping", "RobloxPlayerBeta", "RocketLeague"]',
    "cuda_path = `"C:/Python313/Lib/site-packages/torch/lib`"",
    "",
    "[heartbeat]",
    "interval = 30",
    "",
    "[logging]",
    "log_dir = `"C:/soccer-cam-label/logs`""
)
[System.IO.File]::WriteAllLines($TempConfig, $Lines)
$CfgSession = New-PSSession -ComputerName $RemoteHost -Credential $Cred
Copy-Item -ToSession $CfgSession -Path $TempConfig -Destination "$RemoteProjDir\worker_config.toml" -Force
Remove-PSSession $CfgSession
Remove-Item $TempConfig
Write-Host "  Config written: capabilities=[$Capabilities]"

# Write startup bat — single source of truth for env setup.
# This is the ONLY bat file the scheduled task runs.
$BatContent = @"
@echo off
REM Auto-generated by deploy_worker.ps1 -- do not hand-edit.
set PATH=C:\Python313\Lib\site-packages\torch\lib;%PATH%
set PYTHONPATH=C:\soccer-cam-label\project
cd /d C:\soccer-cam-label\project

REM Ensure SMB share is mounted (idempotent, suppresses errors if already mapped)
net use \\$ServerIP\training /user:$RemoteUser $RemotePass /persistent:yes >nul 2>&1

C:\Python313\python.exe -u -m training.worker run --config worker_config.toml
"@
Invoke-Command -ComputerName $RemoteHost -Credential $Cred -ScriptBlock {
    param($ProjDir, $Content)
    Set-Content -Path "$ProjDir\start_pipeline_worker.bat" -Value $Content -Encoding ASCII
} -ArgumentList $RemoteProjDir, $BatContent
Write-Host "  Startup script written"

# ── 4. Copy ONNX model if needed ─────────────────────────────────────
Write-Host "`n[4/6] Checking ONNX model..."
$ModelSrc = "F:\test\***REDACTED***\model.onnx"
$NeedsModel = Invoke-Command -ComputerName $RemoteHost -Credential $Cred -ScriptBlock {
    -not (Test-Path "C:\soccer-cam-label\models\model.onnx")
}
if ($NeedsModel) {
    if (Test-Path $ModelSrc) {
        Write-Host "  Copying ONNX model via PS session..."
        $ModelSession = New-PSSession -ComputerName $RemoteHost -Credential $Cred
        Copy-Item -ToSession $ModelSession -Path $ModelSrc -Destination "C:\soccer-cam-label\models\model.onnx" -Force
        Remove-PSSession $ModelSession
        Write-Host "  Model copied"
    } else {
        Write-Warning "  ONNX model not found at $ModelSrc"
    }
} else {
    Write-Host "  Model already present"
}

# ── 5. Register scheduled task ────────────────────────────────────────
Write-Host "`n[5/6] Registering scheduled task on $RemoteHostname..."
Invoke-Command -ComputerName $RemoteHost -Credential $Cred -ScriptBlock {
    param($ProjDir)

    # Clean up ALL old worker tasks — one canonical task only
    @("PipelineWorker", "LaptopWorker", "GPUWorker") | ForEach-Object {
        Unregister-ScheduledTask -TaskName $_ -Confirm:$false -ErrorAction SilentlyContinue
    }

    $Action = New-ScheduledTaskAction `
        -Execute "$ProjDir\start_pipeline_worker.bat" `
        -WorkingDirectory $ProjDir

    $Trigger = New-ScheduledTaskTrigger -AtLogon
    $Settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -RestartInterval (New-TimeSpan -Minutes 2) `
        -RestartCount 999 `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Days 365)

    Register-ScheduledTask `
        -TaskName "PipelineWorker" `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Description "Soccer-cam pipeline worker (auto-deployed)" `
        -RunLevel Highest `
        -User "training" `
        -Password "amy4ever"

    Start-ScheduledTask -TaskName "PipelineWorker"
    Write-Output "  Task registered and started"
} -ArgumentList $RemoteProjDir

# ── 6. Verify ─────────────────────────────────────────────────────────
Write-Host "`n[6/6] Verifying worker started..."
Start-Sleep -Seconds 8

Invoke-Command -ComputerName $RemoteHost -Credential $Cred -ScriptBlock {
    param($Base)

    $procs = Get-Process python* -ErrorAction SilentlyContinue
    if ($procs) {
        Write-Output "  Python process running (PID: $($procs[0].Id))"
    } else {
        Write-Warning "  No Python process found!"
    }

    $logFile = "$Base\logs\worker.log"
    if (Test-Path $logFile) {
        $lines = Get-Content $logFile -Tail 5
        Write-Output "  Recent log:"
        $lines | ForEach-Object { Write-Output "    $_" }
    }

    $task = Get-ScheduledTask -TaskName "PipelineWorker" -ErrorAction SilentlyContinue
    Write-Output "  Task state: $($task.State)"
} -ArgumentList $RemoteBase

Write-Host "`nDeploy complete!"
Write-Host "Monitor: uv run python -m training.pipeline status"
