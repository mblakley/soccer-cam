# ONNX Models Guide

Decrypted ONNX models from the ONCE SPORT Autocam app, located at `C:\onnx_models\decrypted\`.

## Models Overview

| Model | Size | Input | Precision | Speed (CPU) |
|---|---|---|---|---|
| `balldet_fp16.onnx` | 70.8 MB | RGB image, dynamic HxW | FP16 weights, FP32 I/O | ~2500ms @ 1280x576 |
| `balldet_int8.onnx` | 53.4 MB | RGB image, dynamic HxW | INT8 quantized | ~1100ms @ 1280x576 |
| `detect_kpts_fp16.onnx` | 26.3 MB | RGB image, 384x768 fixed | FP16 I/O | ~290ms |
| `detect_audio_int8.onnx` | 8.6 MB | Mono waveform, variable length | INT8 quantized | ~44ms for 5s audio |

All models run via `onnxruntime` on CPU. GPU acceleration available with `onnxruntime-gpu` (CUDA/DirectML).

## Requirements

```bash
uv add onnxruntime opencv-python numpy
# or for GPU:
uv add onnxruntime-gpu
```

---

## 1. Ball Detection (`balldet_*.onnx`)

YOLO-based segmentation model. Single class: `ball`. Outputs bounding boxes + instance segmentation masks.

### Input

- **Name:** `images`
- **Shape:** `[batch, 3, height, width]` (dynamic H/W)
- **Format:** RGB, float32, normalized to `[0, 1]`
- **Recommended size:** 1280x576 (native panoramic aspect ratio) or letterboxed to square

Higher resolution = better accuracy. At 640x640 the model barely finds anything; at 1280 it hits 0.76+ confidence.

### Outputs

| Output | Shape | Description |
|---|---|---|
| `outputs` | `[batch, N, 6]` | Detections: `[cx, cy, w, h, 1.0, confidence]` |
| `538` | `[batch, 32, H/4, W/4]` | Segmentation prototype masks |
| `771` | `[batch, N, 33]` | `[unused, 32 mask coefficients]` per detection |

- `cx, cy, w, h` are in pixel coordinates relative to input image size
- Column 4 is always `1.0` (single-class indicator, ignore it)
- Column 5 is the detection confidence

### Usage

```python
import cv2
import numpy as np
import onnxruntime as ort

MODEL_PATH = r"C:\onnx_models\decrypted\balldet_int8.onnx"
CONF_THRESHOLD = 0.25
NMS_IOU_THRESHOLD = 0.5

sess = ort.InferenceSession(MODEL_PATH)

def detect_balls(frame_bgr, input_w=1280, input_h=576):
    """Detect balls in a BGR frame. Returns list of (cx, cy, w, h, conf) in original coords."""
    orig_h, orig_w = frame_bgr.shape[:2]
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    resized = cv2.resize(rgb, (input_w, input_h))
    blob = (resized.astype(np.float32) / 255.0).transpose(2, 0, 1)[np.newaxis]

    outputs = sess.run(None, {"images": blob})
    det = outputs[0][0]  # (N, 6)

    # Filter by confidence
    mask = det[:, 5] > CONF_THRESHOLD
    filtered = det[mask]
    if len(filtered) == 0:
        return []

    # Convert cx,cy,w,h to x1,y1,x2,y2 for NMS
    boxes = np.zeros((len(filtered), 4))
    boxes[:, 0] = filtered[:, 0] - filtered[:, 2] / 2
    boxes[:, 1] = filtered[:, 1] - filtered[:, 3] / 2
    boxes[:, 2] = filtered[:, 0] + filtered[:, 2] / 2
    boxes[:, 3] = filtered[:, 1] + filtered[:, 3] / 2

    indices = cv2.dnn.NMSBoxes(
        boxes.tolist(), filtered[:, 5].tolist(),
        CONF_THRESHOLD, NMS_IOU_THRESHOLD
    )

    results = []
    scale_x = orig_w / input_w
    scale_y = orig_h / input_h
    for i in indices:
        row = filtered[i]
        results.append((
            row[0] * scale_x,  # cx in original coords
            row[1] * scale_y,  # cy
            row[2] * scale_x,  # w
            row[3] * scale_y,  # h
            row[5],            # confidence
        ))
    return results
