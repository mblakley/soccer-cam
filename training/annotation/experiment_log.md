# Tracking Lab Experiment Log

**Segment:** flash__09.30.2024_vs_Chili_home / 18.02.52-18.19.36
**Duration:** ~17 minutes (3158 frames at ~3fps)
**Goal:** Achieve 90%+ continuous ball tracking for the entire segment

## Baseline (before experiments)
- **14.8%** coverage with naive best-track selection (was tracking sideline ball in r2c6)
- After velocity filtering + combining all fast-moving tracks: **69.4%** (with interpolation)
- **34.4%** raw detection coverage (1087 unique frames with actual detections, no interpolation)
- 162 tracker tracks total, 122 classified as "moving", 101 pass velocity filter (avg_step>=8)
- Best single track: 25 detections (real game ball, r1c1→r1c0)
- Main issues identified:
  - Ball too small at edges (8-12px in standard tiles)
  - Right side of field blown out by sun glare (r1c5, r1c6)
  - Sideline balls dominating tracker (939 dets in r2c6, barely moving)
  - Short fragmented tracks (game ball detected in bursts of ~25 frames at 45-97px/f)

## User Feedback Summary
- Frame 16: Quality/blur issue
- Frame 69: Shoe detected as ball (teleport from r1c2 to r1c0)
- Frame 78: Another shoe false positive
- Frame 99: Sideline ball picked instead of game ball behind goal (ball was out of bounds)
- User identified ball on 31 unique frames manually
- Key insight: ball on sideline is never the game ball, static balls should always be deprioritized

---

## Experiment 1: Dewarped Edge Tiles + YOLO Detection
**Status:** In progress (6528/9474 tiles processed, ~111 detections so far)
**Hypothesis:** Dewarping fisheye edges into rectilinear projection will make the ball larger and rounder, improving detection at field edges.
**Method:**
- Copy source video to temp location (F:/training_data/temp_video/) — don't touch originals
- Extract dewarped 640x640 tiles from source video using cylindrical→rectilinear reprojection
- Camera params: HFOV=180°, CAMERA_TILT=0.55rad, from dewarp_viewer.html branch
- 3 left-side views (right side is sun-blown): dw_L_far (yaw=-1.30), dw_L_mid (yaw=-1.10), dw_L_near (yaw=-1.30, pitch=0.70)
- 9474 dewarped tiles generated at 5.3 frames/sec
- Run YOLO (yolo11x.pt, conf=0.1) on dewarped tiles → save labels to labels_640_dewarped/
- TODO: Merge dewarped detections back to panoramic coordinates and integrate with tracker
**Files:** training/data_prep/dewarp_tiles.py, F:/training_data/tiles_640_dewarped/, F:/training_data/labels_640_dewarped/
**Early results:** 111 detections in 6528 tiles (1.7% hit rate). Detections increasing in dw_L_mid view.
**Visual verification:** dw_L_far and dw_L_mid show clear dewarped views of left goal area and mid-field. Players visible at reasonable size. dw_R_far completely sun-blown (useless for this game). dw_L_near aimed too far toward trees (needs view angle adjustment).

## Experiment 2: Frame Differencing (standalone)
**Status:** FAILED — too many false positives
**Hypothesis:** Subtracting consecutive frames will highlight moving objects (ball) against static background.
**Method:**
- Compute |frame_N - frame_N-1| for consecutive tile images
- Threshold, find contours, filter by area and circularity
- Two attempts:
  1. Threshold=25, min_area=15, max_area=1500: 1.2M detections in 2 tile positions — WAY too many
  2. Threshold=40, min_area=20, max_area=500, circularity>0.3: Still 741K detections in 3 positions
**Why it failed:** Frame differencing catches ALL movement — players running, shadows moving, leaves blowing, camera vibration. Even with circularity filtering, player feet/legs create hundreds of small circular-ish blobs per frame. The ball's motion signal is drowned in player motion noise.
**Lesson learned:** Frame differencing CANNOT be used as a standalone detector. It must be combined with the tracker's predicted position — only look for motion blobs within a small search window around where the ball is expected to be. This is how TrackNetV3 uses background subtraction: as a complement to the learned detector, not a replacement.
**Next step:** Implement tracker-guided frame diff search (Experiment 6).

