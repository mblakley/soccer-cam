# Experiment Log

Each experiment has: hypothesis, method, result, conclusion. Failures are as valuable as successes.

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
