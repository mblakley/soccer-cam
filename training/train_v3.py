"""Build v3 dataset and start training.

Reads tiles from D:/tiles_640 and labels from D:/labels_640_ext via network share.
Builds a YOLO dataset with train/val split, then trains YOLO26l.

Usage:
    python -u train_v3.py
"""
import json
import logging
import os
import random
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, r"C:\soccer-cam-label")
from map_share import map_share

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(r"C:\soccer-cam-label\train_v3.log"),
    ],
)
logger = logging.getLogger()

SERVER = "192.168.86.152"

# Map shares
try:
    map_share(f"\\\\{SERVER}\\training", f"DESKTOP-5L867J8\\training", "amy4ever")
    map_share(f"\\\\{SERVER}\\video", f"DESKTOP-5L867J8\\training", "amy4ever")
    logger.info("Shares mapped")
except Exception as e:
    logger.warning("Share mapping: %s", e)

# Paths
tiles_base = Path(f"//{SERVER}/training/tiles_640")
labels_base = Path(f"//{SERVER}/training/labels_640_ext")
dataset_dir = Path(r"C:\soccer-cam-label\dataset_v3")
registry_path = Path(f"//{SERVER}/training/game_registry.json")

VAL_SPLIT = 0.15
SEED = 42

# Load registry
with open(registry_path) as f:
    registry = {g["game_id"]: g for g in json.load(f)}
logger.info("Registry: %d games", len(registry))

# Find games that have BOTH tiles and labels
games_ready = []
for gid in sorted(registry):
    tiles_dir = tiles_base / gid
    labels_dir = labels_base / gid
    if not tiles_dir.exists() or not labels_dir.exists():
        continue
    # Quick check for actual files
    has_tiles = any(True for _ in tiles_dir.glob("*.jpg"))
    has_labels = any(True for _ in labels_dir.glob("*.txt"))
    if has_tiles and has_labels:
        games_ready.append(gid)
    else:
        logger.info("Skip %s: tiles=%s labels=%s", gid, has_tiles, has_labels)

logger.info("Games ready for training: %d/%d", len(games_ready), len(registry))
for g in games_ready:
    logger.info("  %s", g)

if not games_ready:
    logger.error("No games ready!")
    sys.exit(1)

# Build dataset: collect all tile/label pairs
logger.info("Building dataset...")
all_pairs = []  # (tile_path, label_path)

for gid in games_ready:
    tiles_dir = tiles_base / gid
    labels_dir = labels_base / gid

    # Find tiles that have matching labels
    label_stems = set()
    for lf in labels_dir.iterdir():
        if lf.suffix == ".txt":
            label_stems.add(lf.stem)

    for tf in tiles_dir.iterdir():
        if tf.suffix == ".jpg" and tf.stem in label_stems:
            all_pairs.append((str(tf), str(labels_dir / f"{tf.stem}.txt")))

logger.info("Total tile/label pairs: %d", len(all_pairs))

# Train/val split
random.seed(SEED)
random.shuffle(all_pairs)
val_count = int(len(all_pairs) * VAL_SPLIT)
val_pairs = all_pairs[:val_count]
train_pairs = all_pairs[val_count:]
logger.info("Train: %d, Val: %d", len(train_pairs), len(val_pairs))

# Create dataset directory structure
for split in ["train", "val"]:
    (dataset_dir / "images" / split).mkdir(parents=True, exist_ok=True)
    (dataset_dir / "labels" / split).mkdir(parents=True, exist_ok=True)

# Write train.txt and val.txt with image paths (YOLO format)
with open(dataset_dir / "train.txt", "w") as f:
    for tile_path, _ in train_pairs:
        f.write(tile_path + "\n")

with open(dataset_dir / "val.txt", "w") as f:
    for tile_path, _ in val_pairs:
        f.write(tile_path + "\n")

# Write dataset.yaml
yaml_content = f"""path: {dataset_dir}
train: train.txt
val: val.txt

nc: 1
names: ['ball']
"""
with open(dataset_dir / "dataset.yaml", "w") as f:
    f.write(yaml_content)

logger.info("Dataset YAML written to %s", dataset_dir / "dataset.yaml")

# Start training
logger.info("Starting YOLO26l training...")
from ultralytics import YOLO

model = YOLO("yolo26l.pt")
model.train(
    data=str(dataset_dir / "dataset.yaml"),
    epochs=50,
    imgsz=640,
    batch=16,
    device=0,
    project=str(dataset_dir / "runs"),
    name="ball_v3",
    patience=30,
    workers=0,
    deterministic=True,
)

logger.info("Training complete!")
