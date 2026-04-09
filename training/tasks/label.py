"""Label task — run ONNX ball detection on tiles and write labels.

Pull-local-process-push pattern:
  - Pull: copy pack files + manifest.db from server to local SSD
  - Process: run ONNX inference on tiles, write labels to local manifest.db
  - Push: copy updated manifest.db back to server
  - Cleanup: remove local working files
"""

import logging
import os
import shutil
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
    payload = item.get("payload") or {}

    from training.pipeline.config import load_config
    cfg = load_config()

    # Locate game data on server
    server_game_dir = Path(cfg.paths.games_dir) / game_id
    if server_share and not server_game_dir.exists():
        server_game_dir = Path(server_share) / "games" / game_id

    server_manifest = server_game_dir / "manifest.db"
    server_packs = server_game_dir / "tile_packs"

    if not server_manifest.exists():
        raise FileNotFoundError(f"Manifest not found: {server_manifest}")

    # Step 1: Pull to local SSD
    local_game = local_work_dir / game_id
    local_packs = local_game / "tile_packs"
    local_packs.mkdir(parents=True, exist_ok=True)

    logger.info("Pulling manifest.db and pack files for %s...", game_id)
    shutil.copy2(str(server_manifest), str(local_game / "manifest.db"))

    for pack_file in server_packs.glob("*.pack"):
        dest = local_packs / pack_file.name
        if not dest.exists():
            shutil.copy2(str(pack_file), str(dest))

    # Step 2: Run ONNX inference
    from training.data_prep.game_manifest import GameManifest

    manifest = GameManifest(local_game)
    manifest.open()

    # Find ONNX model
    model_path = _find_model(cfg.labeling.onnx_model, local_models_dir)
    if not model_path:
        manifest.close()
        raise FileNotFoundError(f"ONNX model not found: {cfg.labeling.onnx_model}")

    conf_threshold = cfg.labeling.confidence
    nms_iou = cfg.labeling.nms_iou

    logger.info("Running ONNX inference with %s (conf=%.2f)", model_path, conf_threshold)

    import onnxruntime as ort
    import numpy as np
    import cv2

    providers = ["CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"]
    session = ort.InferenceSession(str(model_path), providers=providers)
    input_name = session.get_inputs()[0].name
    input_shape = session.get_inputs()[0].shape  # e.g. [1, 3, 640, 640]

    segments = manifest.get_segments()
    total_labels = 0
    total_tiles = 0

    for segment in segments:
        tiles = manifest.get_tiles_for_segment(segment)
        logger.info("  Segment %s: %d tiles", segment, len(tiles))

        batch_labels = []
        for tile_info in tiles:
            # Read tile from pack
            pack_file = tile_info.get("pack_file")
            if not pack_file:
                continue

            # Adjust pack path to local
            local_pack = local_packs / Path(pack_file).name
            if not local_pack.exists():
                continue

            with open(local_pack, "rb") as f:
                f.seek(tile_info["pack_offset"])
                jpeg_bytes = f.read(tile_info["pack_size"])

            # Decode
            img_arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
            img = cv2.imdecode(img_arr, cv2.IMREAD_COLOR)
            if img is None:
                continue

            # Preprocess for ONNX
            img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            img_resized = cv2.resize(img_rgb, (input_shape[3], input_shape[2]))
            img_norm = img_resized.astype(np.float32) / 255.0
            img_input = np.transpose(img_norm, (2, 0, 1))[np.newaxis, ...]

            # Inference
            outputs = session.run(None, {input_name: img_input})

            # Parse detections
            detections = _parse_onnx_output(outputs, conf_threshold, nms_iou)

            # Write labels
            tile_stem = f"{segment}_frame_{tile_info['frame_idx']:06d}_r{tile_info['row']}_c{tile_info['col']}"
            for det in detections:
                batch_labels.append((
                    tile_stem,
                    0,  # class_id (ball)
                    det["cx"],
                    det["cy"],
                    det["w"],
                    det["h"],
                    "onnx",
                    det["conf"],
                ))

            total_tiles += 1

        if batch_labels:
            inserted = manifest.bulk_insert_labels(batch_labels)
            total_labels += inserted

    manifest.set_metadata("labeled_at", str(time.time()))
    manifest.set_metadata("onnx_model", str(model_path.name))
    manifest.close()

    # Step 3: Push manifest.db back
    logger.info("Pushing updated manifest.db back to server...")
    dest_manifest = server_game_dir / "manifest.db"
    shutil.copy2(str(local_game / "manifest.db"), str(dest_manifest))

    logger.info(
        "Labeled %s: %d tiles processed, %d labels written",
        game_id, total_tiles, total_labels,
    )

    return {
        "tiles_processed": total_tiles,
        "labels_written": total_labels,
        "segments": len(segments),
    }


def _find_model(model_name: str, local_models_dir: Path | None) -> Path | None:
    """Find the ONNX model file, checking local cache first."""
    # Check local models dir
    if local_models_dir:
        local = local_models_dir / model_name
        if local.exists():
            return local

    # Check common locations
    candidates = [
        Path(f"C:/soccer-cam-label/models/{model_name}"),
        Path(f"D:/training_data/models/{model_name}"),
        Path(f"F:/test/***REDACTED***/{model_name}"),
    ]
    for p in candidates:
        if p.exists():
            return p

    return None


def _parse_onnx_output(
    outputs: list,
    conf_threshold: float = 0.45,
    nms_iou: float = 0.5,
) -> list[dict]:
    """Parse ONNX model output into normalized detections.

    Handles common YOLO output formats:
    - [1, num_dets, 6] — (x1, y1, x2, y2, conf, class)
    - [1, 6, num_dets] — transposed variant
    """
    import numpy as np

    output = outputs[0]

    # Handle different output shapes
    if len(output.shape) == 3:
        if output.shape[2] == 6:
            # [1, N, 6] — standard
            dets = output[0]
        elif output.shape[1] == 6:
            # [1, 6, N] — transposed
            dets = output[0].T
        elif output.shape[1] > output.shape[2]:
            # [1, N, C] where C < N — assume standard
            dets = output[0]
        else:
            # [1, C, N] — transposed
            dets = output[0].T
    elif len(output.shape) == 2:
        dets = output
    else:
        return []

    results = []
    for det in dets:
        if len(det) < 5:
            continue

        # Try both (x1,y1,x2,y2,conf,...) and (cx,cy,w,h,conf,...) formats
        if len(det) >= 6:
            x1, y1, x2, y2, conf, cls = det[:6]
        else:
            x1, y1, x2, y2, conf = det[:5]

        if conf < conf_threshold:
            continue

        # Convert to normalized center format
        # Assume input is 640x640
        cx = ((x1 + x2) / 2) / 640.0
        cy = ((y1 + y2) / 2) / 640.0
        w = abs(x2 - x1) / 640.0
        h = abs(y2 - y1) / 640.0

        # Sanity check
        if 0 < cx < 1 and 0 < cy < 1 and w > 0 and h > 0:
            results.append({"cx": cx, "cy": cy, "w": w, "h": h, "conf": float(conf)})

    return results
