"""Dask-based distributed work coordinator.

Embeds scheduler + local worker + dashboard in one process.
Submits labeling (GPU) and tiling (CPU) tasks to all connected workers.

Dashboard: http://localhost:8787

Usage:
    python -m training.distributed.dask_coordinator
    python -m training.distributed.dask_coordinator --games flash__06.01.2024_vs_IYSA_home
    python -m training.distributed.dask_coordinator --no-tiles
    python -m training.distributed.dask_coordinator --list-games
"""

import argparse
import logging
import time
from pathlib import Path

from dask.distributed import Client, LocalCluster

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [coordinator] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Local paths (for coordinator-side checks only)
LABEL_OUTPUT_DIR = Path(r"F:\training_data\labels_640_ext")

# UNC paths that work from any machine on the network
SHARE = "//192.168.86.152/video"
UNC_MODEL = f"{SHARE}/test/onnx_models/decrypted/balldet_fp16.onnx"
UNC_OUTPUT = f"{SHARE}/training_data/labels_640_ext"
UNC_TILES = f"{SHARE}/training_data/tiles_640"

UNC_GAMES = {
    "flash__06.01.2024_vs_IYSA_home": f"{SHARE}/Flash_2013s/06.01.2024 - vs IYSA (home)",
    "flash__09.27.2024_vs_RNYFC_Black_home": f"{SHARE}/Flash_2013s/09.27.2024 - vs RNYFC Black (home)",
    "flash__09.30.2024_vs_Chili_home": f"{SHARE}/Flash_2013s/09.30.2024 - vs Chili (home)",
    "flash__2025.06.02": f"{SHARE}/Flash_2013s/2025.06.02-18.16.03",
    "heat__05.31.2024_vs_Fairport_home": f"{SHARE}/Heat_2012s/05.31.2024 - vs Fairport (home)",
    "heat__06.20.2024_vs_Chili_away": f"{SHARE}/Heat_2012s/06.20.2024 - vs Chili (away)",
    "heat__07.17.2024_vs_Fairport_away": f"{SHARE}/Heat_2012s/07.17.2024 - vs Fairport (away)",
    "heat__Clarence_Tournament": f"{SHARE}/Heat_2012s/07.20.2024-07.21.2024 - Clarence Tournament",
    "heat__Heat_Tournament": f"{SHARE}/Heat_2012s/06.07.2024-06.09.2024 - Heat Tournament",
}


# ---------------------------------------------------------------------------
# Task functions — all imports inside for clean cloudpickle serialization
# ---------------------------------------------------------------------------


def label_game(game_id, video_src, model_path, output_base, conf, frame_interval):
    """Label one game's video segments with ball detections. Requires GPU."""
    import logging
    import socket
    from pathlib import Path

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    hn = socket.gethostname()
    vdir = Path(video_src)
    if not vdir.exists():
        return {
            "game_id": game_id,
            "status": "error",
            "hostname": hn,
            "message": f"Not found: {video_src}",
        }
    mp = Path(model_path)
    if not mp.exists():
        return {
            "game_id": game_id,
            "status": "error",
            "hostname": hn,
            "message": f"No model: {model_path}",
        }
    from label_job import run_label_job

    total = run_label_job(vdir, mp, Path(output_base) / game_id, conf, frame_interval)
    return {"game_id": game_id, "status": "done", "hostname": hn, "label_files": total}


