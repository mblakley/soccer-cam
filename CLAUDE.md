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

The `training/` package contains a ball detection model training pipeline with automated QA. Install with `uv sync --extra ml` plus CUDA PyTorch (`uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124`).

All modules run via `uv run python -m training.<module>` or `uv run python -m training.data_prep.<module>`.

#### Overall Training Loop

```
Video → Extract Frames → Tile → Bootstrap Labels → Clean Labels → Train Model
                                       ↑                               ↓
                           QA Verdicts ← Sonnet Spot-Check ← Detect on Games
                          (+ async human review for disagreements)
```

Each training iteration:
1. **Label** — bootstrap + heuristic filter + trajectory validation + field mask filter
2. **Organize** — `organize_dataset.py --labels labels_640_field_filtered` to update junctions
3. **Sample** — smart sampling with hard negatives, confuser negatives (QA FPs), row 2 oversampling
4. **Train** — YOLO or temporal model on cleaned/sampled data
5. **Spot-check** — Sonnet agents review 10% of model output
5. **Apply verdicts** — three-tier system: automated consensus, Sonnet-only, async human escalation
6. **Repeat** — cleaned labels feed the next training run

#### Data Prep Pipeline (`training/data_prep/`)

1. `extract_frames.py` — Extracts frames from 4096x1800 panoramic video every 8 frames (~3 fps). Supports `.mp4` and `.dav` formats.
2. `tile_frames.py` — Slices panoramic frames into 7x3 grid of 640x640 tiles with overlap (STEP_X=576, STEP_Y=580).
3. `bootstrap_labels.py` — Runs pretrained YOLO (yolo11x.pt) on tiles to auto-label ball positions using COCO sports_ball class. Supports row exclusion.
4. `bootstrap_batch.py` — Runs bootstrap labeling game-by-game to avoid memory issues with large datasets.
5. `organize_dataset.py` — Organizes tiles + labels into YOLO directory structure with game-level train/val split. Supports tile weighting and row exclusion.
6. `create_sample_lists.py` — Creates sampled train.txt/val.txt for memory-safe training with large datasets.
7. `process_batch.py` — Orchestrates steps 1-2 for many videos, with skip/resume support.

#### Label Cleaning Pipeline (`training/data_prep/`)

1. `label_filters.py` — Heuristic pre-filter: aspect ratio, size bounds, edge clipping. Reads `labels_640/`, writes `labels_640_filtered/`.
2. `trajectory_validator.py` — Physics-based cleaning: links detections across consecutive frames in panoramic coords, keeps trajectories ≥3 frames. Reads `labels_640_filtered/`, writes `labels_640_clean/`.
3. `field_mask_filter.py` — Soft field mask filtering using per-game polygons. Far off-field (>150px outside polygon) removed, near off-field (0-150px) kept at 20%, on-field kept in full. Ball legitimately leaves the field during throw-ins and high kicks so this is NOT a hard cutoff.
4. `qa_verdict_ingester.py` — Ingests Sonnet agent QA verdicts into the SQLite label cache. Traces verdicts back to source labels. Exports `verdicts_by_label.json` for downstream use.
5. `smart_sampler.py` — Intelligent sampling: all positives + hard negatives (adjacent tiles) + confuser negatives (QA-identified FPs) + random negatives. Row 2 (sideline) negatives oversampled 2x. Accepts `--confuser-verdicts` for QA-informed sampling.
6. `create_temporal_dataset.py` — Builds 3-frame triplet manifests (.jsonl) for temporal model training.
7. `active_sampler.py` — Prioritized sampling for Sonnet QA spot-checks. Prioritizes: row 2 sideline tiles, near-boundary detections, then random.

#### QA Pipeline (Sonnet Agent Review)

Automated label quality assurance using Claude Sonnet vision agents:

- `label_qa_cache.py` — SQLite cache indexing all ext labels with panoramic coords, field mask status, and QA verdicts. Subcommands: build cache, `apply-field-mask`.
- `label_qa_prep.py` — Creates 3x2 composite grid images (6 tiles each) for batch agent review. Samples positives/negatives, stitches panoramic frames for phase classification.
- `label_qa_report.py` — Aggregates agent verdicts into categorized report (FP by type, position, game; FN rate). JSON + text output.
- `label_qa_spot_check.py` — Post-training spot-check pipeline. Prepares review packets, applies three-tier verdict system (automated consensus / Sonnet-only / async human escalation). Designed for the continuous improvement loop.
- `calibrate_confidence.py` — Computes precision/recall from QA-verified tiles for confidence threshold optimization.

