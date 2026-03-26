"""Full labeling pipeline: map share, copy videos, label, copy results back.

Reads connection details from environment variables or a config file.

Usage:
    # Set env vars and run:
    set SHARE_SERVER=192.168.86.152
    set SHARE_USER=DESKTOP-5L867J8\training
    set SHARE_PASS=<password>
    python full_pipeline.py

    # Or use a config file:
    python full_pipeline.py --config label_config.json
"""

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

os.chdir(r"C:\soccer-cam-label")
sys.path.insert(0, r"C:\soccer-cam-label")

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler("labeling.log", mode="w"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger()


def load_config(config_path=None):
    """Load share connection config from file or environment."""
    if config_path and Path(config_path).exists():
        with open(config_path) as f:
            return json.load(f)
    return {
        "server": os.environ.get("SHARE_SERVER", ""),
        "user": os.environ.get("SHARE_USER", ""),
        "password": os.environ.get("SHARE_PASS", ""),
        "share_name": os.environ.get("SHARE_NAME", "video"),
    }


def map_share(cfg):
    """Map network share to Z: drive."""
    server = cfg["server"]
    user = cfg["user"]
    password = cfg["password"]
    share = cfg.get("share_name", "video")

    if not server or not password:
        logger.error("SHARE_SERVER and SHARE_PASS must be set")
        return False

    unc = f"\\\\{server}\\{share}"
    logger.info("Mapping share %s...", unc)
    subprocess.run(["net", "use", "Z:", "/delete", "/y"], capture_output=True)
    r = subprocess.run(
        ["net", "use", "Z:", unc, f"/user:{user}", password, "/persistent:yes"],
        capture_output=True,
        text=True,
    )
    logger.info("net use: %s %s", r.stdout.strip(), r.stderr.strip())
    return Path("Z:/").exists()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    if not map_share(cfg):
        logger.error("Share mapping failed")
        return

    games = {
        "heat__05.31.2024_vs_Fairport_home": r"Z:\Heat_2012s\05.31.2024 - vs Fairport (home)",
        "heat__06.20.2024_vs_Chili_away": r"Z:\Heat_2012s\06.20.2024 - vs Chili (away)",
        "heat__07.17.2024_vs_Fairport_away": r"Z:\Heat_2012s\07.17.2024 - vs Fairport (away)",
        "heat__Clarence_Tournament": r"Z:\Heat_2012s\07.20.2024-07.21.2024 - Clarence Tournament",
        "heat__Heat_Tournament": r"Z:\Heat_2012s\06.07.2024-06.09.2024 - Heat Tournament",
    }

    from label_job import run_label_job

    for game_id, src in games.items():
        dest = Path(f"videos/{game_id}")
        dest.mkdir(parents=True, exist_ok=True)
        if not list(dest.rglob("*.mp4")):
            logger.info("Copying %s...", game_id)
            subprocess.run(
                ["robocopy", src, str(dest), "*.mp4", "/S", "/MT:4"],
                capture_output=True,
            )
            logger.info("  Copied %d mp4 files", len(list(dest.rglob("*.mp4"))))

        output_dir = Path(f"output/{game_id}")
        logger.info("=== Labeling %s ===", game_id)
        run_label_job(
            video_dir=dest,
            model_path=Path("models/balldet_fp16.onnx"),
            output_dir=output_dir,
            conf=0.45,
            frame_interval=4,
        )

    # Copy results back
    logger.info("Copying results to share...")
    for game_id in games:
        src_dir = Path(f"output/{game_id}")
        if src_dir.exists() and list(src_dir.glob("*.txt")):
            dest_dir = rf"Z:\training_data\labels_640_ext\{game_id}"
            subprocess.run(
                ["robocopy", str(src_dir), dest_dir, "/MIR", "/MT:8"],
                capture_output=True,
            )
            logger.info("  Copied %s", game_id)

    logger.info("=== ALL DONE ===")


if __name__ == "__main__":
    main()
