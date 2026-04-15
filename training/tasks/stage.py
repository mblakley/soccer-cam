"""Stage task — verify video exists and record its path for workers.

Server-only task. Does NOT copy video to D: (D: is full of tile packs).
Instead, verifies the video path on F: is accessible and counts segments.
Workers read video directly from F: (server) or via the video share (remote).

The tile task handles pulling video to local SSD before processing.
"""

import logging
import os
from pathlib import Path

from training.tasks import register_task

logger = logging.getLogger(__name__)


@register_task("stage")
def run_stage(
    *,
    item: dict,
    local_work_dir: Path,
    server_share: str = "",
    local_models_dir: Path | None = None,
) -> dict:
    """Verify video files exist and are accessible for a game."""
    game_id = item["game_id"]
    payload = item.get("payload") or {}
    video_path = payload.get("video_path", "")

    if not video_path:
        raise ValueError(f"No video_path in payload for {game_id}")

    source = Path(video_path)
    if not source.exists():
        raise FileNotFoundError(f"Source video path not found: {source}")

    # Find all video segments
    if source.is_dir():
        video_files = sorted(source.glob("*.mp4")) + sorted(source.glob("*.dav"))
        if not video_files:
            # Try recursive (tournaments have subdirectories)
            video_files = sorted(source.rglob("*.mp4")) + sorted(source.rglob("*.dav"))
    else:
        video_files = [source]

    if not video_files:
        raise FileNotFoundError(f"No video files found in {source}")

    # Filter to actual segment files (Dahua [F]/[0@0] markers)
    # Exclude processed/combined videos that also live in the source directory
    segment_files = [vf for vf in video_files if "[F]" in vf.name or "[0@0]" in vf.name]
    # Fall back to all files if no segment markers found (e.g. Reolink, GoPro)
    if not segment_files:
        segment_files = video_files

    # Verify each file is readable and get sizes
    total_size = 0
    segments = []
    for vf in segment_files:
        try:
            size = vf.stat().st_size
            if size == 0:
                logger.warning("Empty video file: %s", vf)
                continue
            total_size += size
            segments.append(
                {
                    "name": vf.name,
                    "path": str(vf),
                    "size": size,
                }
            )
        except OSError as e:
            logger.warning("Cannot access %s: %s", vf, e)

    if not segments:
        raise FileNotFoundError(f"No readable video files in {source}")

    logger.info(
        "Staged %s: %d segments, %.1f GB at %s",
        game_id,
        len(segments),
        total_size / 1e9,
        source,
    )

    return {
        "segments": len(segments),
        "total_size_bytes": total_size,
        "video_path": str(source),
        "segment_names": [s["name"] for s in segments],
    }
