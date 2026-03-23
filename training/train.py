"""Train YOLO26 ball detector on annotated tile dataset.

Trains both nano (mobile-first) and small (PC fallback) variants.
Supports epoch rotation: pre-generate N train files with different negative
samples and rotate between them each epoch for better coverage.
"""

import argparse
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _make_epoch_rotator(data_yaml: Path, n_variants: int):
    """Create a callback that rotates train.txt variants each epoch.

    Looks for train_0.txt, train_1.txt, ... train_{n-1}.txt in the dataset
    directory. Falls back to train.txt if variants don't exist.
    """
    import yaml

    yaml_path = Path(data_yaml)

    def rotate_dataset(trainer):
        epoch = trainer.epoch
        variant_idx = epoch % n_variants

        with open(yaml_path) as f:
            cfg = yaml.safe_load(f)

        dataset_dir = Path(cfg.get("path", yaml_path.parent))
        variant_file = dataset_dir / f"train_{variant_idx}.txt"

        if variant_file.exists():
            cfg["train"] = str(variant_file).replace("\\", "/")
            with open(yaml_path, "w") as f:
                yaml.safe_dump(cfg, f, default_flow_style=False)
            logger.info("Epoch %d: rotated to %s", epoch, variant_file.name)
        else:
            logger.debug(
                "Epoch %d: variant %s not found, keeping current train file",
                epoch,
                variant_file.name,
            )

    return rotate_dataset


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
    epoch_rotation: int = 0,
    fraction: float = 1.0,
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
        epoch_rotation: Number of train.txt variants to rotate through (0=disabled).
            Expects train_0.txt, train_1.txt, ... in the dataset directory.
        fraction: Fraction of dataset to use (0.0-1.0). Useful for faster iteration.
    """
    from ultralytics import YOLO

    model = YOLO(model_name)

    if epoch_rotation > 0:
        rotator = _make_epoch_rotator(data_yaml, epoch_rotation)
        model.add_callback("on_train_epoch_start", rotator)
        logger.info("Epoch rotation enabled with %d variants", epoch_rotation)

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
        workers=0,
        fraction=fraction,
    )
    logger.info("Training complete. Best model: %s/%s/weights/best.pt", project, name)
    return results


def main():
    parser = argparse.ArgumentParser(description="Train YOLO26 ball detector")
    parser.add_argument(
        "--data",
        type=Path,
        default=Path("training/configs/ball_dataset_640.yaml"),
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
    parser.add_argument(
        "--project",
        type=str,
        default=str(Path(__file__).resolve().parent / "runs"),
        help="Output directory for training runs",
    )
    parser.add_argument(
        "--epoch-rotation",
        type=int,
        default=0,
        help="Number of train.txt variants to rotate (0=disabled)",
    )
    parser.add_argument(
        "--fraction",
        type=float,
        default=1.0,
        help="Fraction of dataset to use (0.0-1.0)",
    )
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
        project=args.project,
        epoch_rotation=args.epoch_rotation,
        fraction=args.fraction,
    )


if __name__ == "__main__":
    main()
