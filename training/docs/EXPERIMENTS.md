# Experiment Log

Each experiment has: hypothesis, method, result, conclusion. Failures are as valuable as successes.

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
