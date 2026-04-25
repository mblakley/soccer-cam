"""Ball-follow virtual camera: detect ball + render dewarped viewport.

Two-pass architecture:
  1. detect: Run ball detection on panoramic video → trajectory JSON
  2. render: Read trajectory + video → dewarped virtual camera output

Supports pluggable detectors:
  - onnx: YOLO-based ONNX model (balldet_fp16.onnx)
  - temporal: TemporalBallNet (heatmap model, requires PyTorch)

Usage:
    python -m training.inference.ball_follow detect \
        --video INPUT.mp4 --detector onnx --model balldet_fp16.onnx

    python -m training.inference.ball_follow render \
        --video INPUT.mp4 --trajectory traj.json -o output.mp4

    python -m training.inference.ball_follow run \
        --video INPUT.mp4 --detector onnx --model balldet_fp16.onnx -o output.mp4
"""

import argparse
import json
import logging
import math
import time
from pathlib import Path

import av
import cv2
import numpy as np

from training.data_prep.dewarp_tiles import (
    CAMERA_TILT,
    SRC_HEIGHT,
    SRC_HFOV,
    SRC_VRANGE,
    SRC_WIDTH,
    _build_dewarp_map,
)
from training.inference.ball_tracker import BallTracker

logger = logging.getLogger(__name__)

# ---- Default virtual camera parameters ----
DEFAULT_CROP_W = 1920
DEFAULT_CROP_H = 1080
DEFAULT_VFOV = 35.0  # degrees

# Autocam uses --movesmooth 0.975 = alpha 0.025 per frame
# We use that as the MAX (fast ball), with a slower MIN for gentle movement
DEFAULT_PAN_SMOOTHING_MIN = 0.010   # slow/stationary ball: ultra smooth
DEFAULT_PAN_SMOOTHING_MAX = 0.025   # fast ball: match Autocam's 0.975 EMA
DEFAULT_ZOOM_SMOOTHING = 0.01
DEFAULT_MAX_LEAD_ROOM = 0.20
DEFAULT_MAX_EXPECTED_SPEED = 60.0  # px/frame at subsample=4 (calibrate per camera)
DEFAULT_DEAD_BALL_THRESHOLD = 3.0  # px/frame
DEFAULT_DEAD_BALL_FRAMES = 15
DEFAULT_ZOOM_BOX = 0.25
DEFAULT_ZOOM_THIRD = 0.35
DEFAULT_ZOOM_MIDFIELD = 0.45
DEFAULT_ZOOM_SPEED_BIAS = 0.10
DEFAULT_MISSING_SHORT = 15  # frames
DEFAULT_MISSING_MEDIUM = 60  # frames

# Legacy defaults (kept for CLI compatibility)
DEFAULT_FOLLOW_GAIN = 0.08
DEFAULT_MAX_SPEED = 0.025
DEFAULT_DEAD_ZONE_YAW = 0.05
DEFAULT_DEAD_ZONE_PITCH = 0.03
DEFAULT_CENTER_DRIFT_FRAMES = 50


def pano_px_to_angles(pano_x: float, pano_y: float) -> tuple[float, float]:
    """Convert panoramic pixel coordinates to (yaw, pitch) angles.

    Uses the inverse of the cylindrical projection from dewarp_tiles.py.
    """
    src_u = pano_x / SRC_WIDTH
    src_v = pano_y / SRC_HEIGHT
    theta = (src_u - 0.5) * SRC_HFOV
    y_cyl = (src_v - 0.5) * SRC_VRANGE

    # Cylindrical to camera-space ray
    r3_x = math.sin(theta)
    r3_z = math.cos(theta)
    r3_y = y_cyl * math.sqrt(r3_x**2 + r3_z**2)

    # Undo camera tilt (camera space → world space)
    ct, st = math.cos(CAMERA_TILT), math.sin(CAMERA_TILT)
    world_x = r3_x
    world_y = ct * r3_y - st * r3_z
    world_z = st * r3_y + ct * r3_z

    yaw = math.atan2(world_x, world_z)
    pitch = math.atan2(-world_y, math.sqrt(world_x**2 + world_z**2))
    return yaw, pitch


