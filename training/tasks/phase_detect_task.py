"""Standalone phase detection task -- lightweight, no pack files needed.

Detects game phases (pre-game, first half, halftime, second half, post-game)
using audio whistle analysis and optionally Sonnet vision.

Only needs video .mp4 files (for audio) and manifest.db (for segment timeline).
Does NOT need pack files -- runs in minutes instead of hours.

Usage:
    Enqueue via: uv run python -m training.pipeline enqueue phase_detect --game GAME_ID
"""

import logging
from pathlib import Path

from training.tasks import register_task
from training.tasks.io import TaskIO

logger = logging.getLogger(__name__)


@register_task("phase_detect")
def run_phase_detect(
    *,
    item: dict,
    local_work_dir: Path,
    server_share: str = "",
    local_models_dir: Path | None = None,
) -> dict:
    """Detect game phases from audio whistle analysis."""
    game_id = item["game_id"]

    # Pull manifest only (lightweight)
    task_io = TaskIO(game_id, local_work_dir, server_share)
    task_io.ensure_space(needed_gb=5)
    task_io.pull_manifest()

    # Pull video files for audio extraction (~2 GB, not ~100 GB of packs)
    try:
        task_io.pull_video()
    except (FileNotFoundError, OSError) as e:
        logger.error("Cannot pull video for %s: %s", game_id, e)
        return {"error": str(e)}

    from training.data_prep.game_manifest import GameManifest
    from training.tasks.phase_detect import detect_game_phases

    manifest = GameManifest(task_io.local_game)
    manifest.open(create=False)

    try:
        result = detect_game_phases(manifest, task_io, force=True)

        if result:
            logger.info(
                "Detected %d phases for %s (source=%s)",
                result.get("phase_count", 0),
                game_id,
                result.get("source", "?"),
            )

            # Pre-load phase samples only for games that don't already have them
            from training.pipeline.config import load_config

            cfg = load_config()
            cache_dir = Path(cfg.paths.games_dir) / game_id / "phase_samples"
            if not cache_dir.exists() or len(list(cache_dir.glob("*.jpg"))) < 10:
                packs_dir = Path(cfg.paths.games_dir) / game_id / "tile_packs"
                if packs_dir.exists() and any(packs_dir.glob("*.pack")):
                    try:
                        from training.tasks.generate_review import (
                            _preload_phase_samples,
                        )

                        _preload_phase_samples(manifest, task_io, cfg)
                    except Exception as e:
                        logger.warning("Phase sample preload failed: %s", e)
                else:
                    logger.info("Skipping preload (no local packs)")
            else:
                logger.info(
                    "Phase samples already cached (%d frames)",
                    len(list(cache_dir.glob("*.jpg"))),
                )
        else:
            logger.warning("No phases detected for %s", game_id)
            result = {"phase_count": 0}

    finally:
        manifest.close()

    # Push manifest back with new phases
    task_io.push_manifest()

    return result
