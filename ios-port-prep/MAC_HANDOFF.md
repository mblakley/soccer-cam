# Mac handoff — soccer-cam → iOS port

Paste this into a fresh Claude Code session on the Mac, or just follow the
commands by hand. Picks up the iOS port at the boundary between Phase W
(Windows pre-work) and Phase 0 (Mac feasibility experiments).

## Context — one paragraph

We're porting soccer-cam's ball detection + cylindrical renderer to iOS
so camera-managers without a PC can run it on iPhone/iPad. Plan lives at
`.claude/plans/based-on-the-code-vectorized-chipmunk.md` in
`team-tech-tools` (sync that file to the Mac too). Architecture: OSS Swift
app, CoreML for inference, Metal for the warp, streaming-segment processing
(~5 min segments with carry-over state). Phase W ran on Windows; this
session finishes on the Mac with Phase 0 experiments + Phases 1–7 build-out.

## What's done — soccer-cam branch `feat/ios-port-parity`

Branched off `feat/broadcast-camera-render`. 7 commits ahead. All gates
green (55 pytest pass + ruff). Pull on the Mac:

```bash
cd <your soccer-cam clone>
git fetch origin
git checkout feat/ios-port-parity
git log --oneline feat/broadcast-camera-render..HEAD
```

Expected commits (top to bottom = newest to oldest):

| Commit | What |
|--------|------|
| b9641b1 | NumPy ref of Metal warp kernel + crop_box normalization gap |
| ff6a983 | render fixes (seed_artifacts + null-keypoints); W.4 baselines complete |
| 365d112 | W.4 partial: detect + track baselines for segment1_first30s |
| 53faf89 | W.2 + W.3 + W.7 staging + Mac-handoff package |
| 6933658 | W.6: skeleton Swift + Metal sources |
| cd8e68a | W.5: 12 iOS design docs |
| 87acf20 | W.1: parity harness (deterministic detect/track/render) |

## What's where

```
ios-port-prep/
├── README.md                 # package map
├── PHASE_0_KICKOFF.md        # Phase 0 runbook — Mac starts here
├── MAC_HANDOFF.md            # this file
├── design/                   # 12 iOS port design docs
├── sources/                  # skeleton Swift + Metal (drop into Xcode)
├── models/README.md          # source paths + CoreML export commands
├── golden/                   # test data (.mp4s gitignored; pointers in README)
└── baselines/segment1_first30s/parity/
    ├── detections.json       # 71 ball detections from production ONNX
    ├── trajectory.json       # 565-frame Kalman output
    ├── camera_states.json    # 593-frame state machine output
    ├── render_frame_*.png    # 20 sampled render frames (1/sec)
    └── leveled_pano_map_*.png  # visualization PNGs
```

## Bring the heavy binaries from the Windows machine

These are `gitignore`d because they're too large. Mark needs to rsync /
AirDrop them onto the Mac from his Windows machine (they live under his
home + on the DESKTOP-5L867J8 SMB share):

```bash
# From the Windows machine's perspective these are the source paths:
#   //DESKTOP-5L867J8/video/test/best.pt                                          (158 MB)
#   //DESKTOP-5L867J8/video/test/best.onnx                                        (101 MB)
#   //DESKTOP-5L867J8/video/test/onnx_models/decrypted/detect_kpts_fp16.onnx
#   //DESKTOP-5L867J8/video/test/onnx_models/decrypted/balldet_fp16.onnx          (FP16 ball detector — for E0.A1 proxy)
#   C:/Users/markb/Downloads/reolink/SoccerCam-0-20250722*.mp4                    (5× ~250-470 MB segments)
#   ios-port-prep/baselines/segment1_first30s/source.mp4                          (44 MB)

# Mac-side staging:
mkdir -p ios-port-prep/models/source
mkdir -p ios-port-prep/golden/{full_segment,segment_pair_10min,short_clips}
mkdir -p ios-port-prep/baselines/segment1_first30s

# Sync the four model files into ios-port-prep/models/source/ (rename for OSS-neutrality):
#   best.pt              -> ball_detector.pt
#   best.onnx            -> ball_detector_onnx_baseline.onnx
#   balldet_fp16.onnx    -> ball_detector_fp16_onnx_baseline.onnx
#   detect_kpts_fp16.onnx -> field_detector_onnx_baseline.onnx

# Sync the 5 Reolink segments into ios-port-prep/golden/full_segment/ (segment 1)
# and segment_pair_10min/ (segments 1+2 for E0.C2).

# Re-extract the 30s clips on the Mac (faster than syncing) using PyAV:
python -c "
import av, os
src = 'ios-port-prep/golden/full_segment/SoccerCam-0-20250722180814-20250722181313.mp4'
dst = 'ios-port-prep/golden/short_clips/segment1_first30s.mp4'
ic = av.open(src); oc = av.open(dst, mode='w')
in_v = ic.streams.video[0]; in_a = next((s for s in ic.streams if s.type=='audio'), None)
out_v = oc.add_stream_from_template(in_v)
out_a = oc.add_stream_from_template(in_a) if in_a else None
max_pts = int(30.0 / float(in_v.time_base))
streams = (in_v,) + ((in_a,) if in_a else ())
for pkt in ic.demux(streams):
    if pkt.dts is None: continue
    if pkt.stream is in_v:
        if pkt.pts is not None and pkt.pts > max_pts: break
        pkt.stream = out_v
    elif in_a is not None and pkt.stream is in_a:
        if pkt.pts is not None and pkt.pts * float(in_a.time_base) > 30.0: continue
        pkt.stream = out_a
    oc.mux(pkt)
oc.close(); ic.close()
print('done:', os.path.getsize(dst)/1024/1024, 'MB')
"

# Also hardlink the extracted clip into the baselines dir for the Mac sanity check:
ln ios-port-prep/golden/short_clips/segment1_first30s.mp4 \
   ios-port-prep/baselines/segment1_first30s/source.mp4
```

