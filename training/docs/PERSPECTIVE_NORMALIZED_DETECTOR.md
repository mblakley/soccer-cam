# Perspective-normalized, full-frame ball detector (v3 architecture)

**Status:** Proposed + validation experiment in progress (branch `feat/perspective-normalized-detector`, 2026-06-14).
Supersedes the fixed 7×3 native-resolution tiling approach for ball detection.

## Why

The v2 tiled detector has a **recall problem on small/far balls** — documented v2 recall ≈ **0.29** (vs 0.95
precision), i.e. it misses most balls, concentrated in the far field. Production gap analysis confirms it: the
detector simply never fires on far balls. Three compounding root causes:

1. **Row 0 (the far third of the field) is excluded from training** (`DEFAULT_EXCLUDE_ROWS = {0}`). The model has
   never seen a far-field tile, so it cannot detect far balls. Single biggest factor.
2. **Camera-domain skew.** Training data is ~81 Dahua games vs ~1 Reolink, but **production is Reolink**
   (7680×2160 panorama). The model barely knows the production camera, and far balls are exactly where the two
   cameras' geometry differs most.
3. **Tiling is slow and scale-locked.** 7×3 = 21 tiles/frame at native resolution; the model is locked to the
   640-tile ball-size geometry. Internal benchmarks show a **full-frame single inference is both faster and
   higher-recall** than the multi-tile path — tiling is a workaround for a model trained on small tiles, not a
   strength.

(Detailed reference-tracker comparison numbers live in the F: research archive, not this repo.)

## Core idea: warp the field to a uniform ball scale, then detect full-frame

Replace native tiling with a **perspective-normalized full-frame detector**:

1. **Anisotropic resample** driven by the per-game field homography (already produced by `field_detect`):
   **keep the far rows at (near-)native resolution; downscale the near rows** where a close ball has redundant
   pixels. Output is a single moderate-size image (target ≈ 3840×~1080) where a ball has **roughly constant pixel
   size regardless of field position**.
2. **One full-frame inference** on that image — no tiling. Fast, and a single-scale model fits a single-scale input.
3. **Inverse-warp** detections back to source pixels; the broadcast renderer is unchanged.

### Why this can exceed a strong reference tracker on far balls
A reference broadcast tracker squashes the whole frame to a uniform working width, so its far balls lose pixels
uniformly. Reolink is **7680-wide — far more native resolution than that working width**. By spending the resolution
budget on the **far field** (and reclaiming it from the near field via the downscale), our far balls carry **more
pixels than the reference ever sees** → we can resolve smaller / farther balls. That headroom is the whole point.

### Why this is faster
The near field — most of the frame area — is downscaled, and there is no tiling overhead (1 inference, not 21).
Far-field resolution, the only place we actually need it, is preserved.

## Cross-camera generalization: Dahua as supplemental data

The warp is also the **domain normalizer**. Both cameras image the same planar field; registering each camera's
view to the **same canonical field frame** (via its own homography) makes a ball at a given field position land at
the same place and scale in both — camera-specific resolution / FOV / mounting differences normalize away.

- **Dahua (81 games)** → supplemental **volume + variety** (lighting, ball colours, backgrounds, occlusions) → a
  more robust, higher-precision detector (attacks the false-positive-on-people problem) and a strong **pretrain
  base**. Its limit: lower native resolution caps its far-field value.
- **Reolink (16 games)** → the **far-resolution specialization** + production-domain fine-tune.
- **Future-proofing:** any new camera joins the training pool the moment `field_detect` finds its field — just its
  homography is needed. No per-camera models.

