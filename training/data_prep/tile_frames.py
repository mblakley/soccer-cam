"""Tile panoramic frames into overlapping crops for annotation and training.

Slices 4096x1800 panoramic frames into a 6x2 grid of ~768x900 tiles
with configurable overlap. Makes the ball 6-20px per tile -- detectable by YOLO.
"""

import argparse
import logging
from pathlib import Path

import cv2

logger = logging.getLogger(__name__)

DEFAULT_COLS = 6
DEFAULT_ROWS = 2
DEFAULT_OVERLAP = 128
DEFAULT_JPEG_QUALITY = 95


def tile_frame(
    frame_path: Path,
    output_dir: Path,
    cols: int = DEFAULT_COLS,
    rows: int = DEFAULT_ROWS,
    overlap: int = DEFAULT_OVERLAP,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
) -> list[Path]:
    """Slice a single panoramic frame into overlapping tiles.

    Args:
        frame_path: Path to the input frame (JPEG)
        output_dir: Directory to write tiles
        cols: Number of tile columns
        rows: Number of tile rows
        overlap: Overlap in pixels between adjacent tiles
        jpeg_quality: JPEG compression quality

    Returns:
        List of paths to generated tile files
    """
    img = cv2.imread(str(frame_path))
    if img is None:
        raise ValueError(f"Cannot read image: {frame_path}")

    h, w = img.shape[:2]
    tile_w = (w + overlap * (cols - 1)) // cols
    tile_h = (h + overlap * (rows - 1)) // rows
    step_x = (w - tile_w) // max(1, cols - 1) if cols > 1 else 0
    step_y = (h - tile_h) // max(1, rows - 1) if rows > 1 else 0

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = frame_path.stem
    tile_paths = []

    for row in range(rows):
        for col in range(cols):
            x = col * step_x
            y = row * step_y
            x2 = min(x + tile_w, w)
            y2 = min(y + tile_h, h)

            tile = img[y:y2, x:x2]
            tile_name = f"{stem}_r{row}_c{col}.jpg"
            tile_path = output_dir / tile_name
            cv2.imwrite(str(tile_path), tile, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
            tile_paths.append(tile_path)

    return tile_paths


def tile_info(
    img_w: int,
    img_h: int,
    cols: int = DEFAULT_COLS,
    rows: int = DEFAULT_ROWS,
    overlap: int = DEFAULT_OVERLAP,
) -> list[tuple[int, int, int, int]]:
    """Compute tile bounding boxes without reading an image.

    Returns list of (x, y, w, h) for each tile.
    """
    tile_w = (img_w + overlap * (cols - 1)) // cols
    tile_h = (img_h + overlap * (rows - 1)) // rows
    step_x = (img_w - tile_w) // max(1, cols - 1) if cols > 1 else 0
    step_y = (img_h - tile_h) // max(1, rows - 1) if rows > 1 else 0

    tiles = []
    for row in range(rows):
        for col in range(cols):
            x = col * step_x
            y = row * step_y
            w = min(tile_w, img_w - x)
            h = min(tile_h, img_h - y)
            tiles.append((x, y, w, h))
    return tiles


def main():
    parser = argparse.ArgumentParser(
        description="Tile panoramic frames into overlapping crops"
    )
    parser.add_argument("input", type=Path, help="Input frame or directory of frames")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("training/data/tiles"),
        help="Output directory",
    )
    parser.add_argument(
        "--cols", type=int, default=DEFAULT_COLS, help="Number of tile columns"
    )
    parser.add_argument(
        "--rows", type=int, default=DEFAULT_ROWS, help="Number of tile rows"
    )
    parser.add_argument(
        "--overlap",
        type=int,
        default=DEFAULT_OVERLAP,
        help="Pixel overlap between tiles",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    input_path = args.input
    if input_path.is_dir():
        frames = sorted(input_path.glob("*.jpg"))
        logger.info("Found %d frames in %s", len(frames), input_path)
    else:
        frames = [input_path]

    total = 0
    for f in frames:
        tiles = tile_frame(f, args.output / f.stem, args.cols, args.rows, args.overlap)
        total += len(tiles)

    logger.info("Generated %d tiles total", total)


if __name__ == "__main__":
    main()
