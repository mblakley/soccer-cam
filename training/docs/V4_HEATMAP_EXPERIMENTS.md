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

## Key finding (2026-06-16): THE ARCHITECTURE WORKS — train-fit recall = 84%
EXP-A (crop128/sigma6/focal) fit its own 05-27 training crops at **84% (169/200)** — the heatmap +
dewarp + mask + multi-frame net *does* learn to localize the ball. The earlier "collapse" (recall 0)
was a **val-only data bug**, not architecture: `build_heatmap_crops` **seeked** to t-2 (exact on the
raw GOP=1 05-27 segments, hence 84% train), but the **Irondequoit val clip is re-encoded/trimmed**, so
the seek returned the WRONG frames → ball not at the target → val recall 0. (The naive-MSE/crop256
collapse note below was also real, but smaller crop + focal fixed *trainability*; the val number was
masked by this separate seek bug.) **Fix:** `build_heatmap_crops` now does frame-exact **sequential
decode** (rolling 3-frame buffer), gop-agnostic. Rebuilding crops + re-running to get the real val
recall vs AutoCam.

Engine = `G:\v4bench\heatmap_exp.py` (cached dense labels + cached crops per size + train + far-split
eval → appends `G:\v4bench\hm_results.jsonl`). `crop_diag2.py` = the crop/target visual check + train-fit.

### (earlier) naive MSE AND focal COLLAPSE at crop=256/sigma=4
Loss → ~0 while recall 0 — blank-heatmap minimum (~2–80 ball px in 65,536). Smaller crop + focal fixed it.

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
| tag | crop | sigma | loss | lr | ep | aug | all | veryfar | acmissed | notes |
|-----|------|-------|------|----|----|-----|-----|---------|----------|-------|
| A | 128 | 6 | focal | 1e-3 | 60 | no | 0.29 | **0.38** | 0.00 | train-fit 84%; big train→val gap (domain/lighting) → augment next. acmissed 0 (only AutoCam labels) |
| B | 128 | 6 | focal | 1e-3 | 80 | yes | 0.53 | **0.50** | **0.13** | photometric aug = big win: veryfar 38→50, **acmissed 0→13 (recovers balls AutoCam missed!)**. fp rising (watch precision) |

_AutoCam baseline: all 76%, **veryfar 74%**, acmissed 0% (by definition). veryfar is the bar to beat._

**Read so far:** architecture works (84% train-fit); val 38% veryfar is generalization-limited. Levers,
in order of expected impact: (B) photometric augmentation (close the 84→38 gap), (C) bigger net + more
epochs, (D) more/lower-conf far labels, (E) motion channel, (F) gap pseudo-labels + self-training for
`acmissed` (the only path past AutoCam on the balls it misses).
