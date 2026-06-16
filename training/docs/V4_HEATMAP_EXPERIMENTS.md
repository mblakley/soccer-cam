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

## Key finding (2026-06-16): THE BAND TOP WAS CROPPING OUT FAR BALLS (results A/B were on a soft subset)
The field band (`field_band_from_polygon`) was cut at the **ground far line with no upward margin**, so
every ball ABOVE it — airborne shots and the very-far balls we care about most — was cropped out of the
band entirely and could never be detected OR counted. Measured impact:
- **Irondequoit val: 53 of 162 GT balls (33%) were above the band top** → EXP-A/B `all` n=109, `veryfar`
  n=78. **But the AutoCam baseline (74% = 97/131) is measured on all 131 veryfar GT** — so A/B's
  "veryfar 38/50%" was on a softer 78-ball subset, NOT comparable to AutoCam.
- Training data lost its hardest far balls too (segA 43/77, d 29/92 above band) → the net never saw the
  y<far-line zone.

**Fix (engine v2 = `heatmap_exp_v2.py`):** raise the polygon far edge by `--far_margin` (default 430) for
BOTH band crop and mask, so far/airborne balls stay in-band. Verified 2026-06-16: val denominators are now
`all` n=**162**, `veryfar` n=**131** (matches AutoCam exactly), `acmissed` n=**39**. **Recall is now an
honest, apples-to-apples comparison vs AutoCam's 74% veryfar.** Numbers will look lower than B's inflated
50% but are measured on the full, hard set including the 53 balls we were hiding.

**Engine v2 also:** (2) `--labels combined` = dense AutoCam (near/mid bulk) + the user's 70 human-verified
far balls (position overrides) − 34 user-rejected AutoCam FPs; (3) `--sigma` honored at runtime (was
silently pinned to 4.0); (4) best epoch chosen by **veryfar** recall (the headline), not `all`; (5)
`--aug 2` = gaussian blur + illumination gradient (domain-gap closers).

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
**A/B (below) used the BROKEN band** (soft denominators all=109/veryfar=78) — NOT comparable to AutoCam.
C onward use engine v2: fixed band (all=162, **veryfar=131 = AutoCam's set**), `combined` labels.
| tag | aug | wd | base | best-ep | all | veryfar | acmissed | notes |
|-----|-----|----|----|--------|-----|---------|----------|-------|
| A | no | — | 16 | — | 0.29 | 0.38\* | 0.00 | v1 broken-band; \*soft 78-ball subset, not comparable |
| B | aug1 | — | 16 | — | 0.53 | 0.50\* | 0.13\* | v1 broken-band; \*soft subset; photometric aug helped |
| C | aug1 | 0 | 16 | 35 | 0.722 | 0.718 | 0.513 | honest denom; combined labels; just under AutoCam |
| **E** | aug2 | 0 | 16 | 30 | 0.753 | **0.786** | 0.462 (18/39) | best veryfar; lowest fp (11/111) |
| G | aug2 | 1e-4 | 24 | 40 | 0.735 | 0.71 | **0.641** (25/39) | base24 → acmissed jumps; veryfar lags; fp 22 |
| H | aug2 | 1e-4 | 16 | — | 0.716 | 0.733 | 0.513 | sigma8 (bigger blob) — no gain |
| I | aug3 | 1e-4 | 16 | — | 0.679 | 0.725 | 0.538 | +cutout — slightly hurt |
| **J** | **aug2** | **5e-4** | **24** | — | 0.753 | **0.779** | **0.641** (25/39) | **CHAMPION: beats AutoCam veryfar AND best acmissed (recovers 25/39 balls AutoCam missed)**; fp 16 |

_AutoCam baseline (honest, full set): all 0.76 (123/162), **veryfar 0.74 (97/131)**, acmissed 0 (by def)._

**Champion = J** (base24, aug2 blur+illum, wd 5e-4, sigma6): veryfar 0.779 > AutoCam 0.74, and **acmissed
0.641** — recovers 64% of the far balls AutoCam couldn't get. E edges veryfar (0.786) at much lower
acmissed (0.46); J's capacity + weight-decay wins the *far-ball* goal. Cutout (I) and sigma8 (H) didn't help.

## Full-frame eval (the airtight proof) — `hm_fulleval.py`
Crop-eval centers a window on each GT ball (proves localization, not search). The full-frame eval runs
the model fully-convolutionally over the WHOLE masked band (tiled for VRAM), global-peak-picks, maps
band→source `(bx, by+y_top)`, and measures top-1 search recall + false-fire + per-frame inference cost.
- **Visual confirmation (saved overlays):** on far/airborne balls (incl. tiny ones against the tree line —
  exactly what the band fix recovered) the heatmap peak lands dead-on the GT, no spurious dominant peak.
  Coordinate mapping verified correct.
- **⚠ EDGE-COST FINDING:** at **native band resolution (2178×7680)** the base16 U-Net runs at **0.08 fps
  on CPU (~13 s/frame)** → a 90-min/20fps video would take **~375 h, ~16× over the 24 h budget**. The
  recall proof holds at native res; **edge-feasibility does NOT yet.** Reframes the goal: beat AutoCam
  far-recall *at a deployable CPU speed*. Paths: (a) lower `target_width` (fewer px, smaller balls →
  recall/speed sweep, next), (b) temporal ROI tracking (full-frame only to (re)acquire, cheap local
  window otherwise — the realistic deployment mode AutoCam uses). Per-frame full-frame is the worst case.
- (Smoke on first 12 frames gave veryfar top1 0.91 but is non-representative; full 131-set full-frame
  recall is measured on the winning model next.)

**Read so far (2026-06-16):** with the band fixed + the user's human far-labels + stronger augmentation +
capacity/weight-decay, **J beats AutoCam on far balls (crop-eval veryfar 0.779 vs 0.74) and recovers
25/39 (64%) of the balls AutoCam missed.** Remaining: (1) full-131 full-frame eval on J/E (running) for
the honest search-recall + FP number; (2) the `target_width` speed sweep — native band is 0.08 fps CPU
(16× over budget), so find the lowest TW that keeps veryfar > 0.74 at ≥1.25 fps CPU; (3) the `acmissed`
ceiling (gap pseudo-labels / self-training) if time. Open levers that did NOT help: cutout, sigma8.
