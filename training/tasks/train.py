"""Train task — build training set and run YOLO training.

Pull-local-process-push pattern:
  - Pull: copy pack files + manifests for all training games to local SSD
  - Process: build training set, run YOLO training
  - Push: copy trained weights back to server
  - Cleanup: remove local training data (keep weights)
"""

import json
import logging
import os
import shutil
import time
from pathlib import Path

from training.tasks import register_task

logger = logging.getLogger(__name__)


@register_task("train")
def run_train(
    *,
    item: dict,
    local_work_dir: Path,
    server_share: str = "",
    local_models_dir: Path | None = None,
) -> dict:
    """Build training set from TRAINABLE games and train YOLO model."""
    payload = item.get("payload") or {}

    from training.pipeline.config import load_config

    cfg = load_config()

    # Determine which games to use
    train_games = payload.get("train_games", [])
    val_games = payload.get("val_games", [])
    version = payload.get("version", f"v{int(time.time())}")
    resume_from = payload.get("resume_from")

    if not train_games:
        raise ValueError("No train_games specified in payload")

    # Local directories
    local_dataset = local_work_dir / "training" / version
    local_images_train = local_dataset / "images" / "train"
    local_images_val = local_dataset / "images" / "val"
    local_labels_train = local_dataset / "labels" / "train"
    local_labels_val = local_dataset / "labels" / "val"
    local_weights = local_work_dir / "training" / "weights"

    for d in [
        local_images_train,
        local_images_val,
        local_labels_train,
        local_labels_val,
        local_weights,
    ]:
        d.mkdir(parents=True, exist_ok=True)

    # Step 1: Pull and build training set
    logger.info(
        "Building training set %s (%d train, %d val games)...",
        version,
        len(train_games),
        len(val_games),
    )

    server_games_dir = Path(cfg.paths.games_dir)
    if server_share and not server_games_dir.exists():
        server_games_dir = Path(server_share) / "games"

    t0 = time.time()
    train_tile_count = _build_split(
        game_ids=train_games,
        server_games_dir=server_games_dir,
        images_dir=local_images_train,
        labels_dir=local_labels_train,
        local_work_dir=local_work_dir,
        neg_ratio=cfg.training.neg_ratio,
    )
    val_tile_count = _build_split(
        game_ids=val_games,
        server_games_dir=server_games_dir,
        images_dir=local_images_val,
        labels_dir=local_labels_val,
        local_work_dir=local_work_dir,
        neg_ratio=cfg.training.neg_ratio,
    )
    build_time = time.time() - t0
    logger.info(
        "Training set built: %d train, %d val tiles (%.0fs)",
        train_tile_count,
        val_tile_count,
        build_time,
    )

    # Write dataset.yaml
    dataset_yaml = local_dataset / "dataset.yaml"
    dataset_yaml.write_text(
        f"path: {local_dataset}\n"
        f"train: images/train\n"
        f"val: images/val\n"
        f"nc: 1\n"
        f"names: ['ball']\n"
    )

    # Step 2: Train
    logger.info("Starting YOLO training...")
    from ultralytics import YOLO

    model_base = cfg.training.model_base
    if resume_from:
        model = YOLO(resume_from)
    else:
        model = YOLO(model_base)

    results = model.train(
        data=str(dataset_yaml),
        epochs=cfg.training.epochs,
        batch=cfg.training.batch_size,
        imgsz=cfg.training.imgsz,
        device=0,
        patience=cfg.training.patience,
        workers=0,
        deterministic=True,
        project=str(local_weights),
        name=version,
    )

    # Find best weights
    best_pt = local_weights / version / "weights" / "best.pt"
    last_pt = local_weights / version / "weights" / "last.pt"

    if not best_pt.exists():
        raise RuntimeError(f"Training did not produce best.pt at {best_pt}")

    # Step 3: Push weights to server
    server_weights = Path(cfg.paths.training_sets) / version / "weights"
    if server_share and not Path(cfg.paths.training_sets).exists():
        server_weights = Path(server_share) / "training_sets" / version / "weights"

    server_weights.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(best_pt), str(server_weights / "best.pt"))
    if last_pt.exists():
        shutil.copy2(str(last_pt), str(server_weights / "last.pt"))

    # Also archive to F: if available
    archive_weights = Path(cfg.paths.archive.checkpoints) / version
    try:
        archive_weights.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(best_pt), str(archive_weights / "best.pt"))
        logger.info("Archived weights to %s", archive_weights)
    except Exception as e:
        logger.warning("Failed to archive weights: %s", e)

    # Extract metrics
    metrics = {}
    try:
        metrics = {
            "mAP50": float(results.results_dict.get("metrics/mAP50(B)", 0)),
            "mAP50-95": float(results.results_dict.get("metrics/mAP50-95(B)", 0)),
            "precision": float(results.results_dict.get("metrics/precision(B)", 0)),
            "recall": float(results.results_dict.get("metrics/recall(B)", 0)),
        }
    except Exception:
        pass

    logger.info(
        "Training complete: %s (mAP50=%.3f, P=%.3f, R=%.3f)",
        version,
        metrics.get("mAP50", 0),
        metrics.get("precision", 0),
        metrics.get("recall", 0),
    )

    return {
        "version": version,
        "train_tiles": train_tile_count,
        "val_tiles": val_tile_count,
        "weights_path": str(server_weights / "best.pt"),
        "metrics": metrics,
    }


