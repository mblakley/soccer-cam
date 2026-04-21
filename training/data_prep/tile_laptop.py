"""Laptop tiler: in-memory decode→tile→zip, transfer to server.

No temp tile files. Decode frame in memory, crop tiles, JPEG-encode in
memory, write directly to zip. Only disk writes are: video download
(one sequential file) and zip output (one sequential file).

Crash-safe: if we die, the zip for the current game is incomplete and
gets deleted on restart. But we don't lose tiles from other games.
"""

import json
import logging
import os
import shutil
import socket
import sys
import time
import zipfile
from pathlib import Path

import psutil
import cv2
import numpy as np

try:
    import av

    HAS_AV = True
except ImportError:
    HAS_AV = False


def log_resources(logger, label=""):
    """Log current memory usage for debugging OOM kills."""
    proc = psutil.Process()
    mem = proc.memory_info()
    vm = psutil.virtual_memory()
    logger.info(
        "  [MEM %s] process: %dMB, system: %dMB free / %dMB total (%.0f%% used)",
        label,
        mem.rss // (1024 * 1024),
        vm.available // (1024 * 1024),
        vm.total // (1024 * 1024),
        vm.percent,
    )


WORKER_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 0
NUM_WORKERS = int(sys.argv[2]) if len(sys.argv) > 2 else 1

sys.path.insert(0, r"C:\soccer-cam-label")
from map_share import map_share

logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [W{WORKER_ID}] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(rf"C:\soccer-cam-label\tiling_w{WORKER_ID}.log"),
    ],
)
logger = logging.getLogger()

SERVER = "192.168.86.152"
HOSTNAME = socket.gethostname()
LOCAL_CACHE = Path(os.environ.get("LOCAL_CACHE", r"C:\soccer-cam-label\video_cache"))
LOCAL_ZIPS = Path(os.environ.get("LOCAL_ZIPS", r"C:\soccer-cam-label\tile_zips"))

FRAME_INTERVAL = 4
DIFF_THRESHOLD = 2.0
TILE_SIZE = 640
COLS, ROWS = 7, 3
QUALITY = 95

# Map share
try:
    map_share(f"\\\\{SERVER}\\training", "DESKTOP-5L867J8\\training", "amy4ever")
    logger.info("Share mapped (worker %d of %d)", WORKER_ID, NUM_WORKERS)
except Exception as e:
    logger.error("Failed to map share: %s", e)
    sys.exit(1)

# Registry
try:
    with open(f"//{SERVER}/training/game_registry.json") as f:
        registry = {g["game_id"]: g for g in json.load(f)}
except Exception as e:
    logger.error("Failed to load registry: %s", e)
    sys.exit(1)

staging = Path(f"//{SERVER}/training/staging")
tiles_dir = Path(f"//{SERVER}/training/tiles_640")
zip_dest = Path(f"//{SERVER}/training/tile_zips")

LOCAL_CACHE.mkdir(parents=True, exist_ok=True)
LOCAL_ZIPS.mkdir(parents=True, exist_ok=True)

start = time.time()
done = 0


def claim_game(gid):
    """Try to atomically claim a game by creating its tile directory.
    Returns True if we got the claim, False if someone else already has it."""
    game_tile_dir = tiles_dir / gid
    try:
        game_tile_dir.mkdir(parents=False, exist_ok=False)
        # Write a claim file so we know who's working on it
        (game_tile_dir / ".claiming").write_text(f"{HOSTNAME} {time.time():.0f}")
        return True
    except FileExistsError:
        return False


def get_unclaimed_games():
    """Return list of games that are staged, ready, and not yet claimed."""
    unclaimed = []
    for gid in sorted(registry.keys()):
        game_dir = staging / gid
        if not game_dir.exists() or not (game_dir / "READY").exists():
            continue
        if (tiles_dir / gid).exists():
            continue
        if not list(game_dir.glob("*.mp4")):
            continue
        unclaimed.append(gid)
    return unclaimed


