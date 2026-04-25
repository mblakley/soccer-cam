"""Build training shard with binary cache for fast YOLO training.

Runs on the SERVER (has local F: and D: access). Reads manifests and packs,
applies quality filters (game phases, field boundaries, QA verdicts), and
writes a binary cache (.bin) + labels to D:/training_data/training_shards/{version}/.

The laptop copies a few large files instead of 100k+ individual JPEGs.

Output format:
    {version}/
      train_images.bin       # raw BGR uint8, N * 640 * 640 * 3 bytes
      train_index.json       # {"meta": {...}, "paths": [...]}
      val_images.bin
      val_index.json
      labels/train/{game_id}/*.txt
      labels/val/{game_id}/*.txt
      labels.zip             # compressed archive of all labels
      manifest.json
      dataset.yaml

Usage (via pipeline):
    uv run python -m training.pipeline enqueue build_shard --priority 10
"""

import json
import logging
import random
import re
import shutil
import time
import zipfile
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

from training.data_prep.field_mask_filter import (
    NEAR_OFF_FIELD_KEEP_RATE,
    classify_label_position,
)
from training.tasks import register_task

logger = logging.getLogger(__name__)

_TILE_RE = re.compile(r"^(.+?)_frame_(\d{6})_r(\d+)_c(\d+)$")

# Binary cache constants
TILE_H, TILE_W, TILE_C = 640, 640, 3
TILE_BYTES = TILE_H * TILE_W * TILE_C  # 1,228,800


class BinCacheWriter:
    """Accumulates decoded tiles into a flat binary file."""

    def __init__(self, bin_path: Path):
        self.bin_path = bin_path
        self.fh = open(bin_path, "wb")
        self.paths: list[str] = []
        self.count = 0

    def write_tile(self, rel_path: str, jpeg_bytes: bytes) -> bool:
        """Decode JPEG and append raw BGR pixels. Returns True on success."""
        img = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            logger.debug("Failed to decode JPEG for %s", rel_path)
            return False
        h, w = img.shape[:2]
        if h != TILE_H or w != TILE_W:
            img = cv2.resize(img, (TILE_W, TILE_H))
        self.fh.write(img.tobytes())
        self.paths.append(rel_path)
        self.count += 1
        return True

    def flush(self):
        self.fh.flush()

    def finish(self) -> dict:
        """Close file and return index metadata."""
        self.fh.close()
        return {
            "meta": {
                "n": self.count,
                "h": TILE_H,
                "w": TILE_W,
                "c": TILE_C,
                "dtype": "uint8",
            },
            "paths": self.paths,
        }


def _parse_stem(stem: str) -> tuple[str, int, int, int] | None:
    """Parse tile stem into (segment, frame_idx, row, col)."""
    m = _TILE_RE.match(stem)
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))


