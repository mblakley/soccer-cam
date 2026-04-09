"""Tile task — extract frames from video and tile into pack files.

Pull-local-process-push pattern:
  - Pull: copy video file(s) from server share to local SSD
  - Process: extract frames, crop tiles, write pack files + manifest.db locally
  - Push: copy pack files + manifest.db back to server per-game directory
  - Cleanup: remove local working files
"""

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from training.tasks import register_task

logger = logging.getLogger(__name__)


@register_task("tile")
def run_tile(
    *,
    item: dict,
    local_work_dir: Path,
    server_share: str = "",
    local_models_dir: Path | None = None,
) -> dict:
    """Tile a game's video segments into pack files."""
    game_id = item["game_id"]
    payload = item.get("payload") or {}

    from training.pipeline.config import load_config
    cfg = load_config()

    # Source: video files in per-game dir on server
    server_game_dir = Path(cfg.paths.games_dir) / game_id
    video_dir = server_game_dir / "video"

    # If running on a remote machine, access via share
    if server_share and not video_dir.exists():
        video_dir = Path(server_share) / "games" / game_id / "video"

    if not video_dir.exists():
        raise FileNotFoundError(f"Video dir not found: {video_dir}")

    video_files = sorted(video_dir.glob("*.mp4"))
    if not video_files:
        raise FileNotFoundError(f"No .mp4 files in {video_dir}")

    # Local working directory
    local_game = local_work_dir / game_id
    local_video = local_game / "video"
    local_packs = local_game / "tile_packs"
    local_video.mkdir(parents=True, exist_ok=True)
    local_packs.mkdir(parents=True, exist_ok=True)

    # Step 1: Pull video files to local SSD
    logger.info("Pulling %d video files to local SSD...", len(video_files))
    for vf in video_files:
        dest = local_video / vf.name
        if not dest.exists():
            shutil.copy2(str(vf), str(dest))

    # Step 2: Process each video segment
    needs_flip = payload.get("needs_flip", False)
    frame_interval = cfg.tiling.frame_interval
    tile_cols = cfg.tiling.tile_cols
    tile_rows = cfg.tiling.tile_rows
    tile_size = cfg.tiling.tile_size

    from training.data_prep.game_manifest import GameManifest

    local_manifest = GameManifest(local_game)
    local_manifest.open()

    total_tiles = 0
    total_pack_bytes = 0

    for video_path in sorted(local_video.glob("*.mp4")):
        segment = video_path.stem
        logger.info("  Tiling segment: %s", segment)

        t0 = time.time()
        pack_path = local_packs / f"{segment}.pack"
        stats = _tile_segment_to_pack(
            video_path=video_path,
            segment=segment,
            pack_path=pack_path,
            manifest=local_manifest,
            frame_interval=frame_interval,
            tile_cols=tile_cols,
            tile_rows=tile_rows,
            tile_size=tile_size,
            flip=needs_flip,
        )

        total_tiles += stats["tiles"]
        total_pack_bytes += stats["pack_bytes"]
        elapsed = time.time() - t0
        logger.info(
            "    %d tiles, %.1f MB pack, %.1fs",
            stats["tiles"], stats["pack_bytes"] / 1e6, elapsed,
        )

    local_manifest.rebuild_segment_stats()
    local_manifest.set_metadata("tiled_at", str(time.time()))
    local_manifest.close()

    # Step 3: Push results back to server
    dest_game_dir = server_game_dir
    if server_share and not server_game_dir.exists():
        dest_game_dir = Path(server_share) / "games" / game_id

    dest_packs = dest_game_dir / "tile_packs"
    dest_packs.mkdir(parents=True, exist_ok=True)

    logger.info("Pushing pack files to server...")
    for pack_file in local_packs.glob("*.pack"):
        dest = dest_packs / pack_file.name
        shutil.copy2(str(pack_file), str(dest))

    # Push manifest.db
    dest_manifest = dest_game_dir / "manifest.db"
    shutil.copy2(str(local_game / "manifest.db"), str(dest_manifest))

    logger.info(
        "Tiled %s: %d tiles, %.1f MB total",
        game_id, total_tiles, total_pack_bytes / 1e6,
    )

    return {
        "tiles": total_tiles,
        "pack_bytes": total_pack_bytes,
        "segments": len(list(local_packs.glob("*.pack"))),
    }


def _tile_segment_to_pack(
    *,
    video_path: Path,
    segment: str,
    pack_path: Path,
    manifest,
    frame_interval: int = 4,
    tile_cols: int = 7,
    tile_rows: int = 3,
    tile_size: int = 640,
    flip: bool = False,
) -> dict:
    """Extract frames from a video segment, tile them, write to a pack file.

    All processing happens in memory — no loose tile files on disk.
    """
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    # Calculate tile step sizes (with overlap)
    step_x = max(1, (width - tile_size) // (tile_cols - 1)) if tile_cols > 1 else 0
    step_y = max(1, (height - tile_size) // (tile_rows - 1)) if tile_rows > 1 else 0

    tile_count = 0
    pack_offset = 0
    tile_rows_db = []
    pack_updates = []

    with open(pack_path, "wb") as pf:
        frame_idx = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            if frame_idx % frame_interval != 0:
                frame_idx += 1
                continue

            if flip:
                frame = cv2.flip(frame, -1)

            # Tile this frame
            for row in range(tile_rows):
                for col in range(tile_cols):
                    y = min(row * step_y, height - tile_size)
                    x = min(col * step_x, width - tile_size)

                    tile = frame[y : y + tile_size, x : x + tile_size]

                    # JPEG encode in memory
                    success, jpeg = cv2.imencode(".jpg", tile, [cv2.IMWRITE_JPEG_QUALITY, 90])
                    if not success:
                        continue

                    jpeg_bytes = jpeg.tobytes()
                    jpeg_size = len(jpeg_bytes)

                    # Write to pack file
                    pf.write(jpeg_bytes)

                    # Record for manifest
                    tile_rows_db.append((segment, frame_idx, row, col))
                    pack_updates.append(
                        (str(pack_path), pack_offset, jpeg_size, segment, frame_idx, row, col)
                    )

                    pack_offset += jpeg_size
                    tile_count += 1

            frame_idx += 1

    cap.release()

    # Write to manifest
    if tile_rows_db:
        manifest.insert_tiles(tile_rows_db)
        manifest.bulk_update_pack_info(pack_updates)

    return {
        "tiles": tile_count,
        "pack_bytes": pack_offset,
        "frames_processed": frame_idx,
    }
