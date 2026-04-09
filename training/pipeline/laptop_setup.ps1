# Setup script for laptop worker — run ON the laptop
# Configures share auth and starts the pipeline worker.

# Share auth
cmdkey /add:192.168.86.152 /user:jared /pass:amy4ever

# Test share
if (Test-Path '\\192.168.86.152\training') {
    Write-Host "Share access OK"
} else {
    Write-Host "Share access FAILED"
    exit 1
}

# Test import
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
Set-Location "C:\soccer-cam-label\project"
uv run python -c "from training.worker.worker import PipelineWorker; print('Worker imports OK')"

# Start the worker via WMI (persists after session close)
$ProjectDir = "C:\soccer-cam-label\project"
$UvPath = "$env:USERPROFILE\.local\bin\uv.exe"
$cmd = "$UvPath run python -u -m training.worker run --config worker_config.toml"

$process = ([wmiclass]"\\.\root\cimv2:Win32_Process").Create(
    "cmd /c cd /d $ProjectDir && $cmd > C:\soccer-cam-label\worker.log 2>&1",
    $ProjectDir,
    $null
)
Write-Host "Worker started with PID: $($process.ProcessId)"
Write-Host "Log: C:\soccer-cam-label\worker.log"