def _select_val_games(game_ids: list[str], val_count: int = 2) -> list[str]:
    """Select diverse val games: one per team, mix home/away."""
    flash = [g for g in game_ids if g.startswith("flash__")]
    heat = [g for g in game_ids if g.startswith("heat__")]

    val = []
    flash_away = [g for g in flash if "away" in g]
    if flash_away:
        val.append(flash_away[len(flash_away) // 2])
    elif flash:
        val.append(flash[len(flash) // 2])

    heat_home = [g for g in heat if "home" in g]
    if heat_home:
        val.append(heat_home[len(heat_home) // 2])
    elif heat:
        val.append(heat[len(heat) // 2])

    return val[:val_count]


def _create_labels_zip(output_dir: Path):
    """Create a zip archive of all label files for efficient transfer."""
    zip_path = output_dir / "labels.zip"
    labels_dir = output_dir / "labels"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for txt_file in labels_dir.rglob("*.txt"):
            arcname = txt_file.relative_to(output_dir)
            zf.write(txt_file, arcname)
    logger.info("Created %s (%.1f MB)", zip_path.name, zip_path.stat().st_size / 1e6)


@register_task("build_shard")
def run_build_shard(
    *,
    item: dict,
    local_work_dir: Path,
    server_share: str = "",
    local_models_dir: Path | None = None,
) -> dict:
    """Build a curated training shard from TRAINABLE games."""
    from training.pipeline.config import load_config

    cfg = load_config()
    payload = item.get("payload") or {}

    # Parse payload
    all_games = payload.get("games", [])
    val_games = payload.get("val_games", [])
    version = payload.get("version", f"shard_v{int(time.time()) % 10000}")
    neg_ratio = payload.get("neg_ratio", 1.0)
    max_tiles_per_game = payload.get("max_tiles_per_game", 4000)
    phase_filter = payload.get("phase_filter", True)
    field_filter = payload.get("field_filter", True)
    qa_filter = payload.get("qa_filter", True)

    if not all_games and not val_games:
        raise ValueError("No games specified in payload")

    # Auto-select val games if not specified
    if not val_games:
        val_games = _select_val_games(all_games)
    train_games = [g for g in all_games if g not in val_games]

    # Output directory
    output_dir = Path(cfg.paths.training_sets).parent / "training_shards" / version
    if output_dir.exists():
        shutil.rmtree(str(output_dir))

    for split in ("train", "val"):
        (output_dir / "labels" / split).mkdir(parents=True)

    # Create binary cache writers
    bin_writers = {}
    for split in ("train", "val"):
        bin_writers[split] = BinCacheWriter(output_dir / f"{split}_images.bin")

    # Local work dir on G: SSD for staging packs
    work_dir = Path(cfg.paths.server_work_dir) / f"shard_{version}"
    work_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "Building shard %s: %d train games, %d val games "
        "(phase=%s, field=%s, qa=%s, neg_ratio=%.1f, max_per_game=%d)",
        version,
        len(train_games),
        len(val_games),
        phase_filter,
        field_filter,
        qa_filter,
        neg_ratio,
        max_tiles_per_game,
    )

    t0 = time.time()
    stats = {
        "train_positive": 0,
        "train_negative": 0,
        "val_positive": 0,
        "val_negative": 0,
        "skipped_phase": 0,
        "skipped_field": 0,
        "skipped_qa": 0,
        "skipped_no_pack": 0,
        "games_with_phases": 0,
        "games_with_field_boundary": 0,
    }

    # Build each split
    for split, game_ids in [("train", train_games), ("val", val_games)]:
        for game_id in game_ids:
            game_stats = _build_game(
                game_id=game_id,
                split=split,
                output_dir=output_dir,
                work_dir=work_dir,
                bin_writer=bin_writers[split],
                games_dir=Path(cfg.paths.games_dir),
                archive_packs=Path(cfg.paths.archive.tile_packs),
                neg_ratio=neg_ratio,
                max_tiles_per_game=max_tiles_per_game,
                phase_filter=phase_filter,
                field_filter=field_filter,
                qa_filter=qa_filter,
            )
            stats[f"{split}_positive"] += game_stats["positive"]
            stats[f"{split}_negative"] += game_stats["negative"]
            stats["skipped_phase"] += game_stats["skipped_phase"]
            stats["skipped_field"] += game_stats["skipped_field"]
            stats["skipped_qa"] += game_stats["skipped_qa"]
            stats["skipped_no_pack"] += game_stats["skipped_no_pack"]
            if game_stats["has_phases"]:
                stats["games_with_phases"] += 1
            if game_stats["has_field_boundary"]:
                stats["games_with_field_boundary"] += 1

    build_time = time.time() - t0

    # Finalize binary caches and write index files
    for split in ("train", "val"):
        index_data = bin_writers[split].finish()
        index_path = output_dir / f"{split}_index.json"
        index_path.write_text(json.dumps(index_data))
        logger.info(
            "  %s cache: %d tiles, %.1f GB",
            split,
            index_data["meta"]["n"],
            (output_dir / f"{split}_images.bin").stat().st_size / 1e9,
        )

    # Create labels.zip for efficient transfer
    _create_labels_zip(output_dir)

    # Write dataset.yaml
    dataset_yaml = output_dir / "dataset.yaml"
    dataset_yaml.write_text(
        f"path: {output_dir}\n"
        f"train: images/train\n"
        f"val: images/val\n"
        f"nc: 1\n"
        f"names: ['ball']\n"
    )

    # Write manifest.json
    manifest = {
        "version": version,
        "format": "bin_cache",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "train_games": train_games,
        "val_games": val_games,
        "filters": {
            "phase": phase_filter,
            "field": field_filter,
            "qa": qa_filter,
        },
        "neg_ratio": neg_ratio,
        "max_tiles_per_game": max_tiles_per_game,
        "counts": stats,
        "build_time_seconds": round(build_time),
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    # Archive shard to F: for safekeeping
    archive_dir = Path(cfg.paths.archive.root) / "training_shards" / version
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        for fname in [
            "train_images.bin",
            "train_index.json",
            "val_images.bin",
            "val_index.json",
            "labels.zip",
            "manifest.json",
            "dataset.yaml",
        ]:
            src = output_dir / fname
            if src.exists():
                logger.info("Archiving %s to F: (%.1f GB)...", fname, src.stat().st_size / 1e9)
                shutil.copy2(str(src), str(archive_dir / fname))
        logger.info("Shard archived to %s", archive_dir)
    except Exception as e:
        logger.warning("Failed to archive shard to F: %s", e)

    # Clean up work dir
    shutil.rmtree(str(work_dir), ignore_errors=True)

    total_tiles = (
        stats["train_positive"]
        + stats["train_negative"]
        + stats["val_positive"]
        + stats["val_negative"]
    )
    logger.info(
        "Shard %s built: %d tiles (%d train, %d val) in %.0fs. "
        "Skipped: %d phase, %d field, %d qa, %d no_pack",
        version,
        total_tiles,
        stats["train_positive"] + stats["train_negative"],
        stats["val_positive"] + stats["val_negative"],
        build_time,
        stats["skipped_phase"],
        stats["skipped_field"],
        stats["skipped_qa"],
        stats["skipped_no_pack"],
    )

    return {"version": version, "total_tiles": total_tiles, **stats}


def _build_game(
    *,
    game_id: str,
    split: str,
    output_dir: Path,
    work_dir: Path,
    bin_writer: BinCacheWriter,
    games_dir: Path,
    archive_packs: Path,
    neg_ratio: float,
    max_tiles_per_game: int,
    phase_filter: bool,
    field_filter: bool,
    qa_filter: bool,
) -> dict:
    """Build one game's contribution to the shard."""
    from training.data_prep.game_manifest import GameManifest

    stats = {
        "positive": 0,
        "negative": 0,
        "skipped_phase": 0,
        "skipped_field": 0,
        "skipped_qa": 0,
        "skipped_no_pack": 0,
        "has_phases": False,
        "has_field_boundary": False,
    }

    # Copy manifest to work dir
    game_dir = games_dir / game_id
    manifest_src = game_dir / "manifest.db"
    if not manifest_src.exists():
        logger.warning("  Skipping %s (no manifest.db)", game_id)
        return stats

    local_game = work_dir / game_id
    local_game.mkdir(parents=True, exist_ok=True)
    local_manifest = local_game / "manifest.db"
    shutil.copy2(str(manifest_src), str(local_manifest))

    manifest = GameManifest(local_game)
    manifest.open(create=False)

    try:
        # Get labeled stems and labels
        labeled_stems = manifest.get_labeled_stems()
        if not labeled_stems:
            logger.info("  %s: no labels, skipping", game_id)
            return stats

        # Load QA-rejected stems
        qa_rejected: set[str] = set()
        if qa_filter:
            rows = manifest.conn.execute(
                "SELECT DISTINCT tile_stem FROM labels "
                "WHERE qa_verdict = 'false_positive'"
            ).fetchall()
            qa_rejected = {r[0] for r in rows}
            if qa_rejected:
                stats["skipped_qa"] = len(qa_rejected & labeled_stems)
                labeled_stems -= qa_rejected

        # Load phase data
        phases = manifest.get_phases()
        stats["has_phases"] = bool(phases)

        # Load field boundary polygon
        polygon = None
        fb_json = manifest.get_metadata("field_boundary")
        if fb_json:
            try:
                fb_data = json.loads(fb_json)
                poly_pts = (
                    fb_data if isinstance(fb_data, list) else fb_data.get("polygon", [])
                )
                if poly_pts:
                    polygon = np.asarray(poly_pts, dtype=np.float32).reshape(-1, 1, 2)
                    stats["has_field_boundary"] = True
            except (json.JSONDecodeError, ValueError):
                pass

        # Collect all candidate tiles with their metadata
        segments = manifest.get_segments()
        rng = random.Random(42 + hash(game_id))

        # Phase 1: Collect positives and candidate negatives separately
        positives: list[tuple[str, dict]] = []
        candidate_negatives: list[tuple[str, dict]] = []

        for segment in segments:
            tiles = manifest.get_tiles_for_segment(segment)
            for tile_info in tiles:
                frame_idx = tile_info["frame_idx"]
                row = tile_info["row"]
                col = tile_info["col"]
                stem = f"{segment}_frame_{frame_idx:06d}_r{row}_c{col}"

                # Phase filter
                if phase_filter and phases:
                    if not manifest.is_active_play(segment, frame_idx):
                        if stem in labeled_stems:
                            stats["skipped_phase"] += 1
                        continue

                has_label = stem in labeled_stems

                # Field boundary filter (for positives)
                if field_filter and polygon is not None and has_label:
                    label_rows = manifest.get_labels_for_tile(stem)
                    if label_rows:
                        lbl = label_rows[0]
                        position = classify_label_position(
                            lbl["cx"], lbl["cy"], row, col, polygon
                        )
                        if position == "far_off_field":
                            stats["skipped_field"] += 1
                            continue
                        if position == "near_off_field":
                            if rng.random() > NEAR_OFF_FIELD_KEEP_RATE:
                                stats["skipped_field"] += 1
                                continue

                if has_label:
                    positives.append((stem, tile_info))
                else:
                    # Field filter for negatives too
                    if field_filter and polygon is not None:
                        position = classify_label_position(0.5, 0.5, row, col, polygon)
                        if position == "far_off_field":
                            continue
                    candidate_negatives.append((stem, tile_info))

        if not positives:
            logger.info("  %s: no positive tiles passed filters", game_id)
            return stats

        # Phase 1b: Apply per-game tile cap, then neg_ratio
        if len(positives) > max_tiles_per_game:
            rng.shuffle(positives)
            pos_count = int(max_tiles_per_game / (1 + neg_ratio))
            positives = positives[:pos_count]
        else:
            pos_count = len(positives)

        max_negatives = min(
            int(neg_ratio * pos_count),
            max_tiles_per_game - pos_count,
        )
        if len(candidate_negatives) > max_negatives:
            rng.shuffle(candidate_negatives)
            candidate_negatives = candidate_negatives[:max_negatives]

        selected: list[tuple[str, dict, bool]] = [
            (stem, ti, True) for stem, ti in positives
        ] + [(stem, ti, False) for stem, ti in candidate_negatives]

        logger.info(
            "  %s: %d positive + %d negative = %d tiles (cap=%d)",
            game_id,
            len(positives),
            len(candidate_negatives),
            len(selected),
            max_tiles_per_game,
        )

        if not selected:
            logger.info("  %s: no tiles passed filters", game_id)
            return stats

        # Phase 2: Group by pack and extract
        pack_tiles: dict[str, list[tuple[str, dict, bool]]] = defaultdict(list)
        for stem, tile_info, has_label in selected:
            pack_file = tile_info.get("pack_file")
            if not pack_file:
                stats["skipped_no_pack"] += 1
                continue
            pack_name = Path(pack_file).name
            pack_tiles[pack_name].append((stem, tile_info, has_label))

        labels_dir = output_dir / "labels" / split / game_id
        labels_dir.mkdir(parents=True, exist_ok=True)

        staging_dir = work_dir / "staged_packs"
        staging_dir.mkdir(parents=True, exist_ok=True)

        for pack_name, tiles_in_pack in pack_tiles.items():
            # Sort by offset for sequential reads
            tiles_in_pack.sort(key=lambda t: t[1]["pack_offset"])

            # Find pack source
            src_path = game_dir / "tile_packs" / pack_name
            if not src_path.exists():
                src_path = archive_packs / game_id / pack_name
            if not src_path.exists():
                logger.debug("    Pack not found: %s/%s", game_id, pack_name)
                stats["skipped_no_pack"] += len(tiles_in_pack)
                continue

            # Stage pack to G: SSD for fast random reads
            staged_path = staging_dir / pack_name
            pack_size_gb = src_path.stat().st_size / 1e9
            logger.info(
                "    Staging %s (%.1f GB, %d tiles) to SSD",
                pack_name,
                pack_size_gb,
                len(tiles_in_pack),
            )
            shutil.copy2(str(src_path), str(staged_path))

            try:
                with open(staged_path, "rb") as f:
                    for stem, tile_info, has_label in tiles_in_pack:
                        try:
                            f.seek(tile_info["pack_offset"])
                            jpeg_bytes = f.read(tile_info["pack_size"])

                            # Write to binary cache
                            rel_path = f"{game_id}/{stem}"
                            if not bin_writer.write_tile(rel_path, jpeg_bytes):
                                continue

                            # Write label file
                            if has_label:
                                label_rows = manifest.get_labels_for_tile(stem)
                                lines = []
                                for lbl in label_rows:
                                    if lbl.get("qa_verdict") == "false_positive":
                                        continue
                                    lines.append(
                                        f"{lbl['class_id']} "
                                        f"{lbl['cx']:.6f} {lbl['cy']:.6f} "
                                        f"{lbl['w']:.6f} {lbl['h']:.6f}"
                                    )
                                if lines:
                                    (labels_dir / f"{stem}.txt").write_text(
                                        "\n".join(lines) + "\n"
                                    )
                                    stats["positive"] += 1
                                else:
                                    (labels_dir / f"{stem}.txt").write_text("")
                                    stats["negative"] += 1
                            else:
                                (labels_dir / f"{stem}.txt").write_text("")
                                stats["negative"] += 1
                        except Exception as e:
                            logger.debug("Failed tile %s: %s", stem, e)
            except Exception as e:
                logger.warning("Failed pack %s: %s", pack_name, e)
            finally:
                try:
                    staged_path.unlink()
                except Exception:
                    pass

        # Flush after each game to ensure data is on disk
        bin_writer.flush()

        # Clean up staging dir
        shutil.rmtree(str(staging_dir), ignore_errors=True)

        logger.info(
            "  %s: %d positive, %d negative tiles",
            game_id,
            stats["positive"],
            stats["negative"],
        )

    finally:
        manifest.close()
        shutil.rmtree(str(local_game), ignore_errors=True)

    return stats
