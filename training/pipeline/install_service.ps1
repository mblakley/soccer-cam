# Install pipeline services as Windows Scheduled Tasks.
# Three separate processes: API server, orchestrator, server worker.
# All run as jared (Interactive) for access to claude CLI and user PATH.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File training\pipeline\install_service.ps1

$ProjectDir = "C:\Users\jared\projects\soccer-cam-annotation"
$UvPath = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $UvPath) {
    $UvPath = "$env:USERPROFILE\.local\bin\uv.exe"
}

Write-Host "Project dir: $ProjectDir"
Write-Host "uv path: $UvPath"

Stop-Process -Name python -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

$CommonSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 999 `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Days 365)

$Principal = New-ScheduledTaskPrincipal -UserId "jared" -LogonType Interactive
$Trigger = New-ScheduledTaskTrigger -AtLogon -User "jared"

# --- 1. API Server (must start first -owns the DBs) ---
Unregister-ScheduledTask -TaskName "PipelineAPI" -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName "PipelineAPI" `
    -Action (New-ScheduledTaskAction -Execute $UvPath -Argument "run python -m training.pipeline serve" -WorkingDirectory $ProjectDir) `
    -Trigger $Trigger `
    -Settings $CommonSettings `
    -Principal $Principal `
    -Description "Pipeline API server (port 8643) -sole DB accessor"

Write-Host "Registered PipelineAPI"

# --- 2. Orchestrator (populates queues via API) ---
Unregister-ScheduledTask -TaskName "PipelineOrchestrator" -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName "PipelineOrchestrator" `
    -Action (New-ScheduledTaskAction -Execute $UvPath -Argument "run python -m training.pipeline run" -WorkingDirectory $ProjectDir) `
    -Trigger $Trigger `
    -Settings $CommonSettings `
    -Principal $Principal `
    -Description "Pipeline orchestrator -queue management via API"

Write-Host "Registered PipelineOrchestrator"

# --- 3. Server Worker (pulls tasks via API) ---
Unregister-ScheduledTask -TaskName "PipelineWorker" -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName "PipelineWorker" `
    -Action (New-ScheduledTaskAction -Execute $UvPath -Argument "run python -m training.worker run --config training\worker\server_worker_config.toml" -WorkingDirectory $ProjectDir) `
    -Trigger $Trigger `
    -Settings $CommonSettings `
    -Principal $Principal `
    -Description "Server pipeline worker"

Write-Host "Registered PipelineWorker"

# --- 4. Server QA Worker (Sonnet QA + review, runs alongside tile worker) ---
Unregister-ScheduledTask -TaskName "PipelineQAWorker" -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName "PipelineQAWorker" `
    -Action (New-ScheduledTaskAction -Execute $UvPath -Argument "run python -u -m training.worker run --config training\worker\server_qa_config.toml" -WorkingDirectory $ProjectDir) `
    -Trigger $Trigger `
    -Settings $CommonSettings `
    -Principal $Principal `
    -Description "Server QA worker (sonnet_qa, generate_review, ingest_reviews)"

Write-Host "Registered PipelineQAWorker"

# --- 5. Annotation Server (human review UI, port 8642) ---
Unregister-ScheduledTask -TaskName "AnnotationServer" -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName "AnnotationServer" `
    -Action (New-ScheduledTaskAction -Execute $UvPath -Argument "run uvicorn training.annotation_server:app --host 0.0.0.0 --port 8642" -WorkingDirectory $ProjectDir) `
    -Trigger $Trigger `
    -Settings $CommonSettings `
    -Principal $Principal `
    -Description "Annotation server for human review (port 8642, Tailscale: trainer.goat-rattlesnake.ts.net)"

Write-Host "Registered AnnotationServer"

# Start in order: API first, then others
Start-ScheduledTask -TaskName "PipelineAPI"
Start-Sleep -Seconds 5
Start-ScheduledTask -TaskName "PipelineOrchestrator"
Start-ScheduledTask -TaskName "PipelineWorker"
Start-ScheduledTask -TaskName "PipelineQAWorker"
Start-ScheduledTask -TaskName "AnnotationServer"

Write-Host "`nAll started. Check: curl http://127.0.0.1:8643/api/status"
Write-Host "Annotation server: https://trainer.goat-rattlesnake.ts.net/"