## First Mac-side checks (do these in order)

### 1. Confirm the Python env reproduces the Windows baselines

If this passes, the iOS port can trust the checked-in baselines as the
parity reference. If it fails, the Mac Python env diverges (different
ONNX Runtime or OpenCV build) and that divergence must be reconciled
before any Swift port can be parity-tested.

```bash
# Re-run the parity harness against the same clip + ONNX model.
python scripts/run_parity_harness.py \
    --input-video ios-port-prep/golden/short_clips/segment1_first30s.mp4 \
    --model ios-port-prep/models/source/ball_detector_onnx_baseline.onnx \
    --output-dir /tmp/parity_mac \
    --field-polygon ios-port-prep/golden/field_polygons/segment1_polygon.json \
    --frame-interval 4

# Detect on CPU was 4h on Mark's Windows laptop. Mac will be similar.
# If on macOS with CoreML EP available, you can pass --device cuda:0
# (it falls back through CUDA→CPU; the deterministic mode forces CPU
# regardless when dump_intermediates is set).

# Diff against the committed baselines:
diff /tmp/parity_mac/parity/detections.json \
     ios-port-prep/baselines/segment1_first30s/parity/detections.json
diff /tmp/parity_mac/parity/trajectory.json \
     ios-port-prep/baselines/segment1_first30s/parity/trajectory.json
diff /tmp/parity_mac/parity/camera_states.json \
     ios-port-prep/baselines/segment1_first30s/parity/camera_states.json
```

Detections + trajectory + camera_states should be **byte-identical**
(the harness uses sort_keys + CPU EP + sequential single-thread ONNX
specifically for this).

### 2. CoreML export (Phase W.2 on the Mac)

```bash
pip install -e ".[ml]"   # ultralytics + coremltools + torch
python training/export_mobile.py \
    ios-port-prep/models/source/ball_detector.pt --formats coreml --imgsz 640

# FP32 variant for E0.A1:
python -c "
from ultralytics import YOLO
YOLO('ios-port-prep/models/source/ball_detector.pt').export(
    format='coreml', imgsz=640, half=False
)
"
mv ios-port-prep/models/source/ball_detector.mlpackage \
   ios-port-prep/models/exported/ball_detector_fp16.mlpackage
mv <wherever the fp32 went> \
   ios-port-prep/models/exported/ball_detector_fp32.mlpackage
```

### 3. Phase 0 experiments (see `PHASE_0_KICKOFF.md`)

Run the full Phase 0 runbook there. It maps every Phase 0 experiment
(E0.A1–A6, E0.B1–B7, E0.C1–C3, E0.D1–D3) to a specific checked-in
baseline file or measurement target.

### 4. Bootstrap the soccer-cam-ios repo

```bash
# Create the new OSS repo (per [[feedback_client_apps_oss]] + [[project_oss_split]])
mkdir -p ~/projects/soccer-cam-ios
cd ~/projects/soccer-cam-ios
git init
# Drop the design + sources into the new repo:
mkdir -p docs SoccerCamIOS Tests/PipelineTests
cp -r <soccer-cam>/ios-port-prep/design/* docs/
cp -r <soccer-cam>/ios-port-prep/sources/App/* SoccerCamIOS/App/
cp -r <soccer-cam>/ios-port-prep/sources/Domain/* SoccerCamIOS/Domain/
# ...etc per the layout in sources/README.md.
# Then: xcodebuild init Xcode project pointing at SoccerCamIOS/
```

## Open followups (Windows-side discoveries to fix on Mac OR back-port)

### Soccer-cam bug: OpenCL warp backend has the same crop_box bug

The cv2.remap-via-cv2.resize production path silently tolerates negative
`cw`/`ch` from `crop_box()` (Python slice semantics), but `opencl_warp.py`
uses the raw values literally in its kernel — same failure mode as the
Metal kernel we caught in W.6. Hasn't been exercised in production because
nobody's run `render_backend="opencl"` on a camera config with this much
mount tilt (~20°+). Fix is one of:

- normalize in `_make_warper` host wrapper (mirrors the Metal spec
  recommendation, see `ios-port-prep/design/metal_warp_shader.md`)
- fix `crop_box()` itself to always return positive dims and update
  consumers accordingly

