"""Training from tar shards — extract once locally, train fast.

Copies shard .tar files from the share, extracts to local disk,
then trains YOLO from local files. No SMB during training.

Usage on remote machines:
    python shard_train.py
    python shard_train.py --local-dir D:/dataset_v2 --epochs 150
"""

import argparse
import os
import shutil
import sys
import tarfile
import time
from pathlib import Path

# Share mapping
SHARE_UNC = r"\\192.168.86.152\video"
SHARDS_DIR = "//192.168.86.152/video/training_data/shards_v2"


def ensure_share():
    """Map share via WNet API."""
    script_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(script_dir))
    try:
        from map_share import map_share

        return map_share(
            SHARE_UNC,
            os.environ.get("SHARE_USER", r"DESKTOP-5L867J8\training"),
            os.environ.get("SHARE_PASS", "amy4ever"),
        )
    except Exception:
        return os.path.exists(SHARDS_DIR)


def extract_shards(shards_dir: Path, local_dir: Path):
    """Extract all .tar shards to local dataset directory."""
    local_dir.mkdir(parents=True, exist_ok=True)

    # Check if already extracted
    marker = local_dir / ".extracted"
    if marker.exists():
        print("Dataset already extracted locally", flush=True)
        return

    print(f"Extracting shards from {shards_dir} to {local_dir}...", flush=True)
    start = time.time()

    for shard in sorted(shards_dir.glob("*.tar")):
        print(f"  {shard.name}...", end=" ", flush=True)
        t0 = time.time()
        with tarfile.open(shard, "r") as tar:
            tar.extractall(local_dir)
        print(f"{time.time() - t0:.0f}s", flush=True)

    # Copy cache files
    for cache in shards_dir.glob("*.cache"):
        # Put caches in the right spot
        dest = local_dir / "labels" / cache.stem.replace(".", "/") / cache.name
        # Actually just copy to labels/train/ and labels/val/
        for split_dir in (local_dir / "labels").iterdir():
            if split_dir.is_dir():
                for game_dir in split_dir.iterdir():
                    if game_dir.is_dir() and cache.stem.startswith(game_dir.name[:10]):
                        shutil.copy2(cache, split_dir / cache.name)
                        break

    marker.touch()
    elapsed = time.time() - start
    print(f"Extraction complete in {elapsed:.0f}s", flush=True)


def write_dataset_yaml(local_dir: Path) -> Path:
    """Write dataset.yaml pointing to local directory."""
    yaml_path = local_dir / "dataset.yaml"
    yaml_path.write_text(
        f"path: {str(local_dir).replace(chr(92), '/')}\n"
        f"train: images/train\n"
        f"val: images/val\n"
        f"\n"
        f"nc: 3\n"
        f'names: ["game_ball", "static_ball", "not_ball"]\n'
    )
    return yaml_path


def train(local_dir: Path, epochs: int, patience: int):
    """Run YOLO training from local dataset."""
    import torch
    from ultralytics import YOLO

    gpu_name = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"
    print(f"GPU: {gpu_name}", flush=True)

    if "4070" in gpu_name:
        model_name, run_name, batch = "yolo11s.pt", "ball_v2_3class", 16
    elif "3060" in gpu_name:
        model_name, run_name, batch = "yolo11n.pt", "ball_v2_3class_3060", 16
    else:
        model_name, run_name, batch = "yolo11n.pt", "ball_v2_3class_nano", 8

    yaml_path = write_dataset_yaml(local_dir)
    print(f"Training {model_name} batch={batch} as {run_name}", flush=True)

    model = YOLO(model_name)
    model.train(
        data=str(yaml_path),
        epochs=epochs,
        imgsz=640,
        batch=batch,
        device=0,
        project=str(local_dir / "runs"),
        name=run_name,
        patience=patience,
        workers=4,  # local disk can handle parallel loading
        deterministic=True,
    )

    # Copy best weights back to share
    best_pt = local_dir / "runs" / run_name / "weights" / "best.pt"
    if best_pt.exists():
        share_dest = Path(SHARDS_DIR).parent / "models" / f"{run_name}_best.pt"
        share_dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(best_pt, share_dest)
            print(f"Best weights saved to share: {share_dest}", flush=True)
        except OSError as e:
            print(f"Could not copy weights to share: {e}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Train from tar shards")
    parser.add_argument(
        "--local-dir",
        type=Path,
        default=Path("C:/soccer-cam-label/dataset_v2"),
        help="Local directory to extract dataset to",
    )
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--patience", type=int, default=30)
    args = parser.parse_args()

    print("Shard trainer starting", flush=True)

    if not ensure_share():
        print("ERROR: Cannot access share", flush=True)
        return

    shards = Path(SHARDS_DIR)
    if not shards.exists():
        print(f"ERROR: Shards dir not found: {shards}", flush=True)
        return

    extract_shards(shards, args.local_dir)
    train(args.local_dir, args.epochs, args.patience)


if __name__ == "__main__":
    main()
