# Experiment Log

Each experiment has: hypothesis, method, result, conclusion. Failures are as valuable as successes.

---

## EXP-OP-30 RESULTS (geometry-conditioning go/no-go): continuous conditioning is WORSE than fd6 ON THE BENT RULER — CONFIRMS the #19 dependency; the ramp acts in the mid-depth range where the planar homography is most distorted (2026-07-29)

Continuous depth-ramp (cont15: 15→6px, cont10: 10→6px) vs fd6 (binary <6px) vs A0,
containment vs ball GT (spc/fair/chili, pitmust pending — signal robust). Δ vs A0:
| band | fd6 (binary) | cont15 (ramp) | cont10 (ramp) |
|---|---|---|---|
| far (242) | **+0.047 (p=0.001)** | +0.035 (p=0.017) | +0.045 (p=0.032) |
| mid (126) | −0.002 | +0.009 | −0.004 |
| near (126) | −0.044 (p=0.11, ns) | **−0.051 (p=0.039, WORSE)** | −0.050 (p=0.070) |
**The continuous ramp is WORSE than the binary fd6:** less far gain AND a decisively worse
near cost (cont15 near p=0.039 vs fd6's non-significant −0.044). **This is the predicted
go/no-go answer (DECISIONS (y)) and it is NO-GO on the bent ruler.** Mechanism = exactly
Mark's distortion point: the ramp applies a graded hold across the mid-depth range
(6–15px expected diameter), which is where the PLANAR homography is MOST distorted, so it
holds based on an unreliable distance and over-holds MORE balls into the far→near
transition. The binary fd6 survives only because it acts on the EXTREME far (<6px), where
even the bent ruler is unambiguous (a far ball is tiny regardless of distortion).
**CONCLUSION — the geometry-conditioning direction is RIGHT but cannot be prototyped on
the current ruler; it is GATED on a lens-compensated distance (#19).** The cheap prototype
did its job: don't invest in continuous conditioning until #19 lands. **fd6 (binary,
robust to the ruler in its extreme-far operating range) stands as the interim far lever.**
**ACT → the loop's next real lever is #19:** a lens-compensated distance-from-camera
model. Next cycle investigates whether a better distance can be prototyped from existing
code (the render's cylindrical model) or needs the full ray-geometry build.

## EXP-OP-30 PRE-REGISTRATION (geometry-conditioning investigation, before numbers): CONTINUOUS depth-conditioned hold vs fd6's binary switch (Mark's direction, DECISIONS (y)) (2026-07-29)

**PLAN (Mark 07-29):** fd6's "helps far / costs near" tension is the signature of a GLOBAL
BINARY switch on a position-dependent quantity. Test the first geometry-conditioned step:
make the hold strength a SMOOTH function of the ball's geometry-derived depth (expected
diameter) — full pnone_far_scale=2.0 at very-far (diam=6), ramping to 1.0 (no hold) at the
near-band edge (diam=near_diam), instead of the binary cliff at 6px (--pnone-far-near-diam,
w2). **DO:** cont15 (ramp 15→6) and cont10 (ramp 10→6) vs fd6 (binary) vs A0, 4 games.
**CHECK (honest containment vs ball GT, pooled events):** does the ramp KEEP the far gain
(~+0.033) while REDUCING the near cost (fd6's −0.018)? A mid lift would be a bonus
(mid-depth balls get a mild hold). **ACT/reads:** (a) cont keeps far + zeros near → the
continuous conditioning wins → it becomes the far lever and motivates the fuller
version (add frame-boundary distance; do #19 for a trustworthy ruler). (b) cont ≈ fd6 →
the binary was already near the geometry ceiling on these games; the win must come from
frame-boundary conditioning or #19. (c) cont loses far → the very-far full hold was
load-bearing; conditioning must keep the far end strong. This is a PROTOTYPE on the
current (wavy-ruler) homography — sizes the effect; the durable version is gated on #19.

## EXP-OP-29 RESULTS (PDCA cycle 5): tighter far-gate (fd6, diam<6) REFINES fn20 — keeps the far win (+0.033, p=0.001), HALVES the near dip (−0.033→−0.018 ns); fd6 is the new far config (2026-07-29)

Far-gate threshold sweep at pnone_far_scale=2.0 (--pnone-far-diam). Containment vs A0
(pooled events):
| band | fd6 (diam<6) | fd7 (diam<7) | [fn20 = diam<8, EXP-OP-26] |
|---|---|---|---|
| far (331) | **+0.033 (p=0.001)** | +0.033 (p=0.003) | +0.031 (p=0.008) |
| mid (247) | −0.004 (ns) | −0.001 (ns) | −0.005 (ns) |
| near (181) | **−0.018 (p=0.42, ns)** | −0.033 (p=0.11) | −0.033 (p=0.11) |
**Hypothesis CONFIRMED:** the fn20 near dip was a loose-gate boundary effect. Tightening
to diam<6 (hold only VERY far balls) KEEPS the far gain (+0.033, actually the sharpest
significance yet, p=0.001) AND halves the near dip (−0.033→−0.018, more clearly
non-significant). **fd6 (pnone_far_scale=2.0, pnone_far_diam=6.0) is the refined far
config** — strictly cleaner than fn20 (same far mechanism, smaller near cost), so it
inherits fn20's full-scoreboard gate PASS (EXP-OP-27) and improves on it.

**The far number climbed every cycle on the honest metric:** cycle 1 +0.021 (ns) → cycle 2
+0.031 (p=0.008) → cycle 5 +0.033 (p=0.001, near dip halved). A five-cycle PDCA arc from
a diagnostic to a decisive, refined, shippable far config — a knob, no retrain, no labels.

## EXP-OP-28 (PDCA cycle 4): the far levers do NOT compound — the HOLD (fn20) is the far lever; mgarch adds nothing on far (mgfn20 vs fn20 = +0.000) (2026-07-28)

Far containment vs ball GT, spc+fair (the mgarch-dump games), 4 arms: A0 (hn4), fn20
(hn4+hold), mg (mgarch), mgfn20 (mgarch+hold). Pooled far events (n=104):
- fn20 vs A0: **+0.056** (p=0.052) — the hold helps.
- mg vs A0: **+0.000** (p=0.875) — mgarch alone does NOT help far event-wise (confirms
  EXP-OP-23).
- mgfn20 vs fn20 (does the DETECTOR add on top of the hold?): **+0.000** (p=0.57) — NO.
- mgfn20 vs mg (does the HOLD add on top of the detector?): +0.056 (p=0.21) — yes, the
  hold is the whole effect.
**The two levers do NOT compound: the hold owns the far win; the detector contributes ~0
on far.** So the far product win is fn20 (the hold) INDEPENDENT of the detector choice —
shipping fn20 helps far whether or not mgarch is promoted, and mgarch's case stays
cross-camera (Dahua), not far. **Watch-item:** mgarch+hold dropped spc's per-game
frame-mean far to 0.633 (vs fn20 0.793) — the hold may over-hold on mgarch's differing
candidate stream on spc; event-weighting washes it out but flag before combining in
production. **ACT:** compound closed; fn20 stands alone. Cycle 5 chases the fn20 near dip
(the far-gate threshold).

## EXP-OP-27 RESULTS (PDCA cycle 3, the GATE): fn20 PASSES the full scoreboard — decisive far win, ZERO referee regressions; human-GT far +0.30 on spc worst-cases; SHIPPABLE far lever (a config knob) (2026-07-28)

fn20 (--pnone-far-scale 2.0) vs A0 on the FULL instrument suite (human-viewport GT +
composite + referee), spc + fair. Human-GT capture@600 / median:
| | spc all | spc far | spc mid | spc near | fair far | fair mid | fair near |
|---|---|---|---|---|---|---|---|
| A0 | 0.598 | 0.441 | 0.719 | 0.762 | 0.525 | 0.327 | 0.793 |
| fn20 | **0.715** | **0.738** | 0.697 | 0.690 | **0.581** | **0.358** | 0.793 |
| Δ | +0.117 | **+0.297** | −0.022 | −0.072 | +0.056 | +0.031 | 0.000 |
Composite BEAT (the GT-override far-failure frames): spc A0 0.519 → fn20 **0.719 (+0.20)**;
fair 0.458 → 0.502. MATCH ~flat both. **Referee fn20-vs-A0: ZERO decisive rows on BOTH
games — no regression anywhere under the dual rule.**

**VERDICT: fn20 PASSES — a decisive, SAFE far win.** On the full ball-GT distribution
(EXP-OP-26) it's +0.031 decisive; on the human-viewport worst-cases it recovers far
dramatically (spc +0.30, the camera finds the far ball a human wanted); the composite
far-failure column jumps +0.20. The ONLY cost is a small non-decisive near dip on spc
(−0.072, referee-clean, and near is our strength) — fair has no near cost. **This is the
arc's FIRST decisive, gated far improvement, and it is a CONFIG KNOB (pnone_far_scale=2.0)
— shippable without a retrain or labels, like the ball_select step config.**

**The PDCA arc worked:** EXP-OP-24 diagnosed far loss = tracker dropping detected balls →
cycle 1 (global hold, +0.021 ns) → cycle 2 (depth-gated, +0.031 DECISIVE) → cycle 3 (full
gate, PASS, human-GT far +0.30, zero regressions). The far number climbed every cycle on
the honest metric.
**ACT → next:** (1) fn20 is the recommended far config — surface behind the ball-select
config for Mark's ship call (side-by-side comparison video per convention). (2) Compound
levers: fn20 + mgarch (detector + hold), and fn20 + the near-RELEASE refinement to erase
the spc near dip. (3) The DETECTOR half (46% undetected far, EXP-OP-24) remains the larger
untapped lever (GPU). Artifacts: gate3_{spc,fair}.json; pdca2 fn20 campaths.

## EXP-OP-26 RESULTS (PDCA cycle 2): DEPTH-GATED far-hold = the first DECISIVE far win of the session (+0.031 containment, p=0.008 at fn20); near cost is a far→near boundary effect, non-significant at fn20 (2026-07-28)

Depth-gated miss-cost (--pnone-far-scale, boost only when top candidate <8px far). Plan
pre-stated in EXP-OP-25's ACT. Containment vs A0 (pooled events):
| band | fn20 (2x) | fn30 (3x) | fn50 (5x) |
|---|---|---|---|
| far (331) | **+0.031 (p=0.008)** ✓ | +0.036 (p=0.013) ✓ | +0.031 (p=0.044) ✓ |
| mid (247) | −0.005 (ns) | −0.008 (ns) | −0.019 (ns) |
| near (181) | −0.033 (p=0.11, ns) | −0.056 (p=0.001) ✗ | −0.049 (p=0.035) ✗ |
**Depth-gating BEAT cycle 1: far gain rose +0.021→+0.031 AND became DECISIVE (p 0.10→
0.008).** This is the FIRST decisive far-containment improvement of the whole arc, on the
honest metric, from a config knob (no retrain, no labels) — grounded in the EXP-OP-24
diagnostic. **The near cost did NOT vanish as predicted** — depth-gating the FRAME didn't
stop the boundary effect: over-holding the far track through the far→near transition
strands the near ball just after (near decisively worse at fn30/fn50). **fn20 = the
winner: decisive far (+0.031), near cost NON-significant (−0.033, p=0.11), mid clean.**
**ACT → cycle 3:** (1) GATE fn20 on the FULL scoreboard (human-viewport GT + composite +
referee, spc/fair) — confirm the ball-GT far win doesn't hide a human-GT near/mid
regression before calling it shippable. (2) If the near boundary cost matters, the next
lever is a faster near-RELEASE (drop the hold the instant a confident near candidate
appears) — cycle 4. fn20 is the standing far-lever candidate.

## EXP-OP-25 RESULTS (PDCA cycle 1): hold-far-ball VALIDATED — pnone-scale recovers far containment (+0.021 at pn15) with a small near-band cost; the first honest far lever this session (2026-07-28)

Containment vs A0 (pooled events, honest metric):
| band | pn15 | pn20 | pn30 |
|---|---|---|---|
| far (n=331) | **+0.021** (p=0.096) | +0.018 (p=0.065) | +0.010 |
| mid (247) | +0.010 | +0.020 | +0.002 |
| near (181) | −0.012 (ns) | −0.027 (p=0.077) | −0.032 |
**Hypothesis CONFIRMED (EXP-OP-24 selection half):** raising the miss cost so the tracker
HOLDS a detected candidate recovers far containment (+0.02, same magnitude as mgarch's
whole far gain, but from a config knob, no retrain). NOT yet decisive (p~0.07-0.10) but
consistent (28-32 event-wins vs 16-18 losses on far). **The cost is near-band:** a higher
global miss cost over-holds near clutter (near −0.01→−0.03 as pnone rises; small,
mostly non-significant at pn15). Mid is clean. **pn15 = the sweet spot** (far gain,
minimal near cost). This is the first far improvement this session that survives the
honest containment metric AND is grounded in the diagnostic, not an adversarial subset.
**ACT → cycle 2:** the near cost is because pnone-scale is GLOBAL — it holds candidates
everywhere. The fix is DEPTH-GATING: raise the miss cost only when the top candidate is
FAR (where detected-but-lost happens), leave near untouched (where holding hurts). Should
keep far +0.02 and zero the near drag → a clean far win. Building --pnone-far-only next.

## EXP-OP-25 PRE-REGISTRATION (PDCA cycle 1, before numbers): hold-detected-far-ball — pnone-scale sweep to recover the 272 detected-but-lost far frames (2026-07-28, overnight)

**PLAN (from EXP-OP-24):** 54% of far loss is the tracker dropping a DETECTED far ball.
Hypothesis: the miss-state (−log p_none) outweighs the faint far candidate's emission, so
the tracker takes a miss over a real ball. **Lever: raise the miss cost (--pnone-scale,
w2 56be52b) so the tracker HOLDS a detected candidate longer.**
**DO:** run-a with pnone-scale ∈ {1.5, 2.0, 3.0} vs A0 (1.0) on the 4 Reolink ball-GT
games (F-OP CPU). **CHECK (honest metrics per Mark, EXP-OP-22):** far CONTAINMENT vs ball
GT, pooled event-level sign test vs A0; AND mid + near containment non-regression (a
higher miss cost could over-hold onto distractors near-band). **ACT/gate:** a pnone value
that IMPROVES far containment (decisive or clean positive) with NO decisive mid/near
regression advances to cycle 2 (tune / combine with arm M); if all hurt near/mid or
don't move far, pnone is closed and cycle 2 tries the emission side (boost far candidates
directly) or the detector half. No labels; CPU-only.

## EXP-OP-24: FAR-LOSS ANATOMY (the keystone diagnostic) — far loss is 100% TRACKING, 0% framing; split ~54% selection (ball detected, tracker didn't hold) / ~46% detector (ball not detected, worst on windy fair) (2026-07-28)

For every far ball-GT frame where the ball is NOT contained in our viewport (champion
hn4 chain, 4 Reolink games), partition the cause. Two diagnostics:
**(1) framing vs tracking** — is the tracker's trajectory near the ball (planner failed to
frame) or off it (tracker lost it)? **505 uncontained far frames: 0 framing, 505 tracking
(100%).** When the tracker has the ball, the planner frames it (P5 confirmed centering is
near-optimal); a far loss is ALWAYS the tracker not being on the ball. **This kills W4
(wider/smarter far framing) as a far lever — a wider view over a lost track just shows
empty field.**
**(2) of the tracking misses, detector vs selection** — was a candidate present near the
true ball in the raw dump (detected, tracker didn't hold → SELECTION lever) or absent
(→ DETECTOR lever)?
| game | trk-miss | detected-but-lost | not-detected |
|---|---|---|---|
| spc | 260 | 169 (65%) | 91 |
| fair | 132 | 41 | **91 (69%)** |
| chili | 94 | 47 | 47 |
| pitmust | 19 | 15 | 4 |
| **pooled** | **505** | **272 (54%)** | **233 (46%)** |
**Two roughly-equal levers, and they differ BY GAME:** spc far loss is mostly SELECTION
(65% detected-but-lost — the detector finds the faint ball, the tracker drops it); windy
fair far loss is mostly DETECTOR (69% never detected — the ball is too faint in wind).

**STRATEGIC CONSEQUENCE — the far roadmap is now data-grounded:**
- **W4 (far framing): DEAD** (0% framing misses).
- **Selection/tracker lever (272 frames, the larger half):** hold a DETECTED far ball
  through the tracker — miss-state cost / emission weighting when a far candidate exists.
  This is W3's real target (NOT the arm-N entry cost, closed EXP-OP-20; the lever is
  holding, not entry-pricing). Tractable, CPU-only, no labels.
- **Detector lever (233 frames):** detect the faint far ball, especially in wind — the
  detector arc was "closed" but far detection is still ~half the far loss. Harder (GPU
  training, windy-far is fundamentally hard); the mgarch modest far gain (EXP-OP-23) is
  in this half.
**Next (overnight PDCA): attack the SELECTION half first** — sweep the hold levers
(miss-cost / pnone-scale / arm M) for recovery of the 272 detected-but-lost far frames on
the honest CONTAINMENT metric, mid/near non-regressing. Artifacts: far_diag inline;
campaths + dumps on F-OP.

## EXP-OP-23: mgarch's FAR WIN re-checked on the honest metric — a small frame-wise containment gain (+0.03), NOT the headline "0.44→0.71"; that number was capture@600 on the adversarial viewport-worst subset (2026-07-28, Mark's containment principle)

Applying the EXP-OP-22 correction to the PROMOTION case: mgarch (candidate) vs hn4
(champion), far ball-GT frames, spc+fair (the games with mgarch dumps), capture@600 AND
containment:
| game | n_far | cap hn4→mg | contain hn4→mg |
|---|---|---|---|
| spc | 1052 | 0.683→0.702 (+0.018) | 0.753→0.781 (+0.029) |
| fair | 178 | 0.236→0.253 (+0.017) | 0.258→0.292 (+0.034) |
**Per-frame: mgarch is better on far on BOTH games and BOTH metrics — but the gain is
SMALL (+0.018 cap, +0.03 containment), not a landslide.** Pooled event-level (104 events):
containment TIED (0.691/0.691, p=0.87); capture mgarch decisively WORSE (−0.072, p=0.03 —
mg wins 12 events, loses 26). So mgarch's per-frame far gains come from a few big events
while it centers slightly worse across many small ones; containment forgives the centering
(zoom contains the ball) → event-wise tied.

**RECONCILIATION with EXP-OP-19's "spc far 0.44→0.71":** same pattern as the filter — that
headline was capture@600 vs the HUMAN VIEWPORT-GT on the DIV-SAMPLED viewport-worst subset
(curated AutoCam-failure moments). Against BALL GT on the FULL far distribution with
CONTAINMENT, mgarch's far edge is a real but modest **+0.03 frame-wise, not decisive
event-wise.** The visible far saves (the promotion win clip) are real — they're the big
adversarial moments — but they're a minority of far frames; the average far frame is only
slightly better.

**PROMOTION IMPACT (honest):** mgarch's far advantage is genuine but SMALL on the
product-relevant full-distribution containment metric — NOT the crushing win the
capture@600-on-worst-subset numbers implied. **mgarch's core case is NOT far; it is
CROSS-CAMERA (Dahua) robustness + non-regression** (its Dahua-refresh design purpose,
EXP-OP-19: PIT 0.54→0.64) AND it does not make far worse. The far headline across the arc
has been inflated by (a) adversarial subsets and (b) the capture@600 centering proxy;
corrected, the far story is "modestly better, not decisive." **Standing method fix: all
"far win" claims report containment vs ball GT on the full distribution, not capture@600
on curated viewport subsets.**
**Artifacts:** analysis inline; spc/fair hn4+mgarch campaths in ladder; ball GT MD5-verified
(EXP-OP-22).

## EXP-OP-22 RESULTS: POWER REVERSES THE FILTER'S FAR STORY — pooled over 4 Reolink games vs BALL GT, F is DECISIVELY WORSE on far (−0.057, p=0.003); its earlier far "win" was specific to the adversarial viewport-worst subset (2026-07-28)

The breadth dumps (chili + pitmust, hn4@s4) finished 13:39; A0 + F campaths generated
for both; pooled the FAR-band (expected diam < 8px) capture@600 vs BALL GT across the
FOUR Reolink ball-GT games. Per-game far capture (A0 champion vs F filter):
| game | n_far | A0 | F | ΔF |
|---|---|---|---|---|
| spc | 1052 | 0.683 | 0.609 | **−0.074** |
| fair | 178 | 0.236 | 0.275 | +0.039 |
| chili | 413 | 0.697 | 0.680 | −0.017 |
| pitmust | 398 | 0.937 | 0.915 | −0.023 |
**Pooled (331 far events): A0 0.769 vs F 0.712, ΔF −0.057.** Event sign test: F beats A0
in 25 events, LOSES in 52 (254 ties) → **two-sided p=0.0028 — DECISIVE that F is WORSE**
on far ball-capture. F helps only fair (smallest n); it hurts on the three larger games.

**Reconciliation with EXP-OP-10/11 (F far "win" 0.441→0.669 on spc):** NOT a contradiction
— different instrument + frame set. Those measured F vs HUMAN VIEWPORT GT on the
DIV-SAMPLED viewport-worst subset (curated AutoCam/champion FAILURE moments), where F
rescues the camera off a static distractor toward the ball. THIS measures F vs BALL GT
over the FULL far distribution. Both are true: **F improves the worst curated far cases
but is net-negative on average far ball-capture** — most far events are unchanged (254/331
ties), but where the filter acts it drops a real far-ball candidate more often than it
saves a distractor-park. The full-distribution ball-GT read is the more fundamental "do we
keep the ball in frame," and at 4-game power it decisively says F costs far.

**DECISION IMPACT — F@0.20 is not a general far WIN, but "decisively worse" was a
CENTERING-PROXY artifact (corrected below).**

**[CORRECTION 07-28, Mark's catch — capture@600 is x-centering, not "ball in viewport";
re-run with CONTAINMENT (ball inside the planned 16:9 view rect, folding in zoom+y)]:**
| metric | A0 | F | ΔF | sign test | verdict |
|---|---|---|---|---|---|
| capture@600 (x-centering PROXY) | 0.769 | 0.712 | −0.057 | F<A0 52/25, p=0.003 | decisive F worse |
| CONTAINMENT (ball actually in frame) | 0.809 | 0.779 | −0.029 | F<A0 44/29, p=0.10 | **NOT decisive** |
Per-game containment ΔF: spc −0.088, fair +0.022, chili **+0.017** (flips positive),
pitmust −0.005. So on the PRODUCT-relevant "is the real ball in the rendered view" metric,
F is roughly NEUTRAL on far, not decisively worse — the wider zoom contains the ball even
when centering drifts. **My "decisive F worse on far" was the centering proxy; the strict
metric retracts it.** (Mark's principle: a model can center worse yet keep the ball framed
— containment, not centering, is the viewport truth. capture@600 stays useful as a
tighter-framing proxy but is NOT "ball in viewport".)

**Corrected F verdict:** F is NOT a general far win (containment neutral, not positive —
its far benefit is confined to the EXP-OP-11 adversarial viewport-worst subset, not the
full far distribution) AND not a decisive far loss. The case against shipping F blanket now
rests on EXP-OP-16 (F's mid dip decisive-worse vs AC on fair, composite) + EXP-OP-20
(composition negative) + F not delivering the far win it was proposed for — NOT on a
decisive far regression. If revived: gate it to fire only on detected distractor-parks.
The far product win still comes from the DETECTOR (mgarch, EXP-OP-19) + W4 framing — but
that mgarch far claim should ALSO be re-checked on containment, not just capture@600
(next).
**Method note:** pit (2024 Dahua) EXCLUDED — no ball GT (viewport-GT only); the pool is
the 4 Reolink ball-GT games. Artifacts: breadth_ladder campaths; analysis inline. **Input
integrity VERIFIED (07-28):** the ball_labels.jsonl the read consumed on F-OP is MD5-
identical to the server originals for all four games (spc DD55FF…/1785, fair E544B3…/216,
chili 41332F…/845, pitmust 22889B…/811) — the analysis ran on staged copies but they are
byte-exact to the authoritative F:\Heat_2012s data (F-OP's local F: ≠ the server F:, which
it reaches via the video share; the path fell back to staged copies, same bytes).

## EXP-OP-21 RESULTS: a single GLOBAL planner tuning improves ALL THREE families — the fit-on-pit config is the universal winner (+spc/+fair/+pit); the shipped planner has modest composite headroom; generality CONFIRMED, no venue memory (2026-07-28)

Three 500-sample fits (F-OP CPU, alongside the breadth dump). Identity gates passed
(spc 1.9 / fair 0.44 / pit 2.9 px within the rounding floor); baselines reproduced the
banked composite cells (spc 0.602 / fair 0.669 / pit 0.880). Winner gain on the fit
game + transfer to the two held-out games:
| fit-on | fit gain | spc | fair | pit |
|---|---|---|---|---|
| spc | +0.0276 | (fit) | **+0.0150** | −0.0176 |
| fair | +0.0154 | −0.0013 | (fit) | −0.0099 |
| **pit** | +0.0103 | **+0.0155** | **+0.0110** | (fit) |

**The fit-on-pit config is the only UNIVERSALLY-positive one** — it lifts its own game
AND both held-out Reolink games (spc +0.0155, fair +0.0110). The spc-fit gets a bigger
spc gain but sacrifices Dahua (−0.018, within its ±0.06 null); the fair-fit is
fair-specific (flat on spc). **Every fit that scores spc/fair shows fair up (3/3) and
spc up (2/3)** — convergent evidence the shipped planner is NOT optimally tuned and a
better GLOBAL config exists. Generality is thus CONFIRMED with a single config (satisfies
the binding no-venue-memory rule — no per-game tuning).

**The winning direction (fit-on-pit knobs vs shipped):** a CALMER, slightly WIDER
operator — pan_smoothing_max 0.12→0.07 (steadier top gain), lead_frames 8→9.1,
max_lead_room 0.20→0.22, zoom_scale 0.90→0.99 + zoom_speed_gain 8→12.7 (gently wider,
more speed-responsive), zoom_smoothing 0.03→0.013. Coherent (steadier + wider = keeps
the play framed), NOT a dramatic re-tune. The spc-fit's more aggressive widening
(zoom_base 47→52) is what broke Dahua — the gentle version generalizes.

**Calibration (do not overstate):** every per-game gain (~+0.01 to +0.03) is COMPARABLE
TO / within the composite capture null bands (±0.06–0.09, EXP-OP-16), so NO single game's
gain is decisive on its own. The evidence is CROSS-GAME CONSISTENCY — all three positive
under one config, and fair up in every fit — not per-game significance. This is a
suggestive modest global gain, not a decisive win.

**Gate + caveats (pre-registered):** the fit-on-pit config PASSES the pre-stated gate —
improves all three, regresses none beyond the null band. It is a promotable CANDIDATE, **not shipped from
here.** Two honest caveats before it becomes real: (1) the gains are containment-leaning
(the composite objective weights containment = capture equally; a gentle widen buys
containment cheaply) — the referee dual-rule on capture cells + a side-by-side visual
(the convention) must confirm it's broadcast-real, not metric-gaming; (2) re-fit after
the breadth games land (more data, decisive power) and after any champion shift (mgarch).
**Next:** score the fit-on-pit config through the standard scoreboard+referee on all
GT sets; visual comparison video; fold into the post-breadth re-read.
**Artifacts:** p0_fop/fit_{spc,fair,pit}.json (winner knobs + cells).

**[REFEREE GATE 07-28 — the composite gain does NOT survive on human GT; SAFE but not a
win]:** re-planned the fit-on-pit campath and scored it vs A0 on the HUMAN-GT viewport
cells (capture@600 — a different instrument than the AutoCam-dense composite the fit
maximized). **Referee fitpit-vs-A0: ZERO decisive on all three games — SAFE, no
regression anywhere.** But the composite gain does NOT translate to human-GT capture:
spc all 0.598→0.589 / far 0.441→0.434 / mid 0.719→0.707 (slightly DOWN — centering a
touch worse), fair ~flat (near 0.793→0.815 up), pit all 0.542→0.547 / mid 0.640→0.653
(slight up). **Mechanism: the fit's gain was the containment term, and containment
rewards a WIDER view — the fit widened (zoom_scale 0.90→0.99) to contain the reference
more often WITHOUT improving where a human says to point.** The gate did its job: a
composite win that is containment-gaming, not operator improvement.
**CONSEQUENCES:** (1) the P5 objective as posed (maximize composite cap600 + containment)
is exploitable by widening — **re-pose it: capture-weighted, or penalize over-widening,
or fit to human-GT capture directly where sets exist.** (2) A composite-standard
subtlety worth a DECISIONS note: the containment term can be gamed by zoom; consumers
that fit/optimize against it must guard the width. (3) The fit-on-pit config is SAFE to
keep as a fallback but is NOT promoted — no human-GT win. W5 re-runs with the re-posed
objective (and after the breadth games add power). This SUPERSEDES the "promotable
candidate" framing above: safe, yes; a win, no.
**Gate artifacts:** p0_fop/fitgate_{spc,fair,pit}.json.

**[CAPTURE-ONLY RE-FIT 07-28 — the close-out: the shipped CENTERING tuning is already
near-optimal; the composite "global win" was containment]:** re-ran all three fits with
``--objective capture`` (capture@600 alone, no containment — not zoom-gameable).
cap600 baselines spc 0.534 / fair 0.626 / pit 0.892. Winners:
- fit-on-spc: spc **+0.020**, fair −0.006, pit −0.023 (spc-specific, NO transfer)
- fit-on-fair: fair +0.006, spc +0.006, pit −0.030 (barely moves; hurts Dahua)
- fit-on-pit: pit +0.003, spc +0.005, fair +0.001 (FLAT everywhere)
Removing the containment reward SHRINKS every gain (spc 0.028→0.020, fair 0.015→0.006,
pit 0.010→0.003) AND kills the universal transfer — the "global" pit-fit is now flat
(all ≤ +0.005, within noise). **Conclusion: the composite objective's apparent global
improvement was almost entirely the containment (widening) term; the shipped
PlannerConfig CENTERING (pan/velocity/lead) tuning is already near-optimal — capture-only
fits find only tiny, non-generalizing gains.** P5 planner-centering-fit is CLOSED
NEGATIVE: there is no real centering headroom in the base follow tuning on these three
games. **W5 re-scopes:** future fit value is (a) RE-fitting after the tracker/champion
changes (mgarch, W3 — the fit clones where the tracker puts the ball, so it moves when
the tracker does) and (b) fitting the W2 HOLD-FSM / deadband knobs (a different lever) —
NOT the base follow knobs, which are done. The breadth games will confirm at power but
are unlikely to overturn a clean cross-game null.
**Artifacts:** p0_fop/fitcap_{spc,fair,pit}.json; --objective flag @ w2.

## EXP-OP-21 PRE-REGISTRATION (committed before numbers): W5/P5 operator-imitation fit — clone the composite's discipline by searching planner knobs (2026-07-27, overnight)

**Build (w2, fit_planner.py):** random-search 10 framing knobs of PlannerConfig
(pan_smoothing_min/max, pitch_smoothing, velocity_ema, lead_frames,
max_lead_room_fraction, zoom_base_deg, zoom_scale, zoom_speed_gain_deg,
zoom_smoothing) — NOT the hold-FSM gate (W2 owns it), NOT fps. Each sample re-plans
a game's cached trajectory/2 (no re-track) and scores capture@600 + containment vs
that game's composite, via the scoreboard's own capture_contain_stats. Identity gate
(rule 8): re-planning at the shipped config reproduces the banked A0 campath within
the 5 px trajectory-save rounding floor (verified: spc 1.9 / fair 0.44 / pit 2.9 px;
baselines spc 0.602 / fair 0.669 / pit 0.880 = the banked composite cells).
**Design:** three fits — fit-on-spc, fit-on-fair, fit-on-pit — 500 samples, seed 72,
each reporting the winner's gain on its fit game AND its transfer to the two HELD-OUT
games (DECISIONS (k): never fit and score the same game).
**Reads:** per fit, fit-game gain + holdout gains. **The generality question:** does a
Reolink fit transfer spc↔fair (a better GLOBAL follow tuning) and to Dahua pit
(cross-camera)? **Gate (pre-stated):** a fitted config is a CANDIDATE only if it
IMPROVES its fit game AND does not regress EITHER holdout beyond the composite's block
null band (spc/fair ~±0.09, pit ~±0.06 from EXP-OP-16); positive holdout transfer on
both Reolink games = a promotable global tuning (then the standard scoreboard+referee
gate, not shipped from here). No per-game memory — one global config or nothing (the
binding no-venue-memory rule). A 20-sample harness-verify peeked spc +0.016 / fair
+0.011 / pit −0.006 — a hint the fit transfers; the registered 500-sample run decides.
**Machine:** F-OP CPU alongside the breadth GPU dump.

## EXP-OP-20 RESULTS: arm N (near-entry) is the WRONG LEVER — the near failure is a SUSTAINED emission gap an entry cost cannot touch (0.452 flat to k=4); arm M (margin) shows a raw fair-far gain (+0.11) but nothing referee-decisive (2026-07-27)

Full k-sweep on hn4@s4 (spc/fair/PIT) + an arm-N follow-up on the mgarch dumps (the
stream whose near band actually fails). F-OP CPU, ~40 min; ZERO referee-decisive rows vs
A0 for ANY arm on ANY game (the known far/near power ceiling — few events).

**Arm N (near+slow entry cost) — CLOSED, mechanism found.** On hn4 it is near-inert
because hn4's near band is not failing (spc near already 0.762); the only motion is spc
mid 0.719→0.751 at k≥2 (a side effect, not the target). **On the mgarch dump — the
promotion candidate WITH the 0.452 near failure — arm N moves the near cell by exactly
ZERO at k=1/2/4** (0.452/med 3612 unchanged even at 5× entry cost). Mechanism (confirmed
synthetically + here): the entry multiplier only DEFERS miss by a frame or two; the near
event is a SUSTAINED window where the ball candidate's emission stays costlier than the
miss floor for the whole ~4.4 s (the EXP near-autopsy: P(ball) 0.07–0.21 < P(none)), so
entry pricing cannot hold the track. **The near fix must be per-frame miss-EMISSION (raise
the miss floor while a confident near candidate PERSISTS) or coast-policy (stop the
planner following the coast — W2's HOLD FSM, already built), NOT entry cost.** This
retires arm N and re-points the near lever at W2/W3-stage-2.

**Arm M (rank-1 margin entry cost) — raw-promising on fair far, overcorrects hot.**
capture@600 by band, A0 → best M:
| game | cell | A0 | M@0.5 | M@1 | M@2 | M@4 |
|---|---|---|---|---|---|---|
| fair | far (217) | 0.525 | **0.636** | **0.636** | 0.641 | 0.641 |
| fair | ALL | 0.449 | 0.483 | 0.483 | 0.482 | 0.477 |
| pit | far (397) | 0.481 | 0.491 | 0.491 | 0.489 | 0.481 |
| spc | far (290) | 0.441 | 0.441 | 0.441 | **0.400** | 0.400 |
| spc | ALL | 0.598 | 0.596 | 0.598 | 0.573 | 0.576 |
fair far +0.11 raw (closes ~⅓ of the gap to AutoCam's 0.857 on the W4 target cell) with
NO decisive regression; but k≥2 OVERCORRECTS — it starts pricing legitimate entries and
spc far drops 0.441→0.400. **Not decisive anywhere (power); not shippable on raw alone.**
The fair-mid cell (EXP-OP-18's P2 gate) is UNMOVED by either arm (0.327) — as expected:
that dip is the static FILTER's re-solve, a different lever than miss-entry.

**Action (pre-registered):** arm N retired (wrong lever, mechanism logged); arm M @ k=0.5
carried as a raw fair-far candidate — NOT promoted on raw alone; next reads (deferred,
not blocking): M on the mgarch stream, and the JOINT read with W2 coast-policy (stage 1
was never to be judged alone). Arm C (the N×M combine) is MOOT — N contributes nothing.
The near failure's real owner is coast-policy/emission, now explicit in the roadmap.
**Artifacts:** p0_fop/op20_{spc,fair,pit}.json + op20b_{spc,fair}.json + campaths.

## EXP-OP-20 PRE-REGISTRATION (committed before numbers): W3 stage-1 — three-arm state-dependent miss-ENTRY cost sweep (task #22) (2026-07-27)

**Build (w2 9b40c28, per W3_LOSS_DISCIPLINE_DESIGN.md):** `miss_entry_multiplier` on the
tracked->miss ENTRY transition only, from the frame's TOP candidate. **Arm N** =
`1 + k_N` when the candidate is NEAR (EXPECTED diameter >= 15 px, the band convention)
AND SLOW (world step <= 0.15 m/source-frame ≈ 3 m/s @ 20 fps; unknown step counts as
slow). **Arm M** = `1 + k_M * adv * sep`: adv = (floor − e_top)/floor clipped [0,1]
(0 when rank-1 does not beat the miss floor — the near-autopsy class belongs to arm N);
sep = (e_2nd − e_top)/(|e_top|+|e_2nd|) clipped [0,1], 1 for a lone candidate. Anchored
frames (floor = inf) disable arm M. Defaults 0 = bit-identical (suite-verified).
Synthetic validation: arm N holds a 5-frame near-slow scramble the default abandons and
stays silent at far; k sweep semantics confirmed (an entry cost DEFERS — it cannot and
must not beat a persistent emission gap).
**Sweep:** k_N ∈ {0.5, 1, 2, 4} and k_M ∈ {0.5, 1, 2, 4} independently; arm C = the 2x2
of per-arm winners after the singles read. Games: PIT + spc + fair **hn4@s4 dumps only**
(dump-provenance rule). F-OP CPU, after the T21 GPU chain drains.
**Readable cells NOW (labels absent — stated, not silent):** THE FAIR-MID CELL
(EXP-OP-18's zero-click primary: fair mid GT capture + composite fair-mid MATCH/BEAT
within the banked block-null bands) · spc near cell (n=42) · PIT |dcx| p90 (tail) ·
far+mid capture NON-regression on all three GT sets (dual rule vs A0, computed from
campaths + labels via the referee port). The near-scramble HEADLINE cells defer to the
~100-click batch. **Pre-registered caveat (design):** stage 1 may read flat alone and is
NOT judged alone — the joint read with W2's coast-policy voter follows; but a DECISIVE
regression anywhere kills an arm outright ((j) walk).
**Action table:** winner = decisive-or-zero improvements on readable cells with ZERO
decisive regressions; ties break toward the simpler arm (N > M > C). All arms flat on
readable cells → park stage 1 until the near-scramble labels exist; no re-tuning beyond
the pre-registered k grid.

## EXP-OP-19 RESULTS: #21 GATE PASSES on the archived mg_ctrl — primary-family viewport NON-regression (zero decisive vs A0 on spc+fair), far substantially UP; the one near cost is the known 4.4s W3 event; promotion package READY for Mark (2026-07-27)

Both primary-family legs at the archived unseeded mg_ctrl ckpt (F-OP, dumps ~7 h/game
decode-bound at stride 4; the chain ran clean overnight). mgarch vs A0 (current champion
hn4) on the GT sets (capture@600/med):
| game | band | A0 | mgarch | AC |
|---|---|---|---|---|
| spc | ALL | 0.598/232 | **0.695/223** | 0.640/453 |
| spc | far | 0.441/1625 | **0.707/184** | 0.443/752 |
| spc | mid | 0.719/82 | 0.716/235 | 0.804/354 |
| spc | near (42) | 0.762/25 | 0.452/3612 | 1.000/47 |
| fair | ALL | 0.449/754 | **0.593/363** | 0.676/413 |
| fair | far | 0.525/360 | **0.751/121** | 0.857/285 |
| fair | mid | 0.327/960 | **0.426/859** | 0.605/500 |
| fair | near (92) | 0.793/281 | **0.946/206** | 0.356/618 |

**GATE = PASS.** Dual-rule referee mgarch-vs-A0: **ZERO decisive rows on either game, any
band** — primary-family viewport non-regression is met with room (far is a large raw
gain: spc +0.27, fair +0.23; fair near +0.15). The PIT confirmation is already banked
(EXP-OP-16, this same ckpt: 0.644 vs 0.542). The (t) coverage-serves-robustness note
holds (fair far/near both up — the Dahua-data effect transfers to the Reolink primary).
**The one soft cell** — spc near 0.762→0.452 (med 3612) — is NOT decisive (n=42, power)
and is the SAME single contiguous ~4.4 s near event EXP-OP-12's seed-1 leg found: the
tracker-dynamics near-autopsy class, i.e. **exactly what W3 stage-1 (EXP-OP-20, built @
w2 9b40c28) targets.** The archived ckpt reproducing seed-1's near behavior to the digit
also RE-VALIDATES the supersession (EXP-OP-19 prereg): same recipe, same result, one
checkpoint for all legs.
**#21 status: PROMOTION PACKAGE READY — decision is Mark's** (pre-registered action). All
gate legs clean; no-straddle satisfied (P0 banked). Promoting = champion shift to mgarch
+ scoreboard re-anchor (cheap, CPU) + the standard config swap. HELD pending Mark's
promote/hold call; nothing auto-promoted.
**Artifacts:** p0_fop/t21_{spc,fair}_mgarch.json + *_mgarch_s4.campath ladder.

## EXP-OP-19 PRE-REGISTRATION (committed before numbers): #21 gate RE-ANCHOR on the archived mg_ctrl — all legs at ONE checkpoint; the stranded seed-1 leg is superseded (2026-07-27)

**Why (Mark's directive + the 4070 outage):** F-OP is the GPU for ~2 weeks; the 4070 is
off-LAN and NOT on the tailnet, stranding mg_ctrl seed-1's checkpoint AND its fair dump.
The gate cannot mix seeds across legs (far-argmax seed variance ±0.04-0.08 is measured).
**Resolution: re-anchor every leg on the ARCHIVED Phase-2 mg_ctrl** (unseeded, epoch 15,
index_v1, sha-identical across both archive copies; ckpt metadata verified) — the SAME
checkpoint behind the banked PIT leg (campath_pit_mg_ctrl, 0.644 reproduced in
EXP-OP-16). spc + fair dumps at this ckpt run on F-OP's GPU (~1 h/game measured on the
1060-class chain), then the standard 6-min CPU reads.
**Reads (per game, same instrument as EXP-OP-16, fps 30):** mgarch vs A0/F/AC on the GT
sets + composite cells + referee. **Gate criteria (pre-defined in docs):**
primary-family viewport NON-regression (no decisive mgarch-vs-A0 loss on spc or fair
GT views) + the banked PIT confirmation (0.644 > 0.542, this ckpt) + the (t) framing
note (coverage serves cross-camera robustness). The seed-1 spc leg (EXP-OP-12, PASS) is
SUPERSEDED with provenance noted — its arm was a different training run of the same
recipe; its result stands as corroborating history, not a gate leg.
**Action mapping:** all legs clean → the #21 promotion package goes to Mark for the
promote/hold call (champion shift = re-anchor scoreboards per the no-straddle rule,
already satisfied — P0 banked). Any decisive regression → #21 held, mechanism entry.

## EXP-OP-18 RESULTS: fair-mid dip = RE-SOLVE COMMITMENT, not deletion — 44/46 lost views are confident-T on another route; truth deleted at 1/46; W3 stage-1 is the P2 gate (2026-07-27)

**fair (the decisive family): mechanism (b), overwhelmingly.** Of the 46 mid GT views
where A0 captures and F misses (6 gap-64 events, the two dominant 19- and 16-frame
clusters late-game at g91.9k / g92.2k): F's trajectory/2 state is **T (confident) at
44/46** (2 coasts, 0 misses) with the F path 1.4-2.5k px from A0's at event start — the
filtered lattice re-solves onto a DIFFERENT object/route. **The deletion theory is
dead:** the fair filter has only 19 static centers, and A0's followed ball sits within a
filtered radius at just **1/46** lost views; at 16/46 the ball candidate demonstrably
SURVIVES filtering while the re-solve still commits elsewhere (the remaining 29 are
undecidable at stride-4/smoothing resolution — A0's point there is not same-frame
detection-backed; stated, not assumed). **Consequence (pre-registered mapping (b)):
miss-cost / commitment calibration — W3 stage-1's exact arms — is THE P2-gate work; its
primary read adds the fair-mid cell** beside the near-scramble cells. The cheap
conditional-filter fix is NOT indicated.
**spc (non-decisive family): different mechanism — (c) planner-side at 21/32** (tracker
near GT, campath not; + 5 coast, 6 re-solve). The spc mid dip is mostly planner
smoothing over filter-perturbed paths — W5-fit/deadband territory, secondary while
non-decisive.
**Artifacts:** analysis inline from p0_fop ladder artifacts + fair_hn4s4 dump (static
recompute at the promotion params); frames listed above for the future label/vision pass.

## EXP-OP-18 PRE-REGISTRATION (committed before numbers): fair-mid dip autopsy — WHERE does F lose the mid views it costs vs A0? (2026-07-27)

**Question (the P2 gate):** F's fair-mid loss vs AC is decisive (EXP-OP-16); EXP-OP-11's
mechanism guess was "indirect global-path re-solve, not deletions" (from F2's keeper
evidence). Distinguish, ON THE LOST VIEWS THEMSELVES (fair mid GT views where A0
captures@600 and F does not):
- **(a) deletion-adjacent:** F's trajectory/2 state at the lost views is C/M
  (coast/miss) — the filter starved the tracker of the object it was following;
- **(b) re-solve commitment:** state stays T with the F path confidently elsewhere —
  the lattice re-solve chose a different object/route;
- **(c) planner-side:** F's TRACKER point is near GT but its CAMPATH is not —
  divergence enters between tracker and planner.
**Reads:** per lost view/event — F traj state composition; |F_traj − GT| vs
|F_campath − GT|; F-vs-A0 campath divergence span around the event (contiguous cluster
vs scattered); same for the SPC mid dip (non-decisive there — is it the same mechanism?).
**Action mapping:** (a) → the filter must protect the actively-tracked candidate (ties
into W3's coast flags; a cheap conditional-filter fix); (b) → miss-cost / commitment
calibration territory (W3 stage-1's exact lever, raising its priority); (c) → planner
knob work (W5 fit or deadband). Mixed → report the split; the dominant mode drives P4.

## EXP-OP-17 RESULTS: COMPOSITION NEGATIVE — mgF far 0.597 < mgctrl 0.724 AND < F 0.669; the far levers do NOT stack; closed per the pre-registered action (2026-07-27)

spc classic cells (capture@600/med): **far mgF 0.597/251 vs mgctrl 0.724/163 vs F
0.669/202** — the primary cell FAILS (below both single arms). ALL 0.618 < mgctrl 0.687;
mid 0.647 < 0.685. Composite far: mgF MATCH 0.406 / BEAT 0.381 — worst arm measured. No
referee-decisive mgF rows (power), but the raw ordering is uniform. **The filter degrades
the mgctrl candidate stream** — coverage's far gains ride on detections/paths the
pixel-persistence suppression disturbs; the EXP-OP-11 mid mechanism (indirect global-path
re-solve) plausibly compounds on the richer stream. **Mechanism nugget for W3/P4:** the
filter FIXES mgctrl's near static-park catastrophe (near med 3612 -> 189, cap 0.452 ->
0.548) — the near failure is static-distractor parking, filterable; the far cost is the
price. **Action (pre-registered): composition CLOSED on spc; single arms stand — mgctrl =
best far coverage arm (0.724), F = best filter arm (0.669). The far product stack is NOT
mgctrl+F as-is;** any revival needs a mechanism-level change (e.g. filter-aware
re-solve or world-cell static_w), not re-tuning. fair/PIT legs remain unrun (no dumps) —
if #21 promotes on the fair leg, the composition question is MOOT unless far regresses.
**Artifacts:** p0_fop/p1_spc_composition.json + spc_mgctrl_s4.F.* campath; F-OP ~24 s.

## EXP-OP-17 PRE-REGISTRATION (committed before numbers): P1 composition read — mgctrl x F@0.20, spc leg (2026-07-27)

**Hypothesis (EXP-OP-12's open read):** coverage (mgctrl) and interpretation (F) attack
far independently (0.724 / 0.669 on spc) — the composition may stack. **Arm mgF** =
run-a on the spc mg_ctrl dump WITH the F filter (frac 0.20 / cell 50 / radius 60,
pixel-space), scored beside the banked A0 / F / mgctrl arms on the SAME instrument as
EXP-OP-16 (spc GT set + composite, referee, fps 30, chain @ w2 127b633, F-OP).
**Primary cell:** spc far capture@600 (classic GT-set) — does mgF exceed
max(mgctrl 0.724, F 0.669)? **Guards:** mid/near non-regression vs mgctrl alone (raw,
with dual-rule check); NO new decisive AC losses (the (t) floor — fair's F-mid lesson).
**Action mapping:** mgF ≥ best single arm on far with guards clean → mgF becomes the
far product-stack candidate, pending the fair leg (blocked on the 4070) before any
promotion; guards dirty or no far gain → the single best arm stands and the composition
is closed as measured. **Scope:** spc only — fair/PIT mgctrl dumps do not exist
(4070-blocked / never made); stated up front, not a silent cap.

## EXP-OP-16 RESULTS: P0 COMPOSITE RE-ANCHOR BANKED — instrument certified on F-OP; the fair-far story REVERSES with the recovered AC reference; F's mid dip goes DECISIVE on fair (P2 blocked); PIT mgctrl provenance confirmed by exact reproduction; MATCH-null unit broken as operationalized (2026-07-27)

**Instrument certification (all three anchors):** PIT --fixture-exp72 reproduces on
FORTNITE-OP to the digit (A0 0.542/450, AC 0.759/207, n=640); the regenerated spc arms
reproduce EVERY EXP-OP-14 composite cell exactly (A0 0.535/0.671 · F 0.471/0.582 ·
mgctrl 0.506/0.651 MATCH; 0.519/0.560/0.583 BEAT); spc classic cells reproduce
EXP-OP-11/12 (F far 0.669, mgctrl far 0.724, AC far 0.443). Chain @ w2 127b633; full
P0 chain (7 replays + 3 scoreboards incl. nulls + referee) = **6 min wall on the 5800X**.

**Composite cells (capture@600 / containment; MATCH = dense tier-ac, BEAT = GT-override):**
| game | arm | MATCH all | BEAT all | far MATCH | far BEAT |
|---|---|---|---|---|---|
| spc | A0 | 0.535/0.671 (75k) | 0.519/0.587 (859) | 0.533/0.692 | 0.448/0.498 |
| spc | F | 0.471/0.582 | 0.560/0.616 | 0.477/0.597 | 0.606/0.647 |
| spc | mgctrl | 0.506/0.651 | 0.583/0.653 | 0.497/0.678 | 0.560/0.587 |
| fair | A0 | 0.627/0.711 (89k) | 0.458/0.648 (321) | 0.608/0.699 | 0.447/0.468 |
| fair | F | 0.565/0.658 | 0.517/0.564 | 0.550/0.648 | 0.617/0.638 |
| pit | A0 | 0.892/0.867 (102k) | 0.747/0.714 (154) | 0.902/0.881 | 0.500/0.500 (14) |
| pit | F | 0.818/0.800 | 0.584/0.519 | 0.834/0.824 | 0.929/0.500 |
| pit | mgctrl | 0.788/0.767 | 0.539/0.500 | 0.797/0.781 | 0.714/0.357 |
(The composite AC row is a TAUTOLOGY — MATCH 1.000 / BEAT 0.000 by construction — its
information lives in the referee rows and the classic GT-set cells.)

**1. The fair-far story REVERSES.** fair's classic GT-set AC cells (the EXP-OP-06
PENDING column, now filled from the remap-verified legacy, r 0.981, n=584/706):
**ALL 0.676/413 · far 0.857/285 · mid 0.605/500 · near 0.356/618** vs A0 0.449 · far
0.525 · mid 0.327 · near 0.793. The 07-19 claim "wind breaks AutoCam far harder than us
(0.683 vs 0.050)" was the TIMEBASE BUG — with the recovered reference AutoCam is
STRONG on fair far. Far-Reolink is now: **AC-dominant on fair (0.857 vs 0.525), par on
spc (0.443 vs 0.441; F 0.669 ahead)** — W4's "beat AutoCam far" target is fair-far, and
the raw A0-vs-AC fair-far gap (−0.33) is not referee-decisive at current event counts
(the power ceiling; event-spreading queue applies).

**2. F's mid dip goes DECISIVE on fair (the (t)-floor catch): P2 PROMOTION BLOCKED.**
Referee ALL/mid F-vs-AC on fair GT views: d = −0.480, p_mag 0.0285, DECISIVE for AC —
the first family where F's known mid raw dip (EXP-OP-11: "never decisive") crosses the
line, and it is an F-INTRODUCED decisive AC loss (A0-vs-AC mid is not decisive). Per the
pre-registered action mapping: **F does not ship until the mid dip is understood — P4's
mid-path investigation is promoted ahead of P2.** F's far BEAT gains stand (spc +0.16,
fair +0.17, pit 0.929@n14).

**3. PIT mgctrl column BANKED with provenance confirmed by reproduction:** the era
campath scores ALL 0.644/294 = the CURRENT-STATE anchor ("arm ctrl 0.644") to the
digit; far 0.680 vs A0 0.481 (the ~+0.19 Dahua-data effect confirmed under the new
instrument). PIT referee: AC decisive over every arm on ALL and most far cells (10
rows) — the (t) supplemental floor unchanged. spc referee: ZERO decisive anywhere
(EXP-OP-06's no-decisive-on-spc holds under the composite standard).

**[AMENDMENT PRE-REGISTRATION 07-27, before the re-calibration run]:** the MATCH-column
null unit becomes **fixed 600-frame global-frame blocks (~30 s)** — dense contiguous
frames grouped by ``g // 600`` — chosen to sit an order of magnitude beyond the gap-64
(~3 s) correlation scale the event unit encodes, yielding ~125-170 blocks per game
(vs 1 collapsed event). The BEAT column keeps the gap-64 cluster unit (sparse frames,
valid as-is; its banked bands stand). Re-calibration = same seed 72 / 300 reps on the
same composites; the amended MATCH bands then gate every later match-side verdict.

**[AMENDMENT RESULTS 07-27 — MATCH nulls LIVE (code @ w2 ab0a275; artifacts
p0_fop/*_v2.json):** blocks 128 (spc) / 150 (fair) / 172 (PIT), reps_valid 300/300,
match-cap600 bands **spc [−0.089, +0.088] · fair [−0.076, +0.091] · PIT [−0.062,
+0.058]**. First match-side verdicts (ALL cell, delta vs A0): **spc F −0.063 and mgctrl
−0.028 WITHIN band; fair F −0.061 WITHIN band; PIT F −0.075 and mgctrl −0.104 OUTSIDE
band** — on the primary family the filter's dense-MATCH cost is null-level (its
promotion blocker remains the GT-set decisive fair-mid row, a different instrument);
on the Dahua family both arms genuinely diverge from AC's dense signal ((t) floor
texture). P0 is now COMPLETE: cells + BEAT nulls + MATCH nulls all banked.]

**4. INSTRUMENT GAP — the composite MATCH null is broken as operationalized:** the
gap-64 cluster-event unit collapses ~80k dense contiguous MATCH frames into ONE event on
all three games (reps_valid 0, power_floor fires). MATCH-side "within null band"
verdicts are UNEVALUABLE until the MATCH null gets a block-based unit (fixed-length time
blocks); BEAT nulls are valid and banked (63/13/10 events; e.g. spc beat-cap600 band
[−0.407, +0.367]). **Amendment queued: block-unit MATCH nulls, pre-registered before the
next match-side verdict.** (This also retro-applies to EXP-OP-14's spc nulls — same
collapsed unit, nobody had read it critically.)

**#21 gate state after this read:** spc leg PASS (EXP-OP-12) · PIT confirmation
re-anchored (0.644 exact) · fair leg BLOCKED — mg_ctrl_seed1 checkpoint AND its fair
dump are both only on the offline 4070; the 1060 re-dump path needs that checkpoint.
Gate paused, not failed.
**Artifacts:** G:/ballresearch/operator/p0_fop/ (p0_{spc,fair,pit}_fop.json + ladder
campaths + logs); staging F:/test/opstage; F-OP D:\opstage (disposable).

## EXP-OP-16 PRE-REGISTRATION (committed before numbers): P0 composite re-anchor — A0 / F@0.20 / mgctrl on spc + PIT + fair under the (w)/(x) standard (2026-07-27)

**Reads (per EXP-OP-14's queue + measured plan v2 P0):** per game x band x arm, the
composite cells — MATCH (tier-ac frames) / BEAT (GT-override frames) / overall, each
capture@600 + planned-view containment — plus the referee dual-rule per subset x band and
split-half null calibration banked in the same run. Verdict language follows (u)/(w):
match = within the cell's null band; beat = decisive under the dual rule.
**Arms:** A0 (champion hn4s4 chain), F (static filter @ frac 0.20/cell 50/radius 60,
pixel-space, the EXP-OP-11 promotion candidate), mgctrl where inputs exist (spc: relayed
dump; PIT: the banked EXP-72-era `campath_pit_mg_ctrl.pkl` — ARM PROVENANCE NOTED, not a
fresh replay; fair: PENDING — the mg_ctrl checkpoint and its fair dump are stranded on the
offline 4070; the 1060 re-dump path is also blocked on that checkpoint), AC (via the (x)
source policy: spc aim / fair remap-verified legacy / PIT validated jsonl).
**Instruments:** composites = spc (EXP-OP-14 banked, aim-derived) + fair (NEW, 92,966
rows, remapped-legacy AC + 706-view set) + PIT (NEW, 128,501 rows, validated jsonl +
650-view set). Campaths regenerated on F-OP from staged dumps through the chain @ w2
127b633; **PIT runs gated on --fixture-exp72** (A0 + AC must reproduce 0.542/450 and
0.759/207 to tolerance — the cross-machine env anchor; F-OP is a first-time analysis
node). fps: spc/fair 30 (matches p0_spc.json), PIT 20 (matches the EXP-72 lineage).
**Machine:** FORTNITE-OP CPU (FLEET 07-26 amendment; Roblox checked = idle zombie, 0.05
cores; GPU procs = desktop only). Chain = scheduled ONCE task (far-future /SD + /Run),
status file D:\opstage\p0.status.
**Action mapping (pre-stated):** F's promotion read (P2) consumes these cells directly —
non-regression on MATCH within null bands + BEAT gains; a decisive MATCH loss for F on
any family = promotion blocked pending the mid-dip investigation (P4). mgctrl cells feed
the #21 promotion package (P1), which additionally waits on the fair leg.

## EXP-OP-15: LEGACY VIEWPORT CLASS RECOVERED — trim remap verified on spc (vs aim r 0.97, med 27 px) and fair (r 0.98, offset confirmed to 1 fr); fair tier-3 restored WITHOUT a re-run; the per-seg r>0.7 gate amended with mechanism (2026-07-27)

Executes EXP-OP-13's breakthrough addendum (method + per-seg r>0.7 admission gate
pre-registered there). Measurements first, implementation after, admission last.

**1. The remap is a constant head-trim offset, and it is 60s x ACTUAL fps, not 20 fps
nominal.** spc per-seg lag scan of the legacy viewport (naive untrimmed mapping) against
the validated fresh aim: every one of 14 segments peaks at D 1157-1176 with NO drift trend
— one global offset. Pooled fit: **D* = 1164 = 60s x 19.4** (EXP-OP-13's "exactly -1200"
was scan-resolution); r@1164 = 0.971 vs r@1200 = ~0.95 — at a +-20 fr ball-gate window the
36 fr difference matters. **Pooled med |leg - aim| at D* = 27 px over 80,565 frames: the
legacy file IS the AutoCam signal**, run-to-run identity level. Per-seg r vs aim
0.887-0.997 (the pre-registered per-seg 0.7 gate PASSES against dense anchors).

**2. Fitting the offset on ball GT alone is follower-lag biased — so the applied offset is
PREDICTED, the fit only confirms.** spc ball-GT-only argmax lands at 1132 = aim-truth − 32
fr (AutoCam trails the ball ~1 s; the fit absorbs it). D_pred from match_info
start_time_offset x mean seg fps: spc 1169 (5 fr from aim-truth — harmless), fair 7130.

**3. The per-seg r>0.7 gate is UNPASSABLE against sparse ball GT — by the validated aim
itself.** spc per-seg legacy-vs-ballGT at the remap: 0.26-0.98; the failing segs are
QUALITY, not alignment: seg9 legacy 0.262 vs **validated aim 0.181 on the same GT**; seg8
0.683 vs aim 0.610. Per-seg r vs instantaneous ball GT bounds AutoCam's LOCAL tracking
quality — locally-bad AC is exactly what the composite's GT-override tier exists for; the
aim's own admission (EXP-OP-13) used pooled U-curve evidence, no per-seg floor.
**Amended gate (pre-registered before the fair admission run): pooled r >= 0.70 at the
fitted offset over >= 100 anchors, fit within 60 fr of the match_info prediction, per-seg
r table recorded as provenance with NO floor.** Alignment sharpness holds: spc pooled r
0.80 at D_pred vs 0.28-0.30 at wrong offsets, falling to 0.41 by +400 fr.

**4. fair: the production case, verified.** fair's aim is a CONFIRMED 0-usable-row stub
(the 07-11 "fresh run"), so fair had NO tier-3 at all. Legacy remap: **D* = 7130 = D_pred
EXACTLY (360s x 19.806), pooled r 0.981 on 204 anchors (naive 0.165); seg8 (windy far set,
n=175) r 0.941.** Row deficit 7,213 ≈ the 7,130 head trim + tail — the truncation
signature. **The fair AutoCam CLI re-run is UNNECESSARY; the hygiene item closes.**

**5. Implementation + E2E (code @ w2 3f05c85):** `load_viewport_trim_remapped`
(distill_dataset) + builder admission: the EXP-OP-05 ban converts condemned ->
**quarantined, admissible ONLY through the verified remap** (unverifiable sources — no
match_info, no fps, < 100 anchors, failed gates — hard-fail, rule 8); remap meta lands in
the composite `_meta` (provenance per the EXP-OP-05 correction). The raw Once.Autocam CLI
aim capture ({xy:[x,y],f} rows amid console lines, BOM) now parses directly. Live E2E:
**fair composite builds — 92,844 rows (ac 92,834; 198/208 ball anchors corroborate; 10
override; 7 novis removed)**; spc-from-aim reproduces the banked EXP-OP-14 composite to
the row (81,444 total / ball 607 / ac 80,837 = 80,585 + the 252 view rows folding back
without the label sets); spc legacy also ADMITS (r 0.809, drift 37) though the aim stays
spc's tier-3.

**Consequences:** (1) EXP-OP-05's retraction is RESOLVED for remap-verified files — the
class is genuine AutoCam tracking behind a timebase bug (load_viewport mapped
trimmed-timeline recordings via untrimmed seg offsets). (2) Tier-3 AC references are now
restorable on ANY Reolink game with >= 100 ball-GT anchors, no AutoCam re-runs. Games
without anchors stay quarantined — OPEN: an anchor-free verification path (e.g. alignment
vs our own dumps) if coverage ever demands it. (3) The scoreboard's auto-AC arm still
loads legacy jsonls naively on Reolink games (EXP-OP-14's report note): wire the remap
loader or suppress that arm before the next scoreboard run. (4) P0 composites for fair
(and PIT/spc rebuilds with view sets) proceed server-side on the banked instrument.
**Artifacts:** measurements in this entry (scratch checks not banked; server rebuild with
view sets = the P0 step); code @ w2 3f05c85.

## EXP-OP-14: FIRST COMPOSITE-STANDARD READ (spc) — the (w) instrument changes the verdicts: F trades MATCH for BEAT; mgctrl is the balanced arm (2026-07-26)

Composite: 81,444 rows (ball 607 / view 252 / ac 80,585; corroborated 1,187). Cells
(cap600 / containment):
| arm | MATCH (75k dense) | BEAT (859 GT-override) |
|---|---|---|
| A0 | 0.535 / 0.671 | 0.519 / 0.587 |
| F@0.20 | 0.471 / 0.582 (−0.06 cap) | 0.560 / 0.616 (+0.04) |
| mgctrl | 0.506 / 0.651 (−0.03) | **0.583 / 0.653 (+0.06)** |
Under GT-only scoring F looked strictly dominant; under Mark's standard its cost is
visible: it diverges from AC's (usually-good) viewport on dense frames to win the
override moments. mgctrl buys MORE beat for HALF the match cost. Next reads (queued):
per-band composite split (is F's MATCH loss the mid path-dip?), the mgctrl x F
composition, nulls (in p0_spc.json), PIT/fair composites. NOTE: the scoreboard's
auto-'AC' arm here is the LEGACY jsonl (0.278 match) — label it banned in the report
or suppress; the reference's AC tier is the validated aim source.
**Artifacts:** spc_composite.jsonl, p0_spc.json; instrument @ w2 31df9fa.

## EXP-OP-13: AC SOURCE VERIFICATION vs ball GT (Mark's directive) — aim file validated WITH a measured ~1s follower lag; legacy jsonls condemned by direct evidence; override gate amended (2026-07-26)

Lag scan of med|ac_x - ball_gt_x| (spc: 1,351 ball anchors):
- **SPC fresh aim: clean U-curve with minimum at lag +20 frames (~1s): 913/721/666/587/540/606/730 px at lags -100..-40..+100.** Reading: the file is frame-aligned (smooth
  structure, no gross offset) and AutoCam's viewport TRAILS the ball by ~1s — consistent
  with the RE'd ~3s recency-weighted follower. Median 540-587px vs instantaneous ball is
  sane for a smoothed viewport. VALIDATED as tier-3 source.
- **SPC + FAIR legacy viewport jsonls: flat 1530-1750px at ALL lags — no alignment
  minimum exists.** Decorrelated at every shift; the EXP-OP-05 ban now rests on direct
  ball-GT evidence on two games, not just the README note.
- PIT jsonl: no ball GT; stands on the 50/60 cold audit + 0.759 vs human GT.
**Consequence for (w) — override-gate amendment (pre-registered):** AC trailing the ball
by ~1s means ball(f) vs ac(f) > 600px fires on pure follower lag during fast play, not
just true losses. **Tier-1 (ball GT) divergence = min distance over the AC window
ac[f-20..f+20] > 600px** — a trailing follower stays within its window; a park on the
wrong object does not. Tier-2 (viewport GT) gate unchanged (both are viewport-speed
signals). Composite builder must implement the WINDOWED tier-1 gate.

**[ADDENDUM 07-26: Mark's rescale hypothesis tested and REFUTED]** — correlation is
scale-invariant, so a downscaled coordinate space would still correlate: legacy-vs-ball
Pearson x = 0.28 (spc) / 0.17 (fair), y NEGATIVE, Spearman (any monotonic remap incl.
warp space) 0.27 / 0.08, best affine leaves 1,021px median residual, no lag helps, and
the raw range already spans the full 7,680 frame. Not rescalable — ban stands.

**[BREAKTHROUGH ADDENDUM 07-26, Mark's push: 'does it MOVE like the ball?']** — per-
SEGMENT wide lag scan (±3000 fr) finds the legacy spc viewport correlates r=0.82/0.81/0.58
with ball GT at large PER-SEGMENT lags — and seg-1's lag is EXACTLY −1200 fr = the 01:00
match_info start_time_offset @20fps. **The legacy jsonls were recorded on the TRIMMED
timeline; load_viewport maps them via untrimmed seg offsets → global decorrelation of
GENUINE AutoCam tracking.** Not garbage, not scale — a timebase bug in the mapping.
CONSEQUENCE: the class is likely RECOVERABLE (trim-offset remap + per-segment lag
verification against ball GT/aim), restoring tier-3 AC references for ALL Reolink games
without AutoCam re-runs. NEXT SESSION: implement trim-aware remap in load_viewport or the
composite builder; verify per game (require per-seg r>0.7 vs anchors before admission);
re-check the fair 'stub' priority (re-run may be unnecessary); the EXP-OP-05 retraction
still stands until remapped files are validated. Ban CONVERTS from 'condemned' to
'quarantined pending remap+verification'.

**[RESOLVED 07-27 — EXP-OP-15]:** remap verified on spc (vs aim r 0.971, med 27 px; the
true offset is 60s x ACTUAL fps = 1164, not 1200) and fair (r 0.981, offset confirmed to
1 fr; fair's aim confirmed a 0-row stub). Admission implemented behind the amended pooled
gate ((x)); fair tier-3 restored without a re-run.

## EXP-OP-12: #21 GATE, SPC LEG — mg_ctrl PASSES non-regression; far +0.28 raw on the primary family; the one near loss is a single 4.4s event (2026-07-26)

mg_ctrl(seed-1)@s4 dump -> v7 -> shipped chain, vs hn4 champion on spc_viewport_worst:
ALL 0.687 vs 0.598 · far 0.724 vs 0.441 (best far cell measured on any arm, beats
hn4+F@0.20's 0.707) · mid 0.685 vs 0.719 · near 0.452 vs 0.762 (med 3612).
Dual rule mgctrl-vs-hn4: ZERO on every band (near ev0v1 — the whole regression is ONE
contiguous 13-view event, g 5416-5504, 4.4s: the near-autopsy tracker-dynamics class,
exactly W3's target). far ev4v1 d=+0.24 (not decisive, power). **Gate leg: PASS (no
decisive regression), with the near event logged.** Pittsford confirmation already banked
(EXP-72: 0.630 vs 0.452, arm-independent data effect). Remaining for the gate: fair leg
(mg_ctrl fair dump finishing on the 4070) + the (t) framing note (coverage serves
cross-camera robustness). Open composition read (not run): mg_ctrl + F@0.20 —
the candidate PRODUCT stack for far.
**Artifacts:** t21_spc.json; dumps/spc_mgctrl_s4 (relayed); ladder spc_mgctrl_s4.*.

## EXP-OP-11: FILTER CROSS-CHECKS + two refinement arms REJECTED — F@0.20 pixel-space is the promotion candidate (2026-07-26 morning)

**F (frac 0.20) cross-game (capture@600, A0 -> F):**
- SPC far 0.441 -> 0.669 · PIT-orig far 0.303 -> 0.632 · FAIR far 0.525 -> 0.562
- near: +0.24 / (n=1) / +0.16 · ALL: +0.07 / +0.11 / -0.01
- mid (the trade): -0.088 / -0.167 / -0.075 — raw only, dual rule ZERO everywhere tested.
- **PIT far vs AC: DECISIVE LOSS -> ZERO** (d halves -0.484 -> -0.257) — the (t)
  non-collapse floor passes with room to spare.
**F2 (world-gated, off-field-only): REJECTED.** SPC far collapses to 0.428, PIT to 0.372 —
the 'in-field' statics near the far boundary are classified by exactly the world geometry
the ONE BUG CLASS proved untrustworthy there (ruler error near/beyond boundaries). World-
gating the filter re-imports the broken ruler; pixel-space-only survives until #19
certifies. (The keeper-deletion theory of the mid dip also DIES here: F2 kept in-field
cells and mid dropped anyway — the dip is indirect global-path re-solve, not deletions;
only 10/34 lost spc mid views sit near a center.)
**F30 (frac 0.30): REJECTED for robustness.** Better on spc (far 0.707!) and fair (0.608)
but COLLAPSES on PIT (far 0.267 < A0; decisive AC loss returns) — PIT's statics sit below
30% presence; frac is not robust above 0.20. Adaptive/percentile thresholds = future work
(session-scoped only).
**VERDICT: F @ frac 0.20, cell 50, radius 60, pixel-space, drop-all = the promotion
candidate.** Wins far on all three games, no decisive regression anywhere, flips the WIN
column 0.195 -> 0.558 on spc. mid raw dip (threshold-independent, never decisive) and the
winfar2 goal-kick residual are W3's opening problems. Next: filter into the product
ball_select path behind config + standard promotion protocol; then W3 arms on the F stream.
**Artifacts:** exp11_{pit,fair,spc_f2,pit_f2,fair_f2,spc_f30,pit_f30,fair_f30}.json;
ladder *.{F,F2,F30}.*; code @ w2 4e35e65.

## EXP-OP-10 RESULTS: the persistence filter PROVES OUT on far GT — WIN-far 0.195 -> 0.558, winfar1 fixed on the exact frames Mark watched; HOLD alone hurts on a dirty stream (2026-07-26)

**Arms on SPC (649 views), capture@600 / med|dcx|:**
| band | A0 | **F (filter)** | H (hold) | FH | AC-fresh |
|---|---|---|---|---|---|
| far (290) | 0.441 / 1625 | **0.669 / 202** | 0.362 / 2756 | 0.483 / 1007 | 0.443 / 752 |
| mid (317) | 0.719 / 82 | 0.631 / 260 | 0.754 / 232 | 0.710 / 201 | 0.804 / 354 |
| near (42) | 0.762 / 25 | 1.000 / 52 | 1.000 / 245 | 1.000 / 309 | 1.000 / 47 |
| ALL (649) | 0.598 / 232 | **0.672 / 204** | 0.595 / 304 | 0.627 / 266 | 0.640 / 453 |

**Pre-registered reads:**
1. HEADLINE WIN-far: **0.195 -> 0.558 (106/190 AC-missed far views rescued, was 37)** —
   +0.363 raw. Event-level dual rule: zero (ev3v2 — the div-sampled set concentrates far
   mass in few events; the KNOWN power ceiling, event-spreading queue is the fix). Raw
   delta reported per prereg.
2. GUARD MATCH-far: 0.940 -> 0.921 (-3 of 151 views; null band for this cell not yet
   banked — flagged, not cleared).
3. GUARDS: no decisive regression anywhere (mid raw -0.088, dual rule zero ev3v6
   p_mag=0.13; near +0.238; ALL +0.074).
4. CASES: **winfar1 FIXED** — F breaks the commitment in ~4s vs A0's 13s park; captures
   9/12 labeled samples on the exact clip. **winfar2 NOT fixed** (1/10): its right-side
   attractors are not static-persistent (goal-area clutter during the goal kick);
   residual = selection commitment (W3 margin arms + competing-cluster switch) + the
   launch-armed bridge. The filter is necessary, not sufficient.
5. Attribution: F carries everything. **H alone HURTS far (0.362)** — HOLD on a
   distractor-committed stream holds WRONG positions (EXP-OP-04's mechanism, now measured
   at arm level); FH partial (0.483). HOLD is NOT refuted — it ran untuned, on default
   knobs, without the OOB exit anchor, and its votes were computed from the UNFILTERED
   stream's states. Re-tune ON TOP of F after the OOB amendment lands.
6. F exceeded Run B's far ceiling (0.669 vs 0.620) — B was sparse-GT-limited (208-anchor
   effect on spc too: B injected only labeled frames); B-A UNDERSTATED input headroom.
**Also:** F beats AC-fresh raw on far (+0.226) and ALL (+0.032); zero at event level
(power). **Mechanism note:** the shipped world-cell static_persistence penalty
(static_w=2.0, 'the dominant lever') did NOT stop these statics — hypothesis: they live
OFF-FIELD where homography extrapolation scatters world cells (pixel-space filter is
immune); verify with a world-cell stability probe before promoting the filter into the
product step.
**Artifacts:** exp10_spc.json; ladder spc_hn4s4.{F,H,FH}.*; filter @ 0c16456 (26 static
centers @ frac 0.20). **Next:** filter into the product ball_select path behind config +
PIT/fair cross-checks + mid-dip investigation; then W3 margin arms on the F stream.

## EXP-OP-10: PRE-REGISTRATION — prove the levers on far GT: persistence filter x HOLD, four arms on SPC (2026-07-26, before any numbers)

**Directive (Mark):** "prove it — run experiments around where we have far GT and show we
can actually keep it in the viewport."

**Arms (all CPU replay, spc hn4s4 dump, v7 selector, shipped tracker config):**
A0 = banked champion baseline. F = persistence-filtered candidate stream (session-scoped:
cell 50px, static = cell present in >=20% of sampled frames game-wide, drop radius 60px —
the offline equivalent of an online rolling window) -> same chain. H = A0 + enable_hold
(pre-registered default knobs, W2 design SS4). FH = F + H.

**Instrument:** spc_viewport_worst (649 views; far n=290: WIN-col 190 / MATCH-col 151) +
case traces on the winfar1/2 windows (g 6624-7224, 8496-8980). PIT re-score for
cross-camera non-regression if any arm promotes.

**Pre-registered reads (dual rule, referee port, gap-64 events):**
1. HEADLINE: WIN-col far capture@600 (A0 = 0.195). Success = decisive improvement vs A0
   on flip events; report raw delta regardless.
2. GUARD: MATCH-col far (A0 = 0.940) must not regress beyond the split-half null band.
3. GUARDS: ALL/mid/near capture non-regression (dual rule zero-or-better).
4. CASE: winfar1/2 traces — does trk_x stay with GT (left) through the previously-parked
   seconds? Reported as the same frame tables as EXP-OP-08/09.
5. Attribution: F vs A0 and FH vs H isolate the filter; H vs A0 and FH vs F isolate HOLD.
**Expected mechanism-level outcomes (stated before running):** F should flip the winfar
commitment (the 6059/6302 snare dies); H should cut the frantic-phase velocity and hold
stoppages; FH is the candidate product config. If F alone flips winfar but WIN-far moves
<0.05 overall, the remaining mass is coast/chase (W3's arms), not statics — that is a
finding, not a failure.

## EXP-OP-09: WINFAR2 AUTOPSY + the game-state/physics lever map (Mark's directive) — the SAME static distractors dominate both clips (2026-07-26)

**Trace (g 8496-8980):** frantic phase sec 0-6 = GT STATIC ~5850 (goal-kick setup) while
the track ping-pongs 5571-7171 among right-side distractors (campath speed med 14 px/fr,
p90 71 - vs human GT median 0); goal kick sec 7-19 = GT sweeps 4662-2377 right-to-left
with a 69-424px candidate present at every labeled frame; track never leaves the
5300-7200 distractor zone. **x=6059 and x=6302 recur at fixed positions across BOTH
winfar clips (thousands of frames apart): game-long static objects scoring 0.8-0.9.**

**Lever map (Mark: "use game states or physics to guess better"), priority per evidence:**
1. PERSISTENCE/STATIC FILTER (#19 component; session-scoped): a ball is never stationary
   for minutes - kills the 6059/6302 snare; PREREQUISITE (in winfar1 it blocked the OOB
   machinery from engaging). Kills 2+3 on record now.
2. OUT-OF-PLAY STATE (rules; winfar1 = out over far touchline, thrown in): polygon exit
   -> DEAD state -> W2 HOLD anchored at exit -> reacquisition prior at RESTART GEOMETRY
   (throw-in near exit; corner/goal-area for goal-line exits). Extends the existing
   reacq_dist_w loss-point bias to the rules-implied restart point.
3. LAUNCH-ARMED BALLISTIC BRIDGE (physics; winfar2 = goal kick): ball-static-in-goal-area
   + player repositioning arms the EXP-DIST-31 aerial bridge + flight-cone reacq bias in
   world meters (<=35 m/s). W4/world-unit consumer, after #19.
4. DEAD-BALL CAMERA DISCIPLINE: W2 dispersion+slow voters during restart setups; W4
   velocity profile as the general jitter clamp (frantic phase = planner faithfully
   following track churn).
This is the SS3d play-state fusion with priorities MEASURED (two clip autopsies), not
guessed. W3's sweep order amended: persistence filter FIRST, then miss-cost arms, then
competing-cluster switch (EXP-OP-08) only if commitment survives filtering.

## EXP-OP-08: WINFAR1 CLIP AUTOPSY (Mark's eyeball → mechanism) — selection COMMITMENT on a static distractor; the ball was a candidate the whole time (2026-07-26)

**Trigger:** Mark watched `spc_winfar1_stack.mp4` (g 6624-7224): GT pinned far-left corner
until ~0:24; BOTH viewports swing right. Frame trace (banked dump + trajectory + campaths):
- sec 5-21: GT fx 2250-3300 (far left). **A candidate ≤20-130px from GT exists at every
  sampled frame, scores to 0.91.** Perception clean; the WIN column is interpreter loss.
- sec 3-8: top-1 candidates are right-side distractors (x≈5321/6059/6303 @ 0.83-0.91);
  the Viterbi commits, track marches 2400→5600, then **parks 13 s** — the ~3000px switch
  back is transition-cost-prohibitive, so the wrong path survives despite a persistent
  in-place true-ball candidate. Planner follows its input honestly. **AutoCam parks at the
  SAME distractor on the same frames** (5300-5570, sec 9-19); both recover ~sec 22-24.
- x≈6059/6302 recur at FIXED positions all clip: a STATIC distractor (adjacent-field
  ball class) — second measured kill for the #19 persistence/static filter (first: IRON's
  (1637,470) furniture, (l)).
**Mechanism named: selection commitment — per-frame emissions carry no credit for a
spatially-CONSISTENT competing cluster, so 17 s of "same spot, decent score" evidence
never accumulates against one wrong high-score commitment.**
**W3 design amendment (pre-registered lever, joins the 3 arms):** competing-cluster
track-switch — maintain cheap statistics of persistent off-path candidate clusters
(position-stable, score≥thr, k-of-n frames); when one out-persists the current path,
propose a switch (Viterbi restart or explicit switch state) instead of requiring a
single-frame out-score. Priced vs GT−B like the rest of W3; scored on the WIN column.
Also: the persistence filter's expected effect on THIS clip (killing 6059/6302) may
alone flip the commitment — test filter-first, switch-second (cheapest-first order).

## EXP-OP-07: AC-CONDITIONAL CELLS + AGREEMENT ROW — the goal decomposed per (u); the far prize is one column (2026-07-25, addendum to the table)

Champion capture@600 on GT views split by whether AutoCam captured the same view
(valid AC references only: SPC fresh aim, PIT validated jsonl):

| family | band | MATCH col (AC captured) | WIN col (AC missed) |
|---|---|---|---|
| SPC | far | **0.940** (142/151) | **0.195** (37/190) |
| SPC | mid | 0.749 (191/255) | 0.597 (37/62) |
| SPC | near | 0.762 (32/42) | — (0 AC misses) |
| PIT | far | 0.480 (184/383) | 0.500 (7/14) |
| PIT | mid | 0.466 (48/103) | 0.770 (107/139) |

**AGREEMENT row (all frames, vs AC):** SPC 0.542 within-600 / med 511px · PIT 0.798 / 150px.

**Reads:** (1) On the primary family we already MATCH AC where it works far (0.940) — the
"mostly the same" half of (u) is nearly closed far, with mid/near match cols (0.75-0.76)
the polish targets. (2) **The beat-far goal collapses to ONE cell: SPC far WIN col = 190
views where AC lost the ball, we currently rescue 37.** These are the hard views (both
systems fail); our detector's measured far advantage should cash exactly here and the
interpreter is discarding it — the W2 (hold instead of sweep) + W3 (don't coast, don't
chase) target population, priced by GT−B. (3) PIT mid WIN col 0.770 shows the pattern
already works cross-camera where AC's mid weakness gives us misses to rescue. (4) SPC
all-frame agreement (0.542) is much lower than PIT's (0.798) — on Reolink we diverge from
AC constantly; fine per (u) as long as GT sides with us on the divergences — the flip reads
(ev5v3 our way, not decisive) say it's currently a coin toss. Pre-registration consequence:
W2/W3 promotion reads ADD the WIN/MATCH columns; a lever that lifts WIN-far while holding
MATCH-far ≥0.94 is the shaped target.

## EXP-OP-06: THE W1 TABLE — goal-as-numbers per band × family, headroom split, lookahead priced, ceiling check (2026-07-25, BANKED)

**The scoreboard the arc runs on. Champion = hn4@s4 dumps → v7 → shipped tracker → planner
(provenance verified per cell). Reolink = primary family (DECISIONS (t)); PIT-Dahua =
supplemental robustness row. All GT = human viewport views; capture@600 / med|Δcx| px.**

### Goal cells (champion vs AutoCam)
| family (n far/mid/near) | band | champion | AutoCam | verdict (dual rule) |
|---|---|---|---|---|
| **SPC-Reolink** (290/317/42) | far | 0.441 / 1625 | 0.443 / 752 | **zero** (parity at power) |
| | mid | 0.719 / 82 | 0.804 / 354 | **zero** |
| | near | 0.762 / 25 | 1.000 / 47 | **zero** (2 events) |
| | ALL | 0.598 / 232 | 0.640 / 453 | **zero** (ev4v4) |
| **FAIR-Reolink** (217/397/92) | far | 0.525 / 360 | — | **AC reference PENDING** |
| | mid | 0.327 / 960 | — | (fresh aim run failed 07-11: |
| | near | 0.793 / 281 | — | version-error stub; re-run queued) |
| **PIT-Dahua** (397/242/1) — supplemental | far | 0.481 / 698 | 0.965 / 129 | **AC decisive** (floor row per (t)) |
| | mid | 0.640 / 345 | 0.426 / 1191 | zero (power) |
| | ALL | 0.542 / 450 | 0.759 / 207 | AC decisive |

**Goal status in one line: MATCH on the primary family = already true within instrument
power (no decisive cell either way on SPC); BEAT far = open everywhere; the Dahua
supplemental row is decisively behind (non-collapse floor applies, not "beat").**

### Oracle ladder / headroom split (spc = the readable family)
- **A (baseline)**: table above. **B (GT-ball injection, 1,351 anchors)**: spc far 0.620 →
  **B−A = +0.18 far = input-side headroom** (calibration/coverage through the CURRENT
  interpreter). fair-B UNREADABLE (208 anchors too sparse to drive the chain — B needs
  dense GT); PIT-B impossible (0 ball GT).
- **GT−B (interpretation-only) ≈ 0.38 far on spc** (GT viewport ceiling ≈1.0 vs B 0.620) —
  **interpretation headroom is ~2× input headroom in the far band.** W2/W3's half of the
  ledger is bigger than the detector-side half, on the family that matters.
- **C (stoppage-freeze, PIT only): far +0.053 lower bound** (span-entry anchor; W2's
  last-good anchor should beat it).
- **D (lookahead pricing): DEAD on all three families.** Leads ≈0 or negative (PIT far
  degrades 0.481→0.443@2-3s; spc flat; fair 0.525→0.493@0.5s); zero-phase ≤ +0.02.
  **W4's lookahead lever is UNFUNDED, three-family verdict.**

### Pixel-ceiling check ((t).4, pre-registered): NOT confirmed as a hard ceiling
Far candidate-present ≤100px of GT focal (each family's own failure-weighted views):
PIT-Dahua **0.647**, SPC-Reolink 0.471, FAIR-Reolink 0.664. The far ball IS a candidate on
~65% of Dahua failure views — the far-Dahua gap is mostly NOT missing pixels; it remains
selection/coast/planner discipline. (t)'s re-anchor stands on its strategic grounds
(Reolink resolution + product default), not on a proven sensor ceiling. Caveats: 100px is
generous; candidate ≠ selectable; sets are family-specific failure samples, not matched.

### Nulls & instruments (banked artifacts)
`G:/ballresearch/operator/{pit_referee,spc_v1,spc_v2,fair_v1,pit_ladder,pit_ladder2,pit_coldaudit}.json`
+ `ladder/` campaths+trajectories. Framing metrics power-limited everywhere (7 velocity
events PIT; hold clusters n=2); capture/|Δcx| are the powered cells. fair AC pending;
mid/near event counts thin — event-spreading queue is the instrument unblocker.

### What this funds next (per §3b pricing)
1. **W2 stoppage-HOLD** (built, e8f6f4d): tune against the GT−B interpretation ledger;
   headline = hold cells + far/ALL non-regression. 2. **W3 miss-cost + coast** vs B−A far
   (+0.18) and GT−B. 3. **W4 far policy WITHOUT lookahead** (deadband/FOV floor/velocity
   profile in field units, after #19). 4. **#21**: gate evidence in flight (mg_ctrl dumps);
   promotion now ALSO gated on the (t) framing. 5. fair AutoCam re-run (server console
   session) to fill the PENDING cells.

## EXP-OP-05: AUTOCAM REFERENCE CORRECTED on the primary family — the legacy viewport jsonl is the documented-BAD file; "we crush AutoCam" (07-19) is RETRACTED for spc/fair (2026-07-25)

**Trigger:** Mark rejected the spc read "AC capture@600 = 0.063 mid" as impossible. He was
right. Investigation (per-frame processing, not assumptions):
- spc's `autocam_viewport.jsonl` pans actively (x 479–7145, 0.6% center) but decorrelates
  from GT — and `autocam_aim.README.txt` (2026-07-11, prior session) SAYS SO OUTRIGHT:
  "the legacy autocam_viewport.jsonl in this dir is BAD (corr 0.28 vs GT); this fresh aim
  corr 0.773." The correct reference is **`autocam_aim.jsonl`** — fresh Once.Autocam 3.0.7
  CLI run, smoothed ball target in SOURCE px (7680×2160), f = combined.mp4 frame idx
  (≈ global frames; alignment verified by the sane re-score below).
**Corrected spc_viewport_worst cells (capture@600 / med|Δcx|, ALL subset):**
| band | champion (hn4s4) | AC (fresh aim) | AC (legacy, BAD) |
|---|---|---|---|
| all | 0.598 / 232 | **0.640 / 453** | 0.086 / 3179 |
| far | 0.441 / 1625 | 0.443 / 752 | 0.117 / 3036 |
| mid | 0.719 / **82** | 0.804 / 354 | 0.063 / 3192 |
| near | 0.762 / **25** | 1.000 / 47 | 0.000 / 4013 |
Referee vs fresh AC: **nothing decisive either way on spc** (ALL ev4v4; far zero; the
same-session "mid DECISIVE for champion" read against the legacy file is VOID — never banked,
retracted here). Pattern: champion capture trails slightly but its MEDIANS are far tighter
when on-target (82 vs 354 mid; 25 vs 47 near) — the tail/discipline story again, now on the
primary family, with far dead-even.
**Corrections that propagate (standing):**
1. **"We crush AutoCam" (07-19, frozen viewport v1) is RETRACTED for spc/fair** — the queue
   builder drew AC positions from the legacy file at labeling time; every spc/fair AC
   comparison to date used the BAD reference. PIT (Dahua) STANDS — its viewport jsonl was
   cold-audit-validated (50/60).
2. **AC-arm provenance is now mandatory**: the scoreboard's AC arm must name its source file;
   Reolink-family legacy `autocam_viewport.jsonl` is a banned reference class. A hard-fail
   guard (refuse legacy viewport when a fresh aim file exists) goes into the scoreboard with
   the next code commit (rule 8).
3. **Run AC references against the TRIMMED (product-regime) video going forward** (Mark,
   07-25), mapping to global frames via match_info start_time_offset — the 07-11 aim run used
   combined.mp4 which aligns, but trimmed is the product regime and the standing convention.
4. viewport_benchmark.jsonl's autocam tier on spc/fair (built from the legacy file) is
   suspect — audit before any read that consumes it.
**Artifacts:** `G:/ballresearch/operator/spc_v2.json` (corrected), `spc_v1.json` (bad-AC,
kept for the delta), `ladder/spc_ac_aim.campath.json`.

**[ADDENDUM 07-27 — EXP-OP-15]:** the legacy class is RECOVERED, not garbage: it is
genuine AutoCam tracking recorded on the TRIMMED timeline and mis-mapped via untrimmed
seg offsets (EXP-OP-13 breakthrough). The ban converts to quarantine-with-verified-remap:
correction 1's retraction stands for every read that USED the naive mapping, but
remap-verified files are admissible tier-3 references ((x)). fair's tier-3 comes from its
remapped legacy (r 0.981 vs ball GT); the fresh-aim preference in correction 2 stands
where an aim exists (spc).

## EXP-OP-04: ORACLE LADDER on PIT — Run A validated to the digit; lookahead prices at ~ZERO; the dump-provenance trap; hn2s8 anomaly adjudicated as selection bias (2026-07-25)

**Run A consistency: PASS, exact.** Replaying the VERDICT dump (`geodet/fullgame_pittsford`,
hn4@stride-4) through `operator_ladder run-a` (defaults, selector_v7) reproduces the banked
champion campath on every band to the digit (ALL 0.542/450; far 0.481/698; mid 0.640/345).
The ladder infrastructure is validated end-to-end against the EXP-72 pipeline.

**Run D (lookahead pricing, per the EXP-OP-01 funding gate): D−A ≈ 0 — the W4 lookahead
lever is NOT funded on any band by these oracles.** Perfect-lead Δ∈{0.5,1,2,3}s on A-inputs:
ALL 0.552/0.542/0.531/0.547 vs base 0.542 (noise); **far DEGRADES with lead** (0.484/0.474/
0.443/0.443 vs 0.481) — leading amplifies the chase; zero-phase +0.022 ALL / flat far.
The far failure is operator discipline (chasing), not response latency. (EXP-DIST-40's
takeaway survives its own instrument upgrade.)

**Run C (crude stoppage-freeze oracle): far +0.053 (0.534 vs 0.481, med 698→448), ALL −0.100,
mid −0.351.** The oracle freezes each hold cluster at SPAN-ENTRY position — and on
champion-failure segments the entry is by construction off-target. C−A is therefore a LOWER
bound on stoppage-HOLD; W2's FSM must hold at last-GOOD position, not entry. The far
median improving 698→448 from freezing 594 frames is the (h) mechanism visible in the ladder.

**THE DUMP-PROVENANCE TRAP (standing rule for every ladder/scoreboard read):** a second
Pittsford dump (`fullgame_dahua/dahua_pittsford0625`) is **hn2@stride-8** — an OLD detector at
half density. The same chain on that dump scores **0.812 ALL / 0.846 far** vs the champion's
0.542/0.481 on PIT-GT. Adjudicated on the NEUTRAL instrument (pittsford_cold_audit, 60 random
active frames): champion far 0.904 vs hn2s8 0.962 (n=52, unpowered) — **the 0.27 gap on
PIT-GT is SELECTION BIAS: original-500 is champion-failure-weighted (pre-registered property),
so ANY decorrelated arm reads inflated there.** Consequences, all standing:
1. Every arm in every read names its dump (ckpt + stride) — "champion" means hn4@s4 through
   the current chain, nothing else. The scoreboard JSON should carry dump provenance.
2. PIT-GT judges only arms whose failures were sampled into it (champion, AC, mg arms);
   cross-arm reads for OTHER arms need neutral instruments (cold-audit, benchmark tier).
3. Champion far capture on RANDOM active frames is **0.904** — the everyday product is far
   better than the failure-weighted 0.481; (h)'s "failure-mode instrument" caveat quantified.
4. **spc/fair heldout dumps are ALSO hn2@stride-8** — this morning's spc/fair run-a arms are
   NOT champion arms; v1-family cells wait for hn4@s4 dumps of those games (1060 job).
5. OPEN mechanism probe (cheap, later): hn2s8's small neutral-set edge (far 0.962 vs 0.904,
   n=52) hints "sparser/weaker candidate stream → calmer operator"; unpowered — if it
   replicates on a bigger neutral read it becomes a W3 lever (stream damping), not detector work.

**Artifacts:** `G:/ballresearch/operator/{pit_ladder.json, pit_ladder2.json,
pit_coldaudit.json}`, ladder dir with all trajectories/campaths.

## EXP-OP-03: FIRST FORMAL PER-BAND READS (referee v3 port) + amended hold unit results (2026-07-25)

**Referee port validated twice:** dual_rule_read is a line-exact port of compose_verdict @
ac1f42c (diffed against the pin before porting), and its PIT ALL read reproduces EXP-72's
pair read exactly (ev5v18, p_sign=0.0106 vs the banked "ev5v18, sign p≈0.011").

**Formal champion-vs-AutoCam reads on PIT-GT (capture@600, dual rule, per band):**
- **ALL/all: DECISIVE → AutoCam** (ev5v18, p_sign=0.011).
- **ALL/far: DECISIVE → AutoCam** (ev4v13, p_sign=0.049; d=−0.484, p_mag=0.057). The far gap
  is now formal — W4's target cell, confirmed at the ALL tier.
- **ALL/mid: ZERO** (ev2v6, p_sign=0.29, d=+0.215, p_mag=0.75). The raw mid rates (0.640 vs
  0.426) do NOT survive event-level power — "champion wins mid" is a positive delta at
  insufficient power, not a win. Original-500/far also zero at its event count (ev1v4) —
  the far mass sits in FEW events on the original sample; ext-div/all is DECISIVE → AC
  (ev4v15, p=0.019/0.020).
- Near: unmeasurable everywhere (≤1 event).
**Consequence:** at PIT's event structure, only ALL-tier and far reads are powered; mid/near
need more events (event-spreading queues) before any "match" claim can even be tested.

**Amended hold unit (gap-40, n≥4, GT<200, no arm conditioning): 2 qualifying clusters**
(not (h)'s 4 — (h) additionally clustered over ALL manifest∩label rows, not just
action=='view', which bridges clusters differently; the amended unit is now the
pre-registered standard). Champion ratios 24.2 / 7.6 (median 15.9); split-half null band
±16.6 at n=2 → **no calibrated hold claim yet — power-limited**, W2 needs either the
event-spreading labels or its own eval design on GT v1.
**Descriptive (not calibrated):** GT pan-velocity median is **0.0 px/s** (p90 374) across
labeled segments — the human operator's default is a dead-still camera; champion median 137,
AC 7.5. Velocity nulls (7 events): band [−352, +454] still swamps the arm gap.

**Artifacts:** `G:/ballresearch/operator/pit_referee.json`. Next: oracle ladder runs
(run-a spc/fair/pit — pit doubles as a consistency check vs the banked EXP-72 campath).

## EXP-OP-02: W1 scoreboard INSTRUMENT LANDS — fixture reproduces EXP-72 to the digit; first per-band cells; null calibration rejects 3 of 4 framing metrics as operationalized (2026-07-25, overnight)

**Fixture (hard gate): PASS.** The committed scorer (`training/cli/operator_scoreboard.py` @
ed17d6e, run on the server checkout `G:/ballresearch/operator/repo`) reproduces EXP-DIST-72
exactly: champion (campath_pittsford.pkl, 07-23) capture@600 **0.542 / median 450 px**; AutoCam
**0.759 / 207 px**; **n=640** — which also resolves the count drift: the banked set is 500
original + 140 ext-div = 640 labeled views ("650" in CURRENT STATE was the rounding).

**First per-band cells (PIT-GT, Dahua adversarial; capture@600 / med|Δcx|; descriptive —
per-band dual-rule reads await referee integration):**
- **far (n=397): champion 0.481 / 698 vs AC 0.965 / 130** — original-500 far: **0.303 / 1456
  vs 0.968 / 133. AutoCam's entire edge on this set is far framing.**
- **mid (n=242): champion 0.640 / 345 vs AC 0.426 / 1191 — the champion WINS mid** on the
  failure-mode set (AC's mid capture is its weak band here).
- near: n=1 (div-sampling produced essentially no near views — near GT must come from
  elsewhere before any near claim).
- ext-div subset (arm-failure-weighted): champion 0.864 vs AC 0.943 — both strong, AC ahead.
- Pair flips ALL: champion-only 5 ev / 115 fr vs AC-only 18 ev / 254 fr.
- Single qualifying stoppage segment: champion swing 24.2× GT vs AC 0.33× — the (h) failure
  in one number, but n=1 (see below).

**Null calibration (300 reps, seed 72, champion arm, 142 gap-64 events) — the instrument-
admission gate REJECTED 3 of 4 framing metrics as currently operationalized:**
- pan_velocity_median: null band ±456 px/s; p90: [−548, +491] — the band swamps the observed
  champion-vs-AC gap (137 vs 7.5 px/s), so NO calibrated velocity claim on PIT-GT.
- reversal_rate: band [0, 0] — degenerate (GT-95th-pct threshold makes flips zero-rare here).
- hold_fidelity: NO valid rep (metric undefined on every half-split; only n=1 qualifying
  segment). Power floor recorded in `nullcal_pit.json`.
- **Diagnosis:** gap-64 clustering fragments the stride-sampled labels into 142 micro-events
  (~4.5 frames each); the (h) hold analysis used the 7 ORIGINAL divergence-segment spans.
  The framing metrics need the true segment unit (from the label-set manifest's segment
  provenance), not gap-64 fragments — and/or the pending event-spreading label queues.
  **Amendment required (pre-register BEFORE any cross-arm framing claim): re-operationalize
  framing-metric units on manifest segments.** capture/|Δcx| are unaffected (banked, frame-level).
- Ordering note: the fixture run printed framing values before the nulls existed (same frozen
  code); nulls banked same session; no cross-arm framing claim is made from those values.

**Campath provenance finding:** `G:/ballresearch/selector/campath/` spc/fair campaths are
dated 07-09/07-10 — PRE-v7, stale as W1 arms. Viewport-v1 (spc/fair) cells therefore wait for
the Run A replay through the CURRENT champion — do not score the stale campaths.

**Artifacts:** `G:/ballresearch/operator/{fixture_pit.json, nullcal_pit.json}`; server
checkout `G:/ballresearch/operator/repo` @ ed17d6e (single-branch, pinned py3.13).
**Next:** referee integration for per-band dual-rule reads; manifest-segment framing units
(amendment); oracle ladder A/B/C/D (Run A doubles as the v1-family champion campath source).

**[CORRECTED same night — the fragmentation diagnosis above was WRONG; measured directly]:**
the PIT labels cluster at gap-64 into exactly **7 large contiguous events (n = 126, 106, 77,
57, 49, 46, 44 ≈ the original-500) + 135 ext-div singletons** — gap-64 does NOT fragment
anything; 142 = 7 + 135. The framing metrics' problem is POWER, not the unit: only 7 events
bear velocity/hold information (singletons can't), so half-splits swing wildly → the ±456 px/s
band. The amendment therefore becomes: framing metrics read on events with ≥5 frames only,
nulls over those events; power grows via the pending event-spreading queues, not a unit change.
- **OPEN DISCREPANCY (gates W2's headline metric):** whole-event GT fx swings measured
  directly: 78, 471, 701, 1643, 1396, 723, 497 px — only **1 of 7** is ≤400 px, but
  DECISIONS (h) states **4 of 7** divergence segments are near-static (≤400 px, 282/500
  frames). (h) evidently operationalized "focal swing" on different units (sub-segments or
  windows) — its segment frame-counts (282 in 4) match no subset of the 7 gap-64 events.
  Before ANY cross-arm hold-fidelity claim: recover (h)'s exact computation and pre-register
  the reconciled stoppage-segment definition. hold_fidelity(n=1) as committed is CORRECT for
  whole-event max−min swing.
- Also measured: manifest carries `gt` ball fields but **0 of 650 PIT frames have ball GT** —
  Run B is spc/fair-only (benchmark v1 ball labels); PIT contributes the GT-viewport ceiling
  and Run C/D only. Label rows carry `fy` AND `hfov_deg` — GT includes zoom intent; a framing-
  width metric is future-available.

**[DISCREPANCY RESOLVED same night — (h)'s computation recovered from
`G:/ballresearch/geodet/_hold_check.py`]:** (h) clustered at **gap-40** (not 64), filtered to
**n≥4**, and classified "near-static" as **GT swing < 200 px AND max(our, AC) swing > 500 px**
— i.e. a hold-FAILURE classifier over sub-segment clusters; "4 segments / 282 frames" are
gap-40 clusters, and the doc text's "≤400 px" was prose imprecision for the script's <200.
Both measurements are correct on their own units: big gap-64 events mix a static stoppage
portion with play, so whole-event swings look large. **Pre-registered AMENDMENT (hold unit,
effective before any cross-arm hold read):** hold segments = gap-40 clusters with n≥4 and
GT fx swing < 200 px — WITHOUT (h)'s camera-swing>500 conditioning (conditioning on the
measured arm's own swing selects its failures and would bias the metric); hold fidelity =
arm swing ratio on those clusters, split-half null re-run on this unit before the first
cross-arm claim. W2's direct headroom estimate stays (h)'s 282 frames.

## EXP-OP-01: W1 operator scoreboard + oracle ladder — PRE-REGISTRATION (2026-07-25, written BEFORE any numbers)

**Arc:** Virtual Operator (kickoff brief 07-24; plan approved; DECISIONS (k) governs). W1 has
three jobs, all on banked/cached data: (1) the goal as numbers per range band, (2) the
input-vs-interpretation headroom split, (3) lookahead pricing. No other workstream builds until
the W1 table exists. This entry pre-registers every definition and read; NO live numbers here.

**Arms & instruments (all banked; inventoried on the server 07-25):**
- Human GT sets (`D:/training_data/viewport_label/`): `pittsford_dahua_gt` (div-sampled +
  ext-div; `labels.json` rows with `action=="view"`, focal `fx`; `manifest.json` `kind` marks
  `ext-div`), `pittsford_cold_audit` (cold audit, reported separately, never pooled),
  `spc_viewport_worst` + `fair_viewport_worst` (frozen viewport v1, primary family).
- Champion campaths: `G:/ballresearch/selector/campath/<gid>.json` (13+ games) and the
  EXP-72 bundle `G:/ballresearch/geodet/campath_pittsford.pkl` (+ `campath_pit_mg_*.pkl`).
- AutoCam reference arm: per-game `autocam_viewport.jsonl` via `distill_dataset.load_viewport`
  + `seg_offsets` — scored against the SAME GT views in every cell (Mark 07-24: match/beat is
  defined empirically off AutoCam's measured cells).
- Candidate dumps for the oracle ladder: `G:/ballresearch/selector/fullgame*/` incl.
  `fullgame_dahua/dahua_pittsford0625`; ball GT from the benchmark v1 label sets.

**Metric definitions (provenance: the EXP-72 session scorer `G:/ballresearch/geodet/_vp_score.py`,
now promoted to committed code):**
- `capture@600` / `capture@300`: fraction of labeled views with |campath_cx − fx| ≤ R (x-axis,
  matching the EXP-72 definition exactly). Secondary diagnostic: Euclidean |Δc| if fy exists.
- `|Δcx|`: median and p90 of |campath_cx − fx| over labeled views.
- `pan-velocity profile`: per-frame |Δcx/Δt| of an arm's campath over each CONTIGUOUS labeled
  segment, summarized median/p90, compared against the GT-label-derived velocity (consecutive
  labeled frames within a segment, stride-normalized). Only computable on contiguous segments —
  stated per instrument.
- `reversal rate`: pan-velocity sign flips with |v| above the 95th percentile of the GT-derived
  velocity distribution (threshold from GT only, fixed before arm reads), per labeled minute.
- `hold fidelity`: on segments where GT focal swing ≤ 400 px (the (h) stoppage definition):
  the arm's max swing, reported as median ratio to GT swing.
- Range bands by expected ball size at the GT focal point through the game polygon
  (`FieldGeometry.expected_ball_diameter_px`): **far < 8 px** (existing convention,
  `replay_fullgame --far-px` default) **; mid 8–15 px; near > 15 px** (15 = log-midpoint of the
  documented ≈3.5× near/far size span, EXP-DIST-66). Banding tolerates the known interior
  ruler waviness (±35%); bands are coarse bins, not measurements — the #19 fix will not
  re-band this instrument retroactively.
- Events: gap=64 frame clustering (noise protocol). Framing-events ≠ flip-events: each framing
  metric's event unit is the contiguous labeled segment.

**Match/beat (pre-registered, per band × metric):** "MATCH AutoCam" = the paired champion-vs-
AutoCam delta on shared events lies within that cell's split-half null band. "BEAT AutoCam" =
decisive under the dual rule (paired event sign test + referee v3 @ ac1f42c) with the champion
on the winning side. AutoCam rows appear in every cell of the table.

**Split-half null calibration (instrument admission, DECISIONS (g)):** for each NEW metric type
× instrument: 300 random event-level half-splits of the SAME arm's views; the null band is the
central 95% of half-vs-half deltas. Run and log BEFORE any live cross-arm read of that metric.
capture@600 / |Δcx| on PIT-GT are NOT new (EXP-72 read them); the framing metrics
(velocity/reversal/hold) and every metric on viewport-v1 ARE new.

**Correctness fixture (hard-fail gate, CLAUDE.md rule 8):** the committed scorer must reproduce
the EXP-72 cells from the same inputs before any new read: champion ALL 0.542 / 450 px,
AutoCam ALL 0.759 / 207 px (and the original-500 / ext-div splits). Tolerance ±0.001 / ±1 px.
Known count drift (640 vs 650 vs 500+150) is resolved by whatever the banked labels actually
contain; the fixture records the true n.

**Oracle ladder (CPU replay on cached dumps; games = those with dump ∧ ball GT, inventory
recorded at run time):**
- Run A: cached dump → current champion chain (select → track → plan) = baseline.
- Run B: GT ball positions injected as the only candidates (validate_tracker `--gt-override`
  pattern) → same tracker/planner. **B−A = ceiling for ALL input work (§3b pricing).**
- Run C: B + GT stoppage flags driving an oracle freeze-pan overlay (PIT divergence segments
  only — the only in-play stoppage GT). **C−B prices play-state awareness.**
- Run D (lookahead pricing): causal planner vs two hindsight oracles on identical inputs —
  (i) perfect-lead-Δ (planner consumes trajectory at t+Δ), (ii) zero-phase smoothing of the
  planned path; Δ ∈ {0.5, 1, 2, 3} s; per band; on A-inputs and B-inputs. **D−A per band =
  the lookahead build's price; no headroom in a band ⇒ no W4 lookahead build for that band.**
  (Prior art: EXP-DIST-40's v1 died on an untrustworthy interpolation TARGET + an unstudied
  window + a pre-referee instrument; these oracles re-decide FRAMING only and sweep the window.)
- Human GT = total ceiling; **GT−B = interpretation-only headroom.**
- Pre-registered reads: B−A, C−B, D−A, GT−B per band per metric, event-level, dual rule for
  any promotion-relevant claim; "no effect" claims state their power floor.

**Sequencing guards:** the table lands on the CURRENT champion (hn4 + v7). Task #21
(Dahua-refresh) may train but may NOT promote before this table + nulls are banked
(no-straddle, Mark 07-24). Placement per FLEET.md: CPU-heavy runs on the 4070 after staging;
server owns the chain and holds canonical data.

## EXP-DIST-72: PHASE 2 COMPOSED VERDICT — three-arm multi-geometry decisive experiment (2026-07-24)

**CLOCK PROVENANCE ((r).1):** the fleet ran ~2.2 h slow through 07-24; the laptop was
NTP-corrected ~16:20 true; the server stayed on the slow clock until after banking
(sync moment noted below). Laptop artifacts after 16:20 carry true time; server
artifacts carry the slow clock. Ordering provenance is by git history (every
pre-statement (h)–(s) committed before its number existed), not wall clocks.
SYNC MOMENT: post-banking, server read 21:13:29 pre-sync → 21:13:47 post (offset
had already converged to ≈0 on its own during the evening; day-time server stamps
remain ~2.2 h slow).

**Design:** mg_ctrl / mg_geo (gray3geo) / mg_norm (per-camera scale-norm) trained on
the same 21-game multi-geometry store (label-identical twins); referee =
compose_verdict (final rule set @ 4a0d63e, hierarchy per (i)/(j)/(k)); instruments
SPC-18k / SPC-134 / FAIR-6k^ / IRON-18k* (stress), each reported WITH its measured
ruler ratio per (m).2; viewport tier = Pittsford-human 640 GT views (500 div-sampled
+ 140 blind ext-div) + AutoCam reference.

**DETECTOR TIER (12/12 pkls; ratios: SPC 0.78, FAIR 2.54, IRON 1.07):**
- geo vs ctrl: SPC rows zero (SPC-134 argmax +0.261, pm=0.060, HARD strata 0.618 vs
  0.314 — real but venue-local); FAIR-6k argmax DECISIVE −0.217 (pm=0.004);
  **(j) DISQUALIFIER fires.**
- norm vs ctrl: all SPC rows zero; FAIR-6k argmax DECISIVE −0.217; **DISQUALIFIER.**
- geo vs norm: pattern 4, nothing separates.

**VIEWPORT TIER (640 human GT views, capture@600px / median |Δcx|):**
| source | ALL | original-500 | ext-div-140 |
|---|---|---|---|
| champion (hn4+v7) | 0.542 / 450px | 0.452 / 710px | 0.864 / 122px |
| mg_ctrl | 0.644 / 294px | 0.630 / 299px | 0.693 / 222px |
| mg_geo | 0.636 / 294px | 0.622 / 293px | 0.686 / 356px |
| mg_norm | 0.662 / 290px | 0.638 / 294px | 0.750 / 232px |
| **AutoCam** | **0.759 / 207px** | 0.708 / 211px | 0.943 / 126px |

Set composition caveats: div-sampled (failure-mode-weighted, (h): 56% stoppage);
ext-div = arm-vs-champion divergence frames (selects arm failures by construction).

**READS:**
1. **Arms do not separate at the viewport tier** — geo 19v22 / norm 28v22 event flips
   vs ctrl: zero. Combined with the detector tier: **pattern 4 among arms; no
   promotion** ((k).1: parity is not a win, and adoption requires a viewport win).
2. **The (r).2 SURPRISING branch fired:** geo/norm ride at ctrl parity despite
   rulers measured broken 1.7–2.8x on this game — the tracker's temporal machinery
   absorbs the size-conditioning damage that destroyed raw argmax. This is the
   (k).2 divergence measurement: the detector-tier veto stands formally, but what
   the pipeline consumes is far more robust than the argmax rows implied.
3. **Champion vs AutoCam on the Dahua holdout: AutoCam wins decisively on this
   failure-mode set** (ev5v18, sign p≈0.011; 0.759 vs 0.542). Cross-camera product
   transfer remains unsolved — consistent with the interpolation headline, NOT
   contradicting the frozen Reolink benchmark ("we crush AutoCam" was primary-family).
4. **All three mg arms beat the champion on Pittsford** (0.63–0.66 vs 0.45 on the
   original set) — a DATA effect (ctrl shows it too; arm-independent): Dahua in
   training closes most of the Dahua viewport gap. Coverage is the lever, again.
5. **Fairprobe = branch (q)(b):** the 2.54 band correction recovered geo 0.017→0.126
   (7x) but ctrl holds 0.246; veto stands; residual = intra-band waviness and/or
   appearance transfer — split by the ray-geometry fix. The (q) operator-diversity
   reading applies: venue-local gain is not fatal for THIS operator's self-covering
   fleet, but nothing here earns adoption tonight.
6. **Unified-mechanism test: NOT supported** — geo's SPC gains are ruler-error-
   independent (corr −0.001 / 0.117; SPC-134 flips 10v0 split 5/5 across error
   halves). The channel uses real venue-local signal; it is not merely a learned
   ruler correction.

**MECHANISM APPENDIX — one bug class, five anomalies ((s).3):** FAIR collapse,
Pittsford bow (1.50/2.78/1.52), SPC ±35% bow, the DEPTH-CAL cross-game collapse,
EXP-66 residuals — all one class: trace-interpolated expectation through a curved
projection, structurally wavy interior, amplified per-game by yaw. Factorization
SUPPORTED ((s).1): one shared bow curve x per-game scalar (+ yaw phase), corr 0.776.
Fix path: interim curve+scalar (days) → ray-geometry expectation (durable);
certification set SPC + FAIR-scale-error + Pittsford (Dahua row included).
A measured bug class, not a fifth story.

**VERDICT:** champions unchanged (hn4 + v7). mg_geo and mg_norm vetoed at the
detector tier ((j)), not adopted at the viewport tier (no win); mg_ctrl not adopted
(no viewport win over champion on primary family — untested there tonight; its
Pittsford edge is a data finding, not a promotion case). Detector arc CLOSES per the
07-23 (g) close condition. Day-one order (executed 07-25, fresh): stoppage-HOLD #1,
ray-geometry fix + interim curve/scalar #2, then per branch (q)(b): no channel
revisit before the ruler fix certifies.

**POWER ADDENDUM (07-25, analysis-only on banked dumps — the pre-registered
pattern-4 qualifier):** dual rule at the deciding instruments, 300 reps:
| instrument | structure | Δ=0.05 | Δ=0.10 | Δ=0.15 |
|---|---|---|---|---|
| SPC-18k (detector) | 494 fr / 9 ev | iid 0.24 / block 0.00 | 0.58 / 0.02 | 0.85 / 0.03 |
| PIT-viewport | 640 fr / 142 ev | iid 1.00 / block 0.72 | 1.00 / 0.97 | 1.00 / 1.00 |

**Qualified pattern-4 language:** the detector-tier arm zeros are WEAK nulls —
SPC-18k's 9-event structure cannot detect block effects at any tested magnitude
(passage-clustered misses are invisible to it). The VIEWPORT-tier arm parity is a
POWERED null: no effect ≥0.05 diffuse / ≥0.10 block at ≥97% power. The verdict's
"arms do not separate" claim therefore rests on the viewport tier, which is both
the objective (k) and the powered instrument — the detector tier contributes vetoes
and diagnostics only. Detector-tier event enrichment (more SPC windows/events)
remains the fix if a detector-tier null ever needs to carry weight alone.

**PARKED (surfaced tonight, no pre-statement, no action):** (i) mg-arms-vs-champion
Pittsford gap suggests a Dahua-refresh retrain of the product detector — parked for
the day-one roadmap discussion; (ii) mg_norm's suggestive viewport edge over ctrl
(28v22) — parked pending seed-2 noise band; (iii) 4070 as eval-dump node (stage
eval-game clips ~35 GB) — ops line in CURRENT STATE.

---

## EXP-DIST-71: eval_detector's FIRST-WINDOW span trap — the hn4 "Fairport anchor" scored 4/208 GT (stale early labels, 20 m off); and SPC evals have been using 134 of 1,785 labeled rows all along (2026-07-23)

**What happened:** the first Fairport anchor run returned ceiling 0.0 on 4 GT. Root cause:
`eval_detector` spans `[min(GT), min(GT)+max_frames]` (eval_detector.py:220-223). Fairport's
labels are whole-game folded (208 ball GT over frames 1,169–94,392; densest 6,000-frame
window = 47,640–53,640 with 175 GT); the default kept the 4 stragglers at the front. The
"result" was an INSTRUMENT failure — neither pre-stated anchor branch applies; anchors
re-running with `--span-lo 47640 --span-hi 53640` (Phase 2 chain's Fairport evals fixed the
same way).

**Protocol:** the default is NOT changed (SPC's historical numbers are first-window
instruments; changing the window breaks every anchor comparison). Instead **every eval on a
new game states its span explicitly**, chosen as the densest GT window (`fair_span.py`).

**Latent discovery — SPC has 1,785 labeled rows; the benchmark uses 134.** The frozen
first-window instrument is kept for continuity, but a SECOND instrument (densest/multi-window
SPC spans) can multiply eval event counts several-fold for ZERO new clicks — directly
attacking the 7-far/5-near-event starvation (EXP-DIST-70) before any new labeling. Queued
under eval enrichment.

**[ADDENDUM, same-day — the full anatomy]**
- **SPC full pool: 1,351 ball GT (1,785 was rows incl. meta/not-visible), 119 events** —
  10.8× the frozen window's 11. **Provenance is NOT protocol-uniform**: the pool mixes
  NORMAL (spc_normal1, seg spans), HARD-adversarial (clip1-5, hard, hard_review), and
  AGREEMENT-easy (agree/true_agree/dynamic_agree spans, 240 rows) sampling. → SPC-full is
  biased as an ABSOLUTE instrument; valid for PAIRED A-vs-B reads (same frames, both arms),
  with strata reportable by set name.
- **IRON is the best event instrument in the fleet: 576 ball GT, 264 full-pool events**;
  its historical evals were ALSO first-window truncated (88 GT / 20 events).
- **Span audit of the 13 fullgame-dump games: CLEAN** (99.5–100% GT coverage) — EXP-DIST-69's
  geometry-insensitivity numbers stand as measured.
- **Declared instrument registry (suffix mandatory, pre-declared per result, never chosen
  after seeing both):** SPC-134 (continuity) · SPC-18k@5473 (905 GT/37 ev) · IRON-18k@1501
  (222 GT/62 ev) · FAIR-6k@47640 (175 GT) · Pittsford-human. Phase 2 verdict pre-declares
  ALL of these; ~+4–5 h eval GPU accepted (labels-over-GPU exchange rate).
- **Re-reads of 67/68/70 on the wide instruments are NOT zero-GPU** (cached dumps only
  contain the 134-window candidates): requires 18k-window re-dumps of the gating checkpoints
  (ctrl, diff5, safe_ctrl, safe_geo ≈ 1.5 h each). Scheduled behind the Phase 2 chain —
  closed-stays-closed verification this week.

**[FAIRPORT ANCHOR RESOLUTION, 07-23 12:40 — pre-stated branch 1 fires]** Span-fixed
anchors on FAIR-6k@47640 (175 GT): hn4 ceiling 0.983 / argmax 0.726; ctrl_cur 0.971/0.663 —
**at-or-above SPC level → the self-leak behaves as modeled → proceed unchanged.** The
MAGNITUDE of the gap vs SPC-134 (argmax 0.73 vs 0.34) is NOT interpretable as difficulty:
it mixes self-leak inflation with strata composition (SPC-134 is HARD-dominated — 102/134
rows are hard-sampled — while the FAIR-6k window is span/normal-sampled). Anchors remain
context only; Phase 2 drops are computed vs each checkpoint's own SPC number per the
pinned denominator rule. (Curiosity flagged: hn4's DEPTH-CAL argmax collapses on Fairport
[NEAR 0.0] — depth calibration is game-specific; raw argmax is the diagnostic.)

---

## EXP-DIST-70: RETROACTIVE verdict audit — every closed call since EXP-DIST-46 against its metric's event-bootstrap CI (18 cached SPC dumps, zero GPU) (2026-07-23)

**Why (Mark):** prevents superseded argmax gaps being cited as measurements later. Full CI
table: `audit_ci_table.py` output (far-ceil/far-arg/near-ceil/near-arg, 95% event-bootstrap,
gap=64). SPC instrument capacity: 7-8 far events, 4-5 near events.

**Verdict classification:**

| verdict | deciding metric | audit basis |
|---|---|---|
| hn4 champion (46-era) | far-ceiling 0.965[0.85,1.00] | **stands on ceiling** |
| 58: σ5 REJECTED | 3 reads incl. ceiling 0.913 vs 0.965 (CIs overlap) | stands on **direction across three reads**; no single read resolvable |
| 59: diff5 ordering-worse | ceiling tie + argmax 0.122 vs 0.261 | ceiling-parity **stands**; argmax gap **never resolvable** (68-addendum event p=0.55) |
| 61: ph1v2 "costs nothing" | ceiling 0.957 vs 0.965 | ceiling claim **stands** — BUT far-argmax 0.104[0.02,0.15] vs ctrl 0.261[0.16,0.43] is the **only CI-SEPARATED pair in the table**: v2 person-cotraining damaged far ORDERING. Flag for any future ph1 revival. |
| 62: viewport crushes AutoCam | 0.683 vs 0.050 R15m | **stands, effect >> any band** |
| 63: stab opt-in | all pairs overlap | "no-harm" **stands on ceiling-parity**; stab-lean was **never resolvable** |
| 64/65: selector fd/db closed | product-chain R15m halvings | stands on **large-delta direction**; CIs not yet computed at selector layer (future) |
| 67: gray3geo seesaw | argmax CIs overlap | **downgraded same-day** (CI addendum) to suggestive |
| 68: encodings CLOSED | event-level flips | **stands on direction** (explicitly not significance) |
| 51/52/53 | — | VOID by store confound (55); excluded |

**Rule going forward (G1 redefinition, DECISIONS):** gate on ceiling + viewport capture;
argmax is directional color — never gate on a metric whose CI floor exceeds the effect size
under study.

---

## EXP-DIST-69: empirical test of the geometry descriptor (13 cached dump games, zero GPU) — plain detector is geometry-INSENSITIVE within the interpolation zone; the cache has NO generalization-zone points at all (2026-07-23)

**Question (Mark):** does the champion detector's per-game far-ceiling/argmax correlate with
descriptor distance-to-nearest-training-game? Real r → stratified verdict validated + the
descriptor becomes a training-set planning tool. Absent → Phase 2's novel-geometry rows read
as a geo-channel-specific test only.

**Result (n≥20-GT rows only; n=0 far rows initially poisoned the correlation and were cut):**
far-ceiling r=+0.20/+0.10 (n=8), far-argmax r=−0.53/−0.62 (n=8, WRONG direction, within noise
at this n), near ~0 (n=13). **But the decisive observation is RANGE RESTRICTION: every cached
dump game sits at d=0.00–1.05 — the interpolation zone.** The generalization zone (d>2, where
every customer deployment lives) has zero measured points in the entire cache.

**Conclusion:** (i) within interpolation, plain-detector performance is geometry-insensitive —
Phase 2's novel-geometry rows are a geo-channel-specific read, not a general geometry effect;
(ii) the stratified protocol could not be validated OR refuted from cache — Phase 2's Dahua/
holdout evals are the first-ever generalization-zone datapoints; (iii) the descriptor's
planning value rests on the EXP-DIST-66 physics, pending those points.
Script: `G:\ballresearch\geodet\descriptor_validation.py`.

---

## EXP-DIST-68 [CORRECTED same-day]: PAIRED read of the encoding factorial — at EVENT level everything is noise (diff5 "p=0.004" was frame-inflation); nothing leans positive → ENCODINGS CLOSED on direction (2026-07-23)

**CORRECTION (Mark): flips are NOT independent trials.** Misses arrive in passages (the near
autopsy: 11 misses = one 3.6 s sequence), so the frame-level sign test inflates n. Clustering
same-direction flips within 64 source-frames into EVENTS (32/128 as sensitivity):

| pair | frame-level p | events A / B (gap 64) | EVENT p (32 / 64 / 128) |
|---|---|---|---|
| diff5 vs ctrl | 0.004 (inflated) | 4 / 7 | 0.096 / **0.549** / 0.508 |
| df3 vs ctrl | 0.169 | 6 / 9 | 0.167 / 0.607 / 1.000 |
| ctrl_stab vs ctrl_cur | 0.150 | 7 / 5 | 0.332 / 0.774 / 0.754 |
| diff5_stab vs ctrl_stab | 0.581 | 4 / 4 | 1.000 / 1.000 / 1.000 |

diff5's "22 flips" were ~7 passages. **Nothing is significant at event level in either
direction; direction remains uniformly not-positive for encodings** → the CLOSED decision
stands on direction + zero evidence of benefit, not on a significance claim. **Protocol from
now on: EVENT-level sign test (gap=64 primary) is the mandatory read for small-delta
comparisons**; frame-level p is recorded only as the inflated diagnostic. Original
frame-level text below retained for the record.

## EXP-DIST-68 [original, superseded]: PAIRED per-frame read of the encoding factorial (cached SPC dumps) — diff5 is SIGNIFICANTLY worse (p=0.004), nothing leans positive → ENCODINGS CLOSED (2026-07-23)

**Method (Mark's protocol):** single-seed argmax deltas inside the ±0.078 seed band prove
nothing; instead, per-GT-frame paired argmax on the CACHED SPC dumps (134 GT, same frames,
same GT, zero GPU): which frames flip between arms? A real effect concentrates flips one way;
seed noise scatters them. Two-sided sign test on the flip counts.

| pair | argmax A / B | flips A-only / B-only (far) | sign p |
|---|---|---|---|
| diff5 vs ctrl (v2 store) | 0.231 / 0.351 | 6 (5) / **22 (21)** | **0.004** |
| df3 vs ctrl (v2 store) | 0.291 / 0.351 | 9 (8) / 17 (14) | 0.169 |
| ctrl_stab vs ctrl_cur | 0.321 / 0.254 | 20 (19) / 11 (10) | 0.150 |
| diff5_stab vs ctrl_stab | 0.343 / 0.321 | 8 (7) / 5 (5) | 0.581 |

**Verdict:** diff5's far-ordering damage is REAL (22 far frames the control gets that diff5
loses, p=0.004) — stronger than EXP-DIST-59's "worse within noise" phrasing. df3 leans the
same direction unresolved; the stabilized twin round is pure noise; stab-vs-cur still leans
stab on far without clearing noise (EXP-DIST-63's opt-in stance unchanged). **No second seed
needed: nothing is ambiguous in a direction worth funding.** → DECISIONS: encodings CLOSED.
Reader: `G:\ballresearch\geodet\paired_read2.py` (reusable for any dump pair — this is the
required read protocol before concluding from small SPC deltas).

---

## EXP-DIST-67 [CI ADDENDUM 2026-07-23 PM]: the "seesaw" is SUGGESTIVE, not established — event-bootstrap 95% CIs OVERLAP (SPC = 7 far / 5 near events as an instrument)

Event-bootstrap CIs (gap=64, B=2000; eval-sampling noise, `loo_nn_and_bootstrap.py`):
near-argmax ctrl 0.368 CI[0.00,0.60] vs geo 0.684 CI[0.20,0.80]; far-argmax ctrl 0.391
CI[0.07,0.46] vs geo 0.252 CI[0.12,0.36] — **all overlapping**. SPC's 134 GT collapse to just
7 far + 5 near EVENTS; it cannot resolve effects smaller than ~±0.2–0.3 argmax. The paired
per-frame flip DIRECTIONS still lean seesaw, so the conclusion is downgraded to: *the channel
is consumed and plausibly acts as a position prior single-rig* — the seed-2 replicate
(queued on FORTNITE-OP) and richer eval games decide. The "never ship single-rig gray3geo"
guard stays (cheap, and the direction is one-sided). Protocol: every reported argmax/ceiling
now carries its event-bootstrap CI alongside the seed band (different noise sources).

## EXP-DIST-67 [original]: gray3geo SAFETY pair (single-rig) — the geometry channel is NOT inert: identical val recall, but on held-out SPC it acts as a NEAR-position prior (near-argmax 0.368→0.684, far 0.391→0.252). Single-rig training must never ship it (2026-07-23)

**Question (Phase 1 gate of the geometry-conditioned detector plan):** does adding the
expected-ball-size channel (`gray3geo`) change anything when trained on SINGLE-RIG data?
Prediction: no — within one camera, band-y already encodes depth, so the channel is ~redundant.

**Method:** TWIN 5-game Reolink subset stores (`geodet/crops_safe_{ctrl,geo}` — identical
`build_distill_dataset` invocation ± `--geo-channel`; deterministic label selection gave
byte-equal sample counts, 16,173/9,513 pos), twin hn4-recipe trains (base 24, 40 ep) on the
1060, paired SPC held-out eval (134 GT, 1,501 frames, eval_tag recipe). No human crops / no
mined negatives in either cell (twins stay twins), so ABSOLUTE numbers are subset-scale —
only the pairing is meaningful.

**Result:**

| SPC (paired) | ctrl | geo | Δ |
|---|---|---|---|
| best_val_recall (health only) | 0.388 | 0.388 | 0 — **looks inert at train time** |
| candidate ceiling ALL | 0.978 | 0.978 | 0 |
| ceiling NEAR / FAR | 1.0 / 0.974 | 0.947 / 0.983 | −1 near ball / +0.009 |
| score-argmax ALL | 0.388 | 0.313 | −0.075 |
| **score-argmax NEAR** | 0.368 | **0.684** | **+0.316** |
| **score-argmax FAR** | 0.391 | **0.252** | **−0.139** |
| a3 vmax30 +kal ALL | 0.470 | 0.291 | −0.179 |

**Conclusion — the channel is CONSUMED, and single-rig it learns the WRONG thing.** The
detection SET is preserved (ceilings ~equal) but the score ORDERING shifts hard toward near:
the same near↔far seesaw signature as the selector's field_depth (EXP-DIST-64/65). Mechanism:
with one camera geometry, the channel is ~identical in every game — it degenerates into an
absolute POSITION feature (a y-coordinate proxy), so the net learns the label-density position
bias instead of a size-consistency invariant. This is exactly the failure mode predicted for
single-rig training, now measured. Two load-bearing consequences:
1. **gray3geo must NEVER be trained single-rig** (guard thought: the trainer could warn when a
   geo store spans one camera family).
2. Phase 1's actual question — "is the channel plumbed correctly and does the net use it?" —
   is answered YES emphatically. Whether MULTI-geometry data turns it from a position prior
   into a camera-invariant size reference is precisely Phase 2's decisive experiment; this
   result neither validates nor kills that thesis. (Promotion bar stays the product VIEWPORT
   vs AutoCam, not these tracker diagnostics.)

Artifacts: `G:\ballresearch\geodet\{gate_sweep.log, dump_{ctrl,geo}.log, runs/safe_{ctrl,geo}}`;
chain scripts alongside. Val-recall identity (0.388=0.388) vs SPC divergence is also a fresh
data point for EXP-DIST-54's "val-crop recall is a health signal only" rule.

---

## EXP-DIST-66: camera↔field geometry — the OPTICS are near-deterministic (r≈0.9) but per-game hyperparameters don't correlate with them; fd+db is NOT a net win pooled over 13 games (2026-07-21/22)

Three linked results that close the selector geometry arc (64/65) and open the detector one.

**(a) fd+db multi-game addendum to EXP-DIST-64/65 — the 2-game story didn't survive 13 games.**
fd+db0.4 vs v7 on ALL 13 games with fullgame dumps + ball_labels GT (training-leaked for BOTH
models equally, so the head-to-head is fair), product chain:

| pooled (13 games) | NEAR | FAR |
|---|---|---|
| v7 (champion) | 1040/1327 = 0.784 | 800/1206 = **0.663** |
| fd+db0.4 | 1062/1327 = **0.800** | 759/1206 = 0.629 |

NEAR +0.016 / FAR −0.034 pooled = net slightly negative. The SPC/Iron held-out win was real but
came from games with little far to lose. **v7's near/far operating point is ~Pareto-optimal:
near/far REBALANCING is zero-sum without NEW information.** (Per-game table:
`G:\ballresearch\selector\geo_corr.log` + eval scripts in `G:\ballresearch\selector\`.)

**(b) Geometry↔hyperparameter correlation (Mark: "does best db scale with polygon size/position?")
— NO usable law.** Per-game best depth-balance (argmax over v7/db0.2..0.5, 13 games) vs polygon
geometry: all |r| ≤ 0.23 (height +0.23, far-width +0.23, foreshorten +0.21). best_db is scattered
(5 games want v7, 4 want db0.3, rest split). The db0.4 *near-gain* does track field POSITION in
frame (near_y +0.51, bottom-gap −0.51, center_y +0.48) — Mark's "closer camera → bigger near
balls" direction — but n=13 and the far-loss offsets it. **A training knob 3 stages downstream of
the physics is the wrong place to look for the physics.**

**(c) Direct optics (the right target) — apparent ball size IS the polygon, nearly deterministically.**
Across 63 games (every game.json with a valid 10-pt polygon), `expected_ball_diameter_px` at the
near/mid/far touchline vs polygon shape:

| relationship | r |
|---|---|
| near-ball px ↔ near-touchline pixel width | **+0.92** |
| near-ball px ↔ near-line height in frame (near_y) | **+0.90** |
| near-ball px ↔ foreground below field (bottom-gap) | **−0.90** |
| near/far size ratio ↔ 1/foreshortening | **+0.87** |

Near ball ≈ **3.5×** the far ball in every game (perspective), and the fleet splits into two
geometry families: **Dahua** (field ~half frame width, near ball ≈9–10 px) vs **Reolink** (~full
frame, ≈14–15 px) — a real ~1.5× cross-camera spread that no current detector input encodes
(crops are dewarped band pixels: band-y encodes depth *within* a camera, nothing encodes it
*across* cameras). Bonus finding: 4 stored 2024 polygons were geometrically INVERTED (near/far
swapped or raw-vs-corrected rotation space) — triggered the polygon-store cleanup (see
DECISIONS 2026-07-22).

**Conclusion / direction:** the physics is exact and computable per game
(`world_geometry.expected_ball_diameter_px`); the selector can't use it (64/65: rebalancing is
zero-sum); the detector currently trains on essentially ONE geometry (`SET_POLYGONS` →
DEFAULT_POLY_0527) so a geometry input has nothing to learn from. → **Geometry-conditioned
detector plan** (branch `feat/geometry-conditioned-detector`): expected-size input channel,
multi-geometry crop store (Dahua = AutoCam-distilled per the standing filter decision).
**Promotion gate = the PRODUCT VIEWPORT vs AutoCam on frozen benchmark GT v1 (match-or-beat, no
swing regression) + the same viewport comparison on a held-out Dahua game** — detector metrics
(Spencerport ceiling-far, near no-regress) are intermediate diagnostics only (Mark, 2026-07-22;
the EXP-DIST-62 lesson).
Aerial thesis: apparent size is aerial-robust while ground-plane position is aerial-fooled — a
size-reference channel lets the net learn "bigger than ground-expected here = airborne ball".

---

## EXP-DIST-65: field_depth as a selector feature (Mark's "geometry as input") — REVERTED; it suppresses near; near is a TRACKER problem, not a selector one (2026-07-21)

**Hypothesis (Mark):** condition the selector on true field position instead of correcting for
camera geometry after the fact — add `field_depth` = `world_y/field_width` (homography, 0=near..
1=far), the camera-invariant near/far axis (apparent-size `depth` bakes in frame resolution +
camera placement; a far camera makes near balls small). Implemented APPEND-ONLY (feature #15 at
the end; `load_selector` pads older nets' `keep` with False → v7 loads unchanged, product numbers
byte-identical) so it couldn't break the champion. field_depth values verified sane (range [0,1],
median 0.66, no NaN, ~23% at the far-touchline clip).

**Result (clean A/B, 14 games + v7 labels, field_depth OFF vs ON, SPC product chain):**

| SPC | near R15m | far R15m |
|---|---|---|
| v7 (champion) | 0.316 | 0.722 |
| v8 field_depth OFF | 0.316 | 0.174* |
| v8 field_depth ON | **0.158** | 0.713 |

**field_depth TRADED near AWAY** (0.316→0.158) for far — the opposite of the goal. Mechanism: the
distillation labels are position-biased (near gold is rare, the teacher `track_ball` is noisier
near), so an explicit field-position input lets the small net LEARN that bias and suppress near
candidates. (The far "recovery" 0.174→0.713 is confounded — the OFF baseline's 0.174 was itself
the broken-baseline pnone collapse, EXP-DIST-64.) **REVERTED** from FEATURE_NAMES (the append-only
+ keep-padding backward-compat machinery in `load_selector` stays for future features).

**The load-bearing conclusion:** this is the THIRD selector-side near attempt to fail (σ was the
detector; depth-balance traded far; field_depth trades near). **Near is a TRACKER-DYNAMICS problem,
not a selector one** — consistent with the near-autopsy (fast near ball → Viterbi takes the
miss-state and the Kalman coasts away; 11/19 near misses were the miss-state over a rank-1 near
ball). The next near lever is the tracker's near-ball miss-state cost / transition model, NOT the
selector features. Selector reasoning (camera-invariance) was correct; the empirical answer is that
the selector isn't where near breaks.

*OFF baseline far 0.174 is the broken-baseline pnone collapse (14 of v7's 15 games); the faithful
15-game baseline (v8base) is training to establish a clean reference.

## EXP-DIST-64: selector depth-balance (v8) — near recovers but TRADES far; camera-invariance matters; v8 doesn't reproduce v7 → NOT promoted (2026-07-21)

**Hypothesis (from the near-autopsy, EXP near 2026-07-20):** the selector is UNDER-confident on
near-camera balls (near gold is scarce), so up-weighting near training samples by inverse
field-depth frequency lifts near without hurting far.

**Method:** `kill_test_selector --depth-balance` (new knob) on 14 training games' v7 labels, held-out
spc+iron. Three banding iterations, each committed — the arc is the finding:
1. global apparent-diameter bands → **no-op** (quantile bands are equal-count by construction);
2. per-game apparent-diameter (Mark: apparent size depends on polygon+frame-size, conflates cameras);
3. **camera-invariant FIELD DEPTH** (Mark: camera isn't fixed — a far camera makes near balls small):
   `world_y/field_width` from each game's homography, 0=near..1=far. Band counts confirmed near is
   genuinely the sparse band (near 6679 vs far 21864, 3.3x) in camera-invariant space.

**Result (field-depth, product chain w/ learned pnone, SPC held-out):**

| SPC | learned-argmax near | product near R15m | product far R15m |
|---|---|---|---|
| v7 (champion) | 0.30* | 0.316 | **0.722** |
| v8 db0 (baseline retrain) | 0.30 | 0.316 | **0.174** |
| v8 db1.0 (field-depth) | **0.389** | **0.474** | 0.530 |

Depth-balance WORKS as a lever: within the v8 pipeline it lifts near 0.316→0.474 AND rescues far
0.174→0.530 (better pnone calibration). Iron near 0.438→0.625. **BUT vs the champion it TRADES:
near +0.158, far −0.192 — not a free win.** And critically **my v8 BASELINE does not reproduce v7**
(far 0.174 vs 0.722 through the tracker; per-frame learned-argmax far is healthy 0.373, so the net
is fine — its `pnone`/miss-state calibration is off). Cause (verified 2026-07-21 after preserving
v7's recipe, `training/docs/selector_recipes/`): v8 trained on 14 of v7's 15 games (pittsford0507
dump cleared) + the selector's seed/val-split→temperature→`pnone` sensitivity; NOT dump staleness
(dumps predate the labels). **Decision: v7 stays champion; v8 NOT promoted. A faithful retrain
needs all 15 games + fresh labels (v7 STEP 1) + reported temperature.**

**Deeper finding:** even v7's product NEAR is only 0.316 (raw tracker w=1.0 miss=0.9 was 0.067) —
the near ball is a TRACKER-DYNAMICS problem (fast near ball, Viterbi miss-state coasts away; the
autopsy's 11/19 near misses were the miss-state), not purely selector confidence. Selector
reweighting helps but can't fully fix it. Next direction (Mark 2026-07-21): make the model
**geometry-conditioned** — feed camera-invariant field position (polygon-derived) as an INPUT
FEATURE, not just a balancing weight, so near/far is a learned, camera-adaptive concept.
The `--depth-balance` knob + camera-invariant field-depth code stay as validated infrastructure.

*v7 learned-argmax near from EXP-DIST-44 protocol; product numbers this entry.

---

## EXP-DIST-63: STABILIZATION PAIR VERDICT — ceiling-neutral, ordering leans stab (within noise); refreshed data sets a NEW best ceiling (2026-07-20)

The decisive pair: hm_ctrl_cur vs hm_ctrl_stab — same seed (123), same recipe (gray3, full-40,
no patience), same-day stores with IDENTICAL positives (EXP-DIST-60), differing ONLY in
`--stabilize` decode. Held-out Spencerport (134 GT):

| SPC | ctrl_cur (raw) | ctrl_stab (stabilized) |
|---|---|---|
| CEILING far R15m | **0.974** | 0.965 |
| CEILING all / med | 0.978 / 2.2 m | 0.970 / **1.5 m** |
| rank-1 far | 0.11 | **0.22** |
| score-argmax far | 0.165 | **0.243** |
| DEPTH-CAL argmax far | 0.322 | **0.339** |

**Verdict: stabilized training data is ceiling-NEUTRAL (delta = 1 ball) with a consistent lean
toward better far ordering and tighter candidate precision — every ordering metric favors stab,
but each individually sits inside the argmax variance band (±0.078). Decision: `--stabilize`
stays opt-in for training; not mandated.** The remaining stabilization stakes: (a) deploy-time
gust tail (A/B #2, awaiting the seg7/seg11 gust GT), (b) the diff5 x stabilization interaction
(diff5_stab/diff5_cur twins running) — jitter-freed diffs were the encoding's surviving hope.

**Side finding: ctrl_cur's 0.974 ceiling-far is the NEW BEST** (hn4 0.965) — the data REFRESH
alone (teacher tracks/filters evolved since v2, plus the repaired 05.10 segments) buys ~1 ball.
The champion recipe on current data beats the frozen champion artifact. Also: ctrl_stab's
full-40 gates-off tracker row collapsed like the patience runs — the "patience causes flat
scores" theory (EXP-DIST-61) is WRONG; eval_tag's gates-off protocol + the research tracker
simply can't hold a track, for any checkpoint. Selection quality is measured by the product
chain / viewport benchmark (EXP-DIST-62), not that row.

## EXP-DIST-62: the PRODUCT chain was the untapped measurement, not temporal coherence — viewport benchmark + swing metric + viewport labeling (2026-07-19)

Mark: temporal coherence is THE feature of a game ball — why untapped? Investigation: it wasn't.
The product stack (learned listwise selector v7 -> Viterbi ``rerank`` with physics transitions,
miss state, aerial bridge, OOB pin -> Kalman RTS -> planner) already implements it; the CAMPAIGN
was measuring the old research tracker (``track_ball``) instead. Full-chain replay on cached dumps,
scored on Mark's bar (planned viewport vs AutoCam viewport on GT frames, capture = ball inside the
view):

| GT frames | OUR capture | AutoCam capture | AC-missed frames -> OUR capture |
|---|---|---|---|
| Spencerport clip (hn4 cands, 134 GT, AC-adversarial) | 0.724 (med 96 px) | 0.060 | 126 -> **0.730** |
| WHOLE windy Fairport (hn2 cands, 206 GT) | **0.971** | 0.243 | 156 -> **0.962** |

Remaining visible gaps: NEAR+MED on the hard clip (0.42, n=19) and the last ~27% of AC-missed far.
**Swing metric** (round-trip pan whips of the planned path): whips are RARE (1-2/game); the dominant
divergence mode is sustained aim difference vs AutoCam (42-54% of runtime) and GT sides with US
87:6 / 101:16 during it. New tooling (committed): swing/divergence segment finder,
``build_viewport_label_queue`` + ``viewport-label.html`` (label the EXPECTED RENDERED OUTPUT:
focal point + view width, scrubbable padded segments, interpolation for airborne spans) — Mark's
viewport labels become the product benchmark for all selector/planner changes.

## EXP-DIST-61: ph1v2 — person co-training costs the ball channel nothing; tracker-collapse PATTERN across the early-stopped re-runs (2026-07-19)

ph1v2 (out_ch=2, person sidecar, v2 store, early-stop ep23 best@13): ceiling-far **0.957**
(hn4/ctrl 0.965, -1 ball = noise) — the person head is free. Near-band best-in-family (rank-1
0.74, argmax-near 0.947); far ordering weak (argmax 0.104). PATTERN: all three patience-10
re-runs (diff5, sig50, ph1v2) collapse the eval's built-in tracker while full-40 ctrl did not —
best.pt selection on the discredited val-crop proxy favors flat score fields. **Protocol going
forward: full-40, no patience (as the ctrl_stab/ctrl_cur pair runs).** ph1v2's real payoff is the
person CHANNEL as a selector feature (candidate-on-person penalty) — next selector experiment,
scored on the viewport benchmark.

## EXP-DIST-60: the v2 twin is UNREPRODUCIBLE — label drift, hidden 4x human upweighting, archive corruption; pivot to the stab/cur PAIR design (2026-07-19)

Building `crops_reolink_stab` as a "v2 twin" (same 15 games via the new `--games` pin, same
recipe, `--stabilize`) surfaced four buried facts:

1. **Teacher-label drift:** per-game teacher-crop filename overlap with v2 is only **15-35%**
   (every game) — the distill tracks/filters evolved since v2 was frozen (2026-07-01). v2's exact
   labels cannot be re-selected from today's inputs; any "same recipe" rebuild is a data refresh.
2. **v2's human crops are 4x-duplicated:** 6,104 human rows = 1,526 unique files, each EXACTLY 4
   rows — the historical store took ~4 append-mode `build_human_crops` passes. hn4/ctrl trained
   with human crops silently upweighted 4x. Part of the de-facto recipe nobody knew about.
3. **GT-neg drift:** stab's `--use-gt` mining produced 480 negs vs v2's 480 with **zero filename
   overlap** — the human-GT frame population has grown/changed since the hn4-era mining.
4. **Archive corruption (REPAIRED):** five 05.10 Upper 90 segments on F: are corrupt mid-file
   (CPU and NVDEC both die at frame ~1949; v2-era build read them fine → degradation since
   early July). Restored from the original D: recording dir (corrupt copies kept as `*.corrupt`).
   First attempt misattributed this to NVDEC session contention from a concurrent decoder — a
   60-frame solo probe "passed"; the deep probe found the truth. Same-day lesson as the wind
   sampling: shallow probes lie.

**Decision — pair design:** stabilization is isolated by TWO stores built from today's inputs by
the same deterministic pipeline, differing ONLY in `--stabilize`: `crops_reolink_stab` (built;
05.10 patched post-repair) vs `crops_reolink_cur` (chain running). `hm_ctrl_stab` vs `hm_ctrl_cur`
(gray3, full-40, no patience, seed 123) is the decisive comparison; hn4 anchors by recipe through
ctrl_cur. The v2-replication attempt (row-level human de-dup/re-dup etc.) was abandoned as
unfalsifiable archaeology.

**Pair symmetry, measured (stab v4 `7cdb9528` 83,924 vs cur v3 `4de2df29` 83,928):** positives
46,771/46,774 IDENTICAL (3 edge-of-crop casualties of the shift correction); human 3,913/3,914;
GT-neg frames 480/480 = 100%; background negatives 74% shared (the stabilized label coords nudge
the rng acceptance stream — sampling policy identical, specific patches differ; inherent to the
treatment and the least identity-sensitive row class). The pair premise holds where it matters.

## EXP-DIST-59: diff5 verdict — ceiling matches hn4 EXACTLY, far score-ordering WORSE; encodings don't pay on clean data (2026-07-19)

**hm_diff5** (5-ch signed-diff prelude, warm-started from the rescued ep8 ckpt, early-stop ep18
best@8, v2 store) — official eval + sweep on the Spencerport G1 clip (134 GT):

| SPC 134 GT (same rows) | ctrl (gray3) | diff5 | sig50 |
|---|---|---|---|
| CEILING far R15m | 0.965 | **0.965** | 0.913 |
| score-argmax far | 0.261 | 0.122 | ~0.12 (local 0.235) |
| DEPTH-CAL argmax far | 0.357 | 0.383 | — |
| best far config (a0.3 σ8 vmax3.5) | **0.861** | 0.443 | — |

The signed-diff channels PRESERVE detection (ceiling identical to the digit) but degrade far
score-calibration — under every comparable selection row ctrl wins. **On clean (unstabilized) data
the encoding thesis fails**; its surviving motivation is jitter-sensitivity — exactly what the
stab/cur twin round isolates. Caveats: warm-start + patience-10 (best.pt chosen by the discredited
val-crop proxy) vs ctrl's full-40; single seed.

## EXP-DIST-58: sig50 (σ=5) REJECTED on three readings — σ=4 confirmed; laptop DirectML eval path certified (2026-07-19)

**σ probe UP** (Mark's sign correction of the brief; σ-down sweep was void per EXP-DIST-50).
`hm_sig50` = hn4 recipe at σ=5.0, v2 store, early-stop ep18 (best@8). Three independent reads:
(1) LOCAL laptop stride-4 eval: ceiling-far **0.948** (hn4 0.965), argmax far 0.235;
(2) OFFICIAL server eval: ceiling-far **0.913**; (3) the built-in tracker COLLAPSES (far R15m
0.009, median 87 m) — σ=5's broader/flatter targets produce a score field track_ball cannot use.
**σ=4 stays the operating point; σ direction is closed both ways.**

**Laptop eval path certified:** hn2 re-run locally (Iris Xe DirectML, ONNX export, product
inference path, stride 4) reproduces the server anchors TO THE DIGIT (ceiling ALL 0.955 / far
0.948; argmax ALL 0.351), ±1 ball on band splits. Local evals are now bankable (~8.5 s/frame —
also re-confirming the iGPU product-floor problem). Mark's phase-union trick (stride-8 phase-0 ∪
phase-4 ≡ stride-4, valid because per-frame inference depends only on its own 3-frame history)
halved the second model's cost.

## EXP-DIST-57: wind sway MEASURED — episodic 10-13px roll gusts, 5-60px cumulative excursions, and ~6x mask-clipping on windy games; band-alignment experiment APPROVED (2026-07-18)

**Trigger:** Mark — would feat/camera-stabilization help the detection experiments? First pass
(300 consecutive mid-segment frames, center patch) said no: sub-pixel sway on calm AND windy games.
**Mark rejected it (correctly): gusts are episodic and the effect is cumulative.** Redone properly
on the windy 06.06 Fairport morning game (Mark: camera visibly moving "a foot side to side"):

1. **Audio-guided gust sampling** (loudest second per segment, L/C/R band patches): peak windows hit
   **10-13 px single-frame shifts at the band EDGES** (L 12.8 / R 10.2 vs C 4.2 = mast ROLL — the
   center-patch measure was structurally blind to it). Whole-game exposure (337 windows): 38% of
   moments >2 px, 5.6% >4 px, max 18.2 px — material diff-channel halo contamination.
2. **Cumulative excursion vs segment-start reference** (Mark's point — the polygon/homography are
   fitted ONCE): median 5-6 px, p90 11-18 px, p99 ~50 px, max 62 px; >5 px more than HALF the game,
   >10 px a quarter of it. Every geometry consumer (mask, in-field gates, expected-size, METERS) is
   silently off by the excursion for most of a windy game.
3. **Mask-clipping rate** (does drift actually cost recall?): teacher balls outside the static
   far-margin mask = **21.2% windy Fairport vs 3.66% calm Spencerport** (calm baseline = genuine
   aerial/OOB exits; windy figure includes some teacher junk but the gap is ~6x). Masked pixels are
   ZEROED before detection — a clipped ball is undetectable by construction, and windy-game TRAINING
   crops may contain zeroed balls with Gaussian targets painted on black (store-quality implication,
   unquantified).

**Decision (delegated by Mark): the experiment is WORTH IT — per-frame band-alignment correction,**
not the full feat/camera-stabilization port: estimate global band shift vs the polygon's reference
frame (phase-correlation, ~ms/frame) and offset the band-crop window before masking/detection.
Method lesson recorded: episodic phenomena need signal-guided sampling (audio→wind) and
edge-sensitive spatial coverage; two consecutive "clean" thin samples were wrong.

**Mark upgraded the scope (2026-07-18): stabilize-first for everything camera-motion-sensitive** —
build the correction into the pipeline (`BandStabilizer` in `iso_warp`, `--stabilize` through
detect/build/mine/eval/dump, all committed), re-run the lever round on stabilized data, run the
store build on the server while FORTNITE-OP works the unstabilized twins.

**A/B #1 result (same-day): hn4 raw vs `--stabilize`, windy Fairport seg8 — 175 human GT, NO
effect at moderate wind.** Candidate ceiling IDENTICAL to the digit (0.954/0.971/0.983 @R5/10/15m);
tracker argmax far 0.310 raw vs 0.293 stab (inside the ±0.078 argmax variance band). Seg8's
excursions (≤17 px vs the 400 px far margin) never push balls out of the mask. Two implications:
(1) the deploy-time geometry benefit concentrates in the extreme tail, not typical wind;
(2) the whole-game 21% teacher-track clipping figure was inflated by teacher junk + extreme
segments. Side-finding: AutoCam's own viewport collapses on the windy span (far R15m 0.063 vs our
0.310) — wind hurts AutoCam far more than our detector→tracker.

**A/B #2 pending — the extreme tail, with real GT (Mark labeling):** gust scan (1 Hz phase-corr
excursion series) found seg16 sits at a SUSTAINED ~50 px displacement from its segment start
(median 50.1, max 92.9) with its first 1300 frames in active second-half play; seg15 gusts to
16 px. Far-label sets built + served: `wind_fair0606_seg16_50px` (163 frames, the decisive set)
and `wind_fair0606_seg15_gusts` (278 frames, displacement-stratified bonus). Once labeled, re-run
both arms over 89200-96418 → ceiling-vs-excursion curve decides whether deploy-time stabilization
ships. Training-data effect (jitter-free stacks for diff encodings) is tested separately by the
`crops_reolink_stab` twin round regardless.

## EXP-DIST-56: size_cont_w with REAL measured sizes — catastrophic collapse; term unusable as implemented (2026-07-18)

**Setup:** EXP-DIST-47 Phase-4 wiring landed (candidates/2: `blob_diameter` in product code;
runtime + dumps emit per-candidate size in source px; ball_select reads both schemas). First live
test: sweep `size_cont_w ∈ {2,4,8}` on the strong physics family against `cands_spc_ctrl.pkl`
(which carries real measured sizes).

**Result:** collapse at every weight — `a0.3 sig8 vmax3.5` goes FAR **0.861 → 0.009** the moment
szc>0 (ALL 0.754 → 0.02–0.07). **Mechanism:** the term charges `w·(act−exp)²` on RAW pixel sizes,
and auto-measured blob diameters are extremely noisy (a far ball flickers ~3→15 px under 0.07 bpp
compression), so every legitimate transition pays a huge penalty and the track freezes onto
statics. EXP-DIST-47's szc=8 coach-rejection was a clean hand-measured case — it does not transfer
to measured sizes.

**Conclusion:** do NOT enable `size_cont_w` in the shipped config. The size PLUMBING stays (sizes
in candidates/2 are cheap and feed selector features/diagnostics); the continuity TERM needs a
noise-robust redesign before retry (ratio/log-scale penalty + Huber/cap, or a smoothed size
estimate). The person-CHANNEL route (ph1v2, training now) is the more promising anti-head lever.
Data: szc rows appended under "##### SPC ctrl szc sweep #####" in sweep_encoding.log.

## EXP-DIST-55 VERDICT (2026-07-18 01:26): CONFOUND CONFIRMED — the control reproduces hn4's ceiling TO THE DIGIT; the lever batch is VOID; plus a new finding: far-argmax has large seed variance

**`hm_ctrl`** (exact hn4 protocol — from-scratch, 40 ep, no patience, same box/venv — on the restored
hn4-era store view, index sha `abb28320dd23cc08`):

| metric (R15m) | ctrl | hn4 (EXP-DIST-46) | batch (51/52/53) |
|---|---|---|---|
| ceiling ALL / FAR / NEAR | **0.970 / 0.965 / 1.0** | 0.970 / 0.965 / 1.0 | 0.93–0.948 far |
| score-argmax NEAR | **0.895** | 0.895 | 0.684–0.895 |
| score-argmax FAR | 0.261 | 0.339 | 0.209–0.270 |

1. **Ceiling and near-argmax reproduce hn4 EXACTLY** → the code path is clean and the in-place store
   mutation (−480 GT negs, +782 corrnegs) fully explains the batch's ceiling cluster.
   **EXP-DIST-51/52/53's lever conclusions are VOID as comparisons vs hn4** — the levers were never
   tested on hn4's data. Re-runs (diff5 / sig50 / ph1v2, all `--index-version 2`) queued on FORTNITE-OP.
2. **NEW FINDING — far-argmax seed variance is large:** identical protocol + identical data produced
   far-argmax 0.261 (ctrl) vs 0.339 (hn4) — a ±0.04–0.08 band from the unseeded draw alone, while
   ceiling-far reproduced to 3 digits. Implication: **single-run far-argmax deltas smaller than ~0.08
   are not interpretable**; ceiling-far is the stable per-run gate metric; argmax comparisons need
   either seeds or replicates. The batch's far-argmax spread (0.209–0.270) vs the hn4/ctrl anchor pair
   (0.261–0.339) overlaps — the levers' far-argmax "regressions" were likely mostly store + noise.
3. **Log hygiene:** the "##### SPC hn4 (baseline)" section in `sweep_encoding.log` is UNRELIABLE —
   its source pkl (`cands_spc_hn4.pkl`) does not exist and its rows duplicate df3's (stale artifact of
   the 07-17 aborted-chain window). hn4's anchor numbers remain EXP-DIST-46's, from `hn4_spc_clip`.
4. **1060 characterization (task follow-up):** batch-1 = 2,465 ms per 1552×2560 tile on the shipped
   hn2 ONNX; **batch>1 is impossible with the current export** (batch dim is static — "Expected: 1").
   If dump turnaround ever matters, re-export with a dynamic batch axis first, then re-measure.

## EXP-DIST-55: the batch's confound FOUND — crops_reolink was MUTATED IN PLACE by the hn5 chain; df3/sig30b/ph1 all trained on hn5's data, not hn4's; CONTROL running (2026-07-17)

**Trigger (Mark):** three unrelated levers landing within 0.009 of each other and below BOTH
baselines is systematic, not scatter — design the control to DISCRIMINATE. Candidate 1 (hn4 was
warm-started) is ELIMINATED: `train_hn4.ps1` shows from-scratch, full 40 ep, no `--resume` (the
external brief's "warm-start lineage" description was wrong). Candidate 2 (patience undertraining)
cannot explain df3, which ran the full 40. Digging further found candidate 3:

**The store hn4 trained on is NOT the store the batch trained on.** `crops_reolink` index snapshots:
- `index.prehn5.json` (hn4's store): 83,459 items, train **77,916** (= hn4's training log exactly),
  480 GT-guarded negs present, **0 corroboration negs**.
- `index.json` (current — what df3/sig30b/ph1/ph1b ALL trained on): the hn5 chain **removed the 480
  GT-guarded negatives and added 782 corroboration negatives in place** → train 78,218. That is
  hn5's data configuration — the negative set EXP-DIST-46 proved over-suppresses far balls (hn5
  far-argmax 0.148, family worst). Every batch run inherited it silently.

**CONTROL (running, `hm_ctrl`):** exact hn4 protocol — from-scratch, FULL 40 epochs, NO patience,
default σ/encoding, same box/venv, our branch code (default path proven byte-identical) — on a
restored hn4-era store VIEW (`crops_reolink_hn4era`: prehn5 index + junction to the shared crops
dir; all 83,459 files verified present; train=77,916 confirmed at startup). Readout ~07:00 07-18.
Interpretation: ceiling-far ≈0.965 → code path clean, store mutation was the confound, **the whole
lever batch (51/52/53) is VOID** and levers re-run on the hn4-era view; ≈0.93 → confound is
elsewhere (seed/code) — investigate before any re-run.

**Durable fix this mandates (queued):** stores must stop being mutated in place — mining writes a
NEW index version (`index_vN.json`) + the trainer takes an explicit index/version argument, so every
run records which data it saw. The silent-mutation hazard cost this batch ~3 GPU-days.

## EXP-DIST-54: the val-crop proxy INVERTS on this batch — worse than no metric (2026-07-17)

**Finding (Mark: worth its own entry):** across the five-run batch, val-crop recall ordering has NO
positive relationship to held-out quality — it inverts at the extremes:

| run | val proxy | held-out far argmax |
|---|---|---|
| ph1b | **0.410** (best) | 0.270 |
| df3 | 0.403 | **0.209** (worst) |
| ph1 | 0.397 | 0.226 |
| sig30b | 0.379 | 0.235 |
| hn4 | 0.368 (worst) | **0.339** (best) |

This is the third and decisive confirmation of the EXP-DIST-46 lesson ("val-crop recall ≠ held-out
quality") — now with a full anti-correlated table. **Implication: the proxy has been steering
`best.pt` checkpoint selection, the new `--patience` early-stop, and possibly earlier decisions —
an inverted metric is worse than no metric.** (The EXP-DIST-55 store confound may explain part of
this batch's inversion — the proxy val split lives in the same mutated store — but hn4-vs-hn2 era
data already showed the same sign.) Mitigations until a better selector exists: (a) treat val-crop
recall as a TRAINING-HEALTH signal only; (b) patience stays acceptable for wall-clock but
checkpoint SELECTION should prefer late/multiple candidates dumped against held-out (e.g.
`--save-every` + a cheap held-out mini-dump per candidate — queued); (c) never promote on proxy
numbers — G1/G2 only.

## EXP-DIST-53: ph1 person head (out_ch=2, yolo26n sidecar) — G1 FAIL on far; near preserved; val proxy now fully discredited (2026-07-17)

**Hypothesis (EXP-DIST-47 Phase-4 / external brief):** a 22 cm ball ≡ a 22 cm head, so a person-center
channel on the shared backbone is the only context that can separate them; ball channel should stop
firing on heads.

**Method:** person supervision WITHOUT a store rebuild — yolo26n over the STORED gray crops
(vision-calibrated recipe: never upscale + two-scale union @ thr 0.20; naive 1280-letterbox upscaling
detected NOTHING), annotated on FORTNITE-OP's 3060 Ti (79,183 crops, 100% of train covered, 1.85 h).
`--person-sidecar` + masked person loss (λ=0.5), out_ch=2. Two legs: `hm_ph1` (crashed at ep26 when
the box was game-loaded — peak ep20 rescued) and `hm_ph1b` (resumed overnight from ep20, early-stopped
ep25, val 0.410 @ep15 — the family's highest-ever proxy). Both dumped + swept, same protocol.

**Result (R15m, 115 far / 19 near) — the full batch:**

| run | ceiling FAR | argmax FAR | argmax NEAR | val-proxy peak |
|---|---|---|---|---|
| **hn4 (champion)** | **0.965** | **0.339** | 0.895 | 0.368 |
| df3 (EXP-51) | 0.939 | 0.209 | 0.789 | 0.403 |
| sig30b (EXP-52) | 0.93 | 0.235 | 0.842 | 0.379 |
| ph1 (ep20) | 0.93 | 0.226 | **0.895** | 0.397 |
| ph1b (resumed) | 0.93 | 0.270 | 0.684 | 0.410 |

**Conclusion: FAIL on the far gate; the least-bad of the batch.** ph1 ties hn4 on near-argmax (the
person head costs nothing near) and posts the batch-best far ranking (P(rank≤3) 0.42); ph1b's far
argmax 0.270 is the batch best but its near collapsed (0.684) — the extra proxy-val epochs traded
near ranking away. **The val-crop proxy is now fully discredited for model selection:** its ordering
across this batch (0.410 > 0.403 > 0.397 > 0.379 > hn4's 0.368) has NO relationship to held-out
quality (hn4 best, df3 worst). Person-channel value likely lives at the SELECTOR (centroids/size
gate — the original EXP-DIST-47 Phase-4 plan), not in the ball channel itself.

**Open question the whole batch raises — RUN VARIANCE:** every new run (three different single
levers) landed ceiling-far 0.93–0.939, below even hn2's 0.948. Either all three levers coincidentally
cost ceiling, or hn4's 0.965 is partly a favorable unseeded draw. **Next: a seeded/plain hn4-recipe
CONTROL re-run under patience (~6 h) to size run-to-run variance before interpreting any of these
gaps as real** — and before spending on the next lever.

## EXP-DIST-52: sig30b (fixed σ=3, CORRECTED sweep) — G1 FAIL; σ=4 stays champion (2026-07-17)

**Hypothesis (EXP-DIST-49 follow-up):** the ball is ~4–17 px but σ=4 paints a ~16 px target blob;
a sharper fixed σ=3 should stop training the net to fire on head-sized blobs (a "half-measure"
toward dynamic-σ). This redoes the run the σ-precedence footgun voided (EXP-DIST-50) — the first
training where `--sigma` actually took effect.

**Method:** `hm_sig30b` = from-scratch hn4 recipe + `--sigma 3.0` + `--patience 10` (new flag; run
early-stopped at ep33, peak val 0.379 @ep23 — patience saved ~4 h). Same held-out dump + sweep
protocol as df3/hn4.

**Result (R15m, 134 GT: 115 far / 19 near) vs hn4:**

| metric | sig30b | df3 (51) | hn4 | verdict |
|---|---|---|---|---|
| ceiling ALL / FAR | 0.94 / 0.93 | 0.948 / 0.939 | 0.970 / 0.965 | FAIL (far bar .955) |
| score-argmax ALL / FAR / NEAR | 0.321 / **0.235** / 0.842 | 0.291 / 0.209 / 0.789 | 0.418 / **0.339** / 0.895 | FAIL (far bar .32) |
| far rank r1 / P(≤3) / med / absent | 0.18 / 0.35 / 6 / 0.07 | 0.15 / 0.26 / 7 / 0.06 | (hn4 better) | — |

**Conclusion: NEGATIVE.** σ=3 regressed far argmax ~30% relative vs σ=4 (0.339→0.235) and shaved
the ceiling too — sharper targets did NOT sharpen far discrimination; they cost recall across the
board (a 3–4 px far ball may simply need the broader supervisory blob to accumulate gradient).
Both of the brief-driven levers (diff encoding, target sharpness) have now failed the same gate in
the same direction; **the hn4 recipe (gray3, σ=4, GT-guarded hard negatives) remains champion.**
Depth-scaled dynamic-σ (σ≈ball radius: ~1.5 far / ~5 near) is a DIFFERENT claim than fixed σ=3 and
remains runnable after `crops_reolink_dyn` depth completion — but two same-family failures argue
for letting the ph1 person-head result (and its held-out eval, running now) pick the direction
before more σ spend. Data: `cands_spc_sig30b.pkl`, `G:\ballresearch\encoding\{train_sig30b.log,
sweep_encoding.log}` (incl. same-protocol hn4 baseline rows appended by the chain).

## EXP-DIST-51: df3 signed-diff encoding — G1 FAIL; far discrimination REGRESSES vs hn4 (2026-07-16)

**Hypothesis (external brief + Jmot re-analysis):** replacing the two redundant gray history frames
with signed differences `(g_t, g_{t-1}-g_{t-2}, g_t-g_{t-1})` hands layer 1 a pixel-level,
direction-preserving motion signature (a ball moving > its diameter/frame leaves a clean ± lobe
pair), un-stranding the temporal signal and improving ball-vs-distractor discrimination — the thing
hn2/hn4/hn5 hard negatives have been patching.

**Method:** `hm_df3` = from-scratch hn4 recipe (crops_reolink + GT-guarded negs, base 24, 40 ep,
σ=4) + `--input-encoding diff3` (EncodingPrelude, in-graph — see the exp/detector-diff-encoding
branch). Val-crop peak 0.403 @ep17 (best in family on the weak proxy; new bests at eps 1,2,3,4,8,17
— gaps 1,1,1,4,9 — then 23 dry epochs; loss still falling = crop memorization). Dump + sweep on the
held-out Spencerport clip (134 GT: 115 far / 19 near), frame-aligned with the hn4 protocol.

**Result (R15m) vs hn4 (EXP-DIST-46):**

| metric | df3 | hn4 | verdict |
|---|---|---|---|
| ceiling ALL / FAR / NEAR | 0.948 / 0.939 / 1.0 | 0.970 / 0.965 / 1.0 | FAIL (bar: far ≥ .955) |
| score-argmax ALL / FAR / NEAR | 0.291 / **0.209** / 0.789 | 0.418 / **0.339** / 0.895 | FAIL (bar: far ≥ .32) |
| far rank: r1 / P(≤3) / med-rank / absent | 0.15 / 0.26 / 7 / 0.06 | (hn4 better across) | — |

**Conclusion: NEGATIVE, both gates failed.** The encoding didn't just fail to move far-argmax — it
regressed it ~40% relative (0.339→0.209), with near-argmax also down (0.895→0.789) and ceiling-far
slightly down. The val-crop proxy (0.403, family-best) was again anti-correlated with held-out
quality — third confirmation of the EXP-DIST-46 lesson. Plausible mechanism for the regression:
collapsing appearance to ONE gray frame costs more far-band discrimination than the diff channels
return (far balls are slow in most GT frames → weak diff signature; compression flicker lives at
ball scale → noisy diffs; and the GT-guarded hard negatives were mined against gray3 models'
confusions). **Per the plan gate: the motion/color thesis is re-examined before any superstore /
chroma build — the 55 GB store is NOT justified on this evidence.** A softer variant (replace only
one history frame, keep two appearance frames) is a possible cheap follow-up but is NOT queued.
The diff3 machinery stays (default-off, byte-identical, tested) — the negative result is the value.
Data: `G:\ballresearch\distill\cands_spc_df3.pkl`, `G:\ballresearch\encoding\{train_df3.log,
sweep_encoding.log}`; checkpoint `runs/hm_df3/best.pt` (ep17).

## EXP-DIST-50: the σ sweep is VOID — a CLI-σ precedence footgun trained hm_sig30 at σ=4; fixed (2026-07-15)

**Trigger:** pre-flight verification of the "in-flight σ sweep" (EXP-DIST-49's `sigma_exp` task) before
reading its results.

**Finding — the footgun:** `HeatmapCropDataset.__init__` did
`self.sigma = data.get("summary", {}).get("sigma", sigma)` — the store summary **always** won over the
constructor/CLI σ, so `train_v4_heatmap --sigma X` on a prebuilt store silently trained at the store's
build-time σ. Verified against the actual sweep launcher (`G:\ballresearch\selector\sigma_exp.ps1`): it
ran `training.train_v4_heatmap --out G:/ballresearch/distill/crops_reolink --sigma 3.0` with **no
rebuild**, and `crops_reolink`'s summary records `"sigma": 4.0`.

**Verdict on the sweep:**
- **`hm_sig30` trained at σ=4.0, not 3.0 — it is an hn4-recipe replica** (val-crop recall ~0.39 matches
  the hn4 family) and says nothing about target sharpness. Do not read `cands_spc_sig30` as a σ=3 result.
- The sweep never completed anyway: `sigma_exp.status` stuck at "dump sig30" since 07-14 17:41, the σ=2.5
  arm never started, no `sigma_exp.done`.

**Second footgun in the same family (fixed together):** `_DynSigmaDataset`/`_DepthDataset` silently
substituted a mid-field default for a missing per-item `depth` (`r.get("depth", 0.5)` / `0.0`), so
`--dynamic-sigma` on a depth-less store would have trained EVERY positive at one constant σ≈3.25 without
erroring. The live risk is real: `crops_reolink_dyn` is **partially** depth-recorded (21,133 of 45,164
positives) — it looks depth-ready and isn't.

**Fix (this commit):** (1) σ precedence — `--sigma`/constructor σ default is now a `None` sentinel; an
explicit value WINS over the store summary, `None` defers to it (fresh builds default 4.0); (2)
`require_positive_depth()` hard-fails `--dynamic-sigma`/`--far-weight` when any positive lacks `depth`,
and the datasets read `r["depth"]` strictly. Unit tests cover both.

**Next:** re-run the sweep corrected (σ=3, σ=2.5 + dynamic-σ after completing `crops_reolink_dyn` depth
coverage) — scheduled after the current multi-game hn2/4/5 dump batch drains the GPU.

## EXP-DIST-49 [CORRECTED 2026-07-14]: dynamic-σ WAS wrongly dismissed — ball size DOES vary ~4→17px with depth; my "constant size" was a flawed measurement (2026-07-13/14)

**CORRECTION (2026-07-14, after Mark challenged the constant-size claim — he was right):** the
conclusion in the original entry below (dynamic-σ dead, ball size constant) is **WRONG**. Two errors:
1. **Wrong warp.** I explained `FieldWarp` (`training/data_prep/field_warp.py`, which normalizes ball
   size from a size-vs-row gradient) — but that is unused training code. The ACTUAL product warp is
   `CropIsoWarp` (`video_grouper/inference/iso_warp.py`): a plain field-band crop
   (`img[y_top:y_bot]`) + a single **isotropic** `cv2.resize` by `scale=target_width/src_w` (same
   factor both axes). It does NOT depth-normalize — its size normalization is CROSS-CAMERA angular
   scale only (Dahua vs Reolink panorama width). Perspective is preserved, so far balls ARE smaller.
2. **Flawed measurement.** My "constant ~6px" area-threshold blob measure captured the ball's
   ~constant bright specular CORE, not its true extent — it badly under-measured near balls (17px→5px).

Geometry (a projected 22 cm ball via the field homography — exact, not a pixel measure) shows the real
gradient:

| depth (far→near) | 0.0 | 0.2 | 0.4 | 0.6 | 0.8 | 1.0 |
|---|---|---|---|---|---|---|
| Cleveland diam px | 3.8 | 6.0 | 8.5 | 11.2 | 14.2 | 17.5 |
| Spencerport diam px | 3.8 | 5.7 | 7.9 | 10.3 | 13.0 | 15.9 |

A **4–4.6× range**. So **dynamic-σ IS motivated**: fixed σ=4 (target blob ≈16px) fits NEAR balls
(~17px) but is ~4× too big for FAR balls (~4px) — exactly the weak band. A depth-scaled target σ
(tight far ~1.5, broad near ~5) matches the ball. The eval-integrity + AutoCam-GPU findings below stand;
only the dynamic-σ conclusion is reversed. Next: analyze the in-flight σ=3 fixed sweep (a half-measure —
a sharper *constant* σ helps far but hurts near), then run dynamic-σ properly (depth-scaled, tuned).
Original (wrong) analysis kept below for the record.

---

**[SUPERSEDED — the reasoning below reached the wrong conclusion; see CORRECTION above]**

**Trigger:** Mark — "the heatmap blob should NOT be a constant size; the ball gets bigger/smaller with
depth" → try dynamic-σ (Experiment A: per-sample target σ scaling with field depth) as a separate
"flavor" vs a size head (Experiment B), and keep the GPU busy overnight.

**Method:** BEFORE spending GPU-hours, measure the premise directly on the champion store
(`crops_reolink`, the hn4 data). Key realization: the detector does NOT see the source panorama — it
sees `_native_iso_warp` dewarped BAND crops. Recompute each positive crop's faithful field depth exactly
as `build_heatmap_crops` records it (`depth = clip(warp.points([(bx,by)])[0][1] / bh, 0, 1)` — the
ball's warped band-y; a pure geometric transform, no decode), then measure the ball's apparent diameter
in the crop, binned by depth (n=4712 matched positive crops across Cleveland/Cuyahoga/Pittsford).

**Result — ball diameter is FLAT across field depth:**

| dby/bh depth band | n | mean diam | median |
|---|---|---|---|
| 0.00–0.20 (far) | 836 | 5.83 | 5.97 |
| 0.20–0.30 | 1331 | 5.97 | 6.18 |
| 0.30–0.40 | 402 | 6.30 | 6.68 |
| 0.40–0.55 | 297 | 5.66 | 6.08 |
| 0.55–0.90 (near) | 326 | 5.41 | 5.53 |

If anything the FAR balls are slightly BIGGER than near — the opposite of dynamic-σ's assumption.
Vision-confirmed on rendered crops (far→near): the ball is a ~6px blob everywhere; the high-depth
"near" crops mostly contain large white PLAYERS (~80px), not a bigger ball. Depth is also heavily
concentrated (median 0.255; ~95% in 0.1–0.4), so dynamic-σ (σ 2→8 over depth 0→1) would emit σ≈3.3 for
virtually every ball regardless.

**Conclusion:** Mark's intuition holds in the SOURCE panorama, but the `iso` in `_native_iso_warp` is
literal — the isometric field-plane warp NORMALIZES apparent ball size before the detector sees it. So a
FIXED σ is correct and **dynamic-σ is not just unhelpful, it is counterproductive** (it would paint
oversized targets on near balls that aren't actually bigger). Flavor A is DEAD. The `--dynamic-sigma`
code (fb01b64) is kept default-off with a "dominated — see EXP-DIST-49" note (not reverted: preserves
the machinery + the documented negative result).

**Evidence-motivated replacement (running — `sigma_exp` task):** the ball is ~6px but fixed σ=4 paints a
~16px target blob — too broad. The real lever is target SHARPNESS, not depth-dependence → a fixed-σ
sweep (σ=3, σ=2.5 vs hn4's σ=4), from scratch, same recipe (`--base 24 --epochs 40`), dumped + swept on
held-out Spencerport. Chained after the Iron dump. [results pending]

**Also this session (verification, not new experiments):**
- **Eval-integrity re-verified (hn4/hn5 CLEAN):** direct scan of `crops_reolink` for held-out
  contamination → **0** crops from Spencerport 05.31 and **0** from Iron 06.15 (the 5207 "05.31" and
  5149 "irond" hits are same-date West-Seneca and trainable 06.04-Irondequoit — date/name collisions,
  not the held-out games). The "we beat AutoCam on both held-out games" claim is NOT
  detector-contaminated.
- **Latent leak FIXED in `train_v4_heatmap`:** its far_label `--rebuild` path guarded only Spencerport;
  `heat_0615_*` + `iron_ourloss_spans` (both = held-out Iron 06.15) would have leaked into a rebuild.
  Added `2026.06.15` + `0615` to `HELD_OUT_TOKENS`, removed the `heat_0615` training-polygon entry.
  hn4/hn5 used the DISTILL path (`build_distill_dataset`/`mine_hard_negatives --holdout`), so no
  existing model is affected — this only hardens the far_label trainer.
- **AutoCam is CPU/RAM-bound, NOT GPU-bound** (Mark's correction — verified): `autocam.exe` = 2281
  CPU-s / ~4 GB RSS; GPU memory-util 1–9% / 595 MiB / ~30 W on a 120 W card; it appears in
  `nvidia-smi` only as a `C+G` GUI context (same tag as dwm/explorer). So a GPU training coexists with
  the CPU-bound aim batch — which is the parallelism the overnight plan uses.

## EXP-DIST-48: oob_w — oob0 tried then REVERTED (wrong metric); + we already BEAT AutoCam 0.71 vs 0.44 (2026-07-13)

**Trigger:** the new multi-game viewport-vs-AutoCam sweep (`sweep_viewport.py`, scores our planned
viewport vs AutoCam's corrected aim across games/configs) flagged `oob0` (oob_w=0) as the best config
on BOTH Spencerport + Chili — higher AutoCam-agreement AND fewer divergence windows.

**Method:** validated against HUMAN GT (not just AutoCam-agreement) via `oob_eval.py` — rerank's SELECTED
pick vs `ball_labels` at R15m, far/near bands, oob_w in {0,1,2,4}, on both held-out games.

**Result (selected recall vs GT):**

| game | band | oob_w=2 (old champion) | oob_w=0 |
|---|---|---|---|
| Spencerport 05.31 | far / near | 0.384 / 0.385 | **0.412 / 0.431** |
| Irondequoit 06.15 | far / near | 0.301 / 0.433 | **0.318 / 0.523** |

Monotonic on Spencerport (oob_w 0 best → 4 worst) with fewer miss frames (7552 vs 9281). The off-field
pin (added EXP-DIST-38 for genuine ball-exits / restart spots) was pulling the track toward field
boundaries during NORMAL play → wrong picks + misses + off-frame viewport centers (the symptom that blew
up on the BU14/Chili out-of-distribution game: plan centers ran to x=-433/7960, off-frame).

**Adopted oob_w=0, then REVERTED — I'd measured the wrong stage.** The `oob_eval.py` above scored
the RAW rerank-selected pick. But the product is the PLANNED VIEWPORT (rerank -> bridge_aerial_gaps ->
kalman -> upsample -> plan_camera). Scoring that vs GT (`adjudicate.py`, R15m, on the SHIPPED pipeline)
FLIPS the result — oob_w=2 wins on BOTH held-out games:

| planned-viewport recall vs GT | oob_w=2 | oob_w=0 |
|---|---|---|
| Spencerport all / far | **0.707 / 0.682** | 0.654 / 0.615 |
| Irondequoit all / far | **0.714 / 0.562** | 0.689 / 0.523 |

So `oob_w` reverted to **2.0** (the EXP-DIST-38 default stands). The oob pin's boundary "misses" are
GOOD — the bridge/kalman coast through them onto the ball; oob0's fewer-but-wronger raw picks smooth into
worse viewport centers. **METHODOLOGY LESSON (load-bearing): evaluate configs on the PLANNED VIEWPORT vs
human GT (`adjudicate.py`), NOT raw-selected recall (`oob_eval.py`) and NOT AutoCam-agreement
(`sweep_viewport.py`).** AutoCam-agreement is an actively bad objective — it pulls toward AutoCam's
mediocre recall.

**BUT the adjudication surfaced the headline win: we ALREADY BEAT AutoCam on both held-out games.**
Per-GT-frame, ball-in-view @ R15m (shipped oob_w=2): Spencerport OURS **0.71** vs AutoCam **0.44**
(net +362 frames; far 0.68 vs 0.46); Iron OURS **0.71** far 0.56 (AutoCam crashed 06-15 -> no aim).
The selector (v7) + tracker is well ahead of AutoCam's own ball target on the metric that matters.
CAVEAT: the human GT skews toward HARD/far frames (some divergence-mined on AutoCam failures), so 0.71
vs 0.44 is the head-to-head ON THE EVAL SET, not the uniform whole-game rate — but it IS a fair
same-frames comparison, and we're clearly ahead where it's hard.
**Data:** `adjudicate.py`, `adj_iron.py` (planned viewport vs GT); `oob_eval.py` (raw-selected, the
misleading proxy); `sweep_viewport.py` (AutoCam-agreement).

## EXP-DIST-47: "detector misses near-aerial ball at 3:08-3:10" = near-range PERSON distractors outrank the in-candidate ball (2026-07-13)

**Trigger:** Mark's review of `spc_hn4_clip` (hn4+v7, Spencerport 05.31): "misses near-range balls in the
air directly in front of the camera — e.g. ~3:08-3:10 the ball crosses L->R and the detector doesn't
fire at all."

**Method:** mapped clip 3:08-3:10 -> global frames 7152-7232 (fps 20, camera-path g_start 3392); rendered
the detector's DEWARPED+MASKED band input (what hn4 actually sees) with all 24 candidates overlaid
(red=#1, orange=rank 1-4, green=5+), frame-by-frame (`frames_308.py`/`probe_308.py`). No human GT in this
window -> adjudicated by vision.

**Finding — NOT a recall miss; a RANKING/precision miss.** On every inspected frame the ball WAS in the
candidate set (rank ~2-4). The detector's #1 score repeatedly went to NEAR-RANGE false positives directly
in front of the camera: the COACH standing at the near touchline (his head = a big, confident, ball-like
blob — g7192 #1 pick (3047,1399) sits on him at score 0.82) + near-grass/shadow peaks
((7248,1167),(5822,1424),(3047,1399)) + recurring STATIC right-sideline distractors ((6303,68x),
(5514,362)). The ball tracks as #1 during the center-left dribble (g7152-7168), then drops to orange once
it's played L->R and a near-person peak outscores it.

**Why it reads as "camera loses the ball":** the viewport is pulled toward the confident near peak
(coach), not the ball.

**CORRECTION (2026-07-13, later — grounded in `static_diag.py` over the full Spencerport game):** an
earlier draft here claimed `static_persistence` (`static_w=2.0`) REWARDS a stationary candidate. That is
BACKWARDS — the emission is `+ static_w * pers` (a COST the Viterbi minimizes), so persistent candidates
are PENALIZED (`pers` = fraction of frames a candidate's ~2m WORLD cell is occupied, computed over the
whole game). What the occupancy map actually shows:
- It DOES catch genuinely-fixed distractors: goals/corners/far-sideline cells hit occ **0.6-0.94** (e.g.
  the recurring `(6303,686)`, `(1612,467)` peaks) -> penalized ~1.9 nats. Mark's "don't reward static"
  is already implemented for these.
- It does NOT catch the 3:08 coach: `(3047,1399)` occ = **0.012 at 2m, 0.007 in image-space** — the
  detector only false-fires on him ~1% of frames, so he is a TRANSIENT false peak, not a persistent
  object; persistence can't apply. (Coarsening to 10m makes it worse: coach 0.11 vs GT-ball regional occ
  median 0.22 / mean 0.33 — the ball legitimately dwells in busy regions/goalmouths, occ up to 0.94, so
  a hard high-occupancy veto would kill the ball AT GOALS.)
- So the coach needs the DETECTOR to stop firing on him (mining) + the size prior — NOT persistence.
- Real persistence improvement (Mark's idea, refined): a LOCATION-AWARE penalty — penalise persistent
  candidates at the SIDELINE / technical-area (where the ball never dwells) hard, stay gentle at
  goalmouths (where the ball scores). Testable on cached dumps (no retrain).

**Levers (all aimed at this exact mode):**
1. **Hard-negative mining (hn4/hn5)** — near-range person/shadow peaks are confident IN-FIELD distractors
   = the mining target; hn5's 782 corroboration negs include them. Direct fix: lower their score -> ball
   returns to #1.
2. **`size_px` prior is DORMANT** — a near coach-head peak is far larger than the perspective-expected
   near-ball diameter; the size-vs-expected penalty (`sweep_tracker._prior_size`) exists but `size_px` is
   never populated (candidates carry `None`). Wiring `size_px` through `extract_peaks` (plan Phase 3)
   would cheaply kill oversized near peaks.
3. **Selector location-aware persistence** — `static_persistence` already penalises persistent FIXED
   distractors (goals/corners/sidelines, occ 0.6-0.94) but (a) the near-range coach isn't persistently
   detected so it can't help him, and (b) it can't be a hard veto because the ball visits busy goalmouths
   (occ up to 0.94). Refinement: weight the persistence penalty by how rarely the BALL dwells at that
   location (hard on sideline/technical-area, gentle at goalmouths). See CORRECTION above.

**Caveat:** can't rule out true recall misses on the fastest airborne frames without GT labels there; but
on all inspected frames the ball was in candidates. **Data:** `frames_308.py`, `probe_308.py`,
`band_g7192.png` (sent to Mark). Cross-ref EXP-DIST-45 (distractor bucket), EXP-DIST-46 (hn4/hn5 mining).
**Next:** re-check this window with hn5 candidates on CHAIN DONE; wire `size_px` and re-eval selection.

### Size-lever prototype — the arc (2026-07-13, Mark-driven)

Prototyped the size idea on the cached flag (0:33 AR) + coach (3:08) windows. Four corrections, each
from Mark, each verified with numbers — the PRINCIPLE is right, the MEASUREMENT is the bottleneck:

1. **`size_px` from the detector heatmap is useless** (my first attempt). The heatmap is a trained
   fixed-σ gaussian, so its blob is ~constant ~13px for *everything* it fires on; `blob/expected` just
   penalised FAR candidates (tiny expected) and ignored the near distractors. Wrong signal.
2. **Measure the ACTUAL object blob in the dewarped image, not the heatmap** (Mark). Corrected
   `object_size()` (foreground-vs-grass connected component). Then the AR reads 60-82px and the coach a
   clean **210-225px** vs a ball's ~13-16px — size IS informative.
3. **The `size/expected_GROUND` ratio is aerial-unsafe** (Mark): a near AIRBORNE ball projects high in
   frame → ground homography says "far" → tiny expected → big ratio → a real aerial ball would be
   rejected (the exact far balls we want to win). So the gate must be **absolute size + shape**, not
   relative-to-position.
4. **Wire size CONTINUITY** (Mark): a ball's apparent size can only change as fast as perspective
   allows, so penalise size JUMPS — catches a small-ball→big-person handoff while ALLOWING an aerial
   ball's smooth growth (aerial-safe by construction). Added `RerankConfig.size_cont_w` (default 0;
   transition penalty on the deviation of the frame-to-frame size ratio from the expected-diameter
   ratio; smoothed per-candidate sizes to fight measurement noise).

**Results (replay on cached windows, v7 selector + champion config):**
- **Coach (in-field, clean measurement):** `size_cont_w=8` correctly REJECTS the jump to a 224px person
  (selected→None, 11/32 frames changed) while leaving size-continuous tracks alone. **Mechanism
  validated.**
- **Flag (AR at the field-edge corner):** ~no effect (1/39). Two reasons: (a) the AR reacquires through
  the MISS state and `size_cont_w` only covers direct candidate→candidate transitions (not
  miss→reacquisition); (b) the AR's object size is UNreliable at the mask edge (reads 59→14→86 even
  smoothed — at some frames it measures ball-sized).

**Conclusion:** the size-continuity mechanism works where the size is measurable; the blocker is a
**reliable image size-measurement at field edges** (foreground thresholding fails there). Real fix =
a person/object detector for size (plan Phase 4 person head), which is also the aerial-safe
"is-this-a-person" signal. Plus `size_cont_w` needs extending through miss→reacquisition (carry pre-miss
size in the miss state) and a held-out aerial-recall check before adoption. `size_cont_w` committed
default-OFF (dormant until `Candidate.size_px` is populated). The size-free alternative for the flag
remains the ANCHOR approach. **Data:** `redetect_size.py`, `replay_size.py`, `replay_coach.py`,
`resize_windows.pkl`/`resize_coach.pkl`.

## EXP-DIST-46: detector hard-neg retrain — hn4 (GT-guard) BEATS hn2 on held-out; the poison-bug arc + corroboration negatives; hn5 in progress (2026-07-13)

**The poison bug (caught by Mark).** `mine_hard_negatives` originally kept peaks FAR from AutoCam's
ball as negatives. On frames where AutoCam is WRONG (its own failure frames — exactly the frames the
far-label sets were mined from), the true ball sits far from AutoCam's pick, so the miner cropped the
REAL BALL as a "negative." Mark caught real game balls in the mined set. Poison: a from-scratch train
on that store teaches the detector to SUPPRESS the ball.

**Two guards added (both verified before any training).**
- `--use-gt` (GT-guard): mine only on HUMAN-GT frames, using the true ball as the exclusion centre.
  Verified min 83px from GT, 0 violations, 480 clean crops; vision-gated.
- `--corroboration-dir` (scalable, poison-free): mine on frames where AutoCam AND our v7 selector
  INDEPENDENTLY agree on the ball (≤50px, `build_corrob_labels.py`) — 2 sources ≈ GT with no human
  label. Far more safe frames than human GT alone (Cleveland 2124 agreement frames vs 54 human-GT),
  15 training games. Held-out Spencerport/Irondequoit excluded.

**hn4 = from-scratch on `crops_reolink` (old teacher-mined `hardmine` negs REMOVED) + 480 GT-guarded
negs.** (Fine-tune rejected — hn3/hn3b regressed, EXP-DIST-27: hard negs belong in a FULL retrain, not
a warm-start.) 40 epochs, early-stopped ep9, best=ep5. Val-CROP recall 0.386(ep5)→0.368(ep9),
fp_on_bg 333→127 — this LOOKED like over-suppression.

**But held-out says hn4 BEATS hn2.** Candidate ceiling + score-argmax on the held-out Spencerport 05.31
clip (frames 3392–9388, 134 GT: 115 far / 19 near), R15m, via `ceiling_hn4hn2.py`:

| metric (R15m) | hn4 (best=ep5) | hn2 |
|---|---|---|
| CEILING all | **0.970** | 0.955 |
| CEILING far | **0.965** | 0.948 |
| CEILING near | 1.000 | 1.000 |
| ARGMAX all | **0.418** | 0.351 |
| ARGMAX far | **0.339** | 0.296 |
| ARGMAX near | **0.895** | 0.684 |
| RANK far absent | 0.03 | 0.05 |

Hard-neg training did exactly what it should: distractors score lower → the true ball is the top peak
more often (argmax up in EVERY band) AND the ceiling ROSE (ball still in candidates). **Lesson:
val-CROP recall ≠ held-out detector quality — judge detectors on held-out ceiling/argmax, not the
crop-val recall curve.** (An overnight "hn4 is a wash" read from the recall curve was WRONG.) Caveat:
1 clip / 1 game / mostly-far — needs the full both-games eval (Irondequoit too) to confirm.

**Viewport check:** plan-scored hn4+v7 = 0.716 human-tier ball-in-fixed-viewport, 3 loss-windows; clip
rendered + sent to Mark. (Render/plan consume the selector `.pt`, NOT the `.npz` — the `.npz` is a numpy
archive and `torch.load` chokes on it: `file in archive is not in a subdirectory: schema.npy`.)

**hn5 (in progress).** Corroboration-mine over 15 games (sample-stride 10, max-per-frame 2,
min-ball-dist 80px) yielded 782 crops (~52/game) — MODEST, because agreement frames are the EASY frames
with few minable distractors; the value is poison-free + venue-diverse (15 games), not raw count. hn5 =
clean positives + 782 corroboration negs, from-scratch, 20 epochs (best.pt on val-recall improvement).
Training done 14:46, best.pt=epoch 13 (val-crop recall 0.389, marginally above hn4's 0.386).

**hn5 RESULT — REGRESSION, DO NOT ADOPT.** 3-way held-out ceiling/argmax on the Spencerport clip
(3392-9388, R15m):

| metric | hn5 | hn4 | hn2 |
|---|---|---|---|
| CEILING far | 0.939 | **0.965** | 0.948 |
| ARGMAX all | 0.254 | **0.418** | 0.351 |
| ARGMAX far | 0.148 | **0.339** | 0.296 |
| RANK far absent | 0.06 | 0.03 | 0.05 |

hn5 is the WORST of the three — ceiling BELOW baseline hn2, and argmax-far (0.148) roughly HALF of hn2's
(0.296). The 782 corroboration negatives OVER-SUPPRESSED, especially far balls — the opposite of the
GT-guarded hn4. Note hn5's val-crop recall (0.389) was the HIGHEST of the family yet it's the worst
held-out detector — a second, stronger confirmation that val-crop recall does NOT predict held-out
(cf. hn4, where the reverse held). Likely cause: corroboration frames are the EASY (agreed) frames, so
their mined "distractors" sit near ball-like far detections and teach the net to suppress them.
**Verdict:** hn4 (GT-guarded) remains the best detector; the corroboration-negatives approach failed.
Adopt hn4 over hn2 pending an Iron-clip confirmation; drop hn5.

**Takeaway:** the SELECTOR (v7, EXP-DIST-44) is still the biggest win, but the detector B-track is NOT
dead — GT-guarded hard-neg mining gives a real (small) held-out gain (hn4 > hn2). hn5 tests whether
scaling clean, venue-diverse negatives helps further. **Data:** `ceiling_hn4hn2.py`,
`build_corrob_labels.py`, `mine_hard_negatives.py --use-gt/--corroboration-dir`. Cross-ref EXP-DIST-27
(fine-tune regression), EXP-DIST-44 (v7 selector), EXP-DIST-45 (3-cause detector diagnosis).

## EXP-DIST-45: cross-game detector difficulty — the hard games are 3 DISTINCT causes, not "lighting" (2026-07-12)

**Method:** `crossgame_diag.py` — per game with a candidate dump, our-detector-finds-AutoCam-ball rate
(any of our top-24 within 100px of AutoCam's top detection = our ceiling proxy) + AutoCam median
confidence (AutoCam emits a detection on ~100% of frames, so COVERAGE is uninformative — only its
CONFIDENCE discriminates). Then VISION-inspected a sample frame from the 4 hardest.

**Ranking (our_finds_AC / AC_conf):** worst Lakefront-Sullivan 06.06 (0.633/0.464), Cleveland 05.09
(0.688/0.381), Pittsford 06.01 (0.703/0.439), West Seneca 05.31 (0.706/0.501), Cuyahoga 03.21
(0.722/**0.298** — lowest conf of any game), Fairport 05.30 (0.753/0.385); easiest Pittsford 05.07
(0.902/0.714). Held-out Iron 06.15 is ABSENT (AutoCam crashed 06-15 → no detections) but is the true
worst by GT (ceiling 0.35).

**Visual diagnosis — low confidence is THREE distinct causes, and only ONE is lighting:**
1. **Background distractors / far play (biggest bucket, most games):** Lakefront-Sullivan + Fairport
   05.30 are multi-field TOURNAMENT complexes shot with play at the FAR touchline — the frame contains
   OTHER games' soccer balls, parked cars, tents, crowds right where the tiny far ball is; Cleveland
   adds a painted midfield logo + mottled turf. → hard-negative mining (adjacent balls, cars, logos,
   line intersections), the classic B-track.
2. **Out-of-distribution surface:** Cuyahoga 03.21 = indoor dome, deep-blue turf, flat artificial
   light, extreme fisheye → grass-trained detector is off-domain (drives AutoCam's 0.298 too). Indoor
   is IN SCOPE (DECISIONS 2026-07-12) → needs turf/dome labels in DETECTOR training.
3. **Backlight:** Iron 06.15 golden-hour (the only pure lighting case) → lighting robustness /
   pre-detection tone normalization.

**Takeaway:** CORRECTS the earlier "find the rough-lighting games" framing — lighting is 1 of 3, and
the dominant, most-fixable cause is DISTRACTOR-heavy tournament venues (hard-neg mining), not sun. All
three are DETECTOR-track (raise the candidate ceiling), orthogonal to the SELECTOR track (v7/mid gold),
and confirmed by Mark against his venue memory. **Data:** `crossgame_diag.py`; frames inspected
2026-07-12. **NEXT (detector roadmap, ranked by leverage):** (a) hard-neg mining on adjacent-field
balls + clutter; (b) indoor/turf labels + training; (c) backlight tone-normalization / golden-hour data.

## EXP-DIST-44: v7 selector retrain (+226 mid, +63 near gold) — beats v5 on EVERY band, PROMOTE (2026-07-12)

**Trigger:** EXP-DIST-43 showed v6 lifted near/mid but traded ~5pts Spencerport far, and mid was the
weakest + only un-labeled band. Mark labeled 5 mid venues (240 frames) + more near. v7 retrains on the
consolidated gold (`overnight_selector_v7.py`: +226 mid / +63 near into ball_labels.jsonl ->
build_selector_labels x15 -> kill_test_selector -> export; held-out excluded; far gold at default weight).

**Result — per-band GT-in-view (`band_diag.py`, R=100px viewport containment), v5 / v6 / v7:**

SPENCERPORT 05.31 (n=1351: near 47 / mid 108 / far 1196)
| band | v5 | v6 | v7 |
|---|---|---|---|
| near | 0.702 | 0.787 | 0.723 |
| mid  | 0.731 | 0.852 | **0.870** |
| far  | 0.844 | 0.795 | **0.868** |
| ALL  | 0.830 | 0.799 | **0.863** |

IRONDEQUOIT 06.15 (n=576: near 166 / mid 99 / far 311; DETECTOR-limited, ceiling ~0.35, golden-hour)
| band | v5 | v6 | v7 |
|---|---|---|---|
| near | 0.578 | 0.681 | 0.627 |
| mid  | 0.747 | 0.869 | **0.869** |
| far  | 0.701 | 0.772 | **0.823** |
| ALL  | 0.674 | 0.762 | **0.774** |

**v7 beats v5 (current champion) on EVERY band on BOTH games** — near +.02/+.05, mid +.14/+.12,
far +.02/+.12, ALL +.033/+.100. The mid gold did what the band analysis predicted: MID 0.73->0.87
(Spc) / 0.75->0.87 (Iron). And v7 RECOVERED v6's far trade (Spencerport far 0.795 v6 -> 0.868 v7,
now ABOVE v5's 0.844). vs v6: v7 keeps mid, fixes far, best ALL; the only give-back is NEAR (v7
0.723/0.627 < v6 0.787/0.681, but still > v5) — v6 over-fit near, v7 balances. Irondequoit ALL rose
0.674->0.774 (+.10) despite an unchanged detector ceiling (same dump) — the selector coasts/selects
better through the golden-hour gaps but cannot exceed the 0.35 ceiling (DETECTOR-domain, GAMES.md).

**DECISION: PROMOTE v7** (strictly dominates v5 across all bands/games; mid gold transfers cleanly).
**NEXT:** (a) deploy v7 into the ball_select step (copy to models/, register, re-run EXP-DIST-42
aim-matching on v7); (b) human render-watch on a held-out clip through v7; (c) optional v8 to recover
near vs v6 by up-weighting near gold; (d) Irondequoit-type venues need the DETECTOR/lighting track,
not more selection gold. **Data:** `band_diag.py`, `selector_v7.npz` (parity 3.87e-07).

## EXP-DIST-43: v6 selector retrain — band-decomposed; near/mid WIN, far trade masked by far-heavy GT (2026-07-12)

**Context (+ a process failure to record):** `selector_v6` (`kill_test_selector` on the 15-game
adjudication gold, incl. EXP-DIST-41's ~500 near/close-ball labels; built 07-10 22:50 from
`ball_labels` consolidated 07-10 22:37, so it DID ingest the recent labels) was trained but never
written up. As a result an overnight aim-matching pass (EXP-DIST-42) ran on v5, and v6 was first
mis-read as a REGRESSION from the Spencerport aggregate alone (0.830→0.799). Decomposing by depth band
on BOTH held-out games flips that read. Lesson: a trained model with no EXPERIMENTS entry is invisible
and will be re-litigated — write up every retrain.

**Method:** `band_diag.py` — champion tracker/planner, GT-in-viewport per depth band
(far d01<0.34 / mid 0.34–0.67 / near >0.67) + detector ceiling (nearest candidate ≤100px) +
selected-on-ball, selector v5 vs v6, on held-out Spencerport 05.31 and Irondequoit 06.15.

**Result — GT-in-viewport (v5 → v6):**

| band | Spencerport (n) | Irondequoit (n) |
|---|---|---|
| near | 0.702→0.787 (47) | 0.578→0.681 (166) |
| mid  | 0.731→0.852 (108) | 0.747→0.869 (99) |
| far  | 0.844→0.795 (1196) | 0.701→0.772 (311) |
| ALL  | 0.830→0.799 | 0.674→0.762 |

v6 improves NEAR and MID on BOTH games (mid +.12 each) and is a net win on Irondequoit (+.088). The
Spencerport aggregate dips ONLY because its GT is 88% far (1196/1351) and v6 traded ~5 pts of far —
the band where v5 was already strongest — for the near/mid gains. v6 is a near/mid WIN with a far
trade, not a regression.

**Detector ceiling = the other, venue-dependent wall:** Spencerport ceiling@100 ~0.68–0.77 (ball
usually IS a candidate → SELECTION-limited); Irondequoit ceiling@100 near 0.277 / mid 0.424 / far
0.379 (ball rarely a candidate → DETECTOR-limited). Selection labels lift selection-limited venues;
detector-limited venues need detector work (hard-neg / ROI detection), not selection gold.

**Conclusion / NEXT:** (1) v6's near/mid gains are real and label-driven → promote v6 or retrain v7,
but WEIGHT THE FAR GOLD so Spencerport far doesn't regress. (2) **MID is the highest-value next label
set** — weakest band (v5 0.73–0.75), the ONLY band with no dedicated GT, already +.12 from adjacent
near labels; Mark to label mid-range. (3) The bottleneck is per-venue: Spencerport=selection,
Irondequoit=detector. (4) EXP-DIST-42's aim-matching numbers were on v5; re-run on the promoted
selector once chosen. **Data:** `band_diag.py`, `v5v6.py` (server); `selector_v6.npz`.

## EXP-DIST-42: homegrown viewport vs AutoCam ball-target — aim-matching validation (2026-07-11)

**Trigger (Mark):** stop leaning on sparse human GT — the real question is whether OUR viewport
looks the same DIRECTION as AutoCam's per-frame ball target (captures the action). Validate against
AutoCam's dense coordinates; human GT overrides where it exists.

**The reference (fresh aim, not the broken legacy file):** the per-game `autocam_viewport.jsonl`
is corrupt (near-constant center, corr 0.28 vs GT). Instead generated a FRESH per-frame external
ball-target track for held-out Spencerport 05.31, output-video discarded (render recipe kept in
`F:\archive\OnceAutocam`, per the no-external-refs-in-repo rule): 80,464 per-frame `{xy}` targets
in source px (7680×2160). Validation: **aim↔GT corr 0.773** (offset 5) — the fresh target genuinely
tracks the ball, unlike the legacy viewport file. **Preserved to F: next to the video**
(`autocam_aim.jsonl` + `autocam_ballboxes.txt` + provenance README), matching the
`autocam_detections.jsonl` sidecar convention. Idempotent.

**Harness (`may31_compare.py`):** reference per frame = human GT if labeled else AutoCam aim.
Metrics — RENDERING containment (reference ball inside our planned viewport rect); GT-only
ball-in-view (human override subset); RAW selection coverage + localization (FOV-independent
tracking quality: how often we select a REAL ball, not coasted, and how close it sits to the ball).

**Baseline (champion config, current code `55112a7`, full 80k-frame aim):**
RENDERING containment **0.689**, GT-in-view **0.830**, RAW selection coverage **0.421**
(up from old-code 05-27's ~0.32), localization **median 319px** (48% <300px, 62% <500px). So when
we select, the track sits on the ball; ~58% of stride-4 frames have no confident ball and coast.

**Sweep 1 — viewport FOV (raises containment but it's a framing knob, not a tracking gain):**
widening zoom + missing/deadball HFOV lifts BOTH containment and GT-in-view monotonically with NO
regression — `zoom 52/50/66` → 0.706/0.844; `zoom 56/52/72 + missing80/deadball70` → **0.729/0.854**.
RAW coverage + localization are unchanged (FOV doesn't touch selection), so the gain is just zooming
out to cover the same tracking error. `select_max_gap_frames` (coast length) has ZERO effect on
containment.

**Sweep 2 — miss_scale (coverage cannot be cheated; champion is the optimum):**
scaling the learned per-frame miss cost — 0.6 → cov 0.311, loc 265px, but containment/GT DROP to
0.655/0.780 (coasts too much); 1.5/2.5/4.0 → cov climbs 0.54/0.74/0.89 but localization DEGRADES
319→379→517→571px and containment/GT FALL (the forced "selections" are distractors dragging the
viewport off the ball). **miss_scale=1.0 (champion) is the sweet spot** — the selector's learned
`p_none` is well-calibrated; the honest 42% coverage is right, and coasting beats guessing.

**Divergence — WHO IS RIGHT when we and AutoCam disagree (Mark's key question):** on the 1351
May-31 frames that have BOTH human GT and an aim value — aim&GT-both-in-viewport 785 (58%);
**aim-OUT but GT-IN 336 (25%) — we diverge from AutoCam's aim yet sit on the TRUE ball**;
aim-in/GT-out 51 (4%); both-missed 179 (13%). So of the 515 frames where our viewport does NOT
contain AutoCam's aim, on **336 (65%) we are on the real ball** — the divergence is AutoCam being
off, not us. And the **aim itself agrees with GT only 0.693** of the time, while OUR viewport
contains the true ball 0.830 — i.e. we are closer to truth than AutoCam's own target is. The 0.689
aim-containment therefore UNDERSTATES quality: chasing aim past ~0.69 would pull us OFF the real ball.
(Caveat: this GT subset was mined at hard/AutoCam-failure points, so it over-samples divergent frames;
on easy frames we and the aim agree — that is the 58% aim&GT-both-in bucket.)

**Conclusion:** on the dense AutoCam-aim reference the homegrown viewport already "looks the right
direction" ~69% of all frames / 83% of human-GT frames. But the divergence breakdown shows the aim
is NOT ground truth (0.69 GT-agreement on hard frames), and where we diverge from it we are on the
real ball 65% of the time — so the target is human GT, not aim-parity, and we already exceed the
aim's own truth-agreement. The current selector/rerank config is validated as the tracking-accuracy
optimum (both directions of the miss-cost lever are worse). The only clean containment lever is FOV
width, a product/framing choice with a zoom-out cost — NOT a tracking improvement. Real headroom is
in selection coverage (the 58% coast) and the both-missed 13%, which need better detections/selector
(EXP-DIST-42-retrain), not planner tuning.

**Second game — 05-27 Chili Vortex (held-out, FULL game 16/16 dump chunks, active-play frames
11196-113968, n=102773; GT n=637):** the 05-27 aim was recovered from the archived `autocam.jsonl`
stdout capture (118,227 `{xy}`), preserved to F:, aligned to our global frames at offset -1153
(corr **0.880** — a ~1153-frame trim, fps ~20 matches). Current-code champion (v5, full game):
RENDERING containment **0.711**, RAW coverage **0.665**, localization 325px, GT-in-view **0.694**;
DIVERGENCE aim&GT-in 408, aim-OUT&GT-IN 34 (we right), aim-in&GT-out 67, both-out 128; **aim agrees
GT 0.843**. The 10/16-chunk partial (0.707 / 0.685 / 0.701, divergence 30 vs 39) predicted the full
game within ~1 pt — the early easy-window number (0.80/0.85) did NOT; always run to full or a
representative fraction.

**HONEST two-game read (do not overclaim "we beat AutoCam"):** vs our OWN old-code 05-27 baseline
(rendering 0.598 / coverage 0.32) the new code is a big jump on both games (coverage ~2x). vs AUTOCAM
it is SPLIT: on May-31 our viewport is closer to the true ball than AutoCam's aim (GT-in 0.830 vs aim's
0.693 GT-agreement; divergence 336 vs 51 in our favor); on 05-27 AutoCam's aim is CLOSER to the true
ball than us (0.843 vs our 0.694; divergence 34 vs 67 against us). We win where AutoCam struggles
(far/hard frames, May-31) and trail where its detector is solid (05-27; both-out 128/637 ~20%, and its
aim is genuinely good at 0.84). The gap is detector COVERAGE on hard far balls, not planner/selection
tuning — consistent with EXP-DIST-43's finding that 05-27-type venues are detector-limited (low ceiling).

**Data:** F: aim sidecars (05.31 autocam_aim.jsonl+ballboxes; 05.27 autocam_aim.jsonl); harness
`G:\ballresearch\selector\may31_compare.py` (env: GAME_DIR/AIM_PATH/DUMP_DIR/OFF_RANGE, CFG=json).
**NEXT:** retrain selector/detector to attack the both-missed + coast (the real headroom, not planner
tuning); for the "aim for ALL games"
batch (#35), a per-game coordinate-validation gate is REQUIRED — the `-once-processed *.mp4.jsonl`
sidecars are heterogeneous (Dahua ~4096-px source; at least one, 05.18 Buffalo Empire, is crop-space
x[66,1213]) so blind extraction would write corrupt aims; validate each sidecar's range vs game.json
source dims + on-ball corr vs that game's `autocam_detections.jsonl` before writing `autocam_aim.jsonl`.

## EXP-DIST-41: position-based CLOSE-ball GT — 500 human labels across 14 venues (2026-07-10)

**Trigger (Mark):** the aerial ball-loss at Spencerport 0:48 is a NEAR-detection failure — the
detector drops big close balls (out of its far-trained scale). Fix = more near GT, "based on
POSITION relative to the field outline, not size."

**Miner (`build_far_label_queue --criteria near`, rewritten):** the geometric ball-size estimate is
useless (an airborne ball reads 7.6px; the metric maxes ~23px game-wide and doesn't track visual
size — the dump stores no actual blob size). Replaced with POSITION: gate the teacher-snap ball by
depth to the near touchline (`--near-depth 0.55`, 1=touchline) and rank by image-y (frontmost=biggest,
where the touchline meets the frame edge). Teacher-backed only (a no-teacher near candidate is a near
PLAYER, not a ball). `--near-span` (default 1) — spans were tried but read as duplicates (near-identical
consecutive frames, worsened by an out-of-order manifest bug, now fixed: frames always emitted in
ascending frame_idx). A separate `--criteria kick` (fast in-field exit + gap, wide bracketed window)
is wired for the pure-aerial arcs but needs the full selector track.

**Result:** Mark labeled **500 close balls + 15 not_visible + 6 obscured + 2 out_of_play across 14
training venues** (12 sets 100%), ~207 of them `near_misrank` (the detector demoted the ball below a
distractor — the exact failure). "I love those, they are so easy." Held-out Spencerport/Iron-06.15
excluded (validation). NEXT (EXP-DIST-42): retrain selector/detector with this gold and measure the
held-out NEAR-band metric — does it move? Baseline near SELECTED is the open problem (EXP-DIST-19/20/29).

## EXP-DIST-40: aerial look-ahead interpolation bridge — NEGATIVE (no trustworthy landing) (2026-07-10)

**Case (Spencerport ~0:48, verified on the YouTube source + decoded frames):** a keeper
punts the ball high and long (airborne 0:48 -> >0:51, players looking up, ball leaves the
top of frame). The detector can't see the airborne ball; its ground candidates are the ball's
SHADOW (0.77), players, coaches, and a real spare ball on the ADJACENT field (0.845). The ball
IS in the candidate set only when on the ground; while airborne the track goes to MISS, then
re-acquires a coach's cap on the left (0.84). Root cause = both a detection hole (no airborne
ball) AND a world-model break (an aerial ball projected to the ground plane teleports).

**Fix attempted (Mark's idea):** OFFLINE look-ahead — bridge the flight by interpolating the
viewport from the launch point to the next STABLE in-field re-acquisition found downstream
(`bridge_aerial_gaps`, config `aerial_*`). Swept gate (consecutive-miss count, exit-speed) x
mode (hold/interp) x stability-K on both held-out games.

**Result — NEGATIVE, default OFF:**
- Ungated (any >=6-frame gap): fires on 500+ fragmented misses -> Spencerport SET-A 76->116,
  ball-in-view .830->.791. Helps Irondequoit (.616->.66) only because it replaces the Kalman
  coast on many gaps, not because it finds real landings.
- Gated to real launches (exit>=1.0 m/f, >=4 misses): does NOT fire on the 0:48 punt at all
  (short, slow-exit gap indistinguishable from an occlusion) yet still nets Spencerport SET-A
  76->84; metric fragile to the look-ahead cap (200 vs 240 frames swings SET-A 84<->72).
- The interpolation TARGET is the problem: for a long punt the only "stable" post-gap track is
  a distractor (coach's cap), and the pre-launch DRIBBLE velocity points opposite the punt.
  There is nothing trustworthy to interpolate to.

**Takeaway (Mark 2026-07-10, "we lose the ball once it leaves the ground"):** the real fix is
to STOP losing it — DETECT the ball in flight, or its ground SHADOW (which stays on the field
plane, so the homography/world-model works on it directly, and Mark spotted it in-frame). That
is a detection-level change, not a tracker post-process. `bridge_aerial_gaps` kept behind the
default-off `aerial_bridge_lookahead` flag for when a trustworthy landing signal exists
(player-convergence / shadow track).

## EXP-DIST-39: re-acquisition distance bias — hold near the loss point; SET-A far-swings-we-lose 52->36 (2026-07-10)

**Trigger (Mark):** the goal is the VIEWPORT in basically the right direction, not perfect detection
— a near distractor is fine (ball comes back to where the camera points); a FAR swing to the wrong
area (far-side crowd) is the real failure. 3-way partition (ours/AutoCam/GT): SET A = far swing where
we disagree with AutoCam (93 frames, 52 of them AutoCam-is-right = we lose); SET B = we agree with
AutoCam but ball elsewhere (85, both wrong).

**Lever:** a ball lost IN-FIELD (not OOB, not aerial) reappears NEAR where it was lost, so re-acquiring
FAR from the miss state is almost always a distant distractor. Penalise re-acquisition distance beyond
`reacq_free_m` so the track HOLDS near the loss point (viewport stays in the right area) and the ball
comes back to it. New `reacq_dist_w` / `reacq_free_m`, applied only to non-OOB/non-aerial miss->cand
transitions.

**Sweep + cross-validation (full-game, scored vs human GT + AutoCam):**

| | ball-in-view | right-dir | SET_A | SET_A AutoCam-right (we lose) |
|---|---|---|---|---|
| Spencerport baseline | .799 | .919 | 93 | **52** |
| Spencerport reacq 0.07/free6 | **.830** | **.932** | **76** | **36** |
| Irondequoit baseline | .614 | .827 | (no AC data) | - |
| Irondequoit reacq 0.07/free6 | .616 | .824 | - | - |

**Result:** Spencerport ball-in-view +.031, right-dir +.013, far-swings-we-lose **-31% (52->36)**;
Irondequoit neutral (no regression). **Adopted as the default** (reacq_dist_w=0.07, reacq_free_m=6).
The champion config in plan_camera_path / ball_select inherits it. NOTE: this shifts the champion
baseline for future comparisons.

## EXP-DIST-38: blanket boundary margin HURTS (catches distractors); needs a state gate (2026-07-10)

**Trigger (Mark, reviewing clip 1):** the end-line/dome detection margin (EXP-nearby, boundary
+250 px) made the render WORSE — it catches off-field distractors (the crowd/sideline).

**A/B on the clip-1 window (g 3392-9392), same selector/config, scored ball-in-viewport vs HUMAN GT:**

| detection mask | ball-in-viewport (vs GT) | selected detections OFF-field |
|---|---|---|
| **field-only (no margin)** | **119/130 = .915** | 86/326 (26%) |
| boundary +250 margin | 78/130 = **.600** | 98/366 (27%) |

**Conclusion — Mark is right, decisively.** The blanket margin drops ball-in-viewport 31 points
(.915 -> .600): it exposes the far-side spectator row + sideline crowd, which win selection on the
MANY in-field frames far more often than the margin saves a behind-goal ball on the FEW OOB frames.
My earlier "confirmed" (EXP end-to-end on loss #1's window ALONE) was too narrow — a single OOB
window looked good; the whole clip regressed. **Blanket margin reverted; clip planning is field-only.**

**The right fix (Mark's design) — a STATE-GATED off-field search (the OOB ball-state, plan 3b):**
default IN_FIELD -> only in-field candidates eligible for selection (off-field ignored). When the
track exits the field near a boundary and finds no in-field ball -> OUT state -> off-field candidates
near the exit become eligible (catch the ball behind the goal / over the line). When an in-field
candidate is re-selected -> back to IN_FIELD (off-field ineligible again). Detection keeps the margin
(candidates must EXIST off-field); SELECTION gates them by state.

**BUILT + RESOLVED (2026-07-10, same day) — the margin degrades DETECTION, not selection; NO gate
fixes it.** A/B on the clip-1 window vs human GT:

| variant | ball-in-viewport | off-field selections |
|---|---|---|
| **field-only** | **.915** | 26% |
| margin +250, no gate | .600 | 27% |
| margin + emission gate (p3/5/8) | .638 | 21% |
| margin + edge-directional TRANSITION gate (p3/5/8) | .631 | 27% |

Both gate variants failed. The transition gate (free continuation / OOB-reentry near the single
pinned exit edge; penalise only fresh miss->offfield grabs) left off-field selection UNCHANGED at
27% — because the harmful crowd sits ~7 px behind the far touchline, reachable as an in-field->
off-field "crossing" indistinguishable from a real far-line ball. **Candidate recall settles it:
ball-in-candidates R5m field-only .731 vs margin .679** (R10m .940 vs .903). Expanding the band
REGRESSES detection — the detector was trained at far_margin=400 band height, so a taller band
shifts its heatmap AND off-field junk fills the top-24, evicting real-ball candidates BEFORE
selection runs. **DECISION: ship FIELD-ONLY (.915). `offfield_gate` stays in code (default off,
tested) but is NOT the behind-goal fix.** The real fix would be a SECOND, targeted detection pass
beyond the exit edge at the TRAINED scale, triggered by the OOB state — not band expansion. Parked:
behind-goal is a small fraction of losses, field-only is strong; revisit after more GT / if priority.

## EXP-DIST-37: Dahua VIEWPORT-to-viewport — AutoCam's viewport is good, OURS drifts (the real gap is ours) (2026-07-10)

**Trigger (Mark, the more important question):** forget the contaminated detector GT — does our
FINAL viewport match AutoCam's viewport on Dahua? Both are per-frame source-pixel camera centers,
verified 1:1 aligned (EXP-DIST-36); compare directly over active play. No ball GT needed.

**Result — NO, and the gap is ours.** Chili-Dahua, active play, n=77,513 overlapping frames:
our-native-vs-AutoCam-viewport center distance **median 41 m (765 px)**, only 8.3% within 15 m
(norm-7680 slightly worse: 49 m). Vision-adjudicated (both viewports drawn as boxes): AutoCam's
viewport is on the play in every sampled frame; OURS matches on ~1/3 (e.g. g=108074 both dead-on)
but on the rest DRIFTS onto the far-side crowd / tents (g=65093, 76720 — our box aimed at empty
grass/spectators while the play is elsewhere).

**Reconciliation of the Dahua picture (all three findings together):**
1. AutoCam's raw per-frame DETECTOR is noisy on Dahua (confident grass FPs, EXP-DIST-36) — BUT
2. AutoCam's final VIEWPORT is GOOD (its tracking/smoothing overcomes the detector noise, stays on
   the play) — so the AutoCam viewport is a VALID product-level reference even though its raw
   detections are not a valid GT.
3. OUR viewport is genuinely WORSE on Dahua: our detector candidates land on the far-side crowd/
   tents (the bright distractors), the tracker follows them, the viewport drifts. **This is a real
   product gap on Dahua, not a benchmark artifact.**

**Conclusion / action.** The Dahua deficit is ours and it's real at the viewport level. Fix path =
improve the Dahua detector/tracker, which needs trustworthy Dahua ball GT. **Queued
`chili_dahua_spans` far-label set** (200 active-play frames, 10 windows across both halves,
stride-8, hints from our dump not AutoCam; served at the far-label landing) — the first real Dahua
ball GT. Angular-norm stays parked (EXP-DIST-36). Dahua remains Reolink-minority
([[reolink_primary_dahua_artifacts]]); this quantifies the gap so the label investment is a
deliberate choice.

---

## EXP-DIST-36: Dahua investigation — angular-norm is a wash + the Dahua benchmark is MISALIGNED (2026-07-10)

**Trigger (Mark):** Dahua campath scores are poor (Chili planned-view .297, Spencerport-Dahua .416
vs Reolink ~.82). Two hypotheses on the table: (a) far-side balls are sub-training-scale on the
4096-wide Dahua pano, fixable by isotropic up-scaling ("angular normalization"); (b) it's venue
diversity, not scale (the distill finding). Experiments on Chili-Dahua, off cached dumps + a
16-frame sample.

**Result 1 — angular norm ≈ wash at the CANDIDATE level.** Decomposing the campath score into
detector-recall vs selection, on the tier-B GT frames: native 4096 candidate recall R5m **.179**
(R2m .072, GT-candidate median score-rank 11); norm-7680 R5m **.170** (R2m .081, median rank **7**).
Upscaling helps rank + tight-recall marginally but does NOT raise R5m — the ball is absent from the
candidate set either way ~82% of the time. Campath planned-view .297 -> .203 with norm (small
sample). **Angular normalization is not the Dahua fix** (supports hypothesis b).

**Result 2 [CORRECTED 2026-07-10 same day — my first read was wrong].** I initially reported the
Dahua benchmark as "temporally misaligned" from an offset sweep. **That was an error: noise-fitting a
0.17-recall signal.** Direct indexing check settles it — the AutoCam sidecars map 1:1 onto our
segments: seg NAMES match exactly (0 unmatched), and every segment's sidecar max frame index = our
game.json frame count - 1 (25214 vs 25215, ...), i.e. clean 0-index-vs-count. **No offset, no
per-segment drift.**

**The real finding — AutoCam's DETECTOR is itself unreliable on Dahua, so every AutoCam-derived GT is
contaminated.** Same-frame head-to-head (Mark's method: AutoCam's decrypted-model detections from the
marathon vs our candidate dump, verified-aligned global frames — no viewport, no benchmark):
- AutoCam emits ~15.8 detections/frame (mean conf 0.13 = mostly junk); 14,899 frames carry a
  high-conf(>0.5) detection, 37% of them OUTSIDE active play (guaranteed FPs — no ball in play).
- Vision-adjudicated: AutoCam's confident + temporally-consistent "ball" sits on EMPTY GRASS at conf
  **0.84 and 0.90**, even DURING active play (frames 61072, 77768) — the far-side Dahua ball is near-
  invisible, so AutoCam locks onto grass/texture artifacts. Our detector correctly did NOT fire there.
- So the low "recall/agreement" (candidate-set-contains-AutoCam-ball R5m .15) is partly US being
  RIGHT (not matching AutoCam's hallucinations), not a detector deficit. The tier-B viewport
  benchmark is built from these same AutoCam detections -> same contamination.

**Conclusion.** (1) The angular-norm wash (Result 1) stands — it's a RELATIVE native-vs-norm A/B
against the same GT, so "no improvement" holds even though the absolute numbers are contaminated.
(2) We CANNOT evaluate our detector's true Dahua quality with existing data: both AutoCam-derived GT
sources are unreliable because AutoCam itself fails on Dahua. (3) The ONLY path to a real Dahua
answer is a small HUMAN far-label set on Dahua active-play frames (same tool as the Reolink sets).
Dahua stays Reolink-minority ([[reolink_primary_dahua_artifacts]]); invest in Dahua labels only if
Dahua support becomes a priority. (Head-to-head + external-detector artifacts in
F:rchive\OnceAutocam per the no-RE-in-repo rule.)

## EXP-DIST-35b: restart-spot priors — end-line exits re-enter at the goal kick / corners (2026-07-09)

**Trigger:** Iron's OOB stretches are long retrievals past the END line (-300..-500 px) where the
crossing pin is too naive — the rules place the restart at the goal area or corner arc, not the
crossing. `_restart_spots`: end-line crossings (edges 4->5 / 9->0 of the 10-point outline) expand
the pin to {crossing, goal-kick spot (mid-end-line + 6 m infield), both corners}; re-entry scores
against the NEAREST spot. Touchline exits keep the throw-in crossing. Test: goal-kick re-entry far
beyond the crossing cone beats a mid-field distractor.

**Held-out A/B (champion v5 stack):**

| game | oob off | crossing pin only | pin + restart spots |
|---|---|---|---|
| Iron bench-h | .571 (10 w) | .587 (10 w) | **.595 (9 w)** — first Iron window fixed; near -.07 |
| Spc bench-h | .782 (23 w) | .839 (18 w) | .823 (18 w) |

**Conclusions:** spots are the rules-correct design and win on the game they target (Iron); they
give back ~.016 on Spc vs pin-only (extra spots occasionally bonus a wrong re-entry near corners).
Adopted (principled over per-game tuning — refine later with crossing-to-corner distance gating if
the render shows it). Iron's near-band regression under OOB remains the open thread — its 9
remaining windows are near-band losses where the pin competes with genuine near play.

## EXP-DIST-35: out-of-bounds state — exit-crossing pin + free boundary wait; Spc benchmark .782 -> .839 (2026-07-09)

**Trigger (Mark):** 23/31 adjudicated loss windows had the ball OUT OF BOUNDS — and the detector mask
has zero margin on near/side lines, so the ball is architecturally invisible there. His physics: the
ball leaves with velocity (extrapolate the crossing), MUST return (rules), and returns NEAR where it
left (throw-in at the crossing). Measured on held-out GT (n=4 measurable exits): re-entry within
7.4-11.5 m median (p90 15 m) of the extrapolated crossing after 7-20 s; re-acquisition latency at
re-entry ZERO frames in 12/12 measurable cases; exits seen 7/11.

**Implementation (`oob_w`):** at miss entry, if the exit ray crosses the polygon (or the exit hugs
the line), pin the expectation at the boundary crossing; waiting there is nearly free (miss-to-miss
trans 0.1 vs 0.6 — a throw-in wait is correct behavior, not a guilty miss); re-entry inside a
slow-growing cone (8 m base, 20 m cap — from the measurements) gets the cone bonus. Behavior test:
bright mid-field distractor during the dead time cannot steal the track.

**Held-out A/B (champion v5 + phys 5 + bridge 2 + static 2 + pnone):**

| game | oob=0 | oob=2 |
|---|---|---|
| Spc bench-human | .782 (23 windows) | **.839 (18 windows)**, near .672->.789, cov .58->.65 |
| Iron bench-human | .571 (10 windows) | .587, far .466->.500, near -.05, teleports 258->346 |

**Conclusions:** clear win on Spencerport (5 windows fixed outright, biggest single-change benchmark
jump of the project); mixed-positive on Irondequoit — its OOB stretches are long retrievals far past
the end line (measured -300..-500 px), where a crossing pin is too naive: needs the RESTART-SPOT
priors (goal-kick box / corner arc) from the plan's ball-state design, plus its 4 lost-before-exit
windows are aerial/fast-exit territory. oob_w=2 adopted into the champion config.

## EXP-DIST-34: LOGO variance — v5 is corpus-wide, no game carries it, no game harms it (2026-07-08)

**Method:** 15 leave-one-game-out retrains (full v5 recipe: 14 supervision sets each, size_ratio
knocked out), each evaluated on both held-out eval dumps (learned argmax + champion tracker replay,
pnone x1.0 / bridge 2.0).

**Results (across the 15 drops):**

| metric | mean ± std | min–max |
|---|---|---|
| Spc argmax FAR | .428 ± .010 | .412–.452 |
| Spc argmax NEAR | .372 ± .027 | .333–.422 |
| Spc tracker FAR | .496 ± .021 | .457–.540 |
| Iron argmax FAR | .606 ± .086 | .364–.727 |
| Iron tracker FAR | .586 ± .060 | .500–.700 |

**Conclusions:**
1. **Gate PASSED.** No drop collapses held-out performance; Spencerport far argmax varies by ±1%
   (std .010) across all 15 drops — the far signal is distributed over the whole corpus.
2. The visible variance sits exactly where GT is thinnest: Iron's eval dump has only 11 far / 16
   near GT frames, so one frame = ±.06–.09 — instrument granularity, not supervision fragility.
   (Mark's 270 new Iron labels fix this panel for future whole-game LOGO.)
3. Largest single-game effect: dropping lakefront0607 dents Iron far argmax (.364 vs .606 mean) —
   a 3-frame swing, noted but not overclaimed. No game is harmful; none is irreplaceable —
   consistent with the flattening v4→v5 gains: supervision scaling is near its plateau, remaining
   gap lives in the tracker/ball-state layer.

**Data:** `G:\ballresearch\selector\logo.log`.

## EXP-DIST-33: viewport instrument CALIBRATED — 9 human-adjudicated windows, bimodal and decisive (2026-07-06)

**Method:** contiguous stride-8 span sets on held-out Spencerport (`spans` criterion; eval-only, never
trains), windows mined from AutoCam's own telemetry, Mark labeling same-day (360 span labels total).
Metric = ball-in-viewport ELLIPSE (source px, half 1200x500 around the sidecar center) — raw far-band
meters are projection-noise-dominated and were retired after a vision check (EXP-DIST-32 note).

| window (how mined) | ball-in-viewport | reading |
|---|---|---|
| fast-pan excursion x2 (viewport speed >12x median) | 0.07 / 0.00 | LOSS (chasing) |
| "normal" pan speed (turned out far-side play) | 0.14 | LOSS (far blindness) |
| near-aimed, gentle pan x2 (viewport y high) | 0.00 / 0.00 | LOSS — **tracking the assistant referee** (vision-verified) |
| viewport follows own dets, static ball | 1.00 | success (trivial) |
| same, partial | 0.47 | partial tracking |
| **+ motion floor >=60 m det travel x2 (Mark's fix)** | **0.94 / 1.00** | success, DYNAMIC (med offset 228-635 px) |

**Conclusions:**
1. The metric is **bimodal**: >=0.9 when AutoCam demonstrably tracks (static or dynamic), <=0.15 in
   losses; a genuine partial reads mid. T3 window semantics: agree >= 0.9 both-tracking, <= 0.15 loss.
2. Sampling AutoCam's behavior on Spencerport found loss modes EVERYWHERE the telemetry looked
   interesting: fast-pan chases, far-side blindness, and a 16 s referee-follow. Its own VIEWPORT is far
   weaker than its detections (the 0.845 far bar is dets->OUR tracker) — the viewport-vs-viewport
   product comparison is a lower bar on this game than detection metrics implied.
3. Mining lesson (Mark): "viewport follows own detections" self-selects parked balls; add a detection
   PATH-LENGTH floor to find dynamic-tracking windows. The two found (250-270 m travel, 0.8-0.96
   self-consistency) adjudicated 0.94/1.00 — they double as must-match windows for our system.

**Data:** `spc_{eval,agreement,true_agree,dynamic_agree}_spans` label sets; consolidated into
Spencerport ball_labels.jsonl (eval-only). Integration run auto-fires on the Spencerport fullgame dump
(`selector_integration_run` task) with `replay_fullgame` + selector_v4.pt.

## EXP-DIST-32: physics-based transitions — full-game teleports 1328 → 18 at no accuracy cost (2026-07-06)

**Trigger (Mark):** "base it on physics — velocity + gravity predict where the ball comes down."
Built two layers (code tags: bridge v1 + 31b):
1. **Ballistic landing cone:** miss entry captures the exiting path's velocity (backpointer step,
   >= 0.8 m/f to trust, capped at flight speed); re-entry inside ``exit + v*airtime`` (cone widening
   0.4 m/f of airtime) gets 0.75*bridge_w bonus vs 0.5 for the direction-blind rate band. Unit test:
   two landings at identical rate — only the cone lands ahead.
2. **Physical transitions (`phys_sigma_px`):** full-game diagnosis showed the track NEVER missed —
   legacy budgets scale with the stride gap (25 m/f × 8 = 200 m) so candidate hops were nearly free
   (Chili: 0 miss entries, 1328 raw teleports; the aerial machinery never engaged; the eval-dump sweep
   was blind to all of this because its GT spans are too short to contain a flight). Physical mode
   prices hops with a REAL ball-speed ceiling (2.5 m/f) + depth-dependent measurement noise (px jitter
   through the local homography Jacobian — measured: ~0.02 m/px near, ~0.13 m/px at the far line, so
   far GROUND jitter is meters, not tens of meters; the huge far excursions are AIRBORNE projections,
   which now route through the miss/bridge state).

**Full-game A/B (Chili, hand-tuned emission, 587 human labels):**

| config | human-R15 | raw teleports | miss frames | fragments |
|---|---|---|---|---|
| legacy | .494 | 1328 | 0 | 161 |
| phys σ=5px | .479 | 22 | 5 | 158 |
| phys σ=5px + bridge 1.0 | .475 | 18 | 0 | 156 |

**Conclusions:**
1. Physics removes 98.6% of raw-path teleports at ~no R15 cost — the raw path becomes render-plausible,
   and the ~18-22 remaining fast events match the human-adjudicated count of REAL launches (~17
   teleport anchors on this game). The transition model now agrees with the human.
2. **Identity is unchanged (.48-.49)** — WHICH object the track holds is the EMISSION's job, and this
   A/B ran the hand-tuned emission. The integration that matters next: learned selector emission ×
   physical transitions × bridge on FULL games. Needs net persistence + a fullgame-replay mode in the
   harness — scheduled with the 15-game retrain (marathon completes tonight).
3. The stride-scaled legacy budget is a bug-shaped design: keep physical mode for all full-game work;
   eval-dump sweeps must re-tune (their spans never contain a flight, so bridge/cone effects are
   invisible there — EXP-DIST-31's gains came from rate-band shaping alone).

**Code:** `d763f1e` (cone), `6b1b9e0` (physical transitions). Data: this table from the server inline
A/B (Chili fullgame dump + consolidated labels).

## EXP-DIST-31: aerial bridge v0 — flight-consistent miss re-entry lifts held-out FAR +0.07..+0.20 (2026-07-06)

**Hypothesis (from EXP-DIST-30):** making the miss state position/time-aware — re-entry at a
flight-consistent rate gets a bonus, faster-than-flight re-entry a quadratic penalty — lets the track
bridge launches instead of parking near the launch point, without the globally-loosened teleport gate.

**Method:** `rerank()` miss state now carries the frozen exit position + missing duration (greedy
backpointer approximation, no state blow-up). `bridge_w=0` = exact legacy. Unit test: launched ball
lands far upfield vs a brighter phantom at the launch point — legacy takes the phantom, bridge lands
with the ball. Replay sweep on both held-out dumps (v3 supervision net; learned emission, hand terms
off): w × {flat miss 0.5/0.9, pnone×0.5/1.0} × bridge {0, 1, 2}.

**Results (FAR R15, br=0 → best bridge, matched config):**
- Spc: miss=0.5 .526→**.598** · miss=0.9 .492→**.583** · pnone×1.0 .442→**.580**
- Iron: miss=0.9 .500→**.700** · pnone×0.5 .500→**.700**
- Best ALL rows: Spc w=2 miss=0.5 br=1.0 **.550** (was .506); Iron w=1 miss=0.5 br=0 .538 vs
  br-rows trading ALL for far. NEAR is the tradeoff: Spc near drops up to −0.19 on some bridge rows
  (the bridge re-enters far more eagerly; near excursions get bridged too). br=1.0 is the sane default;
  br=2.0 over-bridges.
- **Learned p_none miss costs ≈ neutral in v1** (none supervision is sparse: 439/26k frames) — keep the
  plumbing, revisit when none-volume grows.
- Caution: Iron LEARNED argmax far swung .727→.364 across supervision versions (n=11 far GT — small
  sample + near-heavy new gold). The tracker+bridge held far at .6–.7 regardless. LOGO at 15 games will
  say whether the argmax swing is noise or a near/far training tradeoff.

**Conclusions:** the aerial bridge is the first tracker-side change that moves held-out far R15 while
leaving the candidate ceiling untouched; it directly addresses the human-adjudicated failure mode
(parking through flights). Next: direction-aware bridge (launch velocity cone, not just rate), then the
full candidate × ball-state Viterbi (§3b) with out-of-frame ballistic coast.

**Code:** `e19e49a` (reranker bridge + kill_test `--bridge-w`/`--pnone-scales`).

## EXP-DIST-30: track-audit adjudication — labels vindicated; "teleports" are AERIAL balls the tracker fumbles (2026-07-06)

**Method:** `build_far_label_queue --criteria diverge` (track-audit sets, Mark's design): per game, flag
(a) human-label-vs-track disagreements >10 m, (b) raw-selection world jumps >2.5 m/frame (`teleport`),
(c) sustained miss runs — each with ±2 dump-step context frames; hint marker = the track's own position.
Mark labeled 3 full games (Chili, Pittsford 05.07, Lakefront) + 2 partial. Adjudication = meters between
his fresh click and the track position, per signal type.

**Results (fresh-click vs track, 'ball' frames):**

| game | diverge: track-on-ball / med err | teleport: track-on-ball / med err |
|---|---|---|
| Chili | 0.06 / 61.5 m | 0.18 / 34.4 m |
| Pittsford 05.07 | 0.06 / 43.9 m | 0.18 / 22.5 m |
| Lakefront | 0.00 / 53.8 m | 0.16 / 50.8 m |

**Conclusions:**
1. **Label quality vindicated:** on disagreement frames Mark's re-clicks land back on his original
   positions — the track is the one 40–60 m off (~95% of disagreements are TRACK faults). Human gold
   can be trusted at weight ×20.
2. **Mark's read from labeling: most flagged "teleports" are AERIAL balls** — the ball leaves the
   camera's vertical FOV and re-enters far upfield. The jump is real motion, not a distractor switch.
   AND the track handles those windows badly (~82% of teleport-window frames have the track >10 m off):
   it parks on the launch point / a distractor during flight and re-acquires late. This is EXP-DIST-11's
   "impossible 67 m jumps" adjudicated by a human: state-blindness, not detection failure.
3. **The aerial ball-state (plan §3b) is promoted to the top tracker lever.** Concrete v0 to test on
   cached dumps via the replay harness + continuity metric: a "relaunch" transition — long jumps whose
   direction/magnitude are consistent with the pre-gap velocity (launch cone) get reduced cost, instead
   of one global loosened teleport gate. Then: out-of-frame ballistic coast + re-acquisition cone.
4. These windows are exactly where teacher supervision is BLANK (the stability filter drops frames near
   teacher discontinuities) — Mark's clicks there are the only supervision that exists. The diverge-set
   loop (label → consolidate → rebuild → retrain) is the mechanism to keep filling them.

**Data:** `D:/training_data/far_label/*__diverge/labels.json`; supervision rebuilt (Chili gold 386,
Lakefront 302, Pittsford 417+). **Code:** `fa2462a`, `1b87634`.

## EXP-DIST-29: selector v2 at 8-game scale — far GO bar cleared once two train/eval feature mismatches were fixed (2026-07-06)

**Hypothesis:** The kill test's NO-GO was a supervision-volume problem (EXP-DIST-24 re-open conditions):
at ~26k frames / 942 gold / 333 none, the listwise selector should transfer.

**Method:** 8 marathon fullgame dumps (stride 8) + `build_selector_labels` supervision → `kill_test_selector`
(now accepts fullgame dirs; held-out guard added). Eval on `cands_{spc,iron}_hn2.pkl` (stride 4), decomposed.

**Run 1 was poisoned by two train/eval mismatches (both caught by inspection before trusting results):**
1. **Stride:** `cont_*` features measured meters-per-DUMP-STEP — stride-8 train vs stride-4 eval = 2×
   systematic shift on the window family. Fix: `build_features(ef=...)` normalizes to meters-per-FRAME
   (cap 6 m/f), stride-invariant by construction + regression test.
2. **size_ratio:** fullgame dumps carry no sizes (constant 0 at train); eval dumps have real sizes on all
   36k rows. Fix: feature dropped on BOTH sides (`feature_mask` now takes single features;
   `--knockouts sweep <extras>`) until size_px is wired into the dump path.

**Result (fixed run, LEARNED argmax vs raw argmax; raw = Spc N .467/F .264, Iron N .188/F .273):**

| knockout | Spc argmax N/F | Iron argmax N/F | best replay-tracker FAR (Spc / Iron) |
|---|---|---|---|
| none (13 feats) | .322 / **.442** | **.375 / .727** | .526 / **.800** |
| −score | .356 / .388 | .188 / .364 | .498 / .600 |
| −persistence | .333 / .385 | .312 / .455 | .489 / .800 |
| −geometry | .300 / .436 | .375 / .455 | .540 / .700 |
| −window | .367 / **.485** | .250 / .727 | .620 / .700 |
| −frame | .311 / .430 | .375 / .636 | .521 / .700 |

**Conclusions:**
- **The stride fix, not more data alone, unlocked far transfer**: far argmax +0.18 (Spc) / +0.45 (Iron)
  over raw — the +0.15 far GO bar is met on BOTH held-out games. Volume also matters: same harness on the
  same-family features failed at 2-game scale.
- **Score family is the main carrier** (biggest drop when removed, val loss 1.61→2.28). Geometry no longer
  venue-overfits at 8-game scale (kill-test v1's knockout verdict reversed by venue diversity).
- **NEAR is the open problem**: Spc near argmax .322 vs raw .467 — the learned prior actively demotes near
  balls the raw score already ranked #1. Iron near improves (+0.19). Suspects: far-biased gold (all
  far-label sets), teacher-weight flatness, no near-specific features. Replay-tracker near collapses in
  spots (−window Spc N .122) — the window family is what keeps near coherent.
- Emission-only replay (alpha=0, static_w=0, hand terms off) is not yet at the production hand-tuned
  SELECTED bar on Spc far (.526 vs .61 aggregate) but beats it decisively on Iron far (.80). Proper
  comparison needs per-game hand-tuned rows on the same dumps + miss_costs/anchor integration.

**Next:** near-band supervision (near gold / band-balanced loss), hand-tuned-vs-learned per-game baseline,
`-log p_none` miss_costs + kickoff anchors in the replay, LOGO variance, retrain at 15 games when the
marathon lands.

**Data:** `G:\ballresearch\selector\v2_train.log` (+ `v2_train_run1_stridebug.log`); supervision
`sel_labels_*_full.json` (8 games). **Code:** commits `c20284c`, `feed7d5`.

## EXP-DIST-28: soft in-field prior (± airborne dome) — NO effect on the tracker (2026-07-04)

Mark asked whether the field polygon eliminates off-field near distractors, and whether
out-of-polygon far detections might be AIRBORNE balls. Facts + test:
- The detector input is already hard-masked to polygon + 400 px far margin (kept open on purpose:
  cropping at the far line dropped ~1/3 of very-far GT — airborne/far-corner balls sit above it).
- EXP-DIST-17 showed a HARD in-field gate does nothing (0.24→0.24, distractors are in-field). Static
  mining (EXP session 07-04) showed hn2's leaked statics cluster in the far-margin/edge zones — so
  re-tested with a SOFT support cost (w=2.0, margin 120), with and without a 400 px airborne dome
  carve-out (`sweep_tracker` rows `strong +support`, `+support+dome`), on both hn2 held-out dumps:
  **identical to baseline on every metric, both games.** `static_persistence` already keeps the
  Viterbi PATH off those objects; their damage is to per-frame RANKING (argmax/selector), not the
  smoothed track. Rows kept in the sweep for future candidate regimes.
- The airborne thread stays live as designed (§3b aerial ball-state): EXP-DIST-11's "impossible
  67 m jumps" are ground-plane projections of flying balls; the concrete follow-up is an ARC-FIT
  feature (quadratic y(t), linear x(t) over the symmetric dump window — gravity-consistency) as
  selector evidence + dome-zone flying-ball rescue. Slotted for the ball-state phase.

## EXP-DIST-27: hn3b (mined, 2 epochs) — ALSO below hn2; the fine-tune lever is closed (2026-07-04)

`hm_reolink_hn3b` = resume hn2 + mined store + **2 epochs**: held-out Spc best SELECTED far
**0.624** (a1.0/mj40) vs hn2 0.703, near ceiling recovered to 1.0 but far ceiling 0.939 (−0.019);
Iron far 0.636 vs 0.727. Combined with EXP-DIST-25/26: EVERY warm-restart fine-tune variant
(8ep plain, 8ep mined, 2ep mined) lands BELOW the hn2 checkpoint on held-out, while mining only
ever helps relative to its epoch-matched control.

**Conclusions (hn series closed):**
1. **`hm_reolink_hn2` is the production detector checkpoint.** Do not warm-restart fine-tune it
   again on this corpus — the recipe is net-negative regardless of added negatives or epoch count.
2. Mined negatives are kept in the store (they demonstrably counteract overfit); their right use is
   the next FULL training run, not a fine-tune.
3. Remaining within-corpus detector lever: **Dahua supplemental joint training** (camera-balanced,
   warp-normalized — the original v4 cross-camera design; ~50 games / many venues never yet used).
   Remaining eval lever: more held-out labels (sets queued for Mark, candidate overlays injected).

## EXP-DIST-26: first TRUE mined-negatives round — mining is real but the 8-epoch recipe cancels it (2026-07-04)

**Run:** `hm_reolink_hn3` = resume hn2 + 8 epochs on the store WITH +2,391 mined hard negatives
(fixed miner; 14 games, 51–228/game; crops vision-gated: linesman-with-flag, spectator clusters,
player heads, goal-mouths, sideline clutter — the intended distractor taxonomy). Minimal pair vs
`hm_reolink_hn2ep8` (identical but NO mined crops; both stores also carry the 4× human-crop
oversampling, which hn2 itself predates).

**Held-out Spencerport three-way:**

| metric | hn2 (peak) | +8ep control | hn3-true (mined+8ep) |
|---|---|---|---|
| ceiling ALL / far / near | 0.964 / 0.958 / 0.989 | 0.948 / 0.936 / 0.989 | 0.960 / **0.961** / 0.956 |
| score-argmax near / far | **0.467** / 0.264 | 0.222 / 0.236 | 0.289 / 0.248 |
| best SELECTED ALL / far | 0.60 / **0.703** | 0.545 / 0.624 | **0.614** / 0.691 |

Irondequoit: hn3 strong-config far 0.727 (= hn2). Val recall flat ~0.40 throughout (still blind to
all of this — EXP-DIST-25's val≠held-out caution again).

**Read:** (1) **Mining works** — vs the clean control it recovers far ceiling (+0.025), far SELECTED
(+0.067), near argmax (+0.067). (2) **The 8-epoch fine-tune destroys roughly what mining earns** —
net vs hn2 is a wash on far and a clear near-argmax regression. The mined fraction is 3% of the
store; 8 epochs of the other 97% re-overfits.

**Next (launched 05:52): `hn3b` = same mined store, 2 EPOCHS.** hn3b far SELECTED > hn2 0.703 ⇒
mining iterates with short fine-tunes (and possibly mined-crop upweighting); hn3b ≈ hn2 ⇒ the
detector is venue-limited at the current corpus and VENUE DIVERSITY becomes the primary lever (the
long-standing data-scaling diagnosis).

## EXP-DIST-25: hn1/hn2 mining NEVER RAN — gains were epochs; epochs-alone has now PEAKED (2026-07-03)

**Discovery (from the logs, while relaunching round 3).** `hn_mine.log` and `hn2_mine.log` both end in
the same crash: `AttributeError: 'dict' object has no attribute 'append'` — the miner assumed the
old bare-list `index.json` while the store has the `{"summary","items"}` form. **Zero mined hard
negatives ever entered `crops_reolink`** (exactly 1 orphan `*hardmine*` .npy exists, from today's
crash). CORRECTION to EXP-DIST-18/19/22: the "hard-neg" gains (near argmax 0.244→0.378→0.467, Spc
far ceiling →0.958) were produced by **+8 training epochs per round** (hn2 additionally trained with
the +1,526 human crops appended by the dict-aware `build_human_crops`, store 76,875→78,401). Miner
fixed (`load_index`/`save_index`, both forms, round-trip-tested; commit 84d469d).

**Epochs-only control (accidental but clean): `hm_reolink_hn2ep8`** = hn2 + 8 more epochs on the
UNCHANGED store (tonight's first chain ran with the still-broken miner). Held-out Spencerport vs hn2:

| metric | hn2 | hn2+8ep control |
|---|---|---|
| ceiling ALL / far | 0.964 / 0.958 | 0.948 / **0.936** |
| score-argmax near / far | 0.467 / 0.264 | **0.222 / 0.236** |
| best SELECTED ALL / far | 0.60 / 0.703 | 0.545 / 0.624 |
| near med-rank | 6 | **11** |

**The epochs lever has peaked at hn2 and is now degrading** (held-in Cleveland val recall stayed
~0.40 — val does not track held-out; known caution, now quantified). Artifacts renamed
(`hm_reolink_hn2ep8`, `cands_*_hn2ep8.pkl`, `sweep_hn2ep8.log`).

**True round 3 (first honest mined-negatives run) launched 22:51** from the fixed miner
(`selector_hn3_chain`, same recipe: resume hn2 + 8 epochs, only difference = mined crops in the
store). Clean A/B against BOTH hn2 (the peak) and the epochs control: hn3 > hn2 ⇒ mining is a real
lever; hn3 ≈ control ⇒ the detector lane is data-limited ⇒ venue diversity becomes primary.

## EXP-DIST-24: selector KILL TEST — NO-GO at 2-game supervision; depth-cal rescoring a wash (2026-07-03)

**Setup (the pre-registered Phase-1 gate for the learned-selector bet).** Selection-level
distillation labels on 2 training games (Cleveland 505 + Chili 778 frames after stability
filtering; teacher = AutoCam dets → `track_ball`, interpolated onto the dump grid — the marathon's
detections are on the 0-mod-4 grid, a dump's ef grid is phased by its GT span, and on Cleveland they
never intersect: exact-key matching produced 0/1501 labels until `teacher_at()` lerp landed).
Listwise net (14 context features, softmax over 24+none), evaluated on the held-out hn2 dumps.
**GO required: learned argmax ≥ +0.15 over raw argmax on far AND near, BOTH games.**

**Result: NO-GO.**

| game | raw argmax (near/far) | LEARNED argmax (near/far) | best learned-emission tracker (far) | hand-tuned tracker (far) |
|---|---|---|---|---|
| Spencerport (n=420) | 0.467 / 0.264 | **0.300 / 0.294** (−0.17 / +0.03) | 0.29 | **0.703** |
| Irondequoit (n=27) | 0.188 / 0.273 | 0.375 / 0.455 (+0.19 / +0.18) | 0.60 | 0.727 |

Training never fit well (val CE ~1.9 ≈ choosing among ~7; no early stop in 60 epochs). Notable: the
training windows carried **0 none-labels and 0 gold** (the games' human labels fall outside the
dumps' 6,000-frame GT spans), and the label mass is far-biased — consistent with the near
degradation on Spc.

**Knockout diagnostics (the useful part):**
- knockout **score** → far collapses (Spc 0.215): the score family carries most of the signal.
- knockout **geometry** → far IMPROVES on both (Spc argmax 0.294→0.376, Iron 0.455→0.636): at
  2-game scale the depth/infield/size features are venue-overfit noise — the red-team's watch-item
  confirmed empirically.
- window/persistence/frame knockouts ≈ wash at this scale.

**Depth-calibrated confidence replay (B-lever 2, `sweep_tracker` DEPTH-CAL rows):** re-scoring
candidates by score-percentile-within-depth-band: Spc argmax far +0.03 / near −0.11, tracker far
0.636 vs 0.682 raw; Iron tracker far 0.636 vs 0.727 raw. **Wash-to-negative** — the raw sigmoid is
saturated, so percentiles recover no ordering information. Calibration must come from the detector's
TRAINING (which hard-neg rounds demonstrably do: hn2 near argmax 0.30→0.467).

**Decisions.**
1. Per the pre-registered branch: **B-track is now primary** — hard-neg round 3 launched
   (`selector_hn3_chain`: mine with hn2 → fine-tune → dump both held-out → sweep; first live run of
   the segment-decode miner).
2. The learned selector is NOT dead-and-buried — Iron's +0.18/+0.19 and the score-knockout collapse
   show a learnable, transferring signal exists — but it goes back on the shelf until the
   supervision is materially better: full-game dumps (not 6k windows), many more games, none-labels
   + gold overlay in-window, and geometry features dropped until venue count supports them. Do NOT
   re-run the same 2-game test expecting different results.
3. Keep: the harness (features/net/labels/kill CLI), per-frame `miss_costs`, kickoff `anchors` —
   all integration-ready when (2) is revisited.

## EXP-DIST-23: viewport-loader alignment audit — PASS, viewport math unblocked (2026-07-03)

**Why.** EXP-DIST-16 flagged all AutoCam-viewport comparisons as untrustworthy (loader misalignment
suspected). The selector iteration's acceptance protocol (T3) is a mathematical our-viewport-vs-
AutoCam-viewport comparison, so alignment had to be established first.

**Method.** Box-scratch `G:\ballresearch\selector\viewport_audit.py`: sample `autocam_viewport.jsonl`
rows at 25/50/75% of a game; composite (a) the source panorama cropped at the sidecar `(x,y)` with a
crosshair vs (b) the AutoCam-processed video frame at the same global index (and at candidate trim
offsets). Vision-verified panel-by-panel.

**Results.**
- `flash__2024.05.01_vs_RNYFC_away` (Dahua, main-dir `-once-processed.mp4`): **3/3 pose-exact** —
  same instant, crosshair on the framed action. `(seg,f)→global` mapping + pan-center semantics RIGHT.
- `heat__2026.05.31_vs_Spencerport_gold_2_away` (Reolink, render in the upload subdir): tested offsets
  {0, 1162}: **3/3 match at offset 0** (e.g. g=60426: AR in red + identical spectator poses in both
  panels); offset 1162 clearly a different moment. So the sidecar is already on the combined/global
  axis and the subdir render aligns 1:1 — it was made from the UNTRIMMED raw (10.6 GB ≈ combined);
  the far-label +1162 trim belongs to the YouTube upload only, NOT the render.

**Verdict: viewport comparisons are trustworthy** on both the main-dir and subdir-render layouts
(cv2 frame seek verified pose-exact at sampled frames). Composites: `G:\ballresearch\selector\
vp_audit_{rnyfc,spc}\*.jpg`.

## EXP-DIST-22: RANK diagnostic + hn2 read-off — the ball is present but DEEPLY buried (2026-07-03)

**Question.** (a) When selection misses, is the GT ball ranked 2–5 by score (a context re-ranker can
fix it) or 11+/absent (the detector buries it)? (b) Did the 2nd hard-negative round (`hm_reolink_hn2`,
dumped 7/2 but never read) move anything? Tool: new `sweep_tracker` RANK section (`rank_table`) — the
GT ball = nearest candidate in meters within R15; report its 1-based score-rank per band, `--rank-only`
for fast replays. (This is the rank script the 07-03 session attempted; the `geometry` API mismatch is
resolved by using `image_to_world`/`expected_ball_diameter_px` exactly as `_ceiling` does.)

**RANK result (epoch-3 human model dumps; GT sets are hard-mined, so ranks are biased hard):**

| game (band, n) | r1 | r2–3 | r4–5 | r6–10 | r11+ | absent | med rank |
|---|---|---|---|---|---|---|---|
| Spc near (90) | 0.12 | 0.12 | 0.10 | 0.23 | **0.42** | 0.00 | 9 |
| Spc far (330) | 0.17 | 0.08 | 0.05 | 0.15 | **0.46** | 0.08 | 10 |
| Iron near (16) | 0.06 | 0.06 | 0.12 | 0.12 | **0.56** | 0.06 | 14 |
| Iron far (11) | 0.18 | 0.27 | 0.00 | 0.00 | 0.45 | 0.09 | 8 |

**hn2 read-off (Spencerport n=420 / Irondequoit n=27):**
- **Ceiling UP, no regression:** Spc ALL 0.936→**0.964**, far 0.918→**0.958**, near 0.989. Iron flat
  (0.926/0.938/0.909).
- **Near ranking improved a lot:** Spc near argmax 0.30→**0.467** (hn1 was 0.378 — still climbing),
  med rank 9→6, r11+ 0.42→0.32. **Far barely moved:** argmax 0.239→0.264, r11+ 0.46→0.44, med 10.
- **Best SELECTED:** Spc far **0.703** (a3.0/mj15/v8), near ≤0.34; Iron far **0.727** (a1.0/mj25/v12),
  near 0.75 @ a0.3/mj15/v8 (n=16, directional). The per-game optimum DISAGREES across games again
  (Spc wants α3.0, Iron α0.3) — the hand-tuned emission is fragile, same as EXP-DIST-20.
- **Confidence-hybrid identical at T=0.2..0.7 on both dumps** — raw sigmoid saturated ≥0.7 essentially
  everywhere (confirms EXP-DIST-19); raw score carries almost no usable confidence signal.

**Conclusions.**
1. On hard frames the ball is in the candidate set (absent ≤0.09) but **rank 11+ of 24 roughly 40% of
   the time** — this is NOT a "rank 2–5, light re-rank" situation. Any learned selector must lift
   deeply-buried candidates on context alone → the Phase-1 kill test (learned argmax ≥ +0.15 both
   bands, both games) is the decisive gate before building on bet (A).
2. **Hard-negative mining (B) is still climbing** (ceiling +0.04 far, near argmax +0.09 over hn1) →
   run round 3; make **hn2 the base detector** for kill-test dumps and future evals.
3. Depth-calibrated confidence is strongly motivated: scores are saturated and carry no cross-frame
   ranking signal (score_norm is per-frame-max anyway).

**Code:** `training/cli/sweep_tracker.py` (`rank_table`, `--rank-only`), `tests/test_rank_diagnostic.py`.

## EXP-DIST-21: raw per-segment decode replaces combined-video decode (2026-07-02)

**Problem.** Extracting crops/strips by sequentially decoding the per-game `combined.mp4` is slow (a
full-game `__hard` set decodes ~88k 7680×2160 HEVC frames — the human-label crop build took ~2h and a
scheduled-task time limit killed it right before the index write) and fragile (a corrupt packet
crashes the whole decode — the Flaitz game produced no far-label set at all).

**Findings (verified, not assumed).**
- `combined.mp4` is a **stream-copy concat** of the raw segments + realigned audio, so a raw-segment
  frame is **bit-identical** to the combined's `global = segment.global_offset + f` (mean-abs-diff
  0.00, incl. across a segment boundary).
- Both the combined and the raw Reolink clips are **VFR** (irregular PTS — half-frame offsets, dropped
  frames), and the raw clips are **GOP=20** (not GOP=1 as an old docstring claimed). So per-frame
  seeking must land on a keyframe and decode forward, indexed by presentation-order PTS.

**Solution.** `data_prep/segment_decode.py::extract_frames_from_segments`: map each wanted global to
`(segment, f)`, per segment build the presentation-order PTS list, seek to the keyframe ≤ each cluster
and decode forward. Corruption-isolated per segment; decodes ~one GOP per label, not the whole stream.

**Validation.** Module: 11/11 frames pixel-exact incl. cross-segment + a consecutive band. `build_far_
label_queue`: Flaitz (which crashed before) now builds 150/150 strips, vision-confirmed clean. `build_
human_crops`: Fairport rebuild matches old crops to ≤10/255 (NVDEC-vs-CPU decoder rounding, not
misalignment — a wrong frame is 30–80). Wired into both CLIs.

**Code:** `data_prep/segment_decode.py`, `cli/build_far_label_queue.py`, `cli/build_human_crops.py`.

## EXP-DIST-20: cross-game validation (Irondequoit) — generalizes, but the mj40 default was overfit (2026-07-02)

Every labeled Reolink game except Spencerport was IN the distill training set (Cleveland=val, rest=train),
so the only clean 2nd held-out game is **Irondequoit 06.15** (no `autocam_detections` → excluded from the
build → genuinely held-out; n=27 GT in span, so noisy but directional). Dumped with the hard-neg model,
swept:

| config | ALL | NEAR | FAR |
|---|---|---|---|
| candidate ceiling | 0.926 | 0.938 | 0.909 |
| score-argmax (no track) | 0.074 | 0.062 | 0.091 |
| baseline (tight mj6) | 0.296 | 0.188 | 0.455 |
| a0.3 mj40 v20 (shipped) | 0.296 | 0.188 | 0.455 |
| **a1.0 mj15–25 (best)** | **0.481** | **0.312** | **0.727** |

**Validated:** (1) the detector generalizes — Iron ceiling 0.91–0.94 ≈ Spencerport 0.95. (2) Teleport
loosening generalizes — far 0.455→0.727. (3) The tracker adds real value — argmax 0.07 vs tracker 0.48
(opposite of Spencerport, where argmax was competitive — Iron's candidates are noisier, so context matters
more). **Corrected:** the single-game optimum (a0.3, mj40, v20) was **Spencerport-overfit** — on Iron it
gives far 0.455 vs 0.727 at the tighter a1.0/mj25, and **α=1.0 beats α=0.3 decisively** (the hard-neg
detector's scores are now worth trusting). → robust default set to **α=1.0, mj=25, v=12** (Spc far
0.648/near 0.189; Iron far 0.545–0.727/near 0.312). Far cross-validated ~0.55–0.73 (target 0.845 — closer,
not there); NEAR still ~0.19–0.31 (target 0.978) — the open frontier (EXP-DIST-19).

## EXP-DIST-19: hard-neg fine-tune (modest far gain) + near is a TWO-part problem (2026-07-02)

**Hard-neg fine-tune** (`cli/mine_hard_negatives` → augment store → `train_v4_heatmap --resume best.pt`,
8 ep; re-dump + re-sweep). Fine-tuned candidates vs original, held-out Spencerport R15m:

| | far (best cfg) | near argmax | near (best tracker) | ceiling |
|---|---|---|---|---|
| original best.pt | 0.618 | 0.244 | 0.222 | far .933 / near 1.0 |
| **hard-neg best.pt** | **0.648** | **0.378** | 0.189 | far .939 / near .989 |

Hard-neg **cleaned the candidates** (old tight-teleport baseline far 0.288→0.597) and **improved near
detection** (argmax 0.244→0.378) — recall held (0.384), ceiling held. Far nudged 0.618→0.648.

**The confidence-hybrid (EXP 3) failed as a near fix:** the detector's top RAW sigmoid score is saturated
(~1.0 every frame), so "trust argmax where score≥T" collapses to plain argmax at all T. Size-consistency
can't separate a near ball from a far distractor either (both are size-consistent at their own locations).

**Near is now a diagnosed TWO-part problem:** (a) **detector near-score** — even argmax hits only 0.378
(ceiling 0.989), so a distractor still outscores the near ball **62%** of near frames; hard-neg helped but
not enough. (b) **global-path tracker** — argmax (0.378) beats every tracker config on near (≤0.189): the
single global-smooth Viterbi sacrifices the minority near-excursions (90 near vs 330 far) to keep the far
track smooth. Neither yields to a config tweak. → the near fix needs a **near-focused detector pass**
(more near GT / near-distractor hard-negs) AND a **non-global-single-path selector** (mode-aware /
per-frame-competitive — the world-model). Far (0.648, target 0.845) is candidate-quality-limited.
Cross-game validation (Irondequoit, held-out) running.

## EXP-DIST-18: the AutoCam "viewport/sidecar" is camera FRAMING, not ball detection — real bar reset (2026-07-01)

Chasing "beat AutoCam" needs a valid bar. `autocam_viewport.jsonl` starts at x=3840,y=1080 = exact
frame-center of 7680×2160 (constant for the first frames) — it's AutoCam's **pan center** (frames a wide
region), not the ball. The `<processed>.mp4.jsonl` sidecar `xy` is the same camera-framing signal (R15m
**0.022**, median **53 m** vs GT — a working camera is not 53 m from the ball; it frames a region with
the ball off-center). So BOTH are camera framing, **NOT AutoCam's ball detection**, and `load_viewport`'s
"AutoCam's selected ball" docstring is mislabeled. This also finally kills the "AutoCam 0.15 far" premise
(EXP-DIST-08/16) — that number was the viewport too.

**Valid bars (meters vs human GT, held-out Spencerport):**
- AutoCam DETECTIONS → OUR tracker: **far 0.845, near 0.978** — the real "beat AutoCam on detection"
  target (same tracker both sides).
- OUR candidate ceiling: **far 0.933, near 1.0** — EXCEEDS AutoCam's, so the goal is achievable: the ball
  is in our candidates more often than AutoCam's detections realize; the residual is SELECTION/SCORE.
- OUR detector→tracker now (post teleport fix): far 0.618, near 0.222.

**Targets:** far **> 0.845**, near **≥ 0.978**. The near gap (0.222 vs 1.0 ceiling; even argmax 0.244) is
a detector-SCORE problem — near distractors (players) outscore the near ball. → hard-negative fine-tune
(EXP-DIST-19, running overnight) is the lever for BOTH far and near.

## EXP-DIST-17: the teleport gate was the far-ball selection bug (2026-07-01)

**Setup.** Held-out Spencerport, distilled `best.pt` (val 0.397), meters-to-human-GT. Built an offline
tracker sweep (`cli/eval_detector --dump-cands` → `cli/sweep_tracker`): cache the 1,501-frame candidate
set once (~40 min decode), then replay `world_model.reranker` under many configs in seconds — the ceiling
is candidate-fixed (the bar), only selection changes. This broke the 40-min-per-hypothesis loop.

**Cheap gates first — both FAILED (as designed to test):** in-field gate = no change (0.24→0.24; the
distractors are IN-field, not off-field). Size-consistency HARD gate DROPPED the ceiling 0.94→0.869
(rejected real balls — the pixel-blob size measure is unreliable near lines/bright patches); a soft size
prior also hurt. → candidate **gating** is not the selection fix.

**The lever = the teleport gate.** The tracker mis-selects though the ball is a candidate 94% of the
time; even NEAR balls fail (0.067, ceiling 1.0), so it's the tracker, not the candidates. Sweep:

| config | ALL R15 | NEAR | FAR | median |
|---|---|---|---|---|
| candidate ceiling (bar) | 0.948 | 1.0 | 0.933 | 2.7 m |
| score-argmax (no tracker) | 0.293 | 0.244 | 0.306 | 60.4 m |
| baseline (mj6 v2.5) | 0.24 | 0.067 | 0.288 | 51.5 m |
| **mj40 v20 (new default)** | **0.533** | 0.222 | **0.618** | **12.6 m** |

The old tight **meters-space** gate hard-excluded the true far candidate (meters are ill-conditioned near
the far touchline — EXP-DIST-11's 82.5%). Loosening it (max_jump 6→40, vmax 2.5→20 per frame) lifts far
R15m **0.288→0.618**, median **51.5→12.6 m**, and the tracker finally beats argmax (0.293). `alpha=0.3`
and `static_w=2.0` stayed best (raising alpha / cutting static both hurt); Kalman marginal-positive;
size & support priors hurt/null. Shipped as the `RerankConfig` default.

**Still open:** (1) far 0.618 < ceiling 0.933 — our candidates are noisier than AutoCam's
(AutoCam-dets→our-tracker 0.845); cleaner scores (hard-negatives, `cli/mine_hard_negatives`) is the next
far lever. (2) NEAR 0.222 ≪ 1.0 ceiling and ≈ argmax — a single global-smooth path can't cheaply follow
far↔near excursions; needs per-frame trust / mode-aware selection (the world-model). (3) Validate
mj40/v20 across the held-out span before fully trusting the new default (sweep-optimum on one game).

## EXP-DIST-16: distill AutoCam via its detections + OUR existing tracker (not the viewport) (2026-06-30)

**Hypothesis (Mark).** Our detector is better on far balls (human GT); AutoCam is better on near/normal.
Distill AutoCam's near/normal detections into our detector while preserving our far advantage with
human GT, so OUR **existing tracker** (fed OUR detector's detections) follows the ball to viewport
tolerance (~10–15 m) everywhere — matching AutoCam in normal play, beating it on far.

**GT-grounded diagnosis (the pivotal result).** On **1,880 human far-GT balls** (frames AutoCam
loses), scored in meters (`cli/gt_near_far.py`, `cli/validate_tracker.py`):

| signal | R15 m | median err |
|---|---|---|
| candidate ceiling (ball in AutoCam's detection set) | **0.97** | 0.3 m |
| **existing tracker over AutoCam detections** | **0.766** | 2.1 m |
| AutoCam raw viewport-gated pick (nearest cand to viewport) | 0.10 | — |
| **AutoCam's own viewport (what it delivers)** | **0.147** | 41.2 m |

So AutoCam's detector **finds** the far ball 97% of the time; AutoCam's *selection* (viewport) loses
it (0.15); the existing tracker over the same detections recovers it (0.77). The viewport-vs-GT error
is random-direction (mean [418,245] vs std [1931,418]) → AutoCam genuinely looks elsewhere, not a
coordinate bug. **Conclusion: detection is fine, selection is the game, and the existing tracker
already solves it.** → teacher = tracker over AutoCam detections + human-GT override (`teacher_track`),
NOT the smoothed viewport. Dense tracker-vs-viewport agreement across whole games is low (median
13–50 m) because the viewport is a loose camera-centre, not a tight ball track — reinforces the above.

**Method.** teacher_track (active-play + in-field filtered, snapped to real detections; human far-GT
exempt) → `build_heatmap_crops` (NVDEC) → HeatmapNet base24. Held-out `heat__2026.05.31_vs_Spencerport`
(545 GT). Crop vision-gate caught 3 teacher bugs pre-training (warm-up, off-field, Dahua noise).
Reolink-primary first build (2024 Dahua = noisy detection + no GT anchor).

**Result (2026-07-01) — DID NOT meet the goal; the win is a precise diagnosis, not a "beat".**
Reolink build = **76,875 crops / 15 games**. Held-in Cleveland val recall peaked **0.397 @ epoch 10**
then flat → watcher early-stopped at epoch 16. Held-out eval on Spencerport (n=420 GT in the capped
span 6714–12714), R15 m:

| row | ALL | NEAR+MED (n=90) | FAR (n=330) | median |
|---|---|---|---|---|
| **OUR detector → tracker** | **0.24** | **0.067** | **0.288** | 50–65 m |
| AutoCam viewport | 0.026 | 0.044 | 0.021 | 57–66 m |
| AutoCam dets → OUR tracker | 0.845 | 0.978 | 0.809 | ~3 m |
| OUR candidate ceiling | 0.948 | 1.0 | 0.933 | ~3 m |

**Read (honest):**
1. **OUR detector→tracker fails** (R15m 0.24, median 51 m) — the pipeline does NOT follow the ball. Goal not met.
2. **The AutoCam-viewport baseline is BROKEN** (R15m 0.026, median 57 m; within 5 m only 0.2% — not a
   credible number for a shipping product). Our `autocam_viewport.jsonl` loader is misaligned
   (frame-index or coordinate/scale). So "0.24 > 0.026" beats a broken baseline — **meaningless**. This
   also discredits the earlier **"AutoCam 0.15 far bar"** (EXP-DIST-16 above): same viewport-extraction
   artifact + cherry-picked frames. **Do not trust any viewport comparison until the loader is audited.**
3. **Trustworthy (viewport-independent) diagnosis:** our detector SEES the ball (ceiling 0.95, near 1.0),
   our tracker WORKS (AutoCam dets → our tracker 0.85), but **our detector → our tracker = 0.24**. The
   gap is **SELECTION**: our detector emits the ball among top-k=24 peaks *plus* many confident
   distractors (AutoCam emits few, ball-focused), and the Viterbi tracker locks onto a smooth distractor
   trajectory. Tell-tale: NEAR (0.067) is WORSE than FAR (0.288) — a ball-following tracker would ace big
   near balls; ours is stuck on distractors regardless of ball position.

**Next (the real lever): candidate quality, not epochs.** Suppress distractors / calibrate peak scores /
tighten top-k so the tracker can pick the ball from our peaks (it already can from AutoCam's). Separately,
**audit the viewport loader** before believing any AutoCam-viewport number. Detector + tracker are both
sound in isolation — the join is the whole problem.

Supersedes the far-weight dead end (EXP-DIST-13/14) and the world-model-reranker plan; the lever was
never loss-weighting or a bespoke re-ranker — it was giving the existing tracker good detections.

## EXP-PHASE-18: parent event-tap phase anchors — GT simulation validates + hardens (2026-07-02)

**Goal (Mark):** parents already tap Kickoff/Halftime/2nd-Half/Final-Whistle in moment-tagger
(`EventSyncPrompt` → `sync_anchors`). Consume those as human anchors to narrow the phase boundaries —
especially KO/END, the detector's weakest — but robustly, since parents mis-tap. Trust model: a lone/
scattered tap is the LOWEST-quality signal; ≥2 parents agreeing within 10s are a TRUSTED consensus.
Fetch gated on TTT login (a TTT feature). PR #101.

**Design:** `event_tap_anchors.build_anchors` clusters taps per boundary (≥2 within `CLUSTER_WINDOW_S`
= high confidence at the cluster median, outliers excluded; else low). `fuse_phases(anchors=…)`
reconciles: HIGH snaps to a whistle within `ANCHOR_SNAP_SEC` (that whistle IS the event) or, for a
weak (symmetric-prior, no-whistle) KO, direct-fills it; LOW only snap-fills a weak boundary. No-op
when `anchors=None`, so the validated scorecard is byte-identical.

**Method:** no real parent taps exist for the historical GT games (parents tag live games; these were
tagged by Mark), so validate by SIMULATION — `training/data_prep/phase_anchor_eval.py` synthesizes
realistic taps at the true boundaries (reaction lag + jitter) under 5 scenarios and re-fuses WITH vs
WITHOUT anchors, scoring |err| vs human GT across 38 aligned GT games (misaligned excluded, as
phase_eval).

**Result (mean boundary error, s):**
- **trusted_cluster: 155.0 → 101.5 (−53.6)** — **kickoff 262.4 → 97.4 (−165), 17/38 improved, 0 worse**;
  ALL worse=5/152, max regression **+2s** (snap jitter).
- **lone / scattered / wrong_kickoff: 0 changed, 0 worse** — low-quality + mis-labelled taps are inert.
- confidently_offset (contrived: 3 parents all agree but ~40s off): −38.8 net, but 14/152 worse
  (max +52s).

**Failure found + hardened (the sim's payoff):** an early version snapped a "kickoff" tapped at the
2nd-half time to the wrong whistle — **KO 119s → 3235s**, a catastrophe the unit tests missed. Added a
STRUCTURAL-SANITY guard (an anchor may not cross its neighbouring boundaries) + CORROBORATION (an
uncorroborated high cluster can't move a confident boundary). Both verified by re-run: wrong_kickoff →
0 changed. Dropping the weak-KO direct-fill (to also zero-out confidently_offset) was tested and
reverted — it collapsed the real KO win from −165s to −29s, i.e. the win comes from correctly filling
KOs the detector had no whistle for.

**Conclusion:** validated — trusted clusters materially improve KO with no meaningful regression, bad
taps are safe. Residual: `confidently_offset` is net-positive but not zero-regression; it needs an
unrealistic input (agreeing parents saw the same event), so accepted as a known edge. Merged (#101);
anchors default off / TTT-login-gated.

## EXP-PHASE-16: combined-video KO — strategy sweep hits a signal ceiling at 9/14 (2026-07-01)

**Goal (Mark):** get combined-video (untrimmed, production) KO decently close so it can drive the
trim. Current: **9/14** within-10s. The 5 misses: 03.21 (+527, NO whistle in the video), 05.30-Fair
(-261) + 05.30-West (-279) (warm-up whistle picked over the real KO), 06.04 (-40) + 06.10 (-42)
(small, early). The two 05.30 games are the real targets. Swept candidate localizers on the cached
combined signals vs human GT.

**Strategies tried (all measured on the 14 GT combined games):**
- **Extended bench-dip clear** (skip a warm-up whistle when a field-empty starts up to 180s after it,
  long-clear threshold 72s): FIXES 05.30-West (its bench dip 2:50-4:14 disqualifies the 0:18 warm-up
  whistle) but BREAKS 05.10 (+258) -- 05.10's first-half dip 12:26-14:02 empties for 108s and reads
  as a pre-kickoff clear. Net 0. Bench dips and first-half stoppage dips are the same size/depth.
- **Settle-point** (KO must be after the field settles into sustained play): overshoots badly
  (05.28 +352, West-Seneca +175, 06.10 +300) -- first-half stoppages reset the "settle" point.
- **Halftime-anchored symmetric KO** (find the central halftime dip, HT=whistle before it, 2H=whistle
  after, KO = HT - (END - 2H)): **1/14** -- combined HT/2H localization is too noisy to anchor from.
- **AutoCam ball** (both the restart detector AND raw trajectory): the sidecar's center "restarts" are
  scattered mid-play passes, not the kickoff. Raw trajectory around KO: 05.30-Fair's ball settles
  static pre-KO but the kick is <180px (missed) and post-KO it's the lost-ball PARK; 05.30-West's ball
  never goes static (warm-up shooting is continuous); 05.31-Spencerport parks at a CORNER after KO
  (lost-ball park, not center). AutoCam ball is unusable as a kickoff signal.

**Why the 2 hard games are signal-limited:**
- 05.30-Fair: the warm-up whistle (1:45, field 0.60x busy) reads MORE like a kickoff than
  05.31-Spencerport's REAL KO (2:53, 0.56x busy) -- so no field-level threshold separates them
  (raising LO past 0.56 kills Spencerport before it fixes Fairport).
- 05.30-West: the bench dip is real but indistinguishable from a first-half stoppage (same on 05.10).
- 03.21: no whistle at all -- undoable from audio.

**Conclusion:** combined KO is at its **9/14 ceiling** for the available signals (whistle +
player-curve + AutoCam ball). No swept strategy beats it without an equal-and-opposite regression.
FOR TRIMMING this is workable: 9 trim-correct + 06.04/06.10 (small & EARLY -> the 4-min backup makes
them safe) = 11/14 auto-trim-safe; 03.21/05.30-Fair/05.30-West -> NTFY (and 05.30-Fair/West can't be
usefully auto-trimmed anyway -- their detected KO is a warm-up whistle ~4.5min early, so trim=KO-4min
collapses to ~0). **The real unlock is a reliable KICKOFF ball-restart** (ball placed static at the
center circle, then kicked) from the HOMEGROWN ball detector -- AutoCam's ball can't provide it, but
the in-house detector (once accurate) can, and that's the definitive KO signal these games need.

---

## EXP-PHASE-15: halftime anchor — dip→whistle-before, central dip, KO decoupled (2026-07-01)

**Goal (Mark's algorithm):** the field empties at halftime, but the empty region LAGS the whistle
(players run off AFTER the whistle), so HT = the WHISTLE JUST BEFORE the halftime field-empty; if no
whistle within ~2min before the decline, fall back to the DECLINE ONSET (first players leaving).
Parameterized the HT path (`PHASE_HT_MODE` / `PHASE_HT_DIP_SELECT` / `PHASE_HT_DECLINE_REJECT_SEC`)
and swept via the authoritative `phase_detect --predict` + `phase_eval` (NOT side-probes -- the
`phase_cache` disagrees with the scorecard on `--game` runs, so all tuning went through the scorecard).

**Why the old primary failed (W.Seneca):** the committed HT anchored on a central DOUBLE-blast with a
following dip. When the real halftime whistle is a SINGLE blast (W.Seneca 30:13), it's invisible to
that rule, which grabbed a stray double-blast (32:58) sitting INSIDE the empty region -> +163s late.

**Two findings from the sweep:**
1. **central dip >> longest dip.** `longest` scored 51/63 -- a first-half stoppage can be the longest
   field-empty (06.10: a deep 90s stoppage dip at 20:52 vs the real shallow central halftime dip that
   only reaches 3-4 players near center). Selecting the most-CENTRAL dip fixes this. `central`=54/63.
2. **HT feeds KO -> must decouple.** A naive dip-first (HT drives everything) fixed W.Seneca HT but
   BROKE its KO (+0->+175s): KO's symmetric prior `ko_sym = ht - (end - sh)` shifts with HT, which
   rerouted KO's fallback from `firstw` onto a warm-up whistle (3:07). Fix: keep `ht_ko` = the
   COMMITTED halftime estimate and anchor KO (`prek` window + `ko_sym`) to it, while the OUTPUT HT
   (+ 2H window) uses the dipfirst value. KO then never moves when HT is refined.

**Result (trimmed reolink, authoritative scorecard):** committed **53/63** -> dipfirst+central+decoupled
**54/63 (86%)**: KO 13/15 (unchanged), HT **14->15/18** (W.Seneca +163->-2s, no other HT regressed),
2H 14/18, END 12/12. Fixtures byte-identical (06.08/06.06-S/05.28 unchanged). Combined-video KO
unchanged **9/14** (KO decoupled). `HT_DECLINE_REJECT` 90/120/150 made no difference (a whistle is
always found in-window on these games); kept 120. Shipped as the default `PHASE_HT_MODE=dipfirst`.

**Still-missed HT (3):** 03.21 (no-whistle video), 05.30-Fairport (+18s, borderline), 06.07-BU15
(+306s -- degenerate: the field never clearly empties so there's no halftime dip to anchor). These
need a signal the whistle+curve doesn't carry; left as-is.

---

## EXP-PHASE-14: untrimmed-KO — opt-in localize + full-field BAND anchor (resolves 13) (2026-06-30)

**Goal:** push combined-video KO past ff04e6d's whistle-clear anchor without the trust-precision
collapse EXP-PHASE-13 hit when it tried the full-field anchor. Measured on the now-**16-game**
combined cache (`phase_cache_comb\`, `_combval.py --localize`), two-front gate = trimmed stays reolink
53/63 AND combined KO improves with few confident-wrong trims.

**Two changes vs ff04e6d (both load-bearing):**
1. **Opt-in `localize`** — `fuse_phases(signals, *, localize=False)`; the trimmed CLI + fixtures fuse
   with the default (never call `locate_game_block`), `detect_phases` (production, combined) passes
   `localize=True`. This makes the 53/63 guarantee **structural** (localization CANNOT touch trimmed),
   so `locate_game_block` is now free to be aggressive without any trimmed risk. Verified: trimmed
   reolink **53/63, END 12/12 byte-identical**; 3 fixtures pass; ruff clean.
2. **Full-field BAND anchor** — KO whistle = first blast with in-field count in
   `[0.55, 1.8] × busy` (busy = p75 of the smoothed curve) AND not-cleared. EXP-PHASE-13's full-field
   anchor used only a LOWER bound and over-localized onto the **dense both-teams-warming-up crowd**
   (06.06 warm-up ~3.0× busy read as "full" → +323s, ok=True). **The UPPER bound (1.8) is the fix:** it
   rejects that crowd (06.06-Lakefront-Sullivan warm-up 2.2-3.0× excluded, real KO 1.33× kept → -1s).

**Result (combined, 14 scoreable non-truncated reolink):** KO≤10s **9/14** (ff04e6d baseline 8/13);
**05.31-Spencerport -83s→-1s** (warm-up whistles at 0.44× busy dropped, kickoff at 0.56× kept — this
removed a ff04e6d DANGEROUS). Trust: **7/9 trusted-KOs correct**; the 2 wrong-trusted shrank from
ff04e6d's -83/-154s to **-40s (06.04) / -42s (06.10)** — small enough that the S3 NTFY screenshot
verify-loop catches them.

**Failed levers this session (reverted / not baked):**
- *Curve block_start = onset of longest sustained high-play run:* the steadiest high-play is MID-half,
  so block_start landed at 26:02 / 37:02 etc., overshooting KO. Scored 3-4/13. Confirms EXP-PHASE-13's
  "curve head too noisy" for onset localization.
- *Bench-dip via loosened clear (`LONG_CLEAR 120→72s`, `WINDOW 75→180s`):* fixed 05.30-Western-NY
  (bench dip 2:50-4:14 skips its 0:18 warm-up whistle → 4:55 -2s) but **broke 05.10** (+258s DANGEROUS
  — its first-half dip 12:26-14:02 empties for 108s and now reads as a lineup clear). **Bench dips and
  first-half stoppage dips are indistinguishable by duration/depth/timing at 12s sampling** — loosening
  to catch one catches the other. Kept the safe 120/75.
- *Center-ball-restart refinement of the anchor:* the sidecar's center restarts are scattered mid-play
  center passes, NOT the kickoff (06.04 has zero within 75s of its real KO). Not usable as a KO signal.
- *Symmetric-prior cross-check for trust (untrust when anchor disagrees with HT-(END-2H)):* flags the
  wrong 06.04 (anchor 7:57 vs sym 8:43) but ALSO the correct 05.31-Spencerport (its 2H is -104s, so sym
  is 104s off) — no clean separation. Not baked.

**Honest bottom line:** combined KO **9/14 (64%)** vs trimmed **53/63 (84%)** — a real +1 over ff04e6d
with smaller dangerous errors and a cleaner (structural) safety guarantee, but still short of the
trimmed bar. The 5 misses are each genuinely hard from audio + player-curve alone: no-whistle video
(03.21 — correctly untrusted), warm-up whistle over a populated field with no bench dip (05.30-Fair
— untrusted), bench dip indistinguishable from a first-half dip (05.30-West — untrusted), **missing
KO whistle** (06.04, real KO 8:37 silent → anchors the 7:57 pre-kick whistle, -40s), KO field-count
below the band (06.10, 0.50× busy, -42s). 3 of 5 are correctly UNTRUSTED (→ NTFY, no regression); 2
are trusted but only -40s and caught by human verify. This is the robust ceiling for this signal set;
a materially higher combined KO needs a signal the 180° audio+curve doesn't carry (e.g. the homegrown
ball detector's kickoff-restart once it exists, or a finer head curve that still can't separate the
two dip types here).

---

## EXP-PHASE-13: untrimmed-KO fix — locate_game_block + KO anchor (partial) (2026-06-18)

**Goal:** get KO reliable on the untrimmed combined video (the production regime) toward the trimmed
53/63 bar. Iterated against CACHED combined signals (`G:\ballresearch\phase_cache_comb\`); two-front
gate = trimmed scorecard stays reolink 53/63 (END 12/12) AND combined KO improves.
**What works (committed b7af1a1 -> 1d3bc1d -> ff04e6d):**
- `locate_game_block` wraps the validated fusion (renamed `_fuse_core`): on a combined video it anchors
  to the KICKOFF WHISTLE (first blast not followed by a sustained field-clear -- warm-up whistles
  precede the teams lining up), slices signals to the game block, fuses, offsets back. Trimmed uploads
  return `(0,dur)` = byte-identical no-op (53/63 + fixtures unaffected -- verified).
- KO = the kickoff-whistle anchor on a localized video (first blast >= block_start), not the fuse's
  within-block re-derivation (05.27 KO +54 -> -1s).
- `ko_trustworthy` = whistle/kick-anchored AND not-far-from-onset AND (ok OR localized) -- so a youth
  game with an exact KO but failed HT/2H (ok=False) is still trusted if localized; wrong-KO games stay
  untrusted (fall back to NTFY, never a confident mis-trim).
**Result (combined scorecard, 8 games cached so far):** KO<=10s **4/8** (05.10 -2, 05.07 -1, 05.27 -1,
05.28 -3); trustworthy **3/8, precision 3/3** (never trusts a wrong KO). Trimmed **53/63 unchanged**.
**Failed approach (reverted):** replacing the whistle anchor with a PLAYER-CURVE "first sustained
full-field run" onset (Mark's bench-dip idea) -- the head of the curve is too noisy, so it localized
TRIMMED videos and picked wrong onsets on combined: trimmed 53->44/63, combined KO 4->1/8. The
whistle-clear anchor is more reliable than the player-curve onset. Reverted cleanly.
**Honest state:** SAFE (no confident-wrong KO; wrong ones -> NTFY) with zero trimmed regression, but
NOT yet at the trimmed bar on accuracy. Hard remaining cases: no-whistle combined video (03.21,
`n_blasts=0`, flat curve -- likely un-doable, NTFY), recording-starts-at-kickoff (05.09 GT KO=0:00,
first whistle is a foul -- edge case even trimmed), warm-up whistle mistaken for KO where no clear
follows it (05.30 -261s, the whistle-clear heuristic's weak spot). Full 18-game combined scorecard
pending the signal cache (`_cacheall` on the box, ~10 games left).

**More attempts (Mark's hints: "one clear anchor per game, work forward/back" + "combine factors:
whistle + players + static ball + timing") -- net-negative, reverted to ff04e6d:**
- *Opt-in `localize` flag* (fuse_phases localize=False default; detect_phases -> True): CLEAN idea --
  makes the trimmed 53/63 guarantee STRUCTURAL (localization never runs on trimmed) instead of relying
  on a no-op. Worth keeping if this line is resumed.
- *Combination anchor: KO whistle = first whistle over a FULL field* (both teams on; warm-up whistles
  are over a sparse field). Verified the discriminator on the signals: 05.30 kickoff 6:04 @ 11 players
  vs warm-up 1:45/2:03/2:55 @ 4-6; 05.27 9:25 @ 7 + ball-restart vs warm-up 4:50 @ 2. It **fixed 05.30
  combined KO -261 -> -2s** (real win) and lifted combined KO 4/8 -> 6/13. BUT it **OVER-localizes**:
  it localized ~7/13 games, ~5 to a WRONG block, and those pass the sanity gate self-consistently
  (06.06 +323, 05.31 +72, both ok=True), so trust precision collapsed (whistle-clear: 3/3 -> full-
  field: 2/7). No trust rule (require ok / require localized / not-far) recovered precision because the
  wrong localizations look valid. The accuracy gain is not worth the safety loss.
**Conclusion:** the whistle-clear localization (ff04e6d) is the precise sweet spot -- conservative
(localizes few games, but correctly -> precise trust), which is what a safe auto-trim needs. Reliably
LOCALIZING the game block on an untrimmed combined video is the hard open problem; player-count-based
anchors (onset, full-field) improve accuracy but over-localize. **Honest bottom line: combined KO is
accurate for ~half the whistle-capable games and SAFE (wrong KO -> NTFY) with zero trimmed regression,
but NOT at the trimmed 53/63 bar.** Next idea if resumed: a per-game CONFIDENCE score for the block
localization (only trust high-confidence blocks) rather than a binary localized flag; or anchor on the
HALFTIME multi-whistle (loud, mid-game) where present and derive KO from the equal-halves structure.

---

## EXP-PHASE-12: untrimmed (combined-video) KO fails — diagnosis (2026-06-18)

**Why:** productionizing phase detection to (maybe) replace the NTFY game-start question needs the
detector to run on the **untrimmed combined video** (warm-up -> game -> post-game), not the trimmed
game-only upload it was tuned on. Validation via `detect_phases` on combined videos (real polygon)
vs human GT: 05.10 KO -2s / 05.07 -1s (clean), but 03.21 **KO +527s with ok=True**, 05.09/05.27/05.28
hundreds of s off. So it does NOT reliably find the game start untrimmed.
**Method:** cached combined-video signals (`G:\ballresearch\phase_cache_comb\<gid>.json`) + dumped
`fuse_phases(debug=True)` internals for 03.21 (confident-wrong late), 05.27 (early, ok=False), 05.10
(clean) — so fix iteration is now instant.
**Three mechanisms:**
1. **No-whistle combined video (03.21):** `n_blasts=0` (the combined.mp4 audio lacks the whistle even
   though the trimmed upload has it) + very low counts (`med_play=4`, so the 0.6x full-field gate is
   meaningless). KO falls to the symmetric prior; HT is also misplaced; the result is internally
   symmetric (h1≈h2≈31.6) so the **sanity gate passes (ok=True)** -> a confidently-wrong shifted fit.
   This is the dangerous case (would trim 9 min into the game).
2. **Warm-up / mid-half artifacts (05.27):** first whistle 4:50 is warm-up, first multi 25:37 is a
   first-half foul; the "first whistle / first multi" heuristics grab them instead of the real HT
   (49:28), shifting everything early. Sanity gate caught it (ok=False -> NTFY). Safe but wrong.
3. **Clean structure works (05.10):** one unambiguous HT dip (field empty 51:00-57:00) + KO whistle
   10:04 -> all four within 3s. Proof the fusion logic is fine WHEN the game block is located.
**Root cause:** the fusion assumes the game fills the video (true trimmed, false untrimmed) via
"first X" heuristics; it never LOCATES the game block. The robust anchor is the **one halftime dip
with sustained play on BOTH sides** (warm-up has no such dip; post-game empties but never refills).
**Fix plan (next):** (a) locate the game block = longest sustained-activity region bracketing the one
central full-both-sides HT dip; run the (validated) fusion anchored to that block, ignoring warm-up
/post-game tails. (b) anchor KO/HT to whistles/multis WITHIN the block, not the first in the video.
(c) SAFETY: a KO not whistle/kick-anchored (symmetric-only) or a no-whistle combined video is NOT
trustworthy for trimming -> force NTFY fallback; and reject a KO far from the block's activity onset
(kills 03.21's ok=True). (d) **Two-front validation per change: must not regress trimmed 53/63** while
improving combined-video KO. Approach cleanly as a `locate_game_block` pre-step so the validated
`fuse_phases` core stays intact.

---

## EXP-PHASE-11: wind whistle FPs — no global gate possible; trailing-gust END guard (2026-06-30)

**Goal (Mark): handle wind globally, not END-only — "wind is usually all game."** Correct: the global
loudness gate (below) fixed not just 06.06-S END but also West Seneca HT (+163->-2) and 05.30 HT
(+18->0), both wind FPs at other boundaries. Wind FPs are an all-game phenomenon.
**Infrastructure:** `whistle_blasts` now also returns a per-blast peak-loudness ratio (max frame
loudness / p75); cached as `blast_loud` and populated for all GT games via a new `--recompute-whistle`
(re-runs only the audio whistle pass, keeps the slow cached player-curve + ball signals).
**Global gate -- FAILED.** Swept a global "drop blasts < Nx p75" gate: at 12x, reolink within-10s
**52 -> 44** -- it fixed 3 (06.06 END, West Seneca HT, 05.30 HT) but BROKE 7 (05.07 HT +449, 05.27
KO-275/END-747, 05.31, 06.01, 06.07, 06.10) by dropping their quieter REAL whistles. Tested
pitch-stability (loudness-independent) as the alternative axis -- ALSO fails: in poor-audio games the
real whistle is itself faint AND pitch-smeared (05.07 HT 6.8x / 394Hz spread; West Seneca END 5.5x /
289Hz; vs 06.06 clean whistles 17-25x / 11-31Hz). A wind-masked / distant-ref whistle is
indistinguishable from a wind gust by loudness OR pitch -- the separating information is not in the
audio, so ANY global gate necessarily over-prunes those games.
**Fix that works -- trailing-gust END guard.** Wind resonating at the ref pitch fires a FAINT "multi"
in the minute AFTER the real full-time whistle (06.06-S: real 80:14 ~30x, gust multis 80:51/81:03
~8-11x; the "last late multi" rule grabbed 81:03 -> +48s). Override the last late multi ONLY when it
is faint (< LOUD_GATE x p75) AND a loud multi sits within 120s before it -- that loud multi is
full-time, the faint trailer is wind. A loud multi is NOT preferred over a quiet final whistle in
general (that regressed an END to +746s by picking a loud mid-half foul), so quiet-final-whistle games
are untouched.
**Result (reolink, vs human GT):** 06.06-S END +48 -> **-1s**; reolink END **11 -> 12/12** (every
scoreable END within 3.1s), within-10s **52 -> 53/63 (84%)**, median 1.1s, KO/HT/2H unchanged, zero
regressions. LOUD_GATE default 12x (env `PHASE_LOUD_GATE`).
**Open:** HT wind FPs (West Seneca +163, 05.30 +18) are a different mechanism (not "trailing" a louder
whistle) and a loudness gate there would drop 05.07's faint 6.8x real HT -- deferred; needs a
non-loudness signal (e.g. corroborate HT against the player dip, which it already partly does).

---

## EXP-PHASE-10: remaining two full-file misses are within-tolerance edge cases (2026-06-30)

After EXP-PHASE-07/08/09 the only full-file (non-truncated) reolink misses left are both small and
both within Mark's 3-min bound; chased both, neither warrants a risky change now.

**06.06-S END +48s (whistle false positives).** GT END 80:15 is a real, loud, pitch-stable whistle
(verified by re-decoding the audio: f0 locked at 3575-3618 Hz, loudness up to ~30-37x the 75th-pct,
tonality 0.78-0.84). Mark confirmed by ear there is NO whistle at the two later detected "multis"
(80:46, 81:03). Re-decode shows those are rising/falling-pitch CROWD/VOICE sounds (80:46 glides
2842->3230 Hz, 80:55 sweeps up then down) at only ~4-12x p75 -- the STFT's +/-300 Hz pitch band
catches part of the glide, so `whistle_blasts` fires a false multi, and the END "last late multi"
rule latches onto post-game crowd noise. **Clean discriminators: pitch STABILITY (a ref whistle
holds one pitch; a cheer glides) and LOUDNESS (real is ~3-8x louder).** Not fixed: it needs a
pitch-stability/loudness filter inside `whistle_blasts`, which changes global blast detection and
requires re-decoding every game's audio + re-verifying the 52/63 -- real regression risk for a miss
already inside tolerance. Logged as the next whistle-precision improvement.

*Tried (Mark's suggestion): wind-noise reduction as a pre-filter (PR #77 `experiment/wind-noise-
reduction`).* Re-decoded 06.06-S at 16 kHz and ran the whistle STFT on the original vs `afftdn=nf=-25`,
`hp120+afftdn`, `afftdn=nr=25:nf=-30:tn=1` (tracked), and `hp120+afftdn_tracked`. **Negative result:**
every afftdn variant keeps the real 80:15 whistle BUT leaves the 80:51/80:55/81:03 gust FPs intact
(identical end-window blasts), and the tracked variant ADDS blasts (23->31 multis). afftdn removes a
broadband noise FLOOR; these FPs are a TONAL transient (a gust resonating through the mic at ~3.4 kHz),
which spectral denoise is designed to preserve -- consistent with WIND_NOISE.md (afftdn shaves only
~0.7-4.6 dB off the gust; the `noisereduce` hybrid, not on the box, is "modest" too). Conclusion: wind
denoise is the wrong tool for tonal-gust whistle FPs; the discriminator must be pitch-stability +
loudness inside `whistle_blasts`, not audio pre-cleaning.

*Follow-up experiment (06.06-S only, per Mark): a per-blast LOUDNESS gate.* Measured per detected
blast its peak loudness (xp75 frame loudness) and pitch spread. The real END whistle (80:14/80:15)
is 16-21x p75; the three gust FPs (80:51/80:55/81:03) are only 5.5-9.7x. A peak-loudness gate of
**>=10-14x p75** (comfortable margin) kills all three FPs and resolves END to 80:14 (= GT 80:15, +48s
-> ~+1s) while KEEPING every real boundary (KO 17.6/14.3x, HT 20.9/24.8x, 2H 17.4x). Loudness alone
sufficed -- pitch-stability not even needed. A referee whistle is genuinely loud + pitch-locked; the
gust whistle-through-the-mic is quieter + sweeps. **Caveat before rollout:** a global loudness gate
could drop a quieter wind-masked real whistle in another game, so a full rollout needs --recompute on
all games + full scorecard re-verify (at native 44.1kHz, re-calibrating the multiple). On 06.06-S the
fix is clean and unambiguous.

**06.10 KO +24s (upload trimmed exactly to the kickoff).** Mark confirmed the YouTube/trimmed upload
starts right at the kick (field full of players from frame 0; GT KO = 0:00 trimmed). There is no
pre-kick signature (no static-center-ball restart, no kickoff whistle in-frame) so the detector
latches onto the first in-play foul whistle at 0:24. A guard (`ko_sym<=15` + field-full-at-frame-0
-> KO=0:00) both missed 06.10 (its ko_sym is ~18s) and regressed another game's KO (13->12/15), so
it was reverted. Left as a within-tolerance limitation; not truncation (the kickoff IS captured, at
frame 0) so it is not flagged.

**State:** reolink within-10s **52/63 (83%)**, median **1.1s**; KO 13/15, HT 14/18, 2H 14/18, END
11/12. All large full-file misses fixed (EXP-PHASE-07/08/09). The remaining HT/2H reolink misses are
all in truncated games (06.07 BU15/Lakefront, West Seneca, 03.21) or dahua (no whistle).

---

## EXP-PHASE-09: 2H anchors to the FULL-field refill whistle (closes 05.28) (2026-06-30)

**Hypothesis (verified on cached signals):** 05.28 2H -92s. The 2H whistle picker took the first
whistle after the HT dip offset on2, which is a stray warm-up whistle blown while players are still
trickling back (48:06, only 5 in-field). The REAL 2H whistle is a detected blast at 49:38 (= human
GT) where the field is FULL (11 in-field = med_play); the ball model missed the 2H restart, and the
only nearby restart is a no-whistle one 86s later (51:04, a mid-half re-acquire).
**Method:** (1) `w2_full` = first whistle after on2 where the field is full (`pcount_at >= 0.6x
med_play`); use it as the refill whistle, falling back to the first whistle after on2 when none is
full. (2) when the refill whistle is full-field-corroborated, narrow the "trust a later no-whistle
ball restart over the whistle" window from 90s to **60s** -- the kickoff ball moves within ~a minute,
so a restart 86s later (05.28 51:04) is mid-half (keep the whistle), but a restart ~25s after a
pre-kickoff whistle (06.10 50:48 after 50:23) is still the kickoff (keep the restart). Isolated to
the 2H whistle/override; KO/HT/END untouched.
**Result (reolink, vs human GT):** 05.28 2H -92->**+0s**, 06.10 2H held at **-3s** (the gap-based
60s window distinguishes 06.10's 25s restart from 05.28's 86s). Reolink within-10s 51->**52/63
(81->83%)**: 2H 13->14/18, KO/HT/END unchanged, median 1.1s. No regressions.
**Conclusion:** the 2H kickoff whistle is the first whistle once BOTH teams are back in formation,
not the first whistle while players trickle on; and "is the nearby no-whistle restart the kickoff?"
is a function of its gap to that whistle (~within a minute). Remaining full-file reolink misses:
06.06-S END +48, 06.10 KO +24, and one large KO outlier (max 335s, a truncated game).

---

## EXP-PHASE-08: 2H prefers the whistled kickoff over a preceding warm-up restart (2026-06-30)

**Hypothesis (walking 05.27 with the worklist):** 05.27 2H missed by -60s -- the detector picked a
no-whistle full restart at 54:04, but the real 2H kickoff (human GT) is 60s later at 55:04, a
full+center restart with a tightly-coupled whistle (ball 55:03, whistle 55:04). The 2H picker took
the FIRST kickoff()-passing restart in the break window, so the warm-up restart beat the whistled
kickoff right after it. The KO path already handles this (nxt_ko: a no-whistle restart immediately
followed by a whistled kickoff -> trust the whistled one); the 2H path lacked it.
**Method:** before the 2H decision, if the first break-window kickoff is no-whistle, look <=75s ahead
for a whistled full kickoff and prefer it. Isolated to the 2H selection; KO/HT/END untouched.
**Result (reolink, vs human GT):** 05.27 2H -60->**+0s**. Reolink within-10s 50->**51/63 (79->81%)**:
2H 12->13/18, KO/HT/END unchanged, median 1.3->1.1s. No regressions.
**Conclusion:** the warm-up-restart-then-whistled-kickoff pattern needed the same nxt_ko treatment in
the 2H path as in KO. Does NOT help 05.28 (real 2H 49:38 has NO detected ball restart and a stray
whistle 92s before it -- the EXP-PHASE-01 ball-in-play gap, still open).

---

## EXP-PHASE-07: 06.08 KO/END — under-counted opening kickoff + collapsed final whistle (2026-06-30)

**Hypothesis (from walking 06.08 with Mark):** 06.08 (full, non-truncated) missed both KO (+163s)
and END (+315s) while HT/2H were already +0. Mark confirmed from YouTube: the real kickoff is a
center static-ball restart (1:57) + whistle (1:59), and the final whistle at 93:57 is a clear
multi ("twit..twit.....tweeeeeeeeet") with players staying on the field afterward.
**Root cause (verified on cached signals, instant re-fuse):** (1) **KO** — the 1:59 kickoff was a
CENTER restart with a tightly-coupled whistle (1.8s) but only **5** in-field detections, under the
0.6x-median full-field gate, so it was dropped from `prek` and KO jumped to a 4:42 warm-up restart.
(2) **END** — the final multi-blow collapsed into a **single** cached blast (5639.0s = 93:59; the
whole game had only one detected "multi", at 66:22, where two blasts happened to land 0.4s apart),
and because players stay on the field after full-time the player-curve last-play `off2` sat at
end-of-file (99:12). END only anchored to multis -> fell through to `off2` = +315s.
**Method:** (1) include in `prek` a center restart with a whistle <=3s AND a MODERATE field
(>=0.4x median crowd) — accepts the under-counted real kickoff but still rejects an early warm-up
whistle over a near-empty field (05.31 Spencerport: a coincidental tight whistle at 0:30 over just
2 players is NOT the kickoff; real KO is the full restart at 1:45). (2) END: when no late multi
registers, anchor to the LAST whistle blast in the valid 2H window (refs don't whistle after
full-time, so the last blast IS the collapsed final whistle). Both changes isolated to the KO/END
branches; HT/2H/sanity-gate untouched.
**Result (reolink, vs human GT):** 06.08 KO +163->**-1s**, END +315->**+1s** (HT/2H stayed +0; all
four now within 1s). 05.31 Spencerport KO restored (warm-up regression caught + gated: 0:30 ->
**2:53-8s**). Reolink within-10s **48->50/63 (76->79%)**: KO 12->13/15, END 10->11/12, HT/2H
unchanged, median 1.3s. Zero protected-boundary regressions.
**Conclusion:** the full-field gate must flex for the opening kickoff (corroborated by a tight
whistle), and END must fall back from "multi" to "last whistle of the game" when the final
multi-blow merges into one blast. Remaining full-file reolink misses: 06.10 KO +24, the youth
warm-up-on-field 2H gap (05.28, EXP-PHASE-01), and one large KO outlier (max 335s).

---

## EXP-PHASE-06: anchor HT to the dip-preceding whistle + whistle-only 2H kickoff (2026-06-30)

**Hypothesis (Mark):** the HT-selection family (05.27/05.28) and 06.01 2H miss even though the
spot-on whistle is already a detected blast — HT leans on the player-dip (which lags the whistle by
~2min as youth players walk off slowly) and 2H requires a center ball-restart (sometimes missed by
the ball model). Both ignore the whistle. Halftime is usually a multi-blow but its spread varies
(~10s spread, or a tight tweet-tweet that the <0.4s merge collapses to one), so the 5s multi
clustering registers neither.
**Method (fusion-only, cached signals -> instant re-fuse, no re-decode):** (1) recompute wide
multis from the cached `blasts` with an 11s gap (kept the 5s `multis` for htc/END). (2) when no
central 5s-multi-with-following-dip registers, anchor HT to the whistle PRECEDING the halftime dip:
take the dip-cluster onset (merge runs split by a brief mid-break refill, <=60s gap), look back 180s
(not the old 60s snap to the lagging dip) for a wide multi, else the nearest single blast. (3) 2H =
the first whistle once the field has refilled (HT dip offset `on2`); a no-whistle ball restart in the
break window only outranks it if it is <=90s after that whistle (else it is a mid-half re-acquire,
not the kickoff). Validated on the box (`--predict --gt-only` + `phase_eval --human-only`).
**Result (reolink, vs human GT):** 05.27 HT +139->**-1s**, 05.28 HT +194->**-2s**, 06.01 2H
+379->**0s** (bonus 06.08 2H -191->**0s**). Non-truncated reolink HT within-10s **9/11 -> 11/11**;
reolink within-10s 44/63 -> **48/63 (70->76%)**, median 1.6->1.3s (HT 12->14, 2H 10->12, END max
1655->314s). No protected (non-truncated reolink) boundary regressed; KO/END/sanity-gate untouched.
**Conclusion:** anchoring HT to the dip-preceding whistle and accepting a whistle-only 2H kickoff
fixes the HT-selection family + 06.01 without disturbing the KO-signature work. Cost: heat 05.28 2H
+85->-92s (non-protected; both wrong either way — a youth warm-up-on-field game with a stray whistle
after the short dip, real 2H 3min later). Remaining: 05.28 2H and the youth warm-up-on-field 2H/END
gap (EXP-PHASE-01) still need ball-in-play, and dahua KO/2H still lack a whistle.

---

## EXP-PHASE-05: signature-driven kickoff (KO/2H) + interactive GT review (2026-06-30)

**Hypothesis (Mark):** apply the kickoff signature throughout the video — whistle + static ball at
centre + (most) players on the field, then motion — to fix KO/2H, which were grabbing wrong restarts.
**Method:** interactive review (Mark verified each off-boundary on YouTube against the trimmed-raw
upload = the YouTube video, so YT time = trimmed-file time directly). Corrected GT where wrong, then
two detector passes (commits 5026ec5, 6461a53): KO/2H = a CENTER ball-restart corroborated by a
kickoff whistle (tight to the ball ≤3s, OR part of the 2H "ready" multi-blast, OR no-whistle for
wind-masked games) AND a near-full field (relative threshold ~0.6×median in-play count, since YOLO
under-counts at distance so absolute counts can't separate one-team from both-team restarts). KO =
first pre-HT kick, 2H = first kick in the break window. Eval scores truncated games per-boundary.
**Result (reolink within-10s):** KO 6→**12/15**, 2H 5→**10/18**, HT 12/18, END 10/12, median **1.6s**.
Exact fixes: West Seneca KO +384→0, 05.27 KO +114→0, 05.30 2H -80→-1, 05.31 2H -50→-1, 06.06-S KO
-31→0, 05.28 KO -13→-1. GT errors caught: 06.06 KO (truncated), 06.10 HT (42:23→44:25, detector was
right), West Seneca END (truncated).
**Conclusion:** the kickoff signature works; the discriminating feature is a whistle TIGHT to the
ball (positioning whistles fire ~50-80s early), not the player count (too noisy at distance). Cost:
the 2H discriminator regressed 06.01 2H (+379) and the long/odd 06.08 (rejected) — to clean up next,
along with the HT-selection family (05.27/05.28/West Seneca grab the wrong halftime multi).

---

## EXP-PHASE-04: full-coverage phase eval — score ALL GT games, not reolink-only (2026-06-30)

**Hypothesis:** the prior "48% within-10s" was measured on ~13 reolink-heavy games (the detector was
reolink-only). The honest number across the full human-GT set (38 scoreable games, 25 dahua + 13 reolink)
will be lower, and will localize where the detector actually fails.
**Method:** dropped the reolink-only filter; made the dahua paths fire — `find_fullframe_video` (dahua
`combined_video` is often missing/odd), AUTO-detected frame + ball orientation (vote in-field persons/
detections both ways; `video_rotation` is inconsistent across the 2024 archive — raw files tagged rot=0),
`thread_type=AUTO` decode, no-audio crash guard, and `segment()` placeholder + central-multi HT for youth
games whose field never empties. Recomputed the per-game signal cache for all 25 dahua games on the box
(CPU, ~10 min/game; 4-core box, single worker — 2 parallel workers oversubscribe and are slower). Added a
video<->GT misalignment guard: exclude games where |voff+vdur - gt_dur| > 120 s (incomplete or multi-game
videos can't map to GT). Vision-verified orientation + that predictions land on real events.
**Result (vs human GT, 33 aligned games; 5 dahua excluded as data issues):**
- combined within-10s **33%** (43/132), median 49 s.
- **reolink 60%** (31/52), median **3 s** — UP from the old reolink "48%" (no-play robustness +
  symmetric HT/2H pair selection fixed 05.07/05.30/06.06). Near-perfect: Upper_90, 06.04, 05.07, 05.30.
- **dahua 15%** (12/80), median 128 s — HT/END decent (player-curve dip + order: 06.15 HT -1 s, 09.21
  HT -9 s, 07.02 2H -11 s), KO/2H poor (no usable whistle at 8-16 kHz; KO/2H lean on AutoCam ball
  restarts whose center-circle cluster is often mis-detected = a goal-area restart).
- 5 misaligned-video games excluded (flash 06.01 -20 min incomplete `-raw`, 10.04, 10.13 +76 min
  multi-game `combined.mp4`, heat 05.19, 05.28).
**Conclusion:** coverage solved; reolink phase detection is genuinely good (60%, median 3 s) and the
whistle is the load-bearing signal. Dahua is the open problem: with no whistle, KO/2H need a better
center-restart selection or a player-curve 2H (field-refill after the HT dip) rather than the noisy
AutoCam ball restart. The honest full-set number is 33%, not 48% — the 48% was a reolink-subset artifact.

## EXP-PHASE-03: multi-signal phase detector (player-curve + whistle), half-length AGNOSTIC (2026-06-28)

**Hypothesis (Mark):** the fixed-40-min assumption (EXP-PHASE-02) doesn't generalize — younger ages play 30-min
halves, tournaments are shorter, flash halves vary. Instead combine signals that don't assume a half length:
the WHISTLE (multi-blast = halftime/end) AND a PLAYER detector inside the field mask (the ~20 field players are
OFF the field during the halftime break). Add ball-at-center for kickoffs.
**Method (`G:\ballresearch\phase_detect.py`):** backbone = **player-on-field count curve**. Sample ~1 frame / 10s,
run `yolo26n.onnx` (COCO, person class, 1280px) and count persons whose foot-point is inside the field polygon.
Curve goes low -> high(1H) -> **low(halftime, field empties)** -> high(2H) -> low, with NO half-length assumption.
Segment: halftime = the longest SUSTAINED low-player run in the middle 20-80% of the game; 1H = first-play->halftime,
2H = halftime->last-play. **Whistle refinement** (when 44.1kHz trimmed audio exists): HT = multi-blast at the dip,
2H = first whistle once the field refills, END = largest late multi-blast with KO>=0 (the KO>=0 cap excludes
post-game whistles), KO = HT-(END-2H) [measured half] snapped to a kickoff whistle if one is right there. Curve +
whistle cached per game (`phase_cache/<gid>.json`) so the expensive pass runs once; idempotent. Sanity gate rejects
implausible fits (KO<0, break not 2.5-18min, half not 15-50min, |h1-h2|>3min) — never writes garbage. Writes
game_state source=`phase_video_whistle` + `_phase_meta`.
**Gotchas found + fixed:** (1) trimmed uploads are **1920x1080 anamorphically squeezed** from the 7680x2160 source
-> scale the field polygon by (0.25, 0.5), NOT uniform. (2) full-frame yolo@1280 over a 7680-wide panorama misses
small field players (catches only big sideline people); the 1920-wide trimmed file needs no tiling. (3) symmetry-only
half selection is fragile (EXP-PHASE-02's lesson); the player halftime-dip removes the half-length guess entirely.
(4) post-game whistles fooled END until the KO>=0 cap. (5) detached `python > log` is block-buffered -> use `-u`.
**Result — 6/04 Irondequoit (frame GT KO=2:37 HT=42:37 2H=50:16 END=90:09):** KO 2:41 (+4s), HT 42:34 (-3s),
2H 50:16 (0s), END 90:09 (0s); h1=h2=39.9. **All four within ~4s — never assuming 40min.** Batch over all 24
reolink: **2 new clean auto-phased games** beyond EXP-PHASE-02's 6 — heat 5/28 Fairport (40-min: KO 4:09/HT 44:17/
2H 48:06/END 88:13) and heat 6/06 Lakefront_Sullivan (**36-min** halves: KO 4:25/HT 41:05/2H 44:51/END 81:03) —
both vision-verified (kickoff = teams in formation, mid-halftime = empty field). The 36-min game proves the
half-agnostic claim. Sanity gate correctly REJECTED 3 fits whose "halftime" was a 1.3-1.8min stoppage (5/30 Fairport,
5/31 West_Seneca_14.40, 6/07 Lakefront_home) and flagged 6/08 Hilton_Flaitz (American-football-marked field, sparse
kickoff, 45-min/99-min — same game removed in EXP-PHASE-02). 3 games had no detectable halftime dip
(no-play-plateau: 5/30 Western_NY_Flash, 6/06 Fairport, 6/07 BU15).
**File-location fix (2026-06-28):** the detector first globbed `D:\soccer-cam-storage` and reported 6 "missing file"
games — WRONG. The canonical source is the **F: archive folder** (`F:\Heat_2012s\<date>...`, `F:\Flash_2013s\...`,
where game.json lives): named-subdir trimmed upload + `-raw` sibling + combined.mp4 + match_info.ini. Fixed
`files_offsets` to read the F: archive folder. After the fix: flash 3/21 processes (indoor-dome game, no whistle ->
player-curve-only, asymmetric -> rejected, manual). The rest were NOT real missing files: heat 5/07-16:12, flash
5/10-11:19, 5/30 Spencerport-19:43, 5/31 WSeneca-12:40 are **aborted false-starts** (combined.mp4 is 32-87s; each has
a real-game sibling or no game); heat 7/22 is a 2025 raw-segments-only recording (no trimmed/combined). So no real
2026 game is missing its file on F:.
**Conclusion:** the multi-signal detector clears the 10s bar without any half-length assumption, generalizes to
shorter-half games, and works on 16kHz games via the player curve alone (whistle-cut audio no longer fatal). It is
the general phase detector; EXP-PHASE-02's whistle+40min stays valid for the 6 heat-40 games already anchored.
**Failure mode found — HALFTIME WARM-UP ON FIELD (2026-06-28):** cross-checked the detector (--force, no write)
against the 3 manually-set `play_windows` games. The manuals confirm Mark's variable-half point: flash 5/09 ~34min,
flash 5/10 ~38min, heat 5/31 Spencerport ~30min halves (a fixed-40 detector misses all 3). The detector gets HT
(halftime START) RIGHT on all 3 (flash 5/10 42:09 vs PW 42.2min; Spencerport 31:50 vs 31.9min) but UNDER-estimates
2H and END because these teams **warm up on the field during halftime** -> the player-count dip is short (just the
initial clear-off), the "field refills" signal fires on the warm-up, so the 2nd half comes out too short. The sanity
gate REJECTED 2 of 3 (break <2.5min) but flash 5/10 slipped through as a symmetric-but-wrong "OK" (32min half vs real
38; END 12min early). **Lesson: the numeric gate is necessary but NOT sufficient — vision-verification (kickoff
formation + empty-halftime frame) is the real gate; only write what's been eyeballed.** The 2 written games (5/28,
6/06 Sullivan) were vision-verified; the 3 play_windows manuals are kept (better than the warm-up-shortened auto fit).
**Fix (needs ball signal):** distinguish halftime warm-up from real 2nd-half play via ball-in-play (ball moving /
ball-at-center kickoff), which separates scattered warm-up from coordinated play — pending the marathon's ball
detections reaching 2026 games. Until then, warm-up-on-field games stay manual.
**Next:** combined.mp4 fallback for the no-trimmed-file games (player curve works on 16kHz video; warmup at file
start needs handling); ball-at-center kickoff to fix the warm-up 2H/END + rescue the no-dip / rejected games
(post-marathon-on-2026); locate recordings for the 2 no-rec-dir games. Player counts could also be cached as a
per-game signal for the active-play training filter.

---

## EXP-PHASE-02: whistle template + 40-min-structure phase detector — <1s on all boundaries (2026-06-28)

**Hypothesis (Mark):** the referee whistle marks halftime/end; combined with the fixed half length (40 min for
Guzzetta/heat 2026) the whole phase structure can be pinned to ~seconds — far better than the detection-density model
(EXP-PHASE-01, ~4 min) or generic FFT-band whistle detection (EXP-012, ~2 min, "too noisy").
**Method (`G:\ballresearch\whistle_phases2.py`):** (1) STFT the game audio; per frame find the dominant 2-5.5kHz peak +
its tonality (band-energy / 1-8kHz). (2) The generic FFT band fails (crowd/wind), and matching the *exact* kickoff
spectrum fails (pea-whistle warble) — so detect by **pitch**: the ref uses ONE whistle pitch all game. (3) **Self-
calibrate the pitch via the 40-min lock:** try each candidate pitch; keep the one whose whistles form a 'halftime' with a
whistle ~40min BEFORE (kickoff) and a 2nd-half kickoff 4-13min after whose +40min lands on a whistle (end). Derive
kickoff = HT-40, end = 2H+40, snap to whistles.
**Result — 6/04 Irondequoit (Guzzetta), vs Mark's frame-precise GT:** winning pitch ~4250Hz (fit 3/3, 34 whistles).
kickoff 2:37.8 (GT 2:37), halftime 42:37.8 (GT 42:37, 1st half=40.00m), 2nd kickoff 50:16.2 (GT 50:16), end 90:09.2
(GT 1:30:09). **All four boundaries within ~1 second.** The kickoff whistle alone is weak (lower tonality under kickoff
crowd noise), but it's recovered by the HT-40 derivation + corroboration, so no separate kickoff detector is needed when
the structure locks.
**Conclusion:** whistle-pitch + 40-min self-calibrating structure-fit clears the 10s bar (~1s here). This is THE phase
detector for the 40-min-half games. Generalize/batch across Guzzetta 2026; for games where the kickoff/2H whistle is
missing, sharpen the derived restart with the ball-at-center cue in the tight post-derivation window (Mark's hint).
Supersedes EXP-PHASE-01 (density model) and the play_windows seeds for these games.
**Audio-rate scoping (important):** the whistle (~4350Hz) needs Nyquist > 4.4kHz. **2024-25 Dahua games have 8000Hz
audio (Nyquist 4kHz) -> the whistle band is CUT OFF**, so the whistle detector CANNOT work on them (this is exactly why
EXP-012 found whistles unreliable — it was run on 8kHz Dahua). **2026 Reolink: trimmed/upload audio 44.1kHz, combined.mp4
16kHz (Nyquist 8kHz) — whistle intact.** So: whistle+40min detector applies to **2026+ Reolink games**; the 2024-25
Dahua games keep their human game_state (they have it) or need the density/ball method.
**Batch input correction (2026-06-28):** combined.mp4 (16kHz) is UNRELIABLE for the batch — it cuts the whistle's upper
harmonics and (with pre/post-game crowd/PA audio) lets spurious low tones win the pitch search (6/04 picked a bogus
2050Hz with a negative kickoff). Inconsistent (6/15 happened to match its play_windows GT; others failed). **Use the
TRIMMED upload file (44.1kHz, game-only)** — reproduces 6/04 exactly (4250Hz; 2:37/42:37/50:16/90:09, all ~1s). Map
trimmed-time -> (seg,f) via `match_info.ini start_time_offset` (varies per game: 6/04=6:00, 6/15=1:00). Batch tool:
`G:\ballresearch\whistle_batch2.py` (writes game_state source=whistle_40min + `_whistle_meta`{trimmed_times,offset,pitch,
score} for traceability; writes only clean 40-min fits, logs the rest).
**Batch outcome (heat-2026, 19 games):** **7 written** with whistle-anchored game_state — 6/04 (4250, GT-validated),
5/07_18.28 (3750), 5/27 (3750), 6/01 (3850), 6/08 (3450), 6/10 (3950), 6/15 (3550, via raw-file fallback). Two refinements
proved out: (1) **pitch constraint to 3-4.8kHz** — 6/08 had a spurious 2250Hz lock; constrained, it found the real 3450Hz
whistle; 5/28's 2050Hz had no in-range whistle -> honest no-fit. (2) **raw.mp4 fallback** (44.1k/16k full recording,
offset 0) rescued 6/15 (its trimmed upload was a corrupt 52MB stub). **9 no-fit + 3 no-trimmed-file** — no-fits failed at
BOTH trimmed(44.1k) and raw(16k) in 3-4.8kHz, so it's poor ref-mic audio or non-40-min sub-games, NOT a pitch issue.
Those need the marathon's detections (ball-at-center kickoff fallback, once it reaches 2026 games) or manual phase-editor
entry. Net: whistle detector delivers sub-second phases for the games with a clean ref-whistle; ~37% of heat-2026 here.
**Auto-half-length is UNRELIABLE (don't pursue):** tried sweeping the half length (25-45min) for non-40-min leagues
(flash ~34-38min). On 6/04 it picked **half=27min** (perfectly symmetric spurious whistle pair, sym=0) over the correct
40min (sym=6s) — symmetry-only scoring rewards any coincidental symmetric pair. The half length must be a KNOWN league
constant (heat/Guzzetta 2026 = 40min, confirmed). flash halves vary per game (34-38) so even a fixed flash-half won't fit
cleanly -> flash-2026 phases need ball-at-center kickoff (post-marathon) or manual phase-editor. Whistle+fixed-40 is the
solution for the heat/Guzzetta 2026 games only.
**Vision-verification pass (2026-06-28):** decoded the frame at each detected kickoff for the 6 non-GT anchored games.
**5 confirmed game-in-progress** at kickoff (5/07_18.28, 5/27, 6/01, 6/10, 6/15) + 6/04 (frame-precise GT) = **6 verified
whistle-anchored games**. **6/08 REMOVED** — its detected kickoff (0:19, score-2, 21-min offset) was an empty
football-marked field; reverted to needs-manual. Lesson: low fit score + odd offset = "don't trust"; require score 3 +
non-empty kickoff frame.

---

## EXP-PHASE-01: train a game-phase detector on human phases (detection features) — ~4m MAE, marginal (2026-06-27)

**Hypothesis:** Mark's 27 human `game_state` sets (now in `game.json`, aligned (seg,f) space) can supervise a phase
detector using `autocam_detections` features (no upload offset / fps drift, unlike play_windows).
**Method:** box-scratch `G:\ballresearch\train_phase_model.py`. 19 games with human phases + detections. Per 10s window:
top-1 conf, #high-conf candidates/frame, x-spread of high-conf (multi-ball), in-field ratio, top-1 motion, temporal
position. numpy multinomial logistic-regression emission + **duration-constrained segmental DP** (learned per-phase
min/max + Gaussian length prior). Leave-one-game-out. (Whistle dropped — corrupt-audio decode + EXP-012 already showed
it's noisy.)
**Result (LOO MAE, minutes):** kickoff 5.2 · HT-start 4.8 · 2H-start 5.2 · **game-end 2.3** · overall **4.4** (4.1 with
per-boundary calibration). game-end within-2m 12/19; kickoff/HT/2H ~5/19. Stable across feature/DP/calibration variants.
**Conclusion:** Confirms EXP-012 — detection signal is **not sharp** at warmup→1st-half and the halftime edges (ball
activity looks similar across them); **game-end is reliable** (activity stops). A trained density model alone plateaus
~4m. EXP-012's better 2.1m needed whistle + asymmetric density + crowd-energy + calibration, and still only 10/29 games
within 2m on all boundaries. **Practical use:** model is a recording-time SEED for the phase editor (better than the
offset play_windows seeds; game-end trustworthy), NOT accurate enough to auto-fill phases unverified. Phase editor
(`/static/phase-edit.html`) remains the reliable path.
**Follow-up (10s-precision attempt):** to hit a 10-second bar, tested a coarse-to-fine **kickoff fine-localizer** —
"ball at field-center, still, then burst of motion" from `autocam_detections` top-1, in an ORACLE +/-5min window around
the human kickoff (`G:\ballresearch\kickoff_localize.py`). **FAILED: 172s MAE, 1/19 within 10s.** Raw top-1 detections
are too noisy to catch the placed-then-struck ball, and center-restarts (every goal kickoff) are ambiguous even in the
window. **Conclusion: 10s automated precision is NOT reachable from the ball-detection signal.** The only plausible
automated path is a trained **visual** kickoff/whistle model (big build, GPU, uncertain it reaches 10s — EXP-012 already
put sub-1m as hard). For true 10s precision, **human scrubbing in the phase editor is the reliable answer**
(frame-precise); auto-detection can only pre-seed game-end (~2m).
**Follow-up 2 (multi-blast-whistle hypothesis, 6 games):** tested "multi-blast = halftime/end" + temporal window
(`G:\ballresearch\whistle_test.py`, FFT whistle clusters w/ blast counts vs human HT/end). **Hypothesis NOT confirmed:**
the nearest **3+ multi-blast** cluster is **18-21 min** from the true HT/end (median 1125s/1295s) — i.e. 3+ clusters are
NOISE bursts in the 2-4.5kHz band (crowd/coaches/wind), they do NOT mark periods. BUT the nearest whistle cluster of
**any** count IS near the boundary: HT median 33s (4/6 within 60s, most ~23-33s), END median 43s (4/6 within 60s). So
whistle = a **coarse ~30-60s anchor**, not a 10s one, and blast-count doesn't discriminate (matches EXP-012's noise
finding). **Caveat:** the ~30s "error" may be partly human-label imprecision (manifest.db phases scrubbed to ~thumbnail
granularity) — if human GT is only ~30s-precise, 10s is unmeasurable against it and the whistle may already be at GT
precision. Untested refinement: isolate the period-end whistle by **duration/loudness** (long ref blast) not blast-count.

---

## EXP-DIST-15: does the marathon's 448-anisotropic squish actually corrupt Dahua labels? — YES, ~16% (2026-06-26)

**Hypothesis:** `gen_detections_all.py` fed AutoCam's balldet a hardcoded `1600x448` anisotropic resize for
every game. AutoCam's proven recipe is isotropic-to-1600-wide + ceil-pad-to-32 → **480** for 7680x2160 (Reolink)
but **704** for 4096x1800 (Dahua, 50 of 73 trainable games). If the resulting 36% vertical squish on Dahua
materially moves the detected ball, the distill labels for the majority of games are systematically wrong and the
marathon must be re-run; if the detector is squish-robust, keep the running data.

**Method:** 300 real frames from a done Dahua game (`flash__2024.05.10_vs_NY_Rush_away`, 4096x1800). Ran the SAME
decrypted fp32 balldet model two ways — (A) marathon `1600x448` anisotropic, de-scale `sx=W/1600, sy=H/448`;
(B) AutoCam-correct `1600x703` isotropic + bottom-zero-pad to `704`, de-scale `sx=sy=W/1600`. Identical decode/NMS;
**only the input geometry differs.** Compared top-1 in source px. (CPU EP, to not perturb the live DML marathon.)

**Result:**
| metric (both ≥0.3 conf, n=271/300) | value |
|---|---|
| top-1 agree ≤15 px | **0.84** |
| top-1 agree ≤30 px | 0.84 (→ disagreements are big jumps, not 15–30 px drift) |
| median distance | 1.1 px |
| mean / p90 / max distance | 48 / 144 / 1501 px |
| signed Δy mean | **−8.1 px** (systematic vertical bias) |
| only-marathon-strong / only-correct-strong | 15 / 3 of 300 |

**Conclusion:** the squish is harmless on 84% of frames but flips the top-1 onto a **different object on ~16%**
(confident on both sides, so off-field/static/jump filters miss them → ~1-in-6 confident-wrong Dahua labels),
plus a −8 px bias that vanishes at correct geometry. **Channel order was NOT the problem** (file does
`COLOR_BGR2RGB`+`/255`+fp32). Fixed `gen_detections_all.py` to per-game isotropic+ceil-pad (480 Reolink / 704
Dahua, `sx==sy`), wiped the 10 squished Dahua outputs (viewports kept), relaunched all 72 trainable. See STATUS.md
and F:`broadcast_camera_render_docs/DETECTION_PIPELINE.md` (CORRECTION 2026-06-26).

---

## EXP-DIST-14: far-band loss up-weighting (K4) vs control (K0) — NEGATIVE, far-weight HURTS (2026-06-26)

**Hypothesis:** Up-weighting the far ball's heatmap loss (`--far-weight 4`) makes the detector rank the far
ball as its argmax more often → higher far-recall. (The EXP-DIST-13 recipe, run to completion.)

**Method:** `G:\ballresearch\distill\recall_train.py` on the GPU box. base=24 HeatmapNet, 58 training games
(N=8 curve games + the 6/15 human-label game, 232 ball labels), 30k steps, **3 seeds × {K4 far-weight=4, K0
control far-weight=0}** = 6 runs. Eval on the clean held-out Spencerport `spc_normal1` (normal/far-third) +
the hard split, meters R10/R15, vs AutoCam viewport 0.748. Results in `recall.jsonl` (2 rows: K4, K0).

**Result (mean ± std over 3 seeds):**
| split | K4 far-weight | K0 control | AutoCam |
|---|---|---|---|
| hard R15 | 0.239 ± 0.093 | **0.348 ± 0.018** | 0.11 |
| hard R10 | 0.163 | **0.277** | — |
| normal R15 | 0.039 ± 0.043 | **0.069 ± 0.052** | 0.748 |
| normal R10 | 0.015 | **0.030** | — |
| hard ceil15 | 0.723 | 0.749 | — |

**Conclusion: NEGATIVE — far-weight HURTS.** The K0 control (same data, no far-weight) **beats** K4 on BOTH
splits AND has far lower variance (hard std 0.018 vs 0.093; the K4 seeds swing 0.114–0.339). So far-band loss
up-weighting is harmful, not helpful — it destabilizes training without improving far-recall. The candidate
ceiling is unchanged (~0.72–0.75), confirming (again, per EXP-DIST-08..12) the bottleneck is **selection /
detector-recall**, not the loss shaping. K0's hard 0.348 ≈ the prior curve baseline (~0.39); normal 0.069
remains ~10× below AutoCam's 0.748 → the far/normal gap is NOT closable by a loss tweak.

**Implication / next:** abandon far-band loss weighting. The lever is **data — venue diversity** (more games),
which is exactly what the new all-games AutoCam-detection dataset (`gen_detections_all.py`, #27, launched
2026-06-26) generates. This negative result re-justifies that effort. Do NOT re-try loss-weighting variants
(this + D1 augmentation + D4 appearance-discrimination are all negative).

**Data:** `G:\ballresearch\distill\recall.jsonl`, `recall_fw4.log` (`=== RECALL POINT K4/K0 ===`).

---

## EXP-DIST-13: targeted far-ball RECALL recipe — human labels + far-band loss + multi-seed (2026-06-24)

**Status: STAGED, NOT RUN.** Design + code complete and committed; the GPU launch is deferred until the
N=16 data-scaling curve finishes (Mark's decision: finish N=16 first, THEN run recall — one GPU on this box).
This entry is the design of record; it will be updated with RESULTS after the run. Nothing in this entry has
been executed on the GPU. The curve was confirmed byte-undisturbed at staging (orchestrator PID 3620 +
iter_run PID 21412 alive; `curve.jsonl` 4 rows N=1/2/4/8, N=16 mid-run).

**Why (what the curve proved, and what it did NOT):** the corrected data-scaling curve (EXP-DIST-02) shows
that adding games on the CURRENT recipe does NOT lift recall — held-out HARD R15 bounces
0.386 / 0.22 / 0.356 / 0.331 across N=1/2/4/8 and NORMAL R15 is flat-to-crashed 0.155 / 0.081 / 0.153 / 0.009
(N=8 collapsed on seed variance). The decomposition (EXP-DIST-08..12) localizes the wall precisely: on far
play the ball is in the detector's top-12 candidate set ~0.66–0.81 of the time, but is the per-frame ARGMAX
only ~13.5 % of the time (median 25.6 m off). DECISIONS 2026-06-24 ("stop selection engineering") concluded
the only path to AutoCam-class far is **detector far-RECALL** — make the far ball the strongest peak. The
lever is the RECIPE, not data volume. This experiment changes the recipe along three opt-in axes.

**Hypothesis:** at least one of {fresh human labels on the failure cases, far-band loss up-weighting, reading
signal over seed variance} raises held-out far/normal R15 above the best curve row (HARD ~0.33–0.39,
NORMAL ~0.155) on the SAME held-out Spencerport eval the curve uses.

**Levers (all opt-in flags; the running curve is byte-unaffected because they default OFF and live in a NEW
runner, not the live `iter_run.py`):**
1. **Merge Mark's fresh human-override labels.** Two 6/15-game (`guzzetta__2026.06.15_vs_Irondequoit`) sets,
   verified counts (`action=="ball" and x is not None`): `heat_0615_normlowconf1` = **84 ball** (mid/near hard
   discrimination, the normal-field FP-on-player/shadow/goalmouth cases), `heat_0615_gaps1` = **153 ball**
   (far, game-wide). 6/15 is wired as a TRAINING game from its raw clip + the human-tightened polygon
   `det0615/field_polygon_0615.json` (`source: human_field_edit`) gated to Mark's active-play windows. NOTE:
   6/15 is NOT in `F:\archive\ball_distill\` (no `ball_track.json` / per-seg detections — its detector run is a
   flat game-wide dump), so it canNOT be wired the auto-label `iter_run.py` way; it is wired as a **human-label
   game** (labels = the two sets' clicks on the raw `…-raw.mp4`, decoded by global frame_idx — exactly the
   `build_heatmap_crops` game spec). Optional far auto-labels from `det0615/ball_dets_0615.json` are available
   but off by default (human clicks are the high-value signal).
2. **Far-band loss up-weighting** — NEW opt-in flag `--far-weight K` on `training/train_v4_heatmap.py`
   (default `0` = the current uniform `w = 1 + 30·tgt` EXACTLY, so the curve is unaffected even if it
   re-imports). When `K>0`, the POSITIVE (ball-pixel) weight is scaled by `(1 + K·(1−depth))`, where
   `depth ∈ [0,1]` is the ball's normalized field depth (0 = far touchline / band top, 1 = near touchline);
   a far ball weighs up to `(1+K)×` a near ball, pushing the loss to make the far ball the strongest peak.
   `depth` is recorded per positive at crop-build time (`build_heatmap_crops(record_depth=True)`, additive
   index field, never gates which crops are written). Background weight (the leading `1.0`) is untouched.
3. **Multi-seed (2–3 seeds)** so the signal is read over the variance that crashed N=8 NORMAL to 0.009. The
   runner trains each {far-weight} config across seeds and reports mean ± spread on both splits.

**Eval = identical to the curve (apples-to-apples).** Same held-out Spencerport stretch `spc_stretch_s1.json`
+ `spc_poly.json`, frames 7900–11500, HARD = human far labels, NORMAL = human `spc_normal*`, metric =
`world_model.eval.evaluate_recall_metric` in METERS at R10/R15, AutoCam baseline from `autocam_stretch.json` —
reused VERBATIM from `iter_run.py`/`variant_train.py` (the recall runner shares the eval block). **CRITICAL:
`spc_normal1` (Spencerport) is the HELD-OUT eval and is asserted OUT of training** in both the repo entry
(`assemble_games` Spencerport-token guard + post-build assert) and the server runner (explicit exclusion +
assert), so no eval frame can leak into training.

**Baseline to beat (same eval):** best curve row HARD R15 ~0.33–0.39, NORMAL R15 ~0.155, AutoCam viewport
HARD 0.11 / NORMAL 0.748.

**D1/D4 clearance (does this re-tread a failed experiment? NO — checked against the V4 heatmap failures):**
- **Failed AUGMENTATION (the "D1" augmentation entries — V4_HEATMAP_EXPERIMENTS variants I `aug3`+cutout
  "slightly hurt", H `sigma8` "no gain", and Jmot motion-channel "no help"):** those altered the INPUT
  pixels / channels / target blob. Far-band loss up-weighting touches **only the per-pixel loss weight by
  field position** and adds **human GT** — no augmentation, no new input channel, no sigma/blob change, no
  photometric/cutout transform. **NOT the failed augmentation experiment.**
- **Failed LEARNED-APPEARANCE-DISCRIMINATION (the "D4" entries — heavy random negative mining Jn8 8:1
  "destabilized focal training, made it worse"; the motion channel; the multi-task person-head concept):**
  those tried to teach ball-vs-distractor APPEARANCE via architecture/extra-negatives/extra-heads. This
  experiment changes **neither the architecture nor the negative-mining ratio** (HeatmapNet base24,
  `neg_ratio`/`neg_per_pos` unchanged from the curve) and adds **no person head**. Far-band weighting is a
  loss-reweighting by GEOMETRY (depth), and the human labels are GT, not mined hard-negatives.
  **NOT the failed appearance-discrimination experiment.** (The existing SAFE hard-negative cells in
  `iter_run.py` are also untouched and not extended.)
  Conclusion: both levers are distinct from D1 and D4; no overlap, no design change needed.

**Repo deliverables (this commit, branch `feat/homegrown-ball-detector` — generic detector-training features):**
- `training/train_v4_heatmap.py`: the `--far-weight K` flag (default 0 = byte-identical to the curve), the
  6/15 label-set wiring + 6/15-as-training-game (raw clip + human-tightened polygon, via `SET_POLYGONS`), and
  the held-out Spencerport training-exclusion guard + assert.
- `training/data_prep/heatmap_dataset.py`: `build_heatmap_crops(record_depth=...)` — additive per-positive
  field-depth metadata (default off → legacy index schema, curve unaffected).

**Server-scratch deliverable (NOT in the OSS repo, NOT executed):** `G:\ballresearch\distill\recall_train.py`
— a recall variant of `iter_run.py` (a NEW file; the live `iter_run.py` is untouched). It builds the recall
dataset (the N=8 curve games' cached per-game crops + the two 6/15 human-label sets as a new
depth-recorded game), trains 2–3 seeds at the chosen `--far-weight`, and evals on the SAME held-out sets,
appending rows to `G:\ballresearch\distill\recall.jsonl`. Staged; launches only after the curve frees the GPU.

**LAUNCH (deferred — run only after the curve's N=16 row appends and no curve process is alive):** see the
session report / `recall_train.py` header for the exact git-pull-on-server + launch commands (a SEPARATE
checkout `G:\ballresearch\recall_wt`, never the curve's `G:\v4bench\wt`).

---

## EXP-DIST-12: the reference "dumb pixel-smoother" far-follow on OUR detector — selection-algo vs detector-is-the-wall (2026-06-24)

**Status:** DONE. CPU-only, read-only w.r.t. the curve. Did NOT disturb the running curve (orchestrator PID
3620/17348 + iter_run PID 19452/20432 alive before AND after; curve.jsonl byte-unchanged at 14:59:02 / 1384 B
/ 3 rows N=1/2/4 throughout). Scratch on the server (`G:\ballresearch\distill\exp_dist_12.py` faithful replica +
`exp_dist_12_diag.py` decomposition + `exp_dist_12_mpp.py` geometry, `exp_dist_12.json`); only this doc is
repo-resident. `CUDA_VISIBLE_DEVICES=-1` — zero GPU compute, cached continuous candidate stream only, NO video
decode. The reference far-follow mechanism RE → F: archive (not OSS).

**Question (the decisive test EXP-DIST-08→11 set up):** every selector we tried is Viterbi-family (track_ball
0.153, perspective-scaled Viterbi 0.216) — all far below the reference viewport (0.748) and the candidate
ceiling (~0.81). We had NOT tried the reference's ACTUAL far-follow algorithm. Per the F: archive RE, the
reference far-follow is **NOT a tracker**: it is the per-frame **TOP-1 (argmax)** detection fed into a recency-
weighted **~3 s moving average in PIXEL space** + a heavy single-pole EMA, with **NO candidate set, NO
association, NO outlier/teleport gate** (far jumps are chased, not gated). Replicate it faithfully on OUR
detector's far argmax and find its ceiling: is selection the wall (→ jumps toward 0.748) or is our detector's
far argmax the wall (→ stays ~0.15–0.22)?

**Method (faithful replica + EXP-DIST-09 harness verbatim):** continuous champion-J far stream
`spc_stretch_s1.json` (3600 consecutive source frames [7900,11499], gap=1, 3600/3601 with a detection),
clean human `spc_normal1` GT (n=111, deduped vs HARD far), meters via `spc_poly.json` →
`build_field_geometry` → `evaluate_recall_metric` (identical to EXP-DIST-08/09/11). Per frame: input =
the **single highest-score J-peak** (argmax — NO top-12 set, NO association). Maintain a rolling buffer of the
last `buffsecs·fps` frames' argmax points; target = `np.average(points, weights=arange)` (newest weighted
most) in **source pixels**; single-pole EMA `current ← ms·current + (1−ms)·target`. Stale drop out of the
window; `lastBox` carry-forward on detection gaps. **fps = 19.481** (Spencerport container `average_rate`,
metadata only, no decode) → `buffsecs=3.0 s` ≈ 58 frames. Knobs swept (window `buffsecs`, EMA `movesmooth`) on
a **DEV half** of the GT only (frames <9712, n=55); the single best config is frozen and re-scored on the held-
out TEST half (n=56) and on FULL (n=111).

**Frozen config (chosen on DEV, then frozen):** `buffsecs=2.5 s` (≈49 frames), `movesmooth=0.95`. DEV R15
0.382; the faithful reference default (3.0 s / 0.975) gives DEV 0.291 / FULL 0.279 — i.e. the reference's own
constants are *not* optimal for our argmax (our argmax is noisier than the reference detector's, so it wants a
slightly shorter window + lighter EMA). **Sensitivity is benign:** over the 3×3 grid neighbourhood of the
frozen config, FULL R15 ranges only 0.279–0.351 (spread 0.072) — a broad, flat optimum, not a knife-edge tune.
An optional faithful **adaptive-gain** term (the reference's `vf`: buffer scatter loosens the EMA) adds a
little more (FULL R15 0.342 → 0.369).

**COMPARISON TABLE (spc far stream, clean human `spc_normal1` GT, meters, hits/N):**

| selector | R10 | R15 | median_m | hits/N (R15) |
|---|---|---|---|---|
| per-frame argmax (top-1) | 0.099 | 0.135 | 25.6 | 15/111 |
| track_ball (shipped) | 0.135 | 0.153 | 47.3 | 17/111 |
| perspective-scaled Viterbi (EXP-DIST-11) | 0.189 | 0.216 | 46.5 | 24/111 |
| **reference dumb pixel-smoother (frozen 2.5 s / 0.95)** | **0.117** | **0.342** | **19.1** | **38/111** |
| reference dumb pixel-smoother + adaptive gain | 0.189 | 0.369 | 19.6 | 41/111 |
| AutoCam viewport (reference) | 0.694 | 0.748 | 6.9 | 83/111 |

**DECOMPOSITION (`exp_dist_12_diag.py`) — feed the SAME dumb smoother three inputs (isolates smoother vs detector):**

| input → dumb smoother (frozen) | R10 | R15 | median_m |
|---|---|---|---|
| (C) raw argmax, NO smoothing (anchor) | 0.099 | 0.135 | 25.6 |
| (A) OUR detector top-1 argmax | 0.117 | **0.342** | 19.1 |
| (B) ORACLE per-frame pick (best top-12 cand), through the SAME smoother | 0.108 | 0.369 | 16.8 |
| (B′) ORACLE per-frame pick, NO smoothing (= candidate ceiling) | 0.595 | **0.811** | 8.0 |

Detector argmax meters-err to GT: median **25.6 m**, p25 17, p75 33; only **13.5 %** within 15 m.

**KEY READ — BOTH knobs move, but neither reaches AutoCam; the decomposition explains why.**
1. **The selection ALGORITHM was genuinely hurting us on far.** Replacing the meters-Viterbi + teleport gate
   with a dumb pixel time-average **more than doubles R15: 0.153 → 0.342** (and beats the EXP-DIST-11
   perspective-scaled fix 0.216). The Viterbi/teleport machinery was actively *suppressing* recoverable far
   recall (it traps on a distractor and forbids re-acquiring the ball — EXP-DIST-11). So a chunk of the far
   collapse was a selection-algorithm artifact, recoverable by simplifying. Median err also drops 47→19 m.
2. **But the dumb smoother is itself a ~16–19 m-error floor — it CANNOT reach 0.748.** The oracle decomposition
   is decisive: a **perfect** per-frame selector scores R15 **0.811** with NO smoothing (the candidate
   ceiling), yet pushing that same perfect selection **through the dumb smoother collapses it to 0.369**. The
   recency-weighted-average + heavy EMA *lags and averages away* the ball's true far position (it is built for
   a smooth viewport, not a precise point), so even given perfect detections it lands ~16 m off. Our argmax-fed
   smoother (0.342) is already near that smoother-imposed ceiling (~0.37–0.41). The smoother is not a path to a
   precise far point; AutoCam's 0.748 is its **viewport** (camera centre within ~15 m = "ball on screen"), a
   much looser target than a point estimate, fed by a detector whose far **recall** is far higher than ours
   (its argmax-err median ≫ better; ours is 25.6 m / 13.5 % in-15 m).

**THE EXPLICIT DECISION — it is BOTH, but the dominant remaining wall is the DETECTOR, not the selector.** The
dumb smoother did NOT "jump toward 0.748" (it reached 0.342, ~2.2× below) and did NOT merely "stay ~0.15–0.22"
(it cleanly beat both Viterbi variants). The honest read is two-part: **(a)** the shipped meters-Viterbi/
teleport selector was over-engineered for far play and a radically simpler pixel-space recency-average roughly
DOUBLES far recall — so **simplify the shipped tracker on the far band** (ship the dumb pixel-smoother, or at
minimum drop the teleport gate, behind a far-band flag); but **(b)** that only buys ~0.15→0.34, and the oracle
proves the wall to 0.748 is **NOT more selection work** — even a perfect selector through this (or any) smoother
plateaus far below, because our detector's far argmax is only 13.5 % ball-centred (median 25.6 m off). **The
only path to AutoCam-class far performance is detector far-RECALL** (raise the per-frame argmax/ceiling
quality), which is exactly what the venue-diversity curve + the new far-label sets target — NOT additional
selection engineering. Selection simplification is a real, cheap, modest win to bank; detector far-recall is
the lever that actually closes the gap.

**Near/mid regression check (does the EXP-DIST-11 perspective-scaled fix regress near/mid?) — geometric, cheap;
empirical A/B deferred (no GPU/decode).** A direct near/mid A/B of perspective-scaled-Viterbi vs shipped
`track_ball` is **NOT cheaply available**: the only near/mid GT venue (6/15 `heat_0615_normlowconf1`, 84 balls,
y median 686 — genuinely mid/near) has only the `det0615` stream, which is **stride-20 (not the gap=1 continuity
the tracker's transition/motion terms require) and carries NO MOG2 motion blobs**. A continuous gap=1
candidate+motion dump over a near/mid range would need a GPU/decode pass (forbidden — curve owns the GPU) →
**marked a FOLLOW-UP.** The cheap available evidence is geometric and is reassuring: the fix scales the
smoothness budget + teleport ceiling by **local meters-per-pixel**, and m/px falls steeply with field depth —
measured on both venue geometries: FAR ≈ 0.15 m/px, MID ≈ 0.04–0.07, NEAR ≈ 0.01–0.02 (4–15× smaller than far).
So near/mid the per-candidate scale factor is small (and with EXP-DIST-11's `max(scale,1)` guard, effectively
unchanged) — the fix does NOT loosen association near/mid, where the far-only "correct candidate looks like an
impossible teleport" pathology cannot arise (a few px of motion = tenths of a metre, far under any gate). By
construction the fix is far-band-only and cannot manufacture far-style distractor jumps near/mid; an empirical
confirmation awaits a continuous near/mid candidate stream.

**BONUS hypothesis (flagged, NOT tested — no GPU inference run):** the reference detector's strong far recall
may partly depend on its **operating input scale** — it runs the ball detector full-frame at a *specific reduced
width* (≈3264-wide single inference per the F: archive RE), a different input resolution than OUR detector's.
Our far argmax centring (13.5 % in-15 m, median 25.6 m) is the binding wall; whether matching that operating
input scale would raise OUR detector's far argmax is a plausible **detector-side experiment for a future
session** (it would need GPU inference, so it is only a hypothesis here, not a result).

**Caveats (unchanged from EXP-DIST-08/09/11):** candidate stream is champion-J (the only CONTINUOUS far dump;
no continuous distill far stream exists without GPU/decode), so this measures the dumb smoother on champion-J's
far argmax; `spc_normal1` is geometrically far-third GT (this is a far-play head-to-head); meters is the fair
metric (px-radius flatters/penalises by far-corner geometry). The verdict is robust to the stream choice: the
gap to 0.748 and the oracle-through-smoother collapse hold on any argmax of this far-recall quality.

**Curve-alive confirmation:** orchestrator 3620/17348 + iter_run 19452/20432 alive before AND after;
curve.jsonl byte-unchanged (14:59:02, 1384 B, 3 rows N=1/2/4); all analysis ran on CPU from the cached
candidate stream + container metadata only (no decode, no GPU).

---

## EXP-DIST-11: WHY the tracker does nothing on far play — the meters-smoothness/teleport prior traps it (2026-06-24)

**Status:** DONE. CPU-only, read-only w.r.t. the curve. Did NOT disturb the running curve (orchestrator PID
3620/17348 + iter_run PID 19452/20432 alive before AND after; curve.jsonl byte-unchanged at 14:59:02 / 1384 B
/ 3 rows N=1/2/4 throughout; `curve_gpu.flag=8`, GPU 70–76% on the curve's N=8 training, untouched). Scratch on
the server (`G:\ballresearch\distill\exp_dist_11.py` diagnosis + `exp_dist_11_ab.py` prototype,
`exp_dist_11*.json`); only this doc is repo-resident. `CUDA_VISIBLE_DEVICES=-1` — zero GPU compute, cached
candidate stream only, no video decode. The reference selection mechanism RE → F: archive (not OSS).

**Question (the open thread from EXP-DIST-08/09):** EXP-DIST-09 showed the `track_ball` temporal tracker adds
almost nothing on continuous far play (argmax R15 0.135 → tracked 0.153 vs candidate ceiling 0.81) and the
Kalman step HURTS recall (bare rerank 0.180 → +Kalman 0.153). WHY? **Hypothesis:** the reranker's
smoothness/Viterbi cost is computed in METERS; in the far field a tiny pixel error = a huge meters error, so the
meters-smoothness prior treats the CORRECT far candidate as an implausible "jump" and suppresses it.

**Method (instrument the real run, exactly the EXP-DIST-09 harness):** replayed the production
`reranker.track_ball` on the cached continuous far stream `spc_stretch_s1.json` (3600 consecutive frames, top-12
candidate peaks + MOG2 motion/frame) scored on the clean human `spc_normal1` GT (111 far balls, meters, via
`spc_poly.json` geometry). Re-ran the reranker's Viterbi verbatim while keeping the cost/back tables (SANITY:
reproduces the production numbers exactly — bare-rerank 0.180, track_ball 0.153, argmax 0.135). For each GT frame
where the ball IS in the top-12 but the pick is wrong (the recoverable-miss set), logged, for the correct-far
candidate (closest top-12 cand to GT, ≤15 m) vs the chosen candidate: the emission terms (score / static-
persistence / motion) AND the **incoming Viterbi transition cost** (the meters-smoothness term) from the chosen-
path predecessor, plus whether the correct candidate was **teleport-forbidden** (`d_m > max_jump·gap`).

**Reranker cost recap (the thing under test):** emission `−α·score + static_w·persistence − motion_w·motion`;
**transition `(d_meters / (vmax·gap))²`** with `vmax=2.5 m/frame`, and a **hard teleport gate that REMOVES any
candidate with `d_meters > 6 m/frame` from the lattice** (`continue`).

**DIAGNOSIS (n=111 GT; 83 recoverable = ball in top-12; 63 of those picked wrong = the analyzed misses):**

| measure (of the 63 recoverable misses) | count | frac |
|---|---|---|
| **correct far candidate TELEPORT-FORBIDDEN from the chosen predecessor** (`d_m > 6 m`) | **52** | **0.825** |
| correct candidate's meters-displacement to the chosen predecessor | median **67 m**, p75 92 m, max 116 m | — |
| of those forbidden, displacement >30 m (predecessor already wildly off the ball) | 42 / 52 | 0.81 |
| of those forbidden, displacement just over the 6 m ceiling (≤15 m) | 3 / 52 | 0.06 |
| meters-per-pixel over the far stream (median / p90 / max) | 0.088 / 0.10 / 0.19 | — |

**HYPOTHESIS CONFIRMED — with a sharper mechanism than first stated.** It is not merely that the smoothness
*cost* is high for the correct far candidate; the **hard teleport gate hard-EXCLUDES it** in 82.5% of misses.
And the displacement that trips the gate is median **67 m** — far over the 6 m ceiling — because the chosen
meters-smooth track has **already locked onto a low-meters-velocity DISTRACTOR and drifted ~67 m off the ball**;
when the real far ball reappears in the candidates, the 6 m gate forbids "jumping back" to it. So the meters
prior does two compounding things in the far field: (1) it prefers a smooth-in-meters distractor path over the
real far ball (whose apparent meters-velocity is high/jumpy because meters-per-pixel is ~0.09–0.19 there), and
(2) its teleport gate then traps the track on that distractor and blocks far re-acquisition. Only 6% of the
forbidden cases are "ball genuinely moved a bit, a few px tipped it just over 6 m" — the dominant failure is the
distractor-trap cascade, which is the meters-displacement problem at root.

**PROTOTYPE A/B (diagnosis cleanly indicated a fix → built it; server scratch, behind a flag, shipped
`reranker.py` default UNTOUCHED). Same cached far stream, clean `spc_normal1` GT, meters, n=111:**

| variant | R10 | R15 | med_m | hits/111 |
|---|---|---|---|---|
| argmax (reference) | 0.099 | 0.135 | 25.6 | 15 |
| **V0 `track_ball` (production)** | 0.135 | **0.153** | 47.3 | 17 |
| V0r bare rerank (meters, 6 m gate) | 0.153 | 0.180 | 67.5 | 20 |
| **Va perspective-scaled budget+teleport (bare)** | **0.189** | **0.216** | 67.0 | **24** |
| **Va perspective-scaled + Kalman** | 0.189 | **0.216** | **46.5** | 24 |
| Vc loosen-only (max(scale,1)) + Kalman | 0.162 | 0.180 | 46.8 | 20 |
| Vb pixel-space association (bare) | 0.108 | 0.153 | 72.1 | 17 |
| Vb pixel-space + Kalman | 0.126 | 0.162 | 51.2 | 18 |

The fix that wins: scale the smoothness budget AND teleport ceiling **per-candidate by the local meters-per-
pixel** (a far candidate gets a proportionally larger meters budget), so a correct far association is no longer
an "impossible teleport." **R15 0.153 → 0.216 (+0.063, +41% relative; 24 vs 17 hits)**; Kalman then helps here
(cuts median 67→46 m without losing recall — unlike on the meters baseline where it hurt, EXP-DIST-09).
Pixel-space association (Vb) did NOT beat it (0.153) → the fix is "scale the meters budget by perspective," not
"abandon meters." Loosen-only (Vc) underperforms full scaling.

**CONCLUSION / honest read:** the hypothesis is CONFIRMED and points cleanly to a perspective-variance-weighted
association fix that gives a real but **modest** far gain (0.153→0.216). It is NOT a solution: 0.216 is still
~3.5× below the strong reference viewport (0.748) and well below the candidate ceiling (~0.81). Loosening the
association recovers a fraction of the recoverable far misses but **selection remains the dominant unsolved far
bottleneck**, and the reference system's real far edge is higher detector RECALL feeding a trivial pixel-space
time-average (no clever selector at all — see the F: archive RE). **Recommended next experiments, ranked:**
(1) **detector far-recall** is lever #1 (raise the ceiling, not just selection) — the venue-diversity curve +
the new far-label sets target exactly this; (2) ship the perspective-scaled association as a far-band flag and
re-A/B once a distill (not champion-J) continuous far stream exists; (3) revisit the static-persistence /
distractor-trap directly (the meters-smooth Viterbi *prefers* the static distractor before the teleport gate
ever fires — a stronger distractor repellent may matter more than the gate). **Caveats (unchanged from
EXP-DIST-09):** champion-J continuous stream (not the N-curve distill checkpoint — no continuous distill far
stream exists without GPU/decode); `spc_normal1` is geometrically far-third GT (this is a far-play head-to-head).

**Curve-alive confirmation:** orchestrator 3620/17348 + iter_run 19452/20432 alive before AND after;
curve.jsonl byte-unchanged (14:59:02, 1384 B, 3 rows N=1/2/4); `curve_gpu.flag=8` — N=8 trained on the GPU
throughout (70–76% util) and was not touched; all analysis ran on CPU from the cached candidate stream.

---

## EXP-DIST-10: is the selection collapse FAR-ONLY or WHOLE-GAME? — mid/near GT `heat_0615_normlowconf1` (2026-06-24)

**Status:** DONE. CPU-only, read-only w.r.t. the curve. Did NOT disturb the running curve (orchestrator PID
3620/17348 + iter_run PID 19452/20432 alive before AND after; curve.jsonl byte-unchanged at 14:59:02 / 1384 B
/ 3 rows N=1/2/4 throughout; `curve_gpu.flag=8` — N=8 trained on the GPU the whole time and was not touched).
Scratch on the server (`G:\ballresearch\distill\exp_dist_10.py` + `exp_dist_10_seldecomp.py`,
`exp_dist_10*.json`, vis crops `_exp10_vis_*.png`); only this doc is repo-resident.
`CUDA_VISIBLE_DEVICES=-1` — **zero GPU compute, NO video decode** (candidate stream + GT only).

**Question (Mark's standing one, the gap EXP-DIST-08/09 left open):** EXP-DIST-08/09 measured the detector on
`spc_normal1` (top-1 R15 0.153, ceiling 0.66) — but that GT turned out to be geometrically **far-third only**
(all 113 balls y 7–392 of 2160). So the named "selection collapse" was only ever proven on FAR play. Mark
hand-labeled a clean **mid/near** GT on the 6/15 Heat game — `heat_0615_normlowconf1` (84 ball labels, EXP-DIST-05's
hard-NORMAL low-conf set). Is the collapse FAR-ONLY (→ system is close to AutoCam in bulk mid/near play) or
WHOLE-GAME (→ selector overhaul mandatory)?

**GT — genuinely mid/near (the contrast that makes this informative):** `heat_0615_normlowconf1\labels.json`
(`action=="ball" and x is not None`): **84 ball** labels (+ 15 not_visible + 12 out_of_play = 111 adjudicated of
116 strips). **y min/p25/median/p75/max = 186 / 486 / 680 / 1115 / 2107**, mean 810 — spans the WHOLE field
depth, vs `spc_normal1` where 100% were y<392. Band split: **far (y<392) 18/84 (21%); mid (392–1080) 43/84;
near (y≥1080) 23/84; "NORMAL" (y≥700) 41/84**. x 61–7633 (full width). Vision-verified (saved+Read PNGs, magenta
ball marker): near f3140 (y2107) = a large crisp white/pink ball at players' feet; mid f5200 (y570) = small ball
in a goalmouth scene; far f5900 (y313) = small ball among players upper-third — a true near/mid/far spread.

**Detector identity + AutoCam baseline (BOTH are caveats — state them up front):**
- The ONLY continuous candidate stream for 6/15 is `det0615\ball_dets_0615.json`, produced by `detect_0615.py`
  = **AutoCam's decrypted `balldet` ONNX** (raw per-frame, mw1600×448, floor 0.05, NMS 0.5, top-20/frame, source
  px), NOT the distill N-checkpoint and NOT v4 champion-J. It carries **top-K candidates** `[cx,cy,conf]`
  conf-descending → a candidate **ceiling IS computable**. Re-running the *distill* detector on 6/15 needs
  GPU/decode (forbidden — curve owns the GPU; no distill 6/15 stream exists; no F: archive entry). So this
  measures **the AutoCam raw per-frame detector's** top-1 recovery + ceiling on mid/near balls — a DIFFERENT
  detector than EXP-DIST-08's distill, but the SAME *selection* failure mode (argmax-of-candidates).
- **AutoCam VIEWPORT (tracked, the strong 0.748 baseline in EXP-DIST-08) is UNAVAILABLE for 6/15.** The 6/15
  broadcast render crashed: the `.mp4.jsonl` viewport sidecar covers only **f1..1078 (t 0–54.5s)** — ZERO
  overlap with the GT frames (3140..102120). So the AutoCam baseline here is **RAW DETECTION (argmax)**, which
  is the same stream as "det top-1". (On `spc_normal1` EXP-DIST-08 scored this same AutoCam-raw detection at
  R15 **0.000** on far balls — the anchor.)

**Method:** reused the EXP-DIST-08 harness verbatim — `world_model/geometry.build_field_geometry` on the
human-tightened 6/15 polygon (`det0615\field_polygon_0615.json`, 10 pts, `human_field_edit`) →
`world_model/eval.evaluate_recall_metric` in **meters** (homography `valid=True`, R10/R15). All 84 GT ball
frames are stride-20 aligned and have an exact entry in `ball_dets_0615.json` (0 absent). Top-1 = max-conf
candidate (argmax); ceiling = ball within R of some top-12 (and top-20) candidate. Pixel R=40 reported as a
transparency check.

**Decomposition (AutoCam-raw detector, meters, n=84 unless noted):**

| metric | ALL (84) | FAR y<392 (18) | MID 392–1080 (43) | NEAR y≥1080 (23) |
|---|---|---|---|---|
| **detector top-1 recovery (argmax)** R15 | **0.464** (39/84) | 0.222 (4/18) | 0.465 (20/43) | **0.652** (15/23) |
| detector top-1 R10 | 0.345 (29/84) | 0.222 (4/18) | 0.302 (13/43) | 0.522 (12/23) |
| detector top-1 median err | 18.3 m | 67.6 m | 17.5 m | **7.4 m** |
| **detector candidate ceiling (top-12)** R15 | **0.845** (71/84) | 0.833 (15/18) | 0.814 (35/43) | 0.913 (21/23) |
| candidate ceiling (top-20) R15 | 0.905 (76/84) | 0.889 (16/18) | 0.907 (39/43) | 0.913 (21/23) |
| **AutoCam (RAW DETECTION = det top-1)** R15 | 0.464 | 0.222 | 0.465 | 0.652 |
| AutoCam VIEWPORT (tracked) | N/A — render crashed @ f1078, no GT overlap |
| **selection success P(top-1 hit \| ball in top-12)** R15 | 0.549 (39/71) | **0.267** (4/15) | 0.571 (20/35) | **0.714** (15/21) |
| nearest-candidate median (detection floor) | 2.4 m | 3.1 m | 3.5 m | 0.5 m |

**Compare to `spc_normal1` (far): det top-1 0.153, ceiling 0.66, selection-recovery 0.22.** AutoCam-raw on
`spc_normal1` far = 0.000 (EXP-DIST-08).

**KEY READ — selection recovers strongly with field depth.** The candidate **ceiling is uniformly high (0.81–0.91)
across ALL bands** and the nearest-candidate median is tiny (0.5–3.5 m) everywhere → **detection is NOT the wall
in any band; the ball is almost always among the top-12 candidates.** What changes with depth is **selection**:
P(argmax = the ball | ball is in top-12) climbs **0.267 (far) → 0.571 (mid) → 0.714 (near)**. Far-third selection
(0.27) is the same collapse `spc_normal1`/EXP-DIST-09 named for the distill detector (its selection-recovery was
0.22 on far); near-field selection (0.71) is nearly 3× better. So the far selection collapse is real and venue/
detector-independent (AutoCam-raw collapses far exactly as the distill did), and it **does not extend to mid/near
play** — there the same per-frame detector's argmax lands on the ball the majority of the time (top-1 R15 0.61 in
the NORMAL band, 0.65 near, median 7.4 m near).

**VERDICT — FAR-ONLY (far-dominant), NOT whole-game.** Mid/near top-1 recovery is dramatically HIGHER than far
(near 0.652 vs far 0.222 R15; mid 0.465), driven by selection success rising 0.27→0.71 while the candidate ceiling
stays flat-high — i.e. the selector breaks specifically in the far third (small ball + many same-pixel-size
distractors), not across the whole game. In bulk mid/near play a per-frame argmax detector is already much closer
to "right", and the EXP-DIST-08/09 "0.15 collapse + selection is the wall" headline is a **far-third phenomenon**,
not a uniform whole-game failure. **A selector overhaul is high-value FOR THE FAR THIRD specifically; it is NOT
mandatory for mid/near play.** Hits/N for every cell are in the table above.

**Caveats (flag all):** (1) **Detector identity** — this is the **AutoCam raw `balldet` detector**, NOT the
distill checkpoint; a true distill-vs-AutoCam head-to-head on 6/15 is impossible without GPU/decode (no distill
6/15 stream exists). The selection metric (argmax-of-candidates) is the same failure mode, and AutoCam-raw's far
collapse (0.27 recovery / 0.22 R15) matches the distill's far collapse on `spc_normal1` (0.22 / 0.153), so the
far-vs-near *trend* is robust; the absolute mid/near distill number is still unmeasured. (2) **AutoCam baseline
is RAW DETECTION, not VIEWPORT** — the 6/15 render crashed, so there is no tracked-camera baseline; the "AutoCam"
row here equals det top-1 (it is the same stream). The strong AutoCam viewport baseline (0.748 on `spc_normal1`)
has no 6/15 analogue. (3) **GT y-distribution** — genuinely mid/near (median 680, 21% far) but it is a *low-conf*
active-learning set (frames AutoCam was uncertain on), so it over-samples hard/ambiguous mid/near cases vs a
random mid/near sample — if anything that BIASES the recovery numbers DOWN, strengthening the FAR-ONLY verdict.
(4) **Meters** via the valid 6/15 homography (reproj OK); px-R40 numbers (much lower, e.g. top-1 7/84) are a
sanity check — far-corner px-radius badly flatters/penalizes per geometry.py's docstring, so meters is the fair
metric, as in EXP-DIST-08/09.

**Curve-alive confirmation:** orchestrator 3620/17348 + iter_run 19452/20432 alive before AND after;
curve.jsonl byte-unchanged (14:59:02, 1384 B, 3 rows N=1/2/4); `curve_gpu.flag=8` — N=8 trained on the GPU
throughout and was not touched; this analysis ran entirely on CPU from the cached candidate stream (no decode).

---

## EXP-DIST-09: full system = detector + `track_ball` temporal tracker on the CLEAN `spc_normal1` GT, head-to-head vs AutoCam (2026-06-24)

**Status:** DONE. CPU-only, read-only w.r.t. the curve. Did NOT disturb the running curve (orchestrator PID
3620/17348 + iter_run PID 19452/20432 alive before AND after; curve.jsonl byte-unchanged at 14:59:02 / 1384 B
/ 3 rows N=1/2/4 throughout; `curve_gpu.flag=8`, GPU on the curve's N=8 training, untouched). Scratch on the
server (`G:\ballresearch\distill\exp_dist_09.py` + `exp_dist_09.json`, diagnostic `diag_09.py`); only this doc
is repo-resident. `CUDA_VISIBLE_DEVICES="-1"` — zero GPU compute, candidate stream only, no video decode.

**Question (Mark's standing one, carried from EXP-DIST-08):** EXP-DIST-08 pinned the detector's clean-GT loss
on **selection** (per-frame top-1 R15 0.153 vs candidate ceiling 0.658, vs AutoCam viewport 0.748). What does
the FULL system — detector + the `track_ball` temporal selector (`world_model/reranker.py`:
action_density_prior → rerank[static-persistence + motion-support + meters-smooth Viterbi] → kalman RTS +
occlusion-coast) — deliver on this SAME clean human `spc_normal1` GT (111 far-third balls, Spencerport,
meters), head-to-head vs AutoCam's 0.748? track_ball caps ~0.58 on the 5 cherry-picked AutoCam-loses clips;
it had never been scored on this continuous clean GT.

**Continuity path taken (path 2 — tracker over continuous stream, score on labeled frames):** the
`spc_normal1` GT frames are sparse (113 ball labels @ ~every-4th, frames 9464–10016), but the candidate stream
`G:\ballresearch\spc_stretch_s1.json` is **fully CONTINUOUS** — 3600 consecutive source frames [7900,11499],
gap=1 everywhere, **12 detector J-peaks (x,y,score) + 20 MOG2 motion blobs per frame**, all 113 GT frames
present. So track_ball ran over the full continuous stream (it needs the temporal continuity) and was scored
ONLY on the 111 clean `spc_normal1` GT frames (deduped vs HARD far human labels — same n=111 as EXP-DIST-08).
Reused the EXISTING `tracked_eval.py` harness logic verbatim (suppress_static_candidates →
`reranker.track_ball(suppressed, geom, motion=action)` → `evaluate_recall_metric` in meters) and the SAME
field polygon/geometry EXP-DIST-08 used (`spc_poly.json` → `build_field_geometry`). No detector re-run.

**CANDIDATE-SET CAVEAT (stated plainly, it bounds the read):** the only CONTINUOUS candidate dump that exists
is `spc_stretch_s1.json`, dated 2026-06-18 — its J-peaks are the **v4 champion-J heatmap detector**, NOT the
curve's N=4 distill checkpoint. (`iter_run.py` only consumes this file's `video`+`polygon`+`lo/hi` and
re-infers the curve model at labeled frames; EXP-DIST-08 re-inferred N=4 on the sparse memmap cache, which is
NOT continuous, so it cannot feed a tracker.) On this champion-J stream the detector's own argmax top-1 R15 =
0.135 and candidate ceiling R15 = 0.811 — close to but not identical to N=4's 0.153 / 0.658. So the
tracking-gain question is answered **internally consistently on one stream** (argmax → tracked → ceiling all
from the same champion-J candidates); the N=4 row is carried as the reference anchor. The verdict is robust to
this: the gap is enormous on either detector.

**Results — comparison on clean `spc_normal1` GT (meters, n=111):**

| system | R10 | R15 | median_m |
|---|---|---|---|
| detector top-1 (EXP-DIST-08, **N=4** distill) | 0.072 | 0.153 | 26.9 |
| detector argmax (THIS stream, champion-J) | 0.099 | 0.135 | 25.6 |
| **detector + track_ball (THIS, champion-J stream)** | **0.135** | **0.153** | **47.3** |
| detector candidate ceiling (N=4, EXP-DIST-08) | 0.604 | 0.658 | — |
| detector candidate ceiling (THIS stream, top-12) | 0.595 | 0.811 | — |
| AutoCam viewport | 0.694 | 0.748 | 6.9 |

**Diagnostic (`diag_09.py`) — WHY tracking barely moves:**
- bare `rerank` (pre-Kalman) R15 = **0.180**; adding `kalman_smooth` RTS/coast → R15 **0.153** (and median
  67.5 m → 47.3 m). So the Kalman step de-jitters (lower median) but on this dim far-ball play it averages a
  mostly-wrong track and does NOT add recall — it slightly LOWERS R15. The peak of the full pipeline here is
  the bare reranker at 0.18, still nowhere near the ceiling.
- **Selection-recovery rate = 0.222.** The true ball is within 15 m of some top-12 candidate in **90/111**
  frames (≈ the 0.811 ceiling), but the reranker's pick is also within 15 m in only **20/90** of those. The
  temporal context (static-persistence + motion + smoothness) recovers the right candidate only ~1 in 5 times.
- The tracked path is NOT degenerate/stuck — picks span x 2154–7381, y 120–1479 (the whole field band); it is
  genuinely roaming among distractors, just rarely landing on the dim far ball. (Nearest-candidate-to-ball
  median = 8.0 m, p25 0.2 m — detection is fine; selection is the wall.)
- Note: `track_ball`'s action-density prior was **OFF** (no per-frame player boxes are dumped for this
  stretch). That term gave +5 pts viewport recall in EXP-31 — a possible (small) future lever, not run here.

**VERDICT — does temporal tracking close the selection gap / reach AutoCam / exceed the ceiling? NO on all
three.** On the clean continuous `spc_normal1` far GT, detector + `track_ball` delivers **R15 = 0.153**
(R10 0.135, median 47 m). It does **not** close the selection gap — it improves on per-frame argmax by only
~0.02 R15 (argmax 0.135 → tracked 0.153) and stays far below the candidate ceiling (0.811 this stream / 0.658
N=4): the tracker is leaving ≥0.5 of recoverable recall on the table, the SAME selection failure EXP-DIST-08
named. It does **not** approach AutoCam's viewport 0.748 (it is ~5× worse; AutoCam median 6.9 m vs tracked
47 m). It does **not** exceed the per-frame ceiling (tracking across no-candidate frames does not net-recover
here — bare rerank 0.18 is the high-water mark and Kalman coast slightly hurts recall). And it is **consistent
with — actually well BELOW — the prior "track_ball maxed ~0.58" claim**: that 0.58 was on 5 hand-picked
AutoCam-loses clips at the **viewport R=15 m** scale; on this continuous clean far-play GT the same tracker
scores 0.153. So the "0.58 ceiling" does not generalize to continuous normal/far play — it was clip-selected,
exactly the "track_ball's 0.58 was cherry-picked" caveat from the tracker/selection finding. **Bottom line:
the full system (detector + track_ball) does NOT beat AutoCam on this clean GT — selection remains the
unsolved bottleneck, and the temporal tracker as configured recovers only ~22% of the candidates the ball is
actually in.** Caveat unchanged from EXP-DIST-08: `spc_normal1` is geometrically far-third GT, so this is a
far-play head-to-head; true near/mid discrimination still needs the labeled `heat_0615_normlowconf1` set.

**Curve-alive confirmation:** orchestrator 3620/17348 + iter_run 19452/20432 alive before AND after;
curve.jsonl byte-unchanged (14:59:02, 1384 B, 3 rows N=1/2/4); `curve_gpu.flag=8` — N=8 trained on the GPU
throughout and was not touched; this re-score ran entirely on CPU from the cached candidate stream.

---

## EXP-DIST-08: HONEST normal-play number — re-score the distill detector on Mark's CLEAN human `spc_normal1` GT (2026-06-24)

**Status:** DONE. CPU-only, read-only w.r.t. the curve. Did NOT disturb the running curve (orchestrator PID
3620/17348 + iter_run PID 19452/20432 alive before AND after; curve.jsonl byte-unchanged at 14:59:02 / 1384 B
/ 3 rows throughout; GPU stayed 60-67% / 1770 MiB on the curve's N=8 training). Scratch on the server
(`G:\ballresearch\distill\reeval_clean_full.py` + `inline_full.py`, `reeval_clean_N04.{json,log}`); only this
doc is repo-resident.

**Question (carried from EXP-DIST-03 / the two 2026-06-24 DECISIONS entries):** the curve's NORMAL split was a
confounded AutoCam-derived proxy (AutoCam dets conf≥0.40 + viewport-x±500, no Y / no on-field test → ~45%
off-field, ceiling-capped, circular vs AutoCam). Mark has now FULLY labeled the clean human set `spc_normal1`
(`D:\training_data\far_label\spc_normal1\labels.json`: **113 `ball` + 26 `not_visible`, all `source:human`,
submitted 14:03–14:15 UTC today**). How much of the reported "NORMAL R15 ≈ 0.15 collapse" is a real model gap
vs an eval artifact?

**Method:** CPU venv `G:\v4bench\wt\.venv`, `CUDA_VISIBLE_DEVICES=-1` + `dev=cpu` (zero GPU compute). Used the
**N=4 checkpoint** (`run_N04/best.pt`, the latest COMPLETE curve row; N=8 was mid-run — untouched). Live PyAV
decode of the 8K H.265 stretch **segfaults** (0xC0000005) on this box, so re-scored from the pre-decoded
`spc_eval_cache` (rebuilt via the proven sequential-PyAV `build_eval_cache.py` to cover all 113 spc_normal1
frames; 626 want-frames, 8.5 min, exit 0). Scored top-1 (argmax) + candidate ceiling (top-12 peaks) for the
detector, AutoCam's own raw max-conf **detection** (`spc_normal_dets.json`), and AutoCam's **viewport**
(`autocam_stretch.json`, = where the camera actually pointed), all in METERS via `evaluate_recall_metric`.

**KEY TIMELINE FINDING — the curve already self-corrected at N=4.** N=1 (eval 10:03) and N=2 (11:52) ran
*before* the labels existed → confounded proxy. **N=4 finished at 14:59, AFTER labeling (14:15)**, so the
EXP-DIST-03 wiring made N=4's NORMAL split ALREADY use the human `spc_normal*` GT (`n_normal=111`,
`ac_R15=0.748` — the viewport value, not the circular 0.958). My independent CPU re-score reproduces N=4's
NORMAL row **exactly** (top1 R15 0.153, ceil R15 0.658), confirming the curve row is honest.

**KEY GT FINDING — `spc_normal1` is geometrically FAR-third, not near/mid "normal play".** All 113 human balls
sit at source-y 7–392 (median 271) of 2160 → top ~13–18% of frame, near the far touchline (x 1991–4934). So
despite the "normal" name, when Mark labeled this Spencerport stretch (9464–10016) the ball was in the FAR
third the whole time. This clean GT therefore tests far-ball localization and overlaps the HARD regime — it is
NOT a near/mid-field discrimination test. (EXP-DIST-03 assumed this stretch was near/mid-field; it isn't.)

**Results (N=4 checkpoint, meters):**

| split (GT) | n | det top-1 R10 | det top-1 R15 | det ceil R10 | det ceil R15 | AutoCam-viewport R15 | AutoCam-rawDet R15 |
|---|---|---|---|---|---|---|---|
| **NORMAL_clean** (human `spc_normal1`) | 111 | 0.072 | **0.153** | 0.604 | **0.658** | **0.748** | 0.000 |
| NORMAL_confounded (AutoCam-proxy, from curve N=4 = same clean GT) | 111 | 0.072 | 0.153 | 0.604 | 0.658 | 0.748 | — |
| NORMAL_confounded ORIGINAL (curve **N=1**, pre-label proxy, n=283) | 283 | 0.028 | 0.155 | 0.367 | 0.629 | 0.958 | — |
| HARD (human far, curve N=4) | 236 | 0.339 | 0.356 | 0.593 | 0.712 | 0.110 | 0.411 |

**Decomposition on the CLEAN GT (N=4):** detector top-1 R15 = **0.153**, candidate ceiling R15 = **0.658**,
AutoCam (viewport) R15 = **0.748**. So of the gap to a perfect 1.0: detection ceiling loses 0.34 (the ball is
in the candidate top-12 only 66% of the time) and **selection loses another 0.50** (ceiling 0.658 → top-1
0.153) — selection (picking the ball OUT of the candidates) is by far the dominant failure, exactly as the
prior "selection is the unsolved bottleneck" finding said. AutoCam's viewport (0.748) beats the detector's
top-1 (0.153) **and** its ceiling (0.658) on this clean far GT.

**AutoCam-baseline nuance (important, non-circular):** AutoCam's *raw max-conf detection* scores **0.000** on
this clean GT — on these frames its highest-conf raw output is a low-conf (0.2–0.5) FALSE POSITIVE parked at
the far-right (~6058,609) while the true ball is at (3500,100). AutoCam only "finds" the ball via its
internal tracking/smoothing → the **viewport** (camera centre) is the fair "what AutoCam delivers" baseline,
and it tracks the true far ball to within ~15–25 m (R15 0.748). The confounded proxy's `ac_R15=0.958` was the
circular artifact: it scored AutoCam against balls AutoCam itself detected at conf≥0.40.

**VERDICT — was the 0.15 collapse an artifact? NO, the detector's normal number is REAL, not an eval
artifact.** On Mark's clean human GT the detector's normal/far-play top-1 R15 is **0.153** — statistically
indistinguishable from the "confounded collapse" number (0.155 at N=1, 0.153 at N=4). Cleaning the GT did
**not** materially raise the detector's number (Δ ≈ 0.00). What the artifact *did* distort was the
**comparison**: it inflated AutoCam's apparent edge (circular `ac_R15` 0.958 vs the honest viewport 0.748) and
slightly depressed the ceiling (0.629 → 0.658). So the headline "distill collapses on normal-ish play while
AutoCam wins" survives clean GT — and the decomposition pins the cause on **selection, not detection or a GT
artifact** (ceiling 0.658 ≫ top-1 0.153). Caveat to weigh next: `spc_normal1` turned out to be far-third GT,
so this confirms the collapse on FAR-ish play but still leaves true near/mid-field discrimination only
partially tested — the hard-NORMAL human set `heat_0615_normlowconf1` (EXP-DIST-05, mid/near band) is the GT
that would close that gap once labeled.

**Curve-alive confirmation:** orchestrator 3620/17348 + iter_run 19452/20432 alive before+after; curve.jsonl
unchanged (14:59:02, 1384 B, 3 rows N=1/2/4) — N=8 was training on the GPU throughout (61% util) and was not
touched; the re-score ran entirely on CPU from the memmap cache.

---

## EXP-DIST-07: SAME-VENUE far-ball visibility — 6/10 vs 6/15, both at Parma Town Hall Park (2026-06-24)

**Status:** DONE. CPU-only, read-only w.r.t. the curve/labels. Did NOT disturb the running curve (orchestrator
PID 3620, iter_run PID 19452 both alive before+after; curve advanced N=2→N=4 written, training N=8; GPU 13% /
520 MiB — untouched). Scratch on the server (`G:\ballresearch\distill\contrast_cmp_parma\`); only this doc is
repo-resident. Snapshotted `det0610\ball_dets_0610.json` → `ball_dets_0610_contrast_snap.json` before reading.

**Why:** EXP-DIST-06 answered "did the 6/15 contrast calibration help far-ball visibility?" by comparing PRE-cal
6/04 (Irondequoit, **Camp Eastman Way**) vs POST-cal 6/15 (Irondequoit, **Parma**) — but the venues, sun, and
grass all differed, so the gain was NOT attributable to the calibration. This experiment removes the venue
confound: **PRE-cal 6/10 vs Lakefront** and **POST-cal 6/15 vs Irondequoit are BOTH at Parma Town Hall Park,
same Reolink camera, both evening kickoffs** (dir-name capture starts 18:31:10 vs 18:27:13 — ~4 min apart in
clock time and only 5 days apart in the season, so sun elevation is ~identical → sun-angle is a weak confound
here, unlike 6/04). The 6/10 detections that EXP-DIST-06 said didn't exist now do
(`det0610\ball_dets_0610.json`, 5733 frames, completed 6/24 15:50).

**Method:** CPU venv `G:\v4bench\wt\.venv`, PyAV decode. PRE far balls from confident 6/10 AutoCam dets
(`ball_dets_0610_contrast_snap.json`, `{frame:[[cx,cy,conf]]}`, source 7680x2160); POST far balls from Mark's
**HUMAN** far labels (`D:\training_data\far_label\heat_0615_gaps1\labels.json`, action=="ball"). 360x360
ball-centred crops + 1200x600 context crops. Quantitative proxies = EXP-DIST-06's: Rec.601 luma on a tight ball
patch (r≤9) vs a close grass annulus (r 16-42) → `lum_delta` (ball−grass mean), `grass_std`, `cnr`
(lum_delta/grass_std), `peak_delta` (brightest ball px − grass mean). **Every crop vision-checked with a magenta
ball-centre marker overlay before its metric was trusted.**

**Key finding 1 — the PRE side is data-starved (honest blocker):** of **30** confident (conf≥0.55) 6/10 far
detections vision-screened across cy 200-680, only **~3 were genuine ball-on-grass**. The rest are FALSE
POSITIVES on white field lines / the centre-circle arc, players, the referee, spectators, or empty grass — the
exact FP-attractor classes from the distill-label-filtering finding. So a clean 6-pair matched set could not be
built from 6/10 dets; this in itself confirms far-third AutoCam is weak at this venue.

**Key finding 2 — metrics on the verified-clean (quality-A) ball-on-grass crops:**

| cy | game | dLum | grassStd | cnr | peakD | vision |
|----|------|------|----------|-----|-------|--------|
| 462 | PRE 6/10 | +6.0 | 22.5 | 0.27 | 77.5 | white ball on sunlit grass |
| 497 | PRE 6/10 | +0.6 |  9.6 | 0.07 | 85.1 | white ball on grass (ball luma ≈ bright grass) |
| 334 | POST 6/15 | +27.0 | 60.9 | 0.44 | 136.4 | crisp white ball, bright grass |
| 348 | POST 6/15 | +14.3 | 15.9 | 0.89 | 111.8 | isolated white ball on sunlit grass |
| 497 | POST 6/15 | −11.5 | 18.6 | −0.62 | 84.9 | white ball but on very-bright sunlit grass; ball's shadowed underside reads darker |

A-only means: **PRE** dLum +3.3, peakD 81.3, cnr 0.2, grassStd 16.1 / **POST** dLum +9.9, peakD 111.0, cnr 0.2,
grassStd 31.8. (One PRE crop cy337 is quality-B — ball adjacent to a player, snap unreliable — excluded from
means.) The directly-matched **cy497 pair** (both clean, same cy): PRE dLum +0.6 / peakD 85.1 vs POST dLum −11.5
/ peakD 84.9 — essentially a wash.

**Interpretation (be honest about the proxy):** the far ball is only ~5-8 px, so the patch-MEAN proxies
(lum_delta, cnr) are unstable to ±3 px centring error and to whether the local grass is sun-lit or shadowed —
they do NOT robustly separate PRE from POST. The more centring-robust `peak_delta` is consistently higher POST
(111 vs 81), and by eye the POST balls are crisper bright-white spheres on lighter green turf. BUT POST's grass
is also brighter and ~2x more textured/striped (grassStd 32 vs 16), so CNR is flat (0.2 both) and the one clean
same-cy pair (cy497) is a wash. So the quantitative signal is mixed/weak, not a clean POST win.

**Confounds remaining even with venue controlled:** (1) **Auto-exposure / auto-gain + weather** — the camera's
AE and the evening's cloud/haze differ between the two dates; POST grass being brighter+more textured is as
consistent with a brighter/clearer evening (or AE) as with the calibration. (2) **Asymmetric label sources** —
POST uses precise HUMAN labels; PRE uses AutoCam dets that are mostly FPs in the far third, so the PRE sample is
both smaller and selected by a detector that struggles exactly here (survivorship: the few PRE balls that ARE
real may be the easier/brighter ones). (3) cy497 PRE happens to sit on very bright sunlit grass, dragging its
dLum to ~0 — small-N sensitivity to grass illumination. N is tiny (3 PRE / 3 POST clean).

**Conclusion / verdict:** **Even with the venue confound removed (both Parma, same camera, near-identical sun),
the data do NOT cleanly show the far ball is more visible against grass post-calibration. LOW confidence in a
real calibration effect.** Directionally POST balls look a bit crisper and peak brighter (peakD 111 vs 81), but
patch-mean contrast/CNR is flat, the one clean same-cy pair is a wash, and the gain is fully confoundable by
auto-exposure / evening-light / brighter-grass differences plus the asymmetric (human-vs-FP-laden-detector) label
sources. This is a more honest NULL-ish result than EXP-DIST-06's "directionally helps": controlling venue
*shrank* the apparent effect. To actually isolate the calibration we'd need PRE+POST frames with matched
auto-exposure / identical lighting (e.g. two halves of the SAME game, or locked-exposure test footage) — visibility
alone, eyeballed off two different evenings, can't carry the claim.

**Artifacts (`G:\ballresearch\distill\contrast_cmp_parma\`):** 6 ball crops (`{PRE,POST}cal_*_cy*.png`, 3 each),
6 context crops (`*_CONTEXT_*.png`), `COMPARISON_pre_vs_post_parma.png` (labelled side-by-side w/ metrics),
`crop_index.json` (per-crop verified centres + metrics), `picks.json`.

## EXP-DIST-06: Did the 6/15 camera contrast calibration make the far ball more visible? (2026-06-24)

**Hypothesis:** Mark's 2026-06-15 Reolink contrast calibration improves far-ball-vs-grass separability vs a
pre-calibration game (same opponent Irondequoit, same camera).
**Method:** CPU-only, read-only. Matched far-ball crops (small cy = far third, near far touchline) from PRE-cal
6/04 (`guzzetta__2026.06.04_vs_Irondequoit`, archived detections `F:/archive/ball_distill/.../ball_track.json`)
and POST-cal 6/15 (`heat__2026.06.15_vs_Irondequoit_away`, Mark's HUMAN far labels
`D:/training_data/far_label/heat_0615_gaps1/labels.json`). 6 pairs matched by cy
(113/230/334/462/563/623). Full-res 360×360 ball-centered PNG crops + one 1200×600 context crop per game →
`G:/ballresearch/distill/contrast_cmp/`. Vision-checked each; quantitative proxy = ball-patch vs surrounding-grass
annulus on Rec.601 luma (lum_delta, grass_std, CNR, peak_delta).
**Result:** Mean over 6 matched crops — PRE 6/04: CNR 0.34, dLum +7.8, peakD 54.8, grassStd 24.8;
POST 6/15: CNR 1.42, dLum +21.8, peakD 89.2, grassStd 52.1. POST ball is brighter relative to grass on every
grass-backed crop; its higher grassStd (textured/striped sunlit Parma turf) depresses the CNR ratio but the raw
ball-vs-grass luminance gap is clearly larger. Vision: on the clean ball-on-grass crops (cy230, cy334) the POST
ball is a crisp white sphere popping off bright light-green turf; the matched PRE crops sit on darker/shadowed
grass where ball luma ≈ grass luma (the ball reads mainly by shape, not brightness).
**Confounds (state plainly):** (1) DIFFERENT VENUE — 6/04 at "21 Camp Eastman Way" (dimmer evening sun, darker
deep-green grass, dark tree-line) vs 6/15 at "Parma Town Hall Park" (brighter low sun, lighter striped turf). So
lighting+grass differ, NOT just the calibration. (2) Top-row crops (cy113) are ball-airborne-against-trees in BOTH
games — background is dark foliage, not grass, so their high CNR is a background artifact, exclude from the
ball-vs-grass read. (3) Two PRE picks (cy230, cy623) and one PRE (cy563, ball-in-keeper's-hands) are
tracker-derived conf-0.05 detections that look imprecise/possibly-false on inspection; PRE far labels are noisier
than 6/15's human labels. (4) One POST crop (cy462) is a goalmouth — ball partly against the white net, the only
POST crop where it's harder to isolate (net confound, not grass). No clean same-venue pre-cal reference exists
with ball detections (6/10 vs Lakefront at Parma has none; not pursued).
**Conclusion:** The POST-cal 6/15 far ball IS more visible against grass on matched crops — both by eye and by
ball-vs-grass luminance gap (dLum +22 vs +8, peakD 89 vs 55). LOW-TO-MODERATE confidence that this is the
*calibration*: the venue/lighting difference (brighter sun + lighter turf at Parma) plausibly accounts for much or
all of the gain, and 6/04 is not a same-venue control. Directionally consistent with the calibration helping, but
NOT cleanly attributable. A same-venue (Parma) pre-cal game with ball detections would be needed to isolate the
calibration effect.
**Artifacts:** `G:/ballresearch/distill/contrast_cmp/` — 12 ball crops (`{PRE,POST}cal_*_cy*.png`), 2 context
crops (`*_CONTEXT_*.png`), `picks.json`, `crop_index.json` (per-crop metrics).

## EXP-DIST-05: 6/15 hard-NORMAL low-conf active-learning label set `heat_0615_normlowconf1` (2026-06-24)

**Status:** DONE — set built + served + vision-verified. Additive, CPU-only; did NOT disturb the running
curve (orchestrator + iter_run), the `detect_0615.py` pass (which finished naturally during this work,
5408/5408), or Mark's `heat_0615_gaps1`/far labeling. Scratch on the server (`G:\ballresearch\`); only docs
are repo-resident.

**Why (the bottleneck this targets):** the distill wins on FAR/hard balls but COLLAPSES on NORMAL play —
the corrected curve's own rows now show it directly: NORMAL R15 = 0.155 (N=1) / 0.081 (N=2) vs AutoCam
0.958 / 0.748; HARD R15 = 0.386 / 0.22 vs AutoCam 0.11. The unsolved gap is normal-field *discrimination*
(true ball vs players/shadows/corner flags/goalmouth clusters), not far-ball recall. So the most valuable
active-learning GT right now is human labels on **hard-NORMAL** frames — ball in the mid/near field where
AutoCam is genuinely uncertain (low-conf). This is the producer side of the EXP-DIST-03 normal-eval fix:
not just honest eval GT (`spc_normal1`), but hard *training/AL* GT on a real production game (6/15 Reolink,
first post-contrast-calibration game).

**Method:**
- Inputs (server): game-wide CPU AutoCam ball dets `G:\ballresearch\distill\det0615\ball_dets_0615.json`
  (`{frame:[[cx,cy,conf],...]}`, source px 7680x2160) — SNAPSHOT-copied to `ball_dets_0615_normlc_snap.json`
  before reading (no mid-write read); the HUMAN-tightened field polygon
  `det0615\field_polygon_0615.json` (`source: human_field_edit`); the canonical active-play windows
  `G:\ballresearch\play_windows.json` key `guzzetta__2026.06.15_vs_Irondequoit` = `[1902,49459) ∪ [58117,105714)`.
- New builder `G:\ballresearch\distill\build_0615_normlowconf.py` (a COPY of the gap builder; existing
  `build_0615_set*.py` and the `heat_0615_gaps1` set untouched). Selection: top-1 (on-field-preferred)
  detection per frame; **NORMAL band cy > 700** (excludes the far third, where the existing FAR sets live at
  cy ≤ ~480-700); **LOW CONF conf ∈ [0.08, 0.40)** (above the 0.05 floor, below AutoCam's ~0.40 confident
  threshold); also flagged "ambiguous" frames (a 2nd on-field candidate within 0.12 conf and ≥1200 px away =
  no clear winner). Gated to active play; excluded the 53 `heat_0615_gaps1` frame_idx to avoid dup labeling.
  Temporal-spread subsample (best-score per bin) for game-wide diversity. Seeds AutoCam's top-1 as the hint
  (`autocam:true`, `hint_conf` recorded) so Mark confirms with C / clicks to correct.

**Thresholds used + yield at each (from the snapshot, 5200 det frames at build time):**
- active-play frames (excl far-third filtering + excl gaps1): 4624
- on-field top-1: 3856 → **NORMAL band (cy>700, on-field): 1073**
- **NORMAL & low-conf [0.08,0.40): 464** (of which also ambiguous: 123); NORMAL & ambiguous-only (conf≥0.40): 7
- NORMAL-band conf histogram (cumulative): ≤0.10 → 24, ≤0.15 → 107, ≤0.20 → 215, ≤0.30 → 364, ≤0.40 → 475
- pool 471 → temporal-spread subsample (TARGET=200 bins) → **116 selected**, reason mix 42 normlowconf /
  73 normlowconf+ambig / 1 norm+ambig; cy 701-1930 (all > 700), conf 0.090-0.519, frame span 1980-102120.

**Output:** `D:\training_data\far_label\heat_0615_normlowconf1\` — 116 full-frame (7680x2160) strips
(~5.4 MB each) + `manifest.json`. Manifest marks `band:"normal"` and **`far_cut:0`** so the labeler classifies
NO frame as "far": these are reached via the **Next-unlabeled (U)** sweep / the pending auto-advance fix, NOT
the far (F) sweep (`isFar()` needs hint_y ≤ FARY≤480; ours are >700). `firstUnlabeled`/`gotoUnlabeled` have no
far/autocam gate, so every selected frame is reachable today; the AutoCam hint still renders (dashed green
arrow, `autocam:true`).

**Verification (live server, no restart):**
- `GET http://127.0.0.1:8642/api/far-label/heat_0615_normlowconf1` → 200, n_frames=116, band=normal,
  far_cut=0; set appears in `/api/far-label` list; `/strip/{i}` for first/mid/last → 200 image/jpeg ~5.4 MB.
- **Vision check (per the verify-label-semantics rule):** overlaid the seeded hint + tightened polygon +
  NORM_Y line on 4 frames spread across conf+time and Read them. Confirmed: all hints in the NORMAL mid/near
  band, on-field, inside the human polygon; detections genuinely uncertain (conf 0.10-0.31) — f1980 (0.177)
  fires on a yellow-shirt player while the true ball sits far-right on the ground (a real FP-on-person the
  labeler will correct); f29280 (0.098) at a player's feet/shadow; f70900 (0.152) at a player by the corner
  flag; f102120 (0.305) in a goalmouth scramble. These ARE the discrimination-bottleneck cases = the point.
- All protected jobs alive after the work: orchestrator (3620), iter_run/curve (280), and detect_0615
  finished cleanly on its own (DONE, 5408/5408). curve.jsonl unchanged by this work (N=1,2 rows pre-existing).

**Ready-to-click URL for Mark:**
`https://trainer.goat-rattlesnake.ts.net/static/far-label.html?set=heat_0615_normlowconf1`
(use the **U / "Next unlabeled"** sweep — these are NORMAL, not far; C to accept AutoCam's guess, click to
correct, N not-visible, O out-of-play).

**Conclusion:** 116-frame hard-NORMAL low-conf AL set built on the real 6/15 game, complementing the FAR
`heat_0615_gaps1` set. Once labeled it gives the first human GT on the detector's actual failure mode
(normal-field discrimination) for a production Reolink game — the highest-value labeling available now.

---

## EXP-DIST-04: 6/15 Irondequoit active-play windows persisted + wired into the far-label/distill path (2026-06-24)

**Status:** DONE. Additive; did NOT disturb the running curve, `detect_0615.py`, or Mark's `heat_0615_gaps1`
labeling. Scratch lives on the server (`G:\ballresearch\`); only docs are repo-resident.

**Goal:** persist Mark's exact active-play windows for `heat__2026.06.15_vs_Irondequoit_away` in the
canonical per-game store and wire them into the active-play filter so the full far-label rebuild and 6/15's
training data exclude warmup/halftime/post-game.

**Windows (Mark's YouTube timestamps; raw `…-raw.mp4`, 108160 frames @ a *measured* 19.815 fps):**
kickoff 1:36→f1902, halftime 41:36→f49459, half-end 48:53→f58117, game-end 1:28:55→f105714.
ACTIVE PLAY = `[1902,49459) ∪ [58117,105714)` (88.0% of the recording). Recomputed and confirmed against the
container (`av`: `average_rate=19.8149`, `frames=108160`, `duration=5458.5 s`) — matches Mark's frames.

**Where persisted (the EXISTING mechanism — no new format invented):** `G:\ballresearch\play_windows.json`,
the canonical active-play store for 2026 games (which have no `manifest.db`). It is consumed by
`gamedata_sources.play_windows()` (the standard per-game accessor: `manifest.db` human `game_phases` for
2024/25 → falls back to this JSON for 2026), by `iter_run.py` (the distill training builder, exact-key
window gate `inplay(base+f)`), and by `orchestrator.py` (a game is only curve-eligible if its key is in this
file). Entry keyed **`guzzetta__2026.06.15_vs_Irondequoit`** — the `ball_distill` archive-dir convention all
existing entries use (Heat archives carry the legacy `guzzetta__` prefix; the registry id is
`heat__…`). Schema matches existing GT entries: `{fps, start, half_start, half_end, end, windows:[[a,b],…]}`.

**fps deviation (documented, intentional):** existing 2026 entries store frames as `seconds × 20` (an integer
`FPS=20` proxy in `add_play_windows.py`). 6/15's raw video is genuinely 19.815 fps, so its windows are stored
at the **true fps** (verified above). These raw-frame indices equal the archive global-frame space (concat of
the 19 segments), so they line up with the `iter_run.py` consumer's `base+f` global index without conversion.

**Wiring:**
1. **Far-label builders** `build_0615_set.py` (full) + `build_0615_set_partial.py` (partial): added an
   AUTHORITATIVE window gate right after the detection-density `active` set — `active &= {f : inplay(f)}`,
   reading the windows from the canonical `play_windows.json`. The density/motion proxy now only *refines*
   within Mark's windows; any frame outside them is hard-excluded. Builders compile clean; NOT re-run (Mark
   is labeling the partial set — the full rebuild waits for `detect_0615.py` to finish).
2. **Training-data path** `iter_run.py`: NO code change needed — it already reads `play_windows.json` by exact
   key and gates crops with `inplay(base+f)`. With the entry present, once 6/15 is archived to
   `F:\archive\ball_distill\guzzetta__2026.06.15_vs_Irondequoit\`, both the curve selection
   (`orchestrator.py`) and the crop builder arm automatically.

**Verification (read-only; nothing perturbed):**
- Dry-run `inplay()` on the requested indices: f1000 excluded, f5000 included, f52000 (halftime) excluded,
  f70000 included, f106000 excluded — all PASS. Boundaries half-open as specified (1902 in / 49459 out /
  58117 in / 105714 out).
- Ran the builder's active-set logic against the LIVE partial detections (frames 0–27980): density-only
  active=1156 → after window gate=1060, the 96 dropped frames were exactly the pre-kickoff warmup (0–1900,
  all < 1902); min surviving frame 1920 ≥ kickoff. Proves the gate removes warmup the proxy alone admits.
- All three protected processes alive after the work: orchestrator (PID 3620), iter_run/curve (15576),
  detect_0615 (3628); detect_progress advanced 25980→27980; `curve_gpu.flag=2` unchanged.

**Conclusion:** 6/15's active play is now canonical and consumed by the existing filter with one source of
truth. The full `heat_0615_gaps1` rebuild and 6/15-as-distill-game will both exclude non-active frames with
no further changes. Backups: `play_windows.json.bak_0615_*`, `build_0615_set*.py.bak_winsgate_*`.

---

## EXP-DIST-03: Make the curve's NORMAL eval honest — human normal-play GT (2026-06-24)

**Status:** investigation DONE + fix wired + new human-label set built and served (awaiting Mark's clicks).
Scratch lives in `G:\ballresearch\distill\` (NOT in repo). Does NOT disturb the running corrected curve.

**Problem (carried from EXP-DIST-02):** the curve's NORMAL split is confounded. NORMAL GT =
AutoCam-detector dets (conf≥0.40, viewport-x±500, **no Y, no on-field test**); of 283 such "normal" balls
only **120 (42%) fall inside the field polygon** → the detector is structurally unable to fire on ~45% of
"normal" targets and the NORMAL ceiling is capped ~0.55 regardless of model quality. So the NORMAL number
is partly an artifact, not a real weakness. (HARD/far split is clean — 236 human far-label frames, ≈92%
in-mask — and trustworthy: detector R15 ≈ 0.39 vs AutoCam 0.11.)

**Question for this session:** can Mark's EXISTING hand-labels stand in as honest continuous NORMAL-play GT,
or is a NEW label set needed?

**Method:**
1. Inspected all `spc_clip1..5` + `spc_diverge` labels.json/manifests. They ARE dense + continuous
   (1 label every 4 source frames = ~5 fps, `ball` + `not_visible` actions, human-clicked), so they look
   like good GT — BUT the ball positions are all in the **far third** (y 104–604 of a 2160-tall frame;
   the far-label tool targets the balls AutoCam loses). Vision-verified (saved+Read PNGs with the
   labeled ball + polygon drawn) 3/3 sampled frames — spc_clip2 f8435, spc_clip3 f9019, spc_clip4 f10909
   — every one is a small **far ball** near the far touchline / far goal, NOT a near/mid-field normal ball.
   The curve already ingests these as the HARD split (236 in-window frames, all from spc_clip2/3/4 +
   spc_diverge). **There is NO human GT for near/normal-play balls anywhere.**
2. Therefore: **a genuinely new normal-play set IS needed** (fork B). The existing labels correctly serve
   HARD; the NORMAL split has no honest human GT to use.

**Action taken:**
- **Built a new human-label set `spc_normal1`** with the SAME canonical tool that made `spc_clip*`
  (`G:\ballresearch\farlabel_clip.py`, full-frame strips + manifest the running annotation server serves
  from `D:\training_data\far_label\`). Stretch = **Spencerport frames 9460–10020** (sample=4 → 141 frames)
  — a continuous active-play segment INSIDE the curve's eval window [7900,11500], in the clean gap between
  spc_clip3 (ends 9447) and spc_clip4 (starts 10909), full AutoCam viewport coverage (camera pans the
  whole field, vp_x 2134–7024), so the ball travels through near/mid field = normal play. URL for Mark:
  `https://trainer.goat-rattlesnake.ts.net/static/far-label.html?set=spc_normal1`. Verified
  `GET /api/far-label/spc_normal1` → 200 with 141 frames on the live server (PID 7520, jared clone).
- **Wired the NORMAL split to prefer human `spc_normal*` labels** in `iter_run.py` AND `reeval_clean.py`
  (server scratch): HARD ingestion now excludes `spc_normal*`; NORMAL GT uses human `spc_normal*` labels
  when present, else falls back to the old AutoCam-derived proxy (so the running curve is NOT broken before
  Mark labels). The orchestrator launches a fresh `iter_run.py` per N, so the moment `spc_normal1` is
  labeled, the next N (and any `reeval_clean.py` re-run) reports an HONEST NORMAL number with no further
  code change.

**Result:** Fork = **B (new labels needed)**. New normal-play eval set `spc_normal1` (141 frames) built and
served; NORMAL eval wiring made human-GT-first with graceful fallback. Once Mark labels the 141 frames the
curve's NORMAL R15 becomes interpretable for the first time and the venue-diversity question (does NORMAL
R15 rise with N?) can be answered honestly.

**Conclusion:** The `spc_clip*` "Spencerport human labels" are FAR-ball GT (correctly HARD), not normal-play
GT — confirmed by vision + the y-distribution. The honest NORMAL fix is one new ~141-frame human pass on a
near/mid-field stretch, not re-using existing labels. NORMAL numbers stay non-interpretable until then.

---

## EXP-DIST-02: Corrected data-scaling / venue-diversity curve (2026-06-24)

**Status:** corrected curve RE-LAUNCHED and running (server GTX 1060). Prior `curve.jsonl` (N=1..16,
6/19–6/23) is **INVALID** — see method. Scratch: `G:\ballresearch\distill\` (NOT in repo).

**Hypothesis (carried from the distill plan + the prior "normal-play gap" finding):** the distilled
detector wins big on far/hard balls but collapses on normal play; the open question is whether the
normal-play bottleneck is **venue diversity** (more games/venues) vs capacity/quantity. The curve
trains HeatmapNet (base24, no-aug, fixed 30k-step budget so only DATA DIVERSITY varies) on N = 1,2,4,8,16
archived AutoCam-distilled games and evals in **meters** on the held-out Spencerport stretch (frames
7900–11500), split HARD (human far labels) vs NORMAL, vs the AutoCam baseline.

**Method / what was found this session:**
1. **Verified the *previous* session's polygon fix is real:** `resolve_polygon()` (canonical
   `gamedata.polygon(gd.resolve(gid))` → v4_fields date+opp → manifest.db) resolves **16/16** archived
   games (15 via gamedata, 1 via v4_fields). The old fuzzy date+opp resolver matched only ~4 (archive
   games are named `guzzetta__*`/`flash__*`; polygon dirs are `heat__*`).
2. **BUT discovered a SECOND, independent bug that invalidated the entire `curve.jsonl`:** the curve
   rows N=1..16 were produced by an **older `iter_run.py`** (run 6/23 06:44, before the fix finished at
   6/23 21:24). Its run logs show 12/16 games hit `"NO polygon … skip"` and **N=16 actually trained on
   only 4 games** (`train roots (4 games)`, 44 segment-games, 20,303 labels). So every "data-scaling"
   point trained on ~4 games regardless of N — the curve was flat **by construction**, not because
   venue diversity doesn't help. `"NO polygon … skip"` does not even exist in the current code.
3. **DRYRUN of the CURRENT `iter_run.py` on all 16 games → resolves 16/16, 140 segment-games, 52,367
   labels** (+992 human overrides, 94,215 hard-negative false-fires). Confirmed the fix end-to-end
   before relaunch.
4. **Found the NORMAL-split eval is confounded** (the headline metric was partly an artifact): the
   curve's NORMAL GT = AutoCam-detector dets conf≥0.40 corroborated only by viewport-x within 500px —
   **no Y constraint, no on-field test.** Of 283 normal GT balls, **128 (45%) are off-field and/or fall
   outside the eroded eval band-mask** (cluster at the far-right sideline corner, x 6826–7430). So the
   detector is structurally unable to fire on ~45% of "normal" targets → ceiling capped at ~0.55,
   independent of model quality. HARD (human) GT is clean (92% in-mask) and trustworthy.

**Result:** Prior curve quarantined to `curve.jsonl.buggy4games_20260624_081941`. Corrected curve
relaunched 2026-06-24 08:19 via the detached orchestrator; N=1 confirmed training (GPU 27–72% util,
loss 0.012 @ step 2k). HARD numbers from the buggy curve already show the **real, trustworthy** signal:
detector R15 ≈ 0.39 vs **AutoCam 0.11** on hard/far balls — the distill beats AutoCam there. NORMAL
numbers from the buggy curve are NOT interpretable (4-game training + confounded GT).

**Conclusion:** The venue-diversity question is **still open** — it was never honestly tested (curve
trained on 4 games). The corrected 16-game curve will answer it. Separately, the NORMAL eval must use a
**clean on-field + in-band GT** before its numbers mean anything (a CPU re-eval harness, `clean_eval.py`,
quantifies the artifact: raw_normal vs clean_normal=155). Two distinct bugs (fuzzy resolver; stale
binary) silently gutted the curve — re-verify game-count from the run log (`train roots (N games)`),
never trust the curve row alone.

---

## EXP-008: Field-boundary distillation pipeline (2026-06-11)

**Hypothesis:** A small in-house CNN can reproduce the teacher's 10-point field polygon closely enough — IoU ≥ 0.90 vs teacher, gate agreement ≥ 90%, per-point error ≤ ~8px in 768×384 — to replace it as a drop-in ONNX.
**Method:** Standalone distillation — label-gen (teacher over Reolink footage) → placement-split dataset + heavy augmentation → ResNet18 dual-head student → ONNX export matching the teacher's I/O signature + parity check. Corpus: ~33 Reolink games (7680×2160) from `D:/soccer-cam-storage`, ~9 venues, Heat-heavy plus a few Flash; Dahua footage excluded.
**Result (2026-06-12, GPU server):** Generated 1,000 teacher labels over 21 Reolink games → 8 placement clusters (1 Flash + 7 Heat), split train=688 / val=66 / test=246. ResNet18 student, early-stopped epoch 87 (best epoch 72, val pixel error 15.1 px ≈ 2% of 768 width). Held-out **test** (davis + hilton): overall IoU 0.64, gate-agree 0.84 — but per-cluster: **davis_park IoU 0.79**, **hilton_high_school IoU 0.32**. Export parity vs teacher on representative frames: **IoU 0.936, gate-agree 1.00**, mean per-point delta 20.6 px; ONNX signature byte-identical to teacher, drop-in through unmodified `field_detector.py` verified (20/20), checkpoint-vs-ONNX deviation 0.25 px.
**Conclusion:** Distillation works on normal grass venues (davis 0.79, representative-frame parity 0.94) — a viable v1 drop-in. The 0.90 bar is not met *overall* because hilton_high_school is an **American-football turf field** (yard lines, glare) where the teacher itself is unreliable (mean_score 0.43, zero gate-pass frames) — out-of-distribution, not a model defect. Per-point: near-center (pt 2) best (9.7 px), intermediates/corners worst (pt 1: 32.9 px). Next: exclude football-field venues or add human-corrected labels for anomalous venues (v2); more venues would raise the floor. Winning backbone: resnet18.
**Artifacts:** `F:/training_checkpoints/field_outline/student.onnx`, run `training/runs/field_kpts_v1/`.
**Code:** `training/field_outline/`, `training/cli/*_field_outline.py`

## EXP-007: Game phase detection from multi-ball patterns (2026-03-30)

**Hypothesis:** Warmup/halftime/postgame have multiple scattered ball detections; active play has a single ball trajectory.
**Method:** `game_phase_detector.py` — 30-second rolling windows, count frames with >3 concurrent detections spread >500px apart. Phase transitions at multi-ball/single-ball boundaries.
**Result:** Generated manifests for 9 games. Most games show clear warmup→first_half→halftime→second_half→postgame progression.
**Conclusion:** Works for standard games. FAILS for tournaments — sub-game breaks detected as single long halftime. Multi-game recordings need per-sub-game phase detection.
**Data:** `F:/training_data/game_manifests/{game_id}.json`

## EXP-006: Far-field gap detection across all rows (2026-03-30)

**Hypothesis:** ONNX trajectory gaps (missing detections between linked positions) exist in r1/r2 too, not just r0.
**Method:** `exp_allrow_gaps.py` — trajectory linking + gap detection on all tile rows.
**Result:** 19,239 gaps total. r0: 6,967, r1: 9,491, r2: 2,781. r1 has the most gaps.
**Conclusion:** Gap filling should target all rows, not just far-field. r1 (mid-field) is the biggest opportunity.
**Data:** `F:/training_data/experiments/exp_allrow_gaps.json`
**Code:** `training/experiments/exp_allrow_gaps.py`

## EXP-005: Targeted frame diff at gap positions (2026-03-29)

**Hypothesis:** Frame differencing at ONNX gap positions (where ball should be but wasn't detected) will find missed balls with fewer false positives than blind frame diff.
**Method:** `exp3b_fullscale.py` — seek to each gap frame in video, extract small region around predicted position, check for motion blob matching ball size/circularity.
**Result:** 4,570 verified motion candidates, 1,565 high-confidence (size 15-200px², circularity >0.5, on-field).
**Conclusion:** Gap-guided targeting dramatically reduces false positives vs blind frame diff. 797 Sonnet-verified as real balls.
**Code:** `training/experiments/exp3b_fullscale.py`

## EXP-004: ONNX trajectory gap mining (2026-03-29)

**Hypothesis:** When ONNX detects a ball in r0 in frames N and N+2 but not N+1, the ball is likely still there in N+1 — the model just missed it.
**Method:** `exp1_onnx_gaps.py` — trajectory linking on r0 labels, find frames where detections are missing between linked positions, interpolate expected position.
**Result:** 11,425 gap candidates across 9 games (avg 1,269/game).
**Conclusion:** Gaps are real and frequent. Most gaps are 1-3 frames — brief occlusions or model uncertainty. Provides high-quality training targets.
**Data:** `F:/training_data/experiments/exp1_onnx_gaps.json`
**Code:** `training/experiments/exp1_onnx_gaps.py`

## EXP-003: Blind frame differencing for small balls (2026-03-28)

**Hypothesis:** Motion-based detection (frame differencing) can find small balls that ONNX misses at far-field distances.
**Method:** `frame_diff_detector.py` — compute frame diff on r0 tiles, filter by circularity >0.5 and area 15-300px², link into trajectories (min 3 frames, path >30px).
**Result:** 31,000 "moving" trajectories in 200 frames — overwhelmingly player motion, not balls.
**Conclusion:** FAILED as standalone approach. Player motion dominates. Needs: (1) player mask subtraction, (2) ONNX gap guidance to focus search, (3) tighter circularity/size filters.
**Follow-up:** EXP-005 used gap-guided targeting and succeeded.
**Code:** `training/data_prep/frame_diff_detector.py`

## EXP-002: Sonnet Vision QA for label quality (2026-03-27)

**Hypothesis:** Sonnet can reliably verify whether a tile crop contains a soccer ball.
**Method:** `label_qa_prep.py` generates 3x2 composite grids of tile crops. Sonnet classifies each as BALL/NOT_BALL. Batched at ~100/hr to stay within budget.
**Result:** 4,042 positive tiles reviewed: 33.4% true positive, 29% false positive. 4,333 negative tiles: 0.6% false negative rate.
**Conclusion:** Sonnet is excellent at confirming negatives (99.4% accuracy) and good at catching false positives. Positions r1_c5, r1_c6, r2_c4 have highest FP rates (sun glare, poor detection).
**Data:** `F:/training_data/label_qa/report.json`

## EXP-001: Tracker parameter sweep (2026-03-25)

**Hypothesis:** Optimal Kalman filter parameters for ball tracking can be found via systematic sweep.
**Method:** `review_packets/tracking_lab/experiment_log.md` — sweep gate distance (50-500px), max_miss frames (10-120), process noise, on one game segment.
**Result:** Best: gate=300, max_miss=90 achieved 95.2% coverage (frames with tracked ball / total frames).
**Conclusion:** Large gate + high persistence works for panoramic view where ball can move fast between frames. Prediction quality matters more than tight gating.
**Data:** `review_packets/tracking_lab/experiment_log.md`

## EXP-000: Label filtering heuristics (2026-03-22)

**Hypothesis:** Simple geometry filters can remove obvious false detections from ONNX bootstrap labels.
**Method:** `label_filters.py` — aspect ratio 0.5-2.0, width 0.008-0.06 normalized, edge clipping.
**Result:** 568K → 488K files, 759K → 606K detections (20% removed).
**Follow-up:** Trajectory validator removed additional 24% (606K → 462K), keeping only detections in trajectories ≥3 frames.
**Code:** `training/data_prep/label_filters.py`, `training/data_prep/trajectory_validator.py`

## EXP-PHASE-17: human-confirmed truncation re-run (NTFY overload) (2026-07-02)

**Goal (Mark):** truncation is undetectable from signals (no reliable schedule; field-dip / block-
onset / asym all overlap real vs truncated — see EXP-PHASE-16 follow-up), so capture the one bit the
human knows via a context-answerable NTFY tap, then re-run detection with it. NTFY allows **3 action
buttons** (verified against docs.ntfy.sh: "up to three user actions"); the game-start ping already
uses all 3 (Yes/No/Not-a-Game), so **overload "Yes" on the 0:00 question = "game already in progress"
= truncated_start** (no 4th button needed).

**Detector change:** `fuse_phases(..., truncated_start=, truncated_end=)` + `detect_phases(...)`
passthrough. A truncated recording's game body IS the whole file, so truncation forces
`localize=False` (no game-block localization past a missing edge) and pins the missing boundary to the
file edge: `truncated_start` → KO=0 (+ `ko_trustworthy=True`, human-confirmed), `truncated_end` →
END=dur. HT/2H (and the un-truncated edge) stay `_fuse_core`-detected. Non-truncated path is
byte-identical (defaults False; fixtures assert `times` only — 10/10 green).

**Validated on the box (cached combined signals + human GT):**
- 06.06-Fairport (trunc-start): baseline KO **5:23** → **0:00** (exact); HT −2s, END −4s.
- 05.09 (trunc-start): baseline HT **43:38 / +9min garbage**, 2H 46:51 → truncated **HT 34:07 (−4s),
  2H 43:38 (+0s)**, KO 0:00 — the false localized block had wrecked HT/2H; localize=False fixes it.
- West Seneca (trunc-end): END **59:22 (−332s)** → **64:50 (−5s)** (pinned to file end).
- Residual per-game misses (05.09 END −853s, 06.06 2H −31s) are pre-existing detection issues, not
  the truncation logic.

**Wiring (landed):** `game_start_task` overloads the 00:00 "Yes" ("Already started" label +
truncation-framed question) -> `phase_game_start.resolve_truncated_start`: trim at 0 + re-run
`detect_phases(truncated_start=True)` + persist + best-effort TTT re-push. The re-run recomputes
signals (~min); a follow-up can cache the phase_detect-step signals for an instant re-fuse.
**Still to do:** a "still playing" END-task tap for `truncated_end`, and a truncated toggle in the T2
app for the confident-but-truncated games that never get the walk (05.09 / 06.06-Fairport).
