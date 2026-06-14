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

## Rollout

1. Land the warp module + the validation results (this branch).
2. Reolink ingest + far-ball labeling loop.
3. Pretrain (Dahua) → fine-tune (Reolink) → evaluate recall per game (target: beat v2's 0.29 substantially, and the
   reference tracker on far balls).
4. Swap the production `ball_detect` step from tiled inference to the warped full-frame model.
