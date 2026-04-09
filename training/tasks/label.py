"""Label task — run ONNX ball detection on tiles and write labels.

Uses TaskIO for consistent pull-local-process-push:
  - Pull: pack files + manifest.db to local SSD
  - Process: ONNX inference on tiles, write labels to local manifest
  - Push: updated manifest.db back to server
  - Cleanup: remove local working files
"""

import logging
import time
from pathlib import Path

from training.tasks import register_task

logger = logging.getLogger(__name__)


@register_task("label")
def run_label(
    *,
    item: dict,
    local_work_dir: Path,
    server_share: str = "",
    local_models_dir: Path | None = None,
) -> dict:
    """Run ONNX ball detection on all tiles for a game."""
    game_id = item["game_id"]

    from training.tasks.io import TaskIO

    io = TaskIO(game_id, local_work_dir, server_share)
    io.ensure_space(needed_gb=5)

    # Pull manifest + packs
    io.pull_manifest()
    io.pull_packs()

    from training.data_prep.game_manifest import GameManifest

    manifest = GameManifest(io.local_game)
    manifest.open(create=False)

    # Find ONNX model
    model_path = _find_model(io.cfg.labeling.onnx_model, local_models_dir)
    if not model_path:
        manifest.close()
        raise FileNotFoundError(f"ONNX model not found: {io.cfg.labeling.onnx_model}")

    conf_threshold = io.cfg.labeling.confidence
    logger.info("Running ONNX inference with %s (conf=%.2f)", model_path.name, conf_threshold)

    import cv2
    import numpy as np
    import onnxruntime as ort

    providers = ["CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"]
    session = ort.InferenceSession(str(model_path), providers=providers)
    input_name = session.get_inputs()[0].name
    input_shape = session.get_inputs()[0].shape

    segments = manifest.get_segments()
    total_labels = 0
    total_tiles = 0

    for segment in segments:
        tiles = manifest.get_tiles_for_segment(segment)
        logger.info("  Segment %s: %d tiles", segment, len(tiles))

        batch_labels = []
        for tile_info in tiles:
            pack_file = tile_info.get("pack_file")
            if not pack_file:
                continue

            # Read from local pack
            local_pack = io.local_packs / Path(pack_file).name
            if not local_pack.exists():
                continue

            with open(local_pack, "rb") as f:
                f.seek(tile_info["pack_offset"])
                jpeg_bytes = f.read(tile_info["pack_size"])

            img_arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
            img = cv2.imdecode(img_arr, cv2.IMREAD_COLOR)
            if img is None:
                continue

            img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            img_resized = cv2.resize(img_rgb, (input_shape[3], input_shape[2]))
            img_norm = img_resized.astype(np.float32) / 255.0
            img_input = np.transpose(img_norm, (2, 0, 1))[np.newaxis, ...]

            outputs = session.run(None, {input_name: img_input})
            detections = _parse_onnx_output(outputs, conf_threshold)

            tile_stem = (
                f"{segment}_frame_{tile_info['frame_idx']:06d}"
                f"_r{tile_info['row']}_c{tile_info['col']}"
            )
            for det in detections:
                batch_labels.append((
                    tile_stem, 0, det["cx"], det["cy"], det["w"], det["h"],
                    "onnx", det["conf"],
                ))
            total_tiles += 1

        if batch_labels:
            total_labels += manifest.bulk_insert_labels(batch_labels)

    manifest.set_metadata("labeled_at", str(time.time()))
    manifest.set_metadata("onnx_model", model_path.name)
    manifest.close()

    # Push updated manifest
    io.push_manifest()

    logger.info("Labeled %s: %d tiles, %d labels", game_id, total_tiles, total_labels)
    return {"tiles_processed": total_tiles, "labels_written": total_labels, "segments": len(segments)}


def _find_model(model_name: str, local_models_dir: Path | None) -> Path | None:
    """Find the ONNX model file."""
    if local_models_dir:
        local = local_models_dir / model_name
        if local.exists():
            return local
    for p in [
        Path(f"C:/soccer-cam-label/models/{model_name}"),
        Path(f"D:/training_data/models/{model_name}"),
        Path(f"F:/test/***REDACTED***/{model_name}"),
    ]:
        if p.exists():
            return p
    return None


def _parse_onnx_output(outputs: list, conf_threshold: float = 0.45) -> list[dict]:
    """Parse ONNX model output into normalized detections."""
    import numpy as np

    output = outputs[0]
    if len(output.shape) == 3:
        if output.shape[2] <= output.shape[1]:
            dets = output[0]
        else:
            dets = output[0].T
    elif len(output.shape) == 2:
        dets = output
    else:
        return []

    results = []
    for det in dets:
        if len(det) < 5:
            continue
        x1, y1, x2, y2, conf = det[:5]
        if conf < conf_threshold:
            continue
        cx = ((x1 + x2) / 2) / 640.0
        cy = ((y1 + y2) / 2) / 640.0
        w = abs(x2 - x1) / 640.0
        h = abs(y2 - y1) / 640.0
        if 0 < cx < 1 and 0 < cy < 1 and w > 0 and h > 0:
            results.append({"cx": cx, "cy": cy, "w": w, "h": h, "conf": float(conf)})
    return results
