# Current Status

*Last updated: 2026-06-15*

## Active focus: v4 ball detector (perspective-normalized, full-frame, no tiles)

**v4 is ADDITIVE — it does not replace v3.** The tile-based **v3** detector lineage
(`train_v3.py`, `training/train.py` + its `V3_*` config, the shared `manifest.py` knobs, and
their tests) is **fully maintained and unchanged**. v4 is the new perspective-normalized,
warped-full-frame strategy, added as new files only (`train_v4.py`, `data_prep/warped_pack.py`,
`experiments/io_benchmark.py`, `tests/test_warped_pack.py`). See DECISIONS.md (2026-06-15).

Branch: `feat/perspective-normalized-detector`. The full design + experiment findings are in
**`training/docs/PERSPECTIVE_NORMALIZED_DETECTOR.md`** (read it first — source of truth for the v4
architecture, warp levers, labeling plan, I/O design). This STATUS is the launchpad.

### CURRENT (2026-06-15 — HEATMAP pivot; supersedes the YOLO/I-O-gate plan below)
The bbox/YOLO approach FAILED the first honest eval — **12% far-recall vs AutoCam's 74%**
(center-distance vs Mark's human ground truth): a 3-8px ball is at/below the detector stride and
IoU-mAP is meaningless. **v4 is now a ball-center HEATMAP + multi-frame detector** (see DECISIONS
2026-06-15; external survey + the bbox & pretrained-zero-shot baselines archived on
`F:\archive\v4_detector\`). Runtime + training pipeline =
**dewarp (native-res field-band crop) → polygon-mask (human-verified polygon, far margin) →
3 consecutive grayscale frames → compact U-Net → center heatmap → peak.**
- Built + **smoke-verified end-to-end**: `training/models/heatmap_net.py`,
  `training/data_prep/heatmap_dataset.py`, `training/train_v4_heatmap.py`, `tests/test_heatmap.py`.
  Eval = center-distance far-recall vs AutoCam 74% (Irondequoit held out).
- **Blocked on human far-ball labels.** Far-label tool (annotation server `:8650`, Tailscale
  `trainer.goat-rattlesnake.ts.net`) was rebuilt **gap-centric** (conf≥0.5 trajectory → velocity-
  extrapolated "ball went far and got lost" frames): sets `heat_0527_segA/b/c/d` (~362 gap frames)
  await labels; `irondequoit` (162) = eval GT. Tool: pre-seeds AutoCam, `F` jumps gap-to-gap, arrow
  marker, full-height strips.
- Field polygons human-edited via the unified, **resolution-aware** `annotate.html` field editor
  (`/api/field-boundary` now serves the 7680×2160 v4 clips from `D:/training_data/v4_fields`).
- Edge budget: 90 min @ 20 fps in <24 h = 1.25 fps; the heatmap net runs far faster on CPU
  (ONNX/CoreML/TFLite). Optimize for accuracy, not speed.
- **Next:** Mark labels the gap frames → rebuild crops on real labels → train (the GPU venv with
  CUDA-ORT+torch+ultralytics+av is `G:\pipeline_work\fk\.venv`, needs `torch\lib` on PATH) → eval
  vs 74% → iterate (more games, motion-attention, native-resolution tuning).
- The YOLO / I/O-benchmark / warped-shard plan below is **superseded** (kept for history).

### v4 session progress (2026-06-15)
- **I/O benchmark gate built** (the prerequisite — no long run before it passes):
  `data_prep/warped_pack.py` (pre-decoded warped-frame shards: writer/reader + torch Dataset +
  `ShardRotator`, two storage modes raw-memmap vs compressed) + `experiments/io_benchmark.py`
  (nvidia-smi sampler; data-only/compute-only/end-to-end throughput; bottleneck + ms/iter +
  time/epoch + 4070 extrapolation; sweeps `target_width`×workers×prefetch×storage). 11 unit tests.
- **`train_v4.py` scaffold**: warped entry, persistent workers (the `workers=0` fix), v4 config.
  Not run until the warped dataset writer (`data_prep/warped_dataset.py`) lands.
- **`target_width` is a swept speed/accuracy knob** (DECISIONS 2026-06-15): the 1280 warp default
  crushes far balls below AutoCam's resolution — sweep {3264,5120,7680}, pick the lowest that beats
  AutoCam on far balls; match train+infer resolution.
- **Benchmark sources confirmed** on F: — Reolink `heat__2026.05.27_vs_Chili_Vortex_away` (20 segs)
  + Dahua `flash__2024.05.01_vs_RNYFC_away`. Registry: **23 trainable Reolink games, all
  `labels=False`** (the labeling gate), + 42 dahua_segments + 8 dav_only.
- **Hardware plan**: diagnose bottlenecks/timing on the server GTX 1060, then fan training-config
  experiments across all 3 GPUs (server + jared-laptop RTX 4070 + FORTNITE-OP RTX 3060 Ti) via the
  pull-based work queue. Remote workers see only D: via SMB → serve shards from D:, stage to local SSD.
- **Next:** run the gate on the server, report GPU util.

### Done (field-outline filter — the prerequisite for v3)
The in-house **field_outline v2** keypoint model (ResNet18, 10 kpts, resolution-agnostic,
distilled from the reference keypoint model) is trained, validated, exported, published,
and registered:
- Trained on the full distilled corpus: **53 games (dahua 1092 + reolink 592 + other 41
  frames), 1725 trainable frames**, orientation read from the game registry (not detected).
  Test split 17.1px / 0.814 IoU; Reolink ≈ Dahua → no dilution from joint training.
- Exported + parity-checked vs teacher (`training/cli/export_field_outline.py --check`).
- Published as a FREE TTT model: GitHub release `field-outline-v2.0.0` on
  `mblakley/soccer-cam`, encrypted asset `field_outline-2.0.0.enc`
  (sha256 `058b287a8cf1786e87d7a3be3902ff0981f67049909ebf786f1d1d7fb10b167f`).
- Registered in TTT as seed data (`core.model_versions`, channel=stable, tier=free,
  master_key_id=mk_2026_06) — TTT PR #48 → `development` → applies to preview Supabase.
- **This is the field filter v3 must always use — never the full-frame fallback.** Source:
  `video_grouper/inference/field_detector.py`.

### Next: v3 ball detector — ordered plan (see PERSPECTIVE_NORMALIZED_DETECTOR.md §Rollout)
1. **I/O benchmark gate FIRST** — prove the GPU stays fed (>80% util) at an SSD-bounded
   working-set size before any long run. Our prior trainings were starved (GPU 0%): root
   cause was decode of oversized JPEGs + non-persistent DataLoader workers, NOT F: vs G:.
   Design: sequential shards (not random small files) + pre-decoded memmap packs as a
   bounded rolling working set on G: + double-buffer/prefetch next shard while training +
   persistent_workers/pin_memory/prefetch. The full corpus is 15 TB on F:; G: SSD is
   ~271 GB — it does NOT fit, so streaming is mandatory. No blind multi-hour runs.
2. **Reolink labeling loop (the gate — Reolink has ZERO ball labels):**
   a. Run the **reference ball detector** on each Reolink game → raw per-frame detections =
      baseline labels (it nails easy/near balls). Entry point `training/cli/run_ball_detector.py`
      (`--video --model --output [--labels-dir --segment-name]`). RE-adjacent: it runs in the
      F:/storage workspace, never the repo; only ball coordinates feed training. Decrypted
      reference ball ONNX: `\\DESKTOP-5L867J8\video\test\onnx_models\decrypted\` (= F:\test\...);
      `balldet_fp16_dec.onnx` also staged at `D:\detect_work`.
   b. Track + **far-ball mine** the velocity-gap heuristic
      (`training/data_prep/far_ball_miner.py` → `mine_far_ball_gaps` → `candidates_to_queue`
      → `write_queue_json`, queue compatible with `flywheel/priority_queue.py`). Validated on
      05-27: 173 far-moving-then-lost gaps ≈ 17.7 min/game of far-ball footage.
   c. **Web helper** presents the prioritized far-gap queue on the **warped** frames (far
      balls are bigger + uniform there) → human labels only the far balls the reference missed.
3. **Dahua labels:** reuse existing human-verified labels, map into warped coords via
   `field_warp.warp_points` — no re-labeling.
4. **ONE joint, camera-balanced training run** over all games (Dahua + Reolink) — NOT
   pretrain→fine-tune. The warp normalizes geometry; `compute_camera_weights` balances the
   2:1 game-count skew so Reolink (production) dominates the far-field gradient. v3 dataset
   knobs already landed in `training/data_prep/manifest.py` (`DEFAULT_EXCLUDE_ROWS=set()` so
   row 0/far field is INCLUDED, `FAR_POSITIVE_MULTIPLIER=4.0`, `compute_camera_weights`,
   `classify_camera`). Train entry point: `training/train_v3.py` (manifest.db + packs via
   ManifestTrainer; `--data dataset.yaml --model yolo26l.pt`). NOTE: `organize_dataset.py` /
   `smart_sampler.py` carry their own `DEFAULT_EXCLUDE_ROWS={0}` copies and `training/tasks/train.py`
   is a separate path — mirror the v3 knobs there if the production run uses them.
5. Evaluate recall **per game with a per-camera breakdown** (target: beat v2's 0.29 by a lot,
   and beat the reference tracker on far balls). Then swap the production `ball_detect` step
   (`video_grouper/pipeline/steps/ball_detect.py`) from tiled inference to the warped
   full-frame model.

### Landed v3 modules (this branch, unit-tested — 44 tests green)
- `training/data_prep/far_ball_miner.py` — velocity-gap far-ball miner + labeling queue writer.
- `training/data_prep/field_warp.py` — `build_field_warp` / `warp_frame` / `warp_points` /
  `unwarp_points` (anisotropic vertical warp + inverse LUT; round-trips sub-2px; 7680×2160 →
  ~0.08 MP single warped input vs 8.6 MP for the 21-tile path).
- v3 dataset/config in `training/data_prep/manifest.py` (knobs above) + `training/train_v3.py`.

## Server + access

GPU server **DESKTOP-5L867J8** (GTX 1060 6GB; CUDA visible from WinRM). Footage + CUDA are
local there — run training there, not on this dev box. Credential: CliXml at
`%LOCALAPPDATA%\credentials\desktop5l-training.xml` (user `DESKTOP-5L867J8\training`).
T:\ = `\\DESKTOP-5L867J8\video\test\`. Bash tool strips Windows backslashes — use `/c/...`.

### Processes (server + remote workers)

| Process | Machine | Port | What it does |
|---------|---------|------|-------------|
| PipelineAPI | Server | 8643 | FastAPI, sole SQLite accessor for registry + work queue |
| PipelineOrchestrator | Server | — | Populates work queues via API every 60s |
| PipelineWorker | Server | — | Pulls stage/tile/QA/review tasks |
| AnnotationServer | Server | 8642 | Human review UI (Tailscale: trainer.goat-rattlesnake.ts.net) |
| PipelineWorker | jared-laptop | — | Tile/label/train (RTX 4070, CUDA) |
| PipelineWorker | FORTNITE-OP | — | Label/tile (RTX 3060 Ti), yields for games |

**Restart server services:** `powershell -ExecutionPolicy Bypass -File training\pipeline\install_service.ps1`
**Deploy remote worker:** `powershell -ExecutionPolicy Bypass -File training\worker\deploy_worker.ps1 -Machine laptop|fortnite`

## Storage architecture

```
F: (USB, 15TB)   — PERMANENT archive. Original videos (F:\Heat_2012s, F:\Flash_2013s),
                   pack files (F:/training_data/tile_packs/{game_id}/*.pack). Server only.
                   F:\test\onnx_models\decrypted\ = reference detector ONNX (RE-adjacent).
D: (HDD, ~1.8TB) — SERVING (SMB-shared \\192.168.86.152\training). manifest.db per game,
                   staged packs (restored from F: on demand), review packets, deploy files.
G: (SSD, ~271GB) — PROCESSING (local). registry.db, work_queue.db, per-game work dirs.
                   The v3 rolling working set lives here — it CANNOT hold the full corpus.
```

**Pack lifecycle:** create on G: → push to D: (manifest pack_file = D: path) → archive to F:
→ clean D: → `server_packs()` auto-restores F:→D: on demand. Remote workers see D: via SMB only.

## Registry (rebuilt 2026-06-14)

`game_registry.json` rebuilt via `python -m training.data_prep.game_registry` (scans F: team
archives; not hand-edited). **102 entries / 73 trainable / 23 reolink_segments** (all trainable;
≈18 substantial 13–25-segment games + ~4 one-segment fragments). Reolink games already archived
to `F:\Heat_2012s\2026.05.*` (→ `heat__`) and `F:\Flash_2013s\2026.05.*` (→ `flash__`) — no
ingest needed. Orientation comes from the registry (`UPSIDE_DOWN_GAMES` in game_registry.py),
NOT detection (auto-detect proven unreliable). 12 trainable games are upside_down (2024-2025
Dahua); all Reolink are right_side_up.

## Key commands

```bash
uv run python -m training.pipeline status      # pipeline state
uv run python -m training.pipeline games        # game list
uv run python -m training.pipeline events       # last 6h event log
uv run python -m training.pipeline enqueue tile --game GAME_ID --priority 30
```
