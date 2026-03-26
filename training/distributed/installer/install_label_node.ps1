# install_label_node.ps1 — Run on a kid's PC to set up ball detection labeling
#
# Usage:
#   .\install_label_node.ps1 -VideoSource "\\DESKTOP-5L867J8\video" -Games "flash__09.30.2024_vs_Chili_home,heat__Heat_Tournament"
#
# Prerequisites: Python 3.12+ must be installed, and the PC must have an NVIDIA GPU.

param(
    [string]$VideoSource = "\\DESKTOP-5L867J8\video",
    [string]$Games = "",           # Comma-separated game IDs to process
    [string]$ModelSource = "",     # Path to ONNX model (will be copied to local)
    [float]$Confidence = 0.45,
    [int]$FrameInterval = 4,
    [string]$OutputShare = "\\DESKTOP-5L867J8\video\training_data\labels_640_ext"
)

$installDir = "C:\soccer-cam-label"

Write-Host "=== Ball Detection Label Node Setup ===" -ForegroundColor Cyan

# 1. Create install directory
New-Item -ItemType Directory -Force -Path $installDir | Out-Null
New-Item -ItemType Directory -Force -Path "$installDir\models" | Out-Null
New-Item -ItemType Directory -Force -Path "$installDir\output" | Out-Null

# 2. Install Python packages
Write-Host "Installing Python packages..." -ForegroundColor Yellow
pip install onnxruntime-gpu opencv-python numpy

# 3. Copy the labeling script
Write-Host "Copying labeling script..." -ForegroundColor Yellow
Copy-Item -Path ".\label_job.py" -Destination "$installDir\label_job.py" -Force

# 4. Copy the ONNX model
if ($ModelSource -ne "") {
    Write-Host "Copying ONNX model..." -ForegroundColor Yellow
    Copy-Item -Path $ModelSource -Destination "$installDir\models\model.onnx" -Force
} else {
    Write-Host "WARNING: No model source specified. Copy model.onnx to $installDir\models\ manually." -ForegroundColor Red
}

# 5. Create job configs for each game
$gameMap = @{
    "flash__06.01.2024_vs_IYSA_home" = "Flash_2013s\06.01.2024 - vs IYSA (home)"
    "flash__09.27.2024_vs_RNYFC_Black_home" = "Flash_2013s\09.27.2024 - vs RNYFC Black (home)"
    "flash__09.30.2024_vs_Chili_home" = "Flash_2013s\09.30.2024 - vs Chili (home)"
    "flash__2025.06.02" = "Flash_2013s\2025.06.02-18.16.03"
    "heat__05.31.2024_vs_Fairport_home" = "Heat_2012s\05.31.2024 - vs Fairport (home)"
    "heat__06.20.2024_vs_Chili_away" = "Heat_2012s\06.20.2024 - vs Chili (away)"
    "heat__07.17.2024_vs_Fairport_away" = "Heat_2012s\07.17.2024 - vs Fairport (away)"
    "heat__Clarence_Tournament" = "Heat_2012s\07.20.2024-07.21.2024 - Clarence Tournament"
    "heat__Heat_Tournament" = "Heat_2012s\06.07.2024-06.09.2024 - Heat Tournament"
}

$gamesToProcess = if ($Games -ne "") { $Games.Split(",") } else { $gameMap.Keys }

foreach ($gameId in $gamesToProcess) {
    $gameId = $gameId.Trim()
    if (-not $gameMap.ContainsKey($gameId)) {
        Write-Host "  Unknown game: $gameId" -ForegroundColor Red
        continue
    }

    $videoSubdir = $gameMap[$gameId]
    $videoDir = Join-Path $VideoSource $videoSubdir
    $outputDir = Join-Path $OutputShare $gameId

    $config = @{
        video_dir = $videoDir
        model = "$installDir\models\model.onnx"
        output = "$installDir\output\$gameId"
        conf = $Confidence
        frame_interval = $FrameInterval
        output_share = $outputDir
    }

    $configPath = "$installDir\job_$gameId.json"
    $config | ConvertTo-Json | Set-Content $configPath
    Write-Host "  Created job config: $configPath" -ForegroundColor Green
}

# 6. Create run script
$runScript = @"
@echo off
echo === Ball Detection Labeling ===
cd /d $installDir

for %%f in (job_*.json) do (
    echo Processing %%f...
    python label_job.py --config %%f
    echo Done with %%f
)

echo.
echo === All jobs complete ===
echo Labels are in $installDir\output\
echo.
echo Copy results back to the main PC:
echo   robocopy "$installDir\output" "$OutputShare" /MIR /MT:8
pause
"@

Set-Content -Path "$installDir\run_labeling.bat" -Value $runScript
Write-Host "`n=== Setup Complete ===" -ForegroundColor Cyan
Write-Host "To run labeling: $installDir\run_labeling.bat" -ForegroundColor Green
Write-Host "To copy results back: robocopy `"$installDir\output`" `"$OutputShare`" /MIR /MT:8" -ForegroundColor Green
