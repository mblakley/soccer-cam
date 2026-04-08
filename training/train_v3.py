"""Train v3 from manifest.db + pack files.

Uses ManifestTrainer to read images from pack files and labels from SQLite.
No .txt files, no loose .jpg files, no network access.

Usage:
    # On the laptop (after transferring training set):
    python -u train_v3.py --data C:/soccer-cam-label/training_sets/v3.1/dataset.yaml

    # With custom settings:
    python -u train_v3.py --data dataset.yaml --epochs 50 --batch 16 --model yolo26l.pt
"""
import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger()


def main():
    parser = argparse.ArgumentParser(description="Train v3 from manifest + packs")
    parser.add_argument("--data", type=Path, required=True, help="Path to dataset.yaml")
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--device", default="0")
    parser.add_argument("--model", default="yolo26l.pt")
    parser.add_argument("--resume", type=Path, default=None, help="Resume from checkpoint")
    parser.add_argument("--project", type=Path, default=None)
    parser.add_argument("--name", default="ball_v3")
    args = parser.parse_args()

    if not args.data.exists():
        logger.error("Dataset YAML not found: %s", args.data)
        sys.exit(1)

    # Default project to same dir as dataset
    if args.project is None:
        args.project = args.data.parent / "runs"

    # Try project import, fall back to local (standalone laptop deploy)
    try:
        from training.data_prep.manifest_dataset import ManifestTrainer
    except ImportError:
        sys.path.insert(0, str(Path(__file__).parent))
        sys.path.insert(0, ".")
        from manifest_dataset import ManifestTrainer
    from ultralytics import YOLO

    model_path = str(args.resume) if args.resume else args.model
    model = YOLO(model_path)

    logger.info("Training v3: %s, %d epochs, batch %d, device %s",
                model_path, args.epochs, args.batch, args.device)
    logger.info("Dataset: %s", args.data)

    model.train(
        data=str(args.data),
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device=args.device,
        project=str(args.project),
        name=args.name,
        patience=30,
        workers=0,
        deterministic=True,
        exist_ok=True,
        trainer=ManifestTrainer,
    )

    logger.info("Training complete!")


if __name__ == "__main__":
    main()