class VirtualCamera:
    """Broadcast-style virtual camera following the ball.

    Implements the algorithm from VIRTUAL_CAMERA.md:
    - Lead room in direction of ball travel
    - Field zone-based zoom (tight near goal, wide at midfield)
    - Speed-adaptive pan smoothing (fast ball = faster pan response)
    - Dead-ball overrides (corner kicks, free kicks, etc.)
    - Missing ball handling (hold → zoom out → wide default)
    - Dewarped output via cylindrical-to-rectilinear reprojection
    """

    def __init__(
        self,
        crop_w: int = DEFAULT_CROP_W,
        crop_h: int = DEFAULT_CROP_H,
        fps: float = 25.0,
        field_polygon: np.ndarray | None = None,
        max_expected_speed: float = DEFAULT_MAX_EXPECTED_SPEED,
        **kwargs,  # accept legacy params without error
    ):
        self.crop_w = crop_w
        self.crop_h = crop_h
        self.fps = fps
        self.field_polygon = field_polygon
        self.max_expected_speed = max_expected_speed

        # Zoom levels (crop width as fraction of source width)
        self.zoom_box = DEFAULT_ZOOM_BOX
        self.zoom_third = DEFAULT_ZOOM_THIRD
        self.zoom_midfield = DEFAULT_ZOOM_MIDFIELD
        self.zoom_speed_bias = DEFAULT_ZOOM_SPEED_BIAS

        # Smoothing rates
        self.pan_smoothing_min = DEFAULT_PAN_SMOOTHING_MIN
        self.pan_smoothing_max = DEFAULT_PAN_SMOOTHING_MAX
        self.zoom_smoothing = DEFAULT_ZOOM_SMOOTHING

        # Lead room
        self.max_lead_room = DEFAULT_MAX_LEAD_ROOM

        # Dead ball
        self.dead_ball_threshold = DEFAULT_DEAD_BALL_THRESHOLD
        self.dead_ball_frames = DEFAULT_DEAD_BALL_FRAMES

        # Missing ball thresholds
        self.missing_short = DEFAULT_MISSING_SHORT
        self.missing_medium = DEFAULT_MISSING_MEDIUM

        # ---- State ----
        self._initialized = False

        # Smoothed camera position (panoramic pixel coords)
        self._pan_x = SRC_WIDTH / 2.0
        self._pan_y = SRC_HEIGHT / 2.0
        self._zoom = self.zoom_midfield  # crop width fraction

        # Ball kinematics (EMA-smoothed)
        self._vel_x = 0.0
        self._vel_y = 0.0
        self._speed = 0.0
        self._prev_ball_x: float | None = None
        self._prev_ball_y: float | None = None

        # Dead ball counter
        self._dead_ball_count = 0

        # Missing ball counter
        self._missing_frames = 0

        # Remap cache
        self._cached_yaw = None
        self._cached_pitch = None
        self._cached_vfov = None
        self._cached_maps = None

    def _field_zone(self, ball_x: float) -> str:
        """Classify ball x-position into a field zone."""
        norm_x = ball_x / SRC_WIDTH
        if norm_x < 0.10:
            return "left_box"
        elif norm_x < 0.33:
            return "left_third"
        elif norm_x < 0.67:
            return "midfield"
        elif norm_x < 0.90:
            return "right_third"
        else:
            return "right_box"

    def _zone_zoom(self, zone: str) -> float:
        """Base zoom level for a field zone."""
        if zone in ("left_box", "right_box"):
            return self.zoom_box
        elif zone in ("left_third", "right_third"):
            return self.zoom_third
        else:
            return self.zoom_midfield

    def _is_on_field(self, ball_x: float, ball_y: float) -> bool:
        if self.field_polygon is None:
            return True
        dist = cv2.pointPolygonTest(
            self.field_polygon, (float(ball_x), float(ball_y)), measureDist=True
        )
        return dist >= -30

    def update(self, ball_x: float | None, ball_y: float | None) -> None:
        """Advance one frame with optional ball position."""

        if ball_x is not None and ball_y is not None:
            # ---- Ball visible ----

            # Check if ball is out of bounds (outside field mask)
            ball_on_field = self._is_on_field(ball_x, ball_y)

            if not ball_on_field and self._initialized:
                # Ball went out of bounds — hold camera position, slowly zoom out
                # Don't track it off-field; wait for it to come back nearby
                self._missing_frames += 1
                self._speed *= 0.95
                target_zoom = min(self._zoom + 0.001, self.zoom_midfield)
                self._zoom += self.zoom_smoothing * (target_zoom - self._zoom)
                return

            self._missing_frames = 0

            # First frame: snap instantly
            if not self._initialized:
                self._pan_x = ball_x
                self._pan_y = ball_y
                self._prev_ball_x = ball_x
                self._prev_ball_y = ball_y
                self._initialized = True
                zone = self._field_zone(ball_x)
                self._zoom = self._zone_zoom(zone)
                return

            # Step 1-3: Ball kinematics (EMA velocity, lower alpha for smoother)
            if self._prev_ball_x is not None:
                raw_vx = ball_x - self._prev_ball_x
                raw_vy = ball_y - self._prev_ball_y
                alpha = 0.15  # smoother velocity estimate (was 0.3)
                self._vel_x = alpha * raw_vx + (1 - alpha) * self._vel_x
                self._vel_y = alpha * raw_vy + (1 - alpha) * self._vel_y
                self._speed = math.sqrt(self._vel_x**2 + self._vel_y**2)

            self._prev_ball_x = ball_x
            self._prev_ball_y = ball_y

            # Step 4: Field zone
            zone = self._field_zone(ball_x)
            speed_norm = min(self._speed / self.max_expected_speed, 1.0)

            # Step 5: Target zoom — KEY BEHAVIOR: fast horizontal ball = zoom OUT
            # instead of frantic panning. The ball crosses the field; show more.
            horiz_speed = abs(self._vel_x)
            horiz_norm = min(horiz_speed / self.max_expected_speed, 1.0)

            if horiz_norm > 0.4 and zone == "midfield":
                # Fast horizontal movement in midfield — zoom out wide to show
                # the whole transition instead of chasing the ball frantically
                target_zoom = self.zoom_midfield + horiz_norm * 0.15
            else:
                # Normal zone-based zoom + speed bias
                target_zoom = self._zone_zoom(zone) + speed_norm * self.zoom_speed_bias

            # Step 6: Lead room offset
            crop_w_px = target_zoom * SRC_WIDTH
            max_lead_px = self.max_lead_room * crop_w_px
            lead_factor = min(self._speed / self.max_expected_speed, 1.0)

            if self._speed > 0.5:
                vel_nx = self._vel_x / self._speed
                vel_ny = self._vel_y / self._speed
            else:
                vel_nx, vel_ny = 0.0, 0.0

            lead_x = vel_nx * lead_factor * max_lead_px
            lead_y = vel_ny * lead_factor * max_lead_px * 0.3  # less vertical lead

            target_pan_x = ball_x + lead_x
            target_pan_y = ball_y + lead_y

            # Step 7: Dead-ball overrides
            if self._speed < self.dead_ball_threshold:
                self._dead_ball_count += 1
            else:
                self._dead_ball_count = 0

            if self._dead_ball_count >= self.dead_ball_frames:
                if zone in ("left_box", "right_box"):
                    target_zoom = self.zoom_box
                elif ball_y > SRC_HEIGHT * 0.85 or ball_y < SRC_HEIGHT * 0.15:
                    target_zoom = self.zoom_third
                else:
                    target_zoom = min(self._zoom + 0.005, self.zoom_midfield)

                norm_x = ball_x / SRC_WIDTH
                is_corner = (norm_x < 0.05 or norm_x > 0.95) and (
                    ball_y > SRC_HEIGHT * 0.7 or ball_y < SRC_HEIGHT * 0.3
                )
                if is_corner:
                    goal_x = SRC_WIDTH * 0.05 if norm_x < 0.5 else SRC_WIDTH * 0.95
                    target_pan_x = ball_x + (goal_x - ball_x) * 0.3
                    target_zoom = self.zoom_box

            # Step 8: Smoothing — Autocam-style heavy EMA
            # Pan smoothing: slow and cinematic, slightly responsive to speed
            pan_alpha = self.pan_smoothing_min + (
                self.pan_smoothing_max - self.pan_smoothing_min
            ) * speed_norm

            self._pan_x += pan_alpha * (target_pan_x - self._pan_x)
            self._pan_y += pan_alpha * (target_pan_y - self._pan_y)
            self._zoom += self.zoom_smoothing * (target_zoom - self._zoom)

        else:
            # ---- Ball missing (no detection at all) ----
            self._missing_frames += 1
            self._speed *= 0.95

            if not self._initialized:
                return

            # Hold position for short gaps, zoom out gradually for longer
            if self._missing_frames <= self.missing_short:
                pass  # hold
            elif self._missing_frames <= self.missing_medium:
                target_zoom = min(self._zoom + 0.001, self.zoom_midfield + 0.05)
                self._zoom += self.zoom_smoothing * (target_zoom - self._zoom)
            else:
                target_zoom = 0.50
                self._zoom += self.zoom_smoothing * (target_zoom - self._zoom)
                self._pan_x += 0.005 * (SRC_WIDTH / 2 - self._pan_x)
                self._pan_y += 0.01 * (SRC_HEIGHT / 2 - self._pan_y)

    def get_crop_rect(self) -> tuple[float, float, float, float]:
        """Get current crop rectangle in panoramic pixel coords.

        Returns (x, y, w, h) clamped to source bounds.
        """
        w = self._zoom * SRC_WIDTH
        h = w * (self.crop_h / self.crop_w)  # maintain output aspect ratio

        x = self._pan_x - w / 2
        y = self._pan_y - h / 2

        # Clamp to source bounds
        x = max(0, min(x, SRC_WIDTH - w))
        y = max(0, min(y, SRC_HEIGHT - h))

        return x, y, w, h

    def get_remap(self) -> tuple[np.ndarray, np.ndarray]:
        """Get dewarped remap arrays for the current camera state.

        Converts the crop center to yaw/pitch angles and uses the
        cylindrical-to-rectilinear projection for dewarping.
        """
        # Convert pan position to camera angles
        yaw, pitch = pano_px_to_angles(self._pan_x, self._pan_y)

        # Convert zoom level to vertical FOV
        # Wider crop = larger FOV
        # At zoom=0.25, we want ~25° vfov (tight)
        # At zoom=0.50, we want ~50° vfov (wide)
        vfov = self._zoom * 100.0  # simple linear mapping, tune as needed
        vfov = max(20.0, min(55.0, vfov))  # clamp

        # Cache check
        if (
            self._cached_maps is not None
            and self._cached_yaw is not None
            and abs(yaw - self._cached_yaw) < 0.001
            and abs(pitch - self._cached_pitch) < 0.001
            and abs(vfov - self._cached_vfov) < 0.05
        ):
            return self._cached_maps

        map_x, map_y = _build_dewarp_map(
            yaw, pitch, vfov, self.crop_w, self.crop_h
        )
        self._cached_yaw = yaw
        self._cached_pitch = pitch
        self._cached_vfov = vfov
        self._cached_maps = (map_x, map_y)
        return map_x, map_y


