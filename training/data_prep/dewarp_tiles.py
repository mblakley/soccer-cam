"""Generate dewarped (rectilinear) tile crops from fisheye panoramic frames.

Reprojects regions of the cylindrical panoramic image into rectilinear
"virtual camera" views, making the ball appear round and larger at the
edges of the field where fisheye compression is strongest.

Uses the same cylindrical-to-rectilinear reprojection math as the
dewarp_viewer.html (Three.js shader), ported to Python/OpenCV.

Camera parameters from the Dahua B180 fisheye panoramic camera.

Usage:
    python -m training.data_prep.dewarp_tiles \
        --input F:/training_data/frames/flash__09.30.2024_vs_Chili_home \
        --output F:/training_data/tiles_640_dewarped/flash__09.30.2024_vs_Chili_home \
        --views edges
"""

import argparse
import logging
import math
import time
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# ---- Camera calibration (from dewarp_viewer.html) ----
SRC_HFOV = math.pi  # 180 degrees horizontal field of view
SRC_WIDTH = 4096
SRC_HEIGHT = 1800
CAMERA_TILT = 0.55  # radians (~31 degrees down from horizontal)
SRC_VRANGE = SRC_HFOV * (SRC_HEIGHT / SRC_WIDTH)  # vertical range on unit cylinder

# ---- Output tile size ----
TILE_SIZE = 640


def _build_dewarp_map(
    yaw: float,
    pitch: float,
    vfov_deg: float,
    out_w: int = TILE_SIZE,
    out_h: int = TILE_SIZE,
) -> tuple[np.ndarray, np.ndarray]:
    """Build OpenCV remap arrays for a virtual camera view.

    Args:
        yaw: Pan angle in radians (0 = center, negative = left, positive = right)
        pitch: Tilt angle in radians (0 = horizontal, positive = look down)
        vfov_deg: Vertical field of view in degrees
        out_w: Output width in pixels
        out_h: Output height in pixels

    Returns:
        (map_x, map_y) arrays for cv2.remap(), mapping output pixels to
        source panoramic pixel coordinates.
    """
    vfov = math.radians(vfov_deg)
    aspect = out_w / out_h
    tan_half = math.tan(vfov * 0.5)

    # Build NDC grid [-1, 1] for output image
    u = np.linspace(-1, 1, out_w, dtype=np.float32)
    v = np.linspace(-1, 1, out_h, dtype=np.float32)
    ndc_x, ndc_y = np.meshgrid(u, v)

    # Ray direction in virtual camera local space (looking along +Z)
    ray_x = ndc_x * tan_half * aspect
    ray_y = ndc_y * tan_half
    ray_z = np.ones_like(ray_x)

    # Normalize
    ray_len = np.sqrt(ray_x**2 + ray_y**2 + ray_z**2)
    ray_x /= ray_len
    ray_y /= ray_len
    ray_z /= ray_len

    # Rotate by pitch (around X axis, in world space)
    cp, sp = math.cos(pitch), math.sin(pitch)
    r1_x = ray_x
    r1_y = cp * ray_y - sp * ray_z
    r1_z = sp * ray_y + cp * ray_z

    # Rotate by yaw (around world Y axis)
    cy, sy = math.cos(yaw), math.sin(yaw)
    r2_x = cy * r1_x + sy * r1_z
    r2_y = r1_y
    r2_z = -sy * r1_x + cy * r1_z

    # Transform from world space to camera space (undo camera tilt)
    ct, st = math.cos(CAMERA_TILT), math.sin(CAMERA_TILT)
    r3_x = r2_x
    r3_y = ct * r2_y + st * r2_z
    r3_z = -st * r2_y + ct * r2_z

    # Convert to cylindrical panorama coordinates
    theta = np.arctan2(r3_x, r3_z)  # azimuth angle
    y_cyl = r3_y / np.sqrt(r3_x**2 + r3_z**2)  # height on unit cylinder

    # Map to source pixel coordinates
    src_u = theta / SRC_HFOV + 0.5
    src_v = y_cyl / SRC_VRANGE + 0.5

    map_x = (src_u * SRC_WIDTH).astype(np.float32)
    map_y = (src_v * SRC_HEIGHT).astype(np.float32)

    # Mark out-of-bounds pixels
    oob = (src_u < 0) | (src_u > 1) | (src_v < 0) | (src_v > 1)
    map_x[oob] = -1
    map_y[oob] = -1

    return map_x, map_y