Either way, ship the fix back to `feat/broadcast-camera-render` before
that branch merges to main.

### FP16 detection parity (E0.A1 proxy on Windows)

A background FP16 parity-harness run was in flight when this handoff was
written:

- Source: `ios-port-prep/golden/short_clips/segment1_first30s.mp4`
- Model: `balldet_fp16.onnx`
- Output dir: `ios-port-prep/baselines/segment1_first30s_fp16/`

It was started **before** the render-step fixes landed (b9641b1 + ff6a983),
so render will crash the same way the first FP32 run did. Detect + track
baselines will persist on disk. To complete:

```bash
# Once the background run finishes (will crash at render — that's OK):
python scripts/run_parity_harness.py \
    --input-video ios-port-prep/golden/short_clips/segment1_first30s.mp4 \
    --model ios-port-prep/models/source/ball_detector_fp16_onnx_baseline.onnx \
    --output-dir ios-port-prep/baselines/segment1_first30s_fp16 \
    --field-polygon ios-port-prep/golden/field_polygons/segment1_polygon.json \
    --frame-interval 4
# (the manifest's resume logic skips cached detect+track; only render runs.)

# Then diff the FP16 detections vs FP32 baseline:
python -c "
import json, sys
fp32 = json.load(open('ios-port-prep/baselines/segment1_first30s/parity/detections.json'))
fp16 = json.load(open('ios-port-prep/baselines/segment1_first30s_fp16/parity/detections.json'))
# pair by frame_idx, IoU>0.5 match
def by_frame(d): from collections import defaultdict; b = defaultdict(list); [b[x['frame_idx']].append(x) for x in d]; return b
b32, b16 = by_frame(fp32), by_frame(fp16)
matches = misses = false_pos = 0
for f, dets in b32.items():
    for d in dets:
        if any(abs(o['cx']-d['cx'])<10 and abs(o['cy']-d['cy'])<10 for o in b16.get(f, [])):
            matches += 1
        else:
            misses += 1
for f, dets in b16.items():
    for d in dets:
        if not any(abs(o['cx']-d['cx'])<10 and abs(o['cy']-d['cy'])<10 for o in b32.get(f, [])):
            false_pos += 1
print(f'FP32 dets: {sum(len(v) for v in b32.values())}; FP16 matches: {matches}; FP32-only misses: {misses}; FP16-only false pos: {false_pos}')
print(f'match rate: {matches/(matches+misses)*100:.1f}% (E0.A1 threshold: 97% for FP16)')
"
```

This is the closest Windows-can-do proxy for E0.A1 (CoreML FP16 accuracy
regression). If FP16 ONNX drops below 97%, CoreML FP16 almost certainly
will too — ship FP32 by default and document the FP16 fallback as a "Plan
B if E0.A6 latency requires it" route.

### Field-detector .pt not located

Only `detect_kpts_fp16.onnx` (an already-exported ONNX) was found on
DESKTOP-5L867J8. For CoreML export of the field detector, either:

- Mark locates the `.pt` source on his training machine
- OR ship the field detector via ONNX Runtime iOS (the E0.A5-fail
  fallback already baked into the plan)

### Hard-case frames need human labeling

`ios-port-prep/golden/hard_cases/` exists but is empty. E0.A2 needs ~30
frames covering ball-against-sky, ball-on-line, distant-ball-<8px, motion
blur, partial occlusion. Either Mark walks through the 5 Reolink segments
and picks them, or a separate labeling pass on `golden/full_segment/`.

## Memory references this work touched / created

| Memory | Why it matters here |
|--------|---------------------|
| [[reference_mac_for_apple_dev]] | The Windows→Mac handoff pattern; this doc IS the handoff |
| [[feedback_client_apps_oss]] | soccer-cam-ios = OSS, free, premium gating server-side |
| [[project_oss_split]] | Same — soccer-cam family stays OSS even on iOS |
| [[reference_t_drive]] | `\\DESKTOP-5L867J8\video\test\` is the training share |
| [[reference_desktop5l_credential]] | Saved PowerShell creds at `%LOCALAPPDATA%\credentials\desktop5l-training.xml` |
| [[feedback_no_decrypted_onnx_in_oss]] | Generic placeholder names in OSS; never enumerate model types |
| [[feedback_no_security_docs_in_oss]] | Decryption mechanics OK in OSS; threat models stay in TTT |
| [[feedback_phased_work_single_branch]] | One branch (`feat/ios-port-parity`), one commit per W.x sub-deliverable |
| [[feedback_branch_off_active_bridge]] | Branched off `feat/broadcast-camera-render`, not main |
| [[feedback_comprehensive_verification]] | Each phase ships with lint + tests + manual + negative paths |
| [[feedback_use_vision_for_frame_verification]] | Save PNGs and Read them; the W.4 sample frames are this pattern |

## The plan file

Read `.claude/plans/based-on-the-code-vectorized-chipmunk.md` first — it
has the full 7-phase iOS port plan with effort estimates, gating
experiments, and per-phase verification criteria. Sync that file to the
Mac (it's not in the soccer-cam repo; it's in `~/.claude/plans/`).
