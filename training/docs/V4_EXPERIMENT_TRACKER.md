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
| E5 | Train v4 per surviving warp×TW; beat AutoCam far-recall | train_v4 on 4070 + 3060Ti (fan via queue) | far-recall vs AutoCam, per camera | baseline reviewed | **HONEST baseline (Mark frame-by-frame review): 35 FPs / 174 → 139 real → 73% coverage, 80% precision @conf0.25** (the 91% was 20% phantoms: shoes/heads/ref-flag/trees/netting/occlusion). v4 target = beat 73% coverage with far fewer phantoms. FP list saved. |

### Baseline review findings (2026-06-15, Mark frame-by-frame on `irondequoit_ac_review_zoom.mp4`)
35 FP frames: 60,120,136,144,232,264,268,272,300,304,312,328,332,336,340,348,352,356,380,384,392,396,400,412,440,512,520,528,568,572,576,588,592,596,608 (FP-on-people/objects + occlusion + far-goal netting + trees). Analysis vs these labels:
- **Confidence is THE discriminator** (real median 0.84 vs FP 0.39). conf≥0.50 → 95% precision keeping 122/139 reals; conf≥0.55 → 97%. **⇒ bootstrap labels use conf≥0.50** (FP pollution 20%→~5%), NOT conf 0.25. Regenerate the killed `gen_labels` with conf 0.5.
- **Field mask is NOT a useful FP filter here, and warns of a warp bug:** 108/139 *real* balls sit ABOVE the detected far touchline (real cy≈303 vs polygon far edge ≈391). ⇒ **the warp band (y_top from the polygon) would CLIP far-corner balls** — extend the far band ≥~90px above the touchline (track_field_margin 50px is too small). Investigate before warping.
- **Static-location clustering fails** (reals cluster more than FPs — the ball dwells in the far corner).
| E6 | Joint Dahua+Reolink generalizes (held-out camera) | camera-balanced joint train | per-camera recall, held-out | todo | — |

## Test assets

