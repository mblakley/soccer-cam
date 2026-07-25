# World-model ball tracker — permanent decisions

Decisions are permanent. If reversed, add a new entry explaining why; never delete.

## D1 (2026-06-17) — Augmentation is a dead end; train the detector on REAL data only
**Decision:** the ball detector is trained with NO synthetic augmentation (no cut-paste,
no track-shift). Use the `no-aug` model.
**Why:** every augmentation variant tried — naive cut-paste, physics-correct (velocity-from-
appearance), recall/occlusion/dim, and the maximally-realistic track-shift (erase real ball
+ grass-paste, paste shifted on the real action background) — **lowers the detector's recall
ceiling** (held-out Spencerport: no-aug ceiling 0.716 in meters; every augmented model 0.50–
0.60). Mechanism: a real-data detector fires broadly (high recall, ball usually in the
candidate set); augmentation sharpens the "ball" prototype, raising per-frame top-1 a little
but missing more real balls. The downstream re-ranker is **recall-bound** (it can only pick
the ball from candidates that contain it), so augmentation nets out negative. Verified across
6 retrains (EXP-22..34). The augmentation code (`far_ball_augment.py`) is kept and tested but
is not used in the production pipeline.

## D2 (2026-06-17) — The win is the CONTEXT TRACKER, not the detector
**Decision:** put the intelligence in the tracker/world-model layer, not in trying to make
the dim ball the brightest/most-discriminable detection.
**Why:** the detector finds the ball (candidate ceiling ~0.72) but ranks it #1 only ~16% of
the time — it is genuinely dim. The tracker recovers it from context. Verified ablation
(held-out, R15m viewport): brightness-argmax 0.33 -> + re-ranker (static-persistence penalty
+ motion-blob support + meters-smooth Viterbi) 0.49 -> + action-density prior (player
clusters) 0.54 -> + Kalman RTS smoother/occlusion-coast 0.58 (leave-one-clip-out CV: 0.56).
AutoCam scores ~0 on these (its blind clips). Components live in `reranker.py`.

## D3 (2026-06-17) — Evaluate at VIEWPORT scale (meters), not strict pixel/▒5m
**Decision:** the headline metric is recall within a viewport tolerance in METERS on the
field plane (R≈10–15 m), via the homography — not source pixels, not a tight 5 m.
**Why:** the deliverable is a broadcast viewport that points at the action. A near-ball pick
(e.g. a player's foot next to the ball) still frames the action correctly; only FAR picks
swing the camera away. Source-pixel radii also badly flatter the far field (400 px ≈ 13 m at
a far corner vs ~5 m near). Scoring in source px or at strict 5 m understated the system by
~2x.

## D4 (2026-06-18) — Appearance discrimination of the dim far ball does NOT generalize
**Decision:** do not rely on appearance (handcrafted size/brightness OR a learned R3 CNN) to
separate the ball from co-located distractors on held-out games.
**Why:** the dim far ball is appearance-ambiguous. Handcrafted size fails (the dim ball has
no well-defined scale: median char_sigma = max). A tiny ball-vs-distractor CNN reaches 0.80–
0.96 AUC in-distribution but only **0.62 AUC on held-out** games (both bright-maxima and the
detector's-own-false-peaks as hard negatives), and gives no reliable pipeline gain. A 24×24
grayscale patch of the dim ball vs a player part is genuinely not separable. The remaining
~40% gap (occlusion, players co-located with the ball) is a fundamental limit of the
single-camera approach, not a missing technique.

## D5 (2026-06-18) — Candidate fusion is the only further lever, and it is marginal
**Decision:** optionally union multiple detectors' candidates to raise the recall ceiling,
but it is a marginal win at multiplied inference cost — not the default.
**Why:** union(no-aug, champ-J, phys3) raises the ceiling 0.716->0.833 but the pipeline only
0.580->0.595 (+1.5 pts) at 3x inference. Selection — not recall — is the bottleneck, so a
higher ceiling barely helps. Recall levers (top-K dumps, fusion) are deprioritised.
