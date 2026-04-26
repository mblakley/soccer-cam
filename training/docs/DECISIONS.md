# Decision Log

Append-only. Never delete entries — if a decision is reversed, add a new entry explaining why.

---

## 2026-04-26: Virtual camera renders cylindrically; control logic from VIRTUAL_CAMERA.md sits on top

**Context:** `docs/VIRTUAL_CAMERA.md` (added 2026-04-25) specified the homegrown virtual broadcast camera as a flat 2D crop with intelligent control (lead room, zone-based zoom, dead-ball overrides, broadcast/coach modes). Reverse engineering of AutoCam (the third-party tool we are replacing) showed cylindrical projection is its single biggest visual marker and the reason its output looks broadcast-grade rather than "cropped from a fisheye." Our pipeline runs `StitchCorrectStage` upstream, which reduces inter-camera stitch artifacts but does not de-fisheye — the renderer's input is still effectively a ~180° fisheye-like panorama. A flat crop of that source curves straight lines (goal posts, sidelines) and stretches players near output edges; the further off-center the pan, the worse it looks. AutoCam picks cylindrical for the same reason.

**Decision:** Render layer is cylindrical projection (per the AutoCam reference doc at `\\DESKTOP-5L867J8\video\test\onnx_models\camera_viewport_algorithm.md`). The intelligent control logic from `docs/VIRTUAL_CAMERA.md` is unchanged — it sits on top of the cylindrical renderer, operating on yaw/pitch/zoom rather than a 2D crop rectangle. Field-zone classification upgrades from x-position-normalized to ball-projected-through-field-homography (real polygon model from a field-keypoint stage, replacing the heuristic). Smoothing constants borrow AutoCam's two-tier defaults (position EMA much heavier than zoom EMA). Lead room uses Kalman velocity from the existing `BallTracker` rather than a per-frame velocity EMA.

**Trade-off:** Cylindrical render adds per-frame `cv2.remap` cost (mitigated by caching the per-(out_w, out_h, fov) remap LUT) and adds a `field_mask` stage to the homegrown pipeline. In exchange we eliminate the fisheye-edge-distortion failure mode and gain a more accurate field-zone classifier.

**Files:** `docs/VIRTUAL_CAMERA.md` (rendering-layer addendum), `video_grouper/inference/cylindrical_view.py` (NEW), `video_grouper/inference/field_geometry.py` (NEW), `video_grouper/ball_tracking/providers/homegrown/stages/{render,field_mask,track}.py`. Branch `feature/render-cylindrical-and-field`, one commit per phase.

---

## 2026-04-15: Single canonical deploy script for remote workers

**Context:** Laptop worker kept dying after reboots and couldn't restart. Root cause: 3 conflicting scheduled tasks (`GPUWorker`, `LaptopWorker`, `PipelineWorker`) each pointing to different hand-edited bat files with wrong credentials, wrong CUDA paths, and TOML backslash escaping bugs. Each time someone fixed a problem they created a new bat/task instead of fixing the canonical deploy script.

**Decision:** `training/worker/deploy_worker.ps1` is the ONE AND ONLY way to deploy remote workers. It handles everything: code sync, config generation (with correct TOML UNC path escaping), startup bat generation, pip dependencies, scheduled task cleanup + registration, and post-deploy verification. Never create bat files, scheduled tasks, or edit configs by hand on remote machines. If a worker stops, re-run the deploy script.

**Trade-off:** Requires PS remoting enabled on remote machines. No support for partial updates — always does a full redeploy. This is intentional: idempotent full deploys are more reliable than incremental patches.

---

## 2026-04-14: Per-game field boundary replaces row-based spatial filter

**Context:** Trajectory building used `row >= 2` exclusion as a crude proxy for "off-field." This missed off-field detections in rows 0-1 and excluded legitimate on-field detections in row 2. Ball trajectory coverage was only 0-5% across all games, partly due to off-field noise polluting trajectory fragments.

**Decision:** Per-game field boundary polygon stored in manifest metadata (`field_boundary` key). Three-tiered detection: ONNX keypoint model (primary, proven on 9 games) → Sonnet vision fallback → human annotation. Trajectory building requires a valid polygon — skips if none available. Uses **soft filtering**: on-field and near-off-field (within 150px) kept, far-off-field excluded. This preserves throw-in/goal-kick continuity per the field-mask-must-be-soft principle.

