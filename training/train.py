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


# --- v3 far-ball recall hyperparameters -----------------------------------
# Carried over from the v2->v3 analysis. v2 far recall was ~0.29; these target
# small bright-blob balls (esp. in the far field) and reject person false
# positives. See training/ROADMAP.md and training/docs/EXPERIMENTS.md.
V3_MODEL = "yolo26l.pt"  # large variant: STAL + ProgLoss help tiny objects
V3_MULTI_SCALE = 0.5  # vary input scale +/-50%: balls span ~8px (far)..30px (near)
V3_MOSAIC = 0.5  # was 1.0; lower mosaic keeps small far balls intact in tiles
V3_COPY_PASTE = 0.3  # keep — pastes rare ball instances around the frame
V3_CLS = 1.5  # was 0.5; push classification to reject false-positives-on-people
V3_COS_LR = True  # cosine LR anneal (v2 flat decay overfit after warmup)
V3_LR0 = 0.002  # midpoint of the 0.001-0.005 range
V3_PATIENCE = 15  # was 30; cos_lr converges faster, stop overfitting sooner
V3_FREEZE = 8  # freeze backbone for first ~5-10 epochs (midpoint), warm the head
V3_HSV_V = 0.2  # was 0.4; lower value jitter preserves ball/grass brightness contrast


def train(
    data_yaml: Path,
    model_name: str = V3_MODEL,
    epochs: int = 150,
    imgsz: int = 640,
    batch: int = 32,
    patience: int = V3_PATIENCE,
    device: str = "0",
    project: str = str(Path(__file__).resolve().parent / "runs"),
    name: str = "ball_v3",
    epoch_rotation: int = 0,
    fraction: float = 1.0,
    multi_scale: float = V3_MULTI_SCALE,
    mosaic: float = V3_MOSAIC,
    copy_paste: float = V3_COPY_PASTE,
    cls: float = V3_CLS,
    cos_lr: bool = V3_COS_LR,
    lr0: float = V3_LR0,
    freeze: int = V3_FREEZE,
    hsv_v: float = V3_HSV_V,
):
    """Train a YOLO26 model for ball detection (v3 far-ball recall config).

    Args:
        data_yaml: Path to dataset YAML config
        model_name: Pretrained model to start from (default yolo26l.pt)
        epochs: Maximum training epochs
        imgsz: Input image size
        batch: Batch size (32 for nano on 3060 Ti, 16 for small/large)
        patience: Early stopping patience
        device: Training device ("0" for GPU, "cpu" for CPU)
        project: Output directory for training runs
        name: Run name
        epoch_rotation: Number of train.txt variants to rotate through (0=disabled).
            Expects train_0.txt, train_1.txt, ... in the dataset directory.
        fraction: Fraction of dataset to use (0.0-1.0). Useful for faster iteration.
        multi_scale: Random input-scale jitter (helps balls at varied distances).
        mosaic: Mosaic augmentation probability.
        copy_paste: Copy-paste augmentation probability.
        cls: Classification loss gain (higher rejects person false positives).
        cos_lr: Use cosine LR annealing.
        lr0: Initial learning rate.
        freeze: Number of backbone layers to freeze for the first epochs.
        hsv_v: HSV value (brightness) augmentation magnitude.
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
        # Learning-rate schedule
        cos_lr=cos_lr,
        lr0=lr0,
        freeze=freeze,  # freeze backbone for the first epochs (warm the head)
        # Small-object optimizations
        multi_scale=multi_scale,
        mosaic=mosaic,  # lowered 1.0 -> 0.5 to keep far balls intact
        mixup=0.1,
        copy_paste=copy_paste,
        scale=0.9,
        # Loss gains
        cls=cls,  # raised 0.5 -> 1.5 to reject person false positives
        # Augmentations
        flipud=0.0,  # no vertical flip (field has up/down orientation)
        fliplr=0.5,
        hsv_h=0.015,
        hsv_s=0.7,
        hsv_v=hsv_v,  # lowered 0.4 -> 0.2 to preserve ball/grass contrast
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
        default=V3_MODEL,
        help="Pretrained model (default: %(default)s)",
    )
    parser.add_argument("--epochs", type=int, default=150)
    parser.add_argument("--batch", type=int, default=32)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--patience", type=int, default=V3_PATIENCE)
    parser.add_argument(
        "--device", default="0", help="Device: '0' for GPU, 'cpu' for CPU"
    )
    parser.add_argument("--name", default="ball_v3", help="Run name")
    # v3 hyperparameters (overridable for run configs)
    parser.add_argument("--multi-scale", type=float, default=V3_MULTI_SCALE)
    parser.add_argument("--mosaic", type=float, default=V3_MOSAIC)
    parser.add_argument("--copy-paste", type=float, default=V3_COPY_PASTE)
    parser.add_argument("--cls", type=float, default=V3_CLS)
    parser.add_argument(
        "--no-cos-lr",
        action="store_true",
        help="Disable cosine LR annealing (on by default)",
    )
    parser.add_argument("--lr0", type=float, default=V3_LR0)
    parser.add_argument(
        "--freeze", type=int, default=V3_FREEZE, help="Backbone layers to freeze"
    )
    parser.add_argument("--hsv-v", type=float, default=V3_HSV_V)
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
        patience=args.patience,
        device=args.device,
        name=args.name,
        project=args.project,
        epoch_rotation=args.epoch_rotation,
        fraction=args.fraction,
        multi_scale=args.multi_scale,
        mosaic=args.mosaic,
        copy_paste=args.copy_paste,
        cls=args.cls,
        cos_lr=not args.no_cos_lr,
        lr0=args.lr0,
        freeze=args.freeze,
        hsv_v=args.hsv_v,
    )


if __name__ == "__main__":
    main()
