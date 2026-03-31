"""Mass tiling pipeline — tile all games from F: via D: staging.

Reads game registry, copies video segments from F: (USB) to D: (internal HDD),
extracts frames + tiles on D:, then cleans up staging. Pipelined: copies next
game while processing current one.

Usage:
    uv run python -m training.data_prep.mass_tile
    uv run python -m training.data_prep.mass_tile --dry-run
    uv run python -m training.data_prep.mass_tile --game flash__2024.09.27_vs_RNYFC_Black_home
"""

import argparse
import glob
import logging
import shutil
import threading
import time
from pathlib import Path

from training.data_prep.extract_frames import extract_frames
from training.data_prep.game_registry import load_registry, build_registry, save_registry
from training.data_prep.tile_frames import tile_frame

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

STAGING_DIR = Path("D:/training_data/staging")
TILES_DIR = Path("D:/training_data/tiles_640")

DIFF_THRESHOLD = 2.0
FRAME_INTERVAL = 4  # Extract every 4th frame (~6 fps at 24.6 fps source)
TILE_COLS = 7
TILE_ROWS = 3
TILE_SIZE = 640


def copy_video_to_staging(game: dict, staging_dir: Path) -> list[Path]:
    """Copy video segments from F: to D: staging area.

    For corrected videos, copies the single corrected .mp4.
    For segments, copies all [F] segment files.

    Returns list of video paths on D:.
    """
    game_id = game["game_id"]
    game_staging = staging_dir / game_id
    game_staging.mkdir(parents=True, exist_ok=True)

    source_dir = Path(game["path"])

    if game["video_source"] == "corrected" and game["corrected_video"]:
        # Single corrected video file
        src = Path(game["corrected_video"])
        dst = game_staging / src.name
        if not dst.exists():
            logger.info("Copying corrected video: %s (%.1f GB)", src.name, src.stat().st_size / 1e9)
            shutil.copy2(str(src), str(dst))
        return [dst]

    # Copy [F] segment files
    videos = []
    for seg_name in game["segments"]:
        # Find the actual file — may be in subdirectories (Game 1/, Game 2/, etc.)
        matches = list(source_dir.rglob(seg_name))
        if matches:
            src = matches[0]
        else:
            src = source_dir / seg_name
        dst = game_staging / seg_name
        if not dst.exists() and src.exists():
            logger.info("Copying segment: %s (%.1f GB)", seg_name, src.stat().st_size / 1e9)
            shutil.copy2(str(src), str(dst))
        if dst.exists():
            videos.append(dst)

    return videos


def tile_game(game: dict, videos: list[Path], tiles_dir: Path) -> dict:
    """Extract frames and tile a single game from staged videos."""
    game_id = game["game_id"]
    needs_flip = game.get("needs_flip", False)
    game_tiles_dir = tiles_dir / game_id

    total_frames = 0
    total_tiles = 0

    for video in sorted(videos):
        segment_id = video.stem

        # Skip if this segment already has tiles
        existing = list(game_tiles_dir.glob(f"{glob.escape(segment_id)}_*_r0_c0.jpg")) if game_tiles_dir.exists() else []
        if existing:
            logger.info("  Skipping %s (already tiled, %d frames)", segment_id, len(existing))
            total_tiles += len(existing) * TILE_ROWS * TILE_COLS
            total_frames += len(existing)
            continue

        # Extract frames to temp dir
        frames_dir = video.parent / f"_frames_{segment_id}"
        n_frames = extract_frames(
            video,
            frames_dir,
            diff_threshold=DIFF_THRESHOLD,
            frame_interval=FRAME_INTERVAL,
            flip=needs_flip,
        )

        # Tile all frames
        frame_files = sorted(frames_dir.rglob("*.jpg"))
        n_tiles = 0
        for frame_path in frame_files:
            tiles = tile_frame(
                frame_path,
                game_tiles_dir,
                cols=TILE_COLS,
                rows=TILE_ROWS,
                tile_size=TILE_SIZE,
            )
            n_tiles += len(tiles)

        # Cleanup frames
        if frames_dir.exists():
            shutil.rmtree(frames_dir)

        total_frames += n_frames
        total_tiles += n_tiles
        logger.info("  %s: %d frames → %d tiles", segment_id, n_frames, n_tiles)

    return {"game_id": game_id, "frames": total_frames, "tiles": total_tiles}


def game_already_tiled(game: dict, tiles_dir: Path) -> bool:
    """Check if a game already has tiles on D: or F:."""
    for base in [tiles_dir, Path("F:/training_data/tiles_640")]:
        game_tiles = base / game["game_id"]
        if game_tiles.exists():
            return True
    return False


