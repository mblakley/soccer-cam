# Install pipeline orchestrator and server worker as Windows Scheduled Tasks.
# Run from an elevated (admin) PowerShell prompt.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File training\pipeline\install_service.ps1
#
# To uninstall:
#   Unregister-ScheduledTask -TaskName "PipelineOrchestrator" -Confirm:$false
#   Unregister-ScheduledTask -TaskName "PipelineWorker" -Confirm:$false

$ProjectDir = "C:\Users\jared\projects\soccer-cam-annotation"
$UvPath = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $UvPath) {
    $UvPath = "$env:USERPROFILE\.local\bin\uv.exe"
}

Write-Host "Project dir: $ProjectDir"
Write-Host "uv path: $UvPath"

# --- Orchestrator ---
$OrchestratorAction = New-ScheduledTaskAction `
    -Execute $UvPath `
    -Argument "run python -m training.pipeline run" `
    -WorkingDirectory $ProjectDir

$OrchestratorTrigger = New-ScheduledTaskTrigger -AtStartup
$OrchestratorSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 999 `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Days 365)

# Remove existing task if present
Unregister-ScheduledTask -TaskName "PipelineOrchestrator" -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName "PipelineOrchestrator" `
    -Action $OrchestratorAction `
    -Trigger $OrchestratorTrigger `
    -Settings $OrchestratorSettings `
    -Description "Soccer-cam pipeline orchestrator - populates work queues and monitors health" `
    -RunLevel Highest `
    -User "SYSTEM"

Write-Host "Registered PipelineOrchestrator (runs at startup, auto-restarts)"

# --- Server Worker ---
$WorkerAction = New-ScheduledTaskAction `
    -Execute $UvPath `
    -Argument "run python -m training.worker run --config training\worker\server_worker_config.toml" `
    -WorkingDirectory $ProjectDir

$WorkerTrigger = New-ScheduledTaskTrigger -AtStartup
$WorkerSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 999 `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Days 365)

Unregister-ScheduledTask -TaskName "PipelineWorker" -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName "PipelineWorker" `
    -Action $WorkerAction `
    -Trigger $WorkerTrigger `
    -Settings $WorkerSettings `
    -Description "Soccer-cam pipeline worker - pulls and executes tasks from work queue" `
    -RunLevel Highest `
    -User "SYSTEM"

Write-Host "Registered PipelineWorker (runs at startup, auto-restarts)"

# --- Start both now ---
Write-Host ""
Write-Host "Starting tasks..."
Start-ScheduledTask -TaskName "PipelineOrchestrator"
Start-ScheduledTask -TaskName "PipelineWorker"

Write-Host "Done. Check status with:"
Write-Host "  Get-ScheduledTask -TaskName 'PipelineOrchestrator' | Select-Object State"
Write-Host "  Get-ScheduledTask -TaskName 'PipelineWorker' | Select-Object State"
Write-Host "  uv run python -m training.pipeline status"
