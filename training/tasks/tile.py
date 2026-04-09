"""Tile task — extract frames from video and tile into pack files.

Uses TaskIO for consistent pull-local-process-push:
  - Pull: video files from F: (or share) to local SSD
  - Process: extract frames, crop tiles, write pack files + manifest on SSD
  - Push: pack files + manifest.db to server per-game dir on D:
  - Cleanup: remove local working files
"""

import logging
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

    from training.tasks.io import TaskIO

    io = TaskIO(game_id, local_work_dir, server_share)
    io.ensure_space(needed_gb=10)  # video + packs can be large

    # Pull video to local SSD
    io.pull_video()

    # Tiling config
    cfg = io.cfg
    needs_flip = payload.get("needs_flip", False)
    frame_interval = cfg.tiling.frame_interval
    tile_cols = cfg.tiling.tile_cols
    tile_rows = cfg.tiling.tile_rows
    tile_size = cfg.tiling.tile_size

    # Create local manifest
    from training.data_prep.game_manifest import GameManifest

    manifest = GameManifest(io.local_game)
    manifest.open()

    total_tiles = 0
    total_pack_bytes = 0
    io.local_packs.mkdir(parents=True, exist_ok=True)

    # Process each video segment
    local_videos = sorted(io.local_video.glob("*.mp4")) + sorted(io.local_video.glob("*.dav"))
    for video_path in local_videos:
        segment = video_path.stem
        logger.info("  Tiling segment: %s", segment)

        t0 = time.time()
        pack_path = io.local_packs / f"{segment}.pack"
        stats = _tile_segment_to_pack(
            video_path=video_path,
            segment=segment,
            pack_path=pack_path,
            manifest=manifest,
            frame_interval=frame_interval,
            tile_cols=tile_cols,
            tile_rows=tile_rows,
            tile_size=tile_size,
            flip=needs_flip,
        )

        total_tiles += stats["tiles"]
        total_pack_bytes += stats["pack_bytes"]
        logger.info("    %d tiles, %.1f MB pack, %.1fs",
                     stats["tiles"], stats["pack_bytes"] / 1e6, time.time() - t0)

    manifest.rebuild_segment_stats()
    manifest.set_metadata("tiled_at", str(time.time()))

    # Rewrite pack_file paths from local SSD → server destination
    server_packs_dir = io.server_game_dir / "tile_packs"
    manifest.conn.execute(
        "UPDATE tiles SET pack_file = REPLACE(pack_file, ?, ?)",
        (str(io.local_packs), str(server_packs_dir)),
    )
    manifest.conn.commit()
    manifest.close()

    # Push results to server
    io.push_packs()
    io.push_manifest()

    logger.info("Tiled %s: %d tiles, %.1f MB total", game_id, total_tiles, total_pack_bytes / 1e6)

    return {
        "tiles": total_tiles,
        "pack_bytes": total_pack_bytes,
        "segments": len(local_videos),
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
    """Extract frames from video, tile them, write to a pack file.

    All processing in memory — no loose tile files on disk.
    """
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

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

            for row in range(tile_rows):
                for col in range(tile_cols):
                    y = min(row * step_y, height - tile_size)
                    x = min(col * step_x, width - tile_size)
                    tile = frame[y : y + tile_size, x : x + tile_size]

                    success, jpeg = cv2.imencode(".jpg", tile, [cv2.IMWRITE_JPEG_QUALITY, 90])
                    if not success:
                        continue

                    jpeg_bytes = jpeg.tobytes()
                    jpeg_size = len(jpeg_bytes)
                    pf.write(jpeg_bytes)

                    tile_rows_db.append((segment, frame_idx, row, col))
                    pack_updates.append(
                        (str(pack_path), pack_offset, jpeg_size, segment, frame_idx, row, col)
                    )
                    pack_offset += jpeg_size
                    tile_count += 1

            frame_idx += 1

    cap.release()

    if tile_rows_db:
        manifest.insert_tiles(tile_rows_db)
        manifest.bulk_update_pack_info(pack_updates)

    return {"tiles": tile_count, "pack_bytes": pack_offset}
