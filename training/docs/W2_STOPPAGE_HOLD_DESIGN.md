# W2 — Stoppage-HOLD planner state: DESIGN (task #20)

*2026-07-25. Design + pre-registration only — per the standing rule (Mark 07-24), no W2 build
starts before the W1 table is banked. Companion to the Virtual Operator arc plan; measured
grounding below is from EXP-OP-01..04 and DECISIONS (h)/(k).*

## 1. Problem, in measured units

- 56% of labeled champion-vs-GT divergence mass is stoppages: GT holds (focal swing <200 px
  over gap-40 clusters, (h) via `_hold_check.py`) while the champion sweeps 1,100–2,200 px.
- The human default is a STILL camera: GT pan-velocity median 0.0 px/s, p90 374 (EXP-OP-03).
- The champion has no hold at all: dead-ball logic only widens zoom
  (`camera_planner.py:130`); pan freezes only on `None` input. The tracker's rich miss-state
  is erased before the planner sees it (`ball_tracker.py:880-884` drops miss frames; Kalman
  fills every frame with a coasted estimate; `trajectory.json` is bare `[x,y]|null`).
- Crude freeze oracle (EXP-OP-04 Run C): far capture +0.053 as a LOWER bound; it damaged mid
  (−0.35) because it froze at SPAN-ENTRY position — on failure segments the entry position is
  already wrong. **Design consequence: HOLD must anchor at the last-GOOD position, never at
  wherever the campath happens to be when the vote fires.**

## 2. The contract seam: `trajectory/2`

Extend the ball_select → plan_camera contract with per-frame state, plumbed from `rerank`'s
existing internal state instead of dropping it:

```
{"schema": "trajectory/2", "g_start": int, "fps": float,
 "points": [[x, y] | null, ...],
 "state":  ["T"|"C"|"M", ...],      # tracked | coasted (Kalman fill) | missing
 "conf":   [float, ...]}            # per-frame track confidence (emission-derived)
```

- `T` = frame had a selected candidate on the Viterbi path; `C` = in-span Kalman fill
  (occlusion/miss interior); `M` = outside any track span (upsample gap >24 fr, current null).
- Producer changes: `rerank()` returns its miss-frame set alongside preds (no behavior
  change to selection); `kalman_smooth()` tags fills; `ball_select` writes trajectory/2.
- Back-compat: `plan_camera` accepts trajectory/1 (all `T` where non-null); renderer and
  campath schema untouched. Consumers requiring the artifact get the neutral element
  (all-`T`) from v1 inputs — required-artifact, neutral-default convention.

## 3. Planner FSM: LIVE / HOLD / REACQUIRE

States and transitions (all thresholds are named `PlannerConfig` knobs, pre-registered
defaults in §4; tuned only via the W1 scorer per §5):

- **LIVE** (current behavior, unchanged): EMA follow + lead room + zoom curve.
  - → HOLD when the hold vote is sustained for `hold_entry_frames`.
- **HOLD**: pan is FROZEN at the anchor; zoom eases wide on the existing dead-ball curve.
  - **Anchor = the smoothed camera position at the LAST frame the vote was clean** (all
    voters LIVE-ish): computed as the campath position `hold_entry_frames` before HOLD entry
    — i.e. before the entry run began, not at its end. This is the EXP-OP-04 lesson.
  - → REACQUIRE when `hold_exit_frames` consecutive `T` frames land with world-consistent
    motion (not a single distractor flash).
- **REACQUIRE**: ramp the pan gain from 0 → LIVE over `reacq_ramp_frames` toward the new
  target (no snap); if the exit condition collapses mid-ramp, fall back to HOLD at the same
  anchor.

