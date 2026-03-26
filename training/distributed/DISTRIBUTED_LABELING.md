# Distributed Ball Detection Labeling

Run ball detection labeling across multiple GPUs on different machines. Uses PowerShell remoting for initial setup, then a lightweight FastAPI agent for ongoing control.

## Architecture

```
Main PC (DESKTOP-5L867J8)          Kid's PC (e.g., jared-laptop)
┌─────────────────────┐           ┌──────────────────────┐
│  Coordinator         │   HTTP   │  Label Agent (8650)   │
│  (this machine)      │◄────────►│  - Status reporting   │
│                      │          │  - Copy labels        │
│  F:\training_data\   │   SMB    │  - Run commands       │
│  (source videos +    │◄────────►│  - View logs          │
│   label output)      │          │                       │
└─────────────────────┘           │  Scheduled Task:      │
                                  │  - Labeling job       │
                                  │  (GPU inference)      │
                                  └──────────────────────┘
```

## Prerequisites

- **Main PC**: Windows, PowerShell 5.1+, network share enabled
- **Node PC**: Windows, NVIDIA GPU, network access to main PC
- Both machines on the same network

## Setup Sequence

### Step 1: Enable PowerShell Remoting on the Node

On the node PC (Admin PowerShell):

```powershell
# Set network to Private (required for remoting)
Set-NetConnectionProfile -InterfaceAlias "Ethernet" -NetworkCategory Private
# Or for Wi-Fi:
# Set-NetConnectionProfile -InterfaceAlias "Wi-Fi" -NetworkCategory Private

# Enable remoting
Enable-PSRemoting -Force

# Create a local account for the labeling service
net user training <password> /add
net localgroup Administrators training /add
```

### Step 2: Install Python and Dependencies (via Remoting)

From the main PC:

```powershell
$pass = ConvertTo-SecureString '<password>' -AsPlainText -Force
$cred = New-Object PSCredential('<node-hostname>\training', $pass)
Invoke-Command -ComputerName <node-hostname> -Credential $cred -ScriptBlock {
    # Download and install Python
    $pyInstaller = 'C:\temp\python-installer.exe'
    New-Item -ItemType Directory -Force -Path 'C:\temp' | Out-Null
    Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe' -OutFile $pyInstaller
    Start-Process -Wait -FilePath $pyInstaller -ArgumentList '/quiet', 'InstallAllUsers=1', 'PrependPath=1', 'Include_pip=1'

    # Install packages
    & 'C:\Program Files\Python312\python.exe' -m pip install onnxruntime-directml opencv-python numpy fastapi uvicorn

    # Set power profile (don't sleep when plugged in)
    powercfg /change standby-timeout-ac 0
    powercfg /change monitor-timeout-ac 0
    powercfg /change hibernate-timeout-ac 0
}
```

**GPU Provider Notes:**
- **NVIDIA with CUDA 12.x toolkit installed**: Use `onnxruntime-gpu` (fastest)
- **NVIDIA without CUDA toolkit**: Use `onnxruntime-directml` (works with any NVIDIA GPU)
- **AMD/Intel GPU**: Use `onnxruntime-directml`
- The label_job.py automatically tries CUDA first, then DirectML, then CPU

### Step 3: Deploy Scripts and Model

From the main PC:

```powershell
$session = New-PSSession -ComputerName <node-hostname> -Credential $cred

# Create working directory
Invoke-Command -Session $session -ScriptBlock {
    New-Item -ItemType Directory -Force -Path 'C:\soccer-cam-label\models' | Out-Null
    New-Item -ItemType Directory -Force -Path 'C:\soccer-cam-label\output' | Out-Null
    New-Item -ItemType Directory -Force -Path 'C:\soccer-cam-label\videos' | Out-Null
}

# Push files (use base64 to preserve backslashes in Python strings)
foreach ($file in @('label_job.py', 'label_agent.py')) {
    $bytes = [System.IO.File]::ReadAllBytes("training\distributed\$file")
    $b64 = [Convert]::ToBase64String($bytes)
    Invoke-Command -Session $session -ScriptBlock {
        param($encoded, $name)
        [System.IO.File]::WriteAllBytes("C:\soccer-cam-label\$name", [Convert]::FromBase64String($encoded))
    } -ArgumentList $b64, $file
}

# Copy ONNX model from network share
Copy-Item 'F:\label_node_setup\model.onnx' -Destination 'C:\soccer-cam-label\models\' -ToSession $session

Remove-PSSession $session
```