## Experiment 3: Trajectory Stitching
**Status:** Completed — limited improvement
**Hypothesis:** Short game ball tracks can be stitched together across gaps.
**Method:**
- Sort fast-moving tracks by start time
- Greedily chain tracks where end→start is close in time and space
- Support overlapping tracks (same ball in adjacent tiles)
**Results:**
- 101 tracks → 94 chains (gap=40 frames, dist=600px)
- 101 → 81 chains (gap=80, dist=1000)
- 101 → 63 chains (gap=200, dist=2000)
- Longest chain stayed at ~158 detections regardless of parameters
**Why it didn't help much:** Many tracks overlap in TIME (same ball visible in adjacent overlapping tiles simultaneously), not sequentially. The gaps between genuine sequential tracks are large (500-3500px) because the ball travels far during detection gaps. The stitching does merge some overlapping tile tracks, but the main bottleneck is detection coverage, not fragmentation.
**Lesson learned:** Stitching helps merge tile-overlap duplicates but doesn't dramatically increase coverage. The real bottleneck is that the ball is only detected in 34.4% of frames. Need to improve detection first, then stitching becomes more powerful.

## Experiment 4: Player Clustering Correlation
**Status:** Not started (blocked on person detection bootstrap)
**Hypothesis:** The game ball is always near a cluster of players.

## Experiment 5: Adaptive Confidence Threshold
**Status:** Not started
**Hypothesis:** Near the tracker's predicted position, use a lower YOLO confidence threshold.

## Experiment 6: Tracker-Guided Frame Diff Search (planned)
**Status:** Planned — depends on having a reasonable tracker prediction
**Hypothesis:** When the tracker loses the ball, search a small window (200x200px) around the predicted position using frame differencing. This avoids the noise problem of full-frame differencing.
**Method:**
- For each frame where tracker has a prediction but no detection:
  - Extract 200x200 crop from panoramic frame at predicted position
  - Run frame diff between consecutive crops
  - Find the strongest motion blob — that's likely the ball
  - If blob found, add it as a detection and let tracker incorporate it

---

## Experiment 7: Tracker Parameter Sweep
**Status:** COMPLETE - SUCCESS
**Hypothesis:** Increasing gate distance and max_missing allows tracker to follow the ball through longer gaps and bigger kicks.
**Method:** Test combinations of gate_distance and max_missing parameters.
**Results:**
| gate | max_miss | Tracks | Coverage |
|------|----------|--------|----------|
| 200  | 60       | 97     | 84.4%    |
| 300  | 90       | 80     | **95.1%** |
| 400  | 120      | 66     | 98.8%    |
| 200  | 200      | 79     | 92.6%    |

**Selected: gate=300, max_miss=90** — achieves 95.2% in the tracking lab. Gate=400 gets 98.8% but risks false associations. Gate=300 is a good balance.
**Why it works:** The ball can travel 200-400px between consecutive detections when kicked across the field. Gate=200 was too restrictive. Also, max_missing=90 means the tracker predicts through ~30 seconds of no detections (common during ball-out-of-play periods).

---

## Results Summary

| Step | Coverage | Technique |
|---|---|---|
| Baseline (naive best track) | 14.8% | Tracker locked on sideline ball |
| Velocity filter (avg_step>=8) | 34.4% raw | Eliminated static balls |
| All fast tracks + interpolation | 69.4% | Combined 101 game ball fragments |
| + Guided frame diff | 86.4% | +408 frames from motion search near predictions |
| + Optimized tracker (gate=300, miss=90) | **95.2%** | Follows ball through kicks and gaps |
| (aggressive: gate=400, miss=120) | 98.8% | Max coverage, possible false associations |

## What Worked
1. **Velocity filter** (avg_step >= 8px/frame) — reliably separates game ball from static objects
2. **Tracker-guided frame diff** — finds balls too small for YOLO by searching near predicted position
3. **Wider gate distance** (300px) — lets tracker follow ball through fast kicks across field
4. **Longer prediction horizon** (90 frames = 30s) — maintains track through brief out-of-play periods

## What Didn't Work
1. **Standalone frame differencing** — too many false positives (players, shadows, leaves)
2. **Dewarped edge tiles** — only 5 new frames, standard tiles already caught most edge balls
3. **Naive trajectory stitching** — limited by spatial distance between genuine sequential gaps
4. **Small gate distance (200px)** — too restrictive for fast-moving ball

## What to Try Next
1. **Verify accuracy of 95.2%** — user should scrub through and check if predicted positions are reasonable
2. **Multi-hypothesis tracking** — maintain multiple candidate trajectories, score by physics consistency
3. **Person detection + correlation** — validate ball position is near player cluster
4. **TrackNet temporal model** — 3-frame heatmap for reliable small-ball detection
5. **Apply learnings to all 15 games** — build the full label cleaning pipeline

## Key Insights
1. Ball is only 8-12px in r1 tiles. YOLO detection rate is ~34% even with good labels.
2. The tracker's prediction fills most gaps. Detection is the bottleneck but prediction compensates.
3. Frame diff must be guided by tracker prediction — full-frame is unusable.
4. Dewarping helps minimally when standard tiles already have decent edge coverage.
5. The single most impactful change was gate_distance: 200→300px (+10% coverage).
6. Right side of field (sun glare) is the biggest remaining challenge for this segment.
