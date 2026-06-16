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

## Decision: the game ball is a ROLE, not a fixed object — track the in-play ball with handoff (2026-06-16)

**Context (Mark).** The strongest signal that the tracked ball is no longer the game ball is that
*another* ball has been in play on the field for a significant time. Normal scenario: the game ball goes
out of play (kicked out of bounds, shot over the goal); rather than retrieve it, a player puts a
*different* ball into play (from a ball-boy, or one resting on a sideline cone); the original ball is
abandoned out of play and the new ball is now the game ball.

**Decision.** Relax the strict single-physical-object assumption. The "game ball" is a **role** assigned
to whichever ball is in **active play**, and the role can **hand off** between physical balls:
- **Multi-hypothesis:** maintain trajectories for all ball-like candidates, not just one.
- **Role = in-play:** at each moment the game ball is the hypothesis best satisfying *active play* —
  in-bounds (field+dome support), engaged by players (R4 action/possession), moving with game dynamics —
  not merely "the continuous trajectory from before."
- **Handoff requires the game ball to leave the field of play first (the gate).** A swap is only
  *possible* once the current game ball is kicked **out of bounds** (outside the field+margin support)
  and abandoned. While it stays on/at the field — including in-field restarts (free-kick, PK, goal-kick)
  where the ball is static but in play — the **same ball continues**, no handoff. Once the ball is out of
  play, two resolutions: (A) the same ball is returned to play (the usual case — throw-in, retrieved) →
  no switch; (B) a *different* ball is **sustainedly** played in (seconds) while the original stays out →
  transfer the role. Hysteresis on (B) avoids flicker. Net: a second ball appearing while the game ball
  is still in play never triggers a switch — only an out-of-play exit can.
- **Intruder balls (brief in-field non-game balls).** A ball kicked onto the field from the sideline or
  an adjacent field, then collected and removed quickly. Handled by the same two rules with no special
  case: it never wins the role because (a) the real game ball is usually still in play (the gate stays
  shut), and (b) the intrusion is brief (fails the sustained-in-play hysteresis). It's tracked as a
  transient hypothesis that dies out. Its trajectory also *originates from outside* the field (enters
  across the boundary and leaves), unlike the game ball's continuous in-field path — an extra
  distinguishing cue.

**Why it matters.** This is the principled form of "single object, no teleport": *within* a hypothesis,
no teleport; *across* hypotheses, a sustained in-play handoff is allowed. It leans on the player/action
prior (R4), field support, and the restart/out-of-play modes. For the viewport goal it's exactly right —
follow the *in-play* ball / developing action, never an abandoned ball sitting off-field. The current
`tbd.py` is single-hypothesis (correct for a clip with no swap); multi-hypothesis + role assignment is a
layer above it (Phase 2 — does not affect the current far-ball EXP-2).

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

## Decision: viewport/area recall is the real metric, not pixel-exact (2026-06-16)

**Context (Mark):** "I don't really care if we know exactly where the ball is — I just want the rendered
viewport to be looking at the area the ball is in." The renderer crops a large window from the 7680-wide
pano, so a prediction off by 50-150 px still shows the ball; only a jump to a *different area* (a sideline
ball, a player cluster across the field) actually breaks the shot.