**Impact:** Row-based `row >= 2` filter removed. Games need a field polygon before trajectory building runs. Human can draw/adjust polygons via annotation app Field tab.

---

## 2026-04-07: SQLite manifest + pack files replace loose tile/label files

**Context:** 7.7M loose JPEG tiles across 39 games on HDD. `os.listdir` on 300K-file directories takes 5+ minutes. Label files (500K .txt) are equally slow to scan. Everything is I/O-bound on HDD random reads.
**Decision:** Single `manifest.db` (SQLite WAL mode) as the source of truth for all tiles and labels.
- Schema: `games → segments → frames → tiles → labels` hierarchy
- Tile inventory: every .jpg cataloged with game_id, segment, frame_idx, row, col
- Pack files: all tiles for a segment concatenated into one `.pack` binary file, manifest stores (pack_file, pack_offset, pack_size) per tile
- Labels: YOLO bounding boxes stored in labels table (replaces 500K .txt files)
- Verification: `verify_tiles.py` queries manifest instead of scanning filesystem (~2ms vs ~5 min/game)
- Benchmark: pack file reads 245x faster than loose file reads on HDD (21K tiles/sec vs 29/sec cold)
**Trade-off:** One-time migration cost (~5hrs catalog + ~20hrs pack). DB is ~2GB. Pack files are same total size as loose files but 6 files per game instead of 300K.
**Files:** `training/data_prep/manifest.py`, `training/data_prep/verify_tiles.py`

## 2026-04-07: ONNX labeling writes to local manifest.db, merged on server

**Context:** Remote machines (FORTNITE-OP, laptop) run ONNX detection but can't directly write to the server's manifest.db.
**Decision:** `label_job.py` writes detections to a local `manifest.db` on the remote machine. After labeling completes, transfer the DB to the server and merge via `manifest merge`. Auto-backup before merge.
**Trade-off:** Extra transfer + merge step, but keeps the remote script self-contained (no network DB dependency during inference).
**Files:** `training/distributed/label_job.py`, `training/data_prep/manifest.py` (backup_db, merge_labels_from)

---

## 2026-03-31: Distributed tiling — laptop CPU helps while GPU trains

**Context:** 26 games need tiling, server takes ~30 min/game = ~13 hours alone. Laptop GPU is busy training but CPU is idle.
**Decision:** mass_tile.py supports `--remote` mode. Laptop reads video from network share, tiles locally, writes tiles to server's D: via share. Lock files (`.locks/{game_id}.lock`) prevent both machines from tiling the same game. 2-hour stale lock timeout.
**Trade-off:** Network I/O (~100 MB/s gigabit) is slower than local D: for video reads, but it's free CPU cycles. H.264 decode is CPU-bound anyway.
**Commands:**
- Server: `uv run python -m training.data_prep.mass_tile`
- Laptop: `uv run python -m training.data_prep.mass_tile --remote \\192.168.86.152\video \\192.168.86.152\training`

## 2026-03-31: Use YOLO26 for v3 training (upgrade from YOLO11)

**Context:** YOLO26 (Jan 2026) adds Small-Target-Aware Label Assignment (STAL) and Progressive Loss (ProgLoss) — built-in improvements for small object detection. Our ball is 8-40px, exactly the target scenario. SoccerDETR paper showed +3.2% ball mAP from scale-aware loss.
**Decision:** Switch from `yolo11n.pt` to `yolo26n.pt` for v3. Already available in our ultralytics 8.4.27 install — no package changes needed.
**Alternatives:** Custom Scale-Aware Focal Loss on YOLO11 — rejected because YOLO26 provides this natively.

## 2026-03-31: v3 is a continuous improvement loop, not a one-shot build

**Context:** Previous versions (v1, v2) tried to get labels right THEN train. This led to weeks of label work before any training started.
**Decision:** v3 starts training with imperfect labels from all 35 games. Human-in-the-loop and Sonnet fill gaps as training runs. Each model iteration finds more balls, reducing gaps. The loop converges naturally.
**Reason:** 4x more data with okay labels beats 1x data with perfect labels. The continuous loop means label quality improves alongside model quality.

