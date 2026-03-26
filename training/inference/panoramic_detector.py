"""Full-frame panoramic ball detection using the temporal heatmap model.

For each panoramic frame:
1. Tiles into 7x3 grid (reuses tile_frames.py layout)
2. For each tile position, loads current + previous + next frame tiles
3. Runs temporal model → heatmap per tile
4. Stitches heatmaps into full panoramic heatmap (overlap zones averaged)
5. Finds peaks → ball candidates with confidence
"""

import argparse
import logging
from pathlib import Path

import cv2
import numpy as np
import torch

logger = logging.getLogger(__name__)

# Tile grid constants (match tile_frames.py)
PANO_W = 4096
PANO_H = 1800
TILE_SIZE = 640
N_COLS = 7
N_ROWS = 3
STEP_X = (PANO_W - TILE_SIZE) // (N_COLS - 1)  # 576
STEP_Y = (PANO_H - TILE_SIZE) // (N_ROWS - 1)  # 580

# Peak detection
MIN_PEAK_VALUE = 0.3  # Minimum heatmap confidence for a detection
PEAK_RADIUS = 10  # Non-maximum suppression radius in pixels


def _extract_tile(frame: np.ndarray, row: int, col: int) -> np.ndarray:
    """Extract a tile from a panoramic frame."""
    x = col * STEP_X
    y = row * STEP_Y
    x2 = min(x + TILE_SIZE, frame.shape[1])
    y2 = min(y + TILE_SIZE, frame.shape[0])
    tile = frame[y:y2, x:x2]
    # Pad if needed
    if tile.shape[0] < TILE_SIZE or tile.shape[1] < TILE_SIZE:
        padded = np.zeros((TILE_SIZE, TILE_SIZE, 3), dtype=tile.dtype)
        padded[: tile.shape[0], : tile.shape[1]] = tile
        return padded
    return tile


def _stitch_heatmaps(
    tile_heatmaps: dict[tuple[int, int], np.ndarray],
) -> np.ndarray:
    """Stitch tile heatmaps into a full panoramic heatmap.

    Overlap zones are averaged.
    """
    pano_heatmap = np.zeros((PANO_H, PANO_W), dtype=np.float32)
    weight_map = np.zeros((PANO_H, PANO_W), dtype=np.float32)

    for (row, col), heatmap in tile_heatmaps.items():
        x = col * STEP_X
        y = row * STEP_Y
        h, w = heatmap.shape
        x2 = min(x + w, PANO_W)
        y2 = min(y + h, PANO_H)
        hw = x2 - x
        hh = y2 - y

        pano_heatmap[y:y2, x:x2] += heatmap[:hh, :hw]
        weight_map[y:y2, x:x2] += 1.0

    # Average overlapping regions
    mask = weight_map > 0
    pano_heatmap[mask] /= weight_map[mask]

    return pano_heatmap


def _find_peaks(
    heatmap: np.ndarray,
    min_value: float = MIN_PEAK_VALUE,
    radius: int = PEAK_RADIUS,
) -> list[tuple[int, int, float]]:
    """Find peaks in a heatmap using non-maximum suppression.

    Returns list of (x, y, confidence) sorted by confidence descending.
    """
    # Apply NMS via max pooling
    from scipy.ndimage import maximum_filter

    local_max = maximum_filter(heatmap, size=2 * radius + 1)
    peaks_mask = (heatmap == local_max) & (heatmap >= min_value)

    ys, xs = np.where(peaks_mask)
    confidences = heatmap[ys, xs]

    # Sort by confidence descending
    order = np.argsort(-confidences)
    results = [(int(xs[i]), int(ys[i]), float(confidences[i])) for i in order]

    return results


