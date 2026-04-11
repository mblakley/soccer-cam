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

    cfg = io.cfg

    from training.data_prep.game_manifest import GameManifest

    manifest = GameManifest(io.local_game)
    manifest.open()

    try:
        return _tile_segments(manifest, io, cfg, game_id, payload)
    finally:
        manifest.close()


def _tile_segments(manifest, io, cfg, game_id: str, payload: dict) -> dict:
    """Inner tiling logic — separated so manifest.close() is guaranteed."""
    from training.data_prep.game_manifest import GameManifest

    needs_flip = payload.get("needs_flip", False)
    frame_interval = cfg.tiling.frame_interval
    tile_cols = cfg.tiling.tile_cols
    tile_rows = cfg.tiling.tile_rows
    tile_size = cfg.tiling.tile_size

    total_tiles = 0
    total_pack_bytes = 0
    io.local_packs.mkdir(parents=True, exist_ok=True)

    # Process each video segment (skip concatenated full-game videos that are too large)
    local_videos = sorted(io.local_video.glob("*.mp4")) + sorted(
        io.local_video.glob("*.dav")
    )
    skip_keywords = ("combined", "raw", "once-processed", "processed")
    for video_path in local_videos:
        segment = video_path.stem

        # Only tile individual segment files (they have [F] or [0@0] in the name).
        # Skip concatenated full-game videos (raw, combined, etc.) — they duplicate
        # the individual segments and produce 100GB+ packs that fill the SSD.
        is_segment = "[F]" in segment or "[0@0]" in segment
        if not is_segment:
            logger.info("  Skipping non-segment video: %s", segment)
            continue

        # Check disk space before each segment
        import shutil

        _, _, free = shutil.disk_usage(str(io.local_work_dir))
        free_gb = free / (1024**3)
        if free_gb < cfg.resources.min_disk_free_gb:
            logger.error(
                "Disk critically low (%.1fGB < %dGB), stopping tiling early",
                free_gb,
                cfg.resources.min_disk_free_gb,
            )
            break

        # Check if this segment already has tiles (resume from last tiled frame)
        max_frame_row = manifest.conn.execute(
            "SELECT MAX(frame_idx) FROM tiles WHERE segment = ?", (segment,)
        ).fetchone()
        max_frame = (
            max_frame_row[0] if max_frame_row and max_frame_row[0] is not None else None
        )
        start_frame = (max_frame + frame_interval) if max_frame is not None else 0

        if start_frame > 0:
            logger.info(
                "  Resuming %s from frame %d (was tiled to %d)",
                segment,
                start_frame,
                max_frame,
            )
        else:
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
            start_frame=start_frame,
        )

        total_tiles += stats["tiles"]
        total_pack_bytes += stats["pack_bytes"]
        logger.info(
            "    %d tiles, %.1f MB pack, %.1fs",
            stats["tiles"],
            stats["pack_bytes"] / 1e6,
            time.time() - t0,
        )

        # Push this segment's pack immediately (don't accumulate on SSD).
        # Rewrite pack_file path, push to D:, archive to F:, clean local.
        server_packs_dir = io.server_game_dir / "tile_packs"
        manifest.conn.execute(
            "UPDATE tiles SET pack_file = REPLACE(pack_file, ?, ?) WHERE segment = ?",
            (str(io.local_packs), str(server_packs_dir), segment),
        )
        manifest.conn.commit()

        # Push manifest + this pack to D:
        manifest.rebuild_segment_stats()
        # Close before push (releases WAL lock for copy)
        manifest.close()
        io.push_manifest()

        server_packs_dir.mkdir(parents=True, exist_ok=True)
        if pack_path.exists():
            import shutil as _shutil

            dest = server_packs_dir / pack_path.name
            logger.info(
                "    Pushing %s (%.1f GB) to D:",
                pack_path.name,
                pack_path.stat().st_size / 1e9,
            )
            _shutil.copy2(str(pack_path), str(dest))

            # Archive to F:
            archive_dir = Path(cfg.paths.archive.tile_packs) / game_id
            archive_dir.mkdir(parents=True, exist_ok=True)
            f_dest = archive_dir / pack_path.name
            if not f_dest.exists() or f_dest.stat().st_size != pack_path.stat().st_size:
                logger.info("    Archiving %s to F:", pack_path.name)
                _shutil.copy2(str(pack_path), str(f_dest))

            # Verify archive then clean local + D:
            if f_dest.exists() and f_dest.stat().st_size == pack_path.stat().st_size:
                pack_path.unlink()
                dest.unlink()
                logger.info("    Cleaned local + D: pack (archived to F:)")
            else:
                logger.warning("    Archive verify failed — keeping pack on D:")
                pack_path.unlink()  # still clean local to save SSD space

        # Reopen manifest for next segment
        manifest = GameManifest(io.local_game)
        manifest.open(create=False)

        # Also clean the staged video for this segment to free SSD
        if video_path.exists():
            video_path.unlink()
            logger.info("    Cleaned staged video %s", video_path.name)

    manifest.rebuild_segment_stats()
    manifest.set_metadata("tiled_at", str(time.time()))

    manifest.close()
    io.push_manifest()

    logger.info(
        "Tiled %s: %d tiles, %.1f MB total",
        game_id,
        total_tiles,
        total_pack_bytes / 1e6,
    )

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
    start_frame: int = 0,
) -> dict:
    """Extract frames from video, tile them, write to a pack file.

    All processing in memory — no loose tile files on disk.
    If start_frame > 0, seeks to that position first (for resume tiling).
    """
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    if start_frame > 0:
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    step_x = max(1, (width - tile_size) // (tile_cols - 1)) if tile_cols > 1 else 0
    step_y = max(1, (height - tile_size) // (tile_rows - 1)) if tile_rows > 1 else 0

    tile_count = 0
    pack_offset = 0
    tile_rows_db = []
    pack_updates = []

    open_mode = "ab" if start_frame > 0 and pack_path.exists() else "wb"
    if open_mode == "ab":
        pack_offset = pack_path.stat().st_size

    with open(pack_path, open_mode) as pf:
        frame_idx = start_frame
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

                    success, jpeg = cv2.imencode(
                        ".jpg", tile, [cv2.IMWRITE_JPEG_QUALITY, 90]
                    )
                    if not success:
                        continue

                    jpeg_bytes = jpeg.tobytes()
                    jpeg_size = len(jpeg_bytes)
                    pf.write(jpeg_bytes)

                    tile_rows_db.append((segment, frame_idx, row, col))
                    pack_updates.append(
                        (
                            str(pack_path),
                            pack_offset,
                            jpeg_size,
                            segment,
                            frame_idx,
                            row,
                            col,
                        )
                    )
                    pack_offset += jpeg_size
                    tile_count += 1

            frame_idx += 1

    cap.release()

    if tile_rows_db:
        manifest.insert_tiles(tile_rows_db)
        manifest.bulk_update_pack_info(pack_updates)

    return {"tiles": tile_count, "pack_bytes": pack_offset}
