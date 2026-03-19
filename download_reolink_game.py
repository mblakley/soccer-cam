"""Download the July 22, 2025 soccer game from Reolink camera via RTMP playback."""

import json
import os
import subprocess
import sys
import time
from datetime import datetime


CAMERA_IP = "192.168.86.200"
USERNAME = "admin"
PASSWORD = "mblakley82"
STORAGE_PATH = r"C:\Users\markb\projects\soccer-cam\shared_data"
FFMPEG = r"C:\Users\markb\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin\ffmpeg.exe"

# Session 2 files (the game): 18:08:14 - 19:53:42
# 22 main stream files, ~5 min each, ~450 MB each
GAME_FILES = [
    {
        "start": "20250722T180814",
        "end": "20250722T181313",
        "name": "RecM09_DST20250722_180814_181313.mp4",
    },
    {
        "start": "20250722T181314",
        "end": "20250722T181814",
        "name": "RecM09_DST20250722_181314_181814.mp4",
    },
    {
        "start": "20250722T181815",
        "end": "20250722T182315",
        "name": "RecM09_DST20250722_181815_182315.mp4",
    },
    {
        "start": "20250722T182315",
        "end": "20250722T182814",
        "name": "RecM09_DST20250722_182315_182814.mp4",
    },
    {
        "start": "20250722T182815",
        "end": "20250722T183315",
        "name": "RecM09_DST20250722_182815_183315.mp4",
    },
    {
        "start": "20250722T183315",
        "end": "20250722T183814",
        "name": "RecM09_DST20250722_183315_183814.mp4",
    },
    {
        "start": "20250722T183815",
        "end": "20250722T184315",
        "name": "RecM09_DST20250722_183815_184315.mp4",
    },
    {
        "start": "20250722T184315",
        "end": "20250722T184814",
        "name": "RecM09_DST20250722_184315_184814.mp4",
    },
    {
        "start": "20250722T184815",
        "end": "20250722T185314",
        "name": "RecM09_DST20250722_184815_185314.mp4",
    },
    {
        "start": "20250722T185315",
        "end": "20250722T185814",
        "name": "RecM09_DST20250722_185315_185814.mp4",
    },
    {
        "start": "20250722T185815",
        "end": "20250722T190314",
        "name": "RecM09_DST20250722_185815_190314.mp4",
    },
    {
        "start": "20250722T190315",
        "end": "20250722T190814",
        "name": "RecM09_DST20250722_190315_190814.mp4",
    },
    {
        "start": "20250722T190815",
        "end": "20250722T191313",
        "name": "RecM09_DST20250722_190815_191313.mp4",
    },
    {
        "start": "20250722T191315",
        "end": "20250722T191814",
        "name": "RecM09_DST20250722_191315_191814.mp4",
    },
    {
        "start": "20250722T191815",
        "end": "20250722T192315",
        "name": "RecM09_DST20250722_191815_192315.mp4",
    },
    {
        "start": "20250722T192315",
        "end": "20250722T192814",
        "name": "RecM09_DST20250722_192315_192814.mp4",
    },
    {
        "start": "20250722T192815",
        "end": "20250722T193314",
        "name": "RecM09_DST20250722_192815_193314.mp4",
    },
    {
        "start": "20250722T193315",
        "end": "20250722T193814",
        "name": "RecM09_DST20250722_193315_193814.mp4",
    },
    {
        "start": "20250722T193815",
        "end": "20250722T194314",
        "name": "RecM09_DST20250722_193815_194314.mp4",
    },
    {
        "start": "20250722T194315",
        "end": "20250722T194814",
        "name": "RecM09_DST20250722_194315_194814.mp4",
    },
    {
        "start": "20250722T194815",
        "end": "20250722T195314",
        "name": "RecM09_DST20250722_194815_195314.mp4",
    },
    {
        "start": "20250722T195315",
        "end": "20250722T195342",
        "name": "RecM09_DST20250722_195315_195342.mp4",
    },
]

GROUP_DIR = "2025.07.22-18.08.14"


def parse_time(s):
    """Parse '20250722T180814' to datetime."""
    return datetime.strptime(s, "%Y%m%dT%H%M%S")


def duration_seconds(start_str, end_str):
    """Calculate duration in seconds between two time strings."""
    start = parse_time(start_str)
    end = parse_time(end_str)
    return (end - start).total_seconds()