```

### Segmentation Masks

For pixel-precise ball masks (useful for sub-pixel center estimation):

```python
def get_ball_mask(outputs, det_index, input_h, input_w):
    """Generate a segmentation mask for a specific detection."""
    protos = outputs[1][0]    # (32, H/4, W/4)
    coeffs = outputs[2][0]    # (N, 33)
    det = outputs[0][0]       # (N, 6)

    # Mask = sigmoid(coefficients @ prototypes)
    mask_coeffs = coeffs[det_index, 1:]  # 32 mask coefficients (skip col 0)
    mask = np.einsum("c,chw->hw", mask_coeffs, protos)
    mask = 1.0 / (1.0 + np.exp(-mask))  # sigmoid

    # Crop to bounding box region
    cx, cy, w, h = det[det_index, :4]
    proto_scale_x = protos.shape[2] / input_w
    proto_scale_y = protos.shape[1] / input_h
    x1 = max(0, int((cx - w / 2) * proto_scale_x))
    y1 = max(0, int((cy - h / 2) * proto_scale_y))
    x2 = min(protos.shape[2], int((cx + w / 2) * proto_scale_x) + 1)
    y2 = min(protos.shape[1], int((cy + h / 2) * proto_scale_y) + 1)

    # Resize cropped mask to original bbox size
    roi = mask[y1:y2, x1:x2]
    return roi, (x1, y1, x2, y2)
```

### Performance Notes

- **INT8 is ~2.3x faster** than FP16 on CPU with comparable accuracy. Use INT8 for pipeline processing.
- Inference at 1280x576 produces ~15k detection anchors. NMS reduces this to 1-3 actual detections.
- For real-time use, process every Nth frame or use ROI crops (the existing `BallDetector` in `video_grouper/ball_tracking/detector.py` does this).

---

## 2. Field Keypoints (`detect_kpts_fp16.onnx`)

Detects 10 keypoints on the soccer field boundary. Useful for field homography, play-area detection, and camera calibration.

### Input

- **Name:** `input`
- **Shape:** `[1, 3, 384, 768]` (fixed, not dynamic)
- **Format:** RGB, float16, normalized to `[0, 1]`

### Outputs

| Output | Shape | Description |
|---|---|---|
| `keypoints` | `[1, 10, 2]` | `[x, y]` pixel coordinates (in 768x384 input space) |
| `scores` | `[1, 10]` | Per-keypoint confidence (0-1) |

### Keypoint Layout

On a panoramic frame (camera looking at the field from the side):

```
         9---8---7---6---5       <- far sideline (top of image)
        /                 \
       /                   \
      0---1---2---3---4          <- near sideline (bottom of image)
```

- Keypoints 0-4: near field boundary (left to right, lower in image)
- Keypoints 5-9: far field boundary (right to left, upper in image)

### Usage

```python
import cv2
import numpy as np
import onnxruntime as ort

sess = ort.InferenceSession(r"C:\onnx_models\decrypted\detect_kpts_fp16.onnx")

def detect_field_keypoints(frame_bgr, score_threshold=0.5):
    """Returns list of (x, y, score) in original image coordinates."""
    orig_h, orig_w = frame_bgr.shape[:2]
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    resized = cv2.resize(rgb, (768, 384))
    blob = (resized.astype(np.float16) / 255.0).transpose(2, 0, 1)[np.newaxis]

    kpts, scores = sess.run(None, {"input": blob})
    kpts = kpts[0]      # (10, 2)
    scores = scores[0]  # (10,)

    results = []
    for i in range(10):
        if scores[i] >= score_threshold:
            results.append((
                float(kpts[i, 0]) * (orig_w / 768),  # scale to original
                float(kpts[i, 1]) * (orig_h / 384),
                float(scores[i]),
            ))
    return results
