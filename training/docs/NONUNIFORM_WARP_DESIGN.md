# Non-uniform camera warp — pipeline impact assessment

*Created 2026-08-17. Branch `design/nonuniform-warp-impact`. Assesses the proposal to rewrite the
Duo 3's VPE warp mesh so the panorama is sampled non-uniformly (magnify the far field, compress the
near field) in order to buy far-ball image quality upstream of the 20 Mbps encoder ceiling.*

Confidence tiers used throughout: **[M]** measured in this session, **[R]** read from repo/code,
**[D]** derived arithmetically from an [M] or [R] fact, **[I]** inferred (reasoning given).

---

## Verdict (read this first)

**A non-uniform warp is not worth doing for image quality, because the premise is false on this
camera: the far field is not undersampled.** Three measured facts, in descending order of force:

1. **The factory mesh already upsamples the far field.** Decoding the archived VPE 0 mesh
   (`F:\archive\duo3_stitch\dumps\lut\lut_vpe0.bin`, 267,288 B, parse validated against the ranges
   published in `reolink-firmware-patching/vpe/README.md`) gives a horizontal source-sampling rate
   over the field band of **0.63–1.04 source px per destination px**, mean 0.864 **[M]**. A rate
   below 1.0 means the sensor is being *stretched*, not decimated. The maximum rate anywhere in the
   field band is **1.044**, so the total real sensor detail recoverable by any mesh change is
   **≤ 4%**, and it sits in a column range that does not contain the smallest balls **[D]**. Every
   pixel of magnification beyond that is interpolation. The far ball is ~4 px because it is 60–76 m
   away behind a ~90°-per-sensor lens (`world_geometry.py:52-53` field 95×60 m **[R]**), not
   because the mesh is throwing pixels away.

2. **The proposed mesh shape is aimed at the wrong end of the field.** The brief assumes the
   panorama's horizontal edges are the distant ends of the pitch. Measured on a human-confirmed
   polygon (`heat__2026.06.04_vs_Irondequoit_away`), the **near** touchline spans panorama
   x 30–7380 (essentially the full width) while the **far** touchline spans only x 2163–5416 — the
   centre 42% **[M]**. The hardest ball in the frame (4.0 px expected diameter via
   `FieldGeometry.expected_ball_diameter_px`, `world_geometry.py:186-211`) sits at the far-touchline
   midpoint, **panorama x ≈ 3730 — the seam** **[M]**. The panorama edges hold the *near* corners,
   20–36 px balls. This is not a one-game artifact: `_polygon_ordering_ok` (`world_geometry.py:115-117`)
   *rejects* any polygon whose far line is not narrower than its near line, so far-line-inside-near-line
   holds for every polygon the pipeline accepts **[R]**. The symmetric sinusoid magnifies the near
   corners 2× (pure waste) and compresses the region at u≈0.4–0.6 by 1.5×, which is why simulating it
   moves the frame's worst ball only **+10%** (3.7 px → 4.1 px, the minimum simply relocating to
   x≈2340) with a magnification/need correlation of **r = +0.11** — essentially uncorrelated **[M]**.

