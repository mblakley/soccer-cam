# Decision Log

Append-only. Never delete entries — if a decision is reversed, add a new entry explaining why.

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

## 2026-03-30: Upside-down game handling strategy

**Context:** 13 of 32 games recorded with camera mounted upside down. 11 have corrected full-res `*-raw.mp4` files. 2 have no corrected version.
**Decision:** Three strategies: (1) Use corrected .mp4 if available, (2) flip in code via `cv2.flip(frame, -1)` for games without corrected video, (3) exclude from training if too problematic.
**Alternatives:** Always flip in code — rejected because corrected videos may have other fixes (exposure, color correction).

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
