"""YOLO dataset + trainer backed by manifest.db + pack files.

No .txt label files, no loose .jpg tiles. Everything comes from:
- manifest.db: tile inventory + labels (curated training set)
- .pack files: concatenated JPEG bytes with offsets

Usage:
    # Build a curated training set
    from training.data_prep.manifest_dataset import build_training_set
    build_training_set(
        master_db="D:/training_data/manifest.db",
        master_packs="D:/training_data/tile_packs",
        output_dir="D:/training_data/training_sets/v3.1",
        train_games=["flash__2024.05.01_vs_RNYFC_away", ...],
        val_games=["flash__2024.06.02_vs_Flash_2014s_scrimmage"],
        neg_ratio=1.0,
    )

    # Train (on laptop after transferring training set)
    from training.data_prep.manifest_dataset import ManifestTrainer
    from ultralytics import YOLO
    model = YOLO("yolo26l.pt")
    model.train(data="training_sets/v3.1/dataset.yaml", trainer=ManifestTrainer, ...)
"""

import io
import math
import random
import re
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

from ultralytics.data.dataset import YOLODataset
from ultralytics.models.yolo.detect.train import DetectionTrainer
from ultralytics.utils import LOGGER, colorstr
from ultralytics.utils.torch_utils import unwrap_model

TILE_RE = re.compile(r"^(.+)_frame_(\d{6})_r(\d+)_c(\d+)$")
TILE_SIZE = 640
EXCLUDE_ROWS = {0}  # row 0 is sky/far-field


# ---------------------------------------------------------------------------
# ManifestDataset — reads images from packs, labels from SQLite
# ---------------------------------------------------------------------------