```

### Applications

- **Play region detection:** fit a polygon to the 10 keypoints to define the field boundary, then filter ball/player detections to only those inside the field.
- **Homography:** map the detected keypoints to known field coordinates (FIFA standard dimensions) to get a perspective transform. This enables mapping pixel positions to real-world field coordinates.
- **Camera calibration:** if the camera position is known, use keypoint correspondences to compute intrinsic/extrinsic parameters.

---

## 3. Audio Detection (`detect_audio_int8.onnx`)

Binary audio classifier. Likely designed to detect game-relevant audio events (whistle, crowd, etc.).

### Input

- **Name:** `waveform`
- **Shape:** `[1, num_samples]` (variable length)
- **Format:** float32 mono waveform (PCM, range roughly [-1, 1])
- **Sample rate:** works with both 16kHz and 32kHz (original camera audio is 32kHz)

### Outputs

| Output | Shape | Description |
|---|---|---|
| `clipwise_output` | `[1, 2]` | Raw logits for 2 classes (apply softmax) |
| `onnx::Gemm_147` | `[1, 512]` | Audio embedding vector |

### Class Interpretation

Based on testing with synthetic inputs:

| Input | Class 0 | Class 1 |
|---|---|---|
| Real game audio | 0.01 | **0.99** |
| White noise | **0.87** | 0.13 |
| Silence | 0.21 | **0.79** |
| 3kHz tone | 0.14 | **0.86** |

Class 0 appears to correlate with broad-spectrum noise. Class 1 appears to correlate with structured audio (speech, whistles, game sounds) or silence. Further testing with known whistle/game segments would clarify the exact semantics.

### Usage

```python
import subprocess
import numpy as np
import onnxruntime as ort

sess = ort.InferenceSession(r"C:\onnx_models\decrypted\detect_audio_int8.onnx")

def classify_audio(audio_path, start_sec=0, duration_sec=5, sample_rate=16000):
    """Classify an audio segment. Returns (class_0_prob, class_1_prob)."""
    result = subprocess.run([
        "ffmpeg", "-y", "-ss", str(start_sec),
        "-i", audio_path, "-t", str(duration_sec),
        "-ar", str(sample_rate), "-ac", "1", "-f", "f32le", "pipe:1"
    ], capture_output=True)

    waveform = np.frombuffer(result.stdout, dtype=np.float32).reshape(1, -1)
    outputs = sess.run(None, {"waveform": waveform})

    logits = outputs[0][0]
    exp = np.exp(logits - np.max(logits))
    probs = exp / exp.sum()
    return probs[0], probs[1]

def get_audio_embedding(waveform):
    """Get 512-d audio embedding for similarity/clustering."""
    if waveform.ndim == 1:
        waveform = waveform.reshape(1, -1)
    outputs = sess.run(None, {"waveform": waveform.astype(np.float32)})
    return outputs[1][0]  # (512,)
```

### Applications

- **Game start/end detection:** scan audio in sliding windows to detect transitions
- **Audio embeddings:** the 512-d embedding vector can be used for clustering or similarity search (e.g., finding all whistle events by comparing embeddings)

---

## Integration with soccer-cam Pipeline

The `BallDetector` class on the `feature/ball-tracking` branch (`video_grouper/ball_tracking/detector.py`) currently loads models via `ultralytics.YOLO()`. To use these ONNX models instead:

1. **Direct swap:** `ultralytics.YOLO()` accepts `.onnx` files directly:
   ```python
   from ultralytics import YOLO
   model = YOLO(r"C:\onnx_models\decrypted\balldet_int8.onnx")
   ```

2. **Native onnxruntime:** for lighter dependency (no ultralytics needed), use the `detect_balls()` function from this guide. Wire it into the pipeline by replacing `BallDetector.detect_in_rois()`.

3. **Field keypoints:** could replace or augment the `play_region` fallback in the combined viewer, which currently snaps to (0,0) on quiet frames. Keypoint-derived field boundaries would provide a stable fallback.

4. **Audio detection:** could supplement or replace the NTFY-based game start/end detection. Scan the audio track with a sliding window to find the first whistle automatically.