3. **The panorama is already non-uniform and already anisotropic, and nothing in the pipeline models
   it.** The same mesh decode shows horizontal magnification swinging **1.65×** across VPE 0
   (1.58× at the outer edge, 0.96× at x≈2300, 1.47× at the seam — a symmetric bow) and a source
   circle rendering as an ellipse of **0.88:1 to 1.52:1** depending on column, a **1.72× aspect
   swing** **[M]**. The renderer assumes the exact opposite (`cylindrical_view.py:4-5`, "pixel
   columns/rows are linear in azimuth/elevation"). So the pipeline is *already* carrying an
   unmodelled position-dependent geometry error of this magnitude.

The correct reading of the demonstrated capability is therefore inverted from the proposal: **the
value of being able to read the mesh is the read, not the write.** The mesh is the first exact
measurement of the panorama's sampling function this project has ever had, and it is a live
candidate mechanism for the open "ONE BUG CLASS" (DECISIONS CURRENT STATE lines 15-20). Pursue the
read; do not ship a write for image quality.

**One genuine, separable hypothesis survives:** the mesh reallocates pixels *before* the encoder, so
even at zero sensor-information gain it could act as a geometric ROI, giving the 20 Mbps encoder
(`reolink-firmware-patching/docs/FIRMWARE_PATCH_NOTES.md:396`, ceiling in (20,21) Mbps for
16MP/h265 **[R]**) a physically larger far ball to preserve. That is an encoder question, not an
optics question, and there is already a **cheaper, targeted lever for it in flight** — branch
`firmware/encoder-roi-qp-per-game`, commit `ca8b88e` "reach the encoder ROI through the kernel"
**[R]**. Per-region QP buys the same encoder benefit with **zero** geometry change and therefore
zero cost in any of the three areas below. Prefer it.

---

## Is this ground already covered? Partly — and the covering docs are stale in one direction

**Yes, substantially.** Three existing decisions bear directly on this and must not be re-litigated:

- **`training/docs/DECISIONS.md:1235-1236` — "Isotropic everywhere is a hard rule now."** Any
  aspect-distorting resize is banned in all three preprocessing stages. Earned by **EXP-DIST-15**
  (`EXPERIMENTS.md:2892-2918`): a hardcoded 1600×448 resize squished 4096×1800 Dahua ~36%
  vertically and flipped **~16% of confident top-1 detections onto a different object**, with a
  systematic −8.1 px bias **[R]**. A horizontal-only mesh is locally aspect-distorting by
  construction and violates this rule as specified.
- **`training/docs/V4_EXPERIMENT_TRACKER.md:36-42` — the W0/W1/W2 warp bake-off.** W1
  (anisotropic vertical) was built, measured to turn a round ball into a ~3.7:1 ellipse
  (`V4_EXPERIMENT_TRACKER.md:124-126`), and **lost**. What shipped is W0, isotropic:
  `video_grouper/inference/iso_warp.py:20-31`, a single `scale = target_width / src_w` applied to
  both axes, docstring "round balls at a constant px-per-degree". `field_warp.py` (W1) is imported
  by nothing under `video_grouper/` **[R]**. The project has already chosen round balls over
  reallocated pixels, on evidence.
- **`training/docs/DECISIONS.md:1220-1223` — the pipeline already perspective-normalizes in
  software**, isotropically, at student-training time.

**Where the covering docs are stale:** `PERSPECTIVE_NORMALIZED_DETECTOR.md` and `TILING_PLAN.md`
are both pre-pivot (June and April respectively) and describe a 7×3×640 YOLO tile detector that is
no longer the product. The shipped detector is a heatmap U-Net over horizontal band strips
(`ball_detector.py:43-44`, `TILE_W=2560`, `TILE_OVERLAP=256`, one row, full band height) **[R]**.
Anyone reasoning about "re-tiling cost" from `TILING_PLAN.md` will cost the wrong thing.

**What is genuinely new here** and not in any existing doc: the factory mesh's measured sampling
profile, and the observation that it plausibly explains the open bug class. No repo doc contains a
measurement of the panorama's sampling function; `world_geometry.py:66-76` explicitly records that
the reprojection gate cannot tell a good polygon from a bad one because *even a good polygon* fits
the idealized rectangle only to ~250–500 px — which is exactly the residual an unmodelled 1.65×
sampling bow would produce **[I: consistent in sign and magnitude; not proven causal]**.

---

## Area 1 — Ball detection

**Where it lives.** `video_grouper/pipeline/steps/ball_detect.py` (step) →
`video_grouper/inference/ball_detector.py` (`detect_video_candidates`, line 235) →
`training/models/heatmap_net.py` (`HeatmapNet`, line 39). Small U-Net, base=24 champion, ONNX,
fully convolutional, H/W multiples of 8. Anchor-free by design: `heatmap_net.py:8-10` — "the ball
is 3–8 px, at or below a bbox detector's stride, so IoU-based detection collapses". **There are no
anchors and no box priors to invalidate** **[R]**.

**Input geometry.** `iso_warp.CropIsoWarp` — crop the field band, isotropic resize to
`target_width`. Uniform in both axes; a mesh change is invisible to it. It would silently pass
through the new geometry **[R]**.

**Does a mesh change move the input off the trained distribution? Yes, and on the axis the project
has already ruled out.** Not on size — the detector already spans a 4→33 px ball
(`warped_dataset.py:40-44` hardcodes the validated Reolink gradient `[8.5, 11.75, 21.0, 33.2]`
**[R]**) and σ is a fixed 4.0 (`heatmap_dataset.py:95`), with σ=3 and σ=5 both rejected
(EXP-DIST-52, EXP-DIST-58) **[R]**. The shift is on **shape**: a horizontal-only mesh makes the ball
elliptical, position-dependently. The factory mesh already imposes 0.88:1–1.52:1 **[M]**; composing
the proposed 2× edge magnification onto it pushes the outer edge to roughly **3:1** **[D]**. That is
the EXP-DIST-15 failure mode and the W1 rejection, arriving from the camera instead of from a resize.

A note on how the mesh is built matters here. `Lut2D.from_mapping` synthesises a mesh from scratch
(`reolink-firmware-patching/vpe/README.md`) **[R]**, and the factory mesh **is** the stitch
calibration (`APP_REPLACEMENT_DESIGN.md:215`, "Factory 257×257 mesh from `CamStitchPara`") **[R]**.
So a from-scratch mesh does not merely change sampling density — it **discards the per-unit lens and
stitch calibration**, which is not recoverable from a redistribution function. Any production mesh
must be the factory mesh *composed* with the redistribution, never a fresh mapping.

**Absolute-pixel constants that encode today's sampling** (all `[R]`):

| Constant | Site | Sensitivity |
|---|---|---|
| `PEAK_MIN_DISTANCE = 3` → 7×7 NMS dilation in band px | `ball_detector.py:45`, `:90-92` | Position-dependent under a mesh; merges/splits peaks differently by column |
| `blob_diameter(..., win=61)` (123 px window) | `ball_detector.py:101` | Measures `size_px`, which feeds the selector and the size prior |
| `FAR_MARGIN_PX = 400.0` | `ball_detector.py:47` | Band top offset; a vertical mesh component moves it |
| `field_band_from_polygon(margin=20)` | `iso_warp.py:57` | Band extent |
| gray3geo quantisation `clip(round(band_px*8),0,255)`, saturates at **31.875 band px** | `ball_detector.py:191-194` | 2× magnification pushes near balls (33 px today) through the ceiling |
| σ = 4.0 heatmap target | `heatmap_dataset.py:95` | Fixed-radius target vs a now position-varying ball |

**Retraining cost — quantified.** The good news is that **no human label is invalidated**. Labels
are stored as source-pixel `(x, y)` ball centres against archived video
(`heatmap_dataset.py:112-114`; `build_heatmap_crops` takes `labels: {frame_idx: (x, y)}`), and the
crop store is a *derived* artifact rebuilt by that function **[R]**. Old video keeps its old
geometry and its old labels stay correct for it. The ledger:

- Human clicks at risk of needing re-collection: **zero** for old footage. Total human investment
  on record is ~1,800 far-label rows + ~1,400 viewport views + 560 Pittsford views + 39 polygon
  confirms (`LABELING_LOG.md`) **[R]** — all safe, all still describing the video they were made on.
- Crop store to rebuild: `crops_reolink` = **76,875 crops** (71,332 train / 5,543 val / 45,164
  positive) from **15 Reolink games** (`STATUS.md:498-500`) **[R]**. Regenerable by re-running
  `build_heatmap_crops`; cost is decode + write, not labelling.
- Polygons: all **39 human-confirmed** polygons (DECISIONS 2026-07-22) become wrong for post-mesh
  footage and need re-confirmation per camera, **once**, not per game **[D]**.
- Detector retrain: one from-scratch run (hn-family runs are 20–40 epochs, early-stopped ~ep 9,
  `EXP-DIST-46`) **[R]**.

The real cost is not compute, it is **corpus bifurcation**: the archive splits into pre-mesh and
post-mesh geometry, and per DECISIONS 2026-07-23 (b) generalization is judged by *geometry
distance*. A mesh change manufactures a new geometry cluster containing only future games, on a
fleet whose LOO-NN median is already 0.42 (DECISIONS CURRENT STATE line 53) **[R]**. You would be
adding coverage debt to attack a coverage problem.

---

## Area 2 — Tracking / selection

**This area is the most robust of the three, and for a specific structural reason: it does not work
in pixels.** `ball_tracker.py:25-27` — "Operates in **world coordinates** (via the homography) so
distances are perspective-fair". Candidates are converted at ingest (`:501-503`
`geom.image_to_world(xy)`), the Kalman state `[x, y, vx, vy]` is in metres (`:409-415`), and
`rerank` hard-fails without a valid homography (`:491-492`), as does the step
(`ball_select.py:102-108`) **[R]**.

