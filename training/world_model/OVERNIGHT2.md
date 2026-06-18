# Overnight session 2 — hunting non-augmentation wins (2026-06-17 → 18)

Baseline at start (held-out Spencerport, clips 1-4 unless noted):
- Detector: no-aug, ceiling 0.716 (R5m), top1 0.163.
- Pipeline: re-ranker + action-prior(0.5) + Kalman = **R15 0.580**, R10 0.461, R5 0.342. AutoCam ~0.

Decision recorded: **augmentation is a dead end** (every variant incl. realistic track-shift
lowers the detector ceiling; pipeline is recall-bound). Use no-aug + context tracker.

## Research (web)
- ByteTrack: two-stage association — rescue tracks with LOW-confidence detections during
  occlusion. (Our re-ranker already uses low alpha; Kalman coasts misses.)
- Reviews: ball-vs-distractor = "ball-sized objects" focus (SIZE) + player-equipment spatial
  relationship; occlusion = Kalman + optical flow; trajectory = Kalman/IMM.
- Takeaway levers to try: SIZE/appearance discriminator, player-context geometry (feet vs
  body), better motion model (IMM/const-accel), confidence-aware association.

## Experiments

### EXP-F1 candidate fusion (raise ceiling) — MARGINAL
union of detector dumps. ceiling 0.716→0.833 (noaug+champJ+phys3) but pipeline R15 only
0.580→0.595; other unions WORSE. **Recall is not the bottleneck — selection is** (more
candidates = more distractors the re-ranker mis-picks). Not pursuing recall/top-K dumps.

### Cheap selection levers — ALL TAPPED OUT at R15 0.580
- action_density weight: 0.5 confirmed optimal (sweep 0.3-0.8).
- box-y prior (feet vs body): HURTS (ball-on-chest/head cases need high-in-box). 0.580→0.546.
- player-convergence prior (players chase ball): HURTS (box-matching too noisy @ stride-4;
  helps occluded clip3 0.32→0.35 but hurts easy clip4 0.83→0.75). 0.580→0.565.
- motion-blob AREA (ball small / player big): NO signal (ball 430 vs distractor 446 px² —
  the ball's blob merges with the nearby player's).
- Kalman q/r: plateau (R15 insensitive; R10 best ~0.468 at q0.6/r1.5).
- Appearance handcrafted (char_sigma/size): FAILS — the dim ball is scale-ambiguous
  (median char_sigma 20 = max, vs sharp distractors 4). Confounded by dimness.
=> context/motion tracker is MAXED. Per-clip R15: c1 0.51, c2 0.66, c3 0.32(occluded), c4 0.83.

### EXP-R3 learned appearance discriminator (the only lever left) — IN PROGRESS
Tiny CNN (3-frame 24x24 -> ball/distractor) to re-score the re-ranker emission.
- Train data from hm_ds_v2 (no decode): 1419 ball + 3599 distractor (bright non-ball peaks
  in positive crops + random bg). Tiny CNN, **val AUC 0.959** (held-in split) — appearance
  IS discriminable by a learned model where handcrafted size/brightness failed.
- Held-out test: extract a patch at every no-aug candidate on Spencerport clips 1-4 (decode),
  score with R3, add as a re-ranker prior. [waiting on extraction, then integrate+measure]
- Caveat: held-in val is optimistic; hard-negs are bright maxima (lines/kit), may under-cover
  player-part distractors. The held-out pipeline number is the real verdict.
- **VERDICT: R3 does NOT generalize.** Held-out ball-vs-distractor AUC = **0.62** for BOTH
  negative sources (v1 bright-maxima: 0.626; v2 detector's-own-false-peaks: 0.617), despite
  0.80-0.96 held-in. R3-only top1 (0.14-0.17) ties brightness (0.163). v1 showed a small
  weight-overfit pipeline bump (R15 0.599 @ w=3) that v2 (honest negatives) does NOT
  reproduce (R15 <=0.580). **The dim far ball is appearance-ambiguous on held-out games** —
  a 24x24 grayscale patch of the dim ball vs a player part is genuinely not separable by a
  CNN. This is the fundamental wall: not augmentation, not handcrafted features, not a
  learned discriminator — the appearance signal is too weak.

### EXP iterative trajectory refinement — WASH
Run re-ranker, Kalman-smooth, re-run gating candidates by distance from the predicted path,
iterate. 0.580 -> 0.576-0.584 (noise). The global Viterbi is already optimal given its
emission/transition; re-gating by its own first pass adds no information.

### Verified ablation (held-out clips 1-4, R15m viewport)
- brightness argmax (no tracker):        0.331
- + re-ranker (static+motion+smooth):    0.491
- + action-density prior(0.5):           0.543
- + Kalman RTS smoother (FINAL):         0.580
- Leave-one-clip-out CV (honest):        **0.561** (action_w=0.5 generalizes; ~2pt overfit)
The tracker layer ~doubles brightness (0.331 -> 0.561 CV). vs AutoCam ~0 on these clips.

## CONCLUSION (verified, all on held-out clips 1-4)
The pipeline was ALREADY near-optimal. Tonight's exhaustive search:
- **The one positive, marginal: candidate FUSION** (noaug+champJ+phys3) -> R15 0.580→**0.595**
  (+1.5 pts) by raising the ceiling (0.716→0.833), but at 3x inference cost. Other fusions
  worse. Selection (not recall) is the bottleneck, so even the 0.833 ceiling barely helps.
- Everything else NEUTRAL-or-NEGATIVE: box-y prior, convergence prior, motion-blob size,
  Kalman q/r, top-K recall, and R3 appearance (doesn't generalize).
- **Best reliable config: no-aug detector + re-ranker(static+motion+meters-smooth) +
  action-density prior(0.5) + Kalman RTS = R15 0.580** (0.595 w/ fusion @3x cost). vs AutoCam ~0.
- The remaining 42% gap is FUNDAMENTAL: dim appearance-ambiguous ball + occlusion (clip3
  recall-limited) + players co-located with the ball. No technique tonight moved it
  materially. This is the ceiling of the single-camera context-tracker approach.
