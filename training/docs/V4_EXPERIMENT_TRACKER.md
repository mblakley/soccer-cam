# v4 Ball Detector — Experiment Tracker

*Started 2026-06-15. Live tracker; append results as runs complete. Pairs with
PERSPECTIVE_NORMALIZED_DETECTOR.md (design) and DECISIONS.md (choices).*

## Goal / success metric

A single ball detector that **(a) beats AutoCam on far-field recall, per camera**, and
**(b) generalizes across cameras** (Dahua ↔ Reolink ↔ future cameras). We don't pre-commit to a
warp/resolution — we **run experiments, implement what wins, and iterate**.

**Primary metric (final, needs labels):** far-ball recall vs AutoCam, evaluated **per game + per
camera**, on a held-out set with human-verified far-ball ground truth.
**Cheap proxy metrics (pre-training, available now):** see Methodology.

## Critical path / gating

The primary metric needs **far-ball ground truth**, and **Reolink has zero ball labels** (the gate).
So the program is: down-select warp/resolution cheaply (geometry/throughput proxies) → run the
Reolink far-ball **labeling loop** → train+eval survivors on the GPU fleet → iterate.

## GPU fleet (fan experiments across all three via the pull-based work queue)

| Box | GPU | Role |
|-----|-----|------|
| DESKTOP-5L867J8 (server) | GTX 1060 6 GB (sm61) | data-prep, warping, eval, CPU work. **Slow for yolo26l fwd+bwd** at these frame sizes (smoke ran >18 min) — not the primary trainer. torch 2.6/cu124 env at `G:\v4bench\wt\.venv`. |
| jared-laptop | RTX 4070 (12 GB) | primary trainer |
| FORTNITE-OP | RTX 3060 Ti | trainer (yields for games) |

Remote workers see only D: via SMB → serve warped shards from D:, stage to local SSD (TaskIO).

## Methodology

**Warp variants under test** (the transform applied before detection; all crop to the field band
via the field-outline polygon, never the full-frame fallback):
- **W0 crop+isotropic** — crop band, uniform x/y resize to TW. Balls stay ROUND; sizes vary ~2–4×
  (rely on yolo26l + `multi_scale`). Simplest.
- **W1 aniso-vertical** — the landed `field_warp`: compress near rows so vertical ball *extent* is
  uniform. Uniform vertical size, but near balls become ~3.7:1 ellipses.
- **W2 homography-rectify** — use the field polygon to rectify the (curved, wide-angle) field to a
  canonical flat rectangle with uniform per-position **isotropic** scale (far never upscaled). Round
  + uniform + barrel-corrected + camera-canonical. Most work.

**Resolution knob:** `target_width` ∈ {3264 (≈AutoCam), 5120, 7680}. Train and infer must MATCH (or
FixRes fine-tune). Floor ≈ AutoCam's 3264 (below it we can't beat AutoCam on far balls).

**Cheap proxy metrics (no training):** per warp×TW — far-ball pixel size (vs AutoCam ~3.6px),
ball-size uniformity (CV across depth), ball roundness (median aspect), **cross-camera appearance
alignment** (Reolink vs Dahua ball size/shape distributions in the canonical frame), warped
megapixels (compute) + shard GB/frame (I/O).

**Final metric (trained):** far-ball recall vs AutoCam, per camera; precision; cross-camera held-out.

## Experiments