# ---- Detector factories ----


def make_onnx_detector(
    model_path: str | Path,
    conf_threshold: float = 0.45,
    nms_iou: float = 0.5,
):
    """Create an ONNX-based ball detector.

    Returns a callable: frame_bgr → list[(x, y, confidence)]
    """
    import os

    import onnxruntime as ort

    # Ensure CUDA DLLs from PyTorch are on PATH for onnxruntime
    try:
        import torch

        torch_lib = str(Path(torch.__file__).parent / "lib")
        if torch_lib not in os.environ.get("PATH", ""):
            os.environ["PATH"] = torch_lib + os.pathsep + os.environ.get("PATH", "")
    except ImportError:
        pass

    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    sess = ort.InferenceSession(str(model_path), providers=providers)
    active_provider = sess.get_providers()[0]
    logger.info("ONNX detector loaded (provider: %s)", active_provider)

    def detect(frame_bgr: np.ndarray) -> list[tuple[float, float, float]]:
        orig_h, orig_w = frame_bgr.shape[:2]
        stride = 32
        pad_h = (stride - orig_h % stride) % stride
        pad_w = (stride - orig_w % stride) % stride

        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        if pad_h > 0 or pad_w > 0:
            rgb = cv2.copyMakeBorder(
                rgb, 0, pad_h, 0, pad_w, cv2.BORDER_CONSTANT, value=(0, 0, 0)
            )

        blob = (rgb.astype(np.float32) / 255.0).transpose(2, 0, 1)[np.newaxis]
        outputs = sess.run(None, {"images": blob})
        det = outputs[0][0]

        mask = det[:, 5] > conf_threshold
        filtered = det[mask]
        if len(filtered) == 0:
            return []

        boxes = np.zeros((len(filtered), 4))
        boxes[:, 0] = filtered[:, 0] - filtered[:, 2] / 2
        boxes[:, 1] = filtered[:, 1] - filtered[:, 3] / 2
        boxes[:, 2] = filtered[:, 0] + filtered[:, 2] / 2
        boxes[:, 3] = filtered[:, 1] + filtered[:, 3] / 2

        indices = cv2.dnn.NMSBoxes(
            boxes.tolist(), filtered[:, 5].tolist(), conf_threshold, nms_iou
        )
        if len(indices) == 0:
            return []

        results = []
        for idx in indices:
            i = idx[0] if isinstance(idx, (list, np.ndarray)) else idx
            results.append((
                float(filtered[i, 0]),
                float(filtered[i, 1]),
                float(filtered[i, 5]),
            ))
        return results

    return detect


def make_yolo_detector(
    model_path: str | Path,
    conf_threshold: float = 0.45,
    iou_threshold: float = 0.5,
    device: str = "0",
):
    """Create a YOLO/ultralytics-based ball detector.

    Tiles the panoramic frame into a 7x3 grid of 640x640 tiles
    (matching training data), runs detection on each tile, then
    maps detections back to panoramic pixel coordinates.

    Returns a callable: frame_bgr → list[(x, y, confidence)]
    """
    from ultralytics import YOLO

    model = YOLO(str(model_path))
    dev = device if device == "cpu" else int(device)
    logger.info("YOLO detector loaded: %s (device: %s)", model_path, dev)

    # Tile grid constants (must match training pipeline)
    tile_size = 640
    n_cols = 7
    n_rows = 3
    step_x = (SRC_WIDTH - tile_size) // (n_cols - 1)   # 576
    step_y = (SRC_HEIGHT - tile_size) // (n_rows - 1)   # 580

    def detect(frame_bgr: np.ndarray) -> list[tuple[float, float, float]]:
        all_detections = []

        for row in range(n_rows):
            for col in range(n_cols):
                x0 = col * step_x
                y0 = row * step_y
                tile = frame_bgr[y0:y0 + tile_size, x0:x0 + tile_size]

                results = model.predict(
                    tile, conf=conf_threshold, iou=iou_threshold,
                    device=dev, verbose=False, imgsz=640,
                )
                for r in results:
                    for box in r.boxes:
                        # Map tile coords back to panoramic coords
                        tcx, tcy = float(box.xywh[0][0]), float(box.xywh[0][1])
                        conf = float(box.conf[0])
                        pano_cx = x0 + tcx
                        pano_cy = y0 + tcy
                        all_detections.append((pano_cx, pano_cy, conf))

        # NMS across tiles to deduplicate overlapping detections
        if len(all_detections) > 1:
            all_detections = _nms_detections(all_detections, radius=30)

        return all_detections

    return detect


def _nms_detections(
    detections: list[tuple[float, float, float]],
    radius: float = 30,
) -> list[tuple[float, float, float]]:
    """Simple distance-based NMS for cross-tile deduplication."""
    # Sort by confidence descending
    dets = sorted(detections, key=lambda d: d[2], reverse=True)
    keep = []
    for x, y, conf in dets:
        suppressed = False
        for kx, ky, _kc in keep:
            if (x - kx) ** 2 + (y - ky) ** 2 < radius ** 2:
                suppressed = True
                break
        if not suppressed:
            keep.append((x, y, conf))
    return keep