Consequently the physics gates are already invariant to pixel sampling: `ball_vmax_mpf = 2.5`,
`air_vmax_mpf = 2.0` — metres per source frame (`ball_tracker.py:103`, `:116`) **[R]**. The
brief's concern — "constant real-world velocity becomes non-constant pixel velocity" — **is already
solved**, and was solved for exactly this reason: the loose global pixel gate it replaced produced
"0 miss entries, 1328 teleports" on a full game (`ball_tracker.py:105-108`) **[R]**.

The single pixel constant in the shipped gate, `phys_sigma_px = 5.0`, is **mapped through the local
homography Jacobian into metres** before use (`ball_tracker.py:506-521`, probe `d_px = 3.0`) — so
it is automatically position-adaptive and would absorb a mesh change *provided the homography is
correct* **[R]**.

**So what actually breaks here is not the tracker — it is the homography under it.** And that is
the load-bearing risk in this entire assessment:

- `build_field_geometry` fits a **planar projective homography** (`world_geometry.py:318`,
  `cv2.findHomography`) from the 10 touchline points, assuming they are equally spaced at
  0/0.25/0.5/0.75/1.0 (`_touchline_world_points`, `:123-134`) **[R]**.
- A mesh warp is **not a projective transform of the panorama** — it is a free-form 257×257 mapping.
  Composing one with a homography does not yield a homography. The fit will still *succeed*, because
  `MAX_REPROJ_ERROR_PX = 1000.0` is documented as "a CATASTROPHE gate only" (`world_geometry.py:64-76`)
  **[R]**. **It will pass and return silently wrong metres.** Every metre-denominated gate above then
  degrades with no alarm.