**Important**: Always use base64 encoding when pushing Python files through PowerShell remoting. `Copy-Item -ToSession` strips backslashes from file content, breaking UNC paths and escape sequences.

### Step 4: Start the Label Agent

From the main PC:

```powershell
Invoke-Command -ComputerName <node-hostname> -Credential $cred -ScriptBlock {
    # Open firewall
    netsh advfirewall firewall add rule name='LabelAgent' dir=in action=allow protocol=tcp localport=8650

    # Register as scheduled task (persists across reboots)
    $action = New-ScheduledTaskAction -Execute 'C:\Program Files\Python312\python.exe' -Argument 'C:\soccer-cam-label\label_agent.py --port 8650'
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 30)
    Register-ScheduledTask -TaskName 'LabelAgent' -Action $action -Settings $settings -User 'training' -Password '<password>' -RunLevel Highest
    Start-ScheduledTask -TaskName 'LabelAgent'
}
```

### Step 5: Verify Agent

From any machine:

```bash
curl http://<node-hostname>:8650/status
```

Expected response:
```json
{
    "hostname": "jared-laptop",
    "work_dir": "C:\\soccer-cam-label",
    "output_games": {},
    "total_labels": 0
}
```

## Running Labeling Jobs

### Copy Videos to the Node

The node needs access to video files. Two options:

**Option A: Network share (recommended for fast networks)**

Map the share from within a PS remoting session using character-by-character UNC path construction (avoids backslash escaping issues):

```powershell
Invoke-Command -ComputerName <node-hostname> -Credential $cred -ScriptBlock {
    $sl = [char]92  # backslash
    $unc = $sl + $sl + '192.168.86.152' + $sl + 'video'
    net use Z: $unc /user:('DESKTOP-5L867J8' + $sl + 'training') <password> /persistent:yes
}
```

**Option B: Local copy (for slow networks)**

Copy videos via robocopy after mapping the share.

### Start a Labeling Job

Create a Python script on the node (push via base64), then run via scheduled task:

```powershell
# Register and run
$action = New-ScheduledTaskAction -Execute 'C:\Program Files\Python312\python.exe' -Argument 'C:\soccer-cam-label\<script>.py'
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 7)
Register-ScheduledTask -TaskName 'BallLabeling' -Action $action -Settings $settings -User 'training' -Password '<password>' -RunLevel Highest
Start-ScheduledTask -TaskName 'BallLabeling'
```

### Copy Results Back

Via the agent HTTP API:

```bash
curl -X POST "http://<node-hostname>:8650/copy-labels"
```

This runs robocopy on the node to push labels to the main PC's share.

## Label Agent API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/status` | GET | Node status: hostname, output games, label counts |
| `/copy-labels` | POST | Robocopy all output labels to the main PC share |
| `/log?lines=20` | GET | Tail of the labeling log |
| `/run` | POST | Run an arbitrary command (for debugging) |

## Labeling Configuration

The `label_job.py` script handles the full pipeline:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--conf` | 0.45 | Detection confidence threshold |
| `--frame-interval` | 4 | Process every Nth frame |
| `--model` | `model.onnx` | ONNX model file |
| `--cpu` | false | Force CPU inference |

Pipeline: detect at full resolution (4096x1800) → field boundary filter → static detection removal → per-tile YOLO labels.

## Troubleshooting

### "System error 67" on net use
The UNC path has single backslashes instead of double. Use the `[char]92` approach to build the path, or verify the Python source file with `repr()` to ensure `\\` is preserved.

### CUDA not available
- Check `nvidia-smi` for GPU presence
- If driver is CUDA 13.x but onnxruntime needs CUDA 12.x, use `onnxruntime-directml` instead
- DirectML works with any NVIDIA/AMD/Intel GPU without CUDA toolkit

### Scheduled task exits immediately
- Check task action: `(Get-ScheduledTask -TaskName 'X').Actions`
- Run the command manually to see errors: `& 'C:\Program Files\Python312\python.exe' 'C:\soccer-cam-label\script.py'`
- Check stderr: redirect output in a wrapper bat file

### Copy-Item strips backslashes
Always use base64 encoding to push files:
```powershell
$bytes = [System.IO.File]::ReadAllBytes('local_file.py')
$b64 = [Convert]::ToBase64String($bytes)
Invoke-Command -Session $session -ScriptBlock {
    param($encoded)
    [System.IO.File]::WriteAllBytes('C:\remote_file.py', [Convert]::FromBase64String($encoded))
} -ArgumentList $b64
```