## 2026-03-31: Game IDs include timestamp suffix for same-date games

**Context:** Two Flash games on 2025.05.04 produced duplicate game_ids (`flash__2025.05.04`).
**Decision:** Append HHMMSS from folder name when a time component exists (e.g., `flash__2025.05.04_031801`).
**Alternatives:** Sequential suffix (_a, _b) — rejected because timestamp is self-documenting and stable.
**Impact:** Renamed `flash__2025.06.02` to `flash__2025.06.02_181603`. Updated OLD_TO_NEW mapping.

## 2026-03-31: Frame extraction every 4th frame (not 8th)

**Context:** Previous `process_batch.py` used `FRAME_INTERVAL = 8` (~3 fps from 24.6 fps source).
**Decision:** Use `FRAME_INTERVAL = 4` (~6 fps) for denser coverage and more continuous ball tracking.
**Impact:** 2x more tiles per game, 2x more training data, ~2x longer tiling time.

## 2026-03-31: Recursive segment search for tournament games

**Context:** Tournament game folders have sub-folders (Game 1/, Game 2/, Game 3/) with [F] segments inside. Registry scanner only searched top level, missing all tournament games.
**Decision:** Changed `gdir.iterdir()` to `gdir.rglob("*.mp4")` in `build_registry()`.
**Impact:** Found 3 new games: Hershey Tournament (17 segs), and correctly detected Heat Tournament + Clarence Tournament.

## 2026-04-13: Upside-down game handling — `needs_flip` flag

**Context:** 9 games (May–June 2024) were recorded with the Dahua camera mounted upside down. Some have corrected `-raw.mp4` files but we don't use them — we always process the individual `[F]` segment files and flip in code.

**Decision:** The game registry has a `needs_flip INTEGER DEFAULT 0` column. When `needs_flip=1`:

1. **Tiling** (`tile.py:288`): `cv2.flip(frame, -1)` before cutting tiles → tiles are right-side up
2. **Labeling** (`label.py:161`): `cv2.flip(frame, -1)` before ONNX inference → detections are in right-side-up coordinates matching the tiles
3. **Prescan** (`label.py:268`): also flips before sampling frames for game detection

The flip is carried via the task **payload** (`{"needs_flip": true}`), built by:
- Orchestrator `_build_payload()` for both `tile` and `label` tasks (reads from game registry)
- CLI `cmd_enqueue()` also reads from registry when manually enqueueing

**Critical:** If a task is enqueued WITHOUT a payload (e.g., old queue items, direct DB insertion), `needs_flip` defaults to `False` and flipped games will be processed upside down. Always enqueue through the orchestrator or CLI.

**Flipped games (as of 2026-04-13):**
- `flash__2024.05.01_vs_RNYFC_away`
- `flash__2024.05.10_vs_NY_Rush_away`
- `flash__2024.06.01_vs_IYSA_home`
- `flash__2024.06.02_vs_Flash_2014s_scrimmage`
- `heat__2024.05.13_vs_Byron_Bergen_home`
- `heat__2024.05.19_vs_Byron_Bergen_home`
- `heat__2024.05.28_vs_Chili_home`
- `heat__2024.05.31_vs_Fairport_home`
- `heat__2024.06.04_vs_Spencerport_home`

**Files:** `training/pipeline/registry.py` (schema), `training/pipeline/orchestrator.py` (`_build_payload`), `training/tasks/tile.py` (flip at line 288), `training/tasks/label.py` (flip at lines 161, 268), `training/pipeline/__main__.py` (`cmd_enqueue` payload)

## 2026-03-30: Game naming convention

**Context:** Games had inconsistent IDs (old: `flash__06.01.2024_vs_IYSA_home`, tournament: `heat__Heat_Tournament`).
**Decision:** Standardized format: `{team}__{YYYY.MM.DD}_vs_{opponent}_{location}`. Teams lowercase, double underscore separator, date in sortable YYYY.MM.DD, single underscore between words, no spaces/parens.
**Impact:** Renamed all existing tile/label directories. Added OLD_TO_NEW mapping in game_registry.py.

## 2026-03-29: 3-class detection (game_ball / static_ball / not_ball)