def mass_tile(
    games: list[dict] | None = None,
    staging_dir: Path = STAGING_DIR,
    tiles_dir: Path = TILES_DIR,
    dry_run: bool = False,
    game_filter: str | None = None,
):
    """Tile all games with pipelined F:→D: copy."""
    if games is None:
        games = load_registry()

    if not games:
        logger.error("No games in registry. Run game_registry.py first.")
        return

    # Filter if requested
    if game_filter:
        games = [g for g in games if g["game_id"] == game_filter]
        if not games:
            logger.error("Game %s not found in registry", game_filter)
            return

    # Skip excluded and already-tiled games
    to_process = []
    skipped = 0
    for g in games:
        if g.get("exclude"):
            logger.info("Excluding %s: %s", g["game_id"], g.get("exclude_reason", ""))
            continue
        if game_already_tiled(g, tiles_dir):
            logger.info("Already tiled: %s", g["game_id"])
            skipped += 1
            continue
        to_process.append(g)

    # Sort: right-side-up first, then corrected, then flip-in-code
    order = {"segments": 0, "corrected": 1, "flip_in_code": 2}
    to_process.sort(key=lambda g: order.get(g["video_source"], 3))

    logger.info(
        "Mass tiling: %d to process, %d already done, %d excluded",
        len(to_process),
        skipped,
        len(games) - len(to_process) - skipped,
    )

    if dry_run:
        for g in to_process:
            logger.info(
                "  Would process: %s (%d segments, source=%s, flip=%s)",
                g["game_id"],
                g["segment_count"],
                g["video_source"],
                g.get("needs_flip", False),
            )
        return

    staging_dir.mkdir(parents=True, exist_ok=True)
    tiles_dir.mkdir(parents=True, exist_ok=True)

    # Pipelined processing: copy next game while tiling current
    copy_result = {}
    copy_thread = None
    start_time = time.time()

    def _copy_game(game, result_dict):
        try:
            videos = copy_video_to_staging(game, staging_dir)
            result_dict["videos"] = videos
            result_dict["ok"] = True
        except Exception as e:
            logger.exception("Failed to copy %s", game["game_id"])
            result_dict["ok"] = False
            result_dict["error"] = str(e)

    for i, game in enumerate(to_process):
        game_id = game["game_id"]
        logger.info("=== [%d/%d] %s ===", i + 1, len(to_process), game_id)

        # Wait for copy of THIS game (started in previous iteration, or do it now)
        if copy_thread and copy_thread.is_alive():
            logger.info("Waiting for copy to complete...")
            copy_thread.join()

        if game_id in copy_result and copy_result[game_id].get("ok"):
            videos = copy_result[game_id]["videos"]
        else:
            # First game or copy wasn't started yet
            result = {}
            _copy_game(game, result)
            if not result.get("ok"):
                logger.error("Failed to copy %s, skipping", game_id)
                continue
            videos = result["videos"]

        # Start copying NEXT game in background
        if i + 1 < len(to_process):
            next_game = to_process[i + 1]
            next_id = next_game["game_id"]
            copy_result[next_id] = {}
            copy_thread = threading.Thread(
                target=_copy_game,
                args=(next_game, copy_result[next_id]),
                daemon=True,
            )
            copy_thread.start()

        # Tile this game
        result = tile_game(game, videos, tiles_dir)

        # Cleanup staging for this game
        game_staging = staging_dir / game_id
        if game_staging.exists():
            shutil.rmtree(game_staging)
            logger.info("Cleaned staging for %s", game_id)

        # Progress
        elapsed = time.time() - start_time
        games_done = i + 1
        avg_per_game = elapsed / games_done
        remaining = (len(to_process) - games_done) * avg_per_game
        logger.info(
            "Done %s: %d frames, %d tiles | %d/%d games | ETA: %.0f min",
            game_id,
            result["frames"],
            result["tiles"],
            games_done,
            len(to_process),
            remaining / 60,
        )

    # Final cleanup
    if staging_dir.exists() and not any(staging_dir.iterdir()):
        staging_dir.rmdir()

    elapsed = time.time() - start_time
    logger.info("=== COMPLETE: %d games in %.0f min ===", len(to_process), elapsed / 60)


def main():
    parser = argparse.ArgumentParser(description="Mass tile all games from registry")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be processed")
    parser.add_argument("--game", type=str, help="Process a single game_id")
    parser.add_argument("--staging-dir", type=Path, default=STAGING_DIR)
    parser.add_argument("--tiles-dir", type=Path, default=TILES_DIR)
    args = parser.parse_args()

    mass_tile(
        staging_dir=args.staging_dir,
        tiles_dir=args.tiles_dir,
        dry_run=args.dry_run,
        game_filter=args.game,
    )


if __name__ == "__main__":
    main()