def detect_ball_panoramic(
    model: torch.nn.Module,
    prev_frame: np.ndarray,
    curr_frame: np.ndarray,
    next_frame: np.ndarray,
    device: torch.device,
    min_confidence: float = MIN_PEAK_VALUE,
) -> tuple[np.ndarray, list[tuple[int, int, float]]]:
    """Detect ball in a panoramic frame using the temporal model.

    Args:
        model: Trained TemporalBallNet
        prev_frame: Previous panoramic frame (BGR, H×W×3)
        curr_frame: Current panoramic frame (BGR, H×W×3)
        next_frame: Next panoramic frame (BGR, H×W×3)
        device: Torch device
        min_confidence: Minimum peak confidence

    Returns:
        (panoramic_heatmap, detections) where detections is list of (x, y, conf)
    """
    model.eval()
    tile_heatmaps: dict[tuple[int, int], np.ndarray] = {}

    with torch.no_grad():
        for row in range(N_ROWS):
            for col in range(N_COLS):
                # Extract tiles from all 3 frames
                prev_tile = _extract_tile(prev_frame, row, col)
                curr_tile = _extract_tile(curr_frame, row, col)
                next_tile = _extract_tile(next_frame, row, col)

                # Convert BGR→RGB, normalize to [0,1], stack to 9-channel
                tiles = []
                for tile in [prev_tile, curr_tile, next_tile]:
                    t = cv2.cvtColor(tile, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
                    tiles.append(t.transpose(2, 0, 1))  # (3, H, W)

                stacked = np.concatenate(tiles, axis=0)  # (9, H, W)
                tensor = (
                    torch.from_numpy(stacked).unsqueeze(0).to(device)
                )  # (1, 9, H, W)

                # Predict
                heatmap = model(tensor)  # (1, 1, H, W)
                heatmap_np = heatmap[0, 0].cpu().numpy()

                tile_heatmaps[(row, col)] = heatmap_np

    # Stitch and find peaks
    pano_heatmap = _stitch_heatmaps(tile_heatmaps)
    detections = _find_peaks(pano_heatmap, min_value=min_confidence)

    return pano_heatmap, detections


def run_on_frames(
    model_path: Path,
    frames_dir: Path,
    output_dir: Path,
    device: str = "0",
    min_confidence: float = MIN_PEAK_VALUE,
):
    """Run panoramic detection on a directory of frames.

    Args:
        model_path: Path to trained model weights (.pt)
        frames_dir: Directory with panoramic frame JPEGs
        output_dir: Directory for detection results
        device: CUDA device or "cpu"
        min_confidence: Minimum detection confidence
    """
    from training.train_temporal import TemporalBallNet

    # Setup
    dev = torch.device(f"cuda:{device}" if device != "cpu" else "cpu")
    model = TemporalBallNet(in_channels=9)
    model.load_state_dict(torch.load(model_path, map_location=dev, weights_only=True))
    model = model.to(dev)
    model.eval()

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load frames sorted by name
    frame_paths = sorted(frames_dir.glob("*.jpg"))
    if len(frame_paths) < 3:
        logger.error("Need at least 3 frames, found %d", len(frame_paths))
        return

    logger.info("Processing %d frames from %s", len(frame_paths), frames_dir)

    results = []
    for i in range(1, len(frame_paths) - 1):
        prev_frame = cv2.imread(str(frame_paths[i - 1]))
        curr_frame = cv2.imread(str(frame_paths[i]))
        next_frame = cv2.imread(str(frame_paths[i + 1]))

        if any(f is None for f in [prev_frame, curr_frame, next_frame]):
            logger.warning("Skipping frame %d — could not read", i)
            continue

        _heatmap, detections = detect_ball_panoramic(
            model, prev_frame, curr_frame, next_frame, dev, min_confidence
        )

        frame_name = frame_paths[i].stem
        results.append({"frame": frame_name, "detections": detections})

        if detections:
            logger.info(
                "  %s: %d detections (best: x=%d y=%d conf=%.2f)",
                frame_name,
                len(detections),
                detections[0][0],
                detections[0][1],
                detections[0][2],
            )

    # Write results
    import json

    results_path = output_dir / "detections.json"
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2)

    logger.info(
        "Wrote %d frame results to %s",
        len(results),
        results_path,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Panoramic ball detection using temporal model"
    )
    parser.add_argument("--model", type=Path, required=True, help="Model weights path")
    parser.add_argument(
        "--frames", type=Path, required=True, help="Panoramic frames directory"
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("detections/"),
        help="Output directory",
    )
    parser.add_argument("--device", default="0")
    parser.add_argument("--min-confidence", type=float, default=MIN_PEAK_VALUE)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    run_on_frames(
        args.model, args.frames, args.output, args.device, args.min_confidence
    )


if __name__ == "__main__":
    main()
