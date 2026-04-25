"""Train task — build training set and run YOLO training.

Supports two shard formats:
  - bin_cache (new): .bin memmap files for fast loading, no individual image files
  - legacy: individual JPEG files, copied via copytree

Pull-local-process-push pattern:
  - Pull: copy shard files from server share to local SSD
  - Process: run YOLO training with memmap loader
  - Push: copy trained weights back to server
  - Cleanup: remove local training data (keep weights)
"""

import json
import logging
import shutil
import time
import zipfile
from pathlib import Path

import numpy as np

from training.tasks import register_task

logger = logging.getLogger(__name__)

# Binary cache constants (must match build_shard.py)
TILE_H, TILE_W, TILE_C = 640, 640, 3
TILE_BYTES = TILE_H * TILE_W * TILE_C


def _is_bin_cache_shard(shard_dir: Path) -> bool:
    """Check if a shard uses the binary cache format."""
    return (shard_dir / "train_images.bin").exists()


def _check_bin_cache_local(local_dataset: Path) -> bool:
    """Check if binary cache shard is already present locally and valid."""
    for name in ("train_images.bin", "train_index.json"):
        if not (local_dataset / name).exists():
            return False
    try:
        index = json.loads((local_dataset / "train_index.json").read_text())
        expected = index["meta"]["n"] * TILE_BYTES
        actual = (local_dataset / "train_images.bin").stat().st_size
        if actual != expected:
            return False
    except Exception:
        return False
    if not (local_dataset / "labels" / "train").exists():
        return False
    return True


def _copy_bin_shard(shard_dir: Path, local_dataset: Path) -> tuple[int, int]:
    """Copy binary cache shard from server to local. Returns (train_count, val_count)."""
    local_dataset.mkdir(parents=True, exist_ok=True)

    # Copy key files — large sequential reads are fast over SMB
    files = [
        "train_images.bin",
        "train_index.json",
        "val_images.bin",
        "val_index.json",
        "labels.zip",
        "manifest.json",
        "dataset.yaml",
    ]
    for fname in files:
        src = shard_dir / fname
        if not src.exists():
            continue
        dst = local_dataset / fname
        src_size = src.stat().st_size
        # Skip if local file already exists and matches source size
        if dst.exists() and dst.stat().st_size == src_size:
            logger.info("  %s already local (%.2f GB), skipping", fname, src_size / 1e9)
            continue
        logger.info("  Copying %s (%.2f GB)...", fname, src_size / 1e9)
        t0 = time.time()
        shutil.copy2(str(src), str(dst))
        logger.info("  %s copied in %.0fs", fname, time.time() - t0)

    # Extract labels
    zip_path = local_dataset / "labels.zip"
    if zip_path.exists():
        logger.info("  Extracting labels.zip...")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(local_dataset)
        zip_path.unlink()

    # Create empty image dirs (YOLO needs these to exist for get_img_files)
    for split in ("train", "val"):
        (local_dataset / "images" / split).mkdir(parents=True, exist_ok=True)

    # Read counts from indices
    train_count = 0
    val_count = 0
    for split in ("train", "val"):
        index_path = local_dataset / f"{split}_index.json"
        if index_path.exists():
            index = json.loads(index_path.read_text())
            n = index["meta"]["n"]
            if split == "train":
                train_count = n
            else:
                val_count = n

    return train_count, val_count