**Answering the coordinator's projective-mesh variant directly.** The proposal — make the density
reallocation itself a projective transform so the existing homography absorbs it — is sound in
principle and fails in practice, on two independent grounds:

1. *It cannot deliver the shape wanted.* A 1-D projective (Möbius) rescale `h(u) = (au+b)/(cu+d)`
   is monotone with a **monotone** derivative: it can magnify one edge and compress the other, but
   it cannot magnify the middle. The magnification actually needed peaks at the far-touchline
   midpoint (panorama x ≈ 3730 **[M]**), which is the *interior* of VPE 0's half. A projective mesh
   is structurally the wrong function family for this field geometry **[D]**.
2. *The homography would not absorb it anyway.* The composition is only clean if the pre-existing
   mapping is projective, and it demonstrably is not: `reolink-firmware-patching/vpe/README.md`
   records that a free 16-parameter cylindrical+Brown-Conrady fit to the real mesh **stalls at
   26.8 px RMS / 133 px max**, and that the mesh "bulges outward horizontally but inward
   vertically, which no radially symmetric map can do" **[R]**. My decode independently reproduces
   the non-separability: horizontal rate swings 1.65× while vertical rate swings only 1.08× over
   the same band **[M]**.

On the isotropy question: **a projective stretch is still aspect-distorting** in the sense
EXP-DIST-15 meant. Its local Jacobian is anisotropic everywhere except at the fixed point; that is
what "stretch" means. It is *smoothly* anisotropic rather than uniformly squished, which makes it
less damaging than the 1600×448 bug but does not exempt it from the rule.

**Geometry-agnostic in this area:** the Viterbi lattice structure, the static-persistence penalty
(`cell_m = 2.0`, world cells), the OOB pin, the aerial cone, the selector's listwise architecture,
`miss_cost`, and every metre-denominated constant — all correct *by construction* once the
image→world map is right **[R]**.

