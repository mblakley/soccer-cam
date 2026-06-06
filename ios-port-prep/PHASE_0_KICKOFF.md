# Phase 0 kickoff — Mac-side experiment runbook

Mark, this is what you do when you sit down at the Mac with `ios-port-prep/`
synced over.

## Prerequisites on the Mac

1. Clone soccer-cam, check out `feat/ios-port-parity`.
2. Install Python deps in the dev venv (matches Windows):
   ```bash
   uv sync   # or: pip install -e ".[ml]"
   ```
3. Install Xcode 15.0+ (for iOS 17 target SDK).
4. Connect an iPhone 13 + iPhone 15 (Pro if possible) + iPad M2 for the
   on-device experiments. iPhone 15 is the must-have; iPhone 13 + iPad M2
   are needed for the device-matrix points in E0.A6 / E0.D1.

## Step 0 — Fetch the heavy artifacts

```bash
# Model sources (per ios-port-prep/models/README.md)
mkdir -p ios-port-prep/models/source
rsync -av //DESKTOP-5L867J8/video/test/best.pt           ios-port-prep/models/source/ball_detector.pt
rsync -av //DESKTOP-5L867J8/video/test/best.onnx         ios-port-prep/models/source/ball_detector_onnx_baseline.onnx
rsync -av //DESKTOP-5L867J8/video/test/onnx_models/decrypted/detect_kpts_fp16.onnx \
                                                         ios-port-prep/models/source/field_detector_onnx_baseline.onnx

# Test recordings (per ios-port-prep/golden/README.md)
mkdir -p ios-port-prep/golden/{full_segment,segment_pair_10min}
rsync -av //DESKTOP-5L867J8/.../reolink/SoccerCam-0-20250722180814-20250722181313.mp4 \
                                                         ios-port-prep/golden/full_segment/
rsync -av //DESKTOP-5L867J8/.../reolink/SoccerCam-0-20250722181314-20250722181814.mp4 \
                                                         ios-port-prep/golden/segment_pair_10min/
# Re-extract the short_clips locally via the same PyAV script — faster than
# rsyncing tiny mp4s over the WAN.
```

## Step 1 — Mac sanity (Phase 1 of the iOS port plan)

Run the parity harness on the Mac against `segment1_first30s.mp4` and
confirm byte-identical output vs `ios-port-prep/baselines/segment1_first30s/`:

```bash
python scripts/run_parity_harness.py \
    --input-video ios-port-prep/golden/short_clips/segment1_first30s.mp4 \
    --model ios-port-prep/models/source/ball_detector_onnx_baseline.onnx \
    --output-dir /tmp/parity_mac \
    --field-polygon ios-port-prep/golden/field_polygons/segment1_polygon.json \
    --frame-interval 4

diff <(cat /tmp/parity_mac/parity/detections.json) \
     <(cat ios-port-prep/baselines/segment1_first30s/parity/detections.json)
diff <(cat /tmp/parity_mac/parity/trajectory.json) \
     <(cat ios-port-prep/baselines/segment1_first30s/parity/trajectory.json)
```

If both diffs are empty → Mac reproduces Windows baselines; iOS port can
trust the checked-in baselines as the parity reference. If non-empty, fix
the Python-env divergence (different ONNX Runtime build, different OpenCV
build) before any Swift port can be parity-tested.

## Step 2 — CoreML export (Phase W.2 on the Mac)

```bash
python training/export_mobile.py \
    ios-port-prep/models/source/ball_detector.pt --formats coreml --imgsz 640
# → ball_detector.mlpackage (FP16 default)

# FP32 variant for the E0.A1 parity test
python -c "
from ultralytics import YOLO
YOLO('ios-port-prep/models/source/ball_detector.pt').export(
    format='coreml', imgsz=640, half=False
)
"
# → ball_detector_fp32.mlpackage  (rename + move into ios-port-prep/models/exported/)
```

For the field detector — if a `.pt` is found on Mark's training share,
repeat. Otherwise fall back to ONNX Runtime iOS for the field model (already
plan-baked as the E0.A5-fail fallback).

## Step 3 — Phase 0 experiment runbook