**Canonical far-field test clip** (Mark-specified; the consistent footage every warp/model is
evaluated on):
- Clip: `D:\detect_work\v4_test_clips\irondequoit_far_3352-3430.mp4` (38 s, 7680×2160, 19.89 fps).
- Game: **`heat__2026.06.04_vs_Irondequoit_away`** (BU14 Guzzetta vs Irondequoit).
- Source (trimmed raw): `D:\soccer-cam-storage\2026.06.04-18.24.08\…\bu14---guzzetta-irondequoit-…-raw.mp4`
  (also under `F:\Heat_2012s\2026.06.04 - vs Irondequoit (away)\`). Offset **33:52–34:30**
  (≈ frames 40234–40986).
- Content (Mark-verified): the ball goes to the **far-left corner**, then comes **back closer to the
  camera** → exercises far-field detection AND the depth transition (the hard far case + a near case
  in one clip). In-game 2nd-half play; golden-hour glare on the right.
- Camera is the same **strongly barrel/fisheye** Reolink (field bows into a bowl) → off-axis ball
  shape is position-dependent; the warp should ideally correct it.
- Field polygon: generated via the AutoCam keypoint model (below) →
  `D:\detect_work\v4_test_clips\irondequoit_field_polygon.json` (aggregate over frames for a
  complete polygon — single glare frame gave a partial 5-pt one).
- TODO before eval: run the **AutoCam reference detector** (balldet) on the clip to establish the
  baseline (where AutoCam detects/misses the far ball) that v4 must beat.

## Field polygons — SOLVED with OUR model

**Use our own field_outline v2 model: `F:\training_checkpoints\field_outline\field_outline_v2.onnx`**
(43 MB; the pre-encryption original of the published `field_outline-2.0.0.enc` — GitHub `.enc`
sha256 `058b287a…` matches STATUS.md, confirming it). It's the published model, no TTT key / no RE
concern. Drive it via `field_detector.py` (loaded by file path to skip the `video_grouper` package
init, which needs pydantic the bench venv lacks): input `input[1,3,384,768] fp16` →
`keypoints[1,10,2]` + `scores[1,10]`. Per-game polygon = run on ~15 sampled frames, aggregate
(median per keypoint where score≥0.5) → polygon. (The decrypted AutoCam teacher
`detect_kpts_fp16.onnx` is an identical-I/O drop-in if ever needed, but ours is preferred.)
Irondequoit polygon: 10/10 keypoints, but **ACCURACY is poor** — see below.

### ⚠ field_outline_v2 ACCURACY is the real problem (Mark review, 2026-06-15)
Mark verified the Irondequoit polygon frame-by-frame: the model placed the **far line ~250–400px too
low** (and near vertex 2/4 off). The aggregation fix gives a *complete* 10-pt polygon but **cannot fix
mis-located keypoints**. Since the v4 **warp band derives from this polygon, a too-low far line would
clip the far field** — directly hurting the far-ball goal. ⇒ **the field model needs accuracy work
(retrain / better far-line labels), and/or per-game polygons need human verification.** This is a new,
load-bearing item in the v4 plan. For the Irondequoit eval, the polygon is **hand-corrected** from
Mark's annotation (5 far-line dots + vertex 2→frame bottom) → `irondequoit_field_polygon.json`
(source `manual_corrected`); the AutoCam teacher (IoU 0.84 vs ours) is similarly off, so it's not a fix.

### Product behavior (Mark): viewport holds on out-of-play → in-field detection only
The camera viewport **holds position when the ball goes out of play**, so v4 only needs to
detect/track the ball **inside the field polygon** (OOB balls are ignored unless following out-and-back).
⇒ field-mask all labels + eval to the (correct) polygon: keep in-field, drop OOB + background. The
earlier "field mask backfires" was an artifact of the wrong/too-low polygon.

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
- **2026-06-15 (data pipeline unblocked)** — Built+validated `warped_dataset.py` (W0 crop+iso /
  W1 aniso + YOLO generator + field-masked bootstrap). **seg00 (warmup) banned from all use.**
  Resolved the field-polygon blocker via `detect_kpts_fp16` (AutoCam kpt model) + `field_detector`.
  Now-autonomous path to a real train→eval cycle: (1) per-game field polygon via detect_kpts;
  (2) in-game Reolink labels = balldet (have it) on in-game segments, field-masked; (3) Dahua labels
  from manifests; (4) generate W0/W1×TW warped datasets; (5) train on 4070/3060Ti; (6) eval on the
  Irondequoit clip vs the AutoCam baseline. No remaining hard external blocker except training-GPU
  speed (1060 slow; 4070/3060Ti are the trainers).
- **2026-06-15 (E2 executing — baseline LOCKED, comparison build running)** —
  - **Baseline debate closed.** Mark's frame-by-frame FP list (35 frames, above) is FINAL/ground
    truth: AutoCam = 139 real / 174 → **73% coverage, 80% precision**; conf≥0.5 → 95% precision
    (122/139). No further polygon/FP re-litigation — v4 must beat 73% far-coverage with fewer phantoms.
  - **Field polygon (Mark-verified):** Irondequoit far line confirmed by Mark's blue-dot overlay
    (`~/Documents/poly_verify_blue_dots.png`) — the 5 far-sideline vertices match within ~1px. Saved
    `irondequoit_field_polygon.json` (`manual_corrected`). 05-27 train uses `field_polygon_autocam.json`
    (autocam-kpt polygon; same Reolink camera family — good enough for a bootstrap; not re-derived).
  - **Single-pass builder** `e2_build.py` (scratch on `G:\v4bench`, uses committed `warped_dataset.py`
    helpers): decode each 7680×2160 HEVC frame **once** via PyAV (~13.6 fps; cv2.VideoCapture crashes
    on 16.6 MP HEVC), run balldet (CUDA), keep in-field conf≥0.5 balls, warp+write **both** W0 and W1
    YOLO datasets in the same pass. **Env fix (load-bearing):** onnxruntime-GPU needs the CUDA DLLs on
    PATH — prepend `<venv>\Lib\site-packages\torch\lib` (bundles `cublasLt64_12.dll`+`cudnn64_9.dll`);
    `G:\pipeline_work\fk\.venv` is the one env with CUDA-ORT + torch-CUDA + ultralytics + av + cv2.
  - **E2 datasets (building):** TW=3264 (=AutoCam floor, fits the 1060), 2 in-game 05-27 segments →
    train, Irondequoit clip → val (held out). Smoke: W0=3264×704, W1=3264×256, ~65 pos / 1200 frames.
  - **Next:** train yolo26n on `ds_w0` + `ds_w1` (imgsz=3264 matched, rect, 1060), then center-distance
    far-ball recall on the Irondequoit val vs the locked AutoCam baseline → pick the warp.
