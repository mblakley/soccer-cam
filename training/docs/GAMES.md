# Game Notes

Per-game observations about quality, orientation, and quirks. Machine-readable metadata is in `game_registry.json` — this file captures human observations.

---

## 2026-07-22 — polygon-store cleanup findings (vision-verified, cross-cutting)

- **Inverted stored polygons (repaired + Mark-confirmed):** `flash__2024.05.01_vs_RNYFC_away`,
  `flash__2024.05.10_vs_NY_Rush_away` (polygon never re-mapped after `combined_rotated.mp4`),
  `heat__2024.06.04_vs_Spencerport_home` (near/far point order reversed), `heat__2024.06.27_vs_Pittsford_away`.
  Root cause class: polygon labeled in raw vs rotation-corrected space inconsistently; `video_rotation`
  tags in the 2024 archive were unreliable (also fixed: `heat__2024.05.19_vs_Byron_Bergen_home` was a
  real game tagged right_side_up but stored upside-down).
- **Dense legacy traces reduced to 10-pt:** 6× 54-pt `gamedata` outlines (several — e.g.
  `heat__2025.07.22_vs_Fairport_away` — only traced the LEFT half of the field; Mark dragged the right
  side out) + 2× `sonnet` 12/14-pt.
- **Field-model domain misses (real games, polygon drawn by hand):** `flash__2026.03.21_vs_Cuyahoga_Valley_SA_home`
  = BLUE-turf indoor dome (model trained on green grass); `flash__2025.05.10_vs_Rush_home` = patchy
  brown field, near touchline runs off the bottom of frame.
- **House/indoor recordings EXCLUDED (not games; `field_polygon_note` exclude, reversible):**
  `heat__2025.07.11_vs_Honeoye_Falls_Blaze_home_FF`, `heat__2025.07.12_Niagara_Tournament`,
  `heat__2025.07.13_Niagara_Tournament_{17.40,18.56,19.10}`, `heat__2026.05.31_vs_West_Seneca_Sc_away`,
  `heat__2026.05.30_vs_Spencerport_gold_2_away`, `heat__2026.05.07_vs_Pittsford_Mustangs_away`,
  `flash__2026.05.10_vs_Upper_90_FC_home_11.19` — each has a same-date sibling entry that IS the real
  game. `heat__2025.07.12` has NO game footage at all. All `camera__*` entries are raw captures,
  auto-hidden from the field-edit picker.
- **AutoCam distill coverage (for the geometry-conditioned detector):** 39/47 confirmed-polygon Dahua
  games have `*.mp4.jsonl` detections; thin outliers: `heat__2024.05.31_vs_Fairport_home` (167 lines),
  `flash__2025.06.14_vs_Vestal_home` (1,761).

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

## Heat 2012s — 2026 (Reolink)

### heat__2026.06.15_vs_Irondequoit_away
- **Orientation:** Right-side up | **Camera:** Reolink 7680×2160 (`reolink_segments`, 19 segments)
- **Tiled:** No | **Labeled:** No | **Detections:** **None yet (pending)**
- **Archive:** `F:\Heat_2012s\2026.06.15 - vs Irondequoit (away)\` — raw full-field (trimmed game-window)
  video at `…\2026.06.15 - BU14 - Guzzetta vs Irondequoit (Parma Town Hall Park)\bu14---guzzetta-irondequoit-parma-town-hall-park-06-15-2026-raw.mp4` (13.25 GB); 19 `RecM09_*.mp4`
  segments + `combined.mp4` alongside. Registered in `game_registry.json` (`trainable=false`,
  `detections_status=pending`). Venue is Parma Town Hall Park (a neutral pitch) but named `away` to
  match the sibling `heat__2026.06.04_vs_Irondequoit_away` and the 2026 Heat-Reolink `home`/`away`
  convention. "Guzzetta" in the source filenames is the legacy mislabel for team **heat**.
- **Significance — FIRST game after the 2026-06-15 Reolink contrast/visibility calibration**
  (DECISIONS 2026-06-15: WDR on, drc 150, dayNight=Color locked, saturation 150, contrast 140,
  sharpen 145, baked into `ReolinkCamera.apply_optimal_settings`). This is the first real-game footage
  shot with that profile, so it is **high-value for the ball detector if the calibration improved
  far-ball contrast** — exactly the far-field separability the v4/distill detector is bottlenecked on.
  Worth prioritising for detections + a side-by-side far-ball-contrast comparison vs a pre-calibration
  Reolink game (e.g. 06-04 Irondequoit, same opponent/camera) once labels exist.
- **Why no detections:** AutoCam ball-detection **crashed on 2026-06-15** (RDP-GPU contention — see the
  AutoCam RDP/RAM-thrash notes); the per-segment `.mp4.jsonl` sidecar is empty (0 bytes). No ball
  detections were produced, so the game is **not yet trainable**.
- **Next step to make it trainable:** generate ball detections via a post-curve AutoCam distill run
  (decrypted `balldet_fp16_dec.onnx` @ max_width 1600, the standard
  `F:\archive\ball_distill\<game>\{ball_track.json,detections\segNN.json}` pipeline), **or** human
  far-labels with the canonical far-label tool. Do NOT run that on the shared GPU while the distill
  curve is using CUDA.
- **Update 2026-07-12 — GOLDEN HOUR is a DETECTOR wall (EXP-DIST-43; supersedes the "no detections /
  not labeled" status above):** this is now a HELD-OUT eval game. Our HeatmapNet candidate dump exists
  (`G:\ballresearch\selector\fullgame_heldout\heat__2026.06.15_vs_Irondequoit_away`, stride-4, 8 parts)
  and it has **702 human ball labels** (576 positioned), used in the v5/v6 band analysis. Firsthand
  finding (raw strips + candidate ceiling, verified): kickoff **~18:28 mid-June = golden hour**, low sun
  backlighting the far (upper) half of the panorama with a bright glare/haze band across the
  upper-center-right. Measured cost — our detector **ceiling** (GT ball within 100px of ANY candidate)
  is only **~0.35 across the whole frame** vs **~0.75 on held-out Spencerport 05.31**, worst on the
  RIGHT (median 111px to nearest candidate, toward the sun) where ~60% of the labeled play sits. So the
  ball is frequently **not even a candidate**: a DETECTOR-domain (lighting) failure, NOT a selection
  problem — selection gold cannot recover a ball the detector never proposed. This IS the far-ball
  contrast comparison this entry asked for, and the answer is that the 2026-06-15 WDR/high-contrast
  calibration did **not** overcome extreme golden-hour backlight. Fix is detector-side: lighting-diverse
  detector training (more backlit/low-sun games) and/or pre-detection tone/contrast normalization.
  Selector retrains lift Iron's GT-in-view via better tracking/coasting (v5 0.674 → v6 0.762) but cannot
  raise the ceiling.
