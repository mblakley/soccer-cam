"""Train the v4 ball detector: perspective-normalized **warped full frames**.

v4 supersedes the v2 tiled detector (recall ~0.29 on far balls). It trains on a
single warped full frame per sampled frame (no 21-tile grid), at a swept
``target_width`` resolution, jointly over Dahua + Reolink with Reolink
upweighted. This is a SEPARATE entry from ``train_v3.py`` (the tile-based
ManifestTrainer path), which is left untouched.

The two concrete fixes over the starved tile path:
- **No ``workers=0``.** A persistent-worker DataLoader (workers default 8) keeps
  the GPU fed. The I/O benchmark gate (training/experiments/io_benchmark.py)
  must pass before any long run here.
- **No train-time JPEG decode.** Frames are warped once, offline, into
  pre-decoded shards (training/data_prep/warped_pack.py).

Status: SCAFFOLD. The warped YOLO dataset+trainer (mapping Dahua labels into
warped coords via ``warp_points`` and ingesting Reolink far-ball labels) is the
downstream dataset-writer phase; this module wires the v4 config + DataLoader
settings and fails loudly if run before that lands.

Usage (after the dataset-writer phase produces a warped dataset.yaml):
    uv run python -m training.train_v4 --data .../warped_dataset.yaml --target-width 7680
"""

import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger()

# --- v4 hyperparameters (carried from the v2->v3->v4 small-object analysis) ---
# yolo26l: STAL + ProgLoss help tiny far balls; multi_scale gives scale
# robustness; lower mosaic keeps small balls intact; higher cls rejects
# false-positives-on-people; cos_lr + shorter patience converge without overfit;
# lower hsv_v preserves ball/grass contrast.
V4_MODEL = "yolo26l.pt"
V4_MULTI_SCALE = 0.5
V4_MOSAIC = 0.5
V4_COPY_PASTE = 0.3
V4_CLS = 1.5
V4_COS_LR = True
V4_LR0 = 0.002
V4_PATIENCE = 15
V4_FREEZE = 8
V4_HSV_V = 0.2
# Default warped resolution (the swept speed/accuracy knob; production value is
# chosen by the downstream resolution experiment vs AutoCam, NOT the 1280 warp default).
V4_TARGET_WIDTH = 7680
# Persistent-worker DataLoader: the fix for the v3 starvation (workers=0).
V4_WORKERS = 8


def main():
    parser = argparse.ArgumentParser(
        description="Train v4 (warped full-frame) ball detector"
    )
    parser.add_argument("--data", type=Path, required=True, help="warped dataset.yaml")
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument(
        "--imgsz",
        type=int,
        default=V4_TARGET_WIDTH,
        help="long side; warped band is rectangular (use rect=True)",
    )
    parser.add_argument(
        "--target-width",
        type=int,
        default=V4_TARGET_WIDTH,
        help="warped frame width (swept speed/accuracy knob)",
    )
    parser.add_argument("--device", default="0")
    parser.add_argument("--model", default=V4_MODEL)
    parser.add_argument("--resume", type=Path, default=None)
    parser.add_argument("--project", type=Path, default=None)
    parser.add_argument("--name", default="ball_v4")
    parser.add_argument("--workers", type=int, default=V4_WORKERS)
    # v4 hyperparameters (overridable for sweep configs)
    parser.add_argument("--multi-scale", type=float, default=V4_MULTI_SCALE)
    parser.add_argument("--mosaic", type=float, default=V4_MOSAIC)
    parser.add_argument("--copy-paste", type=float, default=V4_COPY_PASTE)
    parser.add_argument("--cls", type=float, default=V4_CLS)
    parser.add_argument("--lr0", type=float, default=V4_LR0)
    parser.add_argument("--patience", type=int, default=V4_PATIENCE)
    parser.add_argument("--freeze", type=int, default=V4_FREEZE)
    parser.add_argument("--hsv-v", type=float, default=V4_HSV_V)
    args = parser.parse_args()

    if not args.data.exists():
        logger.error("Dataset YAML not found: %s", args.data)
        sys.exit(1)
    if args.project is None:
        args.project = args.data.parent / "runs"

    # The warped YOLO dataset+trainer is the downstream dataset-writer phase.
    try:
        from training.data_prep.warped_dataset import WarpedTrainer
    except ImportError as exc:
        logger.error(
            "WarpedTrainer not available: %s\n"
            "train_v4 is a scaffold — the warped dataset writer (map Dahua labels via "
            "warp_points + ingest Reolink far-ball labels into a warped YOLO dataset) is the "
            "next phase. Build training/data_prep/warped_dataset.py before running v4 training. "
            "The I/O benchmark gate (training.experiments.io_benchmark) validates the loader first.",
            exc,
        )
        sys.exit(2)

    from ultralytics import YOLO

    model_path = str(args.resume) if args.resume else args.model
    model = YOLO(model_path)
    logger.info(
        "Training v4 (warped, TW=%d): %s, %d epochs, batch %d, workers %d, device %s",
        args.target_width,
        model_path,
        args.epochs,
        args.batch,
        args.workers,
        args.device,
    )

    model.train(
        data=str(args.data),
        epochs=args.epochs,
        imgsz=args.imgsz,
        rect=True,  # warped band is wide/short — rectangular inference, not square
        batch=args.batch,
        device=args.device,
        project=str(args.project),
        name=args.name,
        workers=args.workers,  # persistent workers (NOT 0) — the starvation fix
        patience=args.patience,
        multi_scale=args.multi_scale,
        mosaic=args.mosaic,
        copy_paste=args.copy_paste,
        cls=args.cls,
        lr0=args.lr0,
        cos_lr=V4_COS_LR,
        freeze=args.freeze,
        hsv_v=args.hsv_v,
        deterministic=True,
        exist_ok=True,
        trainer=WarpedTrainer,
    )
    logger.info("v4 training complete!")


if __name__ == "__main__":
    main()
