# Training Pipeline Roadmap

Last updated: 2026-04-09

## Current State

- **82 games** in registry (53 trainable, 29 excluded)
- **29 games** tiled + labeled (13 Flash 2024, 11 Heat 2024, 5 camera-futsal excluded)
- **24 games** need tiling (8 Flash, 15 Heat, 1 Guest)
- **7 games** have .dav files only (need remux to .mp4 first)
- **1 game** is Reolink 7680x2160 (needs adapted tiling pipeline)
- Training set v3.1: 155K tiles, Flash only, 6 train + 1 val

## Video Inventory Reference

- **YouTube playlists**: WNY Flash 2013s, Hilton Heat 2012s, Guest, Flash BUF 2013s, Heat 2013s
- **RDYSL**: https://www.rdysl.com/standings?Y=2025;GAD=Boys:13:1 (Heat 2025 season, 36 games)
- **Game registry**: `F:/training_data/game_registry.json` (also `D:/training_data/`)
- **Camera types**: Dahua B180 (4096x1800 H264), Reolink (7680x2160 HEVC)

### Missing Games (known, not yet copied)
- 7/22/2025: Heat vs Fairport (away) -- Reolink recording, on camera SD card
- 6/30/2025: Heat vs Greece United (home, Parma) -- may be on Reolink camera
- 8/28/2025: Flash vs Team Challenger FC North (away) -- away game, may not have recording

---

## Priority 1: Data Foundation

### 1.1 Remux .dav Files to .mp4
**Games**: 7 Heat games with dav_only format (07.11 FF, 07.11 guest, 07.12, 07.13 x3, 07.14, 07.16)
**Plus**: Camera/2024.10.27 (Flash vs Rush home -- 6 .dav files of outdoor soccer!)
**Approach**: `ffmpeg -i input.dav -c copy output.mp4` (fast remux, no re-encoding)
**Effort**: ~30 min script + run

### 1.2 Tile All Untiled Games
**Games**: 24 trainable games need tiling
**Approach**: Tile directly to pack format (skip loose tiles):
- Decode frame with PyAV in memory
- Crop 21 tiles (7x3 grid, 640px) in memory
- JPEG-encode in memory
- Append to `{segment}.pack`, record in manifest
**Sequencing**: Stage 1 game at a time from F: -> D:/staging, ~30 min/game
**Total**: ~12 hours unattended

### 1.3 Reolink Tiling Pipeline
**Game**: flash__2026.03.21 (7680x2160 HEVC)
**Challenge**: Different resolution than Dahua. Need wider tile grid or resize.
**Options**:
- Resize to 4096x1152 and use same 7x3 grid (loses some detail)
- Use wider grid (e.g. 12x3 = 36 tiles per frame) at 640px
- Resize to 4096x1800 with padding (maintains compatibility)
**Decision needed**: Which approach preserves ball detection quality best

### 1.4 Copy Reolink Games from Camera
**Connect Reolink camera**, check for:
- 7/22/2025 Heat vs Fairport (away) at Center Park West
- 6/30/2025 Heat vs Greece United (home) at Parma

---

## Priority 2: Pack Integrity & DB Architecture

### 2.1 Fix Pack Job Failure (Issue 18)
Pack files for flash__2024.09.27 are 0 bytes but tiles table says packed.
**Fix**: Write to `{segment}.pack.tmp`, verify size, `os.rename()`, then commit DB.
Add `verify_pack_integrity()` and `repair_pack()` functions.
**File**: `training/data_prep/manifest.py`

### 2.2 DB Corruption Prevention (Issue 19)
**Fix**: Explicit `PRAGMA wal_checkpoint(TRUNCATE)` after heavy writes. Keep last 3 backups per game. Run `PRAGMA integrity_check` on open for small DBs.

