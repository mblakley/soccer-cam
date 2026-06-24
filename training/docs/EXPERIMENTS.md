# Experiment Log

Each experiment has: hypothesis, method, result, conclusion. Failures are as valuable as successes.

---

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