QA database: `F:/training_data/label_qa/tile_cache.db`. Field mask polygons: `F:/training_data/label_qa/{game}/field_mask.json` (generated by Sonnet agents from panoramic images).

#### Temporal Model (`training/`)

- `temporal_dataset.py` — PyTorch Dataset for 3-frame heatmap training. 9-channel input (3 RGB frames) or 11-channel (+ row/col position encoding). Gaussian heatmap target (sigma=2.0).
- `train_temporal.py` — U-Net architecture (TemporalBallNet, ~4M params), weighted focal loss (configurable alpha via `--focal-alpha`). Supports `--position-encoding` for 11-channel input to help suppress sideline FPs.

#### Inference Pipeline (`training/inference/`)

- `panoramic_detector.py` — Full-frame detection: tiles panoramic frame, runs temporal model per tile with row 2 confidence penalty (0.8x), stitches heatmaps, applies soft field mask (off-field gets 0.3x weight, not zeroed), finds top-N peaks (default 3). Supports `--field-mask` and `--max-detections` CLI args.
- `ball_tracker.py` — Kalman filter tracker: state=[x,y,vx,vy,ax,ay]. Out-of-play awareness: records exit point/velocity when ball crosses field boundary, widens gate (800px) for re-entry after punts. On-field gate=150px, min track length=5, min avg confidence=0.4.

#### Training & Evaluation

- `train.py` — YOLO26, workers=0 for 16GB RAM, supports epoch rotation via `--epoch-rotation N`
- `evaluate.py` — Model evaluation with mAP, precision, recall
- `export_mobile.py` — Export to CoreML, TFLite, ONNX for on-device deployment

#### Human-in-the-Loop Annotation

- `annotation_server.py` — FastAPI server on Tailscale for mobile review. Serves crops, collects confirm/reject/adjust/locate actions. Also receives Sonnet QA verdicts for the three-tier review system.
- `review_packet_generator.py` — Selects frames for human review (low confidence, tracker loss, confidence transitions)
- `correction_ingester.py` — Converts mobile annotations back to YOLO training labels

#### Distributed Training

- `distributed/idle_trainer.py` — Background trainer that monitors Windows idle state, starts/stops YOLO training only when machine is idle. Graceful checkpoint saving on user return.

#### Dataset Config

- `training/configs/ball_dataset_640.yaml` — Main config pointing to `F:\training_data\ball_dataset_640` with sampled `train.txt`/`val.txt`
- `training/configs/ball_dataset_640_tiny.yaml` — Smoke test config with `train_tiny.txt`/`val_tiny.txt`

#### Training Data Location

`F:\training_data\` — Top-row (r0) tiles excluded via .excluded rename. Dataset uses NTFS junctions for zero-copy train/val split.

Label directory versions (pipeline flows left to right):
```
labels_640 → labels_640_filtered → labels_640_clean → labels_640_field_filtered
(bootstrap)   (heuristic filter)    (trajectory val)    (field mask + QA verdicts)
```
- `labels_640_ext/` — External model detections from jared-laptop (separate from bootstrap pipeline). QA was performed on these.
- `ball_dataset_640/` — Organized training dataset. `labels/` junctions point to whichever label version is current.
- `label_qa/` — SQLite cache, field mask polygons, composite grids, Sonnet agent results, reports.
- `runs/` — Training run outputs (ball_v1, v2, v3, etc.)

#### Running the Next Training Iteration

```bash
# 1. Apply QA verdicts to labels database
uv run python -m training.data_prep.qa_verdict_ingester
uv run python -m training.data_prep.qa_verdict_ingester --export F:/training_data/label_qa/verdicts_by_label.json

# 2. Filter labels with field masks (soft filter — keeps near-off-field at 20%)
uv run python -m training.data_prep.field_mask_filter

# 3. Re-organize dataset with filtered labels
uv run python -m training.data_prep.organize_dataset --labels F:/training_data/labels_640_field_filtered

# 4. Smart sample with confuser negatives from QA
uv run python -m training.data_prep.smart_sampler --confuser-verdicts F:/training_data/label_qa/verdicts_by_label.json

# 5. Train (YOLO)
uv run python -m training.train --data training/configs/ball_dataset_640.yaml --name ball_v4

# 6. Or train temporal model with position encoding
uv run python -m training.train_temporal --manifest F:/training_data/temporal_triplets.jsonl --position-encoding --focal-alpha 3.0

# 7. After training, run spot-check
uv run python -m training.label_qa_spot_check --run-id ball_v4

# 8. Calibrate confidence threshold
uv run python -m training.calibrate_confidence
```

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
