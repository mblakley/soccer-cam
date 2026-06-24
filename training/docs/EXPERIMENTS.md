# Experiment Log

Each experiment has: hypothesis, method, result, conclusion. Failures are as valuable as successes.

---

## EXP-DIST-02: Corrected data-scaling / venue-diversity curve (2026-06-24)

**Status:** corrected curve RE-LAUNCHED and running (server GTX 1060). Prior `curve.jsonl` (N=1..16,
6/19–6/23) is **INVALID** — see method. Scratch: `G:\ballresearch\distill\` (NOT in repo).

**Hypothesis (carried from the distill plan + the prior "normal-play gap" finding):** the distilled
detector wins big on far/hard balls but collapses on normal play; the open question is whether the
normal-play bottleneck is **venue diversity** (more games/venues) vs capacity/quantity. The curve
trains HeatmapNet (base24, no-aug, fixed 30k-step budget so only DATA DIVERSITY varies) on N = 1,2,4,8,16
archived AutoCam-distilled games and evals in **meters** on the held-out Spencerport stretch (frames
7900–11500), split HARD (human far labels) vs NORMAL, vs the AutoCam baseline.

**Method / what was found this session:**
1. **Verified the *previous* session's polygon fix is real:** `resolve_polygon()` (canonical
   `gamedata.polygon(gd.resolve(gid))` → v4_fields date+opp → manifest.db) resolves **16/16** archived
   games (15 via gamedata, 1 via v4_fields). The old fuzzy date+opp resolver matched only ~4 (archive
   games are named `guzzetta__*`/`flash__*`; polygon dirs are `heat__*`).
2. **BUT discovered a SECOND, independent bug that invalidated the entire `curve.jsonl`:** the curve
   rows N=1..16 were produced by an **older `iter_run.py`** (run 6/23 06:44, before the fix finished at
   6/23 21:24). Its run logs show 12/16 games hit `"NO polygon … skip"` and **N=16 actually trained on
   only 4 games** (`train roots (4 games)`, 44 segment-games, 20,303 labels). So every "data-scaling"
   point trained on ~4 games regardless of N — the curve was flat **by construction**, not because
   venue diversity doesn't help. `"NO polygon … skip"` does not even exist in the current code.
3. **DRYRUN of the CURRENT `iter_run.py` on all 16 games → resolves 16/16, 140 segment-games, 52,367
   labels** (+992 human overrides, 94,215 hard-negative false-fires). Confirmed the fix end-to-end
   before relaunch.
4. **Found the NORMAL-split eval is confounded** (the headline metric was partly an artifact): the
   curve's NORMAL GT = AutoCam-detector dets conf≥0.40 corroborated only by viewport-x within 500px —
   **no Y constraint, no on-field test.** Of 283 normal GT balls, **128 (45%) are off-field and/or fall
   outside the eroded eval band-mask** (cluster at the far-right sideline corner, x 6826–7430). So the
   detector is structurally unable to fire on ~45% of "normal" targets → ceiling capped at ~0.55,
   independent of model quality. HARD (human) GT is clean (92% in-mask) and trustworthy.

**Result:** Prior curve quarantined to `curve.jsonl.buggy4games_20260624_081941`. Corrected curve
relaunched 2026-06-24 08:19 via the detached orchestrator; N=1 confirmed training (GPU 27–72% util,
loss 0.012 @ step 2k). HARD numbers from the buggy curve already show the **real, trustworthy** signal:
detector R15 ≈ 0.39 vs **AutoCam 0.11** on hard/far balls — the distill beats AutoCam there. NORMAL
numbers from the buggy curve are NOT interpretable (4-game training + confounded GT).

**Conclusion:** The venue-diversity question is **still open** — it was never honestly tested (curve
trained on 4 games). The corrected 16-game curve will answer it. Separately, the NORMAL eval must use a
**clean on-field + in-band GT** before its numbers mean anything (a CPU re-eval harness, `clean_eval.py`,
quantifies the artifact: raw_normal vs clean_normal=155). Two distinct bugs (fuzzy resolver; stale
binary) silently gutted the curve — re-verify game-count from the run log (`train roots (N games)`),
never trust the curve row alone.

---

## EXP-008: Field-boundary distillation pipeline (2026-06-11)

**Hypothesis:** A small in-house CNN can reproduce the teacher's 10-point field polygon closely enough — IoU ≥ 0.90 vs teacher, gate agreement ≥ 90%, per-point error ≤ ~8px in 768×384 — to replace it as a drop-in ONNX.
**Method:** Standalone distillation — label-gen (teacher over Reolink footage) → placement-split dataset + heavy augmentation → ResNet18 dual-head student → ONNX export matching the teacher's I/O signature + parity check. Corpus: ~33 Reolink games (7680×2160) from `D:/soccer-cam-storage`, ~9 venues, Heat-heavy plus a few Flash; Dahua footage excluded.
**Result (2026-06-12, GPU server):** Generated 1,000 teacher labels over 21 Reolink games → 8 placement clusters (1 Flash + 7 Heat), split train=688 / val=66 / test=246. ResNet18 student, early-stopped epoch 87 (best epoch 72, val pixel error 15.1 px ≈ 2% of 768 width). Held-out **test** (davis + hilton): overall IoU 0.64, gate-agree 0.84 — but per-cluster: **davis_park IoU 0.79**, **hilton_high_school IoU 0.32**. Export parity vs teacher on representative frames: **IoU 0.936, gate-agree 1.00**, mean per-point delta 20.6 px; ONNX signature byte-identical to teacher, drop-in through unmodified `field_detector.py` verified (20/20), checkpoint-vs-ONNX deviation 0.25 px.
**Conclusion:** Distillation works on normal grass venues (davis 0.79, representative-frame parity 0.94) — a viable v1 drop-in. The 0.90 bar is not met *overall* because hilton_high_school is an **American-football turf field** (yard lines, glare) where the teacher itself is unreliable (mean_score 0.43, zero gate-pass frames) — out-of-distribution, not a model defect. Per-point: near-center (pt 2) best (9.7 px), intermediates/corners worst (pt 1: 32.9 px). Next: exclude football-field venues or add human-corrected labels for anomalous venues (v2); more venues would raise the floor. Winning backbone: resnet18.
**Artifacts:** `F:/training_checkpoints/field_outline/student.onnx`, run `training/runs/field_kpts_v1/`.
**Code:** `training/field_outline/`, `training/cli/*_field_outline.py`

## EXP-007: Game phase detection from multi-ball patterns (2026-03-30)

**Hypothesis:** Warmup/halftime/postgame have multiple scattered ball detections; active play has a single ball trajectory.
**Method:** `game_phase_detector.py` — 30-second rolling windows, count frames with >3 concurrent detections spread >500px apart. Phase transitions at multi-ball/single-ball boundaries.
**Result:** Generated manifests for 9 games. Most games show clear warmup→first_half→halftime→second_half→postgame progression.
**Conclusion:** Works for standard games. FAILS for tournaments — sub-game breaks detected as single long halftime. Multi-game recordings need per-sub-game phase detection.
**Data:** `F:/training_data/game_manifests/{game_id}.json`

## EXP-006: Far-field gap detection across all rows (2026-03-30)

**Hypothesis:** ONNX trajectory gaps (missing detections between linked positions) exist in r1/r2 too, not just r0.
**Method:** `exp_allrow_gaps.py` — trajectory linking + gap detection on all tile rows.
**Result:** 19,239 gaps total. r0: 6,967, r1: 9,491, r2: 2,781. r1 has the most gaps.
**Conclusion:** Gap filling should target all rows, not just far-field. r1 (mid-field) is the biggest opportunity.
**Data:** `F:/training_data/experiments/exp_allrow_gaps.json`
**Code:** `training/experiments/exp_allrow_gaps.py`

## EXP-005: Targeted frame diff at gap positions (2026-03-29)

**Hypothesis:** Frame differencing at ONNX gap positions (where ball should be but wasn't detected) will find missed balls with fewer false positives than blind frame diff.
**Method:** `exp3b_fullscale.py` — seek to each gap frame in video, extract small region around predicted position, check for motion blob matching ball size/circularity.
**Result:** 4,570 verified motion candidates, 1,565 high-confidence (size 15-200px², circularity >0.5, on-field).
**Conclusion:** Gap-guided targeting dramatically reduces false positives vs blind frame diff. 797 Sonnet-verified as real balls.
**Code:** `training/experiments/exp3b_fullscale.py`

## EXP-004: ONNX trajectory gap mining (2026-03-29)

**Hypothesis:** When ONNX detects a ball in r0 in frames N and N+2 but not N+1, the ball is likely still there in N+1 — the model just missed it.
**Method:** `exp1_onnx_gaps.py` — trajectory linking on r0 labels, find frames where detections are missing between linked positions, interpolate expected position.
**Result:** 11,425 gap candidates across 9 games (avg 1,269/game).
**Conclusion:** Gaps are real and frequent. Most gaps are 1-3 frames — brief occlusions or model uncertainty. Provides high-quality training targets.
**Data:** `F:/training_data/experiments/exp1_onnx_gaps.json`
**Code:** `training/experiments/exp1_onnx_gaps.py`

## EXP-003: Blind frame differencing for small balls (2026-03-28)

**Hypothesis:** Motion-based detection (frame differencing) can find small balls that ONNX misses at far-field distances.
**Method:** `frame_diff_detector.py` — compute frame diff on r0 tiles, filter by circularity >0.5 and area 15-300px², link into trajectories (min 3 frames, path >30px).
**Result:** 31,000 "moving" trajectories in 200 frames — overwhelmingly player motion, not balls.
**Conclusion:** FAILED as standalone approach. Player motion dominates. Needs: (1) player mask subtraction, (2) ONNX gap guidance to focus search, (3) tighter circularity/size filters.
**Follow-up:** EXP-005 used gap-guided targeting and succeeded.
**Code:** `training/data_prep/frame_diff_detector.py`

## EXP-002: Sonnet Vision QA for label quality (2026-03-27)

**Hypothesis:** Sonnet can reliably verify whether a tile crop contains a soccer ball.
**Method:** `label_qa_prep.py` generates 3x2 composite grids of tile crops. Sonnet classifies each as BALL/NOT_BALL. Batched at ~100/hr to stay within budget.
**Result:** 4,042 positive tiles reviewed: 33.4% true positive, 29% false positive. 4,333 negative tiles: 0.6% false negative rate.
**Conclusion:** Sonnet is excellent at confirming negatives (99.4% accuracy) and good at catching false positives. Positions r1_c5, r1_c6, r2_c4 have highest FP rates (sun glare, poor detection).
**Data:** `F:/training_data/label_qa/report.json`

## EXP-001: Tracker parameter sweep (2026-03-25)

**Hypothesis:** Optimal Kalman filter parameters for ball tracking can be found via systematic sweep.
**Method:** `review_packets/tracking_lab/experiment_log.md` — sweep gate distance (50-500px), max_miss frames (10-120), process noise, on one game segment.
**Result:** Best: gate=300, max_miss=90 achieved 95.2% coverage (frames with tracked ball / total frames).
**Conclusion:** Large gate + high persistence works for panoramic view where ball can move fast between frames. Prediction quality matters more than tight gating.
**Data:** `review_packets/tracking_lab/experiment_log.md`

## EXP-000: Label filtering heuristics (2026-03-22)

**Hypothesis:** Simple geometry filters can remove obvious false detections from ONNX bootstrap labels.
**Method:** `label_filters.py` — aspect ratio 0.5-2.0, width 0.008-0.06 normalized, edge clipping.
**Result:** 568K → 488K files, 759K → 606K detections (20% removed).
**Follow-up:** Trajectory validator removed additional 24% (606K → 462K), keeping only detections in trajectories ≥3 frames.
**Code:** `training/data_prep/label_filters.py`, `training/data_prep/trajectory_validator.py`
