"""Convert mobile annotation results into YOLO training labels.

Reads annotation_results.json files produced by the mobile annotation helper,
maps user taps back to full-frame pixel coordinates, and generates YOLO-format
label files that can be appended to the training dataset.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class IngestionStats:
    """Statistics from processing an annotation results file."""

    total_frames: int
    confirmed: int
    rejected: int
    adjusted: int
    located: int
    not_visible: int
    skipped: int
    labels_written: int


def ingest_annotations(
    packet_dir: Path,
    output_labels_dir: Path,
    output_images_dir: Path | None = None,
    bbox_size: int = 30,
) -> IngestionStats:
    """Process annotation results from a review packet into YOLO labels.

    Args:
        packet_dir: Directory containing manifest.json and annotation_results.json
        output_labels_dir: Directory to write YOLO label .txt files
        output_images_dir: Optional directory to symlink/copy crop images for training
        bbox_size: Bounding box size in pixels for point annotations (ball diameter)

    Returns:
        Ingestion statistics
    """
    manifest_path = packet_dir / "manifest.json"
    results_path = packet_dir / "annotation_results.json"

    if not manifest_path.exists():
        raise FileNotFoundError(f"No manifest found at {manifest_path}")
    if not results_path.exists():
        raise FileNotFoundError(f"No annotation results found at {results_path}")

    with open(manifest_path) as f:
        manifest = json.load(f)
    with open(results_path) as f:
        results = json.load(f)

    output_labels_dir.mkdir(parents=True, exist_ok=True)

    frame_lookup = {fr["frame_idx"]: fr for fr in manifest["frames"]}
    source_w = manifest["source_resolution"]["w"]
    source_h = manifest["source_resolution"]["h"]

    stats = IngestionStats(
        total_frames=len(results),
        confirmed=0,
        rejected=0,
        adjusted=0,
        located=0,
        not_visible=0,
        skipped=0,
        labels_written=0,
    )

    for result in results:
        frame_idx = result["frame_idx"]
        action = result["action"]
        manifest_frame = frame_lookup.get(frame_idx)

        if manifest_frame is None:
            logger.warning("Frame %d not found in manifest, skipping", frame_idx)
            continue

        crop_origin = manifest_frame["crop_origin"]

        if action == "confirm":
            stats.confirmed += 1
            detection = manifest_frame.get("model_detection")
            if detection:
                full_x = crop_origin["x"] + detection["x"]
                full_y = crop_origin["y"] + detection["y"]
                _write_yolo_label(
                    output_labels_dir,
                    manifest["game_id"],
                    frame_idx,
                    full_x,
                    full_y,
                    bbox_size,
                    source_w,
                    source_h,
                )
                stats.labels_written += 1

        elif action == "reject":
            stats.rejected += 1
            # Write empty label file (negative example -- no ball in this crop)
            _write_empty_label(output_labels_dir, manifest["game_id"], frame_idx)
            stats.labels_written += 1

        elif action == "adjust":
            stats.adjusted += 1
            ball_pos = result.get("ball_position")
            if ball_pos:
                full_x = crop_origin["x"] + ball_pos["x"]
                full_y = crop_origin["y"] + ball_pos["y"]
                _write_yolo_label(
                    output_labels_dir,
                    manifest["game_id"],
                    frame_idx,
                    full_x,
                    full_y,
                    bbox_size,
                    source_w,
                    source_h,
                )
                stats.labels_written += 1

        elif action == "locate":
            stats.located += 1
            ball_pos = result.get("ball_position")
            if ball_pos:
                full_x = crop_origin["x"] + ball_pos["x"]
                full_y = crop_origin["y"] + ball_pos["y"]
                _write_yolo_label(
                    output_labels_dir,
                    manifest["game_id"],
                    frame_idx,
                    full_x,
                    full_y,
                    bbox_size,
                    source_w,
                    source_h,
                )
                stats.labels_written += 1

        elif action == "not_visible":
            stats.not_visible += 1
            _write_empty_label(output_labels_dir, manifest["game_id"], frame_idx)
            stats.labels_written += 1

        elif action == "skip":
            stats.skipped += 1

        else:
            logger.warning("Unknown action '%s' for frame %d", action, frame_idx)

    logger.info(
        "Ingested %d annotations from %s: %d confirmed, %d rejected, "
        "%d adjusted, %d located, %d not_visible, %d skipped -> %d labels",
        stats.total_frames,
        packet_dir.name,
        stats.confirmed,
        stats.rejected,
        stats.adjusted,
        stats.located,
        stats.not_visible,
        stats.skipped,
        stats.labels_written,
    )

    return stats


def _write_yolo_label(
    labels_dir: Path,
    game_id: str,
    frame_idx: int,
    center_x: int,
    center_y: int,
    bbox_size: int,
    img_w: int,
    img_h: int,
) -> None:
    """Write a YOLO-format label file for a single ball detection.

    YOLO format: <class> <cx_norm> <cy_norm> <w_norm> <h_norm>
    Class 0 = ball.
    """
    cx_norm = center_x / img_w
    cy_norm = center_y / img_h
    w_norm = bbox_size / img_w
    h_norm = bbox_size / img_h

    # Clamp to valid range
    cx_norm = max(0.0, min(1.0, cx_norm))
    cy_norm = max(0.0, min(1.0, cy_norm))

    label_file = labels_dir / f"{game_id}_frame_{frame_idx:06d}.txt"
    with open(label_file, "w") as f:
        f.write(f"0 {cx_norm:.6f} {cy_norm:.6f} {w_norm:.6f} {h_norm:.6f}\n")


def _write_empty_label(labels_dir: Path, game_id: str, frame_idx: int) -> None:
    """Write an empty YOLO label file (negative example)."""
    label_file = labels_dir / f"{game_id}_frame_{frame_idx:06d}.txt"
    label_file.touch()


def ingest_all_packets(
    review_packets_dir: Path,
    output_labels_dir: Path,
) -> list[IngestionStats]:
    """Process all completed review packets in a directory.

    Only processes packets that have annotation_results.json.
    """
    all_stats = []

    if not review_packets_dir.exists():
        logger.warning(
            "Review packets directory does not exist: %s", review_packets_dir
        )
        return all_stats

    for packet_dir in sorted(review_packets_dir.iterdir()):
        if not packet_dir.is_dir():
            continue

        results_file = packet_dir / "annotation_results.json"
        if not results_file.exists():
            continue

        # Check if already ingested
        ingested_marker = packet_dir / ".ingested"
        if ingested_marker.exists():
            continue

        try:
            stats = ingest_annotations(packet_dir, output_labels_dir)
            all_stats.append(stats)

            # Mark as ingested
            ingested_marker.touch()
        except Exception as e:
            logger.error("Error ingesting %s: %s", packet_dir.name, e)

    return all_stats