def pano_to_virtual(
    pano_x: float,
    pano_y: float,
    yaw: float,
    pitch: float,
    vfov_deg: float,
    out_w: int = TILE_SIZE,
    out_h: int = TILE_SIZE,
) -> tuple[float, float] | None:
    """Convert panoramic pixel coordinates to virtual camera pixel coordinates.

    Returns (vx, vy) in the virtual camera output, or None if the point
    is outside the virtual camera's field of view.
    """
    # Panoramic pixel to cylindrical coords
    src_u = pano_x / SRC_WIDTH
    src_v = pano_y / SRC_HEIGHT
    theta = (src_u - 0.5) * SRC_HFOV
    y_cyl = (src_v - 0.5) * SRC_VRANGE

    # Cylindrical to camera-space ray
    r3_x = math.sin(theta)
    r3_z = math.cos(theta)
    r3_y = y_cyl * math.sqrt(r3_x**2 + r3_z**2)

    # Camera space to world space (apply camera tilt)
    ct, st = math.cos(CAMERA_TILT), math.sin(CAMERA_TILT)
    r2_x = r3_x
    r2_y = ct * r3_y - st * r3_z
    r2_z = st * r3_y + ct * r3_z

    # Undo yaw rotation
    cy, sy = math.cos(yaw), math.sin(yaw)
    r1_x = cy * r2_x - sy * r2_z
    r1_y = r2_y
    r1_z = sy * r2_x + cy * r2_z

    # Undo pitch rotation
    cp, sp = math.cos(pitch), math.sin(pitch)
    ray_x = r1_x
    ray_y = cp * r1_y + sp * r1_z
    ray_z = -sp * r1_y + cp * r1_z

    # Project to virtual camera image plane
    if ray_z <= 0:
        return None

    vfov = math.radians(vfov_deg)
    aspect = out_w / out_h
    tan_half = math.tan(vfov * 0.5)

    ndc_x = ray_x / (ray_z * tan_half * aspect)
    ndc_y = ray_y / (ray_z * tan_half)

    if abs(ndc_x) > 1 or abs(ndc_y) > 1:
        return None

    vx = (ndc_x + 1) * 0.5 * out_w
    vy = (ndc_y + 1) * 0.5 * out_h
    return vx, vy


# ---- Predefined virtual camera views for edge regions ----

# Each view: (name_suffix, yaw_rad, pitch_rad, vfov_deg)
# These cover the field edges where the ball is small in standard tiles
EDGE_VIEWS = [
    # Left edge - far field (goal area)
    ("dw_L_far", -1.30, 0.35, 30),
    ("dw_L_mid", -1.10, 0.45, 30),
    # Right edge - far field (goal area, sun side)
    ("dw_R_far", 1.30, 0.35, 30),
    ("dw_R_mid", 1.10, 0.45, 30),
    # Left edge - near field
    ("dw_L_near", -1.30, 0.70, 30),
    # Right edge - near field
    ("dw_R_near", 1.30, 0.70, 30),
]

# Center views (less distortion, but useful for completeness)
CENTER_VIEWS = [
    ("dw_C_far", 0.0, 0.30, 35),
    ("dw_C_mid", 0.0, 0.55, 35),
    ("dw_C_near", 0.0, 0.80, 35),
]


