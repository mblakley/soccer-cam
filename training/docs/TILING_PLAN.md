# Tiling Completion Plan

*Created: 2026-04-02*
*Goal: All 31 games tiled, verified, and ready for training*

## Current State (as of 2026-04-02 11:00)

- **15 games** have tile directories on D:/training_data/tiles_640/ (unverified)
- **18 zips** being moved from D: to F:/tile_zips/ (in progress, ~80 MB/s)
- **6 games** not yet staged (video not copied to D:/staging)
- **13 games** not yet tiled at all
- **Laptop** unreachable via PS remoting (auth issue)
- **D: free:** ~195GB and growing as zips move off

## Phase 1: Verify Zip Move

- [ ] Confirm all 18 zips landed on F:/tile_zips/ with correct sizes
- [ ] Retry the locked zip #5 (`flash__2025.05.03_11.16.08-11.32.58[F][0@0][111736].zip`, 20.7GB)
- [ ] Delete D:/training_data/tile_zips/ directory

## Phase 2: Verify Zip Integrity

For every zip on F:/tile_zips/:

- [ ] Run `zipfile.ZipFile.testzip()` — returns first bad file or None
- [ ] Catalog contents: parse filenames to get segments, frame indices, tiles per frame
- [ ] Cross-reference segment list vs game_registry.json — flag missing segments
- [ ] Check tile completeness: every frame must have exactly 21 tiles (r0-r2 × c0-c6)
- [ ] Report: per-game summary of segments found, frame count, tile count, any gaps

### Expected zip contents

Zips contain tiles for these games:

| Game | Zip pattern | Segments expected |
|------|-------------|-------------------|
| `flash__2024.10.04_vs_RNYFC_MLS_Next_away` | 1 large zip | 1 (raw video) |
| `flash__2025.05.03` | 8 segment zips | 8 |
| `flash__2025.05.31` | 1 large zip | 6 |
| `heat__2024.06.25_vs_Pittsford_home` | 6 segment zips | 6 |
| `heat__2024.07.02_vs_Spencerport_away` | 3 segment zips | 5 (already tiled on D:, may be partial overlap) |

## Phase 3: Verify Existing Tiles on D:

For each of the 15 game directories already in D:/tiles_640/:

- [ ] Parse all tile filenames to extract segment ID, frame index, row, col
- [ ] Verify all segments from game_registry.json are represented
- [ ] Verify every frame has exactly 21 tiles (7 cols × 3 rows)
- [ ] Check for suspiciously low frame counts (could indicate early termination)
- [ ] Report: per-game summary with segment coverage, frame count, total tiles

### Games to verify on D:

1. `flash__2024.05.01_vs_RNYFC_away`
2. `flash__2024.05.10_vs_NY_Rush_away`
3. `flash__2024.06.02_vs_Flash_2014s_scrimmage`
4. `flash__2024.06.15_vs_Buffalo_Empire_home`
5. `flash__2024.06.19_vs_NY_Rush_home`
6. `flash__2024.06.30_vs_IYSA_away`
7. `flash__2024.09.21_vs_IYSA_away`
8. `flash__2024.10.04_vs_RNYFC_MLS_Next_away` (partially extracted from zip)
9. `flash__2024.10.13_vs_Kenmore_away`
10. `flash__2024.10.27_vs_Rush_home`
11. `flash__2025.04.12`
12. `flash__2025.05.04`
13. `flash__2025.05.07`
14. `heat__2024.05.19_vs_Byron_Bergen_home`
15. `heat__2024.07.02_vs_Spencerport_away`

## Phase 4: Extract Verified Zips → Tiles

- [ ] Extract each verified zip from F:/tile_zips/ → D:/tiles_640/
- [ ] One at a time, delete zip from F: after successful extraction + verification
- [ ] Skip `heat__2024.07.02_vs_Spencerport_away` if D: copy is already complete
- [ ] Re-verify extracted tiles match zip contents (file count)

## Phase 5: Fill Gaps

For any game with issues found in Phases 2-4:

- [ ] **Missing segments**: Re-tile just those segments from D:/staging video (or re-stage from F: first)
- [ ] **Partial frames** (<21 tiles): Re-tile just those frames
- [ ] **Corrupt zips**: Re-tile the entire game from staging video
- [ ] **Truncated games** (suspiciously low frame count): Investigate — corrupt video? OOM kill? Re-tile.

Gap-filling uses the same in-memory PyAV tiler, writing directly to D:/tiles_640/{game}/.

## Phase 6: Stage & Tile Remaining Games

### 6a: Stage from F: → D:/staging

| Game | Source | Segments | Status |
|------|--------|----------|--------|
| `flash__2024.06.01_vs_IYSA_home` | F:/ root | 6 | **READY** |
| `flash__2024.09.27_vs_RNYFC_Black_home` | F:/Flash_2013s/09.27.2024... | 7 | 2/7 staged |
| `flash__2024.09.30_vs_Chili_home` | F:/Flash_2013s/09.30.2024... | 7 | 0/7 staged |
| `flash__2025.05.17` | already staged | 5 | **READY** |
| `flash__2025.06.02` | already staged | 6 | **READY** |
| `heat__2024.05.13_vs_Byron_Bergen_home` | already staged | 1 | **READY** |
| `heat__2024.05.28_vs_Chili_home` | already staged | 1 | **READY** |
| `heat__2024.05.31_vs_Fairport_home` | F:/ root (1 seg missing) | 5/6 | not staged |
| `heat__2024.06.04_vs_Spencerport_home` | already staged | 1 | **READY** |
| `heat__2024.06.20_vs_Chili_away` | F:/Heat_2012s/06.20.2024... | 7 | not staged |
| `heat__2024.06.27_vs_Pittsford_away` | already staged | 6 | **READY** |
| `heat__2024.07.17_vs_Fairport_away` | F:/Heat_2012s/07.17.2024... | 6 | not staged |
| `heat__2025.05.13` | already staged | 5 | **READY** |

### 6b: Tile all remaining games

- [ ] Update server_games.json with all 13 remaining games
- [ ] Run server tiler (in-memory PyAV → tiles on D:)
- [ ] Split work with laptop if connectivity restored
- [ ] Verify each game after tiling (same checks as Phase 3)

## Phase 7: Final Audit

- [ ] Every game_id in registry has a D:/tiles_640/{game_id}/ directory
- [ ] Every game has all segments represented
- [ ] Every frame has 21 tiles
- [ ] Count total tiles across all 31 games
- [ ] Clean up D:/staging to reclaim space
- [ ] Update STATUS.md with final dataset size

## Phase 8: Bootstrap & Training

- [ ] Run ONNX detector on all new tiles for initial labels
- [ ] Start flywheel cycle 1
- [ ] Resume v3 training on laptop GPU (YOLO26l)

## Tile Filename Format

```
{segment_stem}_frame_{NNNNNN}_r{R}_c{C}.jpg
```

- `segment_stem`: video filename without .mp4 extension
- `NNNNNN`: 6-digit zero-padded frame index (actual frame number in video)
- `R`: row 0-2
- `C`: col 0-6

Example: `18.01.30-18.18.20[F][0@0][189242]_ch1_frame_000048_r1_c3.jpg`

## Tiling Parameters

| Parameter | Value |
|-----------|-------|
| Tile size | 640×640 |
| Grid | 7 cols × 3 rows = 21 tiles/frame |
| Frame interval | every 4th frame |
| Diff threshold | 2.0 (skip near-duplicate frames) |
| JPEG quality | 95 |
| step_x | 576 |
| step_y | 580 |
| Decoder | PyAV (av) with corrupt frame handling |
