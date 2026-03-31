# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Soccer-cam (video-grouper) is an automated pipeline for downloading, processing, and uploading soccer game videos from Dahua IP cameras. It runs as a Windows service with a PyQt6 system tray GUI, or via Docker on Linux.

## Task Execution Rules

**Every task must be verified end-to-end before reporting success.**

1. **Define done before starting.** Before launching anything, state what "success" looks like — a specific output, a process running with expected logs, a file that exists with expected content.

2. **Verify each step before proceeding to the next.** Never chain steps on assumption. After step A completes, confirm its output is correct before starting step B. If A produces files, verify they exist and are valid. If A starts a process, confirm it's running AND producing expected output.

3. **Wait for real evidence.** "Launched" is not "running". "Running" is not "producing results". Check actual output — log lines, result files, GPU utilization, process status. If you can't verify within 60 seconds, set up a check and come back.

4. **Never report success prematurely.** Don't say "training started" until you see the first batch processing. Don't say "file transferred" until you verify the size/checksum matches. Don't say "process is working" from a single log line — wait for sustained progress.

5. **When something fails, fix the root cause.** Don't retry blindly. Understand WHY it failed (permissions? corrupt file? wrong path? process died?) and fix the underlying issue. If the same failure pattern has happened before, fix it permanently.

6. **Track what's actually running.** Maintain awareness of every background process. Know their PIDs, what they're doing, and when they last produced output. A process that hasn't logged in 10 minutes may be dead.

## Game Naming Convention

All games use this format everywhere — tiles, labels, shards, manifests, configs:

```
{team}__{date}_vs_{opponent}_{location}
```

Examples:
- `flash__2024.06.01_vs_IYSA_home`
- `heat__2024.05.31_vs_Fairport_home`
- `flash__2024.09.27_vs_RNYFC_Black_home`
- `heat__2024.07.20_Clarence_Tournament`

Rules:
- Team prefix: `flash` or `heat` (lowercase)
- Double underscore `__` separates team from date
- Date format: `YYYY.MM.DD` (sortable, unambiguous)
- Single underscore `_` between words
- Location: `home`, `away`, or omitted for tournaments
- No spaces, no parentheses, no dashes in game IDs
- Tournament names: `{team}__{date}_{Tournament_Name}`

The game registry (`F:/training_data/game_registry.json`) is the source of truth for all game IDs and their video source paths.

## File Organization Rules

**Do not create files without considering where they belong.** Follow these rules:

1. **No one-off scripts.** If you need to run something once, use inline Python via `uv run python -c` or a heredoc. Do not create `.py` or `.bat` files for throwaway tasks.
2. **No test/debug files in the codebase.** Files like `test_share_access.py`, `run_heat2.py`, temp scripts — these go in `/tmp` or are run inline, never committed.
3. **Every new file needs a home.** Before creating a file, identify which directory it belongs in based on the project structure below. If it doesn't fit anywhere, reconsider whether it's needed.
4. **Prefer editing existing files** over creating new ones. A new function in an existing module beats a new module.
5. **Clean up after yourself.** If a file was scaffolding or is superseded, delete it in the same session.

### Project Structure (training/)

```
training/
  annotation/         # Tracking lab, annotation tools
  data_prep/          # Dataset preparation, tiling, frame extraction
  distributed/        # Dask cluster: coordinator, worker, plugins, label_job
  experiments/        # Threshold sweeps, analysis scripts
  inference/          # Detection models (external ball detector, field filter)
  configs/            # YAML configs for training runs
  static/             # Web UI assets
  annotation_server.py  # FastAPI server
```

## Build & Development Commands

Package manager: `uv` (not pip)

```bash
# Install all dependencies (dev + GUI + service)
uv sync --extra dev --extra tray --extra service

# Run the main application
uv run python run.py

# Run the tray GUI
uv run python -m video_grouper.tray

# Run as Windows service
uv run python -m video_grouper.service
```

### Testing

