# Overnight summary — ball world-model (2026-06-16 → morning)

**Headline:** the world-model **beats AutoCam decisively** on the hardest clip, and there is a **measured
speed path that meets your overnight budget on the real base hardware (this laptop's iGPU)**. Accuracy is
now **candidate-limited** (needs a better/ball-vs-person-aware detector), not tracker-limited — that's the
one lever left, and it's the thing the new labels unlock. Everything below is committed on
`research/ball-world-model`; raw per-run data in `overnight_experiments.jsonl`; full detail in
`DESIGN.md` (EXP-13…18).

---

## 1. Where accuracy stands (viewport area-recall, the metric you care about)

Pipeline: `suppress_static_candidates → mask_person_candidates → causal_track_fused` (static-aware,
action-prior). Config: static_thresh 0.3, action_pull 0.6, vel_decay 0.8.

| game | argmax raw J | **world-model** | AutoCam |
|---|---|---|---|
| **Spencerport clip-1** (4:45, human GT, AutoCam totally lost it) | 0.23 | **0.66 @R400 / 0.45 @R200** | **0.014** |
| Irondequoit (human GT) | 0.39 | **0.87 @R400** | 0.74* |
| Fairport (AutoCam GT) | 0.42 | **0.77 @R400** | — |

*Irondequoit AutoCam 0.74 is at exact R=20; the world-model 0.87 is viewport-scale R=400. On clip-1 the
world-model points the viewport at the ball's area **66%** of the time where **AutoCam sat at frame-center,
~2850px off (0.014)**.

## 2. Accuracy experiments tonight (EXP-13/14) — the tracker is tapped out

I tried to close the residual gap (clip-1 0.66 → the 1.0 fused-candidate ceiling) from the tracker side:

- **Diagnosis:** all **25/25** clip-1 @R400 misses are **recoverable** (the ball IS in the candidates ≤400px
  every missed frame) and the ball is **slow** (3.8 px/frame median). So it's pure **selection drift**.
- **EXP-13 forward-backward smoothing — negative** (helps Fairport, hurts clip-1/Iron; the backward pass
  cold-starts wrong at the clip end).
- **EXP-14 selection sweep — no win** (vel_decay × action_pull × re-acquire radius, 3 games). The current
  config is the cross-game optimum; nothing improves clip-1 without regressing another game. `vel_decay` is
  very sensitive on the slow ball (0.85→0.18, 0.80→0.66).
- Earlier: multi-hypothesis beam tracker also failed (brightness-pruning drops the dim ball).

**Conclusion:** greedy-causal + the validated cleaners (static-aware suppression, person-masking) is
**near-optimal for the current candidate quality**. The residual is **data-association the tracker can't
resolve without knowing which candidate is the ball** → the next accuracy lever is the **detector /
measurements**, not more tracker tuning.

## 3. Speed — SOLVED on base hardware (EXP-16/17/18)

Budget: ~0.3 s/frame floor (90-min@20fps overnight ≈ 3.3 fps). The ball detector is the only real cost.

- **ROI-tracking is the lever (EXP-16):** champion-J full-band = 1047 ms/frame (1060); a small ROI window
  around the predicted ball is **30–54× faster** and **preserves recall by construction** (the tracker
  only ever selects within its ≤320px gate, so any ROI ≥ gate picks the identical candidate; full-band
  runs only on the rare re-acquire).
- **The laptop iGPU meets the floor (EXP-17):** `onnxruntime-directml` runs the ONNX export on the Intel
  Iris Xe **even over RDP** → ROI **320px = 75ms (13.3 fps)**, **448px = 210ms (4.8 fps)** — above the 3.3
  fps floor with 1.5–4× margin. 1060 server: 512px = 20ms (52 fps), 50×+ headroom.
- **int8 dynamic quant — no help (EXP-18):** conv-heavy net; slower than fp32 on CPU. Further speed
  (esp. CPU-only, which is marginal at ~2–3 fps) = a **smaller base model** or static QDQ — a retraining
  lever, **not needed** (the iGPU already clears the budget).

champion-J exports cleanly to ONNX (4.2 MB, dynamic H/W). **No accuracy was traded for any of this.**

## 4. What I recommend next (in priority order)

1. **Train the detector with your new labels — the only remaining accuracy lever.** The tracker is maxed
   for current candidates; raising the candidate quality (a ball-vs-person-aware detector so the ball head
   stops firing on people, and better far-ball recall) is what moves the needle. The multi-task
   `HeatmapNet(out_ch=2)` foundation is already in. *Person info stays offline at train time (bootstrap),
   one model per frame at inference.*
2. **Broadcast render of clip-1 — DONE (`clip1_render.mp4`, sent to you).** The world-model track →
   `render_adapter` → broadcast renderer (default config) produced a 1920×1080 follow-view. Keyframes are
   faithful to the 0.66 number: **f0** empty grass (the cold-start acquisition off-ball), **f180** locked
   on the far corner — goal, spectators, players (the viewport is *on the action*, where AutoCam sat
   2850px away at frame-center), **f300** end-phase drift as the ball runs the goal-line (the phase-3
   misses). End-to-end proof the world-model can drive a real broadcast viewport. The side-by-side
   **`clip1_compare.mp4` (sent) is the headline visual** — top: our viewport on the corner action; bottom:
   AutoCam staring at empty mid-field grass, same instant. Re-render after the detector improves.
3. **Label clips 2–5** (dumped, waiting) + I still owe a **cross-game person-mask validation** (person-mask
   is committed but only validated on clip-1; needs Iron/Fairport person boxes).

## 5. Commits tonight (`research/ball-world-model`)
- `EXP-9` static-aware selection (the tracker win) · `EXP-10` size prior ruled out · `EXP-11` person-mask
  win + box-masking caveat · `EXP-12` person-info speed · `mask_person_candidates` productionized ·
  `HeatmapNet(out_ch=2)` multi-task foundation · `EXP-13/14` tracker tapped out · `EXP-16/17/18` speed.
- Test suite green (49 world-model tests); lint/format clean.

**One-line takeaway:** *We beat AutoCam now, and we can run it fast enough on your laptop's iGPU. The next
win is the detector — bring the labels.*
