# Current Status

*Last updated: 2026-04-08 20:10*

## Running Processes

| Process | Machine | Status | Detail |
|---------|---------|--------|--------|
| Pack job (loose to pack) | Server | Running | Game 13+/23, 8-thread concurrent reads |
| Orchestrator | Server | Running | Checks every 5 min, manages FORTNITE-OP + laptop |
| YOLO26l training v3.1 | Laptop RTX 4070 | Epoch 1/50 | 155K tiles, 5.6s/batch, ManifestDataset from packs |
| ONNX labeling | FORTNITE-OP | Paused (kid gaming) | Auto-resume when idle, checkpoint resume works |

## Architecture

- Manifest DB: D:/training_data/manifest.db (7.7M tiles, 1M+ labels, pack offsets)
- Pack files: D:/training_data/tile_packs/{game}/{segment}.pack (~756GB, 14 games packed)
- Orchestrator: training/pipeline/orchestrator.py (auto-manages all machines)
- ManifestDataset: training/data_prep/manifest_dataset.py (reads from packs + SQLite)
- Curated training sets: D:/training_data/training_sets/v3.1/ (28GB, archived to F:)
- Label job: training/distributed/label_job.py (idle detection + checkpoint resume)
- Human review: training/pipeline/generate_review.py (trajectory breaks, Sonnet QA filter)

## Dataset v3.1 Stats

- 75,598 positives + 79,594 negatives = 155,192 tiles
- 6 train Flash games + 1 val + 2 camera negative games
- 1:1.1 pos:neg ratio, row 0 excluded

## Next Steps

1. Add Sonnet Vision QA filter for human review candidates
2. Generate review packets and start human review of trajectory breaks
3. Wire review verdicts back into manifest
4. Build v3.2 with corrections + new ONNX labels + Heat games
5. Resume training with expanded data
