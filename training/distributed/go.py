"""Quick start: copy shards from D: share, extract, train."""
import os
import shutil
import sys
import tarfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from map_share import map_share

# Map the TRAINING share (D: internal HDD, fast)
map_share(r"\\192.168.86.152\training", r"DESKTOP-5L867J8\training", "amy4ever")
print("Training share mapped", flush=True)

local = "C:/soccer-cam-label/dataset_v2"
share = "//192.168.86.152/training/shards_v2"
cache = "C:/soccer-cam-label/shards_cache"

os.makedirs(cache, exist_ok=True)
os.makedirs(local, exist_ok=True)

# Phase 1: Copy available tars to local SSD
print("Phase 1: Copying tars from share...", flush=True)
start = time.time()
n = 0
for root, dirs, files in os.walk(share):
    for f in sorted(files):
        if not f.endswith(".tar"):
            continue
        src = os.path.join(root, f)
        rel = os.path.relpath(src, share)
        dst = os.path.join(cache, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not os.path.exists(dst):
            try:
                shutil.copy2(src, dst)
            except (PermissionError, OSError):
                continue  # skip locked/in-progress files
        n += 1
        if n % 20 == 0:
            elapsed = time.time() - start
            print(f"  {n} tars copied ({elapsed:.0f}s)", flush=True)
print(f"Phase 1 done: {n} tars in {time.time() - start:.0f}s", flush=True)

# Phase 2: Extract locally
print("Phase 2: Extracting...", flush=True)
start2 = time.time()
n2 = 0
for root, dirs, files in os.walk(cache):
    for f in sorted(files):
        if not f.endswith(".tar"):
            continue
        try:
            with tarfile.open(os.path.join(root, f)) as t:
                t.extractall(local)
            n2 += 1
        except Exception as e:
            print(f"  SKIP corrupt: {f} ({e})", flush=True)
        if n2 % 50 == 0:
            print(f"  {n2} extracted", flush=True)
print(f"Phase 2 done: {n2} extracted in {time.time() - start2:.0f}s", flush=True)

# Ensure val exists
val_img = os.path.join(local, "images", "val")
if not os.path.exists(val_img) or not os.listdir(val_img):
    print("Moving heat__05.31 to val...", flush=True)
    for sub in ["images", "labels"]:
        src = os.path.join(local, sub, "train", "heat__05.31.2024_vs_Fairport_home")
        dst = os.path.join(local, sub, "val", "heat__05.31.2024_vs_Fairport_home")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(src):
            shutil.move(src, dst)

# Write yaml
with open(os.path.join(local, "dataset.yaml"), "w") as f:
    f.write(f"path: {local}\ntrain: images/train\nval: images/val\n\n")
    f.write('nc: 3\nnames: ["game_ball", "static_ball", "not_ball"]\n')

# Clean cache
shutil.rmtree(cache, ignore_errors=True)
print("Ready for training!", flush=True)

# Train
from ultralytics import YOLO

model = YOLO("yolo11s.pt")
model.train(
    data=os.path.join(local, "dataset.yaml"),
    epochs=100, imgsz=640, batch=16, device=0,
    project=os.path.join(local, "runs"), name="ball_v2",
    patience=30, workers=4, deterministic=True,
)