def _install_memmap_loader(local_dataset: Path, max_train_tiles: int = 0) -> None:
    """Monkey-patch YOLO to load images from binary cache via memmap.

    Patches three things:
    1. get_img_files — returns virtual paths from the index (no physical files needed)
    2. cache_labels — skips image verification, builds labels from .txt files directly
    3. load_image — reads from memmap via lazy-init (survives worker process spawn)

    The load_image patch stores serializable config (paths, sizes, index dicts) on
    each dataset instance. Each worker process lazy-inits its own memmap handle on
    first access, avoiding the unpicklable-memmap problem with multiprocessing.
    """
    from ultralytics.data.base import BaseDataset
    from ultralytics.data.dataset import YOLODataset

    # Load indices — build lookup tables (all serializable: str, int, dict)
    split_paths: dict[str, list[str]] = {}  # split -> list of rel_paths
    split_bin_info: dict[str, tuple[str, int]] = {}  # split -> (bin_path_str, n)
    path_to_memmap: dict[str, int] = {}  # abs_img_path -> index in bin

    # Track which split each image belongs to
    path_to_split: dict[str, str] = {}  # abs_img_path -> split name

    for split in ("train", "val"):
        index_path = local_dataset / f"{split}_index.json"
        if not index_path.exists():
            continue
        index = json.loads(index_path.read_text())
        n = index["meta"]["n"]
        bin_path = str(local_dataset / f"{split}_images.bin")
        paths = index["paths"]
        if max_train_tiles > 0 and split == "train" and len(paths) > max_train_tiles:
            logger.info(
                "  Truncating %s from %d to %d tiles",
                split,
                len(paths),
                max_train_tiles,
            )
            paths = paths[:max_train_tiles]
        split_paths[split] = paths
        split_bin_info[split] = (bin_path, n)

        images_base = local_dataset / "images" / split
        for i, rel_path in enumerate(paths):
            abs_path = str(Path(images_base / f"{rel_path}.jpg"))
            path_to_memmap[abs_path] = i
            path_to_split[abs_path] = split

    logger.info(
        "Memmap loader: %d train + %d val images",
        split_bin_info.get("train", (None, 0))[1],
        split_bin_info.get("val", (None, 0))[1],
    )

    # --- Patch 1: get_img_files ---
    _original_get_img_files = BaseDataset.get_img_files

    def _patched_get_img_files(self, img_path):
        """Return image paths from memmap index if available."""
        img_path_str = (
            str(img_path) if not isinstance(img_path, list) else str(img_path[0])
        )
        img_path_norm = img_path_str.replace("\\", "/").lower()
        logger.info("  get_img_files called with: %s", img_path_str)
        for split, paths in split_paths.items():
            split_dir = str(local_dataset / "images" / split).replace("\\", "/").lower()
            if split_dir in img_path_norm:
                images_base = local_dataset / "images" / split
                result = [
                    str(Path(images_base / f"{rel_path}.jpg")) for rel_path in paths
                ]
                logger.info("  %s: %d images from memmap index", split, len(result))
                return result
        logger.warning(
            "  get_img_files: no memmap match for %s, falling back", img_path_str
        )
        return _original_get_img_files(self, img_path)

    BaseDataset.get_img_files = _patched_get_img_files

    # --- Patch 2: cache_labels ---
    _original_cache_labels = YOLODataset.cache_labels

    def _patched_cache_labels(self, path=Path("./labels.cache")):
        """Build label cache without image verification."""
        logger.info(
            "  cache_labels called, %d im_files",
            len(self.im_files) if self.im_files else 0,
        )
        if self.im_files:
            logger.info("  first im_file: %s", self.im_files[0])
            logger.info("  in path_to_memmap: %s", self.im_files[0] in path_to_memmap)
        if not self.im_files or self.im_files[0] not in path_to_memmap:
            return _original_cache_labels(self, path)

        # Store memmap config on the dataset instance for worker processes
        # These are all picklable (str, int, dict)
        self._memmap_idx = {}  # im_file -> index in bin
        self._memmap_split = {}  # im_file -> split name
        self._memmap_bin_info = split_bin_info  # split -> (path, n)
        for im_file in self.im_files:
            if im_file in path_to_memmap:
                self._memmap_idx[im_file] = path_to_memmap[im_file]
                self._memmap_split[im_file] = path_to_split[im_file]

        x = {"labels": []}
        nf, nm, ne = 0, 0, 0
        total = len(self.im_files)
        for idx, (im_file, lb_file) in enumerate(zip(self.im_files, self.label_files)):
            if (idx + 1) % 10000 == 0:
                logger.info("  cache_labels progress: %d/%d", idx + 1, total)
            lb_path = Path(lb_file)
            if lb_path.exists():
                content = lb_path.read_text().strip()
                if content:
                    lb = np.array(
                        [
                            list(map(float, line.split()))
                            for line in content.splitlines()
                        ],
                        dtype=np.float32,
                    )
                    nf += 1
                else:
                    lb = np.zeros((0, 5), dtype=np.float32)
                    ne += 1
            else:
                lb = np.zeros((0, 5), dtype=np.float32)
                nm += 1

            x["labels"].append(
                {
                    "im_file": im_file,
                    "shape": (TILE_H, TILE_W),
                    "cls": lb[:, 0:1]
                    if len(lb)
                    else np.zeros((0, 1), dtype=np.float32),
                    "bboxes": lb[:, 1:]
                    if len(lb)
                    else np.zeros((0, 4), dtype=np.float32),
                    "segments": [],
                    "keypoints": None,
                    "normalized": True,
                    "bbox_format": "xywh",
                }
            )

        x["hash"] = ""
        x["version"] = ""
        x["results"] = nf, nm, ne, 0, len(self.im_files)
        x["msgs"] = []
        logger.info(
            "  Label cache: %d found, %d missing, %d empty, %d total",
            nf,
            nm,
            ne,
            len(self.im_files),
        )
        return x

    YOLODataset.cache_labels = _patched_cache_labels

    # --- Patch 3: swap dataset class to MemmapYOLODataset after creation ---
    # Instead of monkey-patching load_image (which doesn't survive worker spawn),
    # we swap the dataset's __class__ to MemmapYOLODataset which has a proper
    # load_image override. Workers import the class from the module file.
    from ultralytics.data.build import build_yolo_dataset as _orig_build

    def _patched_build(
        cfg,
        img_path,
        batch,
        data,
        mode="train",
        rect=False,
        stride=32,
        multi_modal=False,
    ):
        """Build dataset then swap class to MemmapYOLODataset."""
        dataset = _orig_build(
            cfg, img_path, batch, data, mode, rect, stride, multi_modal
        )
        # Only swap if the dataset has memmap config (set by our patched cache_labels)
        if hasattr(dataset, "_memmap_idx"):
            from training.data_prep.memmap_dataset import MemmapYOLODataset

            dataset.__class__ = MemmapYOLODataset
            logger.info("  Swapped dataset class to MemmapYOLODataset (%s)", mode)
        return dataset

    import ultralytics.data.build

    ultralytics.data.build.build_yolo_dataset = _patched_build
    # Also patch the import in the detect trainer
    import ultralytics.models.yolo.detect.train

    ultralytics.models.yolo.detect.train.build_yolo_dataset = _patched_build


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

    # Local directories
    local_dataset = local_work_dir / "training" / version
    local_weights = local_work_dir / "training" / "weights"
    local_weights.mkdir(parents=True, exist_ok=True)

    # Check for pre-built shard (from build_shard task)
    shard_version = payload.get("shard_version")
    use_memmap = False

    if shard_version:
        shard_dir = (
            Path(cfg.paths.training_sets).parent / "training_shards" / shard_version
        )
        if server_share:
            shard_dir = Path(server_share) / "training_shards" / shard_version

        if shard_dir.exists() and (shard_dir / "dataset.yaml").exists():
            is_bin = _is_bin_cache_shard(shard_dir)

            if is_bin:
                if _check_bin_cache_local(local_dataset):
                    logger.info("Binary cache shard already local, skipping copy")
                    index = json.loads((local_dataset / "train_index.json").read_text())
                    train_tile_count = index["meta"]["n"]
                    val_index_path = local_dataset / "val_index.json"
                    val_tile_count = (
                        json.loads(val_index_path.read_text())["meta"]["n"]
                        if val_index_path.exists()
                        else 0
                    )
                else:
                    logger.info(
                        "Copying binary cache shard %s from %s",
                        shard_version,
                        shard_dir,
                    )
                    t0 = time.time()
                    train_tile_count, val_tile_count = _copy_bin_shard(
                        shard_dir, local_dataset
                    )
                    logger.info(
                        "Shard copied: %d train, %d val tiles (%.0fs)",
                        train_tile_count,
                        val_tile_count,
                        time.time() - t0,
                    )
                use_memmap = True
            else:
                # Legacy JPEG shard format
                local_train_dir = local_dataset / "images" / "train"
                already_cached = False
                if local_train_dir.exists():
                    local_count = sum(1 for _ in local_train_dir.rglob("*.jpg"))
                    if local_count > 0:
                        logger.info(
                            "Legacy shard already local: %d train tiles",
                            local_count,
                        )
                        already_cached = True
                        train_tile_count = local_count
                        val_tile_count = sum(
                            1 for _ in (local_dataset / "images" / "val").rglob("*.jpg")
                        )

                if not already_cached:
                    logger.info(
                        "Using legacy shard %s from %s", shard_version, shard_dir
                    )
                    t0 = time.time()
                    shutil.copytree(
                        str(shard_dir), str(local_dataset), dirs_exist_ok=True
                    )
                    train_tile_count = sum(
                        1 for _ in (local_dataset / "images" / "train").rglob("*.jpg")
                    )
                    val_tile_count = sum(
                        1 for _ in (local_dataset / "images" / "val").rglob("*.jpg")
                    )
                    logger.info(
                        "Shard copied: %d train, %d val tiles (%.0fs)",
                        train_tile_count,
                        val_tile_count,
                        time.time() - t0,
                    )

            # Rewrite dataset.yaml to point to local path
            dataset_yaml = local_dataset / "dataset.yaml"
            dataset_yaml.write_text(
                f"path: {local_dataset}\n"
                f"train: images/train\n"
                f"val: images/val\n"
                f"nc: 1\n"
                f"names: ['ball']\n"
            )
        else:
            logger.warning(
                "Shard %s not found at %s, falling back to _build_split",
                shard_version,
                shard_dir,
            )
            shard_version = None

    if not shard_version:
        if not train_games:
            raise ValueError("No train_games specified in payload")

        local_images_train = local_dataset / "images" / "train"
        local_images_val = local_dataset / "images" / "val"
        local_labels_train = local_dataset / "labels" / "train"
        local_labels_val = local_dataset / "labels" / "val"
        for d in [
            local_images_train,
            local_images_val,
            local_labels_train,
            local_labels_val,
        ]:
            d.mkdir(parents=True, exist_ok=True)

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

        dataset_yaml = local_dataset / "dataset.yaml"
        dataset_yaml.write_text(
            f"path: {local_dataset}\n"
            f"train: images/train\n"
            f"val: images/val\n"
            f"nc: 1\n"
            f"names: ['ball']\n"
        )

    # Install memmap loader if using binary cache
    if use_memmap:
        max_train_tiles = payload.get("max_train_tiles", 0)
        _install_memmap_loader(local_dataset, max_train_tiles=max_train_tiles)

    # Step 2: Train
    logger.info("Starting YOLO training...")
    from ultralytics import YOLO

    model_base = cfg.training.model_base
    if resume_from:
        logger.info("Resuming from checkpoint: %s", resume_from)
        model = YOLO(resume_from)
    else:
        logger.info("Starting from pretrained base: %s", model_base)
        model = YOLO(model_base)

    # Set up epoch-end callback to archive best.pt after each improvement.
    # If training crashes, we still have the latest best weights on F:.
    archive_weights = Path(cfg.paths.archive.checkpoints) / version
    _best_pt_path = local_weights / version / "weights" / "best.pt"

    # Also prepare server weights dir for incremental push
    server_weights = Path(cfg.paths.training_sets) / version / "weights"
    if server_share and not Path(cfg.paths.training_sets).exists():
        server_weights = Path(server_share) / "training_sets" / version / "weights"

    def _archive_on_epoch_end(trainer):
        """Archive best.pt to F: and D: after each epoch if it was updated."""
        from datetime import datetime

        if not _best_pt_path.exists():
            return
        try:
            # Archive to F: (permanent)
            archive_weights.mkdir(parents=True, exist_ok=True)
            dst = archive_weights / "best.pt"
            if not dst.exists() or _best_pt_path.stat().st_mtime > dst.stat().st_mtime:
                shutil.copy2(str(_best_pt_path), str(dst))
                # Also save a timestamped snapshot so we can compare epochs
                snapshot = archive_weights / f"best_e{trainer.epoch}_{datetime.now().strftime('%Y%m%d_%H%M')}.pt"
                shutil.copy2(str(_best_pt_path), str(snapshot))
                logger.info("Archived best.pt to F: (epoch %d → %s)", trainer.epoch, snapshot.name)

            # Push to D: (server share — so orchestrator can find it for next run)
            server_weights.mkdir(parents=True, exist_ok=True)
            srv_dst = server_weights / "best.pt"
            if not srv_dst.exists() or _best_pt_path.stat().st_mtime > srv_dst.stat().st_mtime:
                shutil.copy2(str(_best_pt_path), str(srv_dst))
        except Exception as e:
            logger.debug("Failed to archive checkpoint: %s", e)

    model.add_callback("on_train_epoch_end", _archive_on_epoch_end)

    results = model.train(
        data=str(dataset_yaml),
        epochs=cfg.training.epochs,
        batch=cfg.training.batch_size,
        imgsz=cfg.training.imgsz,
        device=0,
        patience=cfg.training.patience,
        workers=4,  # memmap uses lazy-init per worker, safe with multiprocessing
        cache=False if use_memmap else "disk",
        deterministic=True,
        project=str(local_weights),
        name=version,
    )

    # Find best weights
    best_pt = local_weights / version / "weights" / "best.pt"
    last_pt = local_weights / version / "weights" / "last.pt"

    if not best_pt.exists():
        raise RuntimeError(f"Training did not produce best.pt at {best_pt}")

    # Step 3: Final push of weights to server + archive
    # (epoch callback already does incremental pushes, this ensures last.pt too)
    server_weights.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(best_pt), str(server_weights / "best.pt"))
    if last_pt.exists():
        shutil.copy2(str(last_pt), str(server_weights / "last.pt"))

    try:
        archive_weights.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(best_pt), str(archive_weights / "best.pt"))
        if last_pt.exists():
            shutil.copy2(str(last_pt), str(archive_weights / "last.pt"))
        logger.info("Final archive to %s", archive_weights)
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
    """Get tile stems that should be oversampled for gap-focused training."""
    import json as _json

    conn = manifest.conn
    priority: dict[str, int] = {}

    rows = conn.execute(
        "SELECT DISTINCT tile_stem FROM labels WHERE source = 'human_gap_review'"
    ).fetchall()
    for (stem,) in rows:
        priority[stem] = 3

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
    """Build one split (train or val) by extracting tiles from pack files."""
    from training.data_prep.game_manifest import GameManifest

    total = 0
    for game_id in game_ids:
        game_dir = server_games_dir / game_id
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
            labeled_stems = manifest.get_labeled_stems()
            game_images = images_dir / game_id
            game_labels = labels_dir / game_id
            game_images.mkdir(parents=True, exist_ok=True)
            game_labels.mkdir(parents=True, exist_ok=True)

            manifest.export_labels_yolo(game_labels)

            priority_stems = _get_priority_stems(manifest)
            if priority_stems:
                logger.info(
                    "  %s: %d priority stems for oversampling",
                    game_id,
                    len(priority_stems),
                )

            import random
            from collections import defaultdict

            segments = manifest.get_segments()
            extracted = 0

            pack_tiles: dict[str, list[tuple[str, dict, bool]]] = defaultdict(list)
            for segment in segments:
                tiles = manifest.get_tiles_for_segment(segment)
                for tile_info in tiles:
                    stem = f"{segment}_frame_{tile_info['frame_idx']:06d}_r{tile_info['row']}_c{tile_info['col']}"
                    has_label = stem in labeled_stems
                    if not has_label:
                        if random.random() > neg_ratio / max(
                            1, len(tiles) / max(1, len(labeled_stems))
                        ):
                            continue
                    pack_file = tile_info.get("pack_file")
                    if not pack_file:
                        continue
                    pack_name = Path(pack_file).name
                    pack_tiles[pack_name].append((stem, tile_info, has_label))

            local_pack_dir = local_game / "tile_packs"
            local_pack_dir.mkdir(parents=True, exist_ok=True)

            for pack_name, tiles_in_pack in pack_tiles.items():
                pack_path = local_pack_dir / pack_name

                if not pack_path.exists():
                    src = server_games_dir / game_id / "tile_packs" / pack_name
                    if not src.exists():
                        from training.data_prep.manifest_dataset import (
                            _resolve_pack_path,
                        )

                        try:
                            src = Path(tiles_in_pack[0][1].get("pack_file", ""))
                            src = Path(_resolve_pack_path(str(src)))
                        except FileNotFoundError:
                            continue

                    logger.info(
                        "    Staging pack %s to local SSD (%.1f GB)",
                        pack_name,
                        src.stat().st_size / 1e9,
                    )
                    shutil.copy2(str(src), str(pack_path))

                try:
                    with open(pack_path, "rb") as f:
                        for stem, tile_info, has_label in tiles_in_pack:
                            try:
                                f.seek(tile_info["pack_offset"])
                                jpeg_bytes = f.read(tile_info["pack_size"])

                                (game_images / f"{stem}.jpg").write_bytes(jpeg_bytes)
                                extracted += 1

                                if not has_label:
                                    (game_labels / f"{stem}.txt").write_text("")

                                repeat = priority_stems.get(stem, 1)
                                label_file = game_labels / f"{stem}.txt"
                                for dup in range(1, repeat):
                                    dup_stem = f"{stem}_dup{dup}"
                                    (game_images / f"{dup_stem}.jpg").write_bytes(
                                        jpeg_bytes
                                    )
                                    if label_file.exists():
                                        shutil.copy2(
                                            str(label_file),
                                            str(game_labels / f"{dup_stem}.txt"),
                                        )
                                    extracted += 1
                            except Exception as e:
                                logger.debug("Failed to extract tile %s: %s", stem, e)
                except Exception as e:
                    logger.warning("Failed to read pack %s: %s", pack_name, e)

                try:
                    pack_path.unlink()
                except Exception:
                    pass
        finally:
            manifest.close()

        local_pack_dir = local_game / "tile_packs"
        if local_pack_dir.exists():
            shutil.rmtree(str(local_pack_dir), ignore_errors=True)

        total += extracted
        logger.info(
            "  %s: %d tiles extracted (%d positive)",
            game_id,
            extracted,
            len(labeled_stems),
        )

    return total
