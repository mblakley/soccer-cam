# Current Status

*Last updated: 2026-04-09 14:45*

## What's Running Right Now

Three server processes + one remote worker, all communicating via HTTP API:

| Process | Machine | Status | What it does |
|---------|---------|--------|-------------|
| PipelineAPI | Server (port 8643) | Running | FastAPI, sole SQLite accessor |
| PipelineOrchestrator | Server | Running | Populates work queues via API every 60s |
| PipelineWorker | Server | Running | Pulls stage/tile/QA tasks |
| PipelineWorker | FORTNITE-OP | Running | Pulls label/tile tasks, pauses for games |

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

### 1. Get Sonnet QA working (unblocks 25 games)

The `sonnet_qa` task (`training/tasks/sonnet_qa.py`) is fully implemented and queued for all 25 LABELED games. The server worker has `sonnet_qa` in its capabilities. It needs end-to-end testing:

- Task claims a QA job via API
- Pulls pack files + manifest to local SSD
- Extracts tiles with labels but no `qa_verdict`
- Builds 3x2 composite grid images
- Calls `claude` CLI: `claude -p "..." --output-format json <image>`
- Parses BALL/NOT_BALL verdicts
- Writes `qa_verdict` to per-game manifest
- Pushes manifest back to server
- Game advances: LABELED -> QA_PENDING -> QA_DONE

**Test with one game first.** Manually enqueue: `uv run python -m training.pipeline enqueue sonnet_qa --game flash__2024.06.01_vs_IYSA_home --priority 1`

### 2. Generate review packets + NTFY (after QA)

`training/tasks/generate_review.py` packages uncertain tiles (Sonnet disagreements, low confidence) and sends NTFY notification with link to annotation server.

### 3. Ingest human reviews -> TRAINABLE

`training/tasks/ingest_reviews.py` reads verdicts from annotation server, writes to manifest, advances games to TRAINABLE.

### 4. Auto-trigger training

Once 2+ games reach TRAINABLE, orchestrator auto-enqueues `train` task (targeted to laptop). The `training/tasks/train.py` builds training set from per-game manifests + runs YOLO26l.

### 5. Deploy laptop worker

Same pattern as FORTNITE-OP. Files go to `C:\soccer-cam-label\project\`, `cmdkey` for share auth (run once at keyboard), worker talks to API at http://192.168.86.152:8643. Capabilities: train, label, tile.

### 6. Retry 5 failed games

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
