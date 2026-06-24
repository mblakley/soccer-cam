# Current Status

*Last updated: 2026-06-24*

## Active focus: AutoCam-distillation data-scaling curve (RE-RUNNING — prior curve was invalid)

Branch: `feat/autocam-distill-detector` (worktree `../soccer-cam-autocam-distill`). All curve/distill
runtime lives in **server scratch `G:\ballresearch\distill\`** (per CLAUDE.md: no RE/one-off scripts in
the OSS repo); only the generic `training/data_prep/distill_dataset.py` is repo-resident. The v4-heatmap
focus below is still the architecture — the distill curve is its data-scaling study.

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
