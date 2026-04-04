"""Check if ONNX workers are running. Start them if not and no games are active."""
import os
import subprocess
import sys

import psutil

GAMES = ["FortniteClient", "RobloxPlayer", "RocketLeague"]
PYTHON = r"C:\Python313\python.exe"
NUM_WORKERS = 2


def games_running():
    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info["name"].lower()
            for game in GAMES:
                if game.lower() in name:
                    return proc.info["name"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return None


def workers_running():
    count = 0
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if proc.info["name"] and "python" in proc.info["name"].lower():
                cmd = proc.info.get("cmdline") or []
                if any("run_onnx" in str(c) for c in cmd):
                    count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return count


game = games_running()
if game:
    print(f"Game running: {game}. Not starting workers.")
    sys.exit(0)

n = workers_running()
if n >= NUM_WORKERS:
    print(f"{n} workers already running. OK.")
    sys.exit(0)

print(f"Only {n} workers running, no games. Starting {NUM_WORKERS - n} workers...")

# Figure out which worker IDs are missing
running_ids = set()
for proc in psutil.process_iter(["name", "cmdline"]):
    try:
        if proc.info["name"] and "python" in proc.info["name"].lower():
            cmd = " ".join(proc.info.get("cmdline") or [])
            if "run_onnx_all.py" in cmd:
                # Extract worker ID from command line
                for part in (proc.info.get("cmdline") or []):
                    if part.isdigit():
                        running_ids.add(int(part))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

for wid in range(NUM_WORKERS):
    if wid in running_ids:
        continue
    launcher = os.path.join(r"C:\soccer-cam-label", f"run_onnx_idle_w{wid}.py")
    if not os.path.exists(launcher):
        print(f"Launcher missing: {launcher}")
        continue
    subprocess.Popen([PYTHON, "-u", launcher])
    print(f"Started worker {wid}")
