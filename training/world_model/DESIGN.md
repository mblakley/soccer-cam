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
