# Current Status

*Last updated: 2026-04-09 14:35*

## Pipeline Architecture

Three server processes + remote workers, all communicating via HTTP API:

```
PipelineAPI (port 8643)       — FastAPI, sole SQLite accessor
PipelineOrchestrator          — populates queues via API every 60s
PipelineWorker (server)       — pulls stage/tile/QA tasks via API
FORTNITE-OP worker (remote)   — pulls label/tile tasks via API, pauses for games
```

**Key paths:**
- Registry DB: `G:/pipeline_db/registry.db` (SSD, 39 games)
- Work Queue DB: `G:/pipeline_db/work_queue.db` (SSD)
- Per-Game Manifests: `D:/training_data/games/{game_id}/manifest.db`
- Pack files: `D:/training_data/tile_packs/{game_id}/` (legacy location, ~756GB)
- Config: `training/pipeline/config.toml`

**Services (Windows Scheduled Tasks, run as jared Interactive):**
- `PipelineAPI` — `uv run python -m training.pipeline serve`
- `PipelineOrchestrator` — `uv run python -m training.pipeline run`
- `PipelineWorker` — `uv run python -m training.worker run --config training/worker/server_worker_config.toml`
- FORTNITE-OP: `PipelineWorker` scheduled task under jared, talks to API at http://192.168.86.152:8643

**Install/restart:** `powershell -ExecutionPolicy Bypass -File training\pipeline\install_service.ps1`

## Game States

| State | Count | Next Action |
|-------|-------|-------------|
| LABELED | 25 | Sonnet QA (queued, server worker handles) |
| STAGING | 1 | Tile (video verified, ready for tiling) |
| REGISTERED | 1 | Stage (verify video on F:) |
| EXCLUDED | 7 | 6 futsal + 1 indoor dome |
| FAILED:STAGING | 4 | Need retry (were disk-full failures, D: now has 148GB free) |
| FAILED:TILED | 1 | Need retry |

## What's Working

- API server responds instantly (DBs on SSD)
- FORTNITE-OP worker connected via HTTP, claiming tasks, idle detection active
- Server worker tiling games (pulling video from F:, processing on G: SSD, pushing packs to D:)
- Orchestrator enqueuing work, advancing game states
- NTFY notifications for failures/milestones
- All game IDs consistent (no more camera__ prefix, futsal tracked via game_type field)

## What Needs Work Next (Flywheel Priority)

### 1. Sonnet QA — 25 games blocked
The sonnet_qa task exists (`training/tasks/sonnet_qa.py`) and is queued for all 25 LABELED games. The server worker has `sonnet_qa` in its capabilities. It needs:
- Verify the `claude` CLI call works from the worker (PATH is set since running as jared)
- Test one game end-to-end: claim QA task, build composite grids, call claude, parse BALL/NOT_BALL, write verdicts
- May need to adjust rate limiting (100 batches/hr in config)

### 2. Generate review packets + NTFY
After QA, uncertain tiles need human review. The `generate_review` task packages them and sends NTFY notification. Needs testing.

### 3. Ingest reviews → TRAINABLE
After human reviews, `ingest_reviews` writes verdicts back and games advance to TRAINABLE. Needs testing.

### 4. Training trigger
Once enough games reach TRAINABLE (min 2 per config), orchestrator auto-enqueues training. The `train` task builds a training set from per-game manifests and runs YOLO26l. Laptop worker (not yet deployed) would handle this.

### 5. Deploy laptop worker
Same approach as FORTNITE-OP: push worker files via PS session, `cmdkey` for share auth, HTTP API for queue. Capabilities: train, label, tile.

### 6. Retry failed games
4 FAILED:STAGING + 1 FAILED:TILED from earlier disk-full era. D: now has 148GB free. Just need `uv run python -m training.pipeline retry <game_id>` for each.

### 7. Duplicate worker_status rows
The orchestrator creates a second DESKTOP-5L867J8 row each session. The `worker_status` table primary key constraint should prevent this but something's off. Minor — clean up and investigate.

## Dataset v3.1 Stats (prior training run)

- 75,598 positives + 79,594 negatives = 155,192 tiles
- 6 train Flash games + 1 val + 2 camera negative games
- Training was on laptop RTX 4070 (YOLO26l, epoch 1/50 when paused)

## Architecture Decisions (this session)

- **HTTP API for all DB access** — SQLite over SMB doesn't work, only API process touches DBs
- **DBs on SSD (G:)** — eliminates API contention from HDD random I/O
- **SMB for bulk file transfer only** — pack files, video, weights
- **Per-game manifests** — replaced 2.6GB monolithic manifest.db
- **Pull-based workers** — claim from queue via HTTP, not pushed by orchestrator
- **`cmdkey`** stores share credentials on remote machines (run once in jared's console)
- **3 separate processes** on server — API, orchestrator, worker (avoids SQLite threading issues)
