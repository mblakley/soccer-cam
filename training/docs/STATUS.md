# Current Status

*Last updated: 2026-07-03*

## 2026-07-03 — SELECTION is the bottleneck, not detection (start here on resume)

Full write-up (with the held-out numbers + external comparison, kept out of the repo per policy):
`F:\archive\OnceAutocam\ball_tracking_findings_2026-07-03.md`. Repo-side takeaways:

- **The detector is not the problem; selecting the game ball is.** Held-out candidate CEILING (ball in
  the detector's peaks) is far ~0.91 / near ~1.00 — already at/above the target bar. But the tracker's
  SELECTED pick is only far ~0.61 / near ~0.54. The ball is in the candidate set; the tracker latches a
  distractor. **Always decompose eval into ceiling vs selected** — a single R15m number hides this.
- **It's a precision/confidence problem, not recall.** score-argmax (top-scored peak = GT ball) is only
  **0.24 far / 0.30 near**: the game ball is rarely the highest-scored candidate. Recall (ceiling) stays
  high; confidence tails off as the ball gets far/faint, so distractors outrank it and Kalman keeps the
  wrong lock. Fix = distractor suppression / distance-calibrated confidence, not more recall.
- **The human far-labels are SELECTOR/tracker GT, not detector data.** `ball` xy = track anchor on hard
  frames; `obscured` = where the game ball is while invisible (the detector *cannot* see it — only a
  selector can carry the track through occlusion); `not_game_ball` = identity/distractor supervision.
  This session mistakenly trained them into the detector (as crops) → no held-out gain, as expected.
  Real target = **find THE game ball as ONE continuous track** (identity + continuity through occlusion),
  measured by track length/continuity — not per-frame hit rate. → **task #17 (world-model/selector).**
- **Raw-segment decode infra landed** (`data_prep/segment_decode.py`; see EXP-DIST-21 / DECISIONS
  2026-07-02). Wired into `build_far_label_queue`, `build_human_crops`, `eval_detector`. STILL TODO:
  `heatmap_dataset.build_heatmap_crops` + `mine_hard_negatives`; validate refactored `eval_detector`
  against the old-code candidate dump; switch the two queue/crop builders from dict → streaming.

## AutoCam distillation → homegrown ball detector (2026-06-30)

**Goal (Mark):** train OUR ball *detector* (HeatmapNet, not a viewport model) so that OUR existing
tracker (`world_model.reranker.track_ball`), fed our detector's detections, follows the ball to
viewport tolerance (~10–15 m) everywhere — **matching AutoCam in normal play and beating it on far
balls vs our human GT.** Pipeline at inference: `detector → existing tracker → viewport`.

**Key finding that reframed everything (EXP-DIST-16, GT-grounded).** On 1,880 human far-GT balls
(the frames AutoCam loses): the ball is in AutoCam's re-run detections **0.97** of the time, the
existing tracker over those detections lands within **R15 m 0.77** (median 2.1 m) of GT, while
AutoCam's own **viewport is 0.15** (median 41 m). AutoCam's raw viewport-gated pick only realizes
0.10. So **detection is fine, selection is the game** — and the existing tracker already solves it.
The viewport-GT offset is random-direction (not a coord bug): AutoCam genuinely looks elsewhere.
→ **Teacher = the existing tracker over AutoCam detections + human-GT override**, not the viewport.

**Teacher pipeline (`data_prep/distill_dataset.py:teacher_track`).** Per frame: build Candidate lists
(human `ball` overrides; `not_visible`→empty; else AutoCam `autocam_detections.jsonl` above conf) →
`track_ball` → snap each smoothed position to the **nearest in-field detection** (the real ball
pixel; drop if unbacked/off-field) → keep. Restricted to **active play** (game_state first/second
half — warm-up/halftime AutoCam tracks players) and **in-field** (marathon detections have off-field
FPs). Human far-GT is exempt and always kept. All three teacher bugs (warm-up, off-field, Dahua
noise) were caught by the **crop vision-gate before any training** — always eyeball crops first.

**Build (`cli/build_distill_dataset.py`).** teacher_track → `build_heatmap_crops` with **NVDEC
hardware decode** (`heatmap_dataset.build_heatmap_crops(hwaccel=True)`; the box is 4-core so CPU
decode was the wall — Reolink HEVC 7680×2160 decodes 102 vs 31 fps). Note: decode is still
full-video-per-game (GOP 20–50, no cheap seek), ~hours for many games. `--max-per-game` caps crops
for storage. `--camera reolink` for the clean first build (Dahua 2024 games have noisy AutoCam
detection + no GT to anchor — Reolink-primary; add Dahua down-weighted later).

**Held-out eval (`cli/eval_detector.py`, validated end-to-end).** our detector over the held-out
game's band (NVDEC, tiled to fit the 1060) → `extract_peaks` → inverse-warp to source → `track_ball`
→ meters vs human GT (R5/10/15), far/near by apparent size, vs the AutoCam-viewport bar. Held-out =
`heat__2026.05.31_vs_Spencerport_gold_2_away` (545 GT balls, frames 6714–21760).

**Build DONE, training RUNNING on DESKTOP-5L867J8 (2026-07-01).** Reolink build finished:
`G:\ballresearch\distill\crops_reolink` = **76,875 crops** (71,332 train / 5,543 Cleveland-val /
45,164 positive) from **15 Reolink games** (holdout Spencerport, val Cleveland). Training HeatmapNet
base24 40ep → `runs/hm_reolink/best.pt`, GPU-bound ~23 min/epoch. **Held-in Cleveland val recall
peaked 0.387 @ epoch 5** then flat (0.38x) — clears the ≫0.16 sanity gate; a plateau is forming.
Orchestrated by **two scheduled tasks** (survive a session close — a `Start-Process` launched inside
a WinRM session does NOT): `distill_chain_reolink` (build→train→auto-eval) + `distill_watch_reolink`
(early-stops training at a val-recall plateau — no new best for 6 epochs, ≥12 — which drops the chain
through to the eval). Logs are **UTF-16BE** (`Get-Content -Encoding BigEndianUnicode`); heartbeats
`chain_reolink.status` / `watch_reolink.status`. Box worktree `G:\ballresearch\distill\repo_hg`
(HEAD 7e10727 for build/train; `eval_detector.py` alone bumped to 0155dc4). venv `G:\v4bench\wt\.venv`.
Branch `feat/homegrown-ball-detector`. (One stale eval-waiter from the original overnight run had to
be killed — it fired an early eval on the epoch-5 ckpt and was contending for VRAM.)

**Success bar (Mark, restated 2026-07-01):** OUR `detector→tracker` must **match AutoCam on
near+medium balls and beat it on far**. `cli/eval_detector.py` now prints this head-to-head directly:
ALL / NEAR+MED / FAR bands, each with OUR detector→tracker vs **AutoCam viewport**, plus two
diagnostics — **AutoCam-detections→OUR-tracker** (isolates whether a far gap is our *detector* or our
*selection*) and our per-frame candidate ceiling.

**RESULT (2026-07-01) — goal NOT met; clear diagnosis (EXP-DIST-16 for the table).** Held-out
Spencerport: OUR detector→tracker R15m **0.24** (median 51 m) — does NOT follow the ball. The
AutoCam-viewport baseline is **broken** (R15m 0.026, median 57 m — not a credible product number →
viewport loader misaligned; the old "AutoCam 0.15 far bar" is discredited too). Viewport-independent
truth: our detector SEES the ball (ceiling **0.95**), our tracker WORKS (AutoCam dets→our tracker
**0.85**), but the join (our detector→our tracker) is **0.24** → the gap is **SELECTION**: our top-k=24
peaks bury the ball in confident distractors and the Viterbi tracker locks onto the wrong trajectory
(NEAR 0.067 is worse than FAR 0.288 — not following the ball at all).

**OVERNIGHT PDCA 2026-07-01→02 (EXP-DIST-17..20). Tooling: `cli/eval_detector --dump-cands` →
`cli/sweep_tracker` replays the tracker under many configs in SECONDS off a cached candidate dump — this
broke the 40-min-per-hypothesis loop and is how all of the below was found.**

**Wins (validated on TWO held-out games, Spencerport + Irondequoit):**
1. **Teleport gate was the far bug.** The tight meters-space gate hard-excluded the true far candidate
   (meters ill-conditioned near the far touchline). Loosening it lifts **far R15m 0.288 → ~0.65**, median
   **51 → 12 m**, and the tracker finally beats score-argmax. Cross-validated (Iron far 0.455 → 0.727).
2. **Corrected an overfit:** the single-game optimum (a0.3/mj40/v20) was Spencerport-tuned; robust default
   is **a1.0/mj25/v12** (α raised because the hard-neg detector's scores are now worth trusting).
3. **Hard-neg fine-tune** cleaned candidates + lifted near *detection* (argmax 0.244 → 0.378); recall/
   ceiling held. Detector generalizes (Iron ceiling 0.91–0.94).
4. **Fixed the BAR:** AutoCam's viewport/sidecar is camera *framing* (~53 m from ball), NOT detection —
   the "AutoCam 0.15 far" premise is dead. Real bar = AutoCam-dets→tracker (far **0.845**/near **0.978**);
   our ceiling (0.933/1.0) exceeds it, so the goal is achievable via selection/score.

**Scorecard vs goal** (far>0.845, near≥0.978): FAR **~0.55–0.73** cross-validated (was 0.288 — big,
tractable, candidate-quality-limited). NEAR **~0.19–0.31** (was 0.067) — the OPEN problem.

**Near is DETECTOR-quality-limited (diagnosed, EXP-DIST-19/20):** the ball is in candidates 94–99%
(ceiling), but the detector scores a distractor above the near ball ~60% of the time (argmax near only
0.38 Spc / 0.06 Iron), and the confidence-hybrid can't rescue it (raw sigmoid is saturated ~1.0). The two
tracker levers are exhausted (teleport fixed, cross-validated). **So the remaining gap on BOTH far and
near is DETECTOR candidate quality, not the tracker.**

**Validated architecture / next levers:** (a) **detector** — the biggest lever now: near-focused training
(size-adaptive heatmap targets so big near balls score high; near-distractor hard-negs) and the
**high-value labels Mark offered** (far balls, distractor-disambiguation, new venues — never normal play);
(b) **selector** — a mode-aware / per-frame-competitive path (the RESEARCH_REPORT world-model) for the
near far↔near excursions the single global-smooth Viterbi can't follow. Running overnight: 2nd hard-neg
round (`distill_hn2`, both-game sweep). Detector artifact now = `runs/hm_reolink_hn/best.pt`.

## Game-phase detection — multi-signal, half-length agnostic (2026-06-28)

`training/data_prep/phase_detect.py` — fuses **player-on-field curve** (yolo26n persons in the field
polygon; halftime = field empties) + **ball restart** (AutoCam `<video>.mp4.jsonl`: ball static at
center circle then moves = kickoff → precise KO/2H) + **whistle multi-blast** (HT/END). Picks the
most precise signal per boundary, cross-checks by relative timing; half-length range is only a
sanity gate (no fixed-40 assumption — handles weekday-league 40min, weekend-tournament/youth
30-38min, indoor dome). Per-game compute cached (`phase_cache/<gid>.json`, ~2min/game then instant).
Sanity gate rejects implausible fits — never writes garbage.

- **Validated 6/04 (frame GT): KO 2:37.02 (±0.02s, ball), HT 42:34, 2H 50:21, END 90:09** — all <10s.
- **Reolink coverage: 12/24 game_state.** 6 whistle-40 (EXP-PHASE-02) + 3 manual play_windows + **3
  new fused & vision-verified: heat 5/28 (40min), heat 6/06 Sullivan (36min), flash 3/21 (34min,
  indoor dome).**
- **Known gap — halftime warm-up on field:** when teams warm up on the pitch during halftime, the
  player dip is short and the ball 2H can latch a warm-up center-kick → rejected (5/31_14.40, 6/07
  Lakefront) or asymmetric (5/30 Fairport, 6/08 football-field). Fix = require KO/2H corroboration
  across ball+player-formation+whistle (relative timing), pending. no-dip games (5/30 WNYF, 6/06
  Fairport, 6/07 BU15) need halftime from the ball's idle gap. Aborted false-starts + 2025 raw-only
  games are not real gaps. Details in EXPERIMENTS.md EXP-PHASE-03.
- **Next:** corroboration refinement for warm-up/no-dip games; then pipeline integration (training
  game_state annotation; optional production auto game-start to assist the NTFY prompt).

## IMPLEMENTATION ROADMAP — JSONL measurement store (approved 2026-06-26, autonomous build)

Design locked in DECISIONS.md (3 entries: store-next-to-video / split-preprocessing / frame-indexing+corruption).
Mark: "start with implementation … just run this marathon until it completes." Marathon is NOT relaunched again —
it finishes writing its current correct-geometry `detections.json`; we **convert** to the new format post-hoc.

### PROGRESS (2026-06-26)
- **Step 1 (`game.json` builder) — DONE across all 103 games.** Tool: box-scratch `G:\ballresearch\measurement_store.py`
  (server paths + manifest.db internals → NOT in OSS repo). Wrote game.json next to each video on F:: `segments[]`
  with demux-canonical `frames`+`global_offset`+`corrupt[]`, `field_polygon` (**65 games**, rescued from manifest.db
  `field_boundary` → v4_fields/gamedata fallback), `game_state` phases (**27 games**, manifest.db human rows, contiguity-
  enforced). seg basis = registry `segments` on disk, else `resolve_source()` (the marathon's resolver, `:combined` for
  the 5 tournament umbrellas). **Trainable coverage: 73 → 60 polygon, 27 human phases.**
- **Registry curation metadata now in game.json** (backfilled all 103 + builder updated): `name`, `orientation`
  (**12 upside_down**, 91 right-side-up), **`needs_flip`** (**2 True**: `flash__2025.05.17_vs_NY_Rush_away`,
  `heat__2025.06.02_vs_Fairport_home`), `game_type`, `trainable`, `exclude`/`exclude_reason`, `video_source`.
  **FLIP CORRECTNESS FLAG:** both needs_flip=True games are trainable AND have `combined_video=None` -> the marathon
  resolves their UPSIDE-DOWN raw and would emit upside-down detections. Decide before the marathon reaches them (at
  ~8/72 on 2024 games; these are 2025): flip-before-detect vs accept-and-flip-downstream.
  - **RESOLVED 2026-06-26 (vision):** both `needs_flip` games are ALREADY right-side-up in their mp4 (flip **baked into
    pixels**, no tag) — Mark confirmed; flipping would invert them. `orientation` is raw-camera provenance only.
    **Replaced needs_flip with `video_rotation`** in game.json (resolved video's display tag: **8 games `-180`**
    tag-corrected, **95 `0`**). cv2 auto-applies it; PyAV ignores it. **FIXED:** `warped_dataset.apply_display_rotation`
    + `resolve_video_rotation` (explicit → game.json-beside-video → 0); both crop builders (`heatmap_dataset`,
    `warped_dataset`) apply `cv2.flip(img,-1)` per frame when `video_rotation==±180`. Self-sufficient (reads game.json,
    no assembler dependency). Unit-tested (`tests/test_video_rotation.py`), ruff+pytest green. So the 8 tag-corrected
    games no longer train upside-down.
- **Promoted `heat__2026.06.15_vs_Irondequoit_away` to trainable (2026-06-27)** — was `trainable=False` only because
  `has_labels=False` (set before we consolidated its 301 human far-labels). Now `trainable=True`+`has_labels=True` in
  registry (backup `.bak_promote_irondequoit`) + game.json. **Trainable = 74.** NOT in the running marathon's work-list
  (fixed at launch=72) → **must be processed in the post-marathon all-games detection pass** (#27) along with the
  non-trainable completeness games. Its labels are already in `ball_labels.jsonl` (heat_0615 sets, +1181 offset).
- **Marathon PREFETCH speedup (2026-06-27).** Added `--prefetch` to `gen_detections_all.py`: a producer thread does the
  `grab()`/`retrieve()` decode into a bounded queue while the consumer runs DML `detect()` — overlapping CPU decode with
  GPU inference (both release the GIL during their C work). **Verified byte-identical detections** vs serial (CPU 300 +
  DML 500 frames, 0 diffs) AND refactored-serial == old-serial (0 diffs, so resume is safe). **DML A/B (uncontended):
  7.25 → 8.54 infps = ~18% faster on Dahua** (Reolink should gain more, being decode-bound). Deployed + relaunched
  (resumes from `.done`=17; `--prefetch` forwarded to workers via run_parent; `run_marathon.cmd` updated). Backup
  `.bak_preprefetch`. **Revised ETA ~2.1 days** (from 2.5). Memory note: bounded queue (maxsize=4) ~200MB on Reolink.
- **dav_only → mp4 conversion — DONE (8 games, 30 segs, 0 fail).** `G:\ballresearch\dav_convert.py`: lossless remux
  (`-c:v copy -c:a copy`, AAC audio **byte-identical** to source — MD5-verified), in place on F: as `<dav-stem>.mp4`
  (matches the registry segment stems). Now resolvable + offset-tabled; the running marathon will pick them up when it
  reaches them (they're later in its queue).
- **Known residual gaps (for the final audit, not blockers):** 13 trainable games lack a field polygon; 46 lack human
  phases (some are in `play_windows.json` — builder doesn't pull that yet); `heat__2025.07.22_vs_Fairport_away` has 1
  corrupt segment (demux→None, flagged).
- **Step 2 (`ball_labels.jsonl` consolidator) — DONE (2,438 labels, 7 games, vision-verified).** `G:\ballresearch\build_ball_labels.py`.
  Per-game `ball_labels.jsonl` next to video on F: (`{seg,f,a,p,src,set,ts}`, off transient D:). Game-match: dir → date+opponent
  (handles `guzzetta`==heat, `D:\soccer-cam-storage` clips). **Combined-basis recovery:** spc_*/heat_0615 sets were labeled on
  **start-TRIMMED raws** (Spencerport raw 80640 vs the marathon's full `combined.mp4` 81800), so frame_idx needed a constant
  offset. Auto-computed per game by **frame-matching the raw against the combined** (downscaled diff≈0): **Spencerport +1162,
  Irondequoit +1181** (each its own start-trim, no drift). Vision-verified BOTH recovered: markers land squarely on the ball.
  Per game: Cleveland 274, Pittsford 336, Chili 372, Spencerport 633, Fairport 180, Lakefront 342, Irondequoit 301.
  **2 sets still unmatched** (Pittsford `__queue` 60 — date-only clip; re-indexed `irondequoit` test clip 177). The vision HARD
  GATE caught the misalignment before shipping bad labels — see [[feedback_verify_label_semantics_with_vision]].
  NEXT: fix Pittsford `__queue` match (`18.28` token); `play_windows.json` phase fallback; viewport/detections/track converters → full audit.
- **Steps 3–5 (viewport/detections/track converters) — DONE + validated (2026-06-27).** Tool: box-scratch
  `G:\ballresearch\build_sidecars.py` (idempotent, mtime-gated, `--require-done` so only marathon-complete detections
  convert; writes sidecars + `README.measurements.md` provenance next to each video on F:). **Format follows the locked
  DECISIONS.md table** (flat fields), NOT the older `{seg,f,v}`/`{seg,f,d}` sketch in the build-order list below:
  `autocam_viewport.jsonl {seg,f,x,y}` (camera center), `autocam_detections.jsonl {seg,f,x,y,conf}` **one row per
  candidate**, `ball_track.jsonl {seg,f,x,y}` (gamedata `track.jsonl` was already this shape). Global→(seg,f) via
  `game.json segments[].global_offset` (viewport keys 1-based → `g0=k-1`; detections keys 0-based global → `g0=k`).
  **Coverage: viewport 55, detections 28 (= marathon-`.done`; tail auto-finalizes as the marathon completes — re-run is
  idempotent), ball_track 27; READMEs 69.** Validated on `flash__2024.05.01_vs_RNYFC_away`: viewport 125,127 rows =
  `total_frames`, per-seg counts exact, **0 out-of-range, 0 value-mismatch vs raw source** (200 sampled); detections
  436,703 candidates / 31,280 frames, candidate counts+values match the raw frame; ball_track 232 = source, 0 invalid.
- **Step 7 FULL METADATA AUDIT — first pass DONE (2026-06-27).** Tools: box-scratch `G:\ballresearch\{audit_store,coverage}.py`.
  `game.json` present **103/103**; `field_polygon` **65/103** (60 trainable); `game_state` **27/103** (the gap = games whose
  phases live only in `play_windows.json`, not yet pulled). **Anomalies (root-caused, not conversion bugs):**
  - `heat__2025.07.22_vs_Fairport_away` (**trainable**) — `game.json` INCOMPLETE: segment 6/22 has `frames=None` (builder's
    ffprobe packet-count failed on that one seg; file present at normal size), so `total_frames` is under-counted ~5,900 and
    the next seg inherited its offset. **Fix: rebuild this game's `game.json`** (re-ffprobe seg 6). 7 `camera__*` non-trainable
    junk recordings are likewise incomplete (don't care).
  - `flash__2026.05.10_vs_Upper_90_FC_home_11.19` — genuine **32-second fragment** (640 frames, single seg `11:19:10–11:19:42`);
    marathon ran it (inferred 160) but the ball model fired once → `det_frames=1`. **Curation call: exclude or merge** into the
    main `...Upper_90_FC_home` game (it's in the DATA_INVENTORY held-out list — should not be).
  - Thin viewports — `heat__2024.05.31_vs_Fairport_home` (82) + `flash__2025.06.14_vs_Vestal_home` (841): the AutoCam
    `-once-processed.mp4.jsonl` sidecars were **truncated** (only 82/841 per-frame lines). Conversion is faithful; viewport
    needs an **AutoCam GUI re-run**. Detections are unaffected (marathon regenerates them; heat__2024.05.31 in progress now).
  - Marathon: **28/72 `.done`** (Python `os.listdir` authoritative; the live probe's `Get-ChildItem` had under-counted to 19),
    in-progress `heat__2024.05.31_vs_Fairport_home`, prefetch active.
  NEXT: rebuild incomplete `game.json`s (re-ffprobe); `play_windows.json` phase fallback; repoint `gamedata.py` +
  `DATA_INVENTORY.md` to the per-video sidecars; re-run `build_sidecars.py` as the marathon finishes (idempotent).
- **Auto field_polygon fill (2026-06-27) — 7 of 13 missing trainable games seeded + staged for review.** Tool:
  box-scratch `G:\ballresearch\fill_field_polygons.py` (CPU only — zero marathon GPU contention; self-contained
  inference, fp16 NCHW 768x384). **Finding: `field_outline_v2.onnx` is Reolink-domain** — confident on Reolink
  (max ~0.80) but weak on Dahua (even a known-good human-polygon Dahua game maxes ~0.70 with only 1 kpt ≥0.5). So the
  existing 61 Dahua polygons were **human-drawn** (`human_field_edit`), not auto. At **thr 0.3** the model yields usable
  outlines on Dahua too. Wrote `field_polygon` + `field_polygon_source=auto_field_outline_v2` +
  **`field_polygon_verified=false`** + `field_polygon_thr=0.3` + `mean_score`. **`--force` guarded to NEVER overwrite a
  human polygon.** Vision-checked all: **7 good** (flash 05.10/05.12, flash 03.21 indoor turf, heat 06.24/07.07/07.09,
  heat 07.13_G3), **3 bad** = sampled indoor-wall/non-field footage (heat 07.11_FF forfeit, 07.12 Niagara, 07.13_19.10) →
  polygons removed, `field_polygon_note` set, flagged needs-manual; **3 failed** = tournament/raw-basis games whose
  game.json middle segment is a synthetic `combined`/raw stem the resolver can't map to a file (Saratoga, Hershey, GUFC).
  **Verification page (live, Tailscale-served):** overlay gallery at
  `https://trainer.goat-rattlesnake.ts.net/static/field_review/index.html` (staged into the running annotation server's
  `training/static/`). NOTE: the in-browser Field/Phases **editors in `annotate.html` are legacy-store-bound**
  (`manifest.db` on D: + `v4_fields`, need tiles for the canvas) so they don't show the new sidecar-store games — for
  drag-edit verification they'd need repointing to `game.json` (follow-up). For now the gallery is view+approve.
- **(2026-06-27) Multi-segment rescue + `game.json`-backed field editor.** (1) Upgraded `fill_field_polygons.py` to
  sample across ALL segments (confidence-median auto-skips warmup/indoor segs) + resolve tournament/raw + nested-dir
  files -> **rescued Saratoga / Hershey / GUFC** (10/10 kpts). Net **10 good auto-seeds**; **3 short Niagara/forfeit games**
  (07.11_FF, 07.12, 07.13_19.10) have **no field footage in any segment** -> polygons removed, `field_polygon_note` set
  (exclude candidates). All vision-verified. (2) Built a **`game.json`-backed field editor** (`training/field_edit_v2.py`
  router + `training/static/field-edit.html`): in-browser drag-edit of the 10-pt polygon for ANY registry game, reading/
  writing the canonical sidecars (background frame decoded from the video; save -> `source=human_field_edit`,
  `verified=true`). Lists all 103 (65 verified / 10 auto-unverified / none / exclude). Live at
  **`https://trainer.goat-rattlesnake.ts.net/static/field-edit.html`** (`/api/fieldv2/*`). Deployed additively into the
  jared annotation checkout (new router file + one `include_router` line, backup `.bak_fieldv2`).
  **INCIDENT (resolved):** first deploy imported `cv2` at module load, but the annotation venv has **av+PIL, no opencv** ->
  crashed the server import -> ~5 min outage. Recovered (reverted append + restart); refixed to **PyAV (`frame.to_image`) +
  PIL** (applies `video_rotation` 180 since PyAV ignores the display tag cv2 honors), re-deployed with **import-tests gating
  the restart** so it can't recur. **Durability caveat:** editor lives as copied files + an untracked append in jared's
  checkout (matches existing pattern there); a git-reset of that checkout would drop it.
- **(2026-06-27) Editor made durable** — committed `field_edit_v2.py` + `field-edit.html` + the `annotation_server.py`
  include on jared's branch in the running checkout (`b1c3f9e`, now git-tracked; generated `field_review/` PNGs left
  untracked; jared's 8 WIP commits not pushed).
- **(2026-06-27) Corrupt-seg `game.json` rebuilt — `heat__2025.07.22_vs_Fairport_away`.** Segment 6/22
  (`...183315_183814..._1B93EE35.mp4`) is **genuinely unreadable** (full-size 459 MB but **no moov atom**; cv2 AND
  ffmpeg-7.0.1 both fail to open it, `-c copy` remux fails — unrecoverable without untrunc-class tooling, not worth it for
  ~4 min of a 21-good-segment game). Set `frames=0` + `note` (dead), recomputed offsets continuous,
  `total_frames=119820` over the 21 readable segments. Marathon-aligned (cv2 grab also yields 0). **Watch:** when the
  marathon reaches this game the dead segment could trip its decode if #25's recovery doesn't catch the moov-less-at-open
  case (monitor will flag a crash/stall). Tool: box-scratch `fix_corrupt_seg.py`.
- **(2026-06-27) play_windows phases — NOT bulk-filled (investigated, unsafe).** `play_windows.json` has 18 entries but
  only **7 carry real GT timestamps** (11 are empty `continue_curve` placeholders). Blocking issues for a bulk
  `game_state` fill: (1) timestamps stored at `fps=20` but real fps ~19.7 -> must remap on actual per-game fps; (2) name
  mapping hits **fragment game.jsons** (pw "Pittsford 5/07" -> `heat__2026.05.07_..._away` = 1,740 frames vs 88-min
  timestamps); (3) ~5 s end drift; (4) no new-store phases editor to verify. Recommendation: handle `game_state`
  **on-demand per trained game** (auto-derive from ball-track + verify), and build a new-store **phases editor**
  (analogous to `field-edit.html`) if we want to verify play_windows seeds. Left the store untouched to avoid bad phases.
- **(2026-06-27) Phase editor built + 6 play_windows games seeded (option A).** VISION-CONFIRMED play_windows
  timestamps are **upload-time, not recording-time**: mapping `flash 05.10` "kickoff" 4:05 lands on an **empty field**
  (timestamp 08:57:03 = exactly 4:05 into the recording, but no players) -> a per-game trim offset exists (same class as
  far-label alignment). So a direct fps map is wrong. Seeded the **6 GT games** (Upper_90, Spencerport, Fairport 5/28,
  Chili, Cleveland, Irondequoit 6/15) into `game_state` with `source=play_windows` as **starting scrub positions only**
  (skipped Pittsford 5/07 -- registry target is a 1,740-frame fragment vs 88-min GT). Built a `game.json`-backed
  **phase editor** (`/api/phasesv2/*` in `field_edit_v2.py` + `training/static/phase-edit.html`): scrub the 5 boundaries
  against decoded frames (`/frame?g=`), save -> `source=human`. Live at
  **`https://trainer.goat-rattlesnake.ts.net/static/phase-edit.html`**; lists 103 (27 manifest-verified, 6 pw seeds, rest
  none). Durable on jared branch (`c5db752`). Tools: box-scratch `map_pw_phases.py`.
- **(2026-06-28) WHISTLE + 40-min phase detector — the 10s-precision solution (EXP-PHASE-02).** Mark's idea: ref whistle
  marks HT/end; halves are 40 min (Guzzetta/heat 2026). Self-calibrating detector (`G:\ballresearch\whistle_phases2.py`):
  detect by whistle PITCH (the ref uses one pitch all game), pick the pitch+structure via the 40-min lock, derive
  kickoff=HT-40 / end=2H+40 snapped to whistles. **Validated <1s on all 4 boundaries of 6/04 Irondequoit vs Mark's
  frame-precise GT** (2:37/42:37/50:16/1:30:09). Scope: **2026 Reolink only** (whistle ~4350Hz needs Nyquist>4.4kHz;
  2024-25 Dahua audio is 8kHz -> whistle cut, which is why EXP-012 failed there). Batch (`whistle_batch2.py`) runs on the
  **trimmed 44.1kHz upload files** (combined.mp4@16kHz is unreliable), maps trimmed->（seg,f) via match_info
  `start_time_offset` (per-game), writes `game_state` source=whistle_40min + `_whistle_meta`. Batch over heat-2026 in
  progress. Next: ball-at-center kickoff refinement for no-whistle-lock games; flash-2026 (auto half-length).

Build order (precious/at-risk data first; verify each on a sample game before bulk):
1. **`game.json` builder** — per game, next to the video on F:. Segments[] with `frames`+`global_offset` (from demux
   packet count = canonical, verified == decoded on 06.08) + `corrupt[]`; **field_polygon** (rescued from manifest.db
   `field_boundary` → fallback v4_fields/gamedata) + `game_state` phase timestamps (rescued from manifest.db
   `game_phases` source=human). **This rescues the 11 unique polygons + 36 unique human phase-sets before any DB drop.**
2. **`ball_labels.jsonl` consolidator** — `D:\training_data\far_label\*\labels.json` (27 sets) → per-game next to video,
   `{seg,f,a,p,src,set,ts}`, provenance preserved. Gets the precious human labels off transient D:. Merge gamedata
   `labels.jsonl`.
3. **`autocam_viewport.jsonl`** — convert the 61 `F:\autocam_data\<gid>\viewport.json` → next to video `{seg,f,v}`.
4. **`autocam_detections.jsonl`** — convert marathon `detections.json` → next to video `{seg,f,d:[[x,y,conf]]}` (run on
   completed games; finalize when marathon hits 72/72).
5. **`ball_track.jsonl`** — migrate gamedata `track.jsonl` → next to video.
6. **Repoint `gamedata.py` (`gd`)** to read the per-game video dir; update `F:\DATA_INVENTORY.md`. Stop writing measurements
   to manifest.db (legacy DBs droppable AFTER the full audit verifies extraction).
7. **FULL METADATA AUDIT (Mark-requested):** iterate ALL ~103 games, assert each has a complete sidecar set —
   `game.json` (polygon + game_state + offset table + corrupt[]) and every available stream (autocam det/viewport,
   ball_labels, ball_track) — emit a per-game completeness report (what's present / missing / rescued-from-where).
   Only after this passes are the manifest.db files safe to retire.

Primitives (locked): point `[x,y]` · detection `[x,y,conf]` · viewport `[cx,cy,w,h]` · polygon `[[x,y]]`. Tooling
developed in-repo (generic data plumbing — `training/data_prep/`), synced to the box via git branch to run on F:/D:.

## OVERNIGHT AUTONOMOUS RUN — persistent tracker (2026-06-26, ~8h, Mark asleep, no feedback)

**This is the durable todo so nothing is lost across context compaction.** Update it as work advances.
Discipline every step: recall is the untouched GPU priority; all aux work CPU-only/low-priority; verify
before reporting; CHECK don't assume (projects-root CLAUDE.md rule #7); state lives here in committed docs.

### DONE since this tracker started (2026-06-26)
- **MARATHON PREPROCESSING BUG — Dahua 36% vertical squish — FOUND, FIXED, FULL RERUN LAUNCHED (2026-06-26 ~11:30).**
  Verifying the just-completed AutoCam-input ground truth (input `[1,3,480,1600]`, isotropic-to-1600-wide +
  ceil-pad-to-32, RGB/255/fp32 — proven by live Frida capture; F:`DETECTION_PIPELINE.md`) against the actual
  marathon code revealed a serious defect the capture-pass missed. **Channel order was fine** (`gen_detections_all.py:242`
  does `COLOR_BGR2RGB` + `/255` + fp32 — the earlier "make-or-break unknown" is resolved OK). **But the marathon
  hardcoded `AC_H=448` anisotropic for ALL games**, while AutoCam's height is source-AR-dependent
  (`H=ceil(srcH*1600/srcW/32)*32`). Probing every trainable source video: **two resolutions** — 7680x2160 Reolink
  (23 games, AutoCam feeds 1600x**480**; 448 ≈ 0.4% off, fine) and **4096x1800 Dahua (50 games, AutoCam feeds
  1600x704; 448 = a 36% vertical squish)**. All 10 already-generated games were Dahua → all squished. **A/B on 300
  real Dahua frames (same fp32 model, only geometry differs): 84% of confident top-1s agree (median 1.1 px) but
  ~16% lock onto a DIFFERENT object (p90 144 px, max 1501 px) + a systematic -8 px bias — confident-wrong labels
  the off-field/static/jump filters can't catch (~1-in-6 Dahua labels).** **FIX:** `run_worker` now computes
  per-game `ISO_H=round(srcH*1600/srcW)` + `AC_H=((ISO_H+31)//32)*32`, isotropic-resizes to `1600xISO_H` +
  **bottom-zero-pads** to `AC_H`, de-scales `sx==sy==srcW/1600`, and builds a fixed-shape fp32 model per height
  (`...fp32s_1600x704.onnx` Dahua / `...fp32s_1600x480.onnx` Reolink — both pre-built + DML-fused). Validated
  end-to-end (Dahua→704, Reolink→480, sane dets). **Stopped the marathon, wiped the 10 squished Dahua outputs
  (viewport.json preserved), relaunched against all 72 trainable from scratch (new parent, DML, first game on the
  704 model, 7.1 infps).** Box-scratch script backed up `.bak_448squish`; F: doc corrected (the prior
  "~0.4%, most games fine" was Reolink-only and wrong for the Dahua majority). 704 is ~25% slower/frame than the
  wrong 448 — correctness over speed; ETA ~3-4 days.
  **Audited the STUDENT side too (per the new isotropic-everywhere rule):** `build_heatmap_crops` →
  `_native_iso_warp` → `CropIsoWarp` scales height by the SAME factor as width (`final_h = band_h*target_width/src_w`,
  `points()` applies one `scale` to x AND y) → balls stay round. The `AnisoWarp`/`FieldWarp` option is the
  *intentional* perspective dewarp (depth normalization), applied identically at train + inference (no mismatch),
  not a uniform squish. **So our trained models never inherited the squish-class bug — it was unique to feeding
  AutoCam's fixed pretrained YOLO a distorted input (a train/inference geometry mismatch on an external model).**
  Only the teacher/label-gen path needed the fix.
- **DML inference SPED UP — root cause found, FIXED, marathon SWITCHED (2026-06-26 ~09:00).** The earlier
  "DML is ~155 ms/frame, GPU-inference lever is closed" verdict (CUDA bullet below) had the WRONG root cause.
  ORT profiling of OUR path showed only **~16 ms of real GPU kernel time per inference vs ~135 ms wall — ~89%
  was NON-kernel overhead**, not fp16 compute (the convs are ~9-16 ms total). **Root cause:** the balldet model
  has a **dynamic input** (`['batch',3,'height','width']`) and an **in-graph YOLO decode that rebuilds its anchor
  grid at runtime** (Range/Expand/ConstantOfShape/Reshape/Gather). ORT can't constant-fold that subgraph
  (symbolic shapes + the ops are fp16 with no CPU fp16 kernel → the "Could not find a CPU kernel … can't
  constant fold" warnings), so the **DML EP splits the net into many partitions with a CPU↔GPU sync on every
  boundary** — that's the 130 ms. Session-options levers (free-dim override alone, `ORT_ENABLE_ALL`, IoBinding)
  did NOT help (138→130 ms) — it had to be a **graph-level rewrite**. **FIX:** derive a **fixed-shape
  (1×3×448×1600) fp32 copy** of the model (`ensure_fixed_fp32_model()` in `gen_detections_all.py`, cached at
  `D:\detect_work\balldet_fp16_dec.onnx.fp32s_1600x448.onnx`) → ORT constant-folds the grid and **FUSES the whole
  net into ONE `DmlFusedNode`** (one dispatch, zero per-op sync; 445 nodes → fused). Pascal runs fp32 fine and
  its fp16 path isn't faster here, so fp32 costs nothing. **Measured (equal-contention A/B, identical frames):**
  pure detect **134→71 ms** synthetic (~1.9×); end-to-end **Dahua 215→149 ms, Reolink/processed 153→111 ms**.
  **Live, post-switch, uncontended:** the resumed decode-heavy Dahua game runs **8.88 infps / 112.6 ms vs 7.18
  infps / 139 ms before = 1.24× on this DECODE-bound game** (detect ≈1.9×; **decode is now the floor** on Dahua —
  see NVDEC lever). Detect-bound games (cheap-decode) gain more. **Correctness gate PASSED** (same model+provider,
  fp32 vs fp16 numerics only): top-1 detection matches the fp16 marathon **97.5% (Dahua) / 100% (Reolink)** within
  3 px + 0.03 conf, median dx **0.78 / 0.32 px**, conf delta <0.013 everywhere; only marginal sub-0.05-floor tail
  candidates churn (filtered downstream anyway). **SWITCHED:** stopped the fp16 marathon (parent PID 10120),
  deployed the updated script, relaunched detached (**new parent PID 12124**, worker IDLE prio, `DmlExecutionProvider`
  active — no CPU fallback), which **RESUMED the in-flight Dahua game from its checkpoint** (no recompute; that one
  game's `detections.json` is fp16 up to the resume frame + fp32 after, within tolerance). fp16 run log archived
  `marathon_fp16_20260626_085109.log`; prior code `gen_detections_all.py.bak_fp16_20260626_085109`. `run_marathon.cmd`
  unchanged (the fix is internal to `gen_detections_all.py`).
- **recall_train.py — FINISHED.** EXP-DIST-14 documented: **far-band loss weighting is NEGATIVE** — K0 control
  (no far-weight) beats K4 on both splits (hard 0.348 vs 0.239, normal 0.069 vs 0.039) + lower variance.
  Abandon loss-weighting; the lever is venue DIVERSITY (→ the detection marathon). GPU now free.
- **viewport indexer — DONE (agent a158bf6).** `index_viewports.py` extracted **61 registry games / 7.23M
  viewport frames** → `F:\autocam_data\<gid>\viewport.json` (schema `autocam_viewport_v1`; `xy`=smoothed camera
  center; **frame_base=1**, 0-based decode = f-1). Vision-verified Dahua + Reolink. 0 parse failures.
  UNMATCHED/flag-for-Mark: Baltimore Mania g1-3 + MCC tournament (no registry entry); tournament multi-game
  sidecars written to per-sidecar subdirs (Hershey/GUFC/Clarence/Saratoga/lancers). README per game.
- **detection orchestrator — BUILT + LAUNCHED (agent ad4bf2d).** `gen_detections_all.py` (idempotent/resumable,
  per-game child procs for crash isolation, det0615 schema). **MARATHON RUNNING** (see below).
- **CUDA-vs-DML inference bench — DONE; verdict STAY ON DML (2026-06-26 ~08:10).** Tested switching the
  marathon's balldet inference from DirectML to `onnxruntime-gpu` / `CUDAExecutionProvider` on the GTX 1060.
  **CUDA setup SUCCEEDED** (separate venv `G:\ballresearch\ort_cuda\.venv`: onnxruntime-gpu==1.24.4 — same
  ORT version as the marathon's DML build — + opencv-python/numpy/av; CUDA12/cuDNN9 DLLs reused from the
  proven torch cu124 stack `G:\v4bench\wt\.venv\Lib\site-packages\torch\lib` via `os.add_dll_directory`).
  `CUDAExecutionProvider` initializes + creates a real InferenceSession on `balldet_fp16_dec.onnx`. **BUT CUDA
  is ~20% SLOWER, not faster:** pure inference (sess.run) **CUDA 177 ms vs DML 147 ms**; full detect()
  **CUDA 188 ms (5.3 fps) vs DML 155 ms (6.5 fps)**, identical on Dahua 4K and Reolink 8K (preprocess ~2 ms,
  so it's pure GPU compute). **Root cause: Pascal GP106 (cc 6.1) has crippled FP16 throughput (~1/64 of FP32);
  ORT-CUDA runs the fp16 model's kernels in fp16 and eats that penalty, while DirectML handles the fp16 model
  more favorably.** The premise "CUDA 3-6x faster" does NOT hold on this hardware+model. Decode ceilings
  measured: **Dahua 23 infps (inference-bound), Reolink 8K 6.4 infps (decode-bound)** — so a slower inference
  would actively slow the inference-bound Dahua games. **Correctness gate PASSED** (CUDA path is correct, just
  slow): CUDA-vs-DML on the same frames match within fp16 tolerance — TOP-1 candidate within ~5 source-px
  (~2px in 1600-wide model space) and conf delta <=0.012; NN-matched bulk candidates mean |dxy| 1.7px (Dahua)
  / 2.3px (Reolink); only a ~3% low-conf tail near the 0.05 floor churns on NMS tie-breaks (inherent to any
  provider swap). **DECISION: marathon stays on DML — NOT switched. `run_marathon.cmd` unchanged, marathon venv
  untouched, F:\autocam_data not polluted (test artifacts isolated in `G:\ballresearch\ort_cuda\`).** The DML
  marathon ran undisturbed throughout (advanced 6 -> 7 .done games during the bench; infps dipped transiently
  to ~6.4 under brief shared-GPU test load, recovering to ~6.8). Bench harness kept at
  `G:\ballresearch\ort_cuda\cuda_bench.py` for re-test if an fp32 model or a non-Pascal GPU appears.

### In-flight background jobs
- **DETECTION MARATHON (#27) — RUNNING.** `run_marathon.cmd` → `gen_detections_all.py --provider dml --stride 4
  --trainable-only`, detached, worker IDLE prio. Work-list **65 trainable games remaining**. **As of 2026-06-26
  ~09:00 running the DML fp32-static FUSED model (new parent PID 12124, ~1.9× faster detect)** — **8.9 infps on
  the decode-bound Dahua game** (was 7.18), more on detect-bound games; **decode is now the floor** (see the
  DML-speedup DONE bullet above + NVDEC lever below). Writes `F:\autocam_data\<gid>\detections.json` + `README.detections.md` (avoids the
  indexer's README.md). Log: `G:\ballresearch\distill\marathon.log`. Resumable: `.done` + `.progress.json`.
  **After trainable-only completes (.done=72), re-run WITHOUT `--trainable-only`** for the remaining ~29 games.
  **REAL ETA (measured 2026-06-26 01:15): ~75 min/game for Dahua 4096x1800; the 16 Reolink 8K games ~3-4x
  slower → trainable run ≈ 4-6 DAYS** (decode-bound, not the 3 I first estimated). **SPEEDUP LEVERS FOR MARK
  (not done autonomously — would risk disrupting the stable run): (a) NVDEC hardware decode (cv2.cudacodec /
  PyAV cuvid) → potentially <1 day; (b) 2 parallel instances on disjoint `--games` halves → ~2x (watch 16 GB
  RAM). Left the single stable run going; reliable > fast-but-disrupted.**
  **QA GATE PASSED (2026-06-26 02:20, game 1 `flash__2024.05.01_vs_RNYFC_away`, 9.2 MB detections.json,
  18375/30960 frames with top-conf>0.5):** vision-checked 3 high-conf frames — detections are structurally
  CORRECT (valid source-px coords, on-field, clustered near players, right scale, det0615 schema). The top-1
  is occasionally a high-conf FP on grass — EXPECTED (AutoCam raw-balldet argmax is often a FP; that's the
  selection problem). Capturing the full per-frame [x,y,conf]×20 candidate set is the deliverable. Orientation
  verified: orchestrator uses `corrected_video` with `needs_flip=False` → no flip mismatch. Marathon data is
  GOOD; let it run. Downstream consumers: top-1 ≠ ball; use the candidate set + selection/tracking.
- **nulldecode_scan_v2.py — PAUSED at 50/360 (resumable).** Paused 2026-06-26 00:42 because it competes with
  the marathon for CPU decode AND its results are UNRELIABLE under sustained marathon load: it flagged 2 Upper90
  (2026.05.10) segments (100259@101.9s, 100759@92.1s) as CORRUPT, but the marathon's heavy 8K decode is the
  exact load that caused the ORIGINAL false positives — and Upper90's 092759 segment was already proven clean by
  3 idle decodes. The re-verify-×2 defense is defeated by *sustained* load. **These 2 flags are SUSPECT, not
  confirmed — re-run the scan on an IDLE box (post-marathon) to trust it.** 06.08 remains the only CONFIRMED
  corruption. (jsonl preserved; resume: relaunch nulldecode_scan_v2.py on an idle box — it skips done files.)

### REMAINING POST-RECALL GPU QUEUE (after / alongside the marathon)
- **N=16 curve rerun (#24)** — reuse `crops_game` + 06.08 decode-skip; 5th curve row. NOTE: marathon uses the
  GPU (DML, light) + CPU decode (heavy); N=16 (CUDA training) could co-run since GPU is mostly idle during the
  decode-bound marathon — but watch CPU/RAM contention. LOW value (flat curve) — defer unless idle.
- **6/15 AutoCam broadcast (#21)** — needs `bEnumerateHWBeforeSW=1` + GPU. Deferred.
- **SPEEDUP LEVER (investigate, don't block):** ~7 infps. NOT purely decode-bound as previously thought —
  measured decode ceilings are Dahua **23 infps** (inference-bound) and Reolink 8K **6.4 infps** (decode-bound);
  DML inference is ~155 ms/frame (~6.5 fps). **CUDA was TESTED 2026-06-26 and is ~20% SLOWER than DML** (Pascal
  fp16 penalty — see CUDA DONE bullet). The GPU-inference lever was **NOT actually closed**: the real bottleneck
  was the dynamic-shape in-graph YOLO-decode subgraph forcing per-op CPU↔GPU sync (only ~16 ms of ~135 ms was
  kernel time), **now FIXED 2026-06-26 ~09:00 via DML fixed-shape fp32 graph fusion (~1.9× faster detect; detect
  134→71 ms; see the DML-speedup DONE bullet)**. With detect ~halved, **decode is now the floor**, so the
  remaining levers are decode-side: **(a) NVDEC hardware decode** (cv2.cudacodec / PyAV cuvid) for the
  decode-bound 8K Reolink games; **(b) 2 parallel marathon instances on disjoint `--games` halves** (~2×, watch
  16 GB RAM). Marathon is resumable so switching decode mid-run loses nothing.

### As-they-land (event-driven)
- **Recall done** → analyze K4-vs-K0, write **EXP-DIST-14** to EXPERIMENTS.md (committed, not pushed), decide next.
- **Scan done** → report clean inventory; document.
- Both verdicts → fold into this STATUS.

### Gap-filler (no GPU; some gated)
- **#23 reconcile uncommitted research on `G:\v4bench\wt`** — GATED: recall imports world_model from there;
  do NOT modify until recall finishes. Then commit/push or confirm superseded.
- **Branch content scrub PREP** (AutoCam/distill refs → F: archive) — review only, execute at PR-prep.
- Label sets ready for Mark (no action needed, just don't break): heat_0615_lowconf_visible (124),
  spc_tracker_uncertain1 (118), heat_0527_c (93), Cleveland…seg12 (77).

### Open forks PARKED (need Mark; do NOT block on them)
- #26 faithful AutoCam-viewport-vs-tracker divergence set — needs a 2024 viewport game's balldet stream
  (rides on #27's output), post-recall.

### Done earlier this session (do not redo)
v0.5.3 merged+tagged+deployed (corrupt-segment reactive recovery, PR #92); zombie download cleared; Upper90
proven clean (scan false-positive); 06.08 = only real corruption; disk reclaimed to ~85GB G:.

---

## Active focus: targeted far-RECALL training experiment (EXP-DIST-13); data-scaling curve halted at 4 rows

Branch: `feat/homegrown-ball-detector` (worktree `../soccer-cam-autocam-distill`). All curve/distill
runtime lives in **server scratch `G:\ballresearch\distill\`** (per CLAUDE.md: no RE/one-off scripts in
the OSS repo); only the generic `training/data_prep/distill_dataset.py` is repo-resident. The v4-heatmap
focus below is still the architecture — the distill curve is its data-scaling study.

### 2026-06-25 — Curve HALTED at 4 rows (N=16 native crash); pivot to recall experiment; N=16 deferred
- **Data-scaling curve is DONE-ENOUGH at N=1/2/4/8.** Those 4 rows already answer its only question: more games on
  the current recipe does NOT improve recall (HARD R15 0.39/0.22/0.36/0.33; NORMAL R15 0.155/0.081/0.153/0.009 —
  flat/noisy, N=8 regressed). The lever is the RECIPE, not data volume.
- **N=16 was crash-looping, not slow.** The orchestrator relaunched iter_run ~25× over 2h (every ~5 min, GPU idle),
  each FAILED rc=1 with **no Python traceback**, dying deterministically while building crops for game
  `guzzetta__2026.06.08_vs_Hilton_Heat_Flaitz` (its crop dir never gets an `index.json`). No-traceback + native =
  a PyAV decode segfault (the known `0xC0000005` on this box) or a 16 GB-RAM OOM on that one game's video — an
  environmental crash, NOT a code bug. G: had 20 GB free (not disk). **Halted the orchestrator** (PID 3620/17348);
  `curve.jsonl` preserved at 4 rows.
- **Decision (Mark, 2026-06-25): pivot to the recall experiment now; defer N=16.** A 5th point on a flat line,
  gated behind debugging one game's native crash, is near-zero value. Plan: (1) launch EXP-DIST-13 recall on the
  freed GPU; (2) a CPU sub-agent debugs/re-encodes the 06.08 video in parallel; (3) run N=16 AFTER recall finishes,
  with the fixed video. The recall run does NOT use 06.08, so it sidesteps the crash entirely.

### 2026-06-24 — SYNTHESIS: the gap is FAR-ONLY and DETECTOR-bound; selection thread closed (EXP-DIST-08..12)
- **The whole "5× worse than AutoCam" was a far-third measurement artifact.** Re-scoring on clean human GT
  with the circular AutoCam baseline removed (EXP-DIST-08): honest AutoCam = **0.748** viewport (not 0.958).
  Per-band selection success climbs **0.27 far → 0.57 mid → 0.71 near** (EXP-DIST-10); near/mid argmax is
  already ≈ AutoCam *before any tracking*. The only hole is the **far third**.
- **Far is NOT a tracker problem.** Our shipped meters-Viterbi/Kalman `track_ball` adds nothing on far
  (0.153→0.153) and Kalman *hurts* — its 6 m/frame teleport gate hard-forbids the correct far candidate as an
  "impossible jump" (82.5% of recoverable misses; median forbidden displacement 67 m, because far m/px is
  0.09–0.19) (EXP-DIST-11). The reference's far-follow has **no selection intelligence** — per-frame argmax →
  ~3 s recency-weighted *pixel*-space moving average, no candidate set, no outlier rejection (RE in F: archive).
- **Decisive test (EXP-DIST-12):** the dumb pixel-smoother **more than doubles** our shipped tracker on far
  (0.153 → 0.342) and beats both Viterbi variants — but caps there. Oracle decomp: a *perfect* per-frame
  selector = **0.811** with no smoothing, collapses to **0.369** through the same 2.5 s smoother (smoothing is
  a ~16–19 m floor by construction); our smoother (0.342) already sits on it. Our detector's far argmax is only
  **13.5 % ball-centered** (median 25.6 m off).
- **Verdict / where work goes now:** (1) **Detector far-RECALL is the dominant remaining wall** — the venue-
  diversity curve + new far-label sets are the *only* path to AutoCam-class far; **no more selection
  engineering is warranted.** (2) **Banked, not yet shipped:** simplify the far-band selector to the dumb
  pixel-smoother (or at minimum drop the teleport gate behind a far-band flag) for a cheap ~2.2× far win —
  **gated on a near/mid no-regression check** that needs a continuous near/mid candidate stream (GPU/decode →
  post-curve). (3) **Flagged for a post-curve detector experiment:** does running our detector at the
  reference's operating input width raise far argmax? (Conflicting RE notes on that width; resolve against
  the F: archive RE doc before trusting either.)
- All five experiments CPU-only; the data-scaling **curve (N=8) was confirmed byte-undisturbed** throughout.
  Committed EXP-DIST-08..12 to EXPERIMENTS.md (`7b17f52`→`fef63c5`), branch not pushed.

### 2026-06-24 — Rebuilt 6/15 FAR set `heat_0615_gaps1` GAME-WIDE from complete dets — Mark's 51 labels preserved
- **`heat_0615_gaps1` was only the early game** (53 frames, built from a *partial* detection snapshot at
  ~frames 0–15980). Now that `detect_0615.py` is **DONE (5408/5408, `det0615\ball_dets_0615.json`)**, rebuilt
  it GAME-WIDE: far (cy≤700) + gap/lost-far + low-conf-far across the WHOLE active-play game.
- **191 frames total** (was 53): **142 newly-added** game-wide candidate frames (reason mix: 88 lowconf+far,
  28 lowconf+far+gap, 15 far, 7 far+gap, 4 lowconf±gap) + **49 force-included** Mark-labeled frames not already
  candidates (2 of his 51 were already selected). **108 AutoCam-seeded** (pre-seeded top-1 as the hint;
  gap-only frames center-seeded). Selection: Mark's canonical `play_windows.json` window gate
  `[1902,49459)∪[58117,105714)` + the human-tightened polygon (`field_polygon_0615.json`, `human_field_edit`)
  + detection-density active refine + temporal-spread (best-score per bin, span 320–98440).
- **MARK'S 51 LABELS PRESERVED (the critical constraint).** `labels.json` is owned by the SERVER (keyed by
  `frame_idx`, written only by the `/result` POST), NOT the builder — the builder clears ONLY `strips/` and
  rewrites `manifest.json`, never `labels.json`. **All 51 of Mark's labeled `frame_idx` are force-included in
  the new manifest** (read from `labels.json` at build time and forced into the selection so each labeled
  frame's strip is re-decoded and shows as done, not orphaned). New builder
  `G:\ballresearch\distill\build_0615_set_gamewide.py` (copy; reads a `ball_dets_0615_gapsfull_snap.json`
  snapshot; **seek-based PyAV decode** ~3 min vs ~15 min sequential; existing `build_0615_set*.py` +
  `heat_0615_normlowconf1` untouched). Defensive `labels.json` backup kept at
  `det0615\heat_0615_gaps1_labels.bak_*.json`.
- **Verified live (no restart):** `labels.json` byte-identical (51 labels, 9851 B, LastWrite unchanged).
  `GET /api/far-label/heat_0615_gaps1` → 200, **n_frames=191, labeled=51, n_mark_labeled=51**; list shows
  191/51 (and `heat_0615_normlowconf1` still 116/4, untouched). **All 51 Mark `frame_idx` present in the new
  manifest, 0 orphaned labels.** `/strip/{i}` → 200 ~5.5 MB for new far frames (16300, 18680, 98440) + Mark
  frames (320, 10000). **Vision-checked NEW far frame 18680** (lowconf+far, autocam=true): small far ball
  high on the field near the far touchline, AutoCam hint crosshair on it, inside the green polygon — exactly
  the AutoCam-weak far case the set targets.
- **Ready-to-click URL:** `https://trainer.goat-rattlesnake.ts.net/static/far-label.html?set=heat_0615_gaps1`
  (140 frames still unlabeled; F far-sweep / click ball / N not-visible / O out-of-play).
- **All protected jobs alive + undisturbed afterward** (CPU-only build): orchestrator (3620, running N=4),
  iter_run (280, N=4 worker), `detect_0610.py` (6900, advancing 300/5736), annotation 8642 (9688) — no
  restart; `curve.jsonl` unchanged (2 rows N=1,N=2). See EXP-DIST-05.

### 2026-06-24 — Built 6/15 hard-NORMAL low-conf active-learning set `heat_0615_normlowconf1` (EXP-DIST-05)
- **The detector's bottleneck is NORMAL play, not far balls** — the corrected curve's own rows prove it:
  NORMAL R15 = 0.155 (N=1) / 0.081 (N=2) vs AutoCam 0.958/0.748; HARD R15 = 0.386/0.22 vs AutoCam 0.11. So
  the highest-value human GT now is **hard-NORMAL** frames: ball in the mid/near field where AutoCam is
  genuinely uncertain (low-conf) — the normal-field discrimination cases (true ball vs players/shadows/
  corner flags/goalmouth clusters) the model keeps failing.
- **Built `heat_0615_normlowconf1` (116 frames)** from the now-COMPLETE game-wide CPU AutoCam dets
  (`det0615\ball_dets_0615.json`, snapshotted first). Selection: **NORMAL band cy>700** (excludes the far
  third the existing FAR sets cover at cy≤~480-700) + **low conf [0.08,0.40)** (above the 0.05 floor, below
  AutoCam's ~0.40 confident threshold) + ambiguous (close 2nd candidate); gated to Mark's active-play
  windows; **excluded the 53 `heat_0615_gaps1` frames** to avoid dup labeling; temporal-spread for game-wide
  diversity (span 1980-102120). Yields: 1073 NORMAL-band → 464 NORMAL&low-conf → 116 selected. AutoCam top-1
  pre-seeded as the hint (`autocam:true`, `hint_conf` recorded). New builder
  `G:\ballresearch\distill\build_0615_normlowconf.py` (copy; existing builders + `heat_0615_gaps1` untouched).
- **Manifest `band:"normal"`, `far_cut:0`** so the labeler classifies NO frame as "far" — these are reached
  via the **Next-unlabeled (U)** sweep / the pending auto-advance fix, NOT the far (F) sweep (every frame is
  reachable today via `gotoUnlabeled`, which has no far/autocam gate; the AutoCam hint still renders).
- **Ready-to-click URL:** `https://trainer.goat-rattlesnake.ts.net/static/far-label.html?set=heat_0615_normlowconf1`
  (use **U / "Next unlabeled"**; C accept AutoCam guess, click to correct, N not-visible, O out-of-play).
- **Verified live (no restart):** `GET /api/far-label/heat_0615_normlowconf1` → 200, 116 frames, band=normal;
  listed in `/api/far-label`; `/strip/{i}` → 200 ~5.4 MB. **Vision-checked 4 strips** (hint+polygon overlay):
  all NORMAL mid/near band, on-field, genuinely uncertain (conf 0.10-0.31; FP-on-player / at-feet / goalmouth
  — exactly the discrimination bottleneck). **All protected jobs stayed alive** (orchestrator 3620, iter_run
  280); `detect_0615.py` finished cleanly on its own (DONE, 5408/5408); curve.jsonl unchanged. See EXP-DIST-05.

### 2026-06-24 — 6/15 Irondequoit field polygon wired into the v4 field editor for human tightening
- **Goal:** let Mark drag-tighten the auto-detected 6/15 field polygon instead of redrawing it.
- **Created v4-field-store entry** `D:\training_data\v4_fields\heat__2026.06.15_vs_Irondequoit_away\`
  (3 files, matching the existing siblings' schema exactly):
  - `frame.jpg` — full-res 7680x2160 active-play frame (frame ~50000, t=2523.4s, mid first-half),
    extracted CPU-only via **PyAV** (no GPU; curve untouched).
  - `polygon.json` — the auto polygon pre-loaded as the draggable starting shape (10 pts), copied from
    scratch `G:\ballresearch\distill\det0615\field_polygon_0615.json`, **source kept
    `auto_field_detection_unverified`** so Mark's first Save flips it to `human_field_edit`.
  - `meta.json` — `{game_id, src_w:7680, src_h:2160, canonical_paths:[the scratch polygon JSON]}`.
- **Editor (the SAME annotate.html field editor, v4 branch on port 8650, served live by uvicorn from
  `G:\v4bench\wt`):** the server's `_v4_store_by_game()` resolves a game by `meta.json["game_id"]`
  (NOT dir name) and the GET/POST `/api/field-boundary/{game}` endpoints branch to this store when the id
  matches. **No restart needed** — the resolver re-scans the dir on every request; verified the new game
  appears live.
- **Ready-to-click URL:**
  `http://trainer.goat-rattlesnake.ts.net:8650/static/annotate.html#field/heat__2026.06.15_vs_Irondequoit_away`
  (the Tailscale **root `https://...` maps to port 8642**, the OLD checkout WITHOUT v4 support — must use
  **`:8650` over http**, the v4 editor's direct tailnet bind).
- **Verified end-to-end (live, read-only):** list shows 6/15; GET polygon → 200, 10 pts, src_w/h 7680/2160,
  endpoints match the source polygon; panoramic frame → 200 image/jpeg (6.70 MB full-res frame); browser
  render shows the frame + green polygon overlay with draggable corner handles (polygon is geometrically
  loose on far/right edges — the intended tightening target). All protected jobs still alive afterward.
- **PROPAGATION after Mark saves (automatic + manual):** the v4 POST handler writes the tightened polygon
  to BOTH `polygon.json` (source→`human_field_edit`) AND every `meta.canonical_paths` entry — so the
  scratch `det0615\field_polygon_0615.json` is updated **automatically on Save** (that's why it's the
  canonical path). The far-label builders (`build_0615_set*.py`) read that scratch file at build time, so a
  rebuild picks it up with no extra sync. **Still manual:** if 6/15 has already archived to
  `F:\archive\ball_distill\guzzetta__2026.06.15_vs_Irondequoit\` with a baked-in field polygon, re-copy the
  tightened polygon there too (the curve's crop builder reads the archived copy). Until then, nothing else
  to sync — the v4_fields store is itself on the resolver path.

### 2026-06-24 — 6/15 Irondequoit active-play windows persisted + wired into the active-play filter (EXP-DIST-04)
- **Persisted Mark's exact active-play windows** for `heat__2026.06.15_vs_Irondequoit_away` in the EXISTING
  canonical store **`G:\ballresearch\play_windows.json`** (the 2026 active-play store consumed by
  `gamedata_sources.play_windows()` / `iter_run.py` / `orchestrator.py`; 2024-25 games use `manifest.db`
  `game_phases`). Keyed **`guzzetta__2026.06.15_vs_Irondequoit`** (the `ball_distill` archive-dir convention
  every existing entry uses; Heat archives carry the legacy `guzzetta__` prefix). Frames computed at the
  raw video's **true 19.815 fps** (verified `average_rate=19.8149`, 108160 frames) — NOT the `FPS=20` proxy
  the other 2026 entries use: kickoff f1902, halftime [49459,58117), game-end f105714 → ACTIVE PLAY
  `[1902,49459) ∪ [58117,105714)` (88% kept). Backup `play_windows.json.bak_0615_*`.
- **Wired into the far-label builders** `build_0615_set.py` (full) + `build_0615_set_partial.py`: added an
  AUTHORITATIVE window gate (`active &= {f: inplay(f)}`, reading the canonical windows) right after the
  detection-density `active` set, so the density/motion proxy only refines within Mark's windows and warmup/
  halftime/post-game is hard-excluded. **NOT re-run** — the full rebuild waits for `detect_0615.py`; the
  partial set Mark is labeling is untouched. Backups `build_0615_set*.py.bak_winsgate_*`.
- **Training-data path needs NO code change:** `iter_run.py` already gates crops with `inplay(base+f)` from
  this same JSON, so once 6/15 archives to `F:\archive\ball_distill\guzzetta__2026.06.15_vs_Irondequoit\`
  the curve selection + crop builder arm automatically.
- **Verified (read-only):** dry-run mask f1000✗ f5000✓ f52000✗ f70000✓ f106000✗ all PASS (half-open
  boundaries); the builder's active-set on the LIVE partial detections dropped exactly the 96 pre-kickoff
  warmup frames (0–1900). **All three protected jobs still alive** afterward (orchestrator 3620, iter_run
  15576, detect_0615 3628; detect advanced 25980→27980; `curve_gpu.flag=2` unchanged).

### 2026-06-24 — Added 6/15 Irondequoit game to the archive (pending detections; additive, curve untouched)
- **New game archived + registered: `heat__2026.06.15_vs_Irondequoit_away`** (Reolink, 19 segments).
  Raw full-field + segments + combined copied to `F:\Heat_2012s\2026.06.15 - vs Irondequoit (away)\`
  (size-verified: raw 14,225,519,643 B, combined 14,380,725,627 B, 19 segs 14,389,654,529 B — all match
  source `D:\soccer-cam-storage\2026.06.15-18.27.13\`). Added to `F:\training_data\game_registry.json`
  (now 103 games; backups `game_registry.json.bak_*` kept) with `trainable=false`, `has_labels=false`,
  and a new `detections_status` note = **pending**.
- **NOT yet trainable — no detections.** AutoCam ball-detection **crashed on 6/15** (RDP-GPU contention);
  the `.mp4.jsonl` sidecar is empty. So there is **no ball_distill entry** and **no `F:\gamedata`
  labels** for this game — it is invisible to the running curve (`iter_run.py`/`orchestrator.py` select
  from `F:\archive\ball_distill\` + `gamedata.py has_labels`). **Verified the orchestrator + iter_run
  were still running and curve.jsonl unchanged after this work** — purely additive.
- **Why it matters (see GAMES.md):** 6/15 is the **FIRST game shot after the 2026-06-15 Reolink contrast
  calibration** (DECISIONS 2026-06-15) — high-value for far-ball detection IF the calibration helped.
  Prioritise it for detections + a pre/post-calibration far-ball-contrast comparison once labeled.
- **ACTION to make it trainable:** post-curve AutoCam distill run (`balldet_fp16_dec.onnx` @ mw 1600 →
  `F:\archive\ball_distill\<game>\`) **or** human far-labels — do NOT contend with the curve's CUDA job.

### 2026-06-24 — Built 6/15 far-label CONFIRM set `heat_0615_gaps1` from PARTIAL CPU detections (curve untouched)
- **Built a 53-frame high-value far-label set from the in-progress CPU AutoCam detection job**
  (`detect_0615.py`, stride-20, still running — covered ~frames 0–15980 / 800 sampled at build time).
  Set: `D:\training_data\far_label\heat_0615_gaps1\` (53 strips + `manifest.json`), served at
  `https://trainer.goat-rattlesnake.ts.net/static/far-label.html?set=heat_0615_gaps1`
  (`GET /api/far-label/heat_0615_gaps1` → 200, 53 frames; `/strip/{i}` ~5.6 MB full-frame JPEGs).
  Reason mix: 40 far, plus lowconf/gap; **43 AutoCam-seeded (Mark confirms hint), 10 gap (Mark labels)**.
  Confirmations persist by `frame_idx` in the server's `labels`, so the later game-wide rebuild loses nothing.
- **Field polygon was NOT empty** — `det0615\field_polygon_0615.json` already held a valid 10-pt human
  field edit (`source: human_field_edit`); vision-checked against a 6/15 frame, traces the field tightly.
  No ONNX/approx polygon needed.
- **Builder:** `build_0615_set_partial.py` (copy of `build_0615_set.py`, TARGET 160→60, reads a snapshot
  `ball_dets_0615_snap.json` not the live file, and **seek-based decode** instead of sequential — 56 s vs
  ~15 min for the full clip). All CPU (`G:\v4bench\wt\.venv`); **GPU curve never touched.**
- **Verified after the work:** `orchestrator.py` (3620), `iter_run.py` (15576), and `detect_0615.py` (3628)
  all still alive; `detect_progress.json` advanced 700→900 across the session; `curve_gpu.flag=2` unchanged.

### 2026-06-24 — Curve was silently broken; FIXED and re-launched. GPU running. (the headline)
- **The data-scaling / venue-diversity curve has NOT honestly run yet.** The prior `curve.jsonl`
  (N=1,2,4,5,8,16, dated 6/19–6/23) is **INVALID**: it was produced by a stale `iter_run.py` that
  silently trained on **only ~4 games at every N** (12/16 games hit a now-deleted `"NO polygon … skip"`
  path; N=16's own log says `train roots (4 games)`). The curve was flat *by construction*. Quarantined
  to `curve.jsonl.buggy4games_20260624_081941`. See EXPERIMENTS.md EXP-DIST-02 + DECISIONS 2026-06-24.
- **Polygon fix VERIFIED (this session):** the current `iter_run.py`'s `resolve_polygon()` (canonical
  `gamedata.polygon(gd.resolve(gid))` → v4_fields date+opp → manifest.db) resolves **16/16** archived
  games. A DRYRUN of the current code on all 16 → **140 segment-games / 52,367 labels** (vs the buggy
  44 / 20,303). The polygon fix the previous session made is real; it just landed *after* the curve ran.
- **CORRECTED CURVE RE-LAUNCHED + CONFIRMED TRAINING** (2026-06-24 08:19): detached orchestrator
  (`G:\ballresearch\distill\orchestrator.py`, console log `orchestrator.console.log`) advancing N=1,2,4,
  8,16. N=1 verified: GPU 27–72% util, 1.7 GB VRAM, loss 0.012 @ step 2k. It reuses 4 cached crop dirs
  and decodes the other 12 games (~35 min/game) → a multi-hour autonomous job. **GPU was idle (4%) and
  AutoCam was NOT running** when launched — no contention; 10 GB/16 GB RAM free.
- **Trustworthy result so far (HARD/far split, human GT):** detector R15 ≈ **0.39 vs AutoCam 0.11** —
  the distill **beats AutoCam on hard/far balls** (this held across the buggy curve too; HARD GT is clean,
  92% in eval mask). The NORMAL collapse number from the old curve is **not interpretable** (4-game train
  + confounded GT).
- **NORMAL eval is confounded (must fix before believing any normal number):** the curve's NORMAL GT =
  AutoCam dets conf≥0.40 + viewport-x within 500 px, with **no Y / no on-field test** → **45% (128/283)
  off-field or outside the eroded eval band-mask**, capping the ceiling ~0.55 regardless of the model.
  A CPU-only `reeval_clean.py` (server scratch) re-scores with an on-field + in-band NORMAL GT
  to separate artifact from real weakness.
- **NORMAL-eval honesty FIX (2026-06-24, EXP-DIST-03):** confirmed the existing Spencerport human labels
  (`spc_clip1..5`, dense + continuous) are **FAR-ball GT**, not normal-play GT — vision-verified (every
  sampled label is a small far ball near the far touchline; y 104–604 of 2160). They already (correctly)
  feed the HARD split; there was NO human normal-play GT, so the NORMAL split had to fall back to the
  confounded AutoCam-derived proxy. Fix = **one new human pass**: built far-label set **`spc_normal1`
  (141 frames, Spencerport 9460–10020 — a near/mid-field active-play stretch in the eval window, in the
  clean gap between spc_clip3 and spc_clip4)** with the canonical `farlabel_clip.py`; served live at
  `https://trainer.goat-rattlesnake.ts.net/static/far-label.html?set=spc_normal1` (GET /api/far-label/
  spc_normal1 → 200, 141 frames). Wired `iter_run.py` + `reeval_clean.py` so the NORMAL split prefers
  human `spc_normal*` labels (HARD excludes `spc_normal*`), falling back to the AutoCam proxy only until
  labeled — so the running curve is NOT broken, and the moment Mark labels `spc_normal1` the next N (and
  any `reeval_clean.py` re-run) reports an honest NORMAL R15 with zero further code changes.
  **ACTION FOR MARK: label the 141 frames at the URL above** (click the ball / `N` not-visible / `O`
  out-of-play; ~141 clicks) to make the venue-diversity NORMAL number interpretable for the first time.

### What to check next (for the next session / Mark)
1. **Curve progress:** `Get-Content G:\ballresearch\distill\curve.jsonl` (rows append as each N finishes;
   N=2/4/8/16 build crops first so they take longer). Confirm each `iter_N{N}.log` shows
   `train roots (N games)` = the full count (NOT 4) — that's the bug's signature.
2. **Orchestrator alive:** `Get-CimInstance Win32_Process -Filter "Name='python.exe'"` | grep
   `orchestrator.py`. If dead, relaunch detached (see orchestrator.py header).
3. **Then answer the venue-diversity question honestly** (does NORMAL R15 rise with N on the *corrected*
   curve?) using `clean_eval.py`'s clean_normal split, and update EXP-DIST-02.
4. **GPU coordination:** the box is shared with AutoCam (DirectML/iGPU) + the pipeline. Curve uses CUDA;
   verify headroom before adding fleet jobs (16 GB RAM box has thrashed before).

---

## Prior focus: v4 ball detector (perspective-normalized, full-frame, no tiles)

**v4 is ADDITIVE — it does not replace v3.** The tile-based **v3** detector lineage
(`train_v3.py`, `training/train.py` + its `V3_*` config, the shared `manifest.py` knobs, and
their tests) is **fully maintained and unchanged**. v4 is the new perspective-normalized,
warped-full-frame strategy, added as new files only (`train_v4.py`, `data_prep/warped_pack.py`,
`experiments/io_benchmark.py`, `tests/test_warped_pack.py`). See DECISIONS.md (2026-06-15).

Branch: `feat/perspective-normalized-detector`. The full design + experiment findings are in
**`training/docs/PERSPECTIVE_NORMALIZED_DETECTOR.md`** (read it first — source of truth for the v4
architecture, warp levers, labeling plan, I/O design). This STATUS is the launchpad.

### CURRENT (2026-06-15 — HEATMAP pivot; supersedes the YOLO/I-O-gate plan below)
The bbox/YOLO approach FAILED the first honest eval — **12% far-recall vs AutoCam's 74%**
(center-distance vs Mark's human ground truth): a 3-8px ball is at/below the detector stride and
IoU-mAP is meaningless. **v4 is now a ball-center HEATMAP + multi-frame detector** (see DECISIONS
2026-06-15; external survey + the bbox & pretrained-zero-shot baselines archived on
`F:\archive\v4_detector\`). Runtime + training pipeline =
**dewarp (native-res field-band crop) → polygon-mask (human-verified polygon, far margin) →
3 consecutive grayscale frames → compact U-Net → center heatmap → peak.**
- Built + **smoke-verified end-to-end**: `training/models/heatmap_net.py`,
  `training/data_prep/heatmap_dataset.py`, `training/train_v4_heatmap.py`, `tests/test_heatmap.py`.
  Eval = center-distance far-recall vs AutoCam 74% (Irondequoit held out).
- **Blocked on human far-ball labels.** Far-label tool (annotation server `:8650`, Tailscale
  `trainer.goat-rattlesnake.ts.net`) was rebuilt **gap-centric** (conf≥0.5 trajectory → velocity-
  extrapolated "ball went far and got lost" frames): sets `heat_0527_segA/b/c/d` (~362 gap frames)
  await labels; `irondequoit` (162) = eval GT. Tool: pre-seeds AutoCam, `F` jumps gap-to-gap, arrow
  marker, full-height strips.
- Field polygons human-edited via the unified, **resolution-aware** `annotate.html` field editor
  (`/api/field-boundary` now serves the 7680×2160 v4 clips from `D:/training_data/v4_fields`).
- Edge budget: 90 min @ 20 fps in <24 h = 1.25 fps; the heatmap net runs far faster on CPU
  (ONNX/CoreML/TFLite). Optimize for accuracy, not speed.
### OVERNIGHT 2026-06-16 — trained + evaluated; full record in `V4_HEATMAP_EXPERIMENTS.md`
Mark labeled far balls on `heat_0527_segA` (62) + `_d` (8) + rejected 34 AutoCam FPs. Ran the heatmap
training/eval program on the 1060 (venv `G:\pipeline_work\fk\.venv`, `torch\lib` on PATH). Key outcomes:
- **Root-cause bug fixed (committed):** the field band was cropped at the ground far line with no upward
  margin → ~33% of very-far GT balls were cropped out (uncountable). Fixed: band built from the
  far-margin-expanded polygon (`heatmap_dataset.py`, default `far_margin=400`). Honest denominators now
  match AutoCam (all=162, veryfar=131).
- **Crop-eval (localization given a window): champion J (base24, aug2=blur+illum, wd5e-4) veryfar 0.78,
  recovers 64% of balls AutoCam missed** — beats AutoCam's 0.74 *on that task*. Big levers: band fix,
  human labels, photometric+blur augmentation.
- **⚠ Full-frame SEARCH eval (the real task, = how AutoCam's 0.74 is measured): J only 0.29 veryfar,
  false-fires on 76% of frames. We do NOT yet beat AutoCam on the real task.** The crop-eval overstated
  ~2.7× (tight window hides distractors). Diagnosis (false-fire overlays): the model fires on the
  player/line ADJACENT to the tiny far ball — fine ball-vs-distractor discrimination is the gap.
- Tried + FAILED: heavy random negative mining (8:1 destabilized focal training, worse). Tracking-mode
  eval (track_oracle 0.47) confirms it's discrimination, not search scope. Motion-channel test running.
- **Next levers (untried, ranked):** hard-negative mining (train on the actual false-fire crops),
  more training games (only 05-27 labeled), explicit motion, then the `target_width` SPEED sweep
  (native band = 0.08 fps CPU, ~16× over the 1.25 fps budget — `target_width` knob added, deferred until
  full-frame precision is real). Scratch engine/evals live on the server `G:\v4bench\` (not committed).
- The YOLO / I/O-benchmark / warped-shard plan below is **superseded** (kept for history).

### v4 session progress (2026-06-15)
- **I/O benchmark gate built** (the prerequisite — no long run before it passes):
  `data_prep/warped_pack.py` (pre-decoded warped-frame shards: writer/reader + torch Dataset +
  `ShardRotator`, two storage modes raw-memmap vs compressed) + `experiments/io_benchmark.py`
  (nvidia-smi sampler; data-only/compute-only/end-to-end throughput; bottleneck + ms/iter +
  time/epoch + 4070 extrapolation; sweeps `target_width`×workers×prefetch×storage). 11 unit tests.
- **`train_v4.py` scaffold**: warped entry, persistent workers (the `workers=0` fix), v4 config.
  Not run until the warped dataset writer (`data_prep/warped_dataset.py`) lands.
- **`target_width` is a swept speed/accuracy knob** (DECISIONS 2026-06-15): the 1280 warp default
  crushes far balls below AutoCam's resolution — sweep {3264,5120,7680}, pick the lowest that beats
  AutoCam on far balls; match train+infer resolution.
- **Benchmark sources confirmed** on F: — Reolink `heat__2026.05.27_vs_Chili_Vortex_away` (20 segs)
  + Dahua `flash__2024.05.01_vs_RNYFC_away`. Registry: **23 trainable Reolink games, all
  `labels=False`** (the labeling gate), + 42 dahua_segments + 8 dav_only.
- **Hardware plan**: diagnose bottlenecks/timing on the server GTX 1060, then fan training-config
  experiments across all 3 GPUs (server + jared-laptop RTX 4070 + FORTNITE-OP RTX 3060 Ti) via the
  pull-based work queue. Remote workers see only D: via SMB → serve shards from D:, stage to local SSD.
- **Next:** run the gate on the server, report GPU util.

### Done (field-outline filter — the prerequisite for v3)
The in-house **field_outline v2** keypoint model (ResNet18, 10 kpts, resolution-agnostic,
distilled from the reference keypoint model) is trained, validated, exported, published,
and registered:
- Trained on the full distilled corpus: **53 games (dahua 1092 + reolink 592 + other 41
  frames), 1725 trainable frames**, orientation read from the game registry (not detected).
  Test split 17.1px / 0.814 IoU; Reolink ≈ Dahua → no dilution from joint training.
- Exported + parity-checked vs teacher (`training/cli/export_field_outline.py --check`).
- Published as a FREE TTT model: GitHub release `field-outline-v2.0.0` on
  `mblakley/soccer-cam`, encrypted asset `field_outline-2.0.0.enc`
  (sha256 `058b287a8cf1786e87d7a3be3902ff0981f67049909ebf786f1d1d7fb10b167f`).
- Registered in TTT as seed data (`core.model_versions`, channel=stable, tier=free,
  master_key_id=mk_2026_06) — TTT PR #48 → `development` → applies to preview Supabase.
- **This is the field filter v3 must always use — never the full-frame fallback.** Source:
  `video_grouper/inference/field_detector.py`.

### Next: v3 ball detector — ordered plan (see PERSPECTIVE_NORMALIZED_DETECTOR.md §Rollout)
1. **I/O benchmark gate FIRST** — prove the GPU stays fed (>80% util) at an SSD-bounded
   working-set size before any long run. Our prior trainings were starved (GPU 0%): root
   cause was decode of oversized JPEGs + non-persistent DataLoader workers, NOT F: vs G:.
   Design: sequential shards (not random small files) + pre-decoded memmap packs as a
   bounded rolling working set on G: + double-buffer/prefetch next shard while training +
   persistent_workers/pin_memory/prefetch. The full corpus is 15 TB on F:; G: SSD is
   ~271 GB — it does NOT fit, so streaming is mandatory. No blind multi-hour runs.
2. **Reolink labeling loop (the gate — Reolink has ZERO ball labels):**
   a. Run the **reference ball detector** on each Reolink game → raw per-frame detections =
      baseline labels (it nails easy/near balls). Entry point `training/cli/run_ball_detector.py`
      (`--video --model --output [--labels-dir --segment-name]`). RE-adjacent: it runs in the
      F:/storage workspace, never the repo; only ball coordinates feed training. Decrypted
      reference ball ONNX: `\\DESKTOP-5L867J8\video\test\onnx_models\decrypted\` (= F:\test\...);
      `balldet_fp16_dec.onnx` also staged at `D:\detect_work`.
   b. Track + **far-ball mine** the velocity-gap heuristic
      (`training/data_prep/far_ball_miner.py` → `mine_far_ball_gaps` → `candidates_to_queue`
      → `write_queue_json`, queue compatible with `flywheel/priority_queue.py`). Validated on
      05-27: 173 far-moving-then-lost gaps ≈ 17.7 min/game of far-ball footage.
   c. **Web helper** presents the prioritized far-gap queue on the **warped** frames (far
      balls are bigger + uniform there) → human labels only the far balls the reference missed.
3. **Dahua labels:** reuse existing human-verified labels, map into warped coords via
   `field_warp.warp_points` — no re-labeling.
4. **ONE joint, camera-balanced training run** over all games (Dahua + Reolink) — NOT
   pretrain→fine-tune. The warp normalizes geometry; `compute_camera_weights` balances the
   2:1 game-count skew so Reolink (production) dominates the far-field gradient. v3 dataset
   knobs already landed in `training/data_prep/manifest.py` (`DEFAULT_EXCLUDE_ROWS=set()` so
   row 0/far field is INCLUDED, `FAR_POSITIVE_MULTIPLIER=4.0`, `compute_camera_weights`,
   `classify_camera`). Train entry point: `training/train_v3.py` (manifest.db + packs via
   ManifestTrainer; `--data dataset.yaml --model yolo26l.pt`). NOTE: `organize_dataset.py` /
   `smart_sampler.py` carry their own `DEFAULT_EXCLUDE_ROWS={0}` copies and `training/tasks/train.py`
   is a separate path — mirror the v3 knobs there if the production run uses them.
5. Evaluate recall **per game with a per-camera breakdown** (target: beat v2's 0.29 by a lot,
   and beat the reference tracker on far balls). Then swap the production `ball_detect` step
   (`video_grouper/pipeline/steps/ball_detect.py`) from tiled inference to the warped
   full-frame model.

### Landed v3 modules (this branch, unit-tested — 44 tests green)
- `training/data_prep/far_ball_miner.py` — velocity-gap far-ball miner + labeling queue writer.
- `training/data_prep/field_warp.py` — `build_field_warp` / `warp_frame` / `warp_points` /
  `unwarp_points` (anisotropic vertical warp + inverse LUT; round-trips sub-2px; 7680×2160 →
  ~0.08 MP single warped input vs 8.6 MP for the 21-tile path).
- v3 dataset/config in `training/data_prep/manifest.py` (knobs above) + `training/train_v3.py`.

## Server + access

GPU server **DESKTOP-5L867J8** (GTX 1060 6GB; CUDA visible from WinRM). Footage + CUDA are
local there — run training there, not on this dev box. Credential: CliXml at
`%LOCALAPPDATA%\credentials\desktop5l-training.xml` (user `DESKTOP-5L867J8\training`).
T:\ = `\\DESKTOP-5L867J8\video\test\`. Bash tool strips Windows backslashes — use `/c/...`.

### Processes (server + remote workers)

| Process | Machine | Port | What it does |
|---------|---------|------|-------------|
| PipelineAPI | Server | 8643 | FastAPI, sole SQLite accessor for registry + work queue |
| PipelineOrchestrator | Server | — | Populates work queues via API every 60s |
| PipelineWorker | Server | — | Pulls stage/tile/QA/review tasks |
| AnnotationServer | Server | 8642 | Human review UI (Tailscale: trainer.goat-rattlesnake.ts.net) |
| PipelineWorker | jared-laptop | — | Tile/label/train (RTX 4070, CUDA) |
| PipelineWorker | FORTNITE-OP | — | Label/tile (RTX 3060 Ti), yields for games |

**Restart server services:** `powershell -ExecutionPolicy Bypass -File training\pipeline\install_service.ps1`
**Deploy remote worker:** `powershell -ExecutionPolicy Bypass -File training\worker\deploy_worker.ps1 -Machine laptop|fortnite`

## Storage architecture

```
F: (USB, 15TB)   — PERMANENT archive. Original videos (F:\Heat_2012s, F:\Flash_2013s),
                   pack files (F:/training_data/tile_packs/{game_id}/*.pack). Server only.
                   F:\test\onnx_models\decrypted\ = reference detector ONNX (RE-adjacent).
D: (HDD, ~1.8TB) — SERVING (SMB-shared \\192.168.86.152\training). manifest.db per game,
                   staged packs (restored from F: on demand), review packets, deploy files.
G: (SSD, ~271GB) — PROCESSING (local). registry.db, work_queue.db, per-game work dirs.
                   The v3 rolling working set lives here — it CANNOT hold the full corpus.
```

**Pack lifecycle:** create on G: → push to D: (manifest pack_file = D: path) → archive to F:
→ clean D: → `server_packs()` auto-restores F:→D: on demand. Remote workers see D: via SMB only.

## Registry (rebuilt 2026-06-14)

`game_registry.json` rebuilt via `python -m training.data_prep.game_registry` (scans F: team
archives; not hand-edited). **102 entries / 73 trainable / 23 reolink_segments** (all trainable;
≈18 substantial 13–25-segment games + ~4 one-segment fragments). Reolink games already archived
to `F:\Heat_2012s\2026.05.*` (→ `heat__`) and `F:\Flash_2013s\2026.05.*` (→ `flash__`) — no
ingest needed. Orientation comes from the registry (`UPSIDE_DOWN_GAMES` in game_registry.py),
NOT detection (auto-detect proven unreliable). 12 trainable games are upside_down (2024-2025
Dahua); all Reolink are right_side_up.

## Key commands

```bash
uv run python -m training.pipeline status      # pipeline state
uv run python -m training.pipeline games        # game list
uv run python -m training.pipeline events       # last 6h event log
uv run python -m training.pipeline enqueue tile --game GAME_ID --priority 30
```
