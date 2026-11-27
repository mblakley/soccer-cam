---
paths:
  - "training/**"
---

# Training pipeline, server storage, and GPU workers

Applies to everything under `training/` and to any work on the GPU server (`DESKTOP-5L867J8`) or
remote workers. If you are doing server/GPU work without having opened a `training/` file, read
this file first.

## Auditing pipeline state

**Audit means validate prerequisites, not just state.** When auditing the pipeline, don't just check game states — verify that each game has the resources its state claims. A game in LABELED state with 0 tiles is broken. Specifically check:
   - **LABELED**: has tiles > 0? has labels > 0? pack_file not NULL? packs exist on disk (D: or F:)?
   - **TILED**: has tiles > 0? pack_file not NULL? packs exist?
   - **QA_DONE**: has qa_verdicts? has game_ball_track metadata?
   - **TRAINABLE**: has labels with matching tiles? packs accessible?
   - **Any state**: pipeline_attempts climbing? (indicates retry loop — fix root cause, don't just reset)
   - If a game's state doesn't match its data, demote it to the correct stage.


## Project Structure (training/)

```
training/
  pipeline/             # Pipeline orchestration (API, queue, state machine, config)
  worker/               # Pull-based workers (server + remote GPU workers)
  tasks/                # Task implementations (stage, tile, label, sonnet_qa, train, etc.)
  data_prep/            # Dataset preparation (manifest, tiling, trajectory analysis)
  annotation/           # Tracking lab, annotation tools
  cli/                  # Standalone command-line entry points (run_ball_detector, ...)
  tools/                # Dev utilities (onnx_watchdog, ...)
  experiments/          # Threshold sweeps, analysis scripts (incl. panoramic_detector)
  static/               # Web UI assets (annotate.html, ball-verify.html)
  annotation_server.py  # FastAPI annotation server (port 8642)
  docs/                 # STATUS.md, DECISIONS.md, EXPERIMENTS.md, GAMES.md, ROADMAP.md
```

## Training Pipeline

### Architecture

The ball detection training pipeline runs across 3 machines coordinated by an HTTP API:

```
Server                              Worker 1                   Worker 2
┌─────────────────────┐          ┌──────────────────┐          ┌──────────────┐
│ PipelineAPI (:8643) │◄─HTTP──► │ PipelineWorker   │          │ PipelineWorker│
│ Orchestrator        │          │ (tile,label,train)│          │ (tile,label)  │
│ PipelineWorker      │          │ GPU (CUDA)       │          │ GPU (CUDA)    │
│ AnnotationServer    │          └──────────────────┘          └──────────────┘
│ (:8642)             │                ▲                              ▲
└─────────────────────┘                │ SMB                          │ SMB
         ▲                             │                              │
    D: ──┤──► \\server\training ───────┴──────────────────────────────┘
    F: ──┤    (manifests + staged packs)
    G: ──┘
```

### Storage Rules

**These rules are critical. Violating them causes disk-full failures and data loss.**

| Drive | Role | What lives here | Who accesses |
|-------|------|-----------------|--------------|
| **F:** (USB, 15TB) | Permanent archive | Pack files, original videos | Server only (local) |
| **D:** (HDD, 1.9TB) | Serving tier | Manifests, staged packs (SMB shared) | All workers via SMB |
| **G:** (SSD, 271GB) | Processing | Temporary work dirs, pipeline DBs | Server only (local) |

**Pack file lifecycle:**
1. **Create:** Tile task extracts frames → packs on G: SSD
2. **Serve:** Push packs to D: (manifest `pack_file` column = D: path)
3. **Archive:** Copy packs to F: (permanent)
4. **Clean:** Delete packs from D: (verified against F: first)
5. **Restore:** When a task needs packs, `server_packs()` auto-restores F: → D:

**Rules:**
- Manifests always store `pack_file` as D: paths (served via SMB)
- Never read directly from F: in task code — always stage to D: or G: first
- Never use C: for temp files — use G:/pipeline_work/test/
- Never open live SQLite DBs directly — copy to G: temp first, or use the API
- Only the PipelineAPI process touches registry.db and work_queue.db
- Remote workers cannot access F: or G: — only D: via SMB

### Pipeline Flow

```
REGISTERED → stage → STAGING → tile → TILED → label → LABELED →
sonnet_qa → QA_DONE → generate_review → TRAINABLE → train
```

**CRITICAL: Every task MUST follow pull-local-process-push.** Never read packs or manifests directly from SMB shares — random I/O over SMB is 700x slower than local SSD. Stage everything locally first.

1. **Pull:** Copy manifest + packs from D: (SMB) to local SSD work dir
2. **Process:** Read/write ONLY from local SSD paths — never from `\\server\training\...`
3. **Push:** Copy results back to D: (manifest) and F: (packs)
4. **Cleanup:** Delete local work dir

Use `TaskIO` (training/tasks/io.py) which implements this pattern. If a task needs pack data, call `io.pull_packs()` or stage individual packs with `shutil.copy2()` to local SSD before reading tiles.

### Pipeline Commands

```bash
# Status and monitoring
uv run python -m training.pipeline status
uv run python -m training.pipeline games
uv run python -m training.pipeline events --hours 6
uv run python -m training.pipeline queue

# Training progress — read results.csv from the active run
# Results live at: training/runs/{run_name}/results.csv (local)
#   or: D:/training_data/training_sets/{version}/results.csv (server)
# Columns: epoch, time, train/box_loss, train/cls_loss, train/dfl_loss,
#   metrics/precision(B), metrics/recall(B), metrics/mAP50(B), metrics/mAP50-95(B),
#   val/box_loss, val/cls_loss, val/dfl_loss, lr/pg0-pg7
#
# Quick check latest epoch:
tail -1 training/runs/*/results.csv
# Or on the server:
tail -1 D:/training_data/training_sets/*/results.csv
# Best weights: training/runs/{run_name}/weights/best.pt
# Archived checkpoints: F:/training_checkpoints/{version}/best.pt

# Task management
uv run python -m training.pipeline enqueue tile --game GAME_ID --priority 30
uv run python -m training.pipeline enqueue label --game GAME_ID --priority 20
uv run python -m training.pipeline priority ITEM_ID NEW_PRIORITY
uv run python -m training.pipeline delete ITEM_ID

# Service management
powershell -ExecutionPolicy Bypass -File training\pipeline\install_service.ps1
```

### Machines

Configure machines in `training/pipeline/config.toml` and `training/worker/worker_config.toml`. Each worker needs a GPU with CUDA support for labeling and training tasks. The server runs the API, orchestrator, and non-GPU tasks.

### Deploying Remote Workers

**There is ONE canonical way to deploy a remote worker.** Do not create ad-hoc bat files, scheduled tasks, or startup scripts. Use the deploy script:

```powershell
# From the server, in the project root:
powershell -ExecutionPolicy Bypass -File training\worker\deploy_worker.ps1 -Machine worker1
```

**What it does** (6 steps):
1. Creates directories on remote (`C:\soccer-cam-label\{project,work,models,logs}`)
2. Syncs all `training/` Python code via PS session
3. Generates `worker_config.toml` + `start_pipeline_worker.bat` (the only bat file)
4. Copies ONNX model if missing
5. Cleans up ALL old scheduled tasks, registers one `PipelineWorker` task with user credentials
6. Starts the worker and verifies it's running

**Prerequisites on the remote machine:**
- Python 3.13 installed at `C:\Python313\` with torch+CUDA, opencv, numpy, onnxruntime
- `httpx` and `psutil` pip packages (deploy script auto-installs these)
- PS remoting enabled (`Enable-PSRemoting -Force`)
- The `training` user account exists

**If a worker stops running**, re-run the deploy script. It's idempotent.

**Never:**
- Create bat files by hand on remote machines
- Register scheduled tasks manually
- Edit `worker_config.toml` on the remote — re-run the deploy script instead

