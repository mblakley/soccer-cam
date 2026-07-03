"""Context features for the learned game-ball SELECTOR (kill test + v1).

Per candidate, per frame — everything computable from an ``eval_detector --dump-cands``
pickle alone. Design constraints (each backed by a documented negative result):

- **NO appearance** (R3/D4: learned appearance discrimination collapses held-out).
- **NO track-history kinematics** (exposure bias: at train time the history would be the
  teacher's clean track, at inference the student's own imperfect one). Only per-frame +
  SYMMETRIC-window features — the pipeline is offline, so looking at t±1, t±2 is legal
  and identical at train and inference.
- Score enters as rank/percentile (in-frame and within the game's depth band), not raw:
  the raw sigmoid is saturated (EXP-DIST-19/22) and per-frame max-normalization already
  discards cross-frame scale.
"""

from __future__ import annotations

import numpy as np

from training.world_model.geometry import FieldGeometry
from training.world_model.reranker import static_persistence

FEATURE_NAMES: tuple[str, ...] = (
    "score",  # raw detector score (kept so the net can learn its own calibration)
    "rank_norm",  # score rank within the frame / (K-1); 0 = top
    "pct_frame",  # score percentile within the frame
    "pct_depth",  # score percentile within the game's depth band (cross-frame!)
    "size_ratio",  # observed / expected diameter at that spot (0 = size unknown)
    "persistence",  # 2 m world-cell occupancy across the dump (static distractors ~1)
    "infield",  # signed px distance to the field polygon / 1000, clipped [-1, 1]
    "depth",  # expected ball diameter px / 20 (small = far)
    "n_cands",  # frame candidate count / K
    "cont_p1",  # nearest-candidate distance (m) in the NEXT dump frame, log-scaled
    "cont_m1",  # same, previous frame
    "cont_p2",  # same, +2 frames
    "cont_m2",  # same, -2 frames
    "dens_5m",  # other candidates within 5 m this frame / K
)

# feature families for knockout ablations (kill-test step 5)
FEATURE_FAMILIES: dict[str, tuple[str, ...]] = {
    "score": ("score", "rank_norm", "pct_frame", "pct_depth"),
    "persistence": ("persistence",),
    "geometry": ("size_ratio", "infield", "depth"),
    "window": ("cont_p1", "cont_m1", "cont_p2", "cont_m2"),
    "frame": ("n_cands", "dens_5m"),
}

_CONT_CAP_M = 50.0  # continuity distances capped here (a gap reads as "no support")
_N_DEPTH_BANDS = 4


def _signed_infield(geom: FieldGeometry, xy: np.ndarray) -> np.ndarray:
    """Signed px distance to the field polygon (positive inside), /1000, clipped."""
    import cv2  # noqa: PLC0415

    if geom.polygon is None:
        return np.zeros(len(xy))
    poly = geom.polygon.reshape(-1, 1, 2).astype(np.float32)
    d = np.array(
        [cv2.pointPolygonTest(poly, (float(x), float(y)), True) for x, y in xy]
    )
    return np.clip(d / 1000.0, -1.0, 1.0)


def build_features(
    frames: list[list],
    geom: FieldGeometry,
    top_k: int = 24,
) -> list[np.ndarray]:
    """Per-frame ``(K_t, F)`` float32 feature arrays for ``frames`` (lists of
    :class:`~training.world_model.tbd.Candidate`), aligned with the dump's ``ef`` order
    (window features assume consecutive entries are consecutive dump frames — the same
    stride at train and inference).
    """
    n = len(frames)
    xy = [np.asarray([(c.x, c.y) for c in cs], float).reshape(-1, 2) for cs in frames]
    world = [geom.image_to_world(p) if len(p) else np.zeros((0, 2)) for p in xy]
    scores = [np.asarray([c.score for c in cs], float) for cs in frames]
    sizes = [
        np.asarray([c.size_px if c.size_px is not None else 0.0 for c in cs], float)
        for cs in frames
    ]
    pers = static_persistence(world, cell_m=2.0)
    expected = [
        geom.expected_ball_diameter_px(p) if len(p) else np.zeros(0) for p in xy
    ]

    # depth bands over the whole dump -> per-band sorted scores for cross-frame percentile
    all_exp = np.concatenate([e for e in expected if len(e)]) if n else np.zeros(0)
    all_sc = np.concatenate([s for s in scores if len(s)]) if n else np.zeros(0)
    edges = (
        np.quantile(all_exp, np.linspace(0, 1, _N_DEPTH_BANDS + 1)[1:-1])
        if len(all_exp)
        else np.zeros(_N_DEPTH_BANDS - 1)
    )
    band_of = lambda e: np.searchsorted(edges, e)  # noqa: E731
    band_scores = [
        np.sort(all_sc[band_of(all_exp) == b]) for b in range(_N_DEPTH_BANDS)
    ]

    def _cont(t: int, off: int) -> np.ndarray:
        """log-scaled nearest-candidate distance (m) from frame t's candidates to t+off."""
        k = len(world[t])
        u = t + off
        if k == 0:
            return np.zeros(0)
        if not (0 <= u < n) or len(world[u]) == 0:
            d = np.full(k, _CONT_CAP_M)
        else:
            diff = world[t][:, None, :] - world[u][None, :, :]
            d = np.minimum(np.sqrt((diff**2).sum(-1)).min(axis=1), _CONT_CAP_M)
        return np.log1p(d) / np.log1p(_CONT_CAP_M)

    out: list[np.ndarray] = []
    for t in range(n):
        k = len(frames[t])
        if k == 0:
            out.append(np.zeros((0, len(FEATURE_NAMES)), np.float32))
            continue
        sc = scores[t]
        order = np.argsort(-sc, kind="stable")
        rank = np.empty(k, int)
        rank[order] = np.arange(k)
        pct_frame = 1.0 - rank / max(k - 1, 1)
        bands = band_of(expected[t])
        pct_depth = np.array(
            [
                np.searchsorted(band_scores[b], s) / max(len(band_scores[b]), 1)
                for b, s in zip(bands, sc, strict=True)
            ]
        )
        exp_d = np.maximum(expected[t], 1e-6)
        size_ratio = np.where(sizes[t] > 0, sizes[t] / exp_d, 0.0)
        if k > 1:
            diff = world[t][:, None, :] - world[t][None, :, :]
            dm = np.sqrt((diff**2).sum(-1))
            np.fill_diagonal(dm, np.inf)
            dens = (dm <= 5.0).sum(axis=1) / top_k
        else:
            dens = np.zeros(k)
        cols = [
            sc,
            rank / max(top_k - 1, 1),
            pct_frame,
            pct_depth,
            np.clip(size_ratio, 0.0, 8.0),
            pers[t],
            _signed_infield(geom, xy[t]),
            expected[t] / 20.0,
            np.full(k, k / top_k),
            _cont(t, +1),
            _cont(t, -1),
            _cont(t, +2),
            _cont(t, -2),
            dens,
        ]
        out.append(np.stack(cols, axis=1).astype(np.float32))
    return out


def feature_mask(knockout_families: list[str]) -> np.ndarray:
    """Boolean keep-mask over FEATURE_NAMES with the given families removed."""
    drop: set[str] = set()
    for fam in knockout_families:
        drop.update(FEATURE_FAMILIES[fam])
    return np.array([name not in drop for name in FEATURE_NAMES])
