# Current Status

*Last updated: 2026-04-15*

## What's Running

Five processes on the server, two remote workers:

| Process | Machine | Port | What it does |
|---------|---------|------|-------------|
| PipelineAPI | Server | 8643 | FastAPI, sole SQLite accessor for registry + work queue |
| PipelineOrchestrator | Server | — | Populates work queues via API every 60s |
| PipelineWorker | Server | — | Pulls stage/tile/QA/review tasks |
| AnnotationServer | Server | 8642 | Human review UI (Tailscale: trainer.goat-rattlesnake.ts.net) |
| PipelineWorker | jared-laptop | — | Tile/label/train tasks (RTX 4070, CUDA) |
| PipelineWorker | FORTNITE-OP | — | Label/tile tasks (RTX 3060 Ti), yields for games |

**Restart server services:** `powershell -ExecutionPolicy Bypass -File training\pipeline\install_service.ps1`
**Deploy remote worker:** `powershell -ExecutionPolicy Bypass -File training\worker\deploy_worker.ps1 -Machine laptop|fortnite`

## Storage Architecture

```
F: (USB, 7.4TB free)     — PERMANENT storage
  /training_data/tile_packs/{game_id}/*.pack   — archived pack files
  /Flash_2013s/...         — original video files
  /Heat_2012s/...          — original video files

D: (HDD, ~1.8TB free)    — SERVING storage (SMB shared to remote workers)
  /training_data/games/{game_id}/manifest.db   — per-game manifests (~50-200MB each)
  /training_data/games/{game_id}/tile_packs/   — temporary pack staging (restored from F: on demand)
  /training_data/review_packets/               — human review packets
  /training_data/deploy/                       — remote worker deployment files

G: (SSD, 141GB)           — PROCESSING storage (local to server)
  /pipeline_db/registry.db                     — game registry
  /pipeline_db/work_queue.db                   — work queue
  /pipeline_work/{game_id}/                    — temporary work dirs (cleaned after each task)
```

### Pack File Lifecycle

```
TILE:    video on F: → extract to G: SSD → push packs to D: → archive to F: → clean D:
LABEL:   manifest says D: path → server_packs() restores F:→D: if missing → pull to G: SSD → ONNX
QA:      same as label (restore + pull)
TRAIN:   _resolve_pack_path() checks D: then F: → stages to local SSD → extract tiles → train
```

**Rule:** Manifests store pack_file paths as D: paths. Packs live permanently on F:. When a task needs packs, they're auto-restored from F: to D:, then cleaned after use. Remote workers access D: via SMB share `\\192.168.86.152\training`.

## Pipeline States

```
REGISTERED → STAGING → TILED → LABELING → LABELED →
QA_PENDING → QA_DONE → generate_review → TRAINABLE
```

- `FAILED:{stage}` — failed at a stage, reset with `reset-attempts` + state change
- `EXCLUDED` — not trainable (futsal, indoor)

## Game Pipeline Status

| State | Count | Notes |
|-------|-------|-------|
| LABELED | ~17 | Have labels, need tiling to complete |
| TILED | ~3 | Need ONNX labeling |
| STAGING | ~3 | Video path verified, need tiling |
| QA_DONE | 1 | Kenmore — ready for review |
| EXCLUDED | 7 | Futsal/indoor games |

~21 games need tiling to complete (server worker processing, ~1hr/game).

## What Needs to Happen

### Active (running now)
1. **D:→F: pack archive** — moving existing packs to F:, freeing D: (~975GB)
2. **Tiling** — server worker processing 21 games from F: video files
3. **Labeling** — laptop running ONNX on tiled games (CUDA via torch/lib PATH)

### After tiling/labeling completes
4. **Sonnet QA** — auto-enqueued for LABELED games (~18min/game)
5. **Game ball track confirmation** — human reviews filmstrip in annotation app
6. **Training** — auto-triggers when 2+ games reach TRAINABLE

### Known issues
- FORTNITE-OP needs redeployment when it comes online: `deploy_worker.ps1 -Machine fortnite`
- Phase 2 trajectory gap detection needs end-to-end test with properly tiled+labeled game
- 4 games had corrupt/missing packs (0-byte on F:), reset to REGISTERED on 2026-04-15:
  `flash__2024.05.01_vs_RNYFC_away`, `flash__2024.05.10_vs_NY_Rush_away`,
  `flash__2024.06.01_vs_IYSA_home`, `flash__2024.06.02_vs_Flash_2014s_scrimmage`

## Key Commands

```bash
# Pipeline status
uv run python -m training.pipeline status

# Game list
uv run python -m training.pipeline games

# Event log (last 6 hours)
uv run python -m training.pipeline events

# Queue management
uv run python -m training.pipeline enqueue tile --game GAME_ID --priority 30
uv run python -m training.pipeline priority ITEM_ID PRIORITY
uv run python -m training.pipeline delete ITEM_ID

# Reset failed games
# Via API: POST /api/game/{game_id}/reset-attempts + POST /api/game/{game_id}/state

# Audit all games
uv run python G:/pipeline_work/audit_all_games.py
uv run python G:/pipeline_work/audit_all_games.py --fix
```
