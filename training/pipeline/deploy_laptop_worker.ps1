# Deploy pipeline worker to jared-laptop (192.168.86.24)
# Run from server: powershell -ExecutionPolicy Bypass -File training\pipeline\deploy_laptop_worker.ps1
#
# Prerequisites: laptop must be reachable and have PS remoting enabled.
# Share auth (cmdkey) must be done interactively at the laptop keyboard.

param(
    [string]$LaptopHost = "192.168.86.24",
    [string]$LaptopUser = "training",
    [string]$LaptopPass = "amy4ever",
    [string]$ServerIP   = "192.168.86.152",
    [string]$ProjectDir = "C:\soccer-cam-label\project"
)

$cred = New-Object System.Management.Automation.PSCredential($LaptopUser, (ConvertTo-SecureString $LaptopPass -AsPlainText -Force))

Write-Host "=== Deploying pipeline worker to $LaptopHost ==="

# Step 1: Install uv
Write-Host "`n--- Installing uv ---"
Invoke-Command -ComputerName $LaptopHost -Credential $cred -ScriptBlock {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host "Installing uv..."
        Invoke-WebRequest -Uri "https://astral.sh/uv/install.ps1" -OutFile "$env:TEMP\install_uv.ps1"
        & "$env:TEMP\install_uv.ps1"
        # Add to PATH for this session
        $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
    }
    uv --version
}

# Step 2: Create project directory and copy code
Write-Host "`n--- Copying project code ---"
Invoke-Command -ComputerName $LaptopHost -Credential $cred -ScriptBlock {
    param($ProjectDir)
    New-Item -Path $ProjectDir -ItemType Directory -Force | Out-Null
    Write-Host "Created $ProjectDir"
} -ArgumentList $ProjectDir

# Copy essential files via SMB
$session = New-PSSession -ComputerName $LaptopHost -Credential $cred

# Create the directory structure
Invoke-Command -Session $session -ScriptBlock {
    param($ProjectDir)
    @(
        "$ProjectDir\training\pipeline",
        "$ProjectDir\training\worker",
        "$ProjectDir\training\tasks",
        "$ProjectDir\training\data_prep",
        "$ProjectDir\training\inference"
    ) | ForEach-Object {
        New-Item -Path $_ -ItemType Directory -Force | Out-Null
    }
} -ArgumentList $ProjectDir

# Copy files via the session
$localProject = "C:\Users\jared\projects\soccer-cam-annotation"
$filesToCopy = @(
    "pyproject.toml",
    "training\__init__.py",
    "training\pipeline\__init__.py",
    "training\pipeline\__main__.py",
    "training\pipeline\api.py",
    "training\pipeline\client.py",
    "training\pipeline\config.py",
    "training\pipeline\config.toml",
    "training\pipeline\queue.py",
    "training\pipeline\registry.py",
    "training\pipeline\state_machine.py",
    "training\pipeline\orchestrator.py",
    "training\worker\__init__.py",
    "training\worker\__main__.py",
    "training\worker\worker.py",
    "training\worker\resources.py",
    "training\tasks\__init__.py",
    "training\tasks\io.py",
    "training\tasks\tile.py",
    "training\tasks\label.py",
    "training\tasks\train.py",
    "training\tasks\sonnet_qa.py",
    "training\tasks\generate_review.py",
    "training\tasks\ingest_reviews.py",
    "training\data_prep\__init__.py",
    "training\data_prep\game_manifest.py",
    "training\data_prep\trajectory_gaps.py",
    "training\data_prep\trajectory_validator.py",
    "training\data_prep\manifest_dataset.py",
    "training\data_prep\organize_dataset.py",
    "training\inference\__init__.py",
    "training\inference\external_ball_detector.py"
)

Write-Host "Copying $($filesToCopy.Count) files..."
foreach ($f in $filesToCopy) {
    $src = Join-Path $localProject $f
    $dst = Join-Path $ProjectDir $f
    if (Test-Path $src) {
        Copy-Item -ToSession $session -Path $src -Destination $dst -Force
    } else {
        Write-Host "  SKIP (not found): $f"
    }
}

# Step 3: Create worker config
Write-Host "`n--- Creating worker config ---"
Invoke-Command -Session $session -ScriptBlock {
    param($ProjectDir, $ServerIP)
    $config = @"
# Laptop worker config — jared-laptop
# Connects to server API, pulls work to local SSD, pushes results back.

[worker]
hostname = "jared-laptop"
capabilities = ["tile", "label", "train"]
api_url = "http://${ServerIP}:8643"
server_share = '\\${ServerIP}\training'
local_work_dir = "C:/soccer-cam-label/pipeline_work"
local_models_dir = "C:/soccer-cam-label/models"

[resources]
max_gpu_temp = 85
min_disk_free_gb = 20
gpu_device = 0
idle_games = []

[heartbeat]
interval = 30
"@
    Set-Content -Path "$ProjectDir\worker_config.toml" -Value $config
    Write-Host "Created worker_config.toml"

    # Create work directories
    New-Item -Path "C:\soccer-cam-label\pipeline_work" -ItemType Directory -Force | Out-Null
    New-Item -Path "C:\soccer-cam-label\models" -ItemType Directory -Force | Out-Null
} -ArgumentList $ProjectDir, $ServerIP

# Step 4: Install dependencies
Write-Host "`n--- Installing dependencies ---"
Invoke-Command -Session $session -ScriptBlock {
    param($ProjectDir)
    Set-Location $ProjectDir
    $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
    uv sync --extra dev 2>&1 | Select-Object -Last 5
} -ArgumentList $ProjectDir

# Step 5: Set up share auth
Write-Host "`n--- Share auth ---"
Invoke-Command -Session $session -ScriptBlock {
    param($ServerIP)
    # Check if we already have credentials
    $existing = cmdkey /list 2>&1 | Select-String $ServerIP
    if ($existing) {
        Write-Host "Share credentials already configured"
    } else {
        Write-Host "WARNING: Share credentials not set up."
        Write-Host "You must run this at the laptop keyboard:"
        Write-Host "  cmdkey /add:$ServerIP /user:jared /pass:<password>"
    }
} -ArgumentList $ServerIP

# Step 6: Copy ONNX model
Write-Host "`n--- Copying ONNX model ---"
$modelSrc = "C:\soccer-cam-label\models\model.onnx"
if (Test-Path $modelSrc) {
    Copy-Item -ToSession $session -Path $modelSrc -Destination "C:\soccer-cam-label\models\model.onnx" -Force
    Write-Host "Copied ONNX model"
} else {
    # Try from the laptop's existing location
    Invoke-Command -Session $session -ScriptBlock {
        if (Test-Path "C:\soccer-cam-label\labels_local\model.onnx") {
            Copy-Item "C:\soccer-cam-label\labels_local\model.onnx" "C:\soccer-cam-label\models\model.onnx" -Force
            Write-Host "Copied ONNX model from existing location"
        } elseif (Test-Path "C:\soccer-cam-label\model.onnx") {
            Copy-Item "C:\soccer-cam-label\model.onnx" "C:\soccer-cam-label\models\model.onnx" -Force
            Write-Host "Copied ONNX model from root"
        } else {
            Write-Host "WARNING: ONNX model not found - labeling tasks will fail"
        }
    }
}

Remove-PSSession $session

Write-Host "`n=== Deployment complete ==="
Write-Host "To start the worker, run on the laptop:"
Write-Host "  cd $ProjectDir"
Write-Host "  uv run python -u -m training.worker run --config worker_config.toml"
Write-Host ""
Write-Host "Or use WMI from the server:"
Write-Host "  See deploy script for WMI startup command"
