"""Export trained YOLO26 model to mobile formats (CoreML, TFLite, ONNX).

YOLO26's NMS-free architecture means no post-processing is needed on device.
"""

import argparse
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def export_model(
    model_path: Path,
    formats: list[str] | None = None,
    imgsz: int = 640,
):
    """Export a trained YOLO model to mobile-friendly formats.

    Args:
        model_path: Path to trained model weights (.pt)
        formats: List of export formats (default: coreml, tflite, onnx)
        imgsz: Input image size for export
    """
    from ultralytics import YOLO

    if formats is None:
        formats = ["coreml", "tflite", "onnx"]

    model = YOLO(str(model_path))

    for fmt in formats:
        logger.info("Exporting to %s...", fmt)
        exported = model.export(format=fmt, imgsz=imgsz, nms=False)
        logger.info("Exported: %s", exported)


def main():
    parser = argparse.ArgumentParser(
        description="Export YOLO26 model to mobile formats"
    )
    parser.add_argument("model", type=Path, help="Path to model weights (.pt)")
    parser.add_argument(
        "--formats",
        nargs="+",
        default=["coreml", "tflite", "onnx"],
        help="Export formats",
    )
    parser.add_argument("--imgsz", type=int, default=640)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    export_model(args.model, args.formats, args.imgsz)


if __name__ == "__main__":
    main()
