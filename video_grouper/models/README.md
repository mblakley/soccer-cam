# Bundled models

## `person.onnx` — YOLO person detector (phase detection)

The game-phase detector's player-on-field curve
(`video_grouper/inference/phase_detector.py`) counts persons inside the field
polygon. `person.onnx` is that person detector, shipped with the install so
phase detection works out of the box (the detector resolves it via
`resolve_person_model()` — explicit path, then `YOLO_PERSON_MODEL`, then this
bundled file).

- **Model:** stock Ultralytics **YOLO26n** (COCO, nano), person class.
- **Export:** `yolo26n.pt` → ONNX, `imgsz=1280`, `nms=True` (NMS embedded).
- **I/O:** input `images [1,3,1280,1280]` float; output `output0 [1,300,6]`
  float, rows `[x1, y1, x2, y2, conf, cls]` (post-NMS Ultralytics format).
- **Size / hash:** 10,446,277 bytes,
  SHA256 `c97bed9ce96ca34d43ae097c6c1594ff36f3b10da5d26b3a8a9b1df6372397df`.
- **Storage:** committed via **Git LFS** (see the repo `.gitattributes`), so the
  exact validated bytes stay under our control regardless of upstream.

### License — AGPL-3.0

Ultralytics YOLO (and its pretrained weights) is licensed under **AGPL-3.0**.
soccer-cam is GPL-3.0, which is compatible; the full license text ships
alongside this model as [`LICENSE-AGPL-3.0.txt`](./LICENSE-AGPL-3.0.txt) and is
indexed in the repo-root [`THIRD_PARTY_LICENSES.md`](../../THIRD_PARTY_LICENSES.md).
Keep these weights on the soccer-cam (open-source) side only — never bundle them
into a closed/proprietary product.

Upstream: https://github.com/ultralytics/ultralytics

### Regenerating (should the file ever be lost)

The bundled file is the authoritative, validated copy. It is a stock export and
can be reproduced from the public Ultralytics weights:

```python
from ultralytics import YOLO  # ultralytics>=8.4.27, the [ml] extra
YOLO("yolo26n.pt").export(format="onnx", imgsz=1280, nms=True)
# rename the resulting yolo26n.onnx -> video_grouper/models/person.onnx
```

A re-export should match the I/O signature above; confirm before replacing the
committed file (the detector was validated against these exact bytes).