**Decision.** The primary metric is **area recall** — is the ball within the rendered viewport (≈ a few
hundred px) — plus **area stability** (a smooth track that doesn't jump areas). Pixel-exact recall
(R=20, AutoCam's 0.74) is kept only as a precision *diagnostic*. `iron_eval.py` reports veryfar recall
across a radius sweep (R = 20/50/100/200/400) so we read the area metric directly; R≈200-400 is the
renderer-relevant target. Implication: J's *adjacent-player/line* false-fires (near the ball) are largely
harmless at viewport scale — the world-model's job narrows to preventing **wrong-area** jumps and keeping
the track smooth, which is exactly what continuity + support + TBD do. (A proper viewport simulation —
the renderer's actual crop size + smoothing, scoring ball-in-crop % — is the eventual metric; the radius
sweep is the first-cut proxy.)

## Decision: anti-overfitting experiment protocol (2026-06-16)

**Context.** Training will be short/small (fast 4070 iteration, limited human-labeled games). The classic
trap: frames *within* a game are near-duplicates (same field/ball/lighting/teams, consecutive frames
almost identical), so a frame-level train/test split leaks and inflates metrics; and small data + many
epochs overfits to one game's specifics.

**What protects us structurally:** the world-model spine (geometry + physics + TBD) is **analytic — zero
trained parameters** (the homography/size-prior is derived per game from the polygon; physics is
universal). Overfitting risk is confined to the *detector* (measurement layer). And because the
world-model **amplifies a weak detector**, pipeline-level improvement shows up under *light* training —
we don't need long runs to read signal.

**Protocol (locked in):**
1. **Split by game, never by frame. Leave-one-game-out (LOGO).** A frozen held-out game list — including
   the AutoCam-loses-ball + distractor clips — that never appears in training. Report per-held-out-game.
2. **Tiny trainable surface, short schedule.** Fine-tune a small/pretrained detector (DeepBall/WASB),
   frozen backbone + small head, few epochs, **early-stop on a held-out game**, not held-out frames.
3. **Iterate the analytic world-model on cached peaks (no GPU).** Precompute detector peaks once per
   game; sweep world-model params offline. Retrain the detector only when the analytic side plateaus —
   keeps GPU training rare and short.
4. **Beat small-data overfit with synthetic + augmentation (R7).** Perspective-correct composited
   far-balls + cross-game/camera domain randomization expand effective data without new labeled games.
5. **Decorrelate within games.** Sample frames sparsely for training (consecutive frames are duplicates)
   — more diversity per GPU-hour.
6. **No single-game flukes.** Require a win on >=2-3 held-out games (a single held-out game is a starting
   point, not proof) before believing an improvement.

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

**Result (2026-06-16) — the world-model does NOT rescue J's raw heatmap; the per-frame evidence is the
bottleneck.** Far recall (131 GT) at viewport radii R=20/200/400:
- **per-frame argmax (reproduces J):** 0.290 / 0.359 / 0.389. (R20=0.290 matches J's search exactly →
  the harness is faithful.)
- **motion-only global-MAP TBD:** ~**0.0**. It locks onto a *static bright spot* (track pinned at y≈245
  across all 705 frames) — a stationary point has zero acceleration penalty + a high score every frame,
  so it beats the moving, intermittently-detected ball. (Also exposed an unbounded-CV-extrapolation bug,
  now fixed.)
- **+ static-feature suppression (fixed-camera background):** removes the static lock and **lifts the
  argmax to 0.397 / 0.46 @200/@400** — the best result, and a real, cheap win. But TBD on the suppressed
  set then *coasts through occlusion* (detected-fraction ~0.05) and stays ~0, because after suppression
  the peaks are scattered across different objects each frame with **no coherent ball trajectory to lock
  onto**.

**Root cause:** champion-J's far ball is a *strong* peak only ~29% of far frames; the rest it is weak or
absent while bright distractors (players/lines) dominate. There is no consistent ball signal for
track-before-detect to integrate. This **rigorously confirms the session's "tracking can't rescue a
low-precision detector"** — and pinpoints that the bottleneck is the **detector evidence**, not the
tracker. The world-model is the right *architecture* (the 2025 keystone proves it works on a *decent*
detector) but it is GIGO: it needs a measurement layer that produces consistent, ball-not-distractor
candidates.

**Strategic redirect:** the world-model is necessary but not sufficient on top of J. Phase-1 priority
becomes the **measurement layer** — the cheapest lever being the **size prior** (the far ball is ~8 px;
players/line-blobs are much bigger, so down-weight big candidates). Whether size-aware candidate
selection lifts recall above the 0.46 argmax ceiling is the next test (re-dump with per-peak feature
size, `iron_peaks_size.json`, in progress). If size alone is not enough, a better detector (high-res /
hard-neg-mined / two-stage classifier) is the Phase-1 work — exactly what the GPU is for.

### EXP-2 (2026-06-16): size prior does NOT help — the far field collapses ball and distractors to one size

Re-dumped J's peaks with each peak's observed feature size (`iron_peaks_size.json`) and tested
size-aware candidate selection. **The size signal is too weak in the far field:** ball peaks median
feat **8.0 px** vs distractor peaks median **10.0 px** — almost no separation, because far players,
lines and the ball all collapse to ~8-10 px blobs. Size discriminates ball-from-player in the *near*
field (8 vs ~50 px) but not in the far field, which is exactly where the recall gap is.

| method (far balls, static-suppressed) | R20 | R200 | R400 |
|---|---|---|---|
| argmax, no size | 0.290 | 0.359 | **0.389** |
| + size prior (geometry, expected far ~4.8px) | 0.244 | 0.252 | 0.305 (hurts) |
| + size prior (absolute 8px target) | 0.290 | 0.359 | 0.389 (no change) |
| TBD + size | ~0 | ~0 | ~0 |

(The geometry homography from the human polygon is directionally sane — expected ball px far 4.8 < near
10.0 — but its magnitude is off vs the observed 8px ball, so the geometric size prior actively hurts.)

## Interim conclusion of EXP-1/2 — CORRECTED by EXP-3 (causal tracking DOES rescue J)

> **Correction (see EXP-3 below).** This section concluded the world-model could not help on J. That was
> specific to the *global-MAP Viterbi* inference + the size prior. A *causal continuity* tracker DOES
> rescue J — far-ball viewport area-recall 0.39 → 0.84. The text below is kept as the honest record of
> which levers failed (global-MAP, size prior) and the one that helped (static suppression).

Across both experiments, on the hardest split (far balls — where AutoCam also fails), **no cheap analytic
lever rescues recall**: motion-only TBD locks on static background (fixed by suppression) then coasts
(no coherent trajectory); the size prior is useless-to-harmful (far field size-collapse). The single
real win is **fixed-camera static-suppression**, which lifts the *simple argmax* area-recall 0.39 → 0.46
@400 — keep it. The far ball is a strong peak only ~29% of frames and, when present, is nearly
indistinguishable from far distractors by size or score.

**This definitively confirms "tracking can't rescue a low-precision detector" and locates the bottleneck
in the measurement layer.** The world-model is the right architecture for the *whole-game* job
(smoothness, distractor rejection, identity/handoff, restart modes) and works on a decent detector (the
2025 keystone), but it is GIGO — it cannot manufacture far-ball signal that isn't in the per-frame
evidence. **Phase 1 priority is therefore the far-ball detector**, and the promising directions are the
ones that don't rely on size: (a) the session's high-res perspective-warped detector (more far pixels);
(b) **motion-trajectory candidate generation** — the far ball, even when player-sized, *moves* fast and
ballistically while far players move slowly/bipedally, a multi-frame signal a single-frame heatmap misses;
(c) two-stage candidate→classifier. Scope caveat: this clip is a deliberately hard far-ball segment;
normal near/mid play is far easier, so the world-model + suppression already give a usable viewport there.

### EXP-3 (2026-06-16): causal continuity tracking DOES rescue J — strategy validated pre-GPU

Two findings overturn the interim pessimism:

1. **Candidate-recall ceiling.** The far ball is in J's top-12 candidates **0.565 @R20, 0.656 @R100,
   0.779 @R200** (median rank 0 when present). The detector surfaces the ball *far* more than argmax's
   0.29 uses — in ~36 far frames the ball is present but a distractor outscores it. Recoverable by
   continuity.
2. **Causal tracking recovers them.** Global-MAP Viterbi was the wrong inference for an *intermittent*
   target (prefers a smooth distractor/coast path → ~0). A **causal continuity tracker** (predict → gate
   → pick the candidate **closest to the prediction**; coast + re-acquire) on static-suppressed
   candidates gives:

   | metric (far balls, hardest split) | R20 | R200 | R400 (viewport) |
   |---|---|---|---|
   | argmax (raw J) | 0.290 | 0.359 | 0.389 |
   | **causal tracker (tuned)** | **0.405** | **0.656** | **0.840** |

At viewport scale the world-model lifts far-ball area-recall **0.39 → 0.74–0.84** — near the
candidate-recall ceiling, i.e. it recovers nearly every frame where the ball is detectable at all, on J's
existing heatmap with **no retraining**, and recovers **15% of the balls AutoCam missed** (acmissed@20
0.0 → 0.154). **"Continuity beats appearance" confirmed:** `pick=closest` >> `pick=score`. (The committed
`iron_eval` uses the **restart-safe** suppression default → 0.305 / 0.557 / **0.740**; the 0.84 row uses
more aggressive static suppression, safe only on no-restart clips.)

**Corrected conclusion.** The world-model is the right architecture AND it materially helps on the
current detector — the strategy is promising *before* any GPU training. The residual is the ~22% of
frames (@R200) where the ball is not a candidate at all; raising that ceiling is the detector's job
(motion candidates / Phase-1). **Anti-overfit caveat:** the tracker params (gate0=80, max_lost=8,
alpha=0.5) are tuned on this one clip — multi-game LOGO validation is the immediate robustness step
(even untuned configs beat argmax, e.g. gate0=100 → 0.52 @R400). Productionized as `tracker.py`
(`causal_track` + `TrackerConfig`, 4 tests).
