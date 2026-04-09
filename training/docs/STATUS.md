# Current Status

*Last updated: 2026-04-09 18:10*

## What's Running Right Now

Four server processes + one remote worker, all communicating via HTTP API:

| Process | Machine | Status | What it does |
|---------|---------|--------|-------------|
| PipelineAPI | Server (port 8643) | Running | FastAPI, sole SQLite accessor |
| PipelineOrchestrator | Server | Running | Populates work queues via API every 60s |
| PipelineWorker | Server | Running | Pulls stage/tile/QA tasks |
| PipelineWorker | FORTNITE-OP | Running | Pulls label/tile tasks, pauses for games |
| AnnotationServer | Server (port 8642) | Running | Human review UI via Tailscale |
| PipelineWorker | jared-laptop | Running | Tile/label/train tasks |

**Restart all server services:** `powershell -ExecutionPolicy Bypass -File training\pipeline\install_service.ps1`

## Key Paths

| What | Where | Why there |
|------|-------|-----------|
| Registry DB | `G:/pipeline_db/registry.db` | SSD for fast access |
| Work Queue DB | `G:/pipeline_db/work_queue.db` | SSD for fast access |
| Per-game manifests | `D:/training_data/games/{game_id}/manifest.db` | HDD, one per game |
| Pack files (tiles) | `D:/training_data/tile_packs/{game_id}/` | HDD, legacy location |
| Worker SSD cache | `G:/pipeline_work/` | SSD for processing |
| Config | `training/pipeline/config.toml` | All paths/machines/thresholds |
| FORTNITE-OP worker code | `C:\soccer-cam-label\project\` on FORTNITE-OP | Deployed via PS session |
| FORTNITE-OP config | `C:\soccer-cam-label\project\worker_config.toml` | Uses single-quoted UNC paths |

## Game Pipeline State

| State | Count | What happens next |
|-------|-------|-------------------|
| LABELED | 25 | **Blocked on Sonnet QA** - tasks queued, server worker has capability |
| STAGING | 1 | Video verified, waiting for tile task |
| REGISTERED | 1 | Needs staging (video path lookup on F:) |
| EXCLUDED | 7 | 6 futsal + 1 indoor dome, not trainable |
| FAILED:STAGING | 4 | Need retry - were disk-full failures, D: now has 148GB free |
| FAILED:TILED | 1 | Needs retry |

## What Needs to Happen Next (in priority order)

### 1. Sonnet QA flywheel (WORKING)

**Verified working.** 26 sonnet_qa tasks queued at P45, running after tile/label tasks complete.

Sonnet QA now has two phases:
- **Phase 1**: Verify ONNX detections (BALL/NOT_BALL) — 120 tiles per game, ~4 min
- **Phase 2**: Trajectory gap detection — builds trajectories from verified labels, finds gaps, asks Sonnet with filmstrip context (before + gap + after frames), ~3.5 min

Results: `true_positive`, `false_positive`, `gap_ball_found`, `gap_no_ball`

### 2. Pipeline flow: QA_DONE -> generate_review -> TRAINABLE (BUILT)

State machine now: `LABELED -> sonnet_qa -> QA_DONE -> generate_review -> TRAINABLE`

- `generate_review` packages trajectory gaps where Sonnet failed, sends NTFY via Tailscale
- Human review is ASYNC — doesn't block training
- `ingest_reviews` applies human verdicts for next training run
- Human actions: "Ball Here" (tap position), "Out of Play", "Hidden", "Skip"

### 3. Annotation server + Gap Review UI (BUILT)

- Server at port 8642, Tailscale: https://trainer.goat-rattlesnake.ts.net/
- New "Gaps" tab in annotation app with filmstrip view + tap-to-locate
- API endpoints: `/api/gap-reviews`, `/api/gap-reviews/{id}/filmstrip/{stem}`, etc.
- Added to install_service.ps1 as 4th scheduled task

### 4. Deploy annotation server + restart services

Run: `powershell -ExecutionPolicy Bypass -File training\pipeline\install_service.ps1`

### 5. Deploy laptop worker

Same pattern as FORTNITE-OP. Capabilities: train, label, tile.

### 6. Auto-trigger training

Once 2+ games reach TRAINABLE, orchestrator auto-enqueues `train` task.

### 7. Retry 5 failed games

```
uv run python -m training.pipeline retry heat__2024.05.28_vs_Chili_home
uv run python -m training.pipeline retry heat__2024.06.04_vs_Spencerport_home
uv run python -m training.pipeline retry heat__2024.06.25_vs_Pittsford_home
uv run python -m training.pipeline retry heat__2024.06.27_vs_Pittsford_away
uv run python -m training.pipeline retry flash__2025.05.31_vs_IYSA_away
```

## Architecture (for context)

```
Remote Workers                    Server (192.168.86.152)
+-----------+                    +---------------------------+
| Worker    |--- HTTP claim ---->| PipelineAPI (:8643)       |
| (any PC)  |--- HTTP heartbeat->|   WorkQueue (SSD SQLite)  |
|           |--- HTTP complete ->|   GameRegistry (SSD SQLite)|
|           |                    +---------------------------+
|           |--- SMB copy ------>| \\server\training\games\* |
| (local    |    (pack files,    | (bulk files only,         |
|  SSD)     |     video, weights)|  never databases)         |
+-----------+                    +---------------------------+
```

**Key rule:** Only the API process touches SQLite. Everything else uses HTTP. SMB is for bulk file transfer only.

## TOML Config Gotcha

UNC paths with backslashes MUST use single quotes in TOML:
- WRONG: `server_share = "\\192.168.86.152\training"` (TOML interprets `\t` as tab)
- RIGHT: `server_share = '\\192.168.86.152\training'` (literal string)

## Files Created This Session

```
training/pipeline/
  api.py              # FastAPI server (sole DB accessor)
  client.py           # stdlib HTTP client (no deps for remote machines)
  config.toml         # Single config for all paths/machines
  config.py           # Config loader
  queue.py            # SQLite work queue
  registry.py         # Game registry DB
  state_machine.py    # Pipeline state transitions
  migrate.py          # One-time monolithic -> per-game migration (done)
  install_service.ps1 # Register 3 Windows scheduled tasks
  __main__.py         # CLI: serve, run, status, games, queue, retry, skip, enqueue

training/worker/
  worker.py           # Pull-based worker loop (HTTP API client)
  resources.py        # GPU/CPU/disk/idle monitoring
  __main__.py         # Worker CLI
  server_worker_config.toml
  worker_config.toml  # Template for remote machines

training/tasks/
  io.py               # Shared TaskIO for pull-local-process-push
  stage.py            # Verify video exists on F:
  tile.py             # Extract frames -> tile -> pack files
  label.py            # ONNX inference on tiles
  train.py            # Build training set + YOLO training
  sonnet_qa.py        # Claude CLI vision QA
  generate_review.py  # Package uncertain tiles for human review
  ingest_reviews.py   # Collect human verdicts

training/data_prep/
  game_manifest.py    # Per-game SQLite manifest
```