def tile_segment(video_path, game_id, tiles_base, frame_interval):
    """Tile one video segment into 640x640 crops. CPU-only, no GPU needed.

    Encodes tiles as JPEG in memory, writes to share via background thread.
    No temp files on disk.
    """
    import logging
    import queue
    import socket
    import threading
    from pathlib import Path

    import cv2

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    log = logging.getLogger("tiler")
    hn = socket.gethostname()

    video = Path(video_path)
    seg_id = video.stem
    tiles_dir = Path(tiles_base) / game_id
    tiles_dir.mkdir(parents=True, exist_ok=True)

    TILE_SIZE = 640
    COLS, ROWS = 7, 3

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return {
            "game_id": game_id,
            "segment": seg_id,
            "status": "error",
            "hostname": hn,
            "message": f"Cannot open {video_path}",
        }

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fi = 0
    n_tiles = 0

    # Compute tile offsets
    img_w, img_h = 4096, 1800
    step_x = (img_w - TILE_SIZE) // (COLS - 1) if COLS > 1 else 0
    step_y = (img_h - TILE_SIZE) // (ROWS - 1) if ROWS > 1 else 0

    # Background writer thread — writes JPEGs to share while CPU decodes next frame
    write_queue = queue.Queue()
    writer_count = [0]

    def writer():
        while True:
            item = write_queue.get()
            if item is None:
                write_queue.task_done()
                break
            for fname, jpg_bytes in item:
                try:
                    with open(tiles_dir / fname, "wb") as f:
                        f.write(jpg_bytes)
                except OSError as e:
                    log.warning("Write failed %s: %s", fname, e)
                writer_count[0] += 1
            write_queue.task_done()

    writer_t = threading.Thread(target=writer, daemon=True)
    writer_t.start()

    FLUSH_EVERY = 50  # flush every 50 frames (~1050 tiles, ~50MB in memory)
    batch = []

    while True:
        ret = cap.grab()
        if not ret:
            break
        if fi % frame_interval == 0:
            ret, frame = cap.retrieve()
            if ret and frame is not None:
                try:
                    frame_tiles = []
                    for row in range(ROWS):
                        for col in range(COLS):
                            x0 = col * step_x
                            y0 = row * step_y
                            tile = frame[y0 : y0 + TILE_SIZE, x0 : x0 + TILE_SIZE]
                            fname = f"{seg_id}_frame_{fi:06d}_r{row}_c{col}.jpg"
                            _, jpg = cv2.imencode(
                                ".jpg", tile, [cv2.IMWRITE_JPEG_QUALITY, 95]
                            )
                            frame_tiles.append((fname, jpg.tobytes()))
                            n_tiles += 1
                    batch.extend(frame_tiles)
                except Exception as e:
                    log.warning("Frame %d failed: %s", fi, e)

            if len(batch) >= FLUSH_EVERY * COLS * ROWS:
                write_queue.put(batch)
                batch = []
        fi += 1

    # Flush remaining
    if batch:
        write_queue.put(batch)
    write_queue.put(None)
    writer_t.join()

    cap.release()
    log.info(
        "[%s] %s/%s: %d tiles from %d frames",
        hn,
        game_id,
        seg_id[:40],
        n_tiles,
        total_frames,
    )
    return {
        "game_id": game_id,
        "segment": seg_id,
        "status": "done",
        "hostname": hn,
        "tiles": n_tiles,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Dask distributed coordinator")
    parser.add_argument("--port", type=int, default=8786)
    parser.add_argument("--dashboard-port", type=int, default=8787)
    parser.add_argument("--conf", type=float, default=0.45)
    parser.add_argument("--frame-interval", type=int, default=4)
    parser.add_argument(
        "--list-games",
        action="store_true",
        help="List games and their labeling status, then exit",
    )
    parser.add_argument(
        "--games", nargs="*", help="Specific game IDs to label (default: all)"
    )
    parser.add_argument("--no-local-worker", action="store_true")
    parser.add_argument(
        "--no-tiles", action="store_true", help="Skip tile generation tasks"
    )
    args = parser.parse_args()

    if args.list_games:
        print("\nLabeling Status:")
        print("-" * 70)
        for game_id in sorted(UNC_GAMES):
            label_dir = LABEL_OUTPUT_DIR / game_id
            if label_dir.exists():
                count = len(list(label_dir.glob("*.txt")))
                if count > 0:
                    print(f"  DONE  ({count:>6} labels) {game_id}")
                    continue
            print(f"  TODO                   {game_id}")
        print()
        return

    # Start embedded scheduler + local worker + dashboard
    logger.info(
        "Starting Dask scheduler on port %d (dashboard: %d)",
        args.port,
        args.dashboard_port,
    )

    cluster = LocalCluster(
        n_workers=0 if args.no_local_worker else 1,
        threads_per_worker=2,  # 1 GPU label + 1 CPU tile in parallel
        scheduler_port=args.port,
        dashboard_address=f":{args.dashboard_port}",
        resources={} if args.no_local_worker else {"GPU": 1},
        host="0.0.0.0",
        memory_limit=0,
        name="local-gtx1060",
        local_directory="F:/tmp/dask",  # keep scratch off C: drive
    )
    client = Client(cluster)

    # Upload label_job so all workers can import it
    client.upload_file("training/distributed/label_job.py")

    logger.info("Dashboard: http://localhost:%d", args.dashboard_port)
    logger.info("Scheduler: %s", client.scheduler.address)
    logger.info("Workers: %d", len(client.scheduler_info()["workers"]))

    # Determine which games to label
    games_to_label = (
        {g: UNC_GAMES[g] for g in args.games if g in UNC_GAMES}
        if args.games
        else dict(UNC_GAMES)
    )

    futures = {}

    # Submit labeling tasks (GPU)
    if games_to_label:
        logger.info("Submitting %d labeling jobs (GPU)...", len(games_to_label))
        for game_id, video_src in games_to_label.items():
            logger.info("  label: %s", game_id)
            fut = client.submit(
                label_game,
                game_id,
                video_src,
                UNC_MODEL,
                UNC_OUTPUT,
                args.conf,
                args.frame_interval,
                key=f"label-{game_id}",
                resources={"GPU": 1},
                pure=False,
            )
            futures[f"label-{game_id}"] = fut

    # Submit tiling tasks (CPU only — runs alongside GPU labeling)
    if not args.no_tiles:
        tile_count = 0
        for game_id, video_src in UNC_GAMES.items():
            video_dir = Path(video_src)
            if not video_dir.exists():
                continue
            segments = sorted(
                [p for p in video_dir.rglob("*.mp4") if "[F][0@0]" in p.name]
            )
            for seg in segments:
                key = f"tile-{game_id}-{seg.stem[:30]}"
                fut = client.submit(
                    tile_segment,
                    str(seg),
                    game_id,
                    UNC_TILES,
                    args.frame_interval,
                    key=key,
                    pure=False,
                )
                futures[key] = fut
                tile_count += 1
        if tile_count:
            logger.info("Submitted %d tiling jobs (CPU)...", tile_count)

    # Monitor all tasks
    total = len(futures)
    logger.info("Monitoring %d total jobs...", total)
    completed = set()
    try:
        while len(completed) < total:
            for key, fut in futures.items():
                if key in completed:
                    continue
                if fut.done():
                    try:
                        result = fut.result()
                        logger.info("COMPLETED: %s -- %s", key, result)
                    except Exception as e:
                        logger.error("FAILED: %s -- %s", key, e)
                    completed.add(key)

            label_done = sum(1 for k in completed if k.startswith("label-"))
            tile_done = sum(1 for k in completed if k.startswith("tile-"))
            label_total = sum(1 for k in futures if k.startswith("label-"))
            tile_total = sum(1 for k in futures if k.startswith("tile-"))
            info = client.scheduler_info()
            logger.info(
                "Labels: %d/%d  Tiles: %d/%d  Workers: %d",
                label_done,
                label_total,
                tile_done,
                tile_total,
                len(info["workers"]),
            )
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info("Interrupted.")

    # Keep alive for remote workers
    logger.info("All jobs submitted. Scheduler running. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Shutting down.")
        client.close()
        cluster.close()


if __name__ == "__main__":
    main()