def _get_priority_stems(manifest) -> dict[str, int]:
    """Get tile stems that should be oversampled for gap-focused training.

    Human-verified gap labels are the highest-value training data (3x).
    Sonnet-verified tiles near track gaps are also valuable (2x).

    Returns {tile_stem: multiplier}.
    """
    import json as _json

    conn = manifest.conn
    priority: dict[str, int] = {}

    # Human-verified gap labels: 3x oversample
    rows = conn.execute(
        "SELECT DISTINCT tile_stem FROM labels WHERE source = 'human_gap_review'"
    ).fetchall()
    for (stem,) in rows:
        priority[stem] = 3

    # Tiles near track gaps: 2x oversample
    try:
        raw = conn.execute(
            "SELECT value FROM metadata WHERE key = 'track_coverage'"
        ).fetchone()
        if raw:
            coverage_data = _json.loads(raw[0])
            gap_frames: set[tuple[str, int]] = set()
            for gap in coverage_data.get("gaps", []):
                seg = gap.get("segment", "")
                for fi in range(gap["frame_start"], gap["frame_end"] + 1, 4):
                    gap_frames.add((seg, fi))

            for seg, fi in gap_frames:
                for delta in (-4, 0, 4):
                    pattern = f"{seg}_frame_{fi + delta:06d}_%"
                    near = conn.execute(
                        "SELECT DISTINCT tile_stem FROM labels "
                        "WHERE tile_stem LIKE ? AND qa_verdict = 'true_positive'",
                        (pattern,),
                    ).fetchall()
                    for (stem,) in near:
                        if stem not in priority:
                            priority[stem] = 2
    except Exception:
        pass

    return priority