**Not geometry-agnostic:** `offfield_margin_px = 30.0` (`ball_tracker.py:131-133`, genuine pixel
constant used with `cv2.pointPolygonTest`), the selector's `size_ratio` and `depth` features
(`ball_selector.py:40`, `:43`, the latter normalised by a hardcoded /20), and `upsample_track`'s
linear pixel interpolation (`camera_planner.py:184`) **[R]**.

---

## Area 3 — Broadcast dewarping

**The renderer assumes uniform sampling explicitly, in a docstring, and the assumption is already
false.**

`cylindrical_view.py:4-5`: "a stitched ~180° panoramic frame whose pixel columns/rows are linear in
azimuth/elevation about the *camera* axis." The sampling identity appears in five places **[R]**:

| Site | Code |
|---|---|
| `cylindrical_view.py:167-168` | `map_x = (az/src_hfov + 0.5)*src_w`; `map_y = (el/src_vfov + 0.5)*src_h` |
| `cylindrical_view.py:224-225` | identical, Numba kernel |
| `cylindrical_view.py:310-311` | `pixel_to_yaw_pitch`: `yaw = (px/src_w - 0.5)*src_hfov_deg` |
| `cylindrical_view.py:326-327` | `yaw_pitch_to_pixel`, the exact inverse |
| `cylindrical_view.py:502-505`, `:513-518` | `build_leveled_pano` at a **constant** `deg_per_px = src_hfov_deg/src_w` |

Fed by `render_src_hfov_deg = 180.0` (`render.py:74-75`) — i.e. a flat 42.67 px/degree across 7680
**[R]**. Measured against the mesh, the true rate varies by **1.65×** across one half **[M]**.

Second-order but load-bearing: `field_world_up` (`cylindrical_view.py:343-378`) recovers the mount
tilt by fitting **great-circle plane normals** to the touchlines through `pixel_to_yaw_pitch`. Under
non-uniform sampling a straight world line is not a great circle in pixel space, so the vanishing-point
solve is biased, and the bias feeds `mount_tilt_from_up` → every frame's leveling roll **[D]**.

The planner carries an **independent second copy** of the same assumption with `180` hardcoded:
`camera_planner.py:111` (`speed / (src_w/180.0)`), `:120` (`speed_degf`), `:134`
(`view_w_px = src_w * (hfov/180.0)`) **[R]**. A mesh change makes the dead-ball speed threshold and
the zoom curve position-dependent. Note these are two separate copies — a mesh-aware fix must
change both or they will disagree.

**What the renderer would need to know:** the destination→source mesh as a resampling function, and
an inverse of it, applied before `pixel_to_yaw_pitch` and after `yaw_pitch_to_pixel`. **There is no
2-D inverse-mesh code anywhere in the repo** — `field_warp.py`'s `inv_lut` is row-only
(`field_warp.py:232`, `map_x` is identity in x at `:238`) **[R]**. This would be new code in the
one module that is byte-parity-proven against every clip Mark has reviewed (STATUS 2026-07-10,
px-max=0 over 320 frames) **[R]**.

**The important corollary: this work is worth doing regardless of whether any mesh is ever
written.** Making the renderer and the world model consume the measured mesh instead of assuming
`180.0` linear is a strict correctness improvement against a defect that exists **today**.

---

## The asymmetry problem — the premise is inverted

The brief states that VPE 0's two edges are "the seam at the panorama centre (near field, where
magnification is not wanted)" and "the far end (where it is)". **Measurement says the opposite**
**[M]**:

| VPE 0 edge | Panorama x | What is there | Hardest in-field ball |
|---|---|---|---|
| u = 0 (outer) | 0 | near corner, closest ground | **20.3 px** |
| u = 1 (seam) | 3840 | far-touchline midpoint, most distant ground | **3.7 px** |

A sideline-mounted camera at the halfway line sees the far touchline subtend only the central ~42%
of its azimuth range, while the near touchline runs out to both extremes. The seam is therefore the
*single most valuable* piece of the panorama, and the outer edges the least.

Three consequences:

