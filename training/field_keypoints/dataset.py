"""Dataset, placement clustering and venue-held-out split.

The camera is fixed per game, so frames from one game share a field
boundary. To avoid leakage we split by *placement* (team + venue), never by
frame: whole venue clusters go entirely to train, val or test. Same-named
venues across teams stay separate (Flash-"Home" != Heat-"Home"); only
genuinely identical fields (median-polygon IoU > 0.85, same team) merge.

A ``WeightedRandomSampler`` weight of ``1 / cluster_frame_count`` keeps a
high-volume placement (e.g. a test session) from dominating an epoch.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from training.field_keypoints import GATE_THRESHOLD, NUM_KEYPOINTS
from training.field_keypoints.augment import COORD_SCORE_MIN, augment_sample

logger = logging.getLogger(__name__)

IOU_MERGE_THRESHOLD = 0.85
LOW_SCORE_CLUSTER = 0.60  # mean teacher score below this == "hard" (indoor)
_INDOOR_KEYWORDS = ("total sports", "indoor", "dome", "arena")


@dataclass
class Sample:
    jpg: Path
    kpts: np.ndarray  # (10, 2) normalized
    scores: np.ndarray  # (10,)
    mean_score: float
    gate_pass: bool
    team: str
    venue: str
    game_id: str
    cluster: str = ""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_samples(dataset_root: Path) -> list[Sample]:
    """Load every per-frame label JSON under ``dataset_root/labels``."""
    labels_root = dataset_root / "labels"
    frames_root = dataset_root / "frames"
    samples: list[Sample] = []
    for jf in sorted(labels_root.glob("*/*.json")):
        d = json.loads(jf.read_text(encoding="utf-8"))
        jpg = frames_root / jf.parent.name / (jf.stem + ".jpg")
        if not jpg.exists():
            logger.warning("Missing frame for label %s", jf)
            continue
        samples.append(
            Sample(
                jpg=jpg,
                kpts=np.array(d["keypoints_norm"], dtype=np.float32),
                scores=np.array(d["scores"], dtype=np.float32),
                mean_score=float(d["mean_score"]),
                gate_pass=bool(d["gate_pass"]),
                team=d["team"],
                venue=d["venue"],
                game_id=d["game_id"],
            )
        )
    logger.info("Loaded %d samples from %s", len(samples), dataset_root)
    return samples


# ---------------------------------------------------------------------------
# Placement clustering
# ---------------------------------------------------------------------------


def _median_polygon(samples: list[Sample]) -> np.ndarray | None:
    gp = [s.kpts for s in samples if s.gate_pass] or [s.kpts for s in samples]
    if not gp:
        return None
    return np.median(np.stack(gp), axis=0)


def polygon_iou(
    a: np.ndarray, b: np.ndarray, size: tuple[int, int] = (192, 96)
) -> float:
    """Rasterized IoU of two normalized 10-point polygon rings."""
    w, h = size
    ma = np.zeros((h, w), np.uint8)
    mb = np.zeros((h, w), np.uint8)
    cv2.fillPoly(ma, [(a * [w, h]).astype(np.int32)], 1)
    cv2.fillPoly(mb, [(b * [w, h]).astype(np.int32)], 1)
    inter = int(np.logical_and(ma, mb).sum())
    union = int(np.logical_or(ma, mb).sum())
    return inter / union if union else 0.0


def build_clusters(samples: list[Sample]) -> dict[str, dict]:
    """Assign ``sample.cluster`` and return cluster_id -> info.

    Initial clusters are unique ``(team, venue)`` keys; same-team clusters
    whose median polygons overlap (IoU > threshold) are merged.
    """
    keys = sorted({(s.team, s.venue) for s in samples})
    by_key: dict[tuple, list[Sample]] = defaultdict(list)
    for s in samples:
        by_key[(s.team, s.venue)].append(s)
    medians = {k: _median_polygon(by_key[k]) for k in keys}

    # union-find over same-team keys with overlapping median polygons
    parent = {k: k for k in keys}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for i, ki in enumerate(keys):
        for kj in keys[i + 1 :]:
            if ki[0] != kj[0]:  # never merge across teams
                continue
            if medians[ki] is None or medians[kj] is None:
                continue
            if polygon_iou(medians[ki], medians[kj]) > IOU_MERGE_THRESHOLD:
                parent[find(kj)] = find(ki)

    # build cluster ids from representative key
    rep_to_id: dict[tuple, str] = {}
    for k in keys:
        rep = find(k)
        if rep not in rep_to_id:
            team, venue = rep
            venue_slug = venue.lower().replace(" ", "_") or "unknown"
            rep_to_id[rep] = f"{team}__{venue_slug}"

    info: dict[str, dict] = {}
    for s in samples:
        cid = rep_to_id[find((s.team, s.venue))]
        s.cluster = cid
        c = info.setdefault(
            cid,
            {"team": s.team, "venues": set(), "n_frames": 0, "score_sum": 0.0},
        )
        c["venues"].add(s.venue)
        c["n_frames"] += 1
        c["score_sum"] += s.mean_score

    for cid, c in info.items():
        mean = c["score_sum"] / max(c["n_frames"], 1)
        c["mean_score"] = round(mean, 4)
        venues_lower = " ".join(v.lower() for v in c["venues"])
        c["is_low"] = mean < LOW_SCORE_CLUSTER or any(
            kw in venues_lower for kw in _INDOOR_KEYWORDS
        )
        c["venues"] = sorted(c["venues"])
    return info


# ---------------------------------------------------------------------------
# Split (by placement cluster)
# ---------------------------------------------------------------------------


def make_split(cluster_info: dict[str, dict], seed: int) -> dict[str, list[str]]:
    """~80/10/10 split over whole clusters, stratified by (team, is_low).

    Stratifying buckets means val/test pick up both teams and the hard
    indoor placements when the data allows it. Tiny strata (one cluster)
    fall to train.
    """
    rng = np.random.default_rng(seed)
    buckets: dict[tuple, list[str]] = defaultdict(list)
    for cid, c in cluster_info.items():
        buckets[(c["team"], c["is_low"])].append(cid)

    train, val, test = [], [], []
    for _, cids in sorted(buckets.items()):
        cids = list(cids)
        rng.shuffle(cids)
        n = len(cids)
        n_test = 1 if n >= 2 else 0
        n_val = 1 if n >= 3 else 0
        test += cids[:n_test]
        val += cids[n_test : n_test + n_val]
        train += cids[n_test + n_val :]

    for name, cids in (("val", val), ("test", test)):
        teams = {cluster_info[c]["team"] for c in cids}
        if cids and not any(cluster_info[c]["is_low"] for c in cids):
            logger.warning("%s split has no low-score/indoor cluster", name)
        logger.info("%s split: %d clusters, teams=%s", name, len(cids), sorted(teams))
    return {"train": sorted(train), "val": sorted(val), "test": sorted(test)}


def load_or_make_split(
    dataset_root: Path, cluster_info: dict[str, dict], seed: int
) -> dict[str, list[str]]:
    """Load ``splits.json`` if present (never overwrite); else create it."""
    path = dataset_root / "splits.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        logger.info("Loaded existing split from %s", path)
        return {k: data[k] for k in ("train", "val", "test")}

    split = make_split(cluster_info, seed)
    payload = {
        "seed": seed,
        "iou_merge_threshold": IOU_MERGE_THRESHOLD,
        "cluster_info": {
            cid: {k: v for k, v in c.items() if k != "score_sum"}
            for cid, c in cluster_info.items()
        },
        **split,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("Wrote split to %s", path)
    return split


# ---------------------------------------------------------------------------
# Torch dataset
# ---------------------------------------------------------------------------


class FieldKeypointDataset:
    """Map-style dataset of ``image``/``kpts``/``scores``/``coord_valid``.

    ``coord_valid`` masks which of the 10 points contribute to the
    coordinate loss: in-frame after augmentation, teacher score high enough,
    and (frame-level) the teacher would have trusted the whole frame.

    Not a ``torch.utils.data.Dataset`` subclass so this module stays
    importable without torch (the pure clustering/split logic is used by
    tests); torch is imported lazily where tensors are built. ``DataLoader``
    accepts any object with ``__len__``/``__getitem__``.
    """

    def __init__(self, samples: list[Sample], train: bool):
        self.samples = samples
        self.train = train

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, idx: int) -> dict:
        import torch

        s = self.samples[idx]
        bgr = cv2.imread(str(s.jpg))
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        rng = np.random.default_rng()
        img, kpts, scores, in_frame = augment_sample(
            rgb, s.kpts, s.scores, rng, train=self.train
        )
        coord_valid = (
            in_frame & (scores >= COORD_SCORE_MIN) & bool(s.gate_pass)
        ).astype(np.float32)
        return {
            "image": torch.from_numpy(img.transpose(2, 0, 1).copy()),
            "kpts": torch.from_numpy(kpts),
            "scores": torch.from_numpy(scores),
            "coord_valid": torch.from_numpy(coord_valid),
        }


def cluster_weights(samples: list[Sample]):
    """Per-sample weight ``1 / cluster_frame_count`` for WeightedRandomSampler."""
    import torch

    counts: dict[str, int] = defaultdict(int)
    for s in samples:
        counts[s.cluster] += 1
    return torch.tensor([1.0 / counts[s.cluster] for s in samples], dtype=torch.double)


def build_datasets(
    dataset_root: Path, seed: int = 1234
) -> tuple[FieldKeypointDataset, FieldKeypointDataset, FieldKeypointDataset, dict]:
    """Load samples, cluster, split, and return (train, val, test, split)."""
    samples = load_samples(dataset_root)
    if not samples:
        raise RuntimeError(f"No samples under {dataset_root}")
    cluster_info = build_clusters(samples)
    split = load_or_make_split(dataset_root, cluster_info, seed)

    by_cluster: dict[str, list[Sample]] = defaultdict(list)
    for s in samples:
        by_cluster[s.cluster].append(s)

    def subset(names):
        out = []
        for c in names:
            out += by_cluster.get(c, [])
        return out

    train = FieldKeypointDataset(subset(split["train"]), train=True)
    val = FieldKeypointDataset(subset(split["val"]), train=False)
    test = FieldKeypointDataset(subset(split["test"]), train=False)
    logger.info(
        "Datasets: train=%d val=%d test=%d frames", len(train), len(val), len(test)
    )
    return train, val, test, split


# Re-export for convenience.
__all__ = [
    "GATE_THRESHOLD",
    "NUM_KEYPOINTS",
    "Sample",
    "FieldKeypointDataset",
    "build_clusters",
    "build_datasets",
    "cluster_weights",
    "load_or_make_split",
    "load_samples",
    "make_split",
    "polygon_iou",
]