Recipe: warp everything → **pretrain on warped Dahua** → **fine-tune on warped Reolink** (+ row 0 + far-ball labels)
→ **camera-balanced sampling** (upweight Reolink + far rows so the production target dominates the gradient despite
Dahua's higher game count).

## Data + labeling

- Include row 0 (drop `DEFAULT_EXCLUDE_ROWS = {0}`); weight far-field positives ~4×.
- **Label far balls on the *warped* frames** in the helper app — far balls are bigger and uniform-sized there, so
  they are much easier to spot and click than on the raw 7680 frame. Labeling effort goes straight at the weak spot.
- Ingest the 16 Reolink games (register → warp → bootstrap-label → gap-mine → Sonnet-verify → human review).

## Training config (carry over from the v2→v3 analysis)

`yolo26l` (STAL + ProgLoss for small objects), `multi_scale=0.5`, `mosaic` 1.0→0.5, `copy_paste=0.3`, `cls` 0.5→1.5,
`cos_lr=True`, `lr0` 0.001–0.005, `patience=15`, freeze backbone 5–10 epochs, lower `hsv_v` (preserve ball/grass
contrast). Optuna sweep (20–30 short trials) before the full run.

## Validation experiment (this branch, before any retrain)

Run on the server (`training/experiments/perspective_warp.py`), inputs staged on G: for speed:

1. **Scale gradient.** Derive the far→near apparent-scale curve from the field homography (the detector's bbox sizes
   are unreliable — it emits ~fixed-size boxes). This sets the warp curve.
2. **Build the warp** and render sample warped frames for a Reolink game (05-27) and a Dahua game.
3. **Validate:** ball scale ~uniform across rows; far-field pixels preserved (no upscaling beyond native); warped
   output small enough for a single-pass inference; report dimensions + estimated speed vs the 21-tile path.
4. **Cross-camera overlap:** confirm warped-Dahua and warped-Reolink field geometry + ball-scale distributions
   align — i.e. Dahua is valid supplemental data.

Success → justifies the full retrain on warped, row-0-included, Reolink-inclusive data with the v3 config. The
quantitative "beats the reference on far balls" check is run separately and archived to F: (it invokes a
reverse-engineered reference detector and must not live in this repo).

## Experiment v1 findings (2026-06-14) — refines the levers

Ran `perspective_warp.py` on the Reolink 05-27 game. Key results:

- **The perspective gradient is small.** True ball diameter measured ~12–13px across the mid-field band with only
  ~1.1× far→near variation. The camera is a **high, wide, near-top-down center mount** (visible in the sample
  frames), not a low sideline camera — so far and near balls are nearly the same (tiny) size. **The "downscale the
  near field" half of the idea buys little here.**
- **The field is a narrow band.** Only ~600px of the 2160 vertical is field; the rest is sky/trees/**spectators**.
- **Far balls are tiny everywhere** (~5px at AutoCam's 3264 width; ~8px at 5120; ~12px at full 7680).
- **No-tiles is overwhelmingly cheaper:** a single full-frame pass on the cropped band is **~4–35%** of the 21-tile
  (8.6 MP) cost depending on target width.
- **Measurement caveat:** only 74 mid-field balls could be measured — our detector produces *no far-ball detections
  to measure* (the recall problem is self-reinforcing). A true far-gradient number needs ground-truth far positions
  (archived to F:, RE) or a geometric derivation from the homography.

**Revised lever priority (replaces the near-compression emphasis above):**
1. **Crop to the field band** (~600–800px strip) — drops sky/trees/spectators → fraction of the compute AND removes
   the #1 false-positive source (attacks the wrong-coords problem too).
2. **Keep the band at higher horizontal resolution than the reference** (5120–7680 wide vs its ~3264) → far balls
   carry 1.6–2.4× more pixels → the concrete path to beating it on far balls.
3. **Single full-frame inference on the band — no tiles.**
4. Vertical scale-normalization is now a minor refinement, not the headline.
Plus (unchanged): include row 0, far-ball labels, Reolink fine-tune, Dahua pretrain.

## Experiment v2 (2026-06-14) — the gradient IS real; v1 was measurement-biased

v1 concluded the gradient was small (~1.1×). **That was wrong** — a self-reinforcing artifact: v1 measured ball
size only where *our* detector finds balls (the mid-field), so it never sampled the far or near extremes. Re-measured
with a **recall-independent reference detector** (finds far balls our model misses): 489 balls spanning the full
field (rows 28–2082), true blob diameter by row:

```
row  462- 642 (far):   8.5px
row  642-1182:        11.5–12.0px
row 1182-1362:        21.0px
row 1362-1542 (near): 33.2px
```

**Far ≈ 8.5px, near ≈ 33px → a 2–4× gradient** (1.7× on conservative 20% percentiles; ~3.9× across the extreme
bands). So the **near-compression lever is restored**: the near field carries ~4× the ball pixels and can be
downscaled heavily while the far field is preserved. (Credit: domain review caught that "no far detections to
measure" had biased v1 flat.)

**Corrected lever set — both are real:**
1. **Crop to the field band** (drops sky/trees/spectators → compute + the #1 false-positive source). [v1, still valid]
2. **Anisotropic vertical warp inside the band** — compress near rows ~3–4×, keep far rows near-native → uniform
   ~10px ball everywhere → one training/inference scale, far preserved, compact input. [restored]
3. **Keep far-field horizontal width above the reference's** (5120–7680) so far balls keep more pixels than it sees.
4. **Single full-frame inference, no tiles.**
Far balls top out at ~8.5px native (an information ceiling — can't upscale detail in); the warp + wide far field put
more pixels on them than the reference's ~3.6px working size, which is the headroom to beat it.

## Far-ball label mining (velocity-gap heuristic) — how we exceed the reference

Critical realization: the reference tracker **also misses far balls**, so its detector cannot be our ground truth
for the far field (the experiment-v2 gradient is biased to balls it could find; the true far gradient is steeper).
The only way to get far-ball labels — and to train a model that sees *farther than the reference* — is to find the
moments no detector fires and have a **human** label them.

**The miner (validated on 05-27):** track the ball; for each detection **gap**, look at the pre-gap velocity. If the
ball was **moving toward the far touchline** (upward, `vy < 0`) and was last seen in the **far field** (upper rows),
the ball is almost certainly still out there — too small to detect. Those gap frames are far-ball examples.

Validation on the 05-27 trajectory (gaps ≥10 frames): **173 far-moving-then-lost gaps** (vs 349 other), last seen at
y≈537–644 (far third) — **21,235 frames ≈ 17.7 min of far-ball footage from one game**; far-gaps last median 2.5s
(p90 17s, max 52s). Across the 16 Reolink games that's ~4–5 h of targeted far-ball labels.

**Pipeline:** trajectory → far-gap miner (velocity-direction + far-field end) → extrapolate the ball's far-field
region during the gap → seed the helper-app labeling queue, **prioritized by far-gap length during active play** →
human labels the far ball → labels feed the warped, row-0-included training set. This is additive to the existing
gap mining (it adds the velocity-direction filter that *targets* far balls vs occlusions/fast kicks).

## Rollout

1. Land the warp module + the validation results (this branch).
2. Reolink ingest + far-ball labeling loop.
3. Pretrain (Dahua) → fine-tune (Reolink) → evaluate recall per game (target: beat v2's 0.29 substantially, and the
   reference tracker on far balls).
4. Swap the production `ball_detect` step from tiled inference to the warped full-frame model.

## Implementation status (2026-06-14)

Three modules landed on this branch (built in parallel, fully unit-tested, 44 tests green together):

- **`training/data_prep/far_ball_miner.py`** — the velocity-gap far-ball miner. `mine_far_ball_gaps(trajectory,
  field_band, config)` finds detection gaps where the ball was moving toward the far touchline (`vy < 0`) and was
  last seen in the far third, extrapolates the far-field search region across the gap, and emits a priority-ordered
  labeling queue (`candidates_to_queue` / `write_queue_json`) compatible with `flywheel/priority_queue.py`. Additive
  to the existing generic gap interpolation (which assumes the ball reappears nearby — far balls don't).
- **`training/data_prep/field_warp.py`** — the perspective input transform. `build_field_warp(rows, sizes, W, H, TW)`
  fits a monotone size(row) curve from a recall-independent ball-size gradient and precomputes the anisotropic
  vertical remap + the **inverse LUT**. `warp_frame()` crops to the band and compresses the near field (far never
  upscaled — information ceiling); `unwarp_points()` is the precise inverse used at inference to map detections back
  to source pixels. Round-trips sub-2px. Production-shape sanity: 7680×2160 → ~0.08 MP single warped input vs 8.6 MP
  for the 21-tile path (~114× fewer pixels/frame).
- **v3 dataset/config** (`training/data_prep/manifest.py`, `training/train.py`) — `DEFAULT_EXCLUDE_ROWS` is now `set()`
  (row 0 / far field included by default; explicit `exclude_rows={0}` still honored). New far-field positive
  multiplier (`FAR_POSITIVE_MULTIPLIER=4.0`), camera-balanced sampling (`compute_camera_weights`, anchors Reolink and
  scales Dahua so effective contributions balance despite the game-count skew), and the v3 hyperparameters
  (`yolo26l`, `multi_scale=0.5`, `mosaic=0.5`, `cls=1.5`, `cos_lr`, `lr0=0.002`, `patience=15`, `freeze=8`,
  `hsv_v=0.2`). Note: `organize_dataset.py` / `smart_sampler.py` carry their own `DEFAULT_EXCLUDE_ROWS={0}` copies and
  `tasks/train.py` / `train_v3.py` are separate train paths — mirror these changes there if the production run uses
  them.

### Reolink inventory survey (2026-06-14) — the data is mostly *unregistered*, not missing

`game_registry.json` lists **1** Reolink game, which framed the 81:1 Dahua:Reolink skew as a capture problem. The
filesystem tells a different story: **~17 Reolink (`RecM09…`) games sit on `D:\soccer-cam-storage`**, each as a
combined `…-raw.mp4` plus 13–25 raw segments, and **none of them are in the registry**. So the production-camera
training data already exists — the gating step is *ingest/registration*, not collection. These ~17 games (date range
2026.05.07 → 2026.06.10, mostly BU14 Guzzetta + a few Flash) are the far-resolution fine-tune set. Registering them
(with `video_format = reolink_segments` so camera-balanced sampling picks them up) is the next concrete action and is
what makes the v3 Reolink fine-tune + far-ball labeling loop possible.
