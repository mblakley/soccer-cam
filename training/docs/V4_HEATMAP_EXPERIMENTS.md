# v4 heatmap detector — overnight experiment log (2026-06-15/16)

**Goal:** prove a CPU/edge-runnable detector can beat AutoCam (74% far-recall / 76% precision on the
Irondequoit held-out GT), **especially on far balls**. Architecture = dewarp (native-res field-band
crop) + polygon-mask + multi-frame heatmap U-Net (`training/models/heatmap_net.py`).

**Proof metric (full-frame, center-distance, R=20px in source coords), on the held-out Irondequoit
162 human balls, split:**
- `all` — vs AutoCam 76% (123/162)
- `veryfar` (gt cy≤450) — vs AutoCam 74% (97/131)
- `acmissed` — the balls AutoCam MISSED (gt not within R of an AutoCam detection). **Recall here > 0
  = recovering balls AutoCam couldn't get = beating AutoCam on the hard far balls.** This is the headline.

**Training data (no new human labels available overnight):** dense AutoCam conf≥0.5 in-field
detections on the four 05-27 segments (~1,100 balls) — near/mid + the far balls AutoCam *does* get.
Irondequoit held out for eval. Far-ball signal beyond AutoCam comes from: motion (multi-frame),
gap pseudo-labels (velocity-extrapolated), and self-training — the experiment axes below.

## Key finding (2026-06-15 late): naive MSE AND focal both COLLAPSE at crop=256/sigma=4
Loss drops to ~0 while recall stays 0 — the model predicts a blank heatmap because ~2–80 ball px in
65,536 is too imbalanced. Fix = smaller crop (less background) + larger Gaussian. Engine =
`G:\v4bench\heatmap_exp.py` (cached dense labels + cached crops per size + train + full-frame
far-split eval → appends `G:\v4bench\hm_results.jsonl`).

## Experiment queue (work top-down; keep the winner)
1. **Break the collapse** — crop∈{96,128}, sigma∈{4,6,9}, loss∈{focal,wbce}, lr∈{1e-3,3e-3}. Gate:
   crop-eval recall > 0 and full-frame `all` recall climbing.
2. **Match AutoCam on easy** — best of (1), more epochs; gate: `all`/`veryfar` recall approaches 74%.
3. **Beat AutoCam on far** — add (a) explicit motion channel (frame-diff), (b) gap pseudo-labels
   (velocity-extrapolated far positions as training targets), (c) self-training (net's far hits →
   pseudo-labels → retrain). Gate: `acmissed` recall > 0, `veryfar` > 74%.
4. **Optimize** — backbone width/depth, sigma schedule, label conf threshold, augmentation, FixRes.
5. **Edge check** — CPU fps on the winner (budget 1.25 fps; aim realtime).

## Results
| tag | crop | sigma | loss | lr | ep | train | all | veryfar | acmissed | notes |
|-----|------|-------|------|----|----|-------|-----|---------|----------|-------|
| (pending) | | | | | | | | | | engine build |

_AutoCam baseline for reference: all 76%, veryfar 74%, acmissed 0% (by definition)._