def download_file(file_info, output_dir, index, total):
    """Download a single file via RTMP playback using FFmpeg 8.1."""
    output_path = os.path.join(output_dir, file_info["name"])
    if os.path.exists(output_path):
        size = os.path.getsize(output_path)
        if size > 1024 * 1024:  # > 1MB, probably already downloaded
            print(
                f"[{index}/{total}] SKIP {file_info['name']} (already exists, {size / 1024 / 1024:.1f} MB)"
            )
            return True

    dur = duration_seconds(file_info["start"], file_info["end"])
    print(f"[{index}/{total}] Downloading {file_info['name']} ({dur:.0f}s segment)...")

    rtmp_url = (
        f"rtmp://{CAMERA_IP}:1935/bcs/channel0_main.bcs?"
        f"channel=0&stream=0"
        f"&starttime={file_info['start']}"
        f"&endtime={file_info['end']}"
        f"&user={USERNAME}&password={PASSWORD}"
    )

    # Add duration limit (+5s buffer) since RTMP playback doesn't send EOF
    cmd = [
        FFMPEG,
        "-y",
        "-i",
        rtmp_url,
        "-t",
        str(int(dur) + 5),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        output_path,
    ]

    start_time = time.time()
    # At ~1.2x realtime, allow up to 2x the duration plus 30s buffer
    timeout_sec = int(dur * 2) + 30
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
    elapsed = time.time() - start_time

    if result.returncode == 0 and os.path.exists(output_path):
        size = os.path.getsize(output_path)
        print(
            f"  Done: {size / 1024 / 1024:.1f} MB in {elapsed:.0f}s ({dur / elapsed:.1f}x realtime)"
        )
        return True
    else:
        print(f"  FAILED (exit code {result.returncode})")
        stderr_last = result.stderr.strip().split("\n")[-3:] if result.stderr else []
        for line in stderr_last:
            print(f"  {line}")
        return False


def create_state_json(output_dir, files_info):
    """Create state.json matching the pipeline's DirectoryState format.

    The pipeline expects: {status, error_message, files: {file_path: RecordingFile.to_dict()}}
    """
    now = datetime.now()
    files_dict = {}
    for f in files_info:
        start_dt = parse_time(f["start"])
        end_dt = parse_time(f["end"])
        local_path = os.path.join(output_dir, f["name"])
        is_downloaded = os.path.exists(local_path) and os.path.getsize(local_path) > 0
        files_dict[local_path] = {
            "task_type": "recording_file",
            "file_path": local_path,
            "start_time": start_dt.isoformat(),
            "end_time": end_dt.isoformat(),
            "status": "downloaded" if is_downloaded else "pending",
            "metadata": {
                "path": f"reolink://{CAMERA_IP}/{f['name']}",
                "startTime": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "endTime": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
            },
            "skip": False,
            "screenshot_path": None,
            "group_dir": output_dir,
            "last_updated": now.isoformat(),
            "error_message": None,
        }

    state = {
        "status": "downloaded",
        "error_message": None,
        "files": files_dict,
    }

    state_path = os.path.join(output_dir, "state.json")
    with open(state_path, "w") as fh:
        json.dump(state, fh, indent=4)
    print(f"\nCreated {state_path}")


def main():
    output_dir = os.path.join(STORAGE_PATH, GROUP_DIR)
    os.makedirs(output_dir, exist_ok=True)
    print(f"Output directory: {output_dir}")
    print(f"Files to download: {len(GAME_FILES)}")
    total_dur = sum(duration_seconds(f["start"], f["end"]) for f in GAME_FILES)
    print(f"Total duration: {total_dur / 60:.1f} minutes")
    print(
        f"Estimated download time: ~{total_dur / 1.2 / 60:.0f} minutes (at ~1.2x realtime)"
    )
    print()

    success = 0
    failed = 0
    overall_start = time.time()

    for i, f in enumerate(GAME_FILES, 1):
        if download_file(f, output_dir, i, len(GAME_FILES)):
            success += 1
        else:
            failed += 1

    overall_elapsed = time.time() - overall_start
    print(
        f"\nDownload complete: {success} succeeded, {failed} failed in {overall_elapsed / 60:.1f} minutes"
    )

    if success > 0:
        create_state_json(output_dir, GAME_FILES)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