1. A production mesh should be **monotone toward the seam**, not a symmetric sinusoid: compress the
   outer edge, magnify toward u=1. With FOV preserved (∫₀¹ du/M(u) = 1), a linear-in-1/M ramp gives
   2.0× at the seam for 0.67× at the outer edge **[D]**.
2. **The seam is the worst possible place to put a magnification gradient.** It is where the two
   sensors join, where `stitch_remap.py` applies its per-row `dx` correction, and where any
   left/right sampling mismatch is directly visible. Magnifying *into* the seam from one side only
   — which is all VPE 0 can do, since VPE 1/2/3 carry no mesh — creates a **discontinuity in
   magnification exactly at the join**: 1.47× factory on the left of the seam meeting whatever the
   right half does **[M/D]**. That is a visible tear in the most-watched region of the frame.
3. The existing `stitch_remap` profile (`source_width`, `seam_x`, `dx_anchors`,
   `stitch_remap.py:29-51`) is calibrated against the factory mesh and is **invalidated
   immediately** by any mesh write **[R]**. It is per-camera, pushed from the TTT calibration tool
   (`stitch_remap.py:10-15`), so re-measurement is an operational step for every installed unit.

---

## Migration order (if it were done anyway)

Ordered by what must ship together vs what is independently safe.

**Independently safe, do these regardless — they fix a current defect:**

1. Land the mesh **read** as a calibration artifact: dump VPE 0, store the sampling profile per
   camera. No pipeline change, no retraining, no camera write.
2. Test the mesh against the open ONE BUG CLASS: correlate the measured sampling bow with the
   per-position ratio field already cached in the Phase 2 dumps (`F:\archive\geodet_phase2`). Zero
   GPU, zero labelling. If it explains the ±35%, that is a fix to a live problem.
3. Replace `render_src_hfov_deg = 180.0` linear with the measured profile in `cylindrical_view` +
   the planner's two copies. Gate on the existing byte-parity render test.

**Must ship together (one atomic cut-over) if a mesh is ever written:**

4. Mesh write + `stitch_remap` recalibration + polygon re-confirmation for that camera. All three
   describe the same geometry; shipping any one alone yields silently wrong metres.
5. World model: replace the planar homography with a mesh-aware image↔world map, **and** tighten
   `MAX_REPROJ_ERROR_PX` from its catastrophe-only 1000.0 to something that can actually detect
   non-projectivity. Without step 5, step 4 fails silently — this is the single highest-risk
   dependency in the plan.
6. Detector: rebuild the 76,875-crop store from archived video (labels unchanged), retrain, re-gate
   on product viewport capture vs AutoCam per DECISIONS CURRENT STATE line 41.

**Never independent:** 4, 5, 6. A mesh write with a stale homography passes every existing guard.

---

## The cheapest experiment

**It has already been run, and it is the mesh decode in this document.** Cost: reading one 267 KB
archived file. It answers the primary question — is the far field undersampled? — with **no: rate
≤ 1.044 across the field band, ≤ 4% real detail available** **[M]**. No camera access, no
retraining, no footage processing. Any further spend should be justified against that number.

**If the encoder hypothesis is still to be tested** (the one surviving mechanism), the cheapest
decisive form uses only existing recorded footage and no camera write:

1. Take the canonical far-field clip already frozen for this purpose:
   `D:\detect_work\v4_test_clips\irondequoit_far_3352-3430.mp4` (38 s, 7680×2160, ball goes to the
   far corner and returns — `V4_EXPERIMENT_TRACKER.md:75-82`) **[R]**.
2. Resample it through the candidate mesh in software, re-encode at the camera's real 20 Mbps
   h265 ceiling, then resample back to the original geometry.
3. Run the champion detector (hn4) on baseline vs round-tripped, and score far-ball ceiling/argmax
   against the existing frozen GT.

This is one-sided and therefore conclusive in the direction that matters: the software round trip
can only *lose* information relative to a real mesh, so **if the round-tripped clip is not
materially worse than baseline, the geometry change is survivable; if the round trip is not
materially better than a plain 20 Mbps re-encode of the baseline, there is no encoder benefit to
buy.** Both readings come from the same run. Est. a few hours on the 4070, zero human clicks.

