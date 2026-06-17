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
| Zero-touch field geometry (homography + geometric size prior + support) | `geometry.py` | ✅ done |
| Track-before-detect global-MAP (Viterbi) | `tbd.py` | ✅ done — wrong inference for an *intermittent* target (EXP-1) |
| Causal continuity tracker + action-prior fused tracker | `tracker.py` | ✅ done — the winning inference (EXP-3/5) |
| Fixed-camera static-background suppression | `measurements.py` | ✅ done |
| Eval: peak extraction + center-distance/area recall + canonical comparison | `eval.py`, `iron_eval.py` | ✅ done |
| Game-ball ROLE / handoff state machine | `game_ball.py` | ✅ done (Phase-2) |
| Virtual rectilinear "broadcast camera" views | reuse `dewarp_tiles.py` | planned (needs GPU phase) |
| Multi-game LOGO eval (held-out human GT) | — | blocked on 2nd-game GT / AutoCam-loses-ball clips |

All CPU, no GPU, **36 tests** green. Inference chain: `suppress_static_candidates` →
`causal_track_fused` (appearance + action) → `track_game_ball` (role/handoff).

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

### EXP-4 (2026-06-16): motion candidates are complementary — fused candidate-recall 0.99@200; the lever is the TRACKER, not the detector

Generated motion candidates via fixed-camera background subtraction (MOG2 on the far band — **no model,
no GPU**) and measured candidate-recall (is the ball anywhere in the candidate set?):

| ball present in candidate set (far GT) | R20 | R100 | R200 |
|---|---|---|---|
| J (appearance) | 0.565 | 0.656 | 0.779 |
| motion (MOG2) | 0.252 | 0.771 | 0.908 |
| **fused J + motion** | **0.763** | **0.954** | **0.992** |

**Important nuance:** filtering motion blobs to ball-sized (area ≤30 px²) leaves **zero** blobs — *all*
MOG2 motion is **player-sized**. So motion is not detecting the tiny far *ball*; it is detecting
**player action**, whose centroid sits near the ball at viewport scale. So the "motion candidate-recall"
is really an **action-area** signal (the *"action clusters around the ball"* prior, R4): it bounds where
the ball *is* (to ~200 px, 91% of the time) but does not localize it precisely. The fused 0.99 @R200 is
therefore **area-level coverage**, not 99% precise detection — and exactly right for the *viewport* goal,
while tight-R precision still needs the appearance peak (J 0.565 @R20).

**Reframed conclusion.** Two complementary signals, both cheap and training-free: **appearance (J) for
precise ball position** (when present) and **motion/action for the ball's area** (almost always). The
simple closest-gate tracker doesn't combine them well (motion's ~20 player blobs/frame add noise; after a
J-gap "closest" grabs a player → 0.74–0.77 @R400). The lever is the **tracker + using action as an
area prior** (R4), largely CPU-side — **not necessarily detector retraining.** **Next experiment (EXP-5):
**use motion/action as a soft area *prior* that pulls the prediction toward the action centroid, with the
appearance peak for precision — a windowed/multi-hypothesis tracker — to realize more of the area ceiling
at viewport scale.

### EXP-5 (2026-06-16): action-area prior closes the gap — 0.87 viewport area-recall, all CPU / no training

During J's detection gaps, pull the track toward the **local player-action centroid** (mean of motion
blobs within ~300 px of the prediction) — the *"action clusters around the ball"* prior (R4) — instead of
coasting blind or grabbing the closest player. Result (far balls, hardest split):

| method | R200 | R400 (viewport) |
|---|---|---|
| argmax (raw J) | 0.359 | 0.389 |
| causal tracker (J only) | 0.557 | 0.740 |
| **+ action-area prior** | **0.672** | **0.870** |

On the hardest far-ball clip, at viewport scale, the world-model + cheap **training-free** signals
(appearance → position, action → area) lifts area-recall **0.39 → 0.87**, no detector retraining. The
action-pull is param-sensitive (strong pull helps; weak pull drifts), so robustness + multi-game
validation is next, but the lever is validated.

### EXP-6 (2026-06-16): cross-game generalization — causal tracker holds on a 2nd game, same params

Validated on `heat__2026.06.06_vs_Fairport_away` seg9 (a different game/field, not in J's training; 99
far balls; **GT = AutoCam labels**, so this tests generalization of the *improvement*, not beating
AutoCam). With the **same Irondequoit-tuned params**:

| far balls @R400 | argmax (raw J) | causal tracker | fused (+action) |
|---|---|---|---|
| Irondequoit (human GT) | 0.39 | 0.74 | 0.87 |
| **Fairport (AutoCam GT)** | **0.42** | **0.82** | 0.73 |

The causal tracker's lift — **argmax→causal +0.38–0.43 @R400 on *both* games** — is not Irondequoit-
specific; params transfer. It turns noisy per-frame J into a coherent track matching AutoCam **82% vs
argmax 42%**. Two notes: (1) Fairport's J candidate-recall is already ~0.95 (AutoCam labels = easy
detected balls), so the action prior (which fills J *gaps*) is unneeded and slightly **hurts** here —
it's **situational**, so it should be applied adaptively (gate it on appearance reliability). (2) AutoCam-
only GT can't test beating AutoCam on its misses — that still needs human-GT clips.

### EXP-7 (2026-06-16): adaptive action-prior fusion is open work — causal is the robust default

Tried to make ONE fused tracker optimal on both games by gating the action pull on appearance
reliability. **All three schemes failed** (far-recall @R400):
- `action_min_lost` (pull only after N gap-frames): Irondequoit collapses (0.87→0.20 at N=3 — its long
  gaps need *immediate* pull; delaying lets CV drift), Fairport needs large N.
- reliability-scaled pull (`pull *= 1-reliability`): under-pulls Irondequoit (0.65), still hurts Fairport (0.68).
- reliability-threshold (pull only when EMA < τ): no τ wins both — Irondequoit wants always-pull, any
  pull hurts Fairport.

