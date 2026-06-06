# Golden test data — Phase W.3 staging

Real Reolink panorama recordings curated for the iOS-port parity gates +
Phase 0 experiments. Source recordings are **gitignored** (44–467 MB each)
— the README points at where they live on Mark's machine so the Mac handoff
can fetch them via SMB or AirDrop.

## Source recording

The 2025-07-22 Hilton Heat vs Fairport game lives at
`/c/Users/markb/Downloads/reolink/` — a chronological set of 5 Reolink
segments (~5 min each, ~250–470 MB each):

```
SoccerCam-0-20250722180814-20250722181313.mp4   # segment 1
SoccerCam-0-20250722181314-20250722181814.mp4   # segment 2
SoccerCam-0-20250722181815-20250722182315.mp4   # segment 3
SoccerCam-0-20250722182315-20250722182814.mp4   # segment 4
SoccerCam-0-20250722182815-20250722183315.mp4   # segment 5 (last, partial)
```

Format: 7680×2160 HEVC @ ~19.5 fps. The ball detector internally
downscales to 4096×1800 (its training resolution) per
`ball_detector.py:96-101` — so larger source is OK.

## Extracted clips (gitignored)

```
short_clips/
├── segment1_first30s.mp4         # 30s from segment 1 start (44 MB)
├── segment3_first30s.mp4         # 30s from segment 3 start (44 MB)
└── segment5_first30s.mp4         # 30s from segment 5 start (44 MB)
```

These are stream-copy extractions (no re-encode), keeping the original
HEVC codec + audio passthrough. Sufficient for end-to-end parity-harness
runs against the W.4 baseline ONNX model.

**Mark should label them:** at handoff time, walk through each clip and
annotate which contains midfield play / corner kick / ball-loss /
dead-ball scenarios — that drives the per-clip pass criteria for the
Phase 5 visual parity gates.

## Hard-case frames

```
hard_cases/
└── (not yet extracted — needs human labeling)
```

E0.A2 needs ~30 frames covering ball-against-sky, ball-on-chalk-line,
distant-ball-<8px, motion-blur, partial-occlusion. These need human
visual identification — defer to Mark or to a separate labeling pass on
the source recordings. The directory exists as a placeholder.

## Field polygons

```
field_polygons/
└── segment1_polygon.json         # 10-vertex polygon, src 7680×2160
```

Pulled from `\\DESKTOP-5L867J8\video\test\onnx_models\compare\field_polygon.json`.
The polygon was produced by Mark's field-keypoint detector on this exact
camera setup. Same polygon applies to all five segments (camera doesn't
move during the game).

`field_polygon.json` schema:

```json
{
  "keypoints": null,                 // optional raw keypoint output
  "polygon": [[x, y], ...],          // N-vertex polygon, source pixel coords
  "homography": null,                // optional 3×3 image→field-plane matrix
  "src_w": 7680,
  "src_h": 2160
}
```

## E0.C1 + E0.C2 source recordings

E0.C1 (real end-to-end render) uses one full 5-min segment from the
above set. E0.C2 (segment-boundary continuity) uses two consecutive
segments processed with carry-over, compared against the same 10-min
range processed as one virtual file.

Both reference the source `.mp4`s directly — no extraction needed.

## Mac handoff sync

```bash
# from a Mac with SMB access:
mkdir -p ios-port-prep/golden/{short_clips,full_segment,segment_pair_10min}
rsync -av /c/Users/markb/Downloads/reolink/SoccerCam-0-20250722180814-20250722181313.mp4 \
          ios-port-prep/golden/full_segment/
rsync -av /c/Users/markb/Downloads/reolink/SoccerCam-0-20250722180814-20250722181313.mp4 \
          /c/Users/markb/Downloads/reolink/SoccerCam-0-20250722181314-20250722181814.mp4 \
          ios-port-prep/golden/segment_pair_10min/
# short_clips/ can be re-extracted on Mac with the same PyAV script (faster
# than rsync'ing the 44 MB extractions over LAN).
```
