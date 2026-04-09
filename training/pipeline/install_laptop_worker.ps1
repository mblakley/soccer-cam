# Install pipeline worker on jared-laptop as a scheduled task.
# Runs with stored password so it works without interactive login.

$ProjectDir = "C:\soccer-cam-label\project"

Stop-Process -Name python -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Unregister-ScheduledTask -TaskName "PipelineWorker" -Confirm:$false -ErrorAction SilentlyContinue

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 999 `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Days 365)

$Action = New-ScheduledTaskAction `
    -Execute "$ProjectDir\run_worker.bat" `
    -WorkingDirectory $ProjectDir

$Trigger = New-ScheduledTaskTrigger -AtLogon -User "training"

Register-ScheduledTask `
    -TaskName "PipelineWorker" `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -User "training" `
    -Password "amy4ever" `
    -RunLevel Highest `
    -Description "Pipeline worker (tile, label, train)"

Write-Host "Registered PipelineWorker"

Remove-Item "C:\soccer-cam-label\startup.log" -Force -ErrorAction SilentlyContinue
Remove-Item "C:\soccer-cam-label\worker.log" -Force -ErrorAction SilentlyContinue

Start-ScheduledTask -TaskName "PipelineWorker"
Write-Host "Started PipelineWorker"

Start-Sleep -Seconds 10

$task = Get-ScheduledTask -TaskName "PipelineWorker"
Write-Host "State: $($task.State)"
if (Test-Path "C:\soccer-cam-label\startup.log") {
    Get-Content "C:\soccer-cam-label\startup.log"
}
if (Test-Path "C:\soccer-cam-label\worker.log") {
    Get-Content "C:\soccer-cam-label\worker.log" -Tail 5
}
