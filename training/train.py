"""Train YOLO26 ball detector on annotated tile dataset.

Trains both nano (mobile-first) and small (PC fallback) variants.
"""

import argparse
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def train(
    data_yaml: Path,
    model_name: str = "yolo26n.pt",
    epochs: int = 150,
    imgsz: int = 640,
    batch: int = 32,
    patience: int = 30,
    device: str = "0",
    project: str = str(Path(__file__).resolve().parent / "runs"),
    name: str = "ball_v1",
):
    """Train a YOLO26 model for ball detection.

    Args:
        data_yaml: Path to dataset YAML config
        model_name: Pretrained model to start from (yolo26n.pt or yolo26s.pt)
        epochs: Maximum training epochs
        imgsz: Input image size
        batch: Batch size (32 for nano on 3060 Ti, 16 for small)
        patience: Early stopping patience
        device: Training device ("0" for GPU, "cpu" for CPU)
        project: Output directory for training runs
        name: Run name
    """
    from ultralytics import YOLO

    model = YOLO(model_name)
    results = model.train(
        data=str(data_yaml),
        epochs=epochs,
        imgsz=imgsz,
        batch=batch,
        patience=patience,
        device=device,
        project=project,
        name=name,
        # Small-object optimizations
        mosaic=1.0,
        mixup=0.1,
        copy_paste=0.3,
        scale=0.9,
        # Augmentations
        flipud=0.0,  # no vertical flip (field has up/down orientation)
        fliplr=0.5,
        hsv_h=0.015,
        hsv_s=0.7,
        hsv_v=0.4,
        # Training
        workers=4,
    )
    logger.info("Training complete. Best model: %s/%s/weights/best.pt", project, name)
    return results


def main():
    parser = argparse.ArgumentParser(description="Train YOLO26 ball detector")
    parser.add_argument(
        "--data",
        type=Path,
        default=Path("training/configs/ball_dataset.yaml"),
        help="Dataset YAML config",
    )
    parser.add_argument(
        "--model",
        default="yolo26n.pt",
        help="Pretrained model (yolo26n.pt or yolo26s.pt)",
    )
    parser.add_argument("--epochs", type=int, default=150)
    parser.add_argument("--batch", type=int, default=32)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument(
        "--device", default="0", help="Device: '0' for GPU, 'cpu' for CPU"
    )
    parser.add_argument("--name", default="ball_v1", help="Run name")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    train(
        args.data,
        args.model,
        args.epochs,
        args.imgsz,
        args.batch,
        device=args.device,
        name=args.name,
    )


if __name__ == "__main__":
    main()
