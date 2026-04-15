"""Per-game field boundary detection — identify the playing field polygon.

Three-tiered detection (run once per game, result cached in manifest metadata):
  1. ONNX keypoint model — detects 10 field boundary keypoints
  2. Sonnet vision — asks Claude to trace the field boundary
  3. Human flag — marks game for human annotation

The polygon is stored as a list of [x, y] points in panoramic pixel
coordinates, compatible with cv2.pointPolygonTest.

Pull-local-process-push pattern:
  - Needs pack files to reconstruct panoramic frames
  - Writes to manifest.db metadata table
"""

import json
import logging
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np

from training.data_prep.game_manifest import GameManifest
from training.tasks import register_task
from training.tasks.io import TaskIO

logger = logging.getLogger(__name__)

# Tile layout constants (must match trajectory_gaps.py)
TILE_SIZE = 640
TILE_COLS = 7
TILE_ROWS = 3
STEP_X = 576  # (pano_width - TILE_SIZE) / (TILE_COLS - 1)
STEP_Y = 580  # (pano_height - TILE_SIZE) / (TILE_ROWS - 1)

# Validation thresholds
MIN_KEYPOINTS = 6  # out of 10
MIN_POLYGON_AREA = 2_000_000  # sq pixels (real field is ~4M in panoramic)


@register_task("field_boundary")
def run_field_boundary(
    *,
    item: dict,
    local_work_dir: Path,
    server_share: str = "",
    local_models_dir: Path | None = None,
) -> dict:
    """Pipeline task: detect field boundary for a game."""
    game_id = item["game_id"]

    from training.pipeline.config import load_config

    load_config()

    task_io = TaskIO(game_id, local_work_dir, server_share)
    task_io.ensure_space(needed_gb=3)
    task_io.pull_manifest()

    manifest = GameManifest(task_io.local_game)
    manifest.open(create=False)

    try:
        # Pull packs needed for panoramic reconstruction
        from training.tasks.sonnet_qa import _pull_selective_packs

        # Find which packs we need for the detection frame
        segment, frame_idx = _pick_detection_frame(manifest)
        if segment:
            needed = set()
            for row in range(TILE_ROWS):
                for col in range(TILE_COLS):
                    tile = manifest.get_tile(segment, frame_idx, row, col)
                    if tile and tile.get("pack_file"):
                        needed.add(tile["pack_file"])
            if needed:
                _pull_selective_packs(task_io, needed)

        result = detect_field_boundary(manifest, task_io, force=True)
    finally:
        manifest.close()

    task_io.push_manifest()

    logger.info(
        "Field boundary task complete for %s: source=%s",
        game_id,
        result.get("source", "none"),
    )
    return result


def detect_field_boundary(
    manifest: GameManifest,
    task_io: TaskIO,
    *,
    force: bool = False,
) -> dict:
    """Run field boundary detection and store polygon in manifest metadata.

    Returns summary dict with detection results.
    """
    existing = manifest.get_metadata("field_boundary")
    if existing:
        fb = json.loads(existing)
        # Never overwrite human-confirmed polygons
        if fb.get("source") == "human":
            logger.info(
                "Field boundary for %s is human-confirmed, skipping",
                manifest.game_id,
            )
            return fb
        if not force:
            logger.info(
                "Field boundary already detected for %s, skipping",
                manifest.game_id,
            )
            return fb

    logger.info("Starting field boundary detection for %s", manifest.game_id)

    # Pick a good frame to detect from (mid-game, during active play if phases known)
    segment, frame_idx = _pick_detection_frame(manifest)
    if segment is None:
        logger.warning("No segments found for %s", manifest.game_id)
        return _store_no_polygon(manifest)

    # Reconstruct panoramic frame from tiles
    # NOTE: Tiles are already right-side up (flipping happens during tiling).
    # needs_flip only applies when reading from original video files.
    packs_dir = task_io.local_packs
    pano = reconstruct_panoramic(manifest, segment, frame_idx, packs_dir)
    if pano is None:
        logger.warning("Could not reconstruct panoramic for %s", manifest.game_id)
        return _store_no_polygon(manifest)

    # Tier 1: ONNX keypoint detection
    result = _detect_with_onnx(pano, manifest)
    if result:
        return result

    # Tier 2: Sonnet vision
    result = _detect_with_sonnet(pano, manifest, task_io)
    if result:
        return result

    # No auto-detection succeeded — flag for human
    logger.warning(
        "Field boundary detection failed for %s — flagged for human review",
        manifest.game_id,
    )
    return _store_no_polygon(manifest)