Each experiment compares iOS output against the checked-in baseline. Per-
experiment:

### Track A — Detection (consume `baselines/segment1_first30s/parity/detections.json`)

| Exp | What | Mac action |
|-----|------|-----------|
| E0.A1 | CoreML accuracy parity | Run `BallDetector.detect` on the 30s clip with both FP16 and FP32 CoreML; compare to baseline JSON. Pass if ≥99% FP32 match, ≥97% FP16. |
| E0.A2 | Hard-case detection parity | Same against `golden/hard_cases/` (Mark labels these first). |
| E0.A3 | Confidence calibration | Plot histograms; document any shift in detector config. |
| E0.A4 | Tile-stitching parity | Run full tiled-inference pipeline on the 30s clip; assert aggregated detections match baseline. |
| E0.A5 | Field-keypoint parity | Same but for the field detector against `field_polygons/segment1_polygon.json`. |
| E0.A6 | Tile latency | Time one 640×640 tile inference on iPhone 13 / 15 / iPad M2. Pass if <8 ms on iPhone 15. |

### Track B — Renderer (consume `baselines/segment1_first30s/parity/leveled_pano_*.npy`, `camera_states.json`, render frames)

| Exp | What | Mac action |
|-----|------|-----------|
| E0.B1 | Projection math parity | `CylindricalView.cylindricalRemap` vs `leveled_pano_map_x.npy` / `_map_y.npy`. Pass if <0.01 px diff. |
| E0.B2 | Leveled-pano build parity | `CylindricalView.buildLeveledPano` vs the .npy. Pass if <1 LSB mean diff. |
| E0.B3 | Metal warp vs cv2.remap fidelity | Run WarpKernel.metal on the same map + source; compare to `render_frame_000000.png` baseline. |
| E0.B4 | Camera state machine determinism | Feed synthetic 9000-frame trajectory through both Python + Swift; diff `camera_states.json`. |
| E0.B5 | Metal warp throughput | Time 1920×1080 warp on iPhone 13 / 15 / iPad M2. Pass if ≥60 fps on iPhone 13. |
| E0.B6 | Colorspace round-trip | Sample 10 frames from VideoToolbox output mp4; compare colors to baseline `render_frame_*.png`. |
| E0.B7 | Audio passthrough | Concat 3 rendered segments; check for boundary glitches. |

### Track C — End-to-end (uses `golden/full_segment/` + `golden/segment_pair_10min/`)

| Exp | What | Mac action |
|-----|------|-----------|
| E0.C1 | Real end-to-end | Run full iOS pipeline on the 5-min segment; visual side-by-side vs PC output. |
| E0.C2 | Segment-boundary continuity | Two halves with carry-over vs one continuous run; diff at boundary. |
| E0.C3 | Wall-clock budget | Time E0.C1 on iPhone 15; pass if <4 min. iPhone 13 fallback: <5 min. |

### Track D — Infrastructure

| Exp | What | Mac action |
|-----|------|-----------|
| E0.D1 | AVAssetReader decode throughput | Decode the 5-min segment; pass if ≥30 fps sustained. |
| E0.D2 | Reolink Wi-Fi pull rate | Measured at the actual field venue (not at home). Pass if ≥5 MB/s sustained. |
| E0.D3 | Storage ceiling | Run 30-min streaming test; pass if peak <1.5 GB. |

## Gates before Phase 1+ (Swift port work)

All Track A + Track B experiments must pass before fleshing out
`Pipeline/*` stubs. Detection + renderer fidelity are the load-bearing
assumptions of the entire port.

E0.C1 is the make-or-break visual check. If A + B pass numerically but
E0.C1 looks bad, the iOS port has an integration bug — trace before
proceeding.

## Where Mac-side work goes

- Phase 0 experiment scripts → `experiments/` in `soccer-cam-ios` repo
  (new repo; OSS per [[feedback_client_apps_oss]])
- Phase 0 results memo → `docs/phase_0_results.md` in `soccer-cam-ios`
- Phase 1+ Swift port → `soccer-cam-ios/SoccerCamIOS/` (drop in the
  skeleton files from `ios-port-prep/sources/`)
