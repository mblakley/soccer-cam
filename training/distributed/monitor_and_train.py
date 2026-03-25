"""Monitor labeling progress and start training when done.

Checks both local and remote GPU labeling jobs.
When all games are labeled, updates dataset junctions and starts training.
"""
import subprocess
import time
import json
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger()

LABELS_DIR = Path("F:/training_data/labels_640_ext")
DATASET_DIR = Path("F:/training_data/ball_dataset_640")
EXPECTED_GAMES = [
    "flash__06.01.2024_vs_IYSA_home",
    "flash__09.27.2024_vs_RNYFC_Black_home",
    "flash__09.30.2024_vs_Chili_home",
    "flash__2025.06.02",
    "heat__05.31.2024_vs_Fairport_home",
    "heat__06.20.2024_vs_Chili_away",
    "heat__07.17.2024_vs_Fairport_away",
    "heat__Clarence_Tournament",
    "heat__Heat_Tournament",
]

def check_labels():
    """Check how many games have labels."""
    done = {}
    for game in EXPECTED_GAMES:
        game_dir = LABELS_DIR / game
        if game_dir.exists():
            count = len(list(game_dir.glob("*.txt")))
            done[game] = count
        else:
            done[game] = 0
    return done

def update_junctions():
    """Repoint dataset label junctions to ext labels."""
    for split in ["train", "val"]:
        label_dir = DATASET_DIR / "labels" / split
        for game_dir in label_dir.iterdir():
            if game_dir.is_symlink() or game_dir.is_junction():
                game_name = game_dir.name
                ext_dir = LABELS_DIR / game_name
                if ext_dir.exists() and len(list(ext_dir.glob("*.txt"))) > 0:
                    # Remove old junction and create new one
                    game_dir.unlink()
                    # Windows junction
                    subprocess.run(
                        ["cmd", "/c", "mklink", "/J", str(game_dir), str(ext_dir)],
                        capture_output=True
                    )
                    logger.info("Repointed %s/%s -> %s", split, game_name, ext_dir)

def start_training():
    """Start YOLO training."""
    logger.info("Starting training...")
    subprocess.Popen([
        "uv", "run", "python", "-m", "training.train",
        "--data", "training/configs/ball_dataset_640.yaml",
        "--model", "yolo11m.pt",
        "--epochs", "100",
        "--batch", "4",
        "--project", "F:/training_data/runs",
        "--name", "ball_ext_labels",
    ])
    logger.info("Training started!")

if __name__ == "__main__":
    while True:
        labels = check_labels()
        total_games = len(EXPECTED_GAMES)
        done_games = sum(1 for v in labels.values() if v > 100)
        total_labels = sum(labels.values())
        
        logger.info("Labels: %d/%d games done, %d total labels", done_games, total_games, total_labels)
        for game, count in sorted(labels.items()):
            status = "DONE" if count > 100 else "pending" if count == 0 else "partial"
            logger.info("  %s: %d (%s)", game, count, status)
        
        if done_games >= total_games:
            logger.info("ALL GAMES LABELED! Starting training pipeline...")
            update_junctions()
            start_training()
            break
        
        time.sleep(300)  # Check every 5 minutes
