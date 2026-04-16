"""Tile task — extract frames from video and tile into pack files.

Uses TaskIO for consistent pull-local-process-push:
  - Pull: video files from F: (or share) to local SSD
  - Process: extract frames, crop tiles, write pack files + manifest on SSD
  - Push: pack files + manifest.db to server per-game dir on D:
  - Cleanup: remove local working files
"""

import logging
import os
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

    # Don't pull all videos upfront — tournament games can be 100GB+.
    # Instead, stage one segment at a time inside _tile_segments().
    io.local_video.mkdir(parents=True, exist_ok=True)

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

    # Find segment videos from source (F: or SMB share) — don't copy all upfront.
    # Tournament games can have 100GB+ of video files which would fill the SSD.
    source_video_dir = io.video_path()
    if not source_video_dir:
        raise FileNotFoundError(f"No video path found for {game_id}")

    all_videos = sorted(source_video_dir.glob("*.mp4")) + sorted(
        source_video_dir.glob("*.dav")
    )
    if not all_videos:
        all_videos = sorted(source_video_dir.rglob("*.mp4"))

    # Filter to actual segment files (Dahua [F]/[0@0] markers).
    # Exclude processed/combined videos that also live in the source directory.
    source_videos = [v for v in all_videos if "[F]" in v.stem or "[0@0]" in v.stem]
    # Fall back to all files if no segment markers found (e.g. Reolink, GoPro)
    if not source_videos:
        source_videos = all_videos

    import shutil as _stage_shutil

    for source_video in source_videos:
        segment = source_video.stem

        # Stage this one segment to local SSD
        video_path = io.local_video / source_video.name
        if not video_path.exists():
            logger.info("  Staging %s to SSD...", source_video.name)
            _stage_shutil.copy2(str(source_video), str(video_path))

        # Check disk space before each segment
        _, _, free = _stage_shutil.disk_usage(str(io.local_work_dir))
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
            encode_threads=getattr(cfg.tiling, "encode_threads", tile_rows * tile_cols),
            read_ahead=getattr(cfg.tiling, "read_ahead", 16),
            batch_frames=getattr(cfg.tiling, "batch_frames", 8),
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

            # Archive to F: (server-only — F: is not accessible from remote workers)
            import socket as _socket

            archive_dir = Path(cfg.paths.archive.tile_packs) / game_id
            if (
                _socket.gethostname().upper() == "DESKTOP-5L867J8"
                and archive_dir.drive.upper() != ""
            ):
                try:
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    f_dest = archive_dir / pack_path.name
                    if (
                        not f_dest.exists()
                        or f_dest.stat().st_size != pack_path.stat().st_size
                    ):
                        logger.info("    Archiving %s to F:", pack_path.name)
                        _shutil.copy2(str(pack_path), str(f_dest))

                    # Verify archive then clean local + D:
                    if (
                        f_dest.exists()
                        and f_dest.stat().st_size == pack_path.stat().st_size
                    ):
                        pack_path.unlink()
                        dest.unlink()
                        logger.info("    Cleaned local + D: pack (archived to F:)")
                    else:
                        logger.warning("    Archive verify failed — keeping pack on D:")
                        pack_path.unlink()  # still clean local to save SSD space
                except OSError as e:
                    logger.warning("    F: archive failed (%s) — keeping pack on D:", e)
                    pack_path.unlink()
            else:
                # Remote worker: clean local, keep on D: for server to archive later
                pack_path.unlink()
                logger.info("    Cleaned local pack (remote worker, D: copy kept)")

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
        "segments": len(source_videos),
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
    encode_threads: int = 21,
    read_ahead: int = 16,
    batch_frames: int = 8,
) -> dict:
    """Extract frames from video, tile them, write to a pack file.

    All processing in memory — no loose tile files on disk.
    If start_frame > 0, seeks to that position first (for resume tiling).

    Uses a 3-stage pipeline for throughput:
      1. Reader thread: decodes video frames into a queue
      2. Encoder pool: JPEG-encodes tiles across multiple cores
      3. Writer (main thread): writes encoded tiles to pack in order
    """
    import cv2
    import queue
    import threading
    from concurrent.futures import ThreadPoolExecutor

    cap = cv2.VideoCapture(str(video_path), cv2.CAP_FFMPEG)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    # Auto-compute pipeline params from CPU count (0 = auto-detect)
    cpus = os.cpu_count() or 4
    if not encode_threads:
        encode_threads = min(cpus // 2, tile_rows * tile_cols)
    if not batch_frames:
        batch_frames = max(1, encode_threads // 4)
    if not read_ahead:
        read_ahead = batch_frames * 2

    if start_frame > 0:
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    step_x = max(1, (width - tile_size) // (tile_cols - 1)) if tile_cols > 1 else 0
    step_y = max(1, (height - tile_size) // (tile_rows - 1)) if tile_rows > 1 else 0

    jpeg_params = [cv2.IMWRITE_JPEG_QUALITY, 90]
    _SENTINEL = None  # signals end of stream

    def _slice_and_encode(frame, row, col):
        """Slice a tile from frame and JPEG-encode it. Both release GIL."""
        y = min(row * step_y, height - tile_size)
        x = min(col * step_x, width - tile_size)
        tile = frame[y : y + tile_size, x : x + tile_size]
        success, jpeg = cv2.imencode(".jpg", tile, jpeg_params)
        if not success:
            return None
        return jpeg.tobytes()

    # Stage 1: Reader thread — decodes frames and feeds them to the encode queue
    # Uses grab()/retrieve() to skip decoding of non-target frames (3/4 of all frames)
    frame_queue = queue.Queue(maxsize=read_ahead)

    def _reader():
        fi = start_frame
        while True:
            if fi % frame_interval != 0:
                # grab() advances without decoding — much faster than read()
                if not cap.grab():
                    frame_queue.put(_SENTINEL)
                    return
                fi += 1
                continue
            ret, frame = cap.read()
            if not ret:
                frame_queue.put(_SENTINEL)
                return
            if flip:
                frame = cv2.flip(frame, -1)
            frame_queue.put((fi, frame))
            fi += 1

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    # Stage 2+3: Encode tiles in thread pool, write results in order
    tile_count = 0
    pack_offset = 0
    tile_rows_db = []
    pack_updates = []

    open_mode = "ab" if start_frame > 0 and pack_path.exists() else "wb"
    if open_mode == "ab":
        pack_offset = pack_path.stat().st_size

    num_encoders = encode_threads
    batch_size = batch_frames
    frames_processed = 0

    with open(pack_path, open_mode) as pf, ThreadPoolExecutor(
        max_workers=num_encoders
    ) as pool:
        while True:
            # Drain up to batch_size frames from the queue
            batch = []
            for _ in range(batch_size):
                item = frame_queue.get()
                if item is _SENTINEL:
                    batch.append(_SENTINEL)
                    break
                batch.append(item)

            # Submit all tiles from all frames in the batch
            # Slicing + encoding happens in the thread pool (both release GIL)
            frame_jobs = []  # [(frame_idx, tiles_info, futures), ...]
            for item in batch:
                if item is _SENTINEL:
                    break
                frame_idx, frame = item
                futures = []
                tiles_info = []
                for row in range(tile_rows):
                    for col in range(tile_cols):
                        tiles_info.append((row, col))
                        futures.append(pool.submit(_slice_and_encode, frame, row, col))
                frame_jobs.append((frame_idx, tiles_info, futures))

            # Write results in frame order (maintains sequential pack offsets)
            for frame_idx, tiles_info, futures in frame_jobs:
                for (row, col), future in zip(tiles_info, futures):
                    jpeg_bytes = future.result()
                    if jpeg_bytes is None:
                        continue

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

            frames_processed += len(frame_jobs)
            # Yield GIL periodically so heartbeat thread can run
            if frames_processed % 200 == 0:
                time.sleep(0)

            # Check if we hit the sentinel
            if batch and batch[-1] is _SENTINEL:
                break

    reader_thread.join(timeout=5)
    cap.release()

    if tile_rows_db:
        manifest.insert_tiles(tile_rows_db)
        manifest.bulk_update_pack_info(pack_updates)

    return {"tiles": tile_count, "pack_bytes": pack_offset}