# ------------------------------------------------------------------
# Frame selection
# ------------------------------------------------------------------


def _pick_detection_frame(manifest: GameManifest) -> tuple[str | None, int]:
    """Pick a frame for field detection — mid-game, during active play."""
    segments = manifest.get_segments()
    if not segments:
        return None, 0

    segments.sort()

    # If phases are known, pick from first_half
    play_ranges = manifest.get_play_frame_ranges()
    if play_ranges:
        p = play_ranges[0]  # first_half
        seg = p["segment_start"]
        fi = (p["frame_start"] + p["frame_end"]) // 2
        # Snap to frame interval
        fi = (fi // 4) * 4
        return seg, fi

    # Otherwise pick middle of the middle segment
    mid_seg = segments[len(segments) // 2]
    seg_info = manifest.conn.execute(
        "SELECT frame_min, frame_max FROM segments WHERE segment = ?",
        (mid_seg,),
    ).fetchone()
    if seg_info:
        fi = (seg_info[0] + seg_info[1]) // 2
        fi = (fi // 4) * 4
        return mid_seg, fi

    return segments[0], 0


# ------------------------------------------------------------------
# Panoramic reconstruction
# ------------------------------------------------------------------


def reconstruct_panoramic(
    manifest: GameManifest,
    segment: str,
    frame_idx: int,
    packs_dir: Path,
) -> np.ndarray | None:
    """Reconstruct a full panoramic frame from tile packs.

    Reads all 21 tiles (7 cols x 3 rows) and stitches them into the
    original panoramic layout.

    Returns BGR image or None if tiles are unavailable.
    """
    # Determine panoramic dimensions from tile layout
    pano_w = STEP_X * (TILE_COLS - 1) + TILE_SIZE  # 576*6 + 640 = 4096
    pano_h = STEP_Y * (TILE_ROWS - 1) + TILE_SIZE  # 580*2 + 640 = 1800

    pano = np.zeros((pano_h, pano_w, 3), dtype=np.uint8)
    tiles_read = 0

    for row in range(TILE_ROWS):
        for col in range(TILE_COLS):
            jpeg_bytes = _read_tile(manifest, segment, frame_idx, row, col, packs_dir)
            if jpeg_bytes is None:
                continue

            img_arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
            if img_arr.size == 0:
                continue
            img = cv2.imdecode(img_arr, cv2.IMREAD_COLOR)
            if img is None:
                continue

            if img.shape[:2] != (TILE_SIZE, TILE_SIZE):
                img = cv2.resize(img, (TILE_SIZE, TILE_SIZE))

            y = row * STEP_Y
            x = col * STEP_X
            pano[y : y + TILE_SIZE, x : x + TILE_SIZE] = img
            tiles_read += 1

    if tiles_read < 10:  # need at least ~half the tiles
        logger.warning(
            "Only %d/21 tiles available for panoramic reconstruction", tiles_read
        )
        return None

    logger.info("Reconstructed panoramic from %d/21 tiles", tiles_read)
    return pano


def _read_tile(
    manifest: GameManifest,
    segment: str,
    frame_idx: int,
    row: int,
    col: int,
    packs_dir: Path,
) -> bytes | None:
    """Read tile JPEG bytes from pack files (local or server path)."""
    tile = manifest.get_tile(segment, frame_idx, row, col)
    if not tile or not tile.get("pack_file"):
        return None

    # Try packs_dir first, then original D: path, then F: archive
    pack_name = Path(tile["pack_file"]).name
    local_pack = packs_dir / pack_name
    if not local_pack.exists():
        local_pack = Path(tile["pack_file"])
    if not local_pack.exists():
        try:
            from training.pipeline.config import load_config

            cfg = load_config()
            archive = Path(cfg.paths.archive.tile_packs) / manifest.game_id / pack_name
            if archive.exists():
                local_pack = archive
            else:
                return None
        except Exception:
            return None

    try:
        with open(local_pack, "rb") as f:
            f.seek(tile["pack_offset"])
            data = f.read(tile["pack_size"])
            return data if data else None
    except Exception:
        return None


# ------------------------------------------------------------------
# Tier 1: ONNX keypoint detection
# ------------------------------------------------------------------


def _detect_with_onnx(pano: np.ndarray, manifest: GameManifest) -> dict | None:
    """Detect field boundary using ONNX keypoint model.

    Returns result dict if successful, None if detection fails validation.
    """
    try:
        from training.inference.external_field_detector import (
            create_field_session,
            detect_field_keypoints,
            build_field_polygon,
        )
    except ImportError as e:
        logger.warning("ONNX field detector not available: %s", e)
        return None

    try:
        sess = create_field_session()
    except Exception as e:
        logger.warning("Could not create field detection session: %s", e)
        return None

    keypoints = detect_field_keypoints(pano, sess)
    detected = sum(1 for kp in keypoints if kp[0] is not None)
    logger.info("ONNX field keypoints: %d/10 detected", detected)

    if detected < MIN_KEYPOINTS:
        logger.info(
            "Too few keypoints (%d < %d), falling through to Sonnet",
            detected,
            MIN_KEYPOINTS,
        )
        return None

    polygon_np = build_field_polygon(keypoints)
    if polygon_np is None:
        return None

    # Convert to [[x, y], ...] list
    polygon = [[float(p[0]), float(p[1])] for p in polygon_np]

    # Validate polygon makes physical sense
    if not _validate_polygon(polygon, pano.shape[1], pano.shape[0]):
        logger.warning("ONNX polygon failed validation for %s", manifest.game_id)
        return None

    # Compute confidence as mean of detected keypoint scores
    scores = [kp[2] for kp in keypoints if kp[0] is not None]
    confidence = sum(scores) / len(scores) if scores else 0.0

    result = {
        "polygon": polygon,
        "source": "onnx",
        "confidence": round(confidence, 3),
        "keypoints_detected": detected,
        "needs_human_review": False,
        "created_at": time.time(),
    }

    manifest.set_metadata("field_boundary", json.dumps(result))
    logger.info(
        "ONNX field boundary stored for %s: %d points, confidence=%.2f",
        manifest.game_id,
        len(polygon),
        confidence,
    )
    return result


# ------------------------------------------------------------------
# Tier 2: Sonnet vision
# ------------------------------------------------------------------


def _detect_with_sonnet(
    pano: np.ndarray,
    manifest: GameManifest,
    task_io: TaskIO,
) -> dict | None:
    """Ask Sonnet to trace the field boundary on a panoramic frame."""
    h, w = pano.shape[:2]

    # Save at half resolution for Sonnet
    half_w, half_h = w // 2, h // 2
    small = cv2.resize(pano, (half_w, half_h))

    img_path = task_io.local_game / "field_boundary_pano.jpg"
    img_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(img_path), small, [cv2.IMWRITE_JPEG_QUALITY, 85])

    prompt = (
        f"Read the image at {img_path} and analyze it. "
        f"This is a {half_w}x{half_h} pixel panoramic view of a soccer field from a "
        f"fisheye camera mounted on the side. The field boundary is CURVED due to "
        f"barrel distortion — it is NOT straight lines.\n\n"
        f"Trace the field boundary as a polygon. Return approximately 10-16 points:\n"
        f"- First trace the NEAR sideline (bottom of image) from LEFT to RIGHT\n"
        f"- Then trace the FAR sideline (top of image) from RIGHT to LEFT\n"
        f"- Points should follow the curved edges of the green playing field\n"
        f"- Include the corners where sidelines meet end lines\n\n"
        f'Respond with ONLY a JSON object: {{"polygon": [[x1,y1], [x2,y2], ...]}}\n'
        f"Coordinates should be in the displayed image pixel space ({half_w}x{half_h})."
    )

    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                prompt,
                "--output-format",
                "json",
                "--model",
                "sonnet",
                "--allowedTools",
                "Read",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            logger.warning(
                "Sonnet field detection failed (rc=%d): %s",
                result.returncode,
                result.stderr[:200],
            )
            return None

        from training.tasks.sonnet_qa import _extract_json

        data = _extract_json(result.stdout.strip())
        if not data or "polygon" not in data:
            logger.warning("Could not parse Sonnet field detection response")
            return None

        # Scale from half-res back to full panoramic
        polygon = [[float(p[0]) * 2, float(p[1]) * 2] for p in data["polygon"]]

        if not _validate_polygon(polygon, w, h):
            logger.warning("Sonnet polygon failed validation for %s", manifest.game_id)
            return None

        fb_result = {
            "polygon": polygon,
            "source": "sonnet",
            "confidence": 0.7,
            "needs_human_review": True,  # Sonnet polygons should be reviewed
            "created_at": time.time(),
        }

        manifest.set_metadata("field_boundary", json.dumps(fb_result))
        logger.info(
            "Sonnet field boundary stored for %s: %d points",
            manifest.game_id,
            len(polygon),
        )
        return fb_result

    except subprocess.TimeoutExpired:
        logger.warning("Sonnet field detection timed out")
        return None
    except Exception as e:
        logger.warning("Sonnet field detection error: %s", e)
        return None


# ------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------


def _validate_polygon(polygon: list[list[float]], frame_w: int, frame_h: int) -> bool:
    """Validate that a polygon makes physical sense as a field boundary.

    Checks:
    - Enough points (>= 4)
    - All points within frame bounds
    - Polygon area exceeds minimum (not degenerate)
    - Near sideline Y > far sideline Y (correct orientation)
    """
    if len(polygon) < 4:
        logger.debug("Polygon too few points: %d", len(polygon))
        return False

    # Check bounds
    for x, y in polygon:
        if x < -100 or x > frame_w + 100 or y < -100 or y > frame_h + 100:
            logger.debug("Polygon point out of bounds: (%.0f, %.0f)", x, y)
            return False

    # Check area
    pts = np.array(polygon, dtype=np.float32)
    area = cv2.contourArea(pts)
    if area < MIN_POLYGON_AREA:
        logger.debug("Polygon area too small: %.0f < %d", area, MIN_POLYGON_AREA)
        return False

    # Check orientation: near sideline (first half of points) should have larger Y
    # than far sideline (second half)
    mid = len(polygon) // 2
    near_y = np.mean([p[1] for p in polygon[:mid]])
    far_y = np.mean([p[1] for p in polygon[mid:]])
    if near_y < far_y:
        logger.debug(
            "Polygon orientation wrong: near_y=%.0f < far_y=%.0f", near_y, far_y
        )
        return False

    return True


# ------------------------------------------------------------------
# No polygon
# ------------------------------------------------------------------


def _store_no_polygon(manifest: GameManifest) -> dict:
    """Store a placeholder indicating no polygon was detected."""
    result = {
        "polygon": None,
        "source": "none",
        "confidence": 0.0,
        "needs_human_review": True,
        "created_at": time.time(),
    }
    manifest.set_metadata("field_boundary", json.dumps(result))
    return result
