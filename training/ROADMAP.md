# Ball Detection Training Roadmap

## Current Plan (v2 Training)

### Active Now
- [x] Label all 9 games with external ONNX detector (77/77 segments)
- [x] Sonnet QA all 9 games (4,042 positive + 4,333 negative tiles reviewed)
- [x] Classify labels: game_ball / static_ball / not_ball via trajectory analysis
- [x] Build v2 dataset (275K pairs, 3-class labels)
- [x] Create tar shards organized by game/zone
- [ ] Train v2 on server GTX 1060 (running)
- [ ] Train v2 on laptop RTX 4070 (extracting shards)
- [ ] Train v2 on Fortnite-OP RTX 3060 Ti

### After v2 Training Converges
- [ ] Run Sonnet spot-check on v2 model predictions (label_qa_spot_check.py)
- [ ] Compare v2 FP rate vs v1 baseline (24% → target <10%)
- [ ] Ingest new verdicts → build v3 dataset
- [ ] Confidence calibration (calibrate_confidence.py)
- [ ] Export best model to ONNX/CoreML/TFLite (export_mobile.py)

### Continuous QA Loop
- [ ] Set up recurring Sonnet QA on new predictions
- [ ] Sonnet verification of trajectory chain endpoints
- [ ] Automated precision/recall tracking per training run
- [ ] Per-game quality scoring to identify worst data

## Label Quality Improvements

### Done
- [x] Field mask generation for all 9 games
- [x] Trajectory-based classification (moving vs static vs isolated)
- [x] QA verdict ingestion (2,521 verdicts from Sonnet)
- [x] 3-class labels preserve all data (nothing deleted)

### Planned
- [ ] Complete tile gaps (53 tile jobs submitted)
- [ ] Re-label with v2 model (better model = better labels → virtuous cycle)
- [ ] Verify trajectory chain endpoints with Sonnet (look ahead/behind)
- [ ] Field line detection from pixel analysis (started, needs refinement)
- [ ] Temporal consistency checks (ball can't teleport between frames)

## Infrastructure Improvements

### Done
- [x] Independent worker system (no coordinator SPOF)
- [x] Filesystem job queue (jobs.py)
- [x] WNet share mapping (map_share.py)
- [x] Relay training (server always trains, helpers preempt)
- [x] Tar shard dataset format

### Planned
- [ ] WebDataset integration for streaming training
- [ ] SQLite-backed dataset (one file per game, queryable)
- [ ] Automatic shard sync (new data → new shards → auto-distribute)
- [ ] Training metrics dashboard
- [ ] Automated model comparison (v1 vs v2 vs v3 side-by-side)

## Future Enhancements (Commercial Parity)

### High Priority (Next 3 months)
- [ ] **Temporal model training** (train_temporal.py exists, needs v2 data)
  - 3-frame input (prev + curr + next) for motion-aware detection
  - Should dramatically reduce FPs on static objects
- [ ] **Ensemble detection** (YOLO + temporal + frame differencing)
  - frame_diff_detector.py catches 8-12px balls YOLO misses
  - Combine confidence scores from all three methods
- [ ] **Player tracking** (bootstrap_persons.py exists)
  - Detect all players → ball-player proximity → possession attribution
  - Required for meaningful game analytics
- [ ] **Physics-based trajectory prediction**
  - Model gravity, bounce, friction for post-occlusion prediction
  - Current tracker uses kinematic smoothing only

### Medium Priority (3-6 months)
- [ ] **Real-time inference pipeline**
  - Current: batch processing every 4th frame
  - Target: live 30fps with <100ms latency
  - Use TFLite/ONNX quantized models
- [ ] **Multi-camera support**
  - Triangulate 3D ball position from 2+ cameras
  - Handle occlusion from one view using another
- [ ] **Automated game phase detection**
  - Classify: active play, stoppage, halftime, warmup
  - Only track ball during active play
- [ ] **Ball event detection**
  - Kicks, passes, shots, headers, bounces
  - Requires velocity/acceleration analysis from trajectory

### Long-term (6-12 months)
- [ ] **Broadcast integration** (overlay graphics, replays)
- [ ] **Cloud training pipeline** (move off USB drive!)
- [ ] **Multi-sport support** (adapt for basketball, lacrosse)
- [ ] **Automated highlight generation** (goals, saves, near-misses)

## Architecture Notes

### Dataset Flow
```
Video segments (.mp4)
  → Tile extraction (7x3 grid, 640x640, every 4 frames)
  → External model detection (ONNX model)
  → Trajectory classification (game_ball / static_ball / not_ball)
  → Sonnet QA verification (spot-check 10% of labels)
  → Tar shards (organized by game/zone, ~200 MB each)
  → Training (YOLO11n/s, 3-class detection)
```

### Training Iteration Loop
```
v1 (dirty labels) → model v1 → QA spot-check → clean labels
v2 (classified labels) → model v2 → QA spot-check → refine labels
v3 (refined labels + temporal) → model v3 → ...
```

### Machine Roles
- **Server**: Always training (dedicated GPU), hosts dataset on F: drive
- **Kids' PCs**: Help when idle (relay training), train from local SSD via shards
- **Sonnet agents**: Continuous QA review in background

## Competitive Analysis

### What We Have vs Commercial Products
| Feature | Us | Tracab/Second Spectrum/Hawk-Eye |
|---------|----|---------------------------------|
| Ball detection | ✓ (3-class YOLO) | ✓ |
| Ball tracking | ✓ (EKF + user-guided) | ✓ (more robust) |
| Small ball recovery | ✓ (frame differencing) | ✓ |
| Field-aware filtering | ✓ (polygon masks) | ✓ |
| Multi-camera fusion | ✗ | ✓ (10+ cameras) |
| Player tracking | ✗ (in progress) | ✓ (22 players) |
| Real-time | ✗ | ✓ (50fps) |
| Physics modeling | ✗ | ✓ |
| Human-in-the-loop QA | ✓ (Sonnet + manual) | Limited |
| Temporal detection | ✓ (3-frame model) | ✓ |
| Mobile export | ✓ (CoreML/TFLite) | ✓ |

### Our Key Advantages
1. **Full pipeline ownership** — no vendor lock-in
2. **AI-powered QA loop** — Sonnet reviews labels continuously
3. **3-class detection** — distinguishes game ball from static balls
4. **Cost** — runs on consumer GPUs vs $100K+ commercial systems