class ManifestDataset(YOLODataset):
    """YOLO dataset that reads from manifest.db + pack files."""

    def __init__(self, *args, db_path=None, game_ids=None,
                 neg_ratio=1.0, hard_neg_ratio=0.5, seed=42, **kwargs):
        # Store config BEFORE super().__init__ calls get_img_files/get_labels
        self._db_path = db_path
        self._game_ids = game_ids or []
        self._neg_ratio = neg_ratio
        self._hard_neg_ratio = hard_neg_ratio
        self._seed = seed
        self._tile_index = []    # (pack_file, offset, size) per image
        self._label_data = []    # label dict per image
        self._conn = None
        self._pack_handles = {}  # cache open file handles

        super().__init__(*args, **kwargs)

    def _get_conn(self):
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def get_img_files(self, img_path):
        """Override: build image list from manifest instead of scanning directories."""
        conn = self._get_conn()
        random.seed(self._seed)

        im_files = []
        tile_index = []
        label_data = []

        for gid in self._game_ids:
            # Get labels grouped by stem
            label_rows = conn.execute(
                "SELECT tile_stem, class_id, cx, cy, w, h FROM labels WHERE game_id = ?",
                (gid,),
            ).fetchall()
            labels_by_stem = defaultdict(list)
            for stem, cls, cx, cy, w, h in label_rows:
                labels_by_stem[stem].append((cls, cx, cy, w, h))

            # Get pack info for ALL tiles
            tile_rows = conn.execute(
                "SELECT segment, frame_idx, row, col, pack_file, pack_offset, pack_size "
                "FROM tiles WHERE game_id = ? AND pack_file IS NOT NULL",
                (gid,),
            ).fetchall()

            tile_info = {}  # key -> (pack_file, offset, size)
            for seg, fidx, r, c, pf, po, ps in tile_rows:
                if r in EXCLUDE_ROWS:
                    continue
                tile_info[(seg, fidx, r, c)] = (pf, po, ps)

            # Positive tiles
            positive_keys = set()
            for stem, detections in labels_by_stem.items():
                m = TILE_RE.match(stem)
                if not m:
                    continue
                key = (m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)))
                if key not in tile_info:
                    continue
                positive_keys.add(key)
                pf, po, ps = tile_info[key]

                im_files.append(f"pack://{gid}/{stem}.jpg")
                tile_index.append((pf, po, ps))

                cls_arr = np.array([[d[0]] for d in detections], dtype=np.float32)
                bbox_arr = np.array([[d[1], d[2], d[3], d[4]] for d in detections], dtype=np.float32)
                label_data.append({
                    "im_file": f"pack://{gid}/{stem}.jpg",
                    "shape": (TILE_SIZE, TILE_SIZE),
                    "cls": cls_arr,
                    "bboxes": bbox_arr,
                    "segments": [],
                    "keypoints": None,
                    "normalized": True,
                    "bbox_format": "xywh",
                })

            # Hard negatives: spatial + temporal neighbors
            hard_neg_keys = set()
            for seg, fidx, r, c in positive_keys:
                for dr in (-1, 0, 1):
                    for dc in (-1, 0, 1):
                        if dr == 0 and dc == 0:
                            continue
                        nk = (seg, fidx, r + dr, c + dc)
                        if nk in tile_info and nk not in positive_keys:
                            hard_neg_keys.add(nk)
                for df in (-4, 4):
                    nk = (seg, fidx + df, r, c)
                    if nk in tile_info and nk not in positive_keys:
                        hard_neg_keys.add(nk)

            max_hard = int(len(positive_keys) * self._neg_ratio * self._hard_neg_ratio)
            if len(hard_neg_keys) > max_hard:
                hard_neg_keys = set(random.sample(list(hard_neg_keys), max_hard))

            # Random negatives
            max_random = int(len(positive_keys) * self._neg_ratio * (1 - self._hard_neg_ratio))
            all_neg = set(tile_info.keys()) - positive_keys - hard_neg_keys
            random_neg_keys = set()
            if max_random > 0 and all_neg:
                random_neg_keys = set(random.sample(list(all_neg), min(max_random, len(all_neg))))

            # Add negatives
            empty_cls = np.zeros((0, 1), dtype=np.float32)
            empty_bbox = np.zeros((0, 4), dtype=np.float32)
            for key in hard_neg_keys | random_neg_keys:
                seg, fidx, r, c = key
                stem = f"{seg}_frame_{fidx:06d}_r{r}_c{c}"
                pf, po, ps = tile_info[key]
                vpath = f"pack://{gid}/{stem}.jpg"
                im_files.append(vpath)
                tile_index.append((pf, po, ps))
                label_data.append({
                    "im_file": vpath,
                    "shape": (TILE_SIZE, TILE_SIZE),
                    "cls": empty_cls,
                    "bboxes": empty_bbox,
                    "segments": [],
                    "keypoints": None,
                    "normalized": True,
                    "bbox_format": "xywh",
                })

            n_pos = len(positive_keys)
            n_neg = len(hard_neg_keys) + len(random_neg_keys)
            LOGGER.info(f"  {gid}: {n_pos} pos + {n_neg} neg ({len(hard_neg_keys)} hard, {len(random_neg_keys)} random)")

        self._tile_index = tile_index
        self._label_data = label_data

        LOGGER.info(f"ManifestDataset: {len(im_files)} total tiles "
                     f"({sum(1 for ld in label_data if len(ld['cls']) > 0)} pos, "
                     f"{sum(1 for ld in label_data if len(ld['cls']) == 0)} neg)")
        return im_files

    def get_labels(self):
        """Override: return pre-built labels from manifest queries."""
        self.im_files = [ld["im_file"] for ld in self._label_data]
        n = len(self._label_data)
        nf = sum(1 for ld in self._label_data if len(ld["cls"]) > 0)
        ne = n - nf
        LOGGER.info(f"{self.prefix}Manifest: {nf} labeled, {ne} background, {n} total")
        return self._label_data

    def cache_labels(self, path=None):
        """Override: skip file-based caching."""
        labels = self._label_data
        n = len(labels)
        nf = sum(1 for lb in labels if len(lb["cls"]) > 0)
        ne = n - nf
        return {
            "labels": labels,
            "hash": f"manifest_{self._db_path}_{self._seed}",
            "results": (nf, 0, ne, 0, n),
            "msgs": [],
            "version": 1,
        }

    def load_image(self, i, rect_mode=True):
        """Override: read image from pack file."""
        im = self.ims[i]
        if im is not None:
            return self.ims[i], self.im_hw0[i], self.im_hw[i]

        pack_file, offset, size = self._tile_index[i]

        # Use cached file handle
        if pack_file not in self._pack_handles:
            self._pack_handles[pack_file] = open(pack_file, "rb")
        fh = self._pack_handles[pack_file]

        fh.seek(offset)
        data = fh.read(size)
        im = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)

        if im is None:
            raise FileNotFoundError(f"Failed to decode tile at index {i}")

        h0, w0 = im.shape[:2]
        if rect_mode:
            r = self.imgsz / max(h0, w0)
            if r != 1:
                w, h = (min(math.ceil(w0 * r), self.imgsz), min(math.ceil(h0 * r), self.imgsz))
                im = cv2.resize(im, (w, h), interpolation=cv2.INTER_LINEAR)
        elif not (h0 == w0 == self.imgsz):
            im = cv2.resize(im, (self.imgsz, self.imgsz), interpolation=cv2.INTER_LINEAR)

        if im.ndim == 2:
            im = im[..., None]

        if self.augment:
            self.ims[i], self.im_hw0[i], self.im_hw[i] = im, (h0, w0), im.shape[:2]
            self.buffer.append(i)
            if 1 < len(self.buffer) >= self.max_buffer_length:
                j = self.buffer.pop(0)
                if self.cache != "ram":
                    self.ims[j], self.im_hw0[j], self.im_hw[j] = None, None, None

        return im, (h0, w0), im.shape[:2]

    def __del__(self):
        for fh in self._pack_handles.values():
            fh.close()
        if self._conn:
            self._conn.close()


