# Current Status

*Last updated: 2026-06-29*

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

**KO/2H precision pass (2026-06-29):** within-10s 38%->40%; KO 2/13->3/13 via a no-ball->first-
whistle fallback (Upper_90 KO -247s->-2s, no regressions). Tried + REVERTED: whistle-gated restart
selection (drops the real kickoff when its whistle is wind-masked: 6/04 +0s->+238s) and
frame-scaled center tolerance (too tight on the 1080p trims). **KO (3/13) and 2H (3/13) stay the
weak boundaries and are now BLOCKED on whistle quality**: the warm-up-touch rejection + no-ball
anchoring both need the whistle, but the per-frame whistle detector has wind false-positives AND
misses some real kickoff whistles, so whistle-gating breaks as many games as it fixes. The real
unlock is a **supervised whistle detector** (label a handful of real whistle toots across a windy +
a calm game, train a tiny spectro-temporal classifier) — that would clean multi-blasts (better
HT/END too) AND make the KO/2H whistle-gate reliable. Per-frame DSP cleanup was tried and does not
separate wind from whistle. END + HT are solid; KO/2H need this before they improve further.

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