# ---- Field mask filtering ----


def load_field_polygon(game_id: str) -> np.ndarray | None:
    """Load field boundary polygon from the game's manifest.db.

    Searches D: then F: for the manifest. Returns polygon as
    numpy array shaped for cv2.pointPolygonTest, or None.
    """
    import sqlite3

    for base in ["D:/training_data/games", "F:/training_data/games"]:
        db_path = Path(base) / game_id / "manifest.db"
        if not db_path.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            row = conn.execute(
                'SELECT value FROM metadata WHERE key="field_boundary"'
            ).fetchone()
            conn.close()
            if row:
                import json as _json

                fb = _json.loads(row[0])
                polygon = fb.get("polygon")
                if polygon and len(polygon) >= 4:
                    return np.array(polygon, dtype=np.float32).reshape(-1, 1, 2)
        except Exception as e:
            logger.warning("Failed to load field boundary from %s: %s", db_path, e)
    return None


def apply_field_mask(
    detections: list[tuple[float, float, float]],
    polygon: np.ndarray,
    margin: float = 50.0,
    far_penalty: float = 0.1,
) -> list[tuple[float, float, float]]:
    """Apply soft confidence penalty to detections outside the field polygon.

    - Inside polygon: full confidence (no change)
    - Within margin outside: linear ramp-down
    - Beyond margin: multiply confidence by far_penalty
    """
    if len(detections) == 0:
        return detections

    filtered = []
    for x, y, conf in detections:
        dist = cv2.pointPolygonTest(polygon, (float(x), float(y)), measureDist=True)
        if dist >= 0:
            # Inside field — keep full confidence
            filtered.append((x, y, conf))
        elif dist >= -margin:
            # Near off-field — linear penalty
            scale = 1.0 - (1.0 - far_penalty) * (-dist / margin)
            filtered.append((x, y, conf * scale))
        else:
            # Far off-field — heavy penalty
            filtered.append((x, y, conf * far_penalty))
    return filtered


# ---- Pass 1: Detection (dump all raw detections) ----


def detect_pass(
    video_path: Path,
    detector_type: str,
    model_path: Path,
    output_json: Path,
    device: str = "0",
    subsample: int = 1,
    conf_threshold: float = 0.15,
    game_id: str | None = None,
):
    """Run ball detection on a video and save ALL raw detections.

    Uses a low confidence threshold to capture as many candidates as
    possible. Filtering and tracking happen in track_pass().
    """
    logger.info("Detect pass: %s (detector=%s, subsample=%d, conf=%.2f)",
                video_path.name, detector_type, subsample, conf_threshold)

    # Create detector with low threshold to capture everything
    if detector_type == "onnx":
        detect_fn = make_onnx_detector(model_path, conf_threshold)
    elif detector_type == "yolo":
        detect_fn = make_yolo_detector(model_path, conf_threshold, device=device)
    else:
        raise ValueError(f"Unknown detector type: {detector_type}")

    # Open video
    container = av.open(str(video_path))
    video_stream = container.streams.video[0]
    fps = float(video_stream.average_rate or 25)
    total_frames = video_stream.frames or 0
    logger.info("Video: %dx%d @ %.1f fps, ~%d frames",
                video_stream.width, video_stream.height, fps, total_frames)

    all_detections: dict[int, list[tuple[float, float, float]]] = {}
    frame_idx = 0
    frames_processed = 0
    total_dets = 0
    t0 = time.time()

    for packet in container.demux(video_stream):
        for av_frame in packet.decode():
            if frame_idx % subsample == 0:
                frame_bgr = av_frame.to_ndarray(format="bgr24")
                detections = detect_fn(frame_bgr)
                if detections:
                    all_detections[frame_idx] = detections
                    total_dets += len(detections)
                frames_processed += 1

                if frames_processed % 200 == 0:
                    elapsed = time.time() - t0
                    rate = frames_processed / elapsed if elapsed > 0 else 0
                    frames_with = len(all_detections)
                    logger.info(
                        "  Frame %d/%d (%.1f f/s, %d/%d frames have detections, %d total dets)",
                        frame_idx, total_frames, rate,
                        frames_with, frames_processed, total_dets,
                    )

            frame_idx += 1

    container.close()
    elapsed = time.time() - t0
    rate = frames_processed / elapsed if elapsed > 0 else 0
    frames_with = len(all_detections)
    logger.info("Detection complete: %d frames in %.0fs (%.1f f/s)", frames_processed, elapsed, rate)
    logger.info("  %d/%d frames have detections (%d total detections)",
                frames_with, frames_processed, total_dets)

    # Write raw detections JSON
    result = {
        "version": 2,
        "source_video": str(video_path),
        "detector": detector_type,
        "model": str(model_path),
        "pano_width": SRC_WIDTH,
        "pano_height": SRC_HEIGHT,
        "fps": fps,
        "total_frames": frame_idx,
        "frames_processed": frames_processed,
        "subsample": subsample,
        "conf_threshold": conf_threshold,
        "game_id": game_id,
        "raw_detections": {
            str(fi): [{"x": round(x, 1), "y": round(y, 1), "conf": round(c, 3)}
                       for x, y, c in dets]
            for fi, dets in all_detections.items()
        },
    }

    output_json.parent.mkdir(parents=True, exist_ok=True)
    with open(output_json, "w") as f:
        json.dump(result, f)
    logger.info("Wrote raw detections: %s", output_json)


# ---- Pass 2: Track (build single-ball trajectory from raw detections) ----


