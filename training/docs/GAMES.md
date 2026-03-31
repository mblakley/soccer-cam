# Game Notes

Per-game observations about quality, orientation, and quirks. Machine-readable metadata is in `game_registry.json` — this file captures human observations.

---

## Flash 2013s

### flash__2024.05.01_vs_RNYFC_away
- **Orientation:** Upside-down (corrected video: `flash-rnyfc-away-05-01-2024-raw.mp4`)
- **Tiled:** No | **Labeled:** No
- **Notes:** —

### flash__2024.05.10_vs_NY_Rush_away
- **Orientation:** Upside-down (corrected video: `combined_rotated.mp4`)
- **Tiled:** No | **Labeled:** No
- **Notes:** Corrected video is a single combined file, not per-segment

### flash__2024.05.25_Hershey_Tournament
- **Orientation:** Right-side up
- **Tiled:** No | **Labeled:** No
- **Segments:** 17 across 4 sub-games
- **Notes:** Multi-game tournament recording. Game phase detection will need per-sub-game handling.

### flash__2024.06.01_vs_IYSA_home
- **Orientation:** Upside-down (corrected video: `flash-iysa-home-06-01-2024-raw.mp4`)
- **Tiled:** Yes | **Labeled:** Yes | **QA'd:** Yes (Sonnet)
- **Notes:** Excluded from v2 training (upside-down camera would confuse model). Include in v3 with corrected video tiles.

### flash__2024.06.02_vs_Flash_2014s_scrimmage
- **Orientation:** Upside-down (corrected video: `flash-flash2014s-scrimmage-06-02-2024-raw.mp4`)
- **Tiled:** No | **Labeled:** No
- **Notes:** Scrimmage, not a real game. May have different ball count/behavior.

### flash__2024.06.15_vs_Buffalo_Empire_home
- **Orientation:** Right-side up
- **Tiled:** No | **Labeled:** No

### flash__2024.06.19_vs_NY_Rush_home
- **Orientation:** Right-side up
- **Tiled:** No | **Labeled:** No

### flash__2024.06.30_vs_IYSA_away
- **Orientation:** Right-side up
- **Tiled:** No | **Labeled:** No

### flash__2024.09.21_vs_IYSA_away
- **Orientation:** Right-side up
- **Tiled:** No | **Labeled:** No

### flash__2024.09.27_vs_RNYFC_Black_home
- **Orientation:** Right-side up
- **Tiled:** Yes | **Labeled:** Yes | **QA'd:** Yes (Sonnet)
- **Notes:** Phase detection shows first_half 10:19-20:31 which is 10+ hours — clearly wrong, likely spans multiple segments with gap.

### flash__2024.09.30_vs_Chili_home
- **Orientation:** Right-side up
- **Tiled:** Yes | **Labeled:** Yes | **QA'd:** Yes (Sonnet)
- **Notes:** 328 ball verification frames pending human review in annotation UI.

### flash__2024.10.04_vs_RNYFC_MLS_Next_away
- **Orientation:** Upside-down (corrected video: `flash-rny-mls-next-away-10-04-2024-raw.mp4`)
- **Tiled:** No | **Labeled:** No

### flash__2024.10.13_vs_Kenmore_away
- **Orientation:** Upside-down (corrected video: `flash-kenmore-away-10-13-2024-raw.mp4`)
- **Tiled:** No | **Labeled:** No
- **Segments:** 12 — longest game by segment count

### flash__2024.10.27_vs_Rush_home
- **Orientation:** Right-side up
- **Tiled:** No | **Labeled:** No

### flash__2025.04.12_113623
- **Orientation:** Right-side up
- **Tiled:** No | **Labeled:** No

### flash__2025.05.03_103137
- **Orientation:** Right-side up
- **Tiled:** No | **Labeled:** No

### flash__2025.05.04_031801
- **Orientation:** Right-side up
- **Tiled:** No | **Labeled:** No
- **Notes:** Two games on this date — disambiguated by timestamp suffix.

