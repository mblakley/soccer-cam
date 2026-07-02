# Current Status

*Last updated: 2026-06-30 (untrimmed-KO / production-safety)*

## Game-phase detection — untrimmed-KO opt-in localize + PRODUCTION-SAFE (2026-06-30, latest)

Committed on `feat/phase-detection-game-start`: `3923ad5` (detector) + `bae8a1f` (S1 guard docs);
NOT pushed. See EXPERIMENTS.md **EXP-PHASE-14** and `~/.claude/plans/untrimmed-ko-handoff.md` (final
state) for the full write-up.

**Trimmed reolink 54/63 (86%) — up from 53/63 (EXP-PHASE-15, 2026-07-01):** halftime anchor is now
Mark's "whistle just before the field-empty; else the decline onset", with **central** dip selection
(a first-half stoppage can be the longest empty) and KO **decoupled** from the output HT (`ht_ko`).
HT 14→15/18 (W.Seneca +163→−2s), KO 13/15 unchanged, 2H 14/18, END 12/12; fixtures byte-identical.
Default `PHASE_HT_MODE=dipfirst` (revert with `=committed`). The opt-in-localize structure stands:
`fuse_phases(..., localize=False)` default (trimmed CLI + fixtures never localize); `detect_phases`
passes `localize=True`.

**Combined-video (untrimmed = production regime) KO: 9/14 within-10s** (ff04e6d was 8/13). Fix =
full-field BAND anchor `[0.55,1.8]×busy` + not-cleared (the UPPER bound rejects dense warm-up crowds
that over-localized before). 05.31-Spencerport −83→−1s (removed a confident-wrong trim).

