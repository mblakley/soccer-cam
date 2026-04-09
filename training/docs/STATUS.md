# Current Status

*Last updated: 2026-04-09 11:35*

## Pipeline Architecture (NEW)

The monolithic orchestrator has been replaced with a **pull-based work queue system**:

- **Registry DB**: `D:/training_data/registry.db` (40 games, 28KB, pipeline state per game)
- **Work Queue DB**: `D:/training_data/work_queue.db` (SQLite, atomic claiming, heartbeats)
- **Per-Game Manifests**: `D:/training_data/games/{game_id}/manifest.db` (~20-80MB each)
- **Orchestrator**: `uv run python -m training.pipeline run` (populates queues, monitors health)
- **Workers**: `uv run python -m training.worker run` (pull work, execute, push results)
- **CLI**: `uv run python -m training.pipeline status|games|queue|machines|...`

### Game States (as of migration)

| State | Count | Next Action |
|-------|-------|-------------|
| LABELED | 27 | Sonnet QA → QA_DONE → TRAINABLE |
| TILED | 6 | ONNX labeling (any GPU machine) |
| REGISTERED | 7 | Stage video from F: → D: |

### Queue Status

40 work items enqueued: 6 label (P20), 7 stage (P40), 27 sonnet_qa (P45)

## Running Processes

| Process | Machine | Status | Detail |
|---------|---------|--------|--------|
| Pack job (loose to pack) | Server | Running | Game 13+/23, 8-thread concurrent reads |
| YOLO26l training v3.1 | Laptop RTX 4070 | Epoch 1/50 | 155K tiles, 5.6s/batch, ManifestDataset from packs |
| ONNX labeling | FORTNITE-OP | Paused (kid gaming) | Auto-resume when idle |

## Dataset v3.1 Stats

- 75,598 positives + 79,594 negatives = 155,192 tiles
- 6 train Flash games + 1 val + 2 camera negative games
- 1:1.1 pos:neg ratio, row 0 excluded

## Next Steps

1. Deploy worker configs to laptop and FORTNITE-OP
2. Install orchestrator as Windows Service (nssm) for auto-start
3. Implement flywheel tasks (sonnet_qa, generate_review, ingest_reviews)
4. Move pack files to per-game directories (currently still in tile_packs/)
5. Build v3.2 with expanded data once more games reach TRAINABLE