| ID | Hypothesis | Config | Metric | Status | Result |
|----|-----------|--------|--------|--------|--------|
| E1 | Establish real source-ball appearance (size/shape vs position) | reference coords, field-masked, conf>0.6, blob-filtered, 3000 frames | size/aspect vs (depth,x) | **done (inconclusive)** | **80% of reference detections are OUT of field (4171/5240 = FPs on sky/trees/spectators).** In-field balls are 2-4px → too few-pixel to measure shape (aspect on 4px = noise); near-ball segmentation failed. ⇒ can't down-select warps by pixel proxy. Field mask is mandatory for any bootstrap labels. |
| E2 | Pick best warp EMPIRICALLY (pixel proxy too noisy) | W0/W1[/W2] × TW, trained, recall vs AutoCam | recall vs AutoCam | **revised → train** | pivot: generate pre-warped standard YOLO datasets per warp×TW + bootstrap labels (field-masked reference dets + Dahua clean labels); train on fleet; compare. Simplest trainer path = standard ultralytics on pre-warped jpg+txt (no custom loader needed for v1). |
| E3 | Confirm each survivor feeds the GPU >80% util + iter timing | io_benchmark per config | util, ms/iter, GB/frame | partial | 1060 too slow for yolo26l (smoke >18min); move compute to 4070/3060Ti |
| E4 | Reolink far-ball ground truth (the gate) | run_ball_detector → far_ball_miner → warped web helper → human | far-ball labels | todo | Reolink = 0 labels |
| E5 | Train v4 per surviving warp×TW; beat AutoCam far-recall | train_v4 on 4070 + 3060Ti (fan via queue) | far-recall vs AutoCam, per camera | todo | — |
| E6 | Joint Dahua+Reolink generalizes (held-out camera) | camera-balanced joint train | per-camera recall, held-out | todo | — |

## Test assets

**Canonical far-field test clip** (the consistent footage every warp/model is evaluated on — the
ball on the far side, opposite the near-sideline camera):
- Source: `F:\Heat_2012s\2026.05.27 - vs Chili Vortex (away)\RecM09_DST20260527_182425_182924_0_9D288300000000_2E72363F.mp4` (seg00, raw 7680×2160).
- Range: **frames 2209–2834** (~112–143 s in; 19.8 fps). Ball sustained in the far third
  (median cy ~530–680; far touchline ≈ y 412). Window 2334–2584 = 87% far.
- Extracted clip: `D:\detect_work\v4_test_clips\farclip_0527_seg00_2209-2834.mp4`.
- Field polygon (this static camera): `G:\pipeline_work\test\2026.05.27-…\field_polygon.json`.
- Note: this camera is **strongly barrel/fisheye-distorted** (field bows into a bowl) — confirmed
  visually — so off-axis ball shape is position-dependent; the warp should ideally correct it.

## Log

- **2026-06-15** — Tracker created. Findings so far:
  - Field-outline polygon (ours + AutoCam) shows **strongly curved touchlines → wide-angle/barrel
    projection** → off-axis ball shape is position-dependent (most at edges, least at center).
  - The landed W1 (aniso-vertical) warp turns a round source ball into a ~3.7:1 near-field ellipse
    (synthetic, confirmed). `target_width` controls far-ball pixels (1280 default is wrong: ~1.4px
    far; 7680 → ~8.5px round).
  - First real-ball shape measure was **inconclusive** (measured detector false-positives on
    sky/spectators in the warmup minute). E1 needs the field-polygon mask + higher conf + ball-size
    blob filter + mid-game frames.
  - I/O smoke: yolo26l fwd+bwd is **very slow on the GTX 1060** at these sizes → 1060 = data-prep/eval,
    train on the 4070/3060Ti.
- **2026-06-15 (E1 done)** — Measured reference-detector balls on raw 05-27 (seg00, 3000 frames,
  field-masked): **80% of detections are off-field FPs**; in-field balls are 2-4px so shape is
  unmeasurable by pixel proxy. **Pivot: stop trying to pick the warp analytically — generate
  pre-warped YOLO datasets per warp×TW and compare trained recall vs AutoCam.** Next build:
  `warped_dataset.py` (warp variants W0 crop+iso / W1 aniso + map ball coords → standard YOLO
  jpg+txt) and a label bootstrap (field-masked reference dets for Reolink + Dahua's clean labels).
  Then fan training across the 4070/3060Ti and eval. Note: train_v4's custom-trainer scaffold is
  not needed for v1 — pre-warped jpg+txt trains with the stock ultralytics trainer.