def tile_frame_to_zip(frame, seg_id, frame_idx, gid, zf):
    """Crop frame into tiles, JPEG-encode in memory, write to zip."""
    h, w = frame.shape[:2]
    step_x = (w - TILE_SIZE) // max(1, COLS - 1)
    step_y = (h - TILE_SIZE) // max(1, ROWS - 1)
    count = 0
    for row in range(ROWS):
        for col in range(COLS):
            x = col * step_x
            y = row * step_y
            tile = frame[y : y + TILE_SIZE, x : x + TILE_SIZE]
            _, buf = cv2.imencode(".jpg", tile, [cv2.IMWRITE_JPEG_QUALITY, QUALITY])
            arcname = f"{gid}/{seg_id}_frame_{frame_idx:06d}_r{row}_c{col}.jpg"
            zf.writestr(arcname, buf.tobytes())
            count += 1
    return count


def process_video_av(video_path, seg_id, gid, needs_flip, zf):
    """Decode with PyAV, tile in memory, write to zip."""
    container = av.open(str(video_path))
    stream = container.streams.video[0]
    fps = float(stream.average_rate or 25)
    total_frames = stream.frames or 0
    stream.thread_type = "AUTO"

    prev_frame = None
    frame_idx = 0
    extracted = 0
    total_tiles = 0

    for packet in container.demux(stream):
        try:
            for av_frame in packet.decode():
                if frame_idx % FRAME_INTERVAL == 0:
                    frame = av_frame.to_ndarray(format="bgr24")
                    if needs_flip:
                        frame = cv2.flip(frame, -1)
                    if prev_frame is not None:
                        try:
                            diff = np.mean(
                                np.abs(
                                    frame.astype(np.float32)
                                    - prev_frame.astype(np.float32)
                                )
                            )
                        except (MemoryError, ValueError):
                            frame_idx += 1
                            continue
                        if diff < DIFF_THRESHOLD:
                            frame_idx += 1
                            continue

                    total_tiles += tile_frame_to_zip(frame, seg_id, frame_idx, gid, zf)
                    prev_frame = frame.copy()
                    extracted += 1

                    if extracted % 100 == 0:
                        logger.info(
                            "    %d frames tiled (at %d/%d)",
                            extracted,
                            frame_idx,
                            total_frames,
                        )
                    if extracted % 500 == 0:
                        log_resources(logger, f"frame {frame_idx}")

                frame_idx += 1
        except av.error.InvalidDataError as e:
            logger.warning("    Corrupt packet at frame ~%d: %s", frame_idx, e)
            continue
        except MemoryError:
            logger.error("    OOM at frame %d! Stopping segment.", frame_idx)
            break
        except Exception as e:
            logger.warning(
                "    Error at frame %d: %s %s", frame_idx, type(e).__name__, e
            )
            continue

    container.close()
    return extracted, total_tiles


