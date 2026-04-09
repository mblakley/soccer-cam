"""Stage task — copy source video from F: (archive) to D: for processing.

Server-only task. Copies video files for a game from the archive drive
to the per-game directory where they can be served to remote workers.

Pull-local-process-push pattern:
  - Pull: read from F: (archive, USB)
  - Process: copy to D:/training_data/games/{game_id}/video/
  - Push: N/A (already on D:)
"""

import logging
import os
import subprocess
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
    """Copy video files from archive to per-game directory."""
    game_id = item["game_id"]
    payload = item.get("payload") or {}
    video_path = payload.get("video_path", "")

    if not video_path:
        raise ValueError(f"No video_path in payload for {game_id}")

    source = Path(video_path)
    if not source.exists():
        raise FileNotFoundError(f"Source video path not found: {source}")

    # Destination: per-game dir
    from training.pipeline.config import load_config
    cfg = load_config()
    dest = Path(cfg.paths.games_dir) / game_id / "video"
    dest.mkdir(parents=True, exist_ok=True)

    # Count source files
    if source.is_dir():
        src_files = list(source.glob("*.mp4")) + list(source.glob("*.dav"))
        if not src_files:
            # Try recursive
            src_files = list(source.rglob("*.mp4")) + list(source.rglob("*.dav"))
    else:
        src_files = [source]

    if not src_files:
        raise FileNotFoundError(f"No video files found in {source}")

    logger.info("Staging %d video files from %s to %s", len(src_files), source, dest)

    # Use robocopy for reliable bulk copy (Windows)
    if source.is_dir():
        result = subprocess.run(
            [
                "robocopy",
                str(source),
                str(dest),
                "*.mp4", "*.dav",
                "/E",       # include subdirectories
                "/J",       # unbuffered I/O (better for large files)
                "/R:3",     # 3 retries
                "/W:5",     # 5 sec wait between retries
                "/NP",      # no progress
                "/NDL",     # no directory listing
            ],
            capture_output=True,
            text=True,
        )
        # robocopy returns 0-7 for various success states, 8+ for errors
        if result.returncode >= 8:
            raise RuntimeError(
                f"robocopy failed (exit {result.returncode}): {result.stderr or result.stdout}"
            )
    else:
        # Single file
        import shutil
        shutil.copy2(str(source), str(dest / source.name))

    # Verify
    dest_files = list(dest.glob("*.mp4")) + list(dest.glob("*.dav"))
    total_size = sum(f.stat().st_size for f in dest_files)

    logger.info(
        "Staged %d files (%.1f GB) for %s",
        len(dest_files),
        total_size / 1e9,
        game_id,
    )

    return {
        "files_copied": len(dest_files),
        "total_size_bytes": total_size,
        "destination": str(dest),
    }