### 2.3 Per-Game Manifests (Issue 1)
Split monolithic 2.5GB manifest.db into per-game DBs (~50MB each).
Tiny `manifest_registry.db` for cross-game queries.
**Migration**: Non-destructive -- split, dual-read mode, update consumers, then retire monolithic.

---

## Priority 3: HDD Performance

### 3.1 I/O Serialization (Issues 2, 15)
Cross-process file lock (`hdd_lock.py`) to prevent pack job + build_training_set + review from fighting for D: spindle.
**File**: New `training/data_prep/hdd_lock.py`

### 3.2 Faster Training Set Build (Issue 7)
Current: 2.7 hours for 155K tiles (random seeks in 60GB pack files).
**Fix**: Read entire segment packs into memory and slice. Skip packs with 0 selected tiles.
**Target**: < 30 minutes.
**File**: `training/data_prep/manifest_dataset.py`

---

## Priority 4: Labeling & QA

### 4.1 FORTNITE-OP Crash Fix (Issue 8)
Silent exits during labeling. **Fix**: Per-segment try/except, heartbeat logging every 500 frames, ONNX session recovery on inference errors. Orchestrator detects incomplete jobs and resubmits.
**Files**: `training/distributed/label_job.py`, `training/pipeline/orchestrator.py`

### 4.2 Camera Game Video Discovery (Issue 10)
Now resolved by game registry update -- all games have paths in registry.

### 4.3 Confidence Backfill (Issue 9)
977K labels have NULL confidence. Re-run ONNX inference at tile level, match by IoU, update confidence column. ~30-80 min on GPU.
**File**: New `training/data_prep/backfill_confidence.py`

### 4.4 Sonnet QA Integration (Issue 13)
QA system exists but isn't in the pipeline. Add `action_sonnet_qa()` to orchestrator between labeling and human review. Create `training/pipeline/sonnet_reviewer.py` for Claude API vision calls.

---

## Priority 5: Training Diversity

### 5.1 Heat Game Inclusion (Issue 4)
Orchestrator needs team-aware game selection. Require at least 1 game per team.
**Prerequisite**: Heat games need tiling first (Priority 1.2).

### 5.2 Futsal Exclusion (Issue 5)
Now handled by `game_type` and `trainable` fields in registry. Add validation guard in `build_training_set()`.

### 5.3 ManifestDataset Server Validation (Issue 6)
Test on server GPU: instantiation, image loading, DataLoader with num_workers=2, 1-epoch dry run.
**Known fix needed**: `__getstate__`/`__setstate__` for pickle-safe multiprocessing.

---

## Priority 6: Review System

### 6.1 Root Redirect (Issue 11)
One-line fix: `annotation_server.py` redirect to `ball-verify.html`.

### 6.2 Broaden Review Candidates (Issue 12)
Support loose tile reading as fallback. Remove `confidence IS NOT NULL` filter.

---

## Priority 7: Infrastructure

### 7.1 Orchestrator Monitoring (Issue 3)
Add `/api/pipeline-status` endpoint to annotation server. Retry counters per action (max 3). NTFY alerts for machine offline > 15 min.

### 7.2 Network Training Prevention (Issue 14)
`assert_local_path()` guard in training scripts. Package training sets as tar before transfer.

### 7.3 Pipeline Automation (Issue 16)
Add orchestrator actions: `action_tile_untiled_games()`, `action_pack_unpacked_games()`, `action_generate_review()`, `action_ingest_reviews()`. Track per-game pipeline stage in state.

### 7.4 File Transfer Standardization (Issue 17)
Replace all Copy-Item with robocopy in `machine_manager.py`.

---

## Video Inventory System (Future)

Build `training/data_prep/video_inventory.py`:
- Persistent inventory at `D:/training_data/video_inventory.json`
- Auto-scan all F: directories
- Cross-reference with YouTube playlists via yt-dlp
- Auto-classify by resolution/codec
- Track processing status (tiled, packed, labeled, reviewed)
- `update_inventory()` for ongoing maintenance as new games are added