def track_pass(
    detections_json: Path,
    output_json: Path,
    game_id: str | None = None,
    max_gap_seconds: float = 3.0,
):
    """Build a single continuous ball trajectory from raw detections.

    Core insight: there's exactly 1 game ball, and it's usually the
    detection that MOVES the most between frames. Static detections
    are false positives (field markings, player heads).

    Algorithm:
    1. Apply field mask (soft penalty for off-field detections)
    2. Greedy forward track with Kalman filter + restart on loss
    3. Use inter-frame motion to re-acquire the ball after losing it
    4. Smooth the full trajectory with a second Kalman pass
    5. Interpolate gaps with physics (velocity + gravity for air balls)
    """
    from filterpy.kalman import KalmanFilter

    with open(detections_json) as f:
        data = json.load(f)

    raw = data["raw_detections"]
    fps = data["fps"]
    total_frames = data["total_frames"]
    subsample = data.get("subsample", 1)
    dt = subsample / fps  # seconds between processed frames

    logger.info("Track pass: %d frames with detections, dt=%.3fs",
                len(raw), dt)

    # Load field polygon
    field_polygon = None
    gid = game_id or data.get("game_id")
    if gid:
        field_polygon = load_field_polygon(gid)
        if field_polygon is not None:
            logger.info("Using field boundary for %s", gid)

    # Pre-process: apply field mask, sort by frame index
    frame_dets: dict[int, list[tuple[float, float, float]]] = {}
    for fi_str in sorted(raw.keys(), key=int):
        fi = int(fi_str)
        dets = [(d["x"], d["y"], d["conf"]) for d in raw[fi_str]]
        if field_polygon is not None:
            dets = apply_field_mask(dets, field_polygon, margin=75.0, far_penalty=0.05)
        dets = [(x, y, c) for x, y, c in dets if c > 0.03]
        if dets:
            frame_dets[fi] = dets

    sorted_frames = sorted(frame_dets.keys())
    logger.info("After field mask: %d frames with viable detections", len(sorted_frames))

    # ---- Helper: check if a point is on the field ----
    def _on_field(x: float, y: float) -> bool:
        if field_polygon is None:
            return True
        return cv2.pointPolygonTest(field_polygon, (float(x), float(y)), measureDist=True) >= -30

    def _field_dist(x: float, y: float) -> float:
        """Positive = inside field, negative = outside."""
        if field_polygon is None:
            return 100.0
        return cv2.pointPolygonTest(field_polygon, (float(x), float(y)), measureDist=True)

    # ---- Helper: find the best on-field moving detection ----
    def _find_moving_det(
        fi: int, prev_fi: int | None = None, require_on_field: bool = True
    ) -> tuple[float, float, float] | None:
        """Find the detection most likely to be the game ball.

        Strongly prefers on-field detections. Uses inter-frame motion
        to distinguish the moving game ball from static false positives.
        """
        dets = frame_dets.get(fi)
        if not dets:
            return None

        if prev_fi is None or prev_fi not in frame_dets:
            # No previous frame — return best on-field detection
            on_field = [(x, y, c) for x, y, c in dets if _on_field(x, y)]
            if on_field:
                return max(on_field, key=lambda d: d[2])
            if not require_on_field:
                return max(dets, key=lambda d: d[2])
            return None

        prev_dets = frame_dets[prev_fi]
        frames_gap = (fi - prev_fi) / subsample
        min_move = 3 * frames_gap
        max_move = 80 * frames_gap

        best = None
        best_score = -1
        for x, y, conf in dets:
            # On-field bonus: massive preference for on-field detections
            fd = _field_dist(x, y)
            if fd >= 0:
                field_score = 1.0
            elif fd >= -50:
                field_score = 0.5  # near boundary, maybe ok
            else:
                field_score = 0.05  # far off-field, very unlikely to be game ball
                if require_on_field:
                    continue

            # Motion score from previous frame
            min_dist = float("inf")
            for px, py, _pc in prev_dets:
                d = math.sqrt((x - px) ** 2 + (y - py) ** 2)
                if d < min_dist:
                    min_dist = d

            if min_move <= min_dist <= max_move:
                motion_score = 1.0
            elif min_dist < min_move:
                motion_score = 0.2
            elif min_dist <= max_move * 2:
                motion_score = 0.5
            else:
                motion_score = 0.1

            score = field_score * 0.4 + motion_score * 0.3 + conf * 0.3
            if score > best_score:
                best_score = score
                best = (x, y, conf)

        return best

    # ---- Kalman filter factory ----
    def _make_kf(x0: float, y0: float) -> KalmanFilter:
        """Constant-velocity Kalman filter. State: [x, y, vx, vy]."""
        kf = KalmanFilter(dim_x=4, dim_z=2)
        kf.F = np.array([
            [1, 0, dt, 0],
            [0, 1, 0, dt],
            [0, 0, 1, 0],
            [0, 0, 0, 1],
        ], dtype=np.float64)
        kf.H = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], dtype=np.float64)
        kf.Q = np.diag([10**2, 10**2, 30**2, 30**2])
        kf.R = np.diag([15**2, 15**2])
        kf.x = np.array([x0, y0, 0, 0], dtype=np.float64)
        kf.P = np.diag([50**2, 50**2, 100**2, 100**2])
        return kf

    # ---- Identify non-game-ball detections ----
    # Two-step process:
    # 1. Build a quick rough track of the game ball (on-field, moving)
    # 2. Any detection that co-occurs with the game ball but is far away
    #    is definitively NOT the game ball — suppress it
    SUPPRESS_RADIUS = 300  # detections within this radius of a false positive are suppressed
    COOCCUR_DIST = 500  # must be this far from game ball to be "a different ball"
    COOCCUR_MIN = 30  # must co-occur on this many frames to be flagged

    # Step 1: Quick rough forward track (on-field only, simple greedy)
    rough_track: dict[int, tuple[float, float]] = {}
    rough_kf = None
    rough_last_x, rough_last_y = SRC_WIDTH / 2, SRC_HEIGHT / 2
    for fi in sorted_frames:
        dets = frame_dets[fi]
        on_field_dets = [(x, y, c) for x, y, c in dets if _on_field(x, y)]
        if not on_field_dets:
            continue

        if rough_kf is None:
            best = max(on_field_dets, key=lambda d: d[2])
            rough_kf = _make_kf(best[0], best[1])
            rough_track[fi] = (best[0], best[1])
            rough_last_x, rough_last_y = best[0], best[1]
            continue

        rough_kf.predict()
        px, py = float(rough_kf.x[0]), float(rough_kf.x[1])

        best = None
        best_dist = 400
        for x, y, conf in on_field_dets:
            d = math.sqrt((x - px) ** 2 + (y - py) ** 2)
            if d < best_dist:
                best_dist = d
                best = (x, y)

        if best:
            rough_kf.update(np.array([best[0], best[1]]))
            rough_track[fi] = best
            rough_last_x, rough_last_y = best[0], best[1]

    logger.info("Rough track: %d frames", len(rough_track))

    # Step 2: Find concurrent non-game-ball clusters
    # For each frame with a rough track position, look at other detections
    # that are far away — these are "other balls"
    other_ball_hits: list[tuple[float, float]] = []  # all far-away concurrent detections
    for fi in rough_track:
        if fi not in frame_dets:
            continue
        gx, gy = rough_track[fi]
        for x, y, conf in frame_dets[fi]:
            dist = math.sqrt((x - gx) ** 2 + (y - gy) ** 2)
            if dist > COOCCUR_DIST:
                other_ball_hits.append((x, y))

    # Cluster the other-ball hits
    other_clusters: list[tuple[float, float, int]] = []
    for x, y in other_ball_hits:
        matched = False
        for ci, (cx, cy, count) in enumerate(other_clusters):
            if math.sqrt((x - cx) ** 2 + (y - cy) ** 2) < SUPPRESS_RADIUS:
                n = count + 1
                other_clusters[ci] = ((cx * count + x) / n, (cy * count + y) / n, n)
                matched = True
                break
        if not matched:
            other_clusters.append((x, y, 1))

    # Flag clusters with enough co-occurrences
    suppressed_spots: list[tuple[float, float]] = []
    for cx, cy, count in other_clusters:
        if count >= COOCCUR_MIN:
            suppressed_spots.append((cx, cy))
            logger.info("  Suppressing non-game-ball region at (%.0f, %.0f) — %d co-occurrences",
                        cx, cy, count)
    logger.info("Identified %d non-game-ball regions to suppress", len(suppressed_spots))

    def _is_suppressed(x: float, y: float) -> bool:
        """Check if detection is in a known non-game-ball region."""
        for cx, cy in suppressed_spots:
            if math.sqrt((x - cx) ** 2 + (y - cy) ** 2) < SUPPRESS_RADIUS:
                return True
        return False

    # Remove suppressed detections from frame_dets entirely
    if suppressed_spots:
        removed = 0
        for fi in sorted_frames:
            before = len(frame_dets[fi])
            frame_dets[fi] = [(x, y, c) for x, y, c in frame_dets[fi]
                              if not _is_suppressed(x, y)]
            removed += before - len(frame_dets[fi])
        # Remove empty frames
        frame_dets = {fi: dets for fi, dets in frame_dets.items() if dets}
        sorted_frames = sorted(frame_dets.keys())
        logger.info("Removed %d non-game-ball detections, %d frames remaining",
                    removed, len(sorted_frames))

    # ---- Phase 1: Greedy forward tracking with restart ----
    def _run_tracking_pass(frames_list, direction="forward"):
        """Run a single tracking pass with spatial restart and static suppression."""
        track: dict[int, tuple[float, float, float]] = {}
        kf: KalmanFilter | None = None
        miss_count = 0
        max_miss = 8
        off_field_count = 0
        stuck_count = 0  # frames the track hasn't moved much
        last_x, last_y = SRC_WIDTH / 2, SRC_HEIGHT / 2  # last known ball position

        for idx, fi in enumerate(frames_list):
            dets = frame_dets[fi]

            if kf is None:
                # Restart: search near last known position, on-field only
                best = None
                best_score = -1
                prev_fi = frames_list[idx - 1] if idx > 0 else None

                for x, y, conf in dets:
                    if not _on_field(x, y):
                        continue

                    # Prefer detections near last known ball position
                    dist_to_last = math.sqrt((x - last_x) ** 2 + (y - last_y) ** 2)
                    proximity = max(0, 1.0 - dist_to_last / 1500)  # generous radius

                    score = proximity * 0.4 + conf * 0.3 + _field_dist(x, y) / 500 * 0.3
                    if score > best_score:
                        best_score = score
                        best = (x, y, conf)

                if best:
                    kf = _make_kf(best[0], best[1])
                    track[fi] = best
                    last_x, last_y = best[0], best[1]
                    miss_count = 0
                    off_field_count = 0
                    stuck_count = 0
                continue

            kf.predict()
            px, py = float(kf.x[0]), float(kf.x[1])
            gate = min(150 + miss_count * 40, 600)

            # Score detections
            best = None
            best_score = -1
            for x, y, conf in dets:
                dist = math.sqrt((x - px) ** 2 + (y - py) ** 2)
                if dist > gate:
                    continue

                proximity = 1.0 - dist / gate

                # On-field bonus
                fd = _field_dist(x, y)
                if fd >= 0:
                    field_bonus = 0.3
                elif fd >= -50:
                    field_bonus = 0.1
                else:
                    field_bonus = -0.3

                score = proximity * 0.4 + conf * 0.3 + field_bonus
                if score > best_score:
                    best_score = score
                    best = (x, y, conf)

            if best:
                # Check movement BEFORE updating last position
                move = math.sqrt((best[0] - last_x) ** 2 + (best[1] - last_y) ** 2)

                kf.update(np.array([best[0], best[1]]))
                track[fi] = best
                last_x, last_y = best[0], best[1]
                miss_count = 0

                # Track health checks
                if _on_field(best[0], best[1]):
                    off_field_count = 0
                else:
                    off_field_count += 1

                # Force restart if off-field too long
                if off_field_count > 20:
                    logger.debug("Frame %d: off-field for %d frames, restarting", fi, off_field_count)
                    kf = None
                    off_field_count = 0
                    continue
            else:
                miss_count += 1
                track[fi] = (px, py, 0.0)

                if miss_count >= max_miss:
                    kf = None
                    off_field_count = 0
                    stuck_count = 0

        detected = sum(1 for _, _, c in track.values() if c > 0)
        logger.info("  %s: %d detected + %d predicted = %d total",
                    direction, detected, len(track) - detected, len(track))
        return track

    ball_track = _run_tracking_pass(sorted_frames, "forward")

    detected = sum(1 for _, _, c in ball_track.values() if c > 0)
    predicted = len(ball_track) - detected
    logger.info("Phase 1 (forward track): %d detected + %d predicted = %d total",
                detected, predicted, len(ball_track))

    # ---- Phase 2: Backward pass ----
    bwd_track = _run_tracking_pass(list(reversed(sorted_frames)), "backward")

    # ---- Phase 3: Merge forward + backward ----
    # For each frame, use the pass that has a real detection;
    # if both have one, average them; if neither, use prediction
    merged: dict[int, tuple[float, float, float]] = {}
    for fi in sorted_frames:
        fwd = ball_track.get(fi)
        bwd = bwd_track.get(fi)

        if fwd and bwd:
            fc = fwd[2]
            bc = bwd[2]
            if fc > 0 and bc > 0:
                # Both detected — weighted average by confidence
                total = fc + bc
                mx = (fwd[0] * fc + bwd[0] * bc) / total
                my = (fwd[1] * fc + bwd[1] * bc) / total
                merged[fi] = (mx, my, max(fc, bc))
            elif fc > 0:
                merged[fi] = fwd
            elif bc > 0:
                merged[fi] = bwd
            else:
                # Both predicted — average positions
                merged[fi] = ((fwd[0] + bwd[0]) / 2, (fwd[1] + bwd[1]) / 2, 0.0)
        elif fwd:
            merged[fi] = fwd
        elif bwd:
            merged[fi] = bwd

    merged_detected = sum(1 for _, _, c in merged.values() if c > 0)
    logger.info("Phase 3 (merged): %d detected + %d predicted = %d total",
                merged_detected, len(merged) - merged_detected, len(merged))

    # ---- Phase 4: Smooth with final Kalman pass ----
    smoothed: dict[int, tuple[float, float]] = {}
    kf = None
    for fi in sorted_frames:
        if fi not in merged:
            continue
        x, y, conf = merged[fi]

        if kf is None:
            kf = _make_kf(x, y)
            smoothed[fi] = (x, y)
            continue

        kf.predict()
        # Weight measurement noise by confidence — high conf = trust detection more
        if conf > 0:
            kf.R = np.diag([(20 / (1 + conf * 3)) ** 2] * 2)
        else:
            kf.R = np.diag([200**2, 200**2])  # low trust for predictions
        kf.update(np.array([x, y]))
        smoothed[fi] = (float(kf.x[0]), float(kf.x[1]))

    logger.info("Phase 4 (smoothed): %d points", len(smoothed))

    # ---- Phase 5: Build final trajectory with interpolation ----
    trajectory: list[dict] = []
    smooth_frames = sorted(smoothed.keys())

    for i, fi in enumerate(smooth_frames):
        sx, sy = smoothed[fi]
        conf = merged[fi][2] if fi in merged else 0.0
        trajectory.append({
            "frame": fi, "x": round(sx, 1), "y": round(sy, 1),
            "confidence": round(conf, 3),
        })

        # Interpolate to next frame if gap exists
        if i + 1 < len(smooth_frames):
            next_fi = smooth_frames[i + 1]
            gap = (next_fi - fi) // subsample
            max_gap = int(max_gap_seconds * fps / subsample)
            if gap > 1 and gap <= max_gap:
                nx, ny = smoothed[next_fi]
                for interp_fi in range(fi + subsample, next_fi, subsample):
                    t = (interp_fi - fi) / (next_fi - fi)
                    # Smooth step interpolation (ease in/out)
                    t = t * t * (3 - 2 * t)
                    ix = sx + (nx - sx) * t
                    iy = sy + (ny - sy) * t
                    trajectory.append({
                        "frame": interp_fi,
                        "x": round(ix, 1), "y": round(iy, 1),
                        "confidence": 0.0,
                    })

    trajectory.sort(key=lambda p: p["frame"])

    det_count = sum(1 for p in trajectory if p["confidence"] > 0)
    logger.info("Final trajectory: %d points (%d detected + %d interpolated)",
                len(trajectory), det_count, len(trajectory) - det_count)

    # Write trajectory JSON
    result = {
        "version": 2,
        "source_video": data["source_video"],
        "detector": data["detector"],
        "model": data["model"],
        "pano_width": SRC_WIDTH,
        "pano_height": SRC_HEIGHT,
        "fps": fps,
        "total_frames": total_frames,
        "frames_processed": data["frames_processed"],
        "ball_detections": det_count,
        "interpolated_points": len(trajectory) - det_count,
        "trajectory_points": len(trajectory),
        "trajectory": trajectory,
    }

    with open(output_json, "w") as f:
        json.dump(result, f, indent=2)
    logger.info("Wrote trajectory: %s (%d detected + %d interpolated = %d total)",
                output_json, det_count, len(trajectory) - det_count, len(trajectory))


