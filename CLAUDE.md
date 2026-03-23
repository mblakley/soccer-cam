# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Soccer-cam (video-grouper) is an automated pipeline for downloading, processing, and uploading soccer game videos from Dahua IP cameras. It runs as a Windows service with a PyQt6 system tray GUI, or via Docker on Linux.

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

### Training Pipeline (Ball Detection)

The `training/` package contains a YOLO ball detection model training pipeline. Install with `uv sync --extra ml` plus CUDA PyTorch (`uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124`).

**Data prep pipeline** (`training/data_prep/`):
1. `extract_frames.py` — Extracts frames from 4096x1800 panoramic video every 8 frames (~3 fps). Supports `.mp4` and `.dav` formats.
2. `tile_frames.py` — Slices panoramic frames into 7x3 grid of 640x640 tiles with overlap.
3. `bootstrap_labels.py` — Runs pretrained YOLO (yolo11x.pt) on tiles to auto-label ball positions using COCO sports_ball class. Supports row exclusion.
4. `bootstrap_batch.py` — Runs bootstrap labeling game-by-game to avoid memory issues with large datasets.
5. `organize_dataset.py` — Organizes tiles + labels into YOLO directory structure with game-level train/val split. Supports tile weighting and row exclusion.
6. `create_sample_lists.py` — Creates sampled train.txt/val.txt for memory-safe training with large datasets.
7. `process_batch.py` — Orchestrates steps 1-2 for many videos, with skip/resume support.

All modules run via `uv run python -m training.data_prep.<module>`.

**Label cleaning pipeline** (`training/data_prep/`):
1. `label_filters.py` — Heuristic pre-filter: aspect ratio, size bounds, edge clipping. Reads `labels_640/`, writes `labels_640_filtered/`.
2. `trajectory_validator.py` — Physics-based cleaning: links detections across consecutive frames in panoramic coords, keeps trajectories ≥3 frames. Reads `labels_640_filtered/`, writes `labels_640_clean/`.
3. `smart_sampler.py` — Intelligent sampling: all positives + hard negatives (adjacent tiles) + random negatives. Writes `train.txt`/`val.txt`.
4. `create_temporal_dataset.py` — Builds 3-frame triplet manifests (.jsonl) for temporal model training.

**Temporal model** (`training/`):
- `temporal_dataset.py` — PyTorch Dataset for 3-frame heatmap training (9-channel input → Gaussian heatmap target).
- `train_temporal.py` — Training script with U-Net architecture (TemporalBallNet, ~4M params), weighted focal loss.

**Inference pipeline** (`training/inference/`):
- `panoramic_detector.py` — Full-frame detection: tiles panoramic frame, runs temporal model per tile, stitches heatmaps, finds peaks.
- `ball_tracker.py` — Kalman filter tracker: links detections into trajectories, predicts during occlusion, state=[x,y,vx,vy,ax,ay].

**Training & evaluation**: `training/train.py` (YOLO26, workers=0 for 16GB RAM, supports epoch rotation via `--epoch-rotation N`), `training/evaluate.py`, `training/export_mobile.py`

**Human-in-the-loop annotation**: `training/annotation_server.py` (FastAPI), `training/review_packet_generator.py`, `training/correction_ingester.py`

**Dataset config**: `training/configs/ball_dataset_640.yaml` — points to `F:\training_data\ball_dataset_640`

**Training data location**: `F:\training_data\` (frames, tiles_640, labels_640, ball_dataset_640, runs). Top-row (r0) tiles excluded via .excluded rename. Dataset uses NTFS junctions for zero-copy train/val split.

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