# ---------------------------------------------------------------------------
# ManifestTrainer — plugs ManifestDataset into YOLO training
# ---------------------------------------------------------------------------

class ManifestTrainer(DetectionTrainer):
    """Detection trainer that uses ManifestDataset."""

    def build_dataset(self, img_path, mode="train", batch=None):
        """Build ManifestDataset from data dict's manifest config."""
        gs = max(int(unwrap_model(self.model).stride.max()), 32)

        game_key = "manifest_train_games" if mode == "train" else "manifest_val_games"
        game_ids = self.data.get(game_key, [])
        db_path = self.data.get("manifest_db")
        neg_ratio = self.data.get("manifest_neg_ratio", 1.0)

        if not db_path or not game_ids:
            LOGGER.warning(f"No manifest config for {mode}, falling back to standard dataset")
            return super().build_dataset(img_path, mode, batch)

        LOGGER.info(f"ManifestDataset ({mode}): {len(game_ids)} games, neg_ratio={neg_ratio}")

        return ManifestDataset(
            img_path=img_path,
            imgsz=self.args.imgsz,
            batch_size=batch,
            augment=(mode == "train"),
            hyp=self.args,
            rect=self.args.rect or (mode == "val"),
            cache=self.args.cache or None,
            single_cls=self.args.single_cls or False,
            stride=int(gs),
            pad=0.0 if mode == "train" else 0.5,
            prefix=colorstr(f"{mode}: "),
            task=self.args.task,
            classes=self.args.classes,
            data=self.data,
            fraction=self.args.fraction if mode == "train" else 1.0,
            # ManifestDataset-specific args
            db_path=db_path,
            game_ids=game_ids,
            neg_ratio=neg_ratio if mode == "train" else 0.5,
        )


# ---------------------------------------------------------------------------
# build_training_set — curate a versioned training dataset
# ---------------------------------------------------------------------------