Before spending even that, price it against `firmware/encoder-roi-qp-per-game` (`ca8b88e`), which
targets the same encoder benefit directly and breaks nothing.

---

## What breaks vs what is geometry-agnostic

**Breaks immediately, silently (no guard fires):**

- `world_geometry.build_field_geometry` — fits a homography to a non-projective mapping; passes the
  catastrophe gate (`:64-76`, `MAX_REPROJ_ERROR_PX = 1000.0`) and returns wrong metres. **The single
  most dangerous item.**
- `expected_ball_diameter_px` (`:186-211`) and `size_consistency_logprob` (`:213-235`) — the
  geometric distractor rejector, wrong by the sampling error.
- Every metre-denominated tracker gate — correct in form, wrong in value, via the homography.
- `field_world_up` / `mount_tilt_from_up` — vanishing-point solve biased by non-great-circle lines.

**Breaks immediately, visibly:**

- `stitch_remap` profile (`:29-51`) — per-row `dx` calibration no longer registers the seam.
- Renderer output geometry — `cylindrical_view.py:167-168`, `:310-311`, `:326-327`, `:502-518`.
- Planner units — `camera_planner.py:111`, `:120`, `:134`.
- gray3geo channel — saturates above 31.875 band px (`ball_detector.py:191-194`).

**Invalidated as data:**

- 39 human-confirmed polygons, for post-mesh footage only.
- The `crops_reolink` store (76,875 crops) — regenerable, not re-labellable.
- Detector weights (hn4) and selector weights (v7) — retrain, not relabel.

**Geometry-agnostic (survives untouched):**

- All human ball labels and viewport GT — stored as source pixels against archived video, which
  does not change (`heatmap_dataset.py:112-114`).
- `HeatmapNet` architecture — anchor-free, fully convolutional, no box priors
  (`heatmap_net.py:8-10`, `:41`).
- Band tiling — `TILE_W = 2560` / `overlap = 256` is a VRAM strategy, not a geometry assumption
  (`ball_detector.py:204-232`).
- `CropIsoWarp` — uniform in both axes; passes any geometry through unchanged (`iso_warp.py:20-54`).
- The Viterbi lattice, static-persistence world cells, OOB pin, aerial cone, miss-state machine —
  all metre-denominated and correct once the image→world map is right.
- The selector's listwise architecture (its `size_ratio`/`depth` *features* are not, but the
  network is).
- `BandStabilizer` — measures translation empirically per frame, assumes nothing about sampling
  (`iso_warp.py:108-229`).
- The pipeline step contracts (`candidates/2`, `trajectory.json`, `camera_path/1`) — coordinate
  spaces are named, not assumed.

---

## Appendix: reproducing the measurement

```
F:\archive\duo3_stitch\dumps\lut\lut_vpe0.bin   267,288 B = 8 B header + 257 x 260 x u32
entry = (y << 16) | x, each half unsigned Q14.2 (value/4 = source px)
decoded: source x 4.50..3398.00  y 17.50..2154.75, rows/cols 100% monotonic
         (matches reolink-firmware-patching/vpe/README.md off-camera figures exactly)
```

Destination taken as 3840x2160 (the panorama half). Self-consistency check: the mesh's per-row
source span averages **3318 px** of the sensor's 3840 available columns, written to 3840
destination columns — a net **1.16× upsample**, consistent with a 3840-wide destination and with
~450 sensor columns held back as stitch overlap **[D]**. Had the destination been ~3318 wide the
mean rate would be 1.0, but that contradicts the 7680-wide two-sensor panorama. The *shape* results
(1.65× magnification swing, 1.72× aspect swing, symmetric bow) are ratios, invariant to this
choice; only the absolute "already upsampled" claim depends on it, and that is what the check
closes.

Per-band figures quoted above use destination rows 36–255 (panorama y 300–2150, the field band):
horizontal rate 0.634–1.044 (mean 0.864), vertical rate 0.899–0.975 (swing 1.085×), aspect
0.88:1–1.52:1.

Field-position arithmetic used `heat__2026.06.04_vs_Irondequoit_away`'s human-confirmed polygon from
`game.json` on F:, through the shipped `build_field_geometry` / `expected_ball_diameter_px`.