def process_video_cv2(video_path, seg_id, gid, needs_flip, zf):
    """Decode with cv2 fallback, tile in memory, write to zip."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        logger.error("Cannot open: %s", video_path)
        return 0, 0

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    prev_frame = None
    frame_idx = 0
    extracted = 0
    total_tiles = 0

    while True:
        try:
            ret, frame = cap.read()
        except Exception:
            frame_idx += 1
            continue
        if not ret or frame is None:
            break

        if frame_idx % FRAME_INTERVAL == 0:
            if needs_flip:
                frame = cv2.flip(frame, -1)
            if prev_frame is not None:
                try:
                    diff = np.mean(
                        np.abs(frame.astype(np.float32) - prev_frame.astype(np.float32))
                    )
                except (MemoryError, ValueError):
                    frame_idx += 1
                    continue
                if diff < DIFF_THRESHOLD:
                    frame_idx += 1
                    continue

            total_tiles += tile_frame_to_zip(frame, seg_id, frame_idx, gid, zf)
            prev_frame = frame.copy()
            extracted += 1

            if extracted % 100 == 0:
                logger.info(
                    "    %d frames tiled (at %d/%d)", extracted, frame_idx, total_frames
                )

        frame_idx += 1

    cap.release()
    return extracted, total_tiles


# Main loop — claim games from shared queue
while True:
    unclaimed = get_unclaimed_games()
    if not unclaimed:
        logger.info("No more games to tile. Done.")
        break

    logger.info("%d unclaimed games remaining: %s", len(unclaimed), unclaimed)

    # Claim ONE game at a time, then loop back for the next
    gid = None
    for candidate in unclaimed:
        if claim_game(candidate):
            gid = candidate
            break
        logger.info("Already claimed: %s", candidate)

    if gid is None:
        logger.info("All remaining games claimed by other workers. Done.")
        break

    game_dir = staging / gid
    game = registry.get(gid, {})
    needs_flip = game.get("needs_flip", False)
    videos = sorted(game_dir.glob("*.mp4"))

    logger.info("=== %s (%d segments, flip=%s) ===", gid, len(videos), needs_flip)
    log_resources(logger, "game start")

    total_frames = 0
    total_tiles = 0
    seg_zips = []

    try:
        for video in videos:
            seg_id = video.stem
            seg_zip_path = LOCAL_ZIPS / f"{gid}_{seg_id}.zip"

            # Skip if this segment already has a completed zip
            if seg_zip_path.exists() and seg_zip_path.stat().st_size > 1000:
                logger.info("  Skipping %s (already zipped)", seg_id)
                seg_zips.append(seg_zip_path)
                continue

            # Download video locally (one sequential read)
            local_video = LOCAL_CACHE / video.name
            if not local_video.exists():
                sz = video.stat().st_size / 1e9
                logger.info("  Downloading %s (%.1f GB)...", video.name, sz)
                shutil.copy2(str(video), str(local_video))

            # Decode + tile in memory → one zip per segment
            logger.info("  Processing %s...", seg_id)
            log_resources(logger, "before decode")
            with zipfile.ZipFile(seg_zip_path, "w", zipfile.ZIP_STORED) as zf:
                if HAS_AV:
                    n, nt = process_video_av(local_video, seg_id, gid, needs_flip, zf)
                else:
                    n, nt = process_video_cv2(local_video, seg_id, gid, needs_flip, zf)

            total_frames += n
            total_tiles += nt
            seg_zips.append(seg_zip_path)
            logger.info(
                "  %s: %d frames -> %d tiles (zip: %.1f MB)",
                seg_id,
                n,
                nt,
                seg_zip_path.stat().st_size / 1e6,
            )

            # Delete local video copy
            local_video.unlink(missing_ok=True)

        # Transfer segment zips to server
        zip_dest.mkdir(parents=True, exist_ok=True)
        for sz_path in seg_zips:
            logger.info(
                "Transferring %s (%.1f MB)...",
                sz_path.name,
                sz_path.stat().st_size / 1e6,
            )
            for attempt in range(3):
                try:
                    shutil.copy2(str(sz_path), str(zip_dest / sz_path.name))
                    sz_path.unlink()
                    break
                except PermissionError as pe:
                    logger.warning("Transfer attempt %d failed: %s", attempt + 1, pe)
                    if attempt < 2:
                        time.sleep(10)
                    else:
                        logger.error("Giving up on %s after 3 attempts", sz_path.name)

        # Remove the .claiming marker — tiles are the proof of completion
        (tiles_dir / gid / ".claiming").unlink(missing_ok=True)

        done += 1
        elapsed = time.time() - start
        logger.info(
            "DONE %s: %d tiles | %d games in %.0f min",
            gid,
            total_tiles,
            done,
            elapsed / 60,
        )

    except Exception as e:
        logger.exception("FAILED %s: %s", gid, e)
        # Release the claim so another worker can retry
        try:
            shutil.rmtree(tiles_dir / gid, ignore_errors=True)
        except Exception:
            pass
        continue

    # Loop back to while True to claim next game

# Cleanup
shutil.rmtree(LOCAL_CACHE, ignore_errors=True)

elapsed = time.time() - start
logger.info("=== Complete: %d games in %.0f min ===", done, elapsed / 60)
