"""Mass tiling pipeline — tile all games with multi-machine coordination.

Server mode: copies video from F: (USB) to D: staging, tiles from D:.
Remote mode: reads video from network share, tiles locally, writes to share.

Both modes write tiles to the same output directory (D: on server, exposed
as \\\\192.168.86.152\\training\\tiles_640). Lock files prevent two machines
from tiling the same game.

Usage:
    # Server (local I/O, fastest):
    uv run python -m training.data_prep.mass_tile

    # Laptop (over network, while GPU trains):
    uv run python -m training.data_prep.mass_tile --remote \\\\192.168.86.152\\video \\\\192.168.86.152\\training

    # Dry run:
    uv run python -m training.data_prep.mass_tile --dry-run
    uv run python -m training.data_prep.mass_tile --game flash__2024.09.27_vs_RNYFC_Black_home
"""

import argparse
import glob
import logging
import os
import shutil
import socket
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
HOSTNAME = socket.gethostname()


def claim_game(game_id: str, tiles_dir: Path) -> bool:
    """Try to claim a game for tiling via lock file. Returns True if claimed."""
    lock_dir = tiles_dir / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / f"{game_id}.lock"

    if lock_file.exists():
        # Check if lock is stale (>2 hours old)
        age = time.time() - lock_file.stat().st_mtime
        if age < 7200:
            try:
                owner = lock_file.read_text().strip()
            except OSError:
                owner = "unknown"
            logger.info("Skipping %s (locked by %s, %.0f min ago)", game_id, owner, age / 60)
            return False
        logger.warning("Breaking stale lock on %s (%.0f min old)", game_id, age / 60)

    # Write lock
    lock_file.write_text(f"{HOSTNAME} {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    return True


def release_game(game_id: str, tiles_dir: Path):
    """Release the lock on a game after tiling completes."""
    lock_file = tiles_dir / ".locks" / f"{game_id}.lock"
    if lock_file.exists():
        lock_file.unlink()


def find_video_sources(game: dict, video_share: Path | None = None) -> list[Path]:
    """Find video source files for a game.

    When video_share is set (remote mode), looks for videos under the share
    path instead of the game's registered path. Returns paths directly —
    no copying in remote mode.
    """
    if video_share:
        # Remote mode: find videos under the share path
        # Game path is like F:/Flash_2013s/... — map to share
        game_path = Path(game["path"])
        # Extract the team/game folder part (e.g., Flash_2013s/09.27.2024 - vs RNYFC Black (home))
        parts = game_path.parts
        # Find the team folder (Flash_2013s or Heat_2012s)
        for i, part in enumerate(parts):
            if "Flash" in part or "Heat" in part:
                rel = Path(*parts[i:])
                source_dir = video_share / rel
                break
        else:
            source_dir = video_share / game_path.name
    else:
        source_dir = Path(game["path"])

    if game["video_source"] == "corrected" and game["corrected_video"]:
        src = Path(game["corrected_video"])
        if video_share:
            # Map corrected video path to share
            game_path = Path(game["path"])
            for i, part in enumerate(game_path.parts):
                if "Flash" in part or "Heat" in part:
                    rel = Path(*game_path.parts[i:])
                    src = video_share / rel / src.name
                    break
        if src.exists():
            return [src]
        logger.warning("Corrected video not found: %s", src)
        return []

    # Find [F] segment files
    videos = []
    for seg_name in game["segments"]:
        matches = list(source_dir.rglob(seg_name))
        if matches:
            videos.append(matches[0])
        elif (source_dir / seg_name).exists():
            videos.append(source_dir / seg_name)
    return videos


def copy_video_to_staging(game: dict, staging_dir: Path) -> list[Path]:
    """Copy video segments from F: to D: staging area. Returns staged paths."""
    game_id = game["game_id"]
    game_staging = staging_dir / game_id
    game_staging.mkdir(parents=True, exist_ok=True)

    sources = find_video_sources(game)
    staged = []

    for src in sources:
        dst = game_staging / src.name
        if not dst.exists():
            logger.info("Copying: %s (%.1f GB)", src.name, src.stat().st_size / 1e9)
            shutil.copy2(str(src), str(dst))
        staged.append(dst)

    return staged


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
    video_share: Path | None = None,
):
    """Tile all games with pipelined F:→D: copy.

    In remote mode (video_share set), reads video directly from network
    share without staging copy. Tiles written to tiles_dir (which may
    also be a network share path).
    """
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

    tiles_dir.mkdir(parents=True, exist_ok=True)
    remote_mode = video_share is not None

    if remote_mode:
        logger.info("REMOTE MODE: reading video from %s, writing tiles to %s", video_share, tiles_dir)
    else:
        staging_dir.mkdir(parents=True, exist_ok=True)

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
        logger.info("=== [%d/%d] %s (%s) ===", i + 1, len(to_process), game_id, HOSTNAME)

        # Claim with lock file
        if not claim_game(game_id, tiles_dir):
            continue

        try:
            if remote_mode:
                # Remote: read video directly from share, no staging
                videos = find_video_sources(game, video_share)
                if not videos:
                    logger.error("No video sources found for %s on share", game_id)
                    release_game(game_id, tiles_dir)
                    continue
            else:
                # Local: copy to staging first (pipelined)
                if copy_thread and copy_thread.is_alive():
                    logger.info("Waiting for copy to complete...")
                    copy_thread.join()

                if game_id in copy_result and copy_result[game_id].get("ok"):
                    videos = copy_result[game_id]["videos"]
                else:
                    result = {}
                    _copy_game(game, result)
                    if not result.get("ok"):
                        logger.error("Failed to copy %s, skipping", game_id)
                        release_game(game_id, tiles_dir)
                        continue
                    videos = result["videos"]

                # Pipeline: start copying next game in background
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

            # Cleanup staging (local mode only)
            if not remote_mode:
                game_staging = staging_dir / game_id
                if game_staging.exists():
                    shutil.rmtree(game_staging)

            # Release lock
            release_game(game_id, tiles_dir)

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

        except Exception:
            logger.exception("Failed to tile %s", game_id)
            release_game(game_id, tiles_dir)

    if not remote_mode and staging_dir.exists() and not any(staging_dir.iterdir()):
        staging_dir.rmdir()

    elapsed = time.time() - start_time
    logger.info("=== COMPLETE (%s): %d games in %.0f min ===", HOSTNAME, len(to_process), elapsed / 60)


def main():
    parser = argparse.ArgumentParser(description="Mass tile all games from registry")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be processed")
    parser.add_argument("--game", type=str, help="Process a single game_id")
    parser.add_argument("--staging-dir", type=Path, default=STAGING_DIR)
    parser.add_argument("--tiles-dir", type=Path, default=TILES_DIR)
    parser.add_argument(
        "--remote", nargs=2, metavar=("VIDEO_SHARE", "TRAINING_SHARE"),
        help="Remote mode: read video from VIDEO_SHARE, write tiles to TRAINING_SHARE/tiles_640",
    )
    args = parser.parse_args()

    video_share = None
    tiles_dir = args.tiles_dir

    if args.remote:
        video_share = Path(args.remote[0])
        tiles_dir = Path(args.remote[1]) / "tiles_640"

    mass_tile(
        staging_dir=args.staging_dir,
        tiles_dir=tiles_dir,
        dry_run=args.dry_run,
        game_filter=args.game,
        video_share=video_share,
    )


if __name__ == "__main__":
    main()
