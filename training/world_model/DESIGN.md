# Ball world-model — design & decisions (research track)

Branch `research/ball-world-model`. Parallel, different-angle track to the per-frame
heatmap detector on `feat/perspective-normalized-detector`. External literature survey +
AutoCam comparison numbers live on `F:\archive\ball-detection-research\` (kept off the OSS
repo per policy); this doc holds **our** architecture and decisions.

## Thesis

Stop trying to win on appearance alone. Build a **physics- and game-aware world-model of the
single ball** and treat object detection as one noisy **measurement** feeding it. The ball obeys
hard constraints — one ball, no teleport, **fixed apparent size at a given field location**, capped
speed (drag), gravity + bounce, lives in the field polygon + 3-D dome, persists through occlusion,
leaves only via near-boundary restarts/goals — and the scene contains **other identical-looking
balls** (sideline jugglers, bench balls, adjacent-field games) that **only context, not pixels, can
reject**. The heatmap session proved a per-frame detector localizes well in a window (0.78 far-recall
> AutoCam 0.74) but collapses on full-frame search (0.29, 76% false-fire) and that a greedy tracker
can't rescue it. The world-model uses the levers it lacked: size-at-location prior, single-object
global trajectory, occlusion persistence, full-likelihood track-before-detect.

## Components

| Component | File | Status |
|---|---|---|
| Zero-touch field geometry (homography + geometric size prior + support) | `geometry.py` | ✅ done, 9 tests |
| World-model state estimator + track-before-detect (Viterbi MAP + switching PF) | `tbd.py` (planned) | next |
| Measurement adapters (detector peaks, bg-sub motion, classical blobs, player prior) | `measurements.py` (planned) | — |
| Virtual rectilinear "broadcast camera" views (see decision below) | reuse `dewarp_tiles.py` | planned |
| Shared eval (full-frame far-recall, track coverage, AutoCam-loses-ball + distractor clips) | `eval.py` (planned) | — |

## Decision: measurements need rectilinear "broadcast camera" views (2026-06-16)

**Context.** The strong published detectors (WASB, TrackNet, DeepBall, the 2025 single-camera
localizer) are trained on **rectilinear broadcast video**. Our source is a distorted 180° panorama
(fisheye/cylindrical). Feeding the pano (or even the anisotropically-warped band) to a
broadcast-trained net is a domain mismatch — lines curve, perspective is non-broadcast.

**Decision.** The measurement layer reprojects the pano into **rectilinear virtual broadcast views**
(pan/tilt/fov windows) so broadcast-trained detectors apply, mapping detections back to source/world
coords via the inverse projection. Reuse the existing cylindrical→rectilinear reprojection
(`dewarp_tiles.py`) rather than rebuilding it.

**Consequence — multiple passes.** A single rectilinear view can't cover 180° without extreme edge
distortion, so **full-field coverage needs several virtual-view passes** (tiling in *rectilinear*
space). This costs compute, in tension with the ≥0.3 fps base-hardware budget.

**Synthesis with the world-model.** In ROI-tracking deployment we render **one** rectilinear virtual
camera that *follows the world-model's predicted ball region* — broadcast-like (so the detector is in
its trained domain) **and** cheap (one small view per frame, full-field multi-pass only to
(re)acquire). This is the natural, mobile-friendly inference mode.

**Experiment axis (Phase 1).** Compare measurement front-ends: (a) detector trained on the
warped band (the heatmap session's domain), vs (b) **broadcast-pretrained detector on rectilinear
virtual views**. The world-model spine is agnostic to which wins; it consumes either as measurements.

## Decision: geometric size prior, not measured gradient (2026-06-16)

`field_warp.build_field_warp` fits the ball-size(row) gradient from *measured* detections (needs a
detector that already finds balls — self-reinforcing on the far field). For zero-touch we instead
derive `size(location)` **purely geometrically** in `geometry.py`: project a real 0.22 m ball at each
field location through the homography. Works on a brand-new game the instant the field is detected,
and gives the size-consistency discriminator that geometrically rejects look-alikes.

## Experiment log

### EXP-1 (2026-06-16): decisive pre-GPU — world-model TBD over champion-J's heatmaps

**Question:** does wrapping the *existing* champion-J heatmaps in the world-model (track-before-detect
+ physics) lift full-frame far-recall from the per-frame-argmax wall toward AutoCam's 0.74, with **no
retraining**? If yes, the spine is proven before any GPU training.

**Setup (isolated, on the idle GTX 1060 server `DESKTOP-5L867J8`):** `G:\ballresearch\dump_iron_peaks.py`
reuses J's exact warp/mask/model (`hm_J.pt`, base24) READ-ONLY from `G:\v4bench`, but stores the **top-12
peaks per frame** (not the global argmax) for every frame of the held-out Irondequoit clip (705 frames,
~1.7 s/frame on the 1060). `iron_eval.py` then runs the per-frame-argmax baseline vs `run_tbd` over the
consecutive-frame peaks and scores center-distance recall (R=20, splits all/veryfar/acmissed).

**Baselines (from the session's `hm_fulleval_results.jsonl`, same 162/131/39 GT):**
- AutoCam: all 0.76, **veryfar 0.74**, acmissed 0.0.
- champion-J full-frame *search* (global argmax): veryfar **0.07–0.30** (settings-dependent), false-fire ~76%.
- champion-J *track_oracle* (window anchored on the prev-frame GT — the greedy ceiling): **veryfar 0.473**.
- champion-J *gated_track* (real causal tracker): veryfar **0.0** (locks onto the strongest distractor).

The track_oracle 0.473 is the key number to beat: it's the ceiling a *greedy* per-frame window reaches.
Track-before-detect optimizes the whole trajectory at once, so it can in principle exceed it. **Result:
pending the full dump + eval (this run).**