def build_training_set(
    master_db,
    master_packs,
    output_dir,
    train_games,
    val_games,
    neg_ratio=1.0,
    hard_neg_ratio=0.5,
    seed=42,
    camera_neg_games=None,
    neg_per_camera_game=2000,
):
    """Build a curated training set from the master manifest.

    Creates a self-contained directory with:
    - manifest.db: small DB with only selected tiles + labels
    - packs/: pack files with only selected tiles
    - dataset.yaml: config for ManifestTrainer

    Args:
        master_db: path to master manifest.db
        master_packs: path to master tile_packs directory
        output_dir: where to create the training set
        train_games: list of game IDs for training
        val_games: list of game IDs for validation
        neg_ratio: negative:positive ratio
        hard_neg_ratio: fraction of negatives that are hard (vs random)
        seed: random seed
        camera_neg_games: optional list of unlabeled games for additional negatives
        neg_per_camera_game: how many random negatives per camera game
    """
    import shutil

    random.seed(seed)
    output_dir = Path(output_dir)
    master_db = Path(master_db)
    master_packs = Path(master_packs)

    output_dir.mkdir(parents=True, exist_ok=True)
    packs_out = output_dir / "packs"
    packs_out.mkdir(exist_ok=True)

    # Create empty dirs for YOLO validation
    (output_dir / "images" / "train").mkdir(parents=True, exist_ok=True)
    (output_dir / "images" / "val").mkdir(parents=True, exist_ok=True)

    all_games = list(train_games) + list(val_games)
    if camera_neg_games:
        all_games += list(camera_neg_games)

    print(f"Building training set: {len(train_games)} train, {len(val_games)} val games")
    print(f"Output: {output_dir}")

    # Open master DB
    master = sqlite3.connect(str(master_db))
    master.execute("PRAGMA journal_mode=WAL")

    # Create training manifest DB
    train_db_path = output_dir / "manifest.db"
    if train_db_path.exists():
        train_db_path.unlink()
    train_conn = sqlite3.connect(str(train_db_path))
    train_conn.executescript("""
        CREATE TABLE games (
            game_id TEXT PRIMARY KEY,
            tile_count INTEGER DEFAULT 0,
            labeled_count INTEGER DEFAULT 0
        );
        CREATE TABLE tiles (
            game_id TEXT NOT NULL,
            segment TEXT NOT NULL,
            frame_idx INTEGER NOT NULL,
            row INTEGER NOT NULL,
            col INTEGER NOT NULL,
            pack_file TEXT,
            pack_offset INTEGER,
            pack_size INTEGER,
            PRIMARY KEY (game_id, segment, frame_idx, row, col)
        );
        CREATE TABLE labels (
            id INTEGER PRIMARY KEY,
            game_id TEXT NOT NULL,
            tile_stem TEXT NOT NULL,
            class_id INTEGER DEFAULT 0,
            cx REAL NOT NULL, cy REAL NOT NULL,
            w REAL NOT NULL, h REAL NOT NULL,
            source TEXT, confidence REAL
        );
        CREATE INDEX idx_labels_game ON labels(game_id);
        CREATE INDEX idx_labels_stem ON labels(tile_stem);
        CREATE INDEX idx_tiles_game ON tiles(game_id);
    """)

    t0 = time.time()
    total_pos = 0
    total_neg = 0

    for gid in train_games + val_games:
        print(f"\n  Processing {gid}...")

        # Get labels from master
        label_rows = master.execute(
            "SELECT tile_stem, class_id, cx, cy, w, h, source, confidence FROM labels WHERE game_id = ?",
            (gid,),
        ).fetchall()
        labels_by_stem = defaultdict(list)
        for row in label_rows:
            labels_by_stem[row[0]].append(row)

        # Get tile pack info from master
        tile_rows = master.execute(
            "SELECT segment, frame_idx, row, col, pack_file, pack_offset, pack_size "
            "FROM tiles WHERE game_id = ? AND pack_file IS NOT NULL",
            (gid,),
        ).fetchall()
        tile_info = {}
        for seg, fidx, r, c, pf, po, ps in tile_rows:
            if r in EXCLUDE_ROWS:
                continue
            tile_info[(seg, fidx, r, c)] = (pf, po, ps)

        # Identify positive tiles
        positive_keys = set()
        for stem in labels_by_stem:
            m = TILE_RE.match(stem)
            if not m:
                continue
            key = (m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)))
            if key in tile_info:
                positive_keys.add(key)

        # Hard negatives
        hard_neg_keys = set()
        for seg, fidx, r, c in positive_keys:
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    nk = (seg, fidx, r + dr, c + dc)
                    if nk in tile_info and nk not in positive_keys:
                        hard_neg_keys.add(nk)
            for df in (-4, 4):
                nk = (seg, fidx + df, r, c)
                if nk in tile_info and nk not in positive_keys:
                    hard_neg_keys.add(nk)

        max_hard = int(len(positive_keys) * neg_ratio * hard_neg_ratio)
        if len(hard_neg_keys) > max_hard:
            hard_neg_keys = set(random.sample(list(hard_neg_keys), max_hard))

        # Random negatives
        max_random = int(len(positive_keys) * neg_ratio * (1 - hard_neg_ratio))
        all_neg_pool = set(tile_info.keys()) - positive_keys - hard_neg_keys
        random_neg_keys = set()
        if max_random > 0 and all_neg_pool:
            random_neg_keys = set(random.sample(list(all_neg_pool), min(max_random, len(all_neg_pool))))

        selected_keys = positive_keys | hard_neg_keys | random_neg_keys

        # Build new pack file with only selected tiles
        # Group by source pack file for sequential reads
        by_pack = defaultdict(list)
        for key in selected_keys:
            pf, po, ps = tile_info[key]
            by_pack[pf].append((po, ps, key))

        game_pack_path = packs_out / f"{gid}.pack"
        new_offset = 0
        tile_inserts = []

        with open(game_pack_path, "wb") as out_fh:
            for src_pack in sorted(by_pack.keys()):
                entries = sorted(by_pack[src_pack])  # sort by offset for sequential read
                with open(src_pack, "rb") as src_fh:
                    for offset, size, key in entries:
                        src_fh.seek(offset)
                        data = src_fh.read(size)
                        out_fh.write(data)

                        seg, fidx, r, c = key
                        tile_inserts.append((
                            gid, seg, fidx, r, c,
                            str(game_pack_path), new_offset, len(data),
                        ))
                        new_offset += len(data)

        # Insert tiles into training manifest
        train_conn.executemany(
            "INSERT INTO tiles (game_id, segment, frame_idx, row, col, pack_file, pack_offset, pack_size) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            tile_inserts,
        )

        # Insert labels for positive tiles only
        label_inserts = []
        for stem, rows in labels_by_stem.items():
            m = TILE_RE.match(stem)
            if not m:
                continue
            key = (m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)))
            if key in positive_keys:
                for row in rows:
                    label_inserts.append((gid, *row))

        train_conn.executemany(
            "INSERT INTO labels (game_id, tile_stem, class_id, cx, cy, w, h, source, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            label_inserts,
        )

        # Game metadata
        train_conn.execute(
            "INSERT INTO games (game_id, tile_count, labeled_count) VALUES (?, ?, ?)",
            (gid, len(selected_keys), len(positive_keys)),
        )
        train_conn.commit()

        n_pos = len(positive_keys)
        n_neg = len(hard_neg_keys) + len(random_neg_keys)
        total_pos += n_pos
        total_neg += n_neg
        pack_mb = new_offset / 1024 / 1024
        print(f"    {n_pos} pos + {n_neg} neg = {len(selected_keys)} tiles, pack: {pack_mb:.0f}MB")

    # Handle camera game negatives
    if camera_neg_games:
        for gid in camera_neg_games:
            print(f"\n  Camera negatives: {gid}...")
            tile_rows = master.execute(
                "SELECT segment, frame_idx, row, col, pack_file, pack_offset, pack_size "
                "FROM tiles WHERE game_id = ? AND pack_file IS NOT NULL AND row NOT IN (0) "
                "ORDER BY RANDOM() LIMIT ?",
                (gid, neg_per_camera_game),
            ).fetchall()

            if not tile_rows:
                print(f"    No packed tiles found, skipping")
                continue

            game_pack_path = packs_out / f"{gid}.pack"
            new_offset = 0
            tile_inserts = []

            by_pack = defaultdict(list)
            for seg, fidx, r, c, pf, po, ps in tile_rows:
                by_pack[pf].append((po, ps, (seg, fidx, r, c)))

            with open(game_pack_path, "wb") as out_fh:
                for src_pack in sorted(by_pack.keys()):
                    entries = sorted(by_pack[src_pack])
                    with open(src_pack, "rb") as src_fh:
                        for offset, size, key in entries:
                            src_fh.seek(offset)
                            data = src_fh.read(size)
                            out_fh.write(data)
                            seg, fidx, r, c = key
                            tile_inserts.append((
                                gid, seg, fidx, r, c,
                                str(game_pack_path), new_offset, len(data),
                            ))
                            new_offset += len(data)

            train_conn.executemany(
                "INSERT INTO tiles (game_id, segment, frame_idx, row, col, pack_file, pack_offset, pack_size) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                tile_inserts,
            )
            train_conn.execute(
                "INSERT INTO games (game_id, tile_count, labeled_count) VALUES (?, ?, 0)",
                (gid, len(tile_inserts)),
            )
            train_conn.commit()
            total_neg += len(tile_inserts)
            print(f"    {len(tile_inserts)} negative tiles, pack: {new_offset/1024/1024:.0f}MB")

    master.close()
    train_conn.close()

    # Write dataset.yaml
    yaml_path = output_dir / "dataset.yaml"
    yaml_content = (
        f"path: {output_dir}\n"
        f"train: images/train\n"
        f"val: images/val\n"
        f"\nnc: 1\nnames: ['ball']\n"
        f"\n# ManifestTrainer config\n"
        f"manifest_db: {train_db_path}\n"
        f"manifest_packs: {packs_out}\n"
        f"manifest_train_games:\n"
    )
    for g in train_games:
        yaml_content += f"  - {g}\n"
    if camera_neg_games:
        for g in camera_neg_games:
            yaml_content += f"  - {g}\n"
    yaml_content += f"manifest_val_games:\n"
    for g in val_games:
        yaml_content += f"  - {g}\n"
    yaml_content += f"manifest_neg_ratio: {neg_ratio}\n"
    yaml_path.write_text(yaml_content)

    elapsed = time.time() - t0
    total_pack_size = sum(f.stat().st_size for f in packs_out.glob("*.pack"))
    db_size = train_db_path.stat().st_size

    print(f"\n=== Training Set Built ({elapsed:.0f}s) ===")
    print(f"  Positives:   {total_pos:,}")
    print(f"  Negatives:   {total_neg:,}")
    print(f"  Total:       {total_pos + total_neg:,}")
    print(f"  Ratio:       1:{total_neg/max(total_pos, 1):.1f}")
    print(f"  Packs:       {total_pack_size/1024/1024/1024:.1f} GB")
    print(f"  Manifest DB: {db_size/1024/1024:.0f} MB")
    print(f"  YAML:        {yaml_path}")
    print(f"\nTransfer {output_dir} to laptop to train.")

    return yaml_path