**Context:** Binary ball/no-ball lost the distinction between the active game ball and static balls on sidelines (cones, spare balls, equipment).
**Decision:** Trajectory analysis classifies detections. Moving trajectory (path_length > 50px or max_speed > 20px/frame) = game_ball. Trajectory ≥3 frames but barely moves = static_ball. Isolated 1-2 frame detection = not_ball. QA verdicts override trajectory classification.
**Result:** 272K game_ball, 116K static_ball, 93K not_ball labels.
**Alternatives:** Binary detection + field mask filtering — rejected because field masks are imprecise and we lose sideline context.

## 2026-03-28: Independent workers replace Dask coordinator

**Context:** Dask dashboard websocket flooding DOSed the scheduler event loop. Workers disconnected every time coordinator restarted. Single point of failure.
**Decision:** Filesystem-based job queue (`jobs.py`) with independent workers. Jobs are JSON files in pending/active/done/failed directories. Atomic claim via `os.rename`. No coordinator process needed.
**Alternatives:** Ray — doesn't support Windows. Celery — too heavy for 3 machines. Dask — tried, failed due to dashboard bug and SPOF design.

## 2026-03-27: Tar shards for dataset portability

**Context:** Copying 275K individual tile+label files over network took hours and had high failure rate. SMB random I/O on USB drive was 5 MB/s.
**Decision:** Package dataset into ~200 MB tar shards organized by split/game/zone. Sequential reads, one file copy per shard, extract locally before training.
**Alternatives:** SQLite database per game — considered but YOLO expects filesystem layout. WebDataset streaming — considered for future.

## 2026-03-26: Relay training (server always trains, helpers preempt)

**Context:** 3 machines available but kids use 2 of them for gaming. Need server to always be productive, helpers to train when idle.
**Decision:** Server trains continuously with `train_relay.py`. When a faster GPU (laptop RTX 4070 or Fortnite-OP RTX 3060 Ti) becomes available, it preempts server training after current epoch via lock file + heartbeat.
**Alternatives:** Round-robin scheduling — rejected because server GPU should never be idle.

## 2026-03-22: Dataset uses folder structure, not train.txt file lists

**Context:** YOLO supports both `train.txt` (file lists) and folder-based dataset layout.
**Decision:** Use `images/{train,val}/{game}/` with NTFS hardlinks to tiles that have matching labels. YAML config uses folder paths.
**Reason:** Folder structure is simpler to maintain, YOLO creates .cache on first scan, no file list management.
**Impact:** Train: 348K tiles (13 games), Val: 46K tiles (2 games).

## 2026-03-22: Exclude upside-down games from v2 training

**Context:** `flash__2024.06.01_vs_IYSA_home` and `heat__2024.05.31_vs_Fairport_home` recorded with camera mounted upside down (sky at bottom, spectators at top).
**Decision:** Exclude both from v2 training dataset. For v3, include them with corrected video or code-flipped tiles.
**Reason:** Including upside-down frames would confuse the model about field orientation.

---

## 2026-04-09: HTTP API-only architecture for pipeline

**Context:** Multiple machines (server, laptop, FORTNITE-OP) need to coordinate work. Direct SQLite access from remote machines causes corruption.
**Decision:** Only the PipelineAPI process touches SQLite (registry.db, work_queue.db). Workers communicate exclusively via HTTP API. SMB shares are for bulk file transfer only (packs, manifests, videos).
**Trade-off:** Extra HTTP round-trips, but eliminates all SQLite concurrency issues.
**Files:** `training/pipeline/api.py`, `training/pipeline/client.py`, `training/worker/worker.py`

## 2026-04-09: Sonnet QA with Claude CLI for ball detection verification

**Context:** ONNX model produces many false positives (~85% NOT_BALL). Need automated QA before training.
**Decision:** Use `claude -p` CLI with Sonnet model to verify detections. Build 3x2 grid composites of tiles, ask Sonnet BALL/NOT_BALL for each. ~10s per grid, 120 tiles per game, included in Claude Max subscription.
**Trade-off:** Slower than a dedicated classifier, but zero additional cost and high accuracy.
**Files:** `training/tasks/sonnet_qa.py`

## 2026-04-10: Per-segment tiling — skip concatenated videos