# ---- Pass 2: Render ----


def render_pass(
    video_path: Path,
    trajectory_json: Path,
    output_path: Path,
    crop_w: int = DEFAULT_CROP_W,
    crop_h: int = DEFAULT_CROP_H,
    vfov: float = DEFAULT_VFOV,
    follow_gain: float = DEFAULT_FOLLOW_GAIN,
    max_speed: float = DEFAULT_MAX_SPEED,
    dead_zone: float = DEFAULT_DEAD_ZONE_YAW,
    game_id: str | None = None,
):
    """Render dewarped ball-follow video from a trajectory."""
    logger.info("Render pass: %s → %s", video_path.name, output_path.name)

    # Load trajectory and interpolate to every frame
    with open(trajectory_json) as f:
        traj_data = json.load(f)

    trajectory = traj_data["trajectory"]
    traj_fps = traj_data["fps"]
    total_frames = traj_data.get("total_frames", 0)

    # Build sparse lookup from trajectory points
    sparse_lookup: dict[int, tuple[float, float, float]] = {}
    for pt in trajectory:
        sparse_lookup[pt["frame"]] = (pt["x"], pt["y"], pt["confidence"])

    # Interpolate to every frame for smooth camera motion
    traj_lookup: dict[int, tuple[float, float, float]] = {}
    sorted_traj_frames = sorted(sparse_lookup.keys())
    for i, fi in enumerate(sorted_traj_frames):
        traj_lookup[fi] = sparse_lookup[fi]
        # Fill frames between this point and the next
        if i + 1 < len(sorted_traj_frames):
            next_fi = sorted_traj_frames[i + 1]
            if next_fi - fi > 1:
                x0, y0, c0 = sparse_lookup[fi]
                x1, y1, c1 = sparse_lookup[next_fi]
                for mid_fi in range(fi + 1, next_fi):
                    t = (mid_fi - fi) / (next_fi - fi)
                    t = t * t * (3 - 2 * t)  # smoothstep
                    traj_lookup[mid_fi] = (
                        x0 + (x1 - x0) * t,
                        y0 + (y1 - y0) * t,
                        0.0,
                    )

    logger.info(
        "Trajectory: %d raw points → %d interpolated (every frame), detector=%s",
        len(trajectory), len(traj_lookup), traj_data.get("detector", "unknown"),
    )

    # Load field polygon for out-of-play detection
    field_polygon = None
    gid = game_id or traj_data.get("game_id")
    if gid:
        field_polygon = load_field_polygon(gid)

    # Create virtual camera
    camera = VirtualCamera(
        crop_w=crop_w,
        crop_h=crop_h,
        fps=traj_fps,
        field_polygon=field_polygon,
    )

    # Open input video
    in_container = av.open(str(video_path))
    in_video = in_container.streams.video[0]
    fps_frac = in_video.average_rate or 25
    fps = float(fps_frac)

    # Open output video
    out_container = av.open(str(output_path), mode="w")
    out_video = out_container.add_stream("libx264", rate=fps_frac)
    out_video.width = crop_w
    out_video.height = crop_h
    out_video.pix_fmt = "yuv420p"
    out_video.options = {"crf": "18", "preset": "medium"}

    # Copy audio stream if present
    audio_stream = None
    out_audio = None
    if len(in_container.streams.audio) > 0:
        audio_stream = in_container.streams.audio[0]
        out_audio = out_container.add_stream_from_template(audio_stream)

    frame_idx = 0
    t0 = time.time()

    for packet in in_container.demux(in_video):
        for av_frame in packet.decode():
            frame_bgr = av_frame.to_ndarray(format="bgr24")

            # Look up ball position — use both detections AND interpolated points
            if frame_idx in traj_lookup:
                bx, by, _conf = traj_lookup[frame_idx]
                camera.update(bx, by)
            else:
                camera.update(None, None)

            # Get dewarped viewport
            map_x, map_y = camera.get_remap()
            cropped = cv2.remap(
                frame_bgr, map_x, map_y,
                interpolation=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=(0, 0, 0),
            )

            # Encode output frame
            out_frame = av.VideoFrame.from_ndarray(cropped, format="bgr24")
            out_frame.pts = frame_idx
            for out_packet in out_video.encode(out_frame):
                out_container.mux(out_packet)

            frame_idx += 1
            if frame_idx % 500 == 0:
                elapsed = time.time() - t0
                rate = frame_idx / elapsed if elapsed > 0 else 0
                logger.info("  Rendered %d frames (%.1f f/s)", frame_idx, rate)

    # Flush encoder
    for out_packet in out_video.encode():
        out_container.mux(out_packet)

    # Copy audio packets (second pass)
    if audio_stream and out_audio:
        in_container.seek(0)
        for packet in in_container.demux(audio_stream):
            if packet.dts is None:
                continue
            packet.stream = out_audio
            try:
                out_container.mux(packet)
            except (av.InvalidDataError, av.error.FFmpegError):
                continue

    out_container.close()
    in_container.close()

    elapsed = time.time() - t0
    rate = frame_idx / elapsed if elapsed > 0 else 0
    logger.info("Render complete: %d frames in %.0fs (%.1f f/s)", frame_idx, elapsed, rate)
    logger.info("Output: %s", output_path)


