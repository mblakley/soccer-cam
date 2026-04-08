"""Cross-machine operations via PowerShell remoting.

All operations write .ps1 scripts and execute via powershell.exe -File.
Never inline PowerShell with UNC paths through bash.

Usage:
    mgr = MachineManager()
    if mgr.is_online("FORTNITE-OP"):
        mgr.stage_video("FORTNITE-OP", "flash__2024.05.01", ["/path/to/seg1.mp4"])
        mgr.start_task("FORTNITE-OP", "RunLabeling")
"""

import json
import logging
import subprocess
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Machine credentials
MACHINES = {
    "FORTNITE-OP": {"user": "training", "password": "amy4ever"},
    "jared-laptop": {"user": "training", "password": "amy4ever"},
}

# Server info
SERVER_IP = "192.168.86.152"
SERVER_SHARES = {
    "training": f"\\\\{SERVER_IP}\\training",  # D:\training_data
    "video": f"\\\\{SERVER_IP}\\video",         # F:\
}


def _run_ps1(script_content: str, timeout: int = 300) -> tuple[int, str]:
    """Write a .ps1 script to temp, execute it, return (exit_code, output)."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".ps1", dir="C:/tmp", delete=False, encoding="utf-8"
    ) as f:
        f.write(script_content)
        ps1_path = f.name

    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1_path],
            capture_output=True, text=True, timeout=timeout,
        )
        output = result.stdout + result.stderr
        return result.returncode, output.strip()
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"
    finally:
        Path(ps1_path).unlink(missing_ok=True)


def _cred_block(hostname: str) -> str:
    """Return PowerShell credential creation for a hostname."""
    creds = MACHINES.get(hostname, MACHINES["FORTNITE-OP"])
    return (
        f'$cred = New-Object PSCredential("{creds["user"]}", '
        f'(ConvertTo-SecureString "{creds["password"]}" -AsPlainText -Force))'
    )


class MachineManager:
    """Manages cross-machine operations for the training pipeline."""

    def is_online(self, hostname: str, timeout: int = 10) -> bool:
        """Check if a machine responds to ping."""
        result = subprocess.run(
            ["ping", "-n", "1", "-w", str(timeout * 1000), hostname],
            capture_output=True, timeout=timeout + 5,
        )
        return result.returncode == 0

    def is_idle(self, hostname: str) -> bool:
        """Check if no games are running on the machine."""
        script = f"""
{_cred_block(hostname)}
Invoke-Command -ComputerName {hostname} -Credential $cred -ScriptBlock {{
    $games = Get-Process | Where-Object {{ $_.Name -match "Fortnite|Roblox|RocketLeague" }}
    if ($games) {{ Write-Output "GAMING" }} else {{ Write-Output "IDLE" }}
}}
"""
        code, output = _run_ps1(script, timeout=15)
        return "IDLE" in output

    def get_gpu_usage(self, hostname: str) -> str:
        """Get GPU utilization string."""
        script = f"""
{_cred_block(hostname)}
Invoke-Command -ComputerName {hostname} -Credential $cred -ScriptBlock {{
    nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader 2>&1
}}
"""
        code, output = _run_ps1(script, timeout=15)
        return output if code == 0 else "unknown"

    def check_task(self, hostname: str, task_name: str) -> str:
        """Check scheduled task state. Returns: Running, Ready, Disabled, or error."""
        script = f"""
{_cred_block(hostname)}
Invoke-Command -ComputerName {hostname} -Credential $cred -ScriptBlock {{
    (Get-ScheduledTask -TaskName "{task_name}" -ErrorAction SilentlyContinue).State
}}
"""
        code, output = _run_ps1(script, timeout=15)
        for state in ("Running", "Ready", "Disabled"):
            if state in output:
                return state
        return f"error: {output[:100]}"

    def start_task(self, hostname: str, task_name: str) -> bool:
        """Start a scheduled task on a remote machine."""
        script = f"""
{_cred_block(hostname)}
Invoke-Command -ComputerName {hostname} -Credential $cred -ScriptBlock {{
    Start-ScheduledTask -TaskName "{task_name}"
    Write-Output "OK"
}}
"""
        code, output = _run_ps1(script, timeout=15)
        return "OK" in output

    def get_log_tail(self, hostname: str, log_path: str, lines: int = 20) -> str:
        """Get the last N lines of a log file on a remote machine."""
        script = f"""
{_cred_block(hostname)}
Invoke-Command -ComputerName {hostname} -Credential $cred -ScriptBlock {{
    Get-Content "{log_path}" -Tail {lines} -ErrorAction SilentlyContinue
}}
"""
        code, output = _run_ps1(script, timeout=15)
        return output

    def stage_video(self, hostname: str, game_id: str, video_paths: list[str]) -> bool:
        """Copy video segment files to a remote machine for labeling."""
        dest_dir = f"\\\\{hostname}\\D$\\labeling\\{game_id}"

        # Build copy commands
        copy_lines = [f'New-Item -ItemType Directory -Path "{dest_dir}" -Force | Out-Null']
        for vp in video_paths:
            copy_lines.append(
                f'Copy-Item -LiteralPath "{vp}" -Destination "{dest_dir}\\" -Force'
            )
        copy_lines.append(f'Write-Output "Staged {len(video_paths)} files"')

        script = "\n".join(copy_lines)
        code, output = _run_ps1(script, timeout=600)
        success = f"Staged {len(video_paths)}" in output
        if success:
            logger.info("Staged %d videos for %s on %s", len(video_paths), game_id, hostname)
        else:
            logger.error("Failed to stage videos: %s", output[:200])
        return success

    def pull_file(self, hostname: str, remote_path: str, local_path: str) -> bool:
        """Copy a file from a remote machine to the server."""
        src = f"\\\\{hostname}\\D$\\{remote_path.lstrip('D:/').lstrip('D:\\\\')}"
        script = f"""
Copy-Item -LiteralPath "{src}" -Destination "{local_path}" -Force
if (Test-Path "{local_path}") {{
    Write-Output "OK"
}} else {{
    Write-Output "FAILED"
}}
"""
        code, output = _run_ps1(script, timeout=300)
        return "OK" in output

    def push_directory(self, hostname: str, local_dir: str, remote_dir: str) -> bool:
        """Copy a directory to a remote machine using robocopy."""
        dest = f"\\\\{hostname}\\D$\\{remote_dir.lstrip('D:/').lstrip('D:\\\\')}"
        script = f"""
robocopy "{local_dir}" "{dest}" /E /Z /J /R:3 /W:5 /NP /NFL /NDL
if ($LASTEXITCODE -le 7) {{ Write-Output "OK" }} else {{ Write-Output "FAILED" }}
"""
        code, output = _run_ps1(script, timeout=3600)
        return "OK" in output

    def remote_exec(self, hostname: str, script_block: str, timeout: int = 30) -> tuple[int, str]:
        """Execute arbitrary PowerShell on a remote machine."""
        script = f"""
{_cred_block(hostname)}
Invoke-Command -ComputerName {hostname} -Credential $cred -ScriptBlock {{
    {script_block}
}}
"""
        return _run_ps1(script, timeout=timeout)

    def send_ntfy(self, message: str, title: str = "Training Pipeline") -> bool:
        """Send a push notification via NTFY."""
        import urllib.request
        try:
            req = urllib.request.Request(
                "https://ntfy.sh/video_grouper_mblakley43431",
                data=message.encode(),
                headers={"Title": title},
            )
            urllib.request.urlopen(req, timeout=10)
            return True
        except Exception as e:
            logger.warning("NTFY failed: %s", e)
            return False