def _build_split(
    *,
    game_ids: list[str],
    server_games_dir: Path,
    images_dir: Path,
    labels_dir: Path,
    local_work_dir: Path,
    neg_ratio: float = 1.0,
) -> int:
    """Build one split (train or val) by extracting tiles from pack files.

    Returns total tile count.
    """
    from training.data_prep.game_manifest import GameManifest

    total = 0
    for game_id in game_ids:
        game_dir = server_games_dir / game_id

        # Pull manifest to local
        local_game = local_work_dir / game_id
        local_game.mkdir(parents=True, exist_ok=True)

        manifest_src = game_dir / "manifest.db"
        if not manifest_src.exists():
            logger.warning("  Skipping %s (no manifest.db)", game_id)
            continue

        local_manifest = local_game / "manifest.db"
        if not local_manifest.exists():
            shutil.copy2(str(manifest_src), str(local_manifest))

        manifest = GameManifest(local_game)
        manifest.open(create=False)

        try:
            # Get all positive tiles (have labels)
            labeled_stems = manifest.get_labeled_stems()
            game_images = images_dir / game_id
            game_labels = labels_dir / game_id
            game_images.mkdir(parents=True, exist_ok=True)
            game_labels.mkdir(parents=True, exist_ok=True)

            # Export labels
            manifest.export_labels_yolo(game_labels)

            # Get priority stems for gap-focused oversampling
            priority_stems = _get_priority_stems(manifest)
            if priority_stems:
                logger.info(
                    "  %s: %d priority stems for oversampling",
                    game_id,
                    len(priority_stems),
                )

            # Extract tile images from packs for labeled tiles
            segments = manifest.get_segments()
            extracted = 0

            for segment in segments:
                tiles = manifest.get_tiles_for_segment(segment)
                for tile_info in tiles:
                    stem = f"{segment}_frame_{tile_info['frame_idx']:06d}_r{tile_info['row']}_c{tile_info['col']}"

                    # Only extract tiles that have labels (positive) or are sampled negatives
                    has_label = stem in labeled_stems
                    if not has_label:
                        # Simple negative sampling: skip most negatives
                        import random

                        if random.random() > neg_ratio / max(
                            1, len(tiles) / max(1, len(labeled_stems))
                        ):
                            continue

                    # Read from pack
                    pack_file = tile_info.get("pack_file")
                    if not pack_file:
                        continue

                    # Try local pack, then server pack, then F: archive
                    pack_name = Path(pack_file).name
                    pack_path = local_game / "tile_packs" / pack_name
                    if not pack_path.exists():
                        pack_path = (
                            server_games_dir / game_id / "tile_packs" / pack_name
                        )
                    if not pack_path.exists():
                        # Stage from F: archive to local SSD for fast reads
                        from training.data_prep.manifest_dataset import (
                            _resolve_pack_path,
                        )

                        try:
                            resolved = _resolve_pack_path(pack_file)
                            # Copy to local SSD for the rest of this game's tiles
                            local_pack_dir = local_game / "tile_packs"
                            local_pack_dir.mkdir(parents=True, exist_ok=True)
                            local_dest = local_pack_dir / pack_name
                            if not local_dest.exists():
                                logger.info(
                                    "    Staging pack %s to local SSD (%.1f GB)",
                                    pack_name,
                                    Path(resolved).stat().st_size / 1e9,
                                )
                                shutil.copy2(resolved, str(local_dest))
                            pack_path = local_dest
                        except FileNotFoundError:
                            continue

                    try:
                        with open(pack_path, "rb") as f:
                            f.seek(tile_info["pack_offset"])
                            jpeg_bytes = f.read(tile_info["pack_size"])

                        (game_images / f"{stem}.jpg").write_bytes(jpeg_bytes)
                        extracted += 1

                        # Write empty label file for negatives
                        if not has_label:
                            (game_labels / f"{stem}.txt").write_text("")

                        # Oversample priority tiles (gap-adjacent / human-verified)
                        repeat = priority_stems.get(stem, 1)
                        label_file = game_labels / f"{stem}.txt"
                        for dup in range(1, repeat):
                            dup_stem = f"{stem}_dup{dup}"
                            (game_images / f"{dup_stem}.jpg").write_bytes(jpeg_bytes)
                            if label_file.exists():
                                shutil.copy2(
                                    str(label_file),
                                    str(game_labels / f"{dup_stem}.txt"),
                                )
                            extracted += 1
                    except Exception as e:
                        logger.debug("Failed to extract tile %s: %s", stem, e)
        finally:
            manifest.close()

        total += extracted
        logger.info(
            "  %s: %d tiles extracted (%d positive)",
            game_id,
            extracted,
            len(labeled_stems),
        )

    return total