**Context:** Video directories contain both individual segment files (`18.30.10-18.46.58[F][0@0][215198]_ch1.mp4`) and concatenated full-game videos (`flash-iysa-home-raw.mp4`, `combined.mp4`). Concatenated videos produce 100GB+ packs that fill the SSD.
**Decision:** Only tile files with `[F]` or `[0@0]` markers in the filename. Skip all others. Individual segments cover the same footage in manageable ~15-20GB chunks.
**Files:** `training/tasks/tile.py` (skip filter at line ~60)

## 2026-04-10: Legacy label remapping — raw frame indices to per-segment

**Context:** Legacy labels reference concatenated "raw.mp4" frame numbering (e.g., frame_idx=98444). New tiles use per-segment numbering (e.g., frame_idx=22694 within segment 4). Frame indices map cleanly via cumulative offset table.
**Decision:** `remap_legacy_labels()` builds offset table from individual segment .mp4 files, remaps label tile_stems and tile frame indices in the manifest. Both tiles and labels end up using per-segment frame numbering.
**Files:** `training/data_prep/game_manifest.py` (`remap_legacy_labels()`)

## 2026-04-11: F: as permanent pack archive, D: as serving tier

**Context:** D: (HDD, 1.9TB) can't hold all pack files (~80GB/game × 30+ games = 2.4TB). F: (USB, 15TB) has ample space.
**Decision:** Pack lifecycle: create on G: SSD → push to D: (for SMB serving) → archive to F: (permanent) → clean D:. When a task needs packs, `server_packs()` auto-restores from F: to D:. Remote workers access D: via SMB; they never see F:.
**Manifest convention:** `pack_file` column always stores D: paths. The system transparently stages from F: when D: copies don't exist.
**Trade-off:** Extra copy F:→D: when accessing old games. But D: stays small and we never run out of space.
**Files:** `training/tasks/io.py` (`server_packs`, `cleanup_server_packs`), `training/tasks/tile.py` (archive step), `training/data_prep/manifest_dataset.py` (`_resolve_pack_path`)

## 2026-04-11: Python 3.13 standardized across all machines

**Context:** Server had 3.13, laptop had 3.12. CUDA DLLs were available in system Python's PyTorch installation but not in the uv venv's PATH.
**Decision:** All machines use Python 3.13. Worker startup bat files add `C:\Python313\Lib\site-packages\torch\lib` to PATH for CUDA 12 DLLs (cublas, cudnn, cufft, etc.). No separate CUDA toolkit installation needed — PyTorch bundles everything.
**Files:** `training/pipeline/run_laptop_worker.bat`, worker pyproject.toml (`requires-python = ">=3.13"`)

## 2026-04-11: Flywheel improves training data, not labeling model

**Context:** The pipeline uses an external ONNX model for initial ball detection labels. Our trained model may or may not be better.
**Decision:** The flywheel cycle improves the training DATASET, not the labeling model:
1. External ONNX labels (`source='onnx'`) — baseline, always preserved
2. Sonnet QA verdicts (`qa_verdict`) — automated verification, accumulates
3. Human reviews (`source='human_gap_review'`) — highest-value labels from trajectory gaps
4. Training uses all verified data to build our model
5. Our model is only deployed for labeling if it demonstrably outperforms the external model on the human-verified test set

Label sources are tracked separately so we can always compare model performance against ground truth. We never overwrite external model labels — QA verdicts and human labels are additive.
**Files:** `training/data_prep/game_manifest.py` (labels table: source, qa_verdict columns)

## 2026-04-11: Ball track length is the ground truth metric

**Context:** Multiple metrics could indicate model quality — precision, recall, mAP, false positive rate. But the purpose of the model is to track the game ball continuously.
**Decision:** The primary metric is **verified game ball track length** — the longest continuous trajectory confirmed by the human reviewer, measured as a percentage of total game time. This directly measures what we care about: can the model see the ball throughout the game?
- Sonnet QA helps filter false positives but isn't perfect
- Only human verification of the trajectory confirms ground truth
- Retraining is valuable when track gaps exist that new labels could fill
- The flywheel naturally converges: longer tracks → fewer gaps → fewer human reviews → less retraining needed → done
**Trade-off:** Harder to measure automatically than mAP. Requires trajectory building + human review to evaluate. But it's the metric that actually matters for the autocam use case.
