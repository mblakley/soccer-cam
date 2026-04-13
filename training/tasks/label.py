"""Label task — run ONNX ball detection on full panoramic frames.

The external ball detector model takes full 4096x1800 panoramic frames,
NOT 640x640 tiles. This task:
  1. Pulls manifest + video segments to local SSD
  2. Reads video frames at FRAME_INTERVAL (every 4th frame)
  3. Runs full-frame ONNX inference (CUDA > DML > CPU)
  4. Maps panoramic detections to per-tile YOLO labels
  5. Pushes updated manifest back to server

Uses detect_balls() and pano_to_tile_labels() from label_job.py.
"""

import logging
import time
from pathlib import Path

from training.tasks import register_task

logger = logging.getLogger(__name__)

# Frame interval matches label_job.py — every 4th frame (~6fps from 24.6fps)
FRAME_INTERVAL = 4


@register_task("label")
def run_label(
    *,
    item: dict,
    local_work_dir: Path,
    server_share: str = "",
    local_models_dir: Path | None = None,
) -> dict:
    """Run ONNX ball detection on full video frames for a game."""
    game_id = item["game_id"]

    from training.tasks.io import TaskIO

    io = TaskIO(game_id, local_work_dir, server_share)
    io.ensure_space(needed_gb=10)

    # Pull manifest (for segment list) — no packs needed for labeling
    io.pull_manifest()

    from training.data_prep.game_manifest import GameManifest

    manifest = GameManifest(io.local_game)
    manifest.open(create=False)

    try:
        # Find ONNX model
        model_path = _find_model(io.cfg.labeling.onnx_model, local_models_dir)
        if not model_path:
            raise FileNotFoundError(
                f"ONNX model not found: {io.cfg.labeling.onnx_model}"
            )

        conf_threshold = io.cfg.labeling.confidence

        import cv2
        import onnxruntime as ort

        from training.distributed.label_job import (
            detect_balls,
            pano_to_tile_labels,
        )

        providers = [
            "CUDAExecutionProvider",
            "DmlExecutionProvider",
            "CPUExecutionProvider",
        ]
        session = ort.InferenceSession(str(model_path), providers=providers)
        active_provider = session.get_providers()[0]
        logger.info(
            "Running ONNX inference with %s (conf=%.2f) on %s",
            model_path.name,
            conf_threshold,
            active_provider,
        )

        # Get video directory
        video_dir = io.video_path()
        if not video_dir:
            raise FileNotFoundError(f"No video path found for {game_id}")

        # Find segment video files (only [F] or [0@0] markers = individual segments)
        video_files = sorted(video_dir.glob("*.mp4"))
        segment_videos = [
            v for v in video_files if "[F]" in v.name or "[0@0]" in v.name
        ]
        if not segment_videos:
            # Try rglob for tournament structure
            segment_videos = sorted(
                v
                for v in video_dir.rglob("*.mp4")
                if "[F]" in v.name or "[0@0]" in v.name
            )
        if not segment_videos:
            raise FileNotFoundError(
                f"No segment videos found in {video_dir}"
            )

        logger.info(
            "Found %d segment videos for %s", len(segment_videos), game_id
        )

        total_labels = 0
        total_frames = 0
        total_detections = 0

        for seg_video in segment_videos:
            seg_name = seg_video.stem
            t0 = time.time()

            # Stage video to local SSD for fast I/O
            local_video = io.local_game / "video" / seg_video.name
            local_video.parent.mkdir(parents=True, exist_ok=True)
            if not local_video.exists():
                import shutil

                logger.info("  Staging %s to SSD...", seg_video.name)
                shutil.copy2(str(seg_video), str(local_video))

            cap = cv2.VideoCapture(str(local_video))
            if not cap.isOpened():
                logger.error("  Cannot open: %s", seg_video.name)
                continue

            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            logger.info(
                "  Segment %s: %d frames (interval=%d)",
                seg_name[:50],
                frame_count,
                FRAME_INTERVAL,
            )

            seg_labels = []
            seg_dets = 0
            seg_frames = 0
            fi = 0

            while True:
                ret = cap.grab()
                if not ret:
                    break
                if fi % FRAME_INTERVAL == 0:
                    ret, frame = cap.retrieve()
                    if ret:
                        dets = detect_balls(frame, session, conf_threshold)
                        seg_dets += len(dets)
                        for det in dets:
                            for tl in pano_to_tile_labels(
                                det["cx"], det["cy"], det["w"], det["h"]
                            ):
                                tile_stem = (
                                    f"{seg_name}_frame_{fi:06d}"
                                    f"_r{tl['row']}_c{tl['col']}"
                                )
                                seg_labels.append(
                                    (
                                        tile_stem,
                                        0,
                                        tl["cx_norm"],
                                        tl["cy_norm"],
                                        tl["w_norm"],
                                        tl["h_norm"],
                                        "onnx",
                                        det["conf"],
                                    )
                                )
                        seg_frames += 1
                fi += 1

            cap.release()

            # Write labels to manifest
            if seg_labels:
                written = manifest.bulk_insert_labels(seg_labels)
                total_labels += written
            total_frames += seg_frames
            total_detections += seg_dets

            elapsed = time.time() - t0
            logger.info(
                "    %d detections -> %d labels in %.0fs (%.1f fps)",
                seg_dets,
                len(seg_labels),
                elapsed,
                seg_frames / elapsed if elapsed > 0 else 0,
            )

            # Clean staged video to free SSD space
            if local_video.exists():
                local_video.unlink()

        manifest.set_metadata("labeled_at", str(time.time()))
        manifest.set_metadata("onnx_model", model_path.name)
        manifest.set_metadata("label_provider", active_provider)
    finally:
        manifest.close()

    # Push updated manifest
    io.push_manifest()

    logger.info(
        "Labeled %s: %d frames, %d detections, %d labels",
        game_id,
        total_frames,
        total_detections,
        total_labels,
    )

    if total_frames == 0 and len(segment_videos) > 0:
        raise RuntimeError(
            f"Label task processed 0 frames across {len(segment_videos)} "
            "segments — video files may be unreadable"
        )

    return {
        "frames_processed": total_frames,
        "detections": total_detections,
        "labels_written": total_labels,
        "segments": len(segment_videos),
        "provider": active_provider,
    }


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
