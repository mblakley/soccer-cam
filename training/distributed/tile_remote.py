"""Remote tiling: run on laptop/helper machines to tile games over network.

Maps shares via WNet API (works from WMI/PSRemoting), reads video from
video share, writes tiles to training share. Coordinates with server
via lock files.

Deploy: copy this file + dependencies to C:\\soccer-cam-label\\ on remote machine.
Run: C:\\Python313\\python.exe -u C:\\soccer-cam-label\\tile_remote.py
"""

import glob as glob_mod
import json
import logging
import os
import shutil
import socket
import sys
import time
from pathlib import Path

# Add script directory to path for local imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from extract_frames import extract_frames
from map_share import map_share
from tile_frames import tile_frame

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("C:/soccer-cam-label/tiling.log"),
    ],
)
logger = logging.getLogger()

# Config
SERVER = "192.168.86.152"
SHARE_USER = "DESKTOP-5L867J8\\training"
SHARE_PASS = "amy4ever"
HOSTNAME = socket.gethostname()
FRAME_INTERVAL = 4
DIFF_THRESHOLD = 2.0
TILE_COLS = 7
TILE_ROWS = 3
TILE_SIZE = 640
TEMP_FRAMES = Path("C:/soccer-cam-label/temp_frames")


def setup_shares():
    """Map network shares using WNet API."""
    map_share(f"\\\\{SERVER}\\training", SHARE_USER, SHARE_PASS)
    logger.info("Training share mapped")
    # Video is served from D: staging, NOT from F: (USB).
    # Server copies video to D:/training_data/staging/ before tiling.
    # Laptop reads from there via training share.


def load_registry():
    """Load game registry from training share."""
    reg_path = f"//{SERVER}/training/game_registry.json"
    with open(reg_path) as f:
        return json.load(f)


def claim_game(game_id: str, tiles_base: Path) -> bool:
    """Claim a game via lock file."""
    lock_dir = tiles_base / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / f"{game_id}.lock"

    if lock_file.exists():
        try:
            age = time.time() - lock_file.stat().st_mtime
            if age < 7200:
                owner = lock_file.read_text().strip()
                logger.info("  Locked by %s (%.0f min ago)", owner, age / 60)
                return False
        except OSError:
            pass

    lock_file.write_text(f"{HOSTNAME} {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    return True


def release_game(game_id: str, tiles_base: Path):
    """Release lock after tiling."""
    lock_file = tiles_base / ".locks" / f"{game_id}.lock"
    lock_file.unlink(missing_ok=True)


def find_videos(game: dict) -> list[Path]:
    """Find video segments in server's D: staging directory.

    Server copies video from F: to D:/training_data/staging/{game_id}/
    before tiling. Laptop reads from there via training share.
    F: (USB) should NEVER be read directly over the network.
    """
    game_id = game["game_id"]
    staging_dir = Path(f"//{SERVER}/training/staging/{game_id}")

    if not staging_dir.exists():
        logger.info("  Staging not ready for %s (server hasn't copied yet)", game_id)
        return []

    videos = sorted(staging_dir.glob("*.mp4"))
    return videos


def tile_game(game: dict, videos: list[Path], tiles_dir: Path):
    """Tile one game from network video sources."""
    game_id = game["game_id"]
    needs_flip = game.get("needs_flip", False)
    game_tiles = tiles_dir / game_id
    total_frames = 0
    total_tiles = 0

    for video in sorted(videos):
        seg_id = video.stem
        existing = list(game_tiles.glob(f"{glob_mod.escape(seg_id)}_*_r0_c0.jpg")) if game_tiles.exists() else []
        if existing:
            logger.info("  Skipping %s (already tiled)", seg_id)
            continue

        frames_dir = TEMP_FRAMES / seg_id
        n = extract_frames(
            video, frames_dir,
            diff_threshold=DIFF_THRESHOLD,
            frame_interval=FRAME_INTERVAL,
            flip=needs_flip,
        )

        game_tiles.mkdir(parents=True, exist_ok=True)
        n_tiles = 0
        for fp in sorted(frames_dir.rglob("*.jpg")):
            tiles = tile_frame(fp, game_tiles, cols=TILE_COLS, rows=TILE_ROWS, tile_size=TILE_SIZE)
            n_tiles += len(tiles)

        shutil.rmtree(frames_dir, ignore_errors=True)
        total_frames += n
        total_tiles += n_tiles
        logger.info("  %s: %d frames -> %d tiles", seg_id, n, n_tiles)

    return total_frames, total_tiles


def main():
    logger.info("=== Remote tiling on %s ===", HOSTNAME)

    # Map shares
    setup_shares()

    # Load registry
    games = load_registry()
    tiles_base = Path(f"//{SERVER}/training/tiles_640")
    tiles_base.mkdir(parents=True, exist_ok=True)

    logger.info("Registry: %d games", len(games))

    start = time.time()
    processed = 0

    for game in games:
        game_id = game["game_id"]

        if game.get("exclude"):
            continue

        # Skip already tiled
        if (tiles_base / game_id).exists():
            continue

        # Claim
        if not claim_game(game_id, tiles_base):
            continue

        logger.info("=== %s ===", game_id)
        try:
            videos = find_videos(game)
            if not videos:
                logger.warning("No videos found for %s", game_id)
                release_game(game_id, tiles_base)
                continue

            frames, tiles = tile_game(game, videos, tiles_base)
            processed += 1
            elapsed = time.time() - start
            logger.info(
                "Done %s: %d frames, %d tiles (%.0f min elapsed, %d games done)",
                game_id, frames, tiles, elapsed / 60, processed,
            )
        except Exception:
            logger.exception("Failed: %s", game_id)
        finally:
            release_game(game_id, tiles_base)

    elapsed = time.time() - start
    logger.info("=== Complete: %d games in %.0f min ===", processed, elapsed / 60)


if __name__ == "__main__":
    main()
