# W3 — Loss discipline: miss-cost sweep + coast policy, ONE project (DESIGN)

*2026-07-25. Design + pre-registration only; build blocked until the W1 table is banked.
Task number: allocated in DECISIONS CURRENT STATE at W3 kickoff (next free in the DECISIONS
registry — the kickoff brief's "#16" was not a docs number and is corrected by this doc).
Companion to W2_STOPPAGE_HOLD_DESIGN.md — stage 2 here IS W2's voter #1; the two workstreams
share the trajectory/2 seam and are scored jointly.*

## 1. The failure, in measured units

- **Near-autopsy (EXP near, 07-20):** every near miss in the hard clip = ONE 3.6 s event.
  Candidates sat 0.0–0.1 m from the ball, the selector ranked them #1–2, but
  P(ball) 0.07–0.21 < P(none) 0.20–0.34 → Viterbi took the miss-state, the Kalman coasted
  30 m, and the planner followed the ghost out of frame. 11/19 near misses in that clip were
  the miss-state overriding a rank-1 near ball. "Near is a TRACKER-DYNAMICS problem, not a
  selector one." Detector near ceiling = 1.000 for every model — no perception headroom.
- **The future doesn't fix it:** the offline Viterbi is already global (EXP-OP-04 analysis);
  the miss was taken WITH full hindsight available. This is COST CALIBRATION at the
  selector→tracker boundary — the one input dimension measured as strongly consumed
  (§3b of the arc plan).
- Prior art: EXP-DIST-31 (flight-consistent miss re-entry, POSITIVE, in champion);
  EXP-DIST-39 (re-acq distance bias, POSITIVE); learned p_none miss costs ≈ neutral in v1
  (none-supervision sparse: 439/26k frames); a learned miss-cost lever was tried once and
  both directions were worse (EXPERIMENTS ~1321) — the sweep below is STATE-DEPENDENT
  structural cost, not a learned scalar.

## 2. Stage 1 — three-arm state-dependent miss-ENTRY cost sweep (DECISIONS (g) arms)

Code anchor: `rerank()` in `video_grouper/inference/ball_tracker.py` — flat
`cfg.miss_cost=0.9` / learned `miss_costs[t]`, `_MISS_TRANS_COST=0.6`. The sweep multiplies
the ENTRY transition into miss by a context factor computed from the top candidate at t:

- **Arm N (nearness×velocity):** entering miss gets MORE expensive when the top candidate is
  NEAR (expected diameter ≥ the near band edge) and SLOW (candidate-track world speed below
  `slow_mps`). Rationale: ball-at-feet scrambles are exactly where P(ball) collapses but the
  ball is certainly there. `mult_N = 1 + k_N · near(t) · slow(t)`, sweep `k_N ∈ {0.5, 1, 2, 4}`.
- **Arm M (candidate-margin):** entering miss gets more expensive when the top candidate's
  emission margin over P(none)-floor is large relative to the field of candidates
  (rank-1 clearly separated ⇒ trust it). `mult_M = 1 + k_M · margin_norm(t)`,
  sweep `k_M ∈ {0.5, 1, 2, 4}`.
- **Arm C (combined):** `mult_C = mult_N · mult_M` on a coarse 2×2 of the per-arm winners.

Sweep = CPU replay on cached dumps (`operator_ladder run-a` variants exposing the three
multipliers through `RerankConfig`), **hn4@s4 dumps ONLY** (dump-provenance rule, EXP-OP-04:
the hn2-era dump fleet is not the champion's input stream). Games: PIT verdict dump + the
spc/fair hn4s4 dumps (in flight). Zero GPU beyond the dumps.

## 3. Stage 2 — coast policy: NEVER pan on a coasted estimate

Implementation = W2's FSM voter #1 (`state[f] ∈ {C, M}` → HOLD at last-good anchor, widen
toward `missing_hfov`, REACQUIRE ramp on confident return). Built once, in W2. W3 stage 2 =
enabling that voter with stage-1's winning cost arm and reading the PAIR jointly:
stage 1 keeps the track honest longer; stage 2 stops the planner from chasing whatever
coast remains. The felt product lives in the pair — stage 1 alone may read flat on
viewport cells and MUST NOT be judged alone (pre-registered).

## 4. Instruments, labels, pre-registered reads

- **LABEL ASK (budget format, the only blocking ask):** near-scramble batch, **~100 clicks**.
  Consumer: stage-1 arm scoring in the near band (PIT-GT has n=1 near view; near is
  otherwise UNMEASURED at the viewport tier). Mining criterion (DECISIONS (g)): frames where
  v7 enters miss-state with a rank-1 near candidate present — mined from the **hn4@s4**
  dumps. Why this queue beats alternatives per click: these frames are the literal decision
  points of the lever under test; no existing set prices the miss-state decision.
  Confirm-not-draw; log to LABELING_LOG.md.
- Ball-position instruments (benchmark GT v1, near/far meter-recall): DIAGNOSTIC + VETO only
  (DECISIONS (k)); the deciding cells are viewport-tier.
- **Pre-registered cells:** near-band capture@600 on the near-scramble views (headline);
  sustained-loss windows count; PIT |Δcx| p90 (tail discipline); far + mid capture
  NON-regression on PIT-GT and viewport v1 under the dual rule (referee port).
- Nulls: near-scramble views are a NEW instrument → split-half null before first live read
  (event-level; expect few events — state the power floor if thin).
- Pricing: stage 1 vs **B−A** (calibration is a consumed input dimension; B = the GT-injected
  ceiling, banked for spc with 1,351 GT anchors); stage 2 vs **GT−B** (interpretation).
  Neither stage is funded for further iteration if its priced slice reads ≈0 after the
  first sweep — same discipline as Run D's lookahead verdict.
- Action table (write before numbers): winner promotion requires decisive-or-zero wins on
  the headline cells with zero decisive regressions anywhere (DECISIONS (j) walk semantics);
  ties break toward the SIMPLER arm (N > M > C in simplicity order).

## 5. Adjacent probe (NOT part of the sweep; gated separately)

EXP-OP-04 #5: the hn2@s8 chain's small far edge on the neutral cold-audit (0.962 vs 0.904,
n=52, unpowered) hints that a SPARSER/damped candidate stream yields a calmer operator.
If it replicates on a bigger neutral read (benchmark tier, more games), "stream damping"
(persistence-filtered or stride-thinned candidates into the same tracker) becomes a cheap
fourth lever — filed here so it isn't re-discovered, NOT funded until the neutral
replication exists.

## 6. Cost & sequencing

Zero clicks except the near-scramble ask; zero GPU beyond the in-flight hn4 dumps; sweep =
CPU replay (4070 CPU staged, per FLEET). Build order: after W1 table banks → W2 seam+FSM
(shared infrastructure) → stage-1 sweep (can overlap W2 tuning; different code paths) →
joint read. Estimated effort: sweep harness ~1 day, sweep runs hours (CPU), joint read
gated on the near-scramble labels.