Root cause: the tracker's gated hit-rate isn't a clean "is the ball detected" proxy (it stays high while
locked on a distractor; drops when the ball is just outside the gate). The action prior is a genuine
**precision↔recovery tradeoff**: it recovers *undetected* far balls (Irondequoit +0.13) but costs
precision on *detected* ones (Fairport −0.10). **Decision:** `action_pull=0` (pure causal) is the robust
default that generalises; the action prior is an **explicit opt-in** for hard far-ball recovery. A clean
adaptive switch (likely needing a real detector-confidence signal, not the tracker's own hit-rate) is
deferred.

### EXP-8 (2026-06-16): REAL AutoCam-loses-ball clip — decisive win vs AutoCam, on human GT

First gold-standard validation on a real clip Mark flagged (Spencerport 4:45–5:03), with **human ground
truth** he labelled via the far-label tool (88 frames: 73 visible ball + 15 player-occluded). The ball is
ultra-far in the corner (y≈159–229) and frequently occluded — the worst case, which is why AutoCam lost
it. Scored vs GT (R=400 viewport):

| method | R200 | R400 | median dist-to-ball |
|---|---|---|---|
| **AutoCam** (its logged viewport xy) | 0.00 | **0.01** | **2852 px** (lost; sat at frame centre) |
| world-model **fused** (causal + action) | 0.36 | **0.41** | **474 px** |
| world-model causal-only | 0.08 | 0.15 | 749 px |
| argmax raw J | 0.21 | 0.23 | 1192 px |

**The world-model decisively beats AutoCam where AutoCam fails** — 0.41 vs 0.01 @R400, and 6× closer to
the ball. AutoCam's viewport sat ~2850px off (defaulted to centre); the world-model held the ball's area
41% of the time. The action prior is essential here (causal-only 0.15 → fused 0.41) — exactly the hard
far-ball recovery it's for. **But the fused candidates contain the ball 97% @R200**, so the tracker
realises only ~0.41 of a ~1.0 ceiling: on this hard clip the bottleneck is the **tracker's selection**
(greedy acquisition locks on a brighter player distractor), not detection — large CPU-only headroom for
the Phase-2 multi-hypothesis tracker. Validation pipeline (dump → far-label set → human GT → score vs
AutoCam) is now turnkey for the rest of Mark's clips.

**Fix from EXP-8 — motion-protected suppression (`measurements.suppress_static_candidates(..., motion=)`):**
the diagnostics showed my static-suppression was *deleting the nearly-static deep-corner ball as
background* (candidate-recall 0.93 → 0.82 @R400), and no single occupancy threshold worked across games
(clip1 wants high, Fairport catastrophic at high). The principled fix uses Mark's "action clusters around
the ball" prior: **only suppress a static cell if it has NO motion nearby** — a background line has no
motion next to it; a static ball has players moving around it. Result: clip-1 candidate-recall restored to
0.93, tracker **0.41 → 0.52 @R400**, and **no regression on Irondequoit (0.87) / Fairport (0.71)**.
Remaining gap (0.52 vs 0.93 ceiling) is the greedy tracker's selection — the Phase-2 multi-hypothesis work.

### EXP-9 (2026-06-16): the 0.52 wall is detector FALSE-POSITIVES, not the tracker — static-aware selection wins; beam-MHT fails

Closing the 0.52→0.93-ceiling gap on Spencerport clip-1. First built the turnkey clip-scoring harness
(`clip_eval.py`, generalising `iron_eval` to the far-label format) and **reproduced EXP-8 exactly**
(AutoCam 0.014, fused 0.521 @R400, ceiling 1.00@400/0.97@200). Then a per-frame miss audit found the
selection failure is concrete:

- **Detector false-positives, not tracker weakness.** champion-J fires **persistent high-score (0.81–0.96)
  peaks at 6 fixed scene locations** (line corners, posts, far-side clutter) — occupied 51–77% of the clip.
  The greedy/argmax trackers lock onto these because they outscore the dim (≤0.6) far ball. Two are the
  exact failure anchors: a near-side **assistant-referee/linesman** at (6008,1186) the tracker acquires at
  frame 0 and follows ~3000px off for 13 frames; a fixed corner feature at (2340,220) it parks on while
  the ball drifts away. J nails the ball precisely (dist ≤5px) in ~15 frames — those exact peaks are just
  never selected.
- **Multi-hypothesis beam-MHT FAILS (documented negative).** Built a full beam tracker
  (`multi_hypothesis_track`, backpointer beam search + merge/prune + re-acquire jumps + action/support
  priors). Global-max-accumulated-score selection reintroduces the **EXP-1 global-MAP failure** — a long,
  smooth, *bright distractor* path outscores the dim/intermittent/occluded ball under any
  appearance-summing objective. Oracle-selecting the best path *in the final beam* (0.44 @R400) barely
  beats global-max (0.41) → **the beam doesn't even contain a good ball path**: brightness-pruning kills
  the dim ball hypothesis early. Neither global-MAP nor a pruned beam works here. (`MHTConfig` /
  `multi_hypothesis_track` kept — useful tool + oracle diagnostic — but not the production tracker.)
- **The action centroid does NOT anchor acquisition.** On this clip the ball is played *ahead* of the
  play (a long ball): the global action centroid sits **1060px** from the ball (5% within 400). Action is
  only a *local* gap-filler (fused tracker), never an acquisition anchor.

**The win — static-aware selection (productionised, `TrackerConfig.static_thresh`).** The 6 FPs are
*static* (same cell every frame); the ball *moves* (no-teleport physics). So the tracker **avoids
acquiring or following** any candidate whose cell holds a peak in ≥30% of the clip — but keeps it as a
fallback (the ball may pass through one) and **coasts past it** rather than parking. This is soft, at the
*selection* level, where binary cell-removal failed (unconditional suppression drops @R400 0.52→0.41 by
deleting ball candidates in FP cells). Cross-game (anti-overfit, same params):

| game (far balls @R) | base @200 | **SA @200** | base @400 | **SA @400** |
|---|---|---|---|---|
| **Spencerport clip-1** (human GT) | 0.260 | **0.452** | 0.521 | **0.534** |
| Fairport seg9 (AutoCam GT) | 0.707 | **0.758** | 0.707 | **0.768** |
| Irondequoit (human GT) | 0.672 | 0.672 | 0.870 | 0.870 |

**No regression anywhere, +0.05–0.19 on 2 of 3** (Irondequoit's J has no persistent-FP problem → correctly
untouched). Median dist-to-ball on clip-1 **347→258px**. Decisively still beats AutoCam (0.534 vs 0.014).
**Residual wall = detector discrimination** (dim ball vs bright linesman/players): the next real lever is
candidate-level — the geometric **size prior** (far ball ~8px vs 50px person; needs the detector to emit
size → re-dump) and the **two-stage candidate→classifier** (Phase-1 R3), NOT a smarter tracker over the
same ambiguous candidates. Independently confirms the heatmap session's "it's fine appearance
discrimination." Validation harness `clip_eval.py`; detections for clips 2–5 dumped and ready to score on
Mark's labels.

### EXP-10 (2026-06-16): cheap classical SIZE prior does NOT reject the person distractor — needs a learned classifier (R3)

EXP-9 named the residual wall as detector FPs, esp. the near-field **linesman** the tracker acquires at
frame 0. Tested whether a **geometric size prior** can reject it. First confirmed the geometry's
size(location) is correct and discriminative (expected ball Ø **4.8px far → 14.3px near**). Re-dumped
clip-1's J peaks (`dump_size.py`, 351 frames) with three image-measured size features per peak, measured
on the warped band (where the far ball is resolved): `char_sigma` (normalized-LoG bright-blob scale
selection), `log_resp`, `brightdim` (bright connected-component max-dim). Per-class medians:

| class | char_sigma | brightdim | dexp |
|---|---|---|---|
| ball (n=35) | 20.0 | **5** | 4.8 |
| linesman (n=26) | 4.0 | **12** | 14.3 |
| FP (n=1414) | 4.0 | 7 | 5.6 |
| other (n=2737) | 4.0 | 5 | 4.9 |

`char_sigma` is **inverted/noisy** (ball=20, the max scale — σ²-normalization over-weights large scales for
faint blobs): useless. `brightdim` separates ball(5) from linesman(12) **in the median** but **every
filtering mechanism regressed** the tracker vs the EXP-9 baseline (0.452/0.534):

- hard ÷dexp ratio cap → 0.27/0.34 (near-field dexp is large, so the linesman's 12px never trips a ratio);
- absolute brightdim cap (8–14) → 0.26–0.37 (a person's bright sub-patch — socks/highlight — is ≤8px, so
  the linesman only partly filters: 26→8 surviving peaks; and ball candidate-recall drops 0.93→0.90);
- soft size-preference (prefer ball-sized at acquire/select, à la static-aware) → best 0.47 @400, still < 0.534.

**Why cheap size fails (measured, decisive):** (1) `brightdim` captures bright *sub-patches*, not object
extent — overlaps the ball; (2) perspective makes a near person and a near ball similar-sized; (3) even
when size removes the linesman it does **not help *find* the ball** — there are ~2700 ball-sized "other"
candidates, so "pick the brightest *small* blob" still isn't the ball, and filtering costs continuity
anchors. A human distinguishes the linesman crop from the ball crop instantly (rich appearance, not a size
scalar) — so the lever is the plan's **R3 two-stage candidate→classifier** (a tiny ball-vs-person/-line CNN
on candidate crops), a GPU-trained model — **this is where the 4070 finally earns its keep.** Caveat: the
clip-1 acquisition failure is partly a **cold-start artifact** of an isolated 18s excerpt — mid-game the
tracker enters already locked on the ball, so deployment recall is better than 0.534 implies; the classifier
mainly hardens (re)acquisition. (`dump_size.py` lives in the box workspace, not the repo.)

### EXP-11 (2026-06-16): person-box MASKING is the wrong mechanism (ball-on-player) — mandates a unified ball+person *appearance* detector

PoC: ran pretrained YOLO (yolo11m, COCO person) offline on clip-1's 351 frames → person
boxes in source coords; tested masking ball candidates that fall in a person box. Three findings:

1. **Person-appearance is separable.** YOLO boxes the linesman at the acquisition frame
   (5552: (6008,1186) inside a person box) → a learned detector can tell person from ball.
2. **Ball-on-player is common — masking is destructive.** The true ball is inside a person box
   **17/73 (23%)** of GT frames (33% with a 15% box expand) — and this is a *far* ball usually
   ahead of play; close play (chesting/heading/feet — Mark's point) is far higher. A box mask
   deletes the ball whenever it is on a player, i.e. most of in-play time.
3. **Masking nets +0.12 here but for the wrong reason.** static-aware + person-mask (0.3 expand):
   **0.521 @200 / 0.658 @400** vs EXP-9 0.452/0.534, and acquisition moves off the linesman
   (6008,1186)→(4414,139). The gain is purely *removing person-distractors*; it is paid for by
   sacrificing the 23–33% ball-on-player frames, a trade that flips negative in close play.

**Decision:** do NOT mask. The person signal must be **appearance-level**, not a box gate — a
**single multi-class detector** (champion-J backbone + a **person head**; ball-vs-person is an easy
discrimination so it stays small, ~yolo-nano scale, fits the base-hardware budget). The ball head fires
on the ball *even on a player's chest/head*; the person head marks the body; they coexist with no
masking. This captures EXP-11's +0.12 distractor-removal upside **without** the ball-on-player loss, so
its ceiling is above the 0.658 masking number. Fully-occluded-behind-body → no detection → the
world-model coasts (physics + action prior), as today. YOLO is used **only at training-data-prep** to
bootstrap person labels (`bootstrap_persons.py` already does this), never at inference — one model per
frame. This is also the shared backbone for Mark's eventual 2-team player tracker (add a team head). PoC
scripts (`yolo_persons.py`) live in the box workspace, not the repo.

## Bottom line of Phase-0 research (2026-06-16)

The strategy is **proven promising before any GPU training.** On champion-J's existing detector, the
world-model lifts far-ball **viewport area-recall from 0.39 (argmax) → 0.74 (causal tracker) → 0.87
(+ action prior)** on the hardest clip, and recovers ~15% of the balls AutoCam misses — entirely CPU,
no retraining. Two cheap complementary training-free signals carry it: **appearance (J heatmap) for
precise position, player-action (MOG2 motion) for the ball's area.** **The bottleneck is the tracker +
priors (CPU-side), not the detector** — so beating AutoCam may not need expensive GPU detector training
at all; the 4070 is held in reserve. **Caveats:** EXP-6 confirms the causal-tracker
improvement **generalizes to a 2nd game** (Fairport, same params: argmax→causal +0.40 @R400), so it is
not Irondequoit-specific — but that 2nd game has AutoCam-only GT, so **beating AutoCam *on its misses*
across games still needs human-GT clips** (your AutoCam-loses-ball timestamps). The action prior is
**situational** (helps on hard/sparse far balls; gate it on appearance reliability).

**Refinement (EXP-9):** On the hardest *real* human-GT clip the residual gap (0.53 vs 1.0 ceiling) is
**detector false-positives** (6 persistent static high-score peaks + dim ball), not tracker cleverness —
a beam-MHT can't beat it and the dim ball loses any brightness-summing objective. The training-free
**static-aware selection** (don't acquire/follow a persistent-static FP) is the validated tracker-side win
(no cross-game regression); past it, the lever moves to **candidate discrimination**. EXP-10 then ruled out
the *cheap* size prior (classical blob-size features can't separate a dim far ball from a near person), so
the remaining lever is specifically a **learned ball-vs-person/-line classifier on candidate crops (R3)** —
which is where the 4070 finally earns its keep.

## Phase-1 plan (2026-06-16): unified ball+person detector benchmark (A vs B)

Decision (Mark): **benchmark both architectures, ship the winner on the data** (his experiment-driven
preference). Scope is *ball detection only* — the team / per-player classifier he eventually wants is
explicitly deferred (a later head on the same backbone, not needed to solve the ball now).

**Why a person head at all (from EXP-9/10/11):** the residual wall is the ball detector firing on
persons (the linesman) and being out-competed by them. Cheap size features (EXP-10) and person-box
masking (EXP-11) both fail — masking deletes the ball whenever it's on a player (23–33% of frames even
on a far clip; far higher in close play). The fix must be **appearance-level**: one model whose ball
head fires on the ball *even on a player's body* while a person head claims the body. Person labels are
bootstrapped **offline** by a pretrained detector (`bootstrap_persons.py`, YOLO) → never at inference →
one model per frame.

**A — HeatmapNet + person head (low risk).** `HeatmapNet(out_ch=2)` (done, backward-compatible): ch0
ball, ch1 person, shared backbone, multi-task weighted-MSE. Persons get their own *center-heatmap* — no
bbox/stride, so the tiny 3-8px ball and big persons coexist in one cheap net. Protects champion-J's
proven far-ball recall; person head teaches "not a ball."

**B — yolo26n/m multi-class (Mark's suggestion).** One YOLO, ball+person classes. Standard, easy team
head later, clean ONNX. *Risk, stated by the repo itself:* "the ball is 3-8px, at/below a bbox stride,
so IoU detection collapses" — so B must prove it can hold far-ball recall, the whole point. Benchmark
honestly; if it can't see the far ball, A wins by default.

**Protocol:** train both on the `far_label` crops (ball labels = Mark's GT; person labels = bootstrapped
YOLO centers/boxes), **leave-one-game-out** (split by game, never frame — anti-overfit). Score on the
shared metric: far-ball viewport recall @R200/R400 + the linesman-FP check (does the ball head stop
firing on the linesman?) + fps on the base-hardware target. Append to the experiment tracker; ship the
winner as the world-model's measurement source. Sync experiment code to the GPU box via the research
git branch (not file-copy). Status: `HeatmapNet(out_ch=2)` landed; next = multi-task target builder
(ball + person Gaussians) + the two training runs.

### EXP-12 (2026-06-16): person-info is cheap either way — the ball detector is the cost; Path 1 (cheap nano + mask) favored

Mark corrected the budget: a 90-min @20fps game (~108k frames) must process overnight ⇒ **~0.3 s/frame
FLOOR, and even that is too slow** — so per-frame compute is precious. Two findings change the
Path-1-vs-Path-2 picture:

1. **Sparse person detection fails.** Holding person boxes between detections breaks at ≥16-frame cadence
   (clip-1 @R400 0.658 → 0.27) — the linesman *moves*, so stale boxes mask the wrong place. Person info
   must be **per-frame fresh**.
2. **Per-frame person info is cheap either way (GTX 1060):** champion-J full band **1048 ms/frame**;
   marginal cost of person = **+23 ms** (Path 2, person head, one pass) vs **+14 ms** (Path 1, yolo11n
   @1280, a separate nano pass) / +36 ms @2560. The person addition is **1–3% of the ball pass** — the
   ball detector is the real budget problem (needs the 4070 + ROI-tracking, not the 1060's 0.95 fps).

**Implication:** the tiny person speed delta should not drive the choice. **Path 1** (separate nano person
net, downscaled, + EXP-11 masking) is +14 ms, needs **no retraining**, and is **already validated at
0.658**; a nano net catches the big near-field linesman fine. **Path 2**'s only edge (pixel-accurate
ball-on-player) is exactly what the viewport goal does NOT need (Mark: "don't care about exact coords as
long as the viewport points the right way"). So the data favors Path 1; Path 2's retrain is only worth it
if a future need for ball-on-player precision appears. Next: confirm Path 1 at its budget setting
(yolo11n @1280) still kills the linesman / holds 0.658, then productionise it as a `mask_person_candidates`
measurement step. (Speed measured on the 1060; absolute s/frame is a 4070 + Phase-3 ROI concern.)

### EXP-13/14 (2026-06-16, overnight): tracker is candidate-limited, not tunable — accuracy lever is the detector

With static-aware + person-mask in place (clip1 0.658 @R400), tried to close the residual
0.658→1.0-ceiling gap from the tracker side. Diagnosis first: all **25/25 clip1 @R400 misses are
recoverable** (the ball IS in the fused candidates ≤400 every missed frame) and the ball is **slow**
(median 3.8 px/frame, max 21.5) — so it is pure *selection drift*, not gate size or detection.

- **EXP-13 forward-backward (fixed-interval) smoothing — negative.** Bidirectional causal pass + merge
  (staleness / prefer-detected / avg). Helps Fairport (0.77→0.83) but **hurts clip1 (0.66→0.55) and Iron
  (0.87→0.85)** — the backward pass cold-starts wrong at the clip *end*, so the merge picks bad segments.
  Net wash. Dropped.
- **EXP-14 selection sweep — no config beats the current one.** Swept vel_decay × action_pull ×
  re-acquire-radius across 3 games. `vel_decay` is very sensitive on the slow ball (0.85→0.18, **0.80→
  0.658** clip1 — faster decay stops the coast drifting). A "local re-acquire" (nearest candidate to the
  prediction vs global-best) made **no** difference (re-acquire rarely fires). The current config
  (decay 0.8, action_pull 0.6, static 0.3, person-mask) is the cross-game optimum: clip1 0.66 / Iron 0.87
  / Fair 0.77; nothing improves clip1 without regressing Iron or Fair.

**Conclusion:** greedy causal + the validated cleaners (static-aware, person-mask) is **near-optimal for
the current candidate quality**; the residual gap is data-association the tracker can't resolve without
knowing *which* candidate is the ball. So the next accuracy lever is the **detector/measurements** (better
candidates / a ball-vs-person-aware net — the GPU work for when more labels land), not more tracker
tuning. The pipeline already beats AutoCam decisively (clip1 0.66 vs 0.014), so per Mark's steer the next
focus is **speed** (EXP-16+). Note clip1 is the hardest excerpt (cold-start in an 18s window); mid-game,
recall is higher (Iron 0.87 is more representative). All runs logged to `overnight_experiments.jsonl`.

### EXP-16/17/18 (2026-06-16, overnight): SPEED — ROI-tracking + iGPU meets the budget on real base hardware

Budget (Mark): ~0.3 s/frame floor (90-min @20fps overnight ≈ 3.3 fps), faster preferred. The ball
detector is the cost; the world-model/tracker is ~free.

**EXP-16 ROI-tracking (the lever), champion-J base24, GTX 1060:** full band (1552×7680, tiled) **1047
ms/frame**; a small square ROI around the world-model's predicted ball is 30–54× faster:

| ROI | 1060 | Iris Xe iGPU (DirectML) | laptop CPU (16-core) |
|---|---|---|---|
| 320×320 | 8 ms (125 fps) | **75 ms (13.3 fps)** | 293–490 ms (2–3.4 fps) |
| 448×448 | 15 ms (66 fps) | **210 ms (4.8 fps)** | ~720 ms (1.4 fps) |
| 512×512 | 20 ms (52 fps) | 297 ms (3.4 fps) | ~900 ms (1.1 fps) |
| full band | 1047 ms | (n/a) | **36,300 ms** |

ROI **preserves recall by construction**: the tracker already only selects candidates within its gate
(≤320 px radius), so any ROI ≥ the gate picks the identical candidate; full-band runs only on the rare
re-acquire (amortized). The ball is slow (≤21.5 px/frame), so a 320–448 px window has large margin.

**EXP-17 iGPU is the real deployment target — and it clears the floor.** `onnxruntime-directml` sees the
Intel Iris Xe **even over this remote session** (the RDP-hides-GPU gotcha that kills AutoCam's GL path
does NOT block DirectML). ROI 320–448 px = **4.8–13.3 fps**, comfortably above the 3.3 fps floor. champion-J
exports cleanly to ONNX (4.2 MB, dynamic H/W, opset 17).

**EXP-18 int8 dynamic quant — no help (negative).** Conv-heavy net; onnxruntime's CPU int8 conv path is
weak, so dynamic quant is *slower* (320: int8 430 ms vs fp32 293 ms) despite 3× smaller weights (1.4 vs
4.2 MB). Further speed (esp. CPU-only) comes from a **smaller base** (base16/8) or static QDQ with
calibration — a retraining lever for later, not needed now (iGPU already meets the budget).

**Bottom line:** the speed path is proven on real base hardware — **iGPU + ROI-tracking at a 320–448 px
window meets the overnight budget with 1.5–4× margin**, ball detector exported to ONNX. The 1060 server
has 50×+ headroom. No accuracy was traded (ROI preserves the gate). Remaining speed upside (smaller model,
static quant, ROI-size tuning) is available but unneeded for the floor.

### EXP-19 (2026-06-17): ROI robustness to a fast ball + render zoom ↔ track-accuracy coupling

Two questions from Mark on the speed/render work.

**Q1 — does a fast ball escape the ROI?** No, because the ball's top speed is *physically bounded* (the
world-model's drag constraint) and the ROI is centred on the **predicted** (pos+velocity) position, so it
only absorbs the per-frame *acceleration* residual. Computed for the clip-1 geometry, a **30 m/s hard
shot** = **34 px/frame far, 94 px/frame near** — a 512 px ROI (±256) covers the worst single-frame jump
with margin. Layered backstops: (a) **velocity-adaptive ROI** size = base + k·|velocity| (bounded, since
max speed is bounded); (b) **free full-band MOG2 motion every frame** — a fast ball in open space is a
strong motion blob, so an escape is caught and the ROI re-centres; (c) **full-band J re-acquire** when
lost (rare, amortized). To build into the ROI deployment path.

**Q2 — render too zoomed out.** Default `render_zoom_scale=0.90` (AutoCam-matched) is ~42° HFOV — far too
wide for a deep-corner ball. Lowering to 0.65 (~30°) / 0.50 (~24°) zooms in. Key coupling surfaced:
**zoom tightness is limited by track accuracy** — at ~24° the current track's ~300 px wobble pushes the
action to the frame edge. So a *cleaner, tighter* broadcast zoom is another thing the detector accuracy
improvement buys. Also: clip-1's deep-corner ball is the **worst case for visibility** (tiny far speck in
open grass ahead of the play); mid/near plays show the ball far larger at the same zoom. Shipped z65
render + the z65 world-model-vs-AutoCam comparison to Mark.