```bash
# All tests
uv run pytest

# Single test file
uv run pytest tests/test_camera_poller.py

# Single test method
uv run pytest tests/test_dahua_camera.py::TestDahuaCameraAvailability::test_check_availability_failure -v

# Unit tests only (skip integration/e2e)
uv run pytest -m "not integration and not e2e"

# Integration tests only
uv run pytest -m "integration"
```

Test markers: `slow`, `integration`, `e2e`. Async tests use `asyncio_mode = "strict"` — always use `@pytest.mark.asyncio` on async test functions.

### Linting & Formatting

```bash
uv run ruff check --fix    # lint + autofix
uv run ruff format          # format
uv run pre-commit run --all-files  # run all pre-commit hooks
```

Ruff config: double quotes, spaces, target Python 3.13.

## Architecture

### Processing Pipeline

The app runs 6 task processors orchestrated by `VideoGrouperApp` (video_grouper/video_grouper_app.py):

```
CameraPoller → DownloadProcessor → VideoProcessor → UploadProcessor
                                        ↑
StateAuditor ──────────────────────── NtfyProcessor
```

**CameraPoller** polls the camera for new .dav files, groups them by timestamp, and queues downloads.
**DownloadProcessor** downloads files from the camera to local storage.
**VideoProcessor** runs FFmpeg to combine .dav→MP4 and trim videos.
**NtfyProcessor** sends push notifications asking the user to identify game start/end times.
**UploadProcessor** uploads finished videos to YouTube.
**StateAuditor** scans all video directories and queues work based on state.json status.

### Two-Tier Processor Pattern

All processors extend one of two base classes in `video_grouper/task_processors/`:

- **PollingProcessor** (`base_polling_processor.py`): Discovery-based. Runs `discover_work()` on an interval. Used by CameraPoller and StateAuditor.
- **QueueProcessor** (`base_queue_processor.py`): Work-processing. Maintains an async queue with JSON-persisted state, deduplication via `get_item_key()`, and max 3 retries. Used by Download, Video, Upload, and Ntfy processors.

### Task System

Tasks are registered in `task_processors/task_registry.py` for serialization/deserialization. Key task types: `CombineTask`, `TrimTask`, `YoutubeUploadTask`, `GameStartTask`, `GameEndTask`, `TeamInfoTask`. All extend `BaseTask`.

### State Management

Each video group directory gets a `state.json` tracking file states: `pending → downloading → downloaded → combined → trimmed → complete`. State is read/written with `FileLock` for concurrency safety (`video_grouper/models/directory_state.py`).

### Configuration

Pydantic models in `video_grouper/utils/config.py`. Loaded from INI file (default: `shared_data/config.ini`). Sections: CAMERA, STORAGE, APP, PROCESSING, LOGGING, TEAMSNAP, PLAYMETRICS, YOUTUBE, NTFY, AUTOCAM.

### External Integrations

- **TeamSnap** (`api_integrations/teamsnap.py`): OAuth 2.0, fetches game schedules
- **PlayMetrics** (`api_integrations/playmetrics.py`): Selenium-based web scraping
- **NTFY** (`api_integrations/ntfy.py`): Push notifications with image attachments
- **YouTube** (`utils/youtube_upload.py`): Google OAuth 2.0 upload
- **Dahua Camera** (`cameras/dahua.py`): HTTP/Digest auth for file listing and download

### Test Conventions

- Global fixtures in `tests/conftest.py`: `mock_ffmpeg`, `mock_file_system`, `mock_httpx`, `temp_storage`, `mock_config`, `cleanup_asyncio_tasks`
- Unit tests mock all I/O; integration tests use temp directories with real processor instances
- Always run from project root for proper path resolution

## Verification

Always verify changes before committing:
1. `uv run ruff check` — must pass with no errors
2. `uv run ruff format --check` — must show no reformatting needed
3. `uv run pytest -m "not integration and not e2e"` — all unit tests must pass

Use `/verify` to run all checks at once.

## Workflow

- Start sessions in Plan mode (Shift+Tab twice) for non-trivial changes
- Use git worktrees for parallel work: `git worktree add ../soccer-cam-feature feature-branch`
- Use `/commit-push` to commit and push in one step
- Use `/verify-app` for thorough end-to-end verification before merging
