# Models — Phase W.2 staging

CoreML `.mlpackage` export requires macOS (coremltools' newer pipeline is
macOS-only). Windows can run the ONNX side but can't produce `.mlpackage`,
so W.2 stages source weights here and the Mac session runs the export.

## Source weights (NOT committed — too large for git)

On Mark's training share (`\\DESKTOP-5L867J8\video\test\`):

| Role | Source path | Size |
|------|-------------|------|
| Ball detector (PyTorch) | `\\DESKTOP-5L867J8\video\test\best.pt` | ~158 MB |
| Ball detector (ONNX baseline) | `\\DESKTOP-5L867J8\video\test\best.onnx` | ~101 MB |
| Field keypoint detector (ONNX baseline) | `\\DESKTOP-5L867J8\video\test\onnx_models\decrypted\detect_kpts_fp16.onnx` | (size unknown) |

The field-detector `.pt` source is not at the above share root — Mark should
verify whether a `.pt` exists elsewhere on DESKTOP-5L867J8 before Mac
handoff. If only the ONNX survives, the iOS port can either:

1. Use the ONNX via ONNX Runtime iOS for the field detector (no CoreML
   conversion needed; the runtime is on iOS officially)
2. Re-train + export the field detector before Phase 3 starts

## Sync to Mac at handoff time

```bash
# from a Mac with the soccer-cam repo cloned and SMB access to DESKTOP-5L867J8:
mkdir -p ios-port-prep/models/source
rsync -av --progress \\DESKTOP-5L867J8/video/test/best.pt          ios-port-prep/models/source/ball_detector.pt
rsync -av --progress \\DESKTOP-5L867J8/video/test/best.onnx        ios-port-prep/models/source/ball_detector_onnx_baseline.onnx
rsync -av --progress \\DESKTOP-5L867J8/video/test/onnx_models/decrypted/detect_kpts_fp16.onnx \
                                                                   ios-port-prep/models/source/field_detector_onnx_baseline.onnx
```

## Mac-side CoreML export

Soccer-cam already ships `training/export_mobile.py` (uses Ultralytics).
Run it against the staged `.pt`:

```bash
cd <soccer-cam clone>
python -m pip install -e ".[ml]"           # pulls ultralytics, coremltools, torch
python training/export_mobile.py ios-port-prep/models/source/ball_detector.pt \
    --formats coreml --imgsz 640
# Outputs: ios-port-prep/models/source/ball_detector.mlpackage
```

Default Ultralytics CoreML export emits FP16. To also produce an FP32
variant for the E0.A1 parity test (per Phase 0 plan), patch
`training/export_mobile.py` to call `model.export(format="coreml",
imgsz=640, half=False)` and re-run; rename the output to
`ball_detector_fp32.mlpackage`.

For the field detector, repeat with the field `.pt` if one is found.
Otherwise document the ONNX Runtime iOS fallback in the iOS port's design
docs (already noted as the E0.A5-fail fallback).

## Final layout (after Mac export)

```
ios-port-prep/models/
├── README.md                                              # this file
├── source/                                                # gitignored — too large
│   ├── ball_detector.pt
│   ├── ball_detector_onnx_baseline.onnx
│   └── field_detector_onnx_baseline.onnx
└── exported/                                              # gitignored — too large
    ├── ball_detector_fp32.mlpackage
    ├── ball_detector_fp16.mlpackage
    └── field_detector_fp16.mlpackage                      # if .pt was found
```