**Auto-trim gate = `ko_trustworthy`, backup = 60s (decision 2026-07-01).** Trim =
`KO − PHASE_KO_TRIM_BACKUP_SECONDS` (60s; decoupled from the NTFY walk's 240s), so an EARLY KO is
trim-safe; only a LATE KO mis-trims. Every trusted, **non-truncated** KO is early/tiny (worst −42s,
< 60s margin). Gating on `ko_trustworthy` (not `ok`) auto-trims 9/14 combined GT games (was 7), all
within 60s. **Truncated-start is NOT separable** from a real game by any available signal (no reliable
schedule; field-dip / block-onset / asym all overlap — 05.27 real ≡ 05.09 truncated), and the
`truncated_start` flag does not exist in the live pipeline. Per **option B (accepted)** we auto-proceed
on trust and let a truncated recording (05.09, 06.06-Fairport) silently mis-trim rarely, caught by the
S3 verify loop / a viewer — rather than interrupt every confident game. The old "S1 skips on
`truncated_start`" guard (decision 8) was never implemented and is not achievable; superseded here.

**Ceiling reached** for the audio+player-curve signal set (9/14 vs 84% trimmed). Reverted failed
levers (curve-onset, loosened-clear bench-dip, ball-refine, symmetric-trust) — all in EXP-PHASE-14.
The 5 combined misses are genuinely signal-limited; 3 correctly untrusted (→NTFY), 2 early/trim-safe.

**OPEN DECISION (Mark):** flip game-start default to phase-detection (proceed S1–S4/T2) accepting 64%
display accuracy + human-verify, or keep game-start on NTFY and use phases for post-trim display only?
Safety bar met; the accuracy gap is display-quality (human-verify catches it), not trim-safety.

## Game-phase detection — kickoff signature + GT review (2026-06-30, earlier)

Interactive GT review (Mark verifying each off-boundary on YouTube) + two detector passes.

**GT corrections found + written to game.json (source=human):** 06.06 Fairport KO was invalid
(mapped before video start) → start-truncated; 06.10 HT 42:23→**44:25** (the 42:23 was the ref's
attention-whistle; the detector's 44:23 was right); West Seneca END → end-truncated. Truncation now
has explicit `truncated_start`/`truncated_end` booleans (see DECISIONS 2026-06-30); `trunc_flag.py`
is in the repo; phase_eval scores truncated games **per-boundary** (valid boundaries still count).

**Detector — signature-driven kickoff (commits 5026ec5, 6461a53):** KO/2H now = a CENTER static-
ball restart + a kickoff whistle (tight to the ball, or part of the 2H "ready" multi, or no-whistle
for wind-masked) + a near-full field (relative count) — applied throughout the video. Fixed the
"grabbed a one-team restart / a positioning whistle / a later restart" misses.

**Reolink within-10s (verified):** KO **12/15**, HT 12/18, 2H **10/18** (was 5), END 10/12, ALL
**44/63 (70%)**, **median 1.6s**. Near-exact now: 05.07, 06.04, 05.10, 05.30-Western, 05.31-gold,
06.06-Fairport, 06.10; KO fixed on West Seneca/05.27/05.28/06.06-S.

**Remaining reolink misses (next):** (1) HT-selection family — 05.27 +139, 05.28 +194, West Seneca
+163 (GT correct; detector grabs the wrong halftime multi). (2) Two regressions the kickoff pass
introduced: **06.01 2H** -193→+379 and **06.08** (long 115-min game, now rejected: 2H -191 / END
-1655). (3) Truncated hard games 06.07-BU15, 03.21. Dahua KO/2H still need the homegrown ball
detector. NOTE: 70% isn't comparable to the earlier 60% — per-boundary truncation grew the
denominator (44→63); the within-10s count rose in every transition.

## Game-phase detection — FULL coverage of all GT games (2026-06-30)

**Coverage achieved.** The detector now scores **every** human-GT game, not just reolink (was ~13
of 38). The reolink-only filter is gone (`--reolink-only` is now opt-in); the dahua paths that
already existed are now actually exercised:
- **video**: `find_fullframe_video` picks the largest non-segment, non-cropped mp4 (dahua
  `combined_video` is frequently missing/odd across the 2024 archive).
- **orientation is auto-detected**, NOT from `video_rotation` (which is inconsistent — raw files
  tagged rot=0, rotated files too). `player_curve` votes in-field persons both ways over the first
  frames; `ball_restarts` flips iff that lands more detections in the upright polygon. **Vision-
  verified** on raw + pre-rotated dahua games (players land in the field; dahua 06.15 HT frame =
  field empty + teams at the sidelines).
- **no-play-plateau robustness**: youth games whose field never empties at halftime used to hard-
  fail; `segment()` now returns a placeholder and fusion uses the central multi-whistle (the
  earlier of the symmetric HT/2H pair) for HT. Fixed 3 reolink games (05.07 perfect).
- **no-audio crash fix** (some dahua combined videos have no audio stream).
- **misalignment guard**: games whose video span (`voff+vdur`) differs from the GT recording span
  by >120 s are incomplete or multi-game videos (06.01 -20 min, 10.13 +76 min); their timeline
  can't map to GT, so they're excluded as a DATA issue (like truncated games).

**Honest within-10s vs human GT (33 aligned games, 5 dahua excluded as data issues):**
| split | games | within-10s | median |err| |
|---|---|---|---|
| **combined** | 33 | **33%** (43/132) | 49 s |
| **reolink** | 13 | **60%** (31/52) | **3 s** |
| **dahua** | 20 | **15%** (12/80) | 128 s |

- Reolink is strong and **up from the prior reolink-heavy "48%"** (no-play robustness + symmetric
  HT/2H pairs). Near-perfect: Upper_90 (all <3 s), 06.04 Irondequoit (all <4 s), 05.07, 05.30.
- Dahua: **HT/END decent** (player-curve dip + order; 06.15 HT -1 s, 09.21 HT -9 s, 07.02 2H -11 s)
  but **KO/2H poor** — no usable whistle (8-16 kHz audio) so KO/2H lean on AutoCam ball restarts,
  and the center-circle restart is often mis-clustered (picks a goal-area restart, not the centre).

**Real remaining detector gaps (not data):**
- Dahua KO/2H. **Measured (2026-06-30) and ruled out the easy fix:** the player-curve `on2`
  (field-refill at the end of the HT dip) is a LOWER BOUND on 2H, not the kickoff — it ≈ GT 2H only
  when the 2nd-half warm-up is short (05.01 on2 51:08 vs GT 50:56, where the ball restart wrongly
  picked 60:09), but it REGRESSES games where the ball restart is already right (07.02 ball 47:07 =
  GT 47:18, on2 39:50 would be -448 s). `on1` ≈ 0:02 always (warm-up onset), useless for KO. There
  is no clean `on2`-vs-ball selection rule (distance-from-on2 doesn't separate the two regimes). The
  true gap is the dahua ball **detection**: the actual 2H-kickoff center restart is often not
  detected (AutoCam center cluster lands on a goal-area restart), so KO (symmetric off 2H) follows
  it wrong. Needs better center-restart detection (homegrown ball detector), not selection.
- A few reolink 2H -60..-120 s (05.27/05.28/06.01) and KO outliers (West_Seneca +384, 06.06 +236).

Branch `feat/game-phase-detection` pushed to origin. Box scripts (`G:\ballresearch\phase_*.py`)
mirror the committed copies; per-game signal cache rebuilt for all dahua games with correct
orientation. `phase_eval.py --human-only` is the scoreboard; `--include-misaligned` re-adds the 5.

## Game-phase detection — split to its own branch (2026-06-29)

Phase-prediction work now lives on **`feat/game-phase-detection`** (off `main`), extracted from
the `feat/homegrown-ball-detector` mega-branch so it can iterate/merge independently. Code:
`training/data_prep/phase_detect.py` (multi-signal detector: player-on-field curve + AutoCam
ball-restart + whistle), `phase_eval.py` (scores `--predict` output vs `game_state` source=`human`,
per-boundary seconds + within-10s fraction), `static/phase-edit{,-yt}.html` (GT verifiers — their
`/api/phasesv2` + YT-match endpoints still live on the mega-branch's `annotation_server.py`; port
if the in-browser editor is needed here). Detector/eval run on the box against `G:/F:`.

**Latest eval vs human GT (2026-06-29, 12 games / 48 transitions, predictions from 6/28):**
- within-10s **14/48 (29%)**, median |err| **72.5 s**; per-boundary medians: kickoff 42.8 s,
  halftime 17.7 s, second_half 125.4 s, end 133.6 s.
- **Bimodal**, not uniformly weak: 6/04 Irondequoit is exact on all four (kick +0 / half -4 / 2H +5 /
  end +0); 6/01 Pittsford + 6/06 close-ish. But several are off by **tens of minutes**
  (6/10 Lakefront: 2H -1699 s, end -3152 s; Upper_90: kick +356 s, end -705 s) — and some of those
  are even flagged `OK` by the detector's own cross-check, so the self-assessment is unreliable too.

**Alignment hypothesis DISPROVEN (verified).** The outliers were NOT a trim/segment offset (errors
weren't a constant per-game shift). Three real causes, all addressed or scoped:
- `ball_restarts` **crashed** on dahua sidecars (`KeyError: 't'` from the once-native `.mp4.jsonl`);
  fixed to read `ball_track.jsonl` {seg,f,x,y}->global sec. 6/10 video is **NOT missing** (full 97
  min present); it failed purely in fusion.
- **Fusion** leaned on the player-dip for halftime and capped END at `ht+sh`, so a spurious early
  dip (6/10 @21 min) killed the real end-whistle (which WAS detected @94.9 min).
- **Whistle detector is wind-noisy** (6/10: 17 multi-blasts; real HT/END are in there but buried).
  Per-frame wind-vs-whistle cleanup doesn't separate cleanly (kills clean games' real whistles), so
  we lean on **fusion + boundary signatures** instead of perfect whistle cleanup.

**Signature-based fusion (Mark's spec, 2026-06-29):** KO=single whistle->center ball-restart;
HT=multi-whistle the player-dip FOLLOWS (pick the one nearest game-centre); 2H=single whistle->next
center restart; END=last late multi-whistle (no `ht+sh` cap). Result on the 13 scored human-GT
games: **within-10s 27%->38%**, END **median 134s->2s** (8/13), HT **median 18s->6s** (7/13),
6/10 now 2H -3s / END -1s.

**GT is CORRECT — do NOT re-investigate a "+60s GT offset" (2026-06-29).** A timestamp spot-check
appeared to show human GT ~60s late on several games; this was a SCRIPT error, not a GT/web-app bug.
The YouTube uploads are the TRIMMED videos (yt_dur ≈ trimmed dur, confirmed via yt-dlp), so YT time
= global − trim_offset. A one-off check script used `t = global` instead of `global − trim_offset`,
so its links pointed ~trim_offset late; Mark's reported YT times + trim_offset exactly match the
stored game_state. The phase-edit-yt editor (offset = match_info trim_offset) is correct. Net: the
eval numbers below are scored against CORRECT GT; the detector's KO/2H/HT misses are real.

**KO snap tighten (2026-06-29):** within-10s 44%→**48%**, KO 4→6/13 (median 13s) — symmetric KO
prior snaps to a restart only within ±60s, else trusts the prior (6/10 +102→+24s, 06.01 −54→0s,
GUFC +106→−4s). Real remaining detector misses (vs correct GT): 06.01 2H −3min (grabs a warm-up-
return touch, not the real 2H set→burst), 05.27/05.28 HT +3min (HT locks onto a wrong dip/multi).

**KO/2H precision pass (2026-06-29):** got to within-10s **44%** via the phase ORDER + structure
(Mark's insight), after several from-scratch-selection approaches all washed out.
- DEAD ENDS (tried + reverted, each regressed as many games as it fixed because KO/2H are limited
  by the ball-restart CANDIDATES, not selection): whistle-gated restart (drops the real kickoff
  when its whistle is wind-masked: 6/04 +0->+238s), whistle->restart pairing, motion-confirm
  (set->burst burst is real — validated — but "first confirmed" lands on the wrong restart: 6/10
  +1352s), frame-scaled center tolerance (too tight on 1080p trims). The motion `cen_motion` data
  is cached in phase_cache but NOT used by the fusion.
- WHAT WORKED (committed): use the reliable HT (median 6s) + END (median 2s) to CONSTRAIN KO/2H.
  **2H = first center restart in [HT+3min, HT+18min]** (ordered after halftime, plausible break) —
  fixed Upper_90 2H -453s->-1s. **KO = equal-halves symmetric prior HT-(END-2H), snapped to the
  nearest real restart before HT** (rejects warm-up), first-whistle fallback for no-ball games.
  Result: KO 2->4/13, 2H 3->4/13; **Upper_90 + 6/04 now solved on all four boundaries**; no
  regressions. Remaining KO/2H misses (~1-3 min) are candidate quality: dahua 8kHz (no whistle,
  e.g. 09.30) and games where the ball detector missed the kickoff restart. Next gain would need a
  better ball-restart detector or a supervised whistle model, not more fusion.

**Still only 13 of 43 GT games scored** — 26 lack predictions: ~23 are 2024/25 **dahua_segments**
games whose video IS present (top-level timestamped `.mp4` + `combined.mp4`) but `files_offsets`
only looks for subdir/​combined mp4, and the detector's default list is **reolink-only** (line
~339). Extending coverage to dahua = next after KO/2H.

**COVERAGE is the bigger problem (census 2026-06-29).** Mark labeled **43** games with human GT,
but `phase_eval` scored only **12**. The other 31: **6 truncated** (excluded by design) + **25 with
no prediction**. Running `phase_detect --predict` on the 25 reveals THREE failure modes, not one:
- **no video on F:** — several 2024 flash games (`no video file in F:\Flash_2013s\...`); only the GT
  timestamps were entered, the source video was never archived. Data gap, not a detector issue.
- **detector abstains** — `no-play-plateau` (e.g. 6/06 Fairport, a 1080p video): the player-on-field
  curve never formed a clear plateau, so the detector correctly declines rather than guessing.
- **`KeyError: 't'` CRASH (bug)** — `phase_detect.py:300` `T.append(r["t"])` assumes every AutoCam
  sidecar line has a `t` key; some games' `.mp4.jsonl` use a different schema, so the ball-restart
  parser crashes (flash 09.30, 10.04 — ~3 min in, after the curve). Real fix needed: tolerate
  missing `t` (derive from `f`/frame index, or skip the line).

**Prioritized next steps:** (1) fix the `KeyError:'t'` parser bug → unblocks video-having games;
(2) confirm + fix the alignment/offset outliers (6/10, Upper_90) → could lift several into <10s;
(3) scope decision on the no-video 2024-flash games (need source video archived first).
Full per-game census: `G:\ballresearch\predict_missing.summary.json` (batch running).

## Field-boundary distillation (in progress, 2026-06-11)

Building an in-house "student" model to replace the third-party teacher field-polygon
model, on branch `feat/field-outline-distillation`. Code is complete and unit-tested:
`training/field_outline/` (package: augment, dataset, model) plus
`training/cli/{generate,train,eval,export}_field_outline.py`. See DECISIONS 2026-06-11
and EXP-008.

**Next steps (run on the GPU server — footage + CUDA are local there):**
1. Archive `D:/soccer-cam-storage` Reolink games to `F:` (team-routed Heat/Flash) and
   confirm zero UNKNOWN team routing (`generate_field_outline_labels --dry-run`).
2. `generate_field_outline_labels` over the F: archive → per-frame labels + overlays.
3. `train_field_outline` (overfit smoke first, then full run).
4. `eval_field_outline` on held-out venues; `export_field_outline --check` for parity.

## What's Running

Five processes on the server, two remote workers:

| Process | Machine | Port | What it does |
|---------|---------|------|-------------|
| PipelineAPI | Server | 8643 | FastAPI, sole SQLite accessor for registry + work queue |
| PipelineOrchestrator | Server | — | Populates work queues via API every 60s |
| PipelineWorker | Server | — | Pulls stage/tile/QA/review tasks |
| AnnotationServer | Server | 8642 | Human review UI (Tailscale: trainer.goat-rattlesnake.ts.net) |
| PipelineWorker | jared-laptop | — | Tile/label/train tasks (RTX 4070, CUDA) |
| PipelineWorker | FORTNITE-OP | — | Label/tile tasks (RTX 3060 Ti), yields for games |

**Restart server services:** `powershell -ExecutionPolicy Bypass -File training\pipeline\install_service.ps1`
**Deploy remote worker:** `powershell -ExecutionPolicy Bypass -File training\worker\deploy_worker.ps1 -Machine laptop|fortnite`

## Storage Architecture

```
F: (USB, 7.4TB free)     — PERMANENT storage
  /training_data/tile_packs/{game_id}/*.pack   — archived pack files
  /Flash_2013s/...         — original video files
  /Heat_2012s/...          — original video files

D: (HDD, ~1.8TB free)    — SERVING storage (SMB shared to remote workers)
  /training_data/games/{game_id}/manifest.db   — per-game manifests (~50-200MB each)
  /training_data/games/{game_id}/tile_packs/   — temporary pack staging (restored from F: on demand)
  /training_data/review_packets/               — human review packets
  /training_data/deploy/                       — remote worker deployment files

G: (SSD, 141GB)           — PROCESSING storage (local to server)
  /pipeline_db/registry.db                     — game registry
  /pipeline_db/work_queue.db                   — work queue
  /pipeline_work/{game_id}/                    — temporary work dirs (cleaned after each task)
```

### Pack File Lifecycle

```
TILE:    video on F: → extract to G: SSD → push packs to D: → archive to F: → clean D:
LABEL:   manifest says D: path → server_packs() restores F:→D: if missing → pull to G: SSD → ONNX
QA:      same as label (restore + pull)
TRAIN:   _resolve_pack_path() checks D: then F: → stages to local SSD → extract tiles → train
```

**Rule:** Manifests store pack_file paths as D: paths. Packs live permanently on F:. When a task needs packs, they're auto-restored from F: to D:, then cleaned after use. Remote workers access D: via SMB share `\\192.168.86.152\training`.

## Pipeline States

```
REGISTERED → STAGING → TILED → LABELING → LABELED →
QA_PENDING → QA_DONE → generate_review → TRAINABLE
```

- `FAILED:{stage}` — failed at a stage, reset with `reset-attempts` + state change
- `EXCLUDED` — not trainable (futsal, indoor)

## Game Pipeline Status

| State | Count | Notes |
|-------|-------|-------|
| LABELED | ~17 | Have labels, need tiling to complete |
| TILED | ~3 | Need ONNX labeling |
| STAGING | ~3 | Video path verified, need tiling |
| QA_DONE | 1 | Kenmore — ready for review |
| EXCLUDED | 7 | Futsal/indoor games |

~21 games need tiling to complete (server worker processing, ~1hr/game).

## What Needs to Happen

### Active (running now)
1. **D:→F: pack archive** — moving existing packs to F:, freeing D: (~975GB)
2. **Tiling** — server worker processing 21 games from F: video files
3. **Labeling** — laptop running ONNX on tiled games (CUDA via torch/lib PATH)

### After tiling/labeling completes
4. **Sonnet QA** — auto-enqueued for LABELED games (~18min/game)
5. **Game ball track confirmation** — human reviews filmstrip in annotation app
6. **Training** — auto-triggers when 2+ games reach TRAINABLE

### Known issues
- FORTNITE-OP needs redeployment when it comes online: `deploy_worker.ps1 -Machine fortnite`
- Phase 2 trajectory gap detection needs end-to-end test with properly tiled+labeled game
- 4 games had corrupt/missing packs (0-byte on F:), reset to REGISTERED on 2026-04-15:
  `flash__2024.05.01_vs_RNYFC_away`, `flash__2024.05.10_vs_NY_Rush_away`,
  `flash__2024.06.01_vs_IYSA_home`, `flash__2024.06.02_vs_Flash_2014s_scrimmage`

## Key Commands

```bash
# Pipeline status
uv run python -m training.pipeline status

# Game list
uv run python -m training.pipeline games

# Event log (last 6 hours)
uv run python -m training.pipeline events

# Queue management
uv run python -m training.pipeline enqueue tile --game GAME_ID --priority 30
uv run python -m training.pipeline priority ITEM_ID PRIORITY
uv run python -m training.pipeline delete ITEM_ID

# Reset failed games
# Via API: POST /api/game/{game_id}/reset-attempts + POST /api/game/{game_id}/state

# Audit all games
uv run python G:/pipeline_work/audit_all_games.py
uv run python G:/pipeline_work/audit_all_games.py --fix
```
