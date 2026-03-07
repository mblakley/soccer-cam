"""Evaluate a trained ball detection model.

Reports mAP, per-region breakdown, frame-level miss rate, and false positive rate.
"""

import argparse
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def evaluate(
    model_path: Path,
    data_yaml: Path,
    imgsz: int = 640,
    conf: float = 0.25,
    device: str = "0",
):
    """Run evaluation on the test set and report metrics.

    Args:
        model_path: Path to trained model weights (.pt)
        data_yaml: Path to dataset YAML config
        imgsz: Input image size
        conf: Confidence threshold for evaluation
        device: Inference device
    """
    from ultralytics import YOLO

    model = YOLO(str(model_path))
    results = model.val(
        data=str(data_yaml),
        imgsz=imgsz,
        conf=conf,
        device=device,
    )

    logger.info("=== Evaluation Results ===")
    logger.info("mAP@0.5:      %.4f", results.box.map50)
    logger.info("mAP@0.5:0.95: %.4f", results.box.map)
    logger.info("Precision:     %.4f", results.box.mp)
    logger.info("Recall:        %.4f", results.box.mr)

    return {
        "map50": results.box.map50,
        "map": results.box.map,
        "precision": results.box.mp,
        "recall": results.box.mr,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate trained ball detection model"
    )
    parser.add_argument("model", type=Path, help="Path to model weights (.pt)")
    parser.add_argument(
        "--data",
        type=Path,
        default=Path("training/configs/ball_dataset.yaml"),
        help="Dataset YAML",
    )
    parser.add_argument("--conf", type=float, default=0.25)
    parser.add_argument("--device", default="0")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    evaluate(args.model, args.data, conf=args.conf, device=args.device)


if __name__ == "__main__":
    main()
