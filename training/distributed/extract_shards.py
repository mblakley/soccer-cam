"""Copy tar shards locally, then extract. Two-phase for speed."""
import os
import shutil
import sys
import tarfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from map_share import map_share

map_share(r"\\192.168.86.152\video", r"DESKTOP-5L867J8\training", "amy4ever")
print("Share mapped", flush=True)

local = "C:/soccer-cam-label/dataset_v2"
tars_local = "C:/soccer-cam-label/shards_cache"
share = "//192.168.86.152/video/training_data/shards_v2"

os.makedirs(local, exist_ok=True)
os.makedirs(tars_local, exist_ok=True)

# Phase 1: Copy all tars to local disk (sequential network reads — fast)
print("Phase 1: Copying tars to local disk...", flush=True)
start = time.time()
n = 0
total_bytes = 0
for root, dirs, files in os.walk(share):
    for f in sorted(files):
        if not f.endswith(".tar"):
            continue
        src = os.path.join(root, f)
        # Preserve relative path
        rel = os.path.relpath(src, share)
        dst = os.path.join(tars_local, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not os.path.exists(dst):
            shutil.copy2(src, dst)
            total_bytes += os.path.getsize(dst)
        n += 1
        if n % 50 == 0:
            elapsed = time.time() - start
            mb = total_bytes / 1024 / 1024
            rate = mb / elapsed if elapsed > 0 else 0
            print(f"  {n} tars copied ({mb:.0f} MB, {rate:.0f} MB/s)", flush=True)

elapsed = time.time() - start
mb = total_bytes / 1024 / 1024
print(f"Phase 1 done: {n} tars, {mb:.0f} MB in {elapsed:.0f}s", flush=True)

# Phase 2: Extract from local disk (all local I/O — fast)
print("Phase 2: Extracting locally...", flush=True)
start2 = time.time()
extracted = 0
for root, dirs, files in os.walk(tars_local):
    for f in sorted(files):
        if not f.endswith(".tar"):
            continue
        with tarfile.open(os.path.join(root, f)) as t:
            t.extractall(local)
        extracted += 1
        if extracted % 100 == 0:
            print(f"  {extracted} shards extracted", flush=True)

elapsed2 = time.time() - start2
print(f"Phase 2 done: {extracted} shards extracted in {elapsed2:.0f}s", flush=True)

# Clean up tar cache
shutil.rmtree(tars_local)
print("Tar cache cleaned", flush=True)

# Move one train game to val if no val exists
val_dir = os.path.join(local, "images", "val")
if not os.path.exists(val_dir) or not os.listdir(val_dir):
    print("No val data — moving heat__05.31 to val", flush=True)
    os.makedirs(os.path.join(local, "images", "val"), exist_ok=True)
    os.makedirs(os.path.join(local, "labels", "val"), exist_ok=True)
    for sub in ["images", "labels"]:
        src = os.path.join(local, sub, "train", "heat__05.31.2024_vs_Fairport_home")
        dst = os.path.join(local, sub, "val", "heat__05.31.2024_vs_Fairport_home")
        if os.path.exists(src):
            shutil.move(src, dst)

# Write dataset yaml
with open(os.path.join(local, "dataset.yaml"), "w") as f:
    f.write(f"path: {local}\n")
    f.write("train: images/train\nval: images/val\n\n")
    f.write('nc: 3\nnames: ["game_ball", "static_ball", "not_ball"]\n')

total = time.time() - start
print(f"Total: {total:.0f}s. Ready for training!", flush=True)