### flash__2025.05.04_051657
- **Orientation:** Right-side up
- **Tiled:** No | **Labeled:** No
- **Notes:** Second game on 2025.05.04. Different time slot.

### flash__2025.05.07_113623
- **Orientation:** Right-side up
- **Tiled:** No | **Labeled:** No

### flash__2025.05.17_164952
- **Orientation:** Upside-down (**NO corrected video — flip in code**)
- **Tiled:** No | **Labeled:** No
- **Notes:** Must use `cv2.flip(frame, -1)` during frame extraction.

### flash__2025.05.31_161424
- **Orientation:** Right-side up
- **Tiled:** No | **Labeled:** No

### flash__2025.06.02_181603
- **Orientation:** Upside-down (**NO corrected video — flip in code**)
- **Tiled:** Yes | **Labeled:** Yes | **QA'd:** Yes (Sonnet)
- **Notes:** Was originally `flash__2025.06.02`, renamed with timestamp suffix.

## Heat 2012s

### heat__2024.05.13_vs_Byron_Bergen_home
- **Orientation:** Upside-down (corrected video: `heat-bergen-home-05-13-2024-raw.mp4`)
- **Tiled:** No | **Labeled:** No

### heat__2024.05.19_vs_Byron_Bergen_home
- **Orientation:** Right-side up
- **Tiled:** In progress (test run) | **Labeled:** No
- **Notes:** Used as test game for mass_tile.py pipeline validation. 4 segments.

### heat__2024.05.28_vs_Chili_home
- **Orientation:** Upside-down (corrected video: `heat-chili-05-28-2024-raw-fixed2.mp4`)
- **Tiled:** No | **Labeled:** No
- **Notes:** Multiple "fixed" versions exist on D: (fixed2 through fixed6) — unclear which is best.

### heat__2024.05.31_vs_Fairport_home
- **Orientation:** Upside-down (corrected video: `flash-fairport-home-05-31-2024-raw2.mp4`)
- **Tiled:** Yes | **Labeled:** Yes | **QA'd:** Yes (Sonnet)
- **Notes:** Excluded from v2 training (upside-down camera). Include in v3 with corrected tiles.

### heat__2024.06.04_vs_Spencerport_home
- **Orientation:** Upside-down (corrected video: `heat-spencerport-home-06-04-2024-raw.mp4`)
- **Tiled:** No | **Labeled:** No

### heat__2024.06.07_Heat_Tournament
- **Orientation:** Right-side up
- **Tiled:** Yes | **Labeled:** Yes | **QA'd:** Yes (Sonnet)
- **Segments:** 17 across 3 sub-games
- **Notes:** Phase detection shows "second_half" 07:57-18:06 (10+ hours) — includes breaks between sub-games. Need per-sub-game phase detection.

### heat__2024.06.20_vs_Chili_away
- **Orientation:** Right-side up
- **Tiled:** Yes | **Labeled:** Yes | **QA'd:** Yes (Sonnet)

### heat__2024.06.25_vs_Pittsford_home
- **Orientation:** Right-side up
- **Tiled:** No | **Labeled:** No

### heat__2024.06.27_vs_Pittsford_away
- **Orientation:** Upside-down (**NO corrected video — flip in code**)
- **Tiled:** No | **Labeled:** No
- **Notes:** Must use `cv2.flip(frame, -1)` during frame extraction.

### heat__2024.07.02_vs_Spencerport_away
- **Orientation:** Right-side up
- **Tiled:** No | **Labeled:** No

### heat__2024.07.17_vs_Fairport_away
- **Orientation:** Right-side up
- **Tiled:** Yes | **Labeled:** Yes | **QA'd:** Yes (Sonnet)

### heat__2024.07.20_Clarence_Tournament
- **Orientation:** Right-side up
- **Tiled:** Yes | **Labeled:** Yes | **QA'd:** Yes (Sonnet)
- **Segments:** 15 across 3 sub-games
- **Notes:** Same tournament detection issue as Heat Tournament.

### heat__2025.05.13_182605
- **Orientation:** Right-side up
- **Tiled:** No | **Labeled:** No