def dewarp_frame(
    frame: np.ndarray,
    views: list[tuple[str, float, float, float]],
    precomputed_maps: dict | None = None,
) -> list[tuple[str, np.ndarray]]:
    """Generate dewarped tiles from a panoramic frame.

    Args:
        frame: Panoramic image (H x W x 3), expected 1800 x 4096
        views: List of (name, yaw, pitch, vfov_deg) virtual camera definitions
        precomputed_maps: Optional dict of name -> (map_x, map_y) for reuse

    Returns:
        List of (name, tile_image) tuples, each tile is TILE_SIZE x TILE_SIZE
    """
    results = []
    for name, yaw, pitch, vfov in views:
        if precomputed_maps and name in precomputed_maps:
            map_x, map_y = precomputed_maps[name]
        else:
            map_x, map_y = _build_dewarp_map(yaw, pitch, vfov)
            if precomputed_maps is not None:
                precomputed_maps[name] = (map_x, map_y)

        tile = cv2.remap(
            frame,
            map_x,
            map_y,
            interpolation=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(0, 0, 0),
        )
        results.append((name, tile))

    return results


def dewarp_segment(
    frames_dir: Path,
    output_dir: Path,
    segment_prefix: str,
    views: list[tuple[str, float, float, float]] | None = None,
    frame_interval: int = 8,
) -> dict[str, int]:
    """Generate dewarped tiles for all frames in a segment.

    Args:
        frames_dir: Directory containing panoramic frame images
        output_dir: Output directory for dewarped tiles
        segment_prefix: Segment filename prefix to filter frames
        views: Virtual camera views to generate (default: EDGE_VIEWS)
        frame_interval: Only process every Nth frame (for speed)

    Returns:
        Stats dict with counts.
    """
    if views is None:
        views = EDGE_VIEWS

    output_dir.mkdir(parents=True, exist_ok=True)

    # Find panoramic frames for this segment
    frame_paths = sorted(frames_dir.glob(f"{segment_prefix}*.jpg"))
    if not frame_paths:
        # Try with wildcard matching
        frame_paths = sorted(
            p for p in frames_dir.glob("*.jpg") if segment_prefix in p.name
        )

    logger.info(
        "Found %d frames matching segment '%s'", len(frame_paths), segment_prefix
    )

    stats = {"frames_processed": 0, "tiles_generated": 0}
    precomputed_maps: dict[str, tuple] = {}
    start = time.time()

    for frame_path in frame_paths:
        frame = cv2.imread(str(frame_path))
        if frame is None:
            continue

        stats["frames_processed"] += 1
        stem = frame_path.stem  # e.g., "18.02.52-18.19.36[F][0@0][236858]_frame_002400"

        tiles = dewarp_frame(frame, views, precomputed_maps)
        for name, tile in tiles:
            out_path = output_dir / f"{stem}_{name}.jpg"
            cv2.imwrite(str(out_path), tile, [cv2.IMWRITE_JPEG_QUALITY, 90])
            stats["tiles_generated"] += 1

        if stats["frames_processed"] % 100 == 0:
            elapsed = time.time() - start
            rate = stats["frames_processed"] / elapsed
            logger.info(
                "%d frames, %d tiles (%.1f frames/s)",
                stats["frames_processed"],
                stats["tiles_generated"],
                rate,
            )

    elapsed = time.time() - start
    logger.info(
        "DONE: %d frames, %d tiles in %.0fs",
        stats["frames_processed"],
        stats["tiles_generated"],
        elapsed,
    )
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Generate dewarped tiles from panoramic frames"
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Directory containing panoramic frame images",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output directory for dewarped tiles",
    )
    parser.add_argument(
        "--segment",
        type=str,
        default="",
        help="Segment prefix to filter frames (empty = all)",
    )
    parser.add_argument(
        "--views",
        choices=["edges", "center", "all"],
        default="edges",
        help="Which virtual camera views to generate",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if args.views == "edges":
        views = EDGE_VIEWS
    elif args.views == "center":
        views = CENTER_VIEWS
    else:
        views = EDGE_VIEWS + CENTER_VIEWS

    dewarp_segment(args.input, args.output, args.segment, views)


if __name__ == "__main__":
    main()