**Hold vote (per frame, OR of enumerated voters — the FSM takes votes from day one so later
voters plug in without redesign):**
1. `state[f] in {C, M}` — the tracker itself has lost or is coasting (W3 stage-2 consumer:
   NEVER pan on a coasted estimate; this voter alone implements that policy's hold half).
2. Sustained slow ball: |world velocity| < `hold_speed_thresh` for `hold_entry_frames`
   (pixel-space fallback until #19 certifies; then field units).
3. Low candidate dispersion (from the detections artifact when present): candidate cloud
   spread < `hold_dispersion_px` — a scramble/pile-up signature.
- Future voters (pre-wired, not built in W2): player-cluster play-state (camera- and
  weather-independent, the PRIMARY play-state signal per the arc brief §3d) and whistle
  (per-camera capability flag — 2026 Reolink only, wind-aware confidence; Dahua 8 kHz audio
  cannot carry the ~4.35 kHz whistle).

## 4. New `PlannerConfig` knobs (defaults pre-registered here; W5 may fit them later)

| knob | default | meaning |
|---|---|---|
| `hold_entry_frames` | 20 (1 s @20fps) | sustained vote frames to enter HOLD |
| `hold_exit_frames` | 8 | consecutive `T` frames to leave HOLD |
| `hold_speed_thresh` | 0.094 deg/f (= existing `deadball_speed_degf`) | slow-ball voter |
| `hold_dispersion_px` | 180 | candidate-cloud spread voter (off if no detections artifact) |
| `reacq_ramp_frames` | 12 | gain ramp length leaving HOLD |
| `hold_anchor_lookback` | = `hold_entry_frames` | anchor = campath position this many frames before entry |

Everything else (zoom curve, dead-ball widening, missing_hfov) unchanged.

## 5. Tuning + evaluation protocol (zero GPU, zero clicks)

1. Implement behind a `PlannerConfig.enable_hold` flag; trajectory/2 producer + FSM =
   ~1–2 days of work, CPU replay only.
2. Tune on banked data via `operator_ladder run-a` variants + the W1 scoreboard: PIT-GT 650
   views + frozen viewport v1 (spc/fair) once their hn4@s4 champion baselines exist
   (in flight as of this writing).
3. **Pre-registered reads (before any number):**
   - Headline: amended hold-fidelity cell (gap-40/n≥4/GT<200 clusters) reaches GT-swing
     parity within that instrument's split-half null band.
   - Far capture@600 ≥ the Run C lower bound (+0.05 on PIT) with mid/near NON-regression —
     the crude oracle's mid damage must NOT reproduce (the last-good anchor is the fix).
   - Live-play cells (capture, |Δcx|) non-regressing on BOTH families under the dual rule
     (referee v3 port in `operator_metrics.dual_rule_read`).
   - Power reality (EXP-OP-03/04): PIT has only 2 qualifying hold clusters → the hold cell
     may be power-limited there; count v1-family clusters when their nulls run; if total
     clusters < ~6, the hold claim states its power floor and the event-spreading label
     queue (250–400 clicks, pending) becomes the W2 unblocker — budget ask goes to Mark
     only at that point.
4. Promotion gate: standard — decisive-or-zero under the dual rule on the pre-registered
   cells; detector rows veto-only (DECISIONS (k)).

## 6. Explicitly out of scope for W2

- Any lookahead/windowed re-decision (priced ≈0 by EXP-OP-04 Run D; unfunded).
- Whistle/player-cluster voters (Tier-1 backlog; the FSM accepts them later).
- Per-venue anything (binding: no cross-game memory; all knobs global or session-scoped).
- Detector/selector changes of any kind.

## 7. OOB-HOLD amendment (Mark, 2026-07-26 — "if the ball goes out, HOLD until it comes back in near where it went out")

Extends the FSM with an OOB-DEAD state (grounded in the winfar1 autopsy, EXP-OP-08/09):
- **Trigger:** the selected path crosses the field polygon outward (exit point + exit
  velocity already computed in `rerank`'s OOB bookkeeping — plumb, don't rebuild).
- **Seam:** trajectory/2 gains state `O` + an `exit_xy` anchor field (neutral-element
  rules as before; v1 inputs never produce `O`).
- **Planner:** OOB-DEAD behaves as HOLD anchored on the EXIT POINT (projected to view
  coords), slight widen; it outranks the generic vote-based HOLD while active.
- **Reacquisition GATE (not just bias):** during OOB-DEAD, candidates farther than R
  meters from the rules-implied restart location (touchline exit -> exit point;
  goal-line exit -> corner arc / goal area) are REJECTED for path re-entry; R grows
  slowly; the gate decays to global reacquisition after T seconds (handles fouls/quick
  restarts elsewhere). Insertion point: the existing `reacq_cap`/`reacq_dist_w`
  machinery.
- **Prerequisite:** the persistence/static filter (EXP-OP-09 lever 1) — without it the
  commitment failure prevents the exit from being detected at all (winfar1's mechanism).
- **Caveats:** polygon accuracy at the boundary ((n) ratio-alarm applies); exit-type
  matters (goal-line exits can resume as long punts — R per exit type); knobs
  session-scoped, no cross-game memory.
Pre-registered read: WIN-column far + the OOB segments' hold-fidelity; scored with the
same referee protocol as the base FSM.