# ---- CLI ----


def main():
    parser = argparse.ArgumentParser(
        description="Ball-follow virtual camera with dewarping"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # detect subcommand
    p_detect = subparsers.add_parser("detect", help="Dump all raw detections (Pass 1)")
    p_detect.add_argument("--video", type=Path, required=True, help="Input panoramic video")
    p_detect.add_argument(
        "--detector", choices=["onnx", "yolo"], required=True,
        help="Detection backend",
    )
    p_detect.add_argument("--model", type=Path, required=True, help="Model file path")
    p_detect.add_argument("-o", "--output", type=Path, help="Output raw detections JSON")
    p_detect.add_argument("--device", default="0", help="CUDA device or 'cpu'")
    p_detect.add_argument("--subsample", type=int, default=4, help="Process every Nth frame")
    p_detect.add_argument("--conf-threshold", type=float, default=0.15, help="Low threshold to capture all candidates")
    p_detect.add_argument("--game-id", type=str, help="Game ID (stored in output for track pass)")

    # track subcommand
    p_track = subparsers.add_parser("track", help="Build ball trajectory from raw detections (Pass 2)")
    p_track.add_argument("--detections", type=Path, required=True, help="Raw detections JSON from detect pass")
    p_track.add_argument("-o", "--output", type=Path, help="Output trajectory JSON")
    p_track.add_argument("--game-id", type=str, help="Game ID for field mask (overrides detect-time value)")

    # render subcommand
    p_render = subparsers.add_parser("render", help="Render dewarped output (Pass 3)")
    p_render.add_argument("--video", type=Path, required=True, help="Input panoramic video")
    p_render.add_argument("--trajectory", type=Path, required=True, help="Trajectory JSON from track pass")
    p_render.add_argument("-o", "--output", type=Path, help="Output video path")
    p_render.add_argument("--size", default="1920x1080", help="Output resolution WxH")
    p_render.add_argument("--vfov", type=float, default=DEFAULT_VFOV, help="Vertical FOV (degrees)")
    p_render.add_argument("--follow-gain", type=float, default=DEFAULT_FOLLOW_GAIN)
    p_render.add_argument("--max-speed", type=float, default=DEFAULT_MAX_SPEED)
    p_render.add_argument("--dead-zone", type=float, default=DEFAULT_DEAD_ZONE_YAW)
    p_render.add_argument("--game-id", type=str, help="Game ID for field boundary (out-of-play detection)")

    # run subcommand (detect + track + render)
    p_run = subparsers.add_parser("run", help="Full pipeline: detect → track → render")
    p_run.add_argument("--video", type=Path, required=True, help="Input panoramic video")
    p_run.add_argument("--detector", choices=["onnx", "yolo"], required=True)
    p_run.add_argument("--model", type=Path, required=True, help="Model file path")
    p_run.add_argument("-o", "--output", type=Path, help="Output video path")
    p_run.add_argument("--device", default="0", help="CUDA device or 'cpu'")
    p_run.add_argument("--subsample", type=int, default=4)
    p_run.add_argument("--conf-threshold", type=float, default=0.15)
    p_run.add_argument("--size", default="1920x1080", help="Output resolution WxH")
    p_run.add_argument("--vfov", type=float, default=DEFAULT_VFOV)
    p_run.add_argument("--follow-gain", type=float, default=DEFAULT_FOLLOW_GAIN)
    p_run.add_argument("--max-speed", type=float, default=DEFAULT_MAX_SPEED)
    p_run.add_argument("--dead-zone", type=float, default=DEFAULT_DEAD_ZONE_YAW)
    p_run.add_argument("--keep-intermediate", action="store_true", help="Keep intermediate files")
    p_run.add_argument("--game-id", type=str, help="Game ID for field mask filtering")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.command == "detect":
        output = args.output or args.video.with_name(
            args.video.stem + f"_dets_{args.detector}.json"
        )
        detect_pass(
            args.video, args.detector, args.model, output,
            device=args.device, subsample=args.subsample,
            conf_threshold=args.conf_threshold,
            game_id=args.game_id,
        )

    elif args.command == "track":
        output = args.output or args.detections.with_name(
            args.detections.stem.replace("_dets_", "_traj_") + ".json"
        )
        track_pass(
            args.detections, output,
            game_id=args.game_id,
        )

    elif args.command == "render":
        w, h = (int(x) for x in args.size.split("x"))
        output = args.output or args.video.with_name(
            args.video.stem + "_ballfollow.mp4"
        )
        render_pass(
            args.video, args.trajectory, output,
            crop_w=w, crop_h=h, vfov=args.vfov,
            follow_gain=args.follow_gain, max_speed=args.max_speed,
            dead_zone=args.dead_zone,
            game_id=args.game_id,
        )

    elif args.command == "run":
        w, h = (int(x) for x in args.size.split("x"))
        dets_path = args.video.with_name(
            args.video.stem + f"_dets_{args.detector}.json"
        )
        traj_path = args.video.with_name(
            args.video.stem + f"_traj_{args.detector}.json"
        )
        output = args.output or args.video.with_name(
            args.video.stem + f"_ballfollow_{args.detector}.mp4"
        )

        detect_pass(
            args.video, args.detector, args.model, dets_path,
            device=args.device, subsample=args.subsample,
            conf_threshold=args.conf_threshold,
            game_id=args.game_id,
        )

        track_pass(dets_path, traj_path, game_id=args.game_id)

        render_pass(
            args.video, traj_path, output,
            crop_w=w, crop_h=h, vfov=args.vfov,
            follow_gain=args.follow_gain, max_speed=args.max_speed,
            dead_zone=args.dead_zone,
            game_id=args.game_id,
        )

        if not args.keep_intermediate:
            dets_path.unlink(missing_ok=True)
            traj_path.unlink(missing_ok=True)
            logger.info("Cleaned up intermediate files")


if __name__ == "__main__":
    main()
