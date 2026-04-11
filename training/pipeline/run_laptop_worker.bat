@echo off
echo %date% %time% Starting worker >> C:\soccer-cam-label\startup.log

echo Setting up share auth... >> C:\soccer-cam-label\startup.log
net use \\192.168.86.152\training amy4ever /user:jared /persistent:yes >> C:\soccer-cam-label\startup.log 2>&1
echo Net use result: %errorlevel% >> C:\soccer-cam-label\startup.log

echo Testing share... >> C:\soccer-cam-label\startup.log
dir \\192.168.86.152\training >> C:\soccer-cam-label\startup.log 2>&1

echo Starting worker process... >> C:\soccer-cam-label\startup.log
cd /d C:\soccer-cam-label\project
REM Add CUDA 12 DLLs to PATH (from system Python's PyTorch installation)
set PATH=C:\Program Files\Python312\Lib\site-packages\torch\lib;%PATH%
%USERPROFILE%\.local\bin\uv.exe run python -u -m training.worker run --config worker_config.toml >> C:\soccer-cam-label\worker.log 2>&1
