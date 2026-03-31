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

## v3 Training Plan (Informed by Small Ball Experiments)

### Key Findings from Experiments
1. **Far field balls ARE detectable** — 88% of ONNX trajectory gaps had visible motion, 78% Sonnet-verified as real balls
2. **r0 was excluded from all training** — model has never seen far-field tiles. Including r0 with 797 verified labels at 4x weight is the biggest single improvement
3. **Brightness is a strong signal** — verified balls avg brightness 134, false positives are dimmer. Augmentation should preserve ball/grass brightness contrast
4. **Trajectory context is the real discriminator** — single-frame can't distinguish 12px ball from white sock. 3-frame temporal model (train_temporal.py) would natively capture this
5. **Player exclusion zones** — 18% of FPs are on player body parts. Train with player bbox awareness
6. **Overall detection coverage is only 37.5%** — need v2 model re-detection + gap filling for 95% continuous tracking
7. **Coverage varies by segment** (30-70%) — weight training samples by segment difficulty

### v3 Dataset Improvements
- [ ] Tile all 35 games (mass_tile.py, 26 remaining)
- [ ] Bootstrap label new games with ONNX detector
- [ ] Include r0 tiles with verified labels (797 from experiments + ongoing)
- [ ] Apply single-ball constraint during active play phases
- [ ] Run gap detection on ALL games, ALL rows
- [ ] Automated gap filling (ONNX low-conf, frame diff, optical flow)
- [ ] Adaptive density gap mining for fast kicks (every frame in gap)
- [ ] Sonnet triage of gaps >= 2 seconds
- [ ] Human review via prioritized queue (Sonnet failures first)
- [ ] v2 model re-detection + disagreement review with ONNX
- [ ] Weight r0 positives at 4x, difficult segments at 2x
- [ ] Per-game coverage tracking (target: 95% during active play)

### v3 Training Configuration
- [ ] Include r0 in training (remove `DEFAULT_EXCLUDE_ROWS = {0}`)
- [ ] Include corrected/flipped upside-down games
- [ ] Train temporal model (3-frame input) alongside single-frame YOLO
- [ ] Add player bbox as auxiliary negative signal (suppress detections on players)
- [ ] Reduce augmentation brightness jitter to preserve ball/grass contrast
- [ ] Per-row confidence thresholds at inference (lower for r0)
- [ ] Continuous training: rebuild dataset as human labels arrive, resume training

### v3 Training Loop (continuous improvement, not one-shot)

The v3 process is a loop, not a pipeline. Training starts with imperfect labels and improves as human + Sonnet labels arrive. Each cycle produces a better model that finds more balls, reducing gaps and human workload.

```
┌─────────────────────────────────────────────────────────┐
│  PHASE 1: Automated labeling (all 35 games)             │
│  ├── Tile all games (mass_tile.py)                      │
│  ├── ONNX bootstrap at conf=0.45                        │
│  ├── Trajectory linking + 3-class classification        │
│  ├── Single-ball constraint (remove multi-ball in play) │
│  └── Gap detection: find all gaps > 50 frames           │
│                         ↓                               │
│  PHASE 2: Automated gap filling                         │
│  ├── Gaps < 2s: ONNX low-conf + frame diff + opt flow  │
│  ├── Adaptive density for fast kicks (every frame)      │
│  └── v2 model re-detection at lower confidence          │
│                         ↓                               │
│  PHASE 3: Sonnet triage                                 │
│  ├── Review all gaps >= 2s                              │
│  ├── Verify automated gap fills                         │
│  ├── Review v2-vs-ONNX disagreements                    │
│  └── Result: gaps filled OR escalated to human          │
│                         ↓                               │
│  PHASE 4: Human review (prioritized queue)              │
│  ├── #1: Sonnet failures (AI can't find ball)           │
│  ├── #2: Longest gaps during active play                │
│  ├── #3: High-quality tracks that broke                 │
│  └── Annotation app shows context + priority score      │
│                         ↓                               │
│  PHASE 5: Train → Evaluate → Repeat                    │
│  ├── Build dataset from current best labels             │
│  ├── Train v3 model                                     │
│  ├── Measure coverage per game (target: 95%)            │
│  ├── Run v3 model on all games → find NEW gaps          │
│  └── New gaps feed back into Phase 2 ──────→ LOOP       │
└─────────────────────────────────────────────────────────┘
```

**Key principles:**
- Training doesn't wait for perfect labels — start with what we have
- Human time goes to highest-value gaps (Sonnet failures, longest gaps)
- Each model iteration finds more balls → fewer gaps → less human work
- Per-game coverage tracking: when a game hits 95%, deprioritize it
- The loop converges: v3 model finds gaps v2 missed, v4 finds gaps v3 missed

### Flywheel Implementation

The flywheel is a long-running process that orchestrates cycles automatically. Each cycle runs to completion, then the next cycle starts with the improved model. Human review happens asynchronously — the flywheel doesn't block on it, just picks up whatever labels are available.

```
training/flywheel/
  runner.py          — Long-running orchestrator, manages cycles
  coverage.py        — Measure per-game tracking coverage + find gaps
  gap_filler.py      — Automated gap filling (ONNX low-conf, frame diff, optical flow)
  sonnet_triage.py   — Send gaps >= 2s to Sonnet, rank failures for human
  label_merger.py    — Merge new labels from any source into main set
  priority_queue.py  — Prioritized human review queue, serves annotation app
```

**Runner lifecycle:**
```
runner.py (long-running, restartable)
  │
  ├── On startup: read state from flywheel_state.json
  │   (which cycle, which step, what's pending)
  │
  ├── CYCLE N:
  │   ├── Step 1: Run current model on all tiles → detections
  │   ├── Step 2: Build trajectories, measure coverage, find gaps
  │   ├── Step 3: Auto-fill gaps < 2s
  │   ├── Step 4: Sonnet triage gaps >= 2s (async, batched)
  │   ├── Step 5: Merge all available labels (auto + Sonnet + human)
  │   ├── Step 6: Rebuild dataset from merged labels
  │   ├── Step 7: Train model (resume from last checkpoint)
  │   ├── Step 8: Evaluate: per-game coverage, total gaps, improvement
  │   └── Step 9: If coverage < 95% → start CYCLE N+1
  │
  ├── Between cycles: pick up any human labels that arrived
  │
  └── On completion (all games >= 95%): notify human, stop
```

**State persistence:** `flywheel_state.json` tracks current cycle, step, per-game coverage, pending human reviews. Runner can be killed and restarted — it picks up where it left off.

**Human review runs in parallel:** The annotation app serves from the priority queue. Human labels land in a directory that label_merger picks up at the start of each cycle. No blocking.

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
  → External model detection (ONNX balldet_fp16)
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
