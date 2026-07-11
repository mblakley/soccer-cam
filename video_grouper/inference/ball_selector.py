"""Learned game-ball SELECTOR: context features + the listwise net, numpy inference.

Which of a frame's K candidates is the game ball, or none? A small shared MLP phi
scores each candidate from CONTEXT features (no appearance, no track history);
mean+max pooling over phi embeddings gives a frame context vector that produces the
"none visible" logit. Softmax over K+1 -> calibrated P(candidate j is the game ball)
and P(no visible ball) — the Viterbi emission (``-log p``) and the per-frame miss
cost (``-log p_none``) consumed by :func:`video_grouper.inference.ball_tracker.rerank`.

Design constraints on the features (each backed by a documented negative result):

- **NO appearance** (learned appearance discrimination collapses held-out).
- **NO track-history kinematics** (exposure bias: at train time the history would be
  the teacher's clean track, at inference the student's own imperfect one). Only
  per-frame + SYMMETRIC-window features — the pipeline is offline, so looking at
  t±1, t±2 is legal and identical at train and inference.
- Score enters as rank/percentile (in-frame and within the game's depth band), not
  raw: the raw sigmoid is saturated and per-frame max-normalization already discards
  cross-frame scale.

Training lives in ``training/models/selector_net.py`` (torch); this module runs the
exported ``selector_net_npz/1`` weights with plain numpy — the product runtime has
no torch. The export CLI parity-checks the two forwards.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from video_grouper.inference.ball_tracker import static_persistence
from video_grouper.inference.world_geometry import FieldGeometry

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

# feature families for knockout ablations
FEATURE_FAMILIES: dict[str, tuple[str, ...]] = {
    "score": ("score", "rank_norm", "pct_frame", "pct_depth"),
    "persistence": ("persistence",),
    "geometry": ("size_ratio", "infield", "depth"),
    "window": ("cont_p1", "cont_m1", "cont_p2", "cont_m2"),
    "frame": ("n_cands", "dens_5m"),
}

_CONT_CAP_M = 50.0  # continuity distances capped here (a gap reads as "no support")
_CONT_CAP_MPF = 6.0  # per-FRAME cap when ``ef`` is given (stride-invariant mode)
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
    ef: list[int] | None = None,
) -> list[np.ndarray]:
    """Per-frame ``(K_t, F)`` float32 feature arrays for ``frames`` (lists of
    :class:`~video_grouper.inference.ball_tracker.Candidate`), in dump order.

    Pass ``ef`` (the dump's GLOBAL frame numbers) to make the window-continuity
    features stride-invariant: distances are normalized to meters-PER-FRAME instead
    of per dump step. Without it, a stride-8 training dump and a stride-4 eval dump
    measure different physical quantities."""
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
        """log-scaled nearest-candidate distance from frame t's candidates to t+off —
        meters per dump step, or meters per FRAME when ``ef`` is given."""
        k = len(world[t])
        u = t + off
        if k == 0:
            return np.zeros(0)
        cap = _CONT_CAP_MPF if ef is not None else _CONT_CAP_M
        if not (0 <= u < n) or len(world[u]) == 0:
            d = np.full(k, cap)
        else:
            diff = world[t][:, None, :] - world[u][None, :, :]
            d = np.sqrt((diff**2).sum(-1)).min(axis=1)
            if ef is not None:
                d = d / max(abs(int(ef[u]) - int(ef[t])), 1)
            d = np.minimum(d, cap)
        return np.log1p(d) / np.log1p(cap)

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
        bands = np.atleast_1d(band_of(expected[t]))
        pct_depth = np.array(
            [
                np.searchsorted(band_scores[b], s) / max(len(band_scores[b]), 1)
                for b, s in zip(bands.tolist(), sc.tolist(), strict=True)
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


def feature_mask(knockouts: list[str]) -> np.ndarray:
    """Boolean keep-mask over FEATURE_NAMES with the given FAMILIES or individual
    FEATURES removed."""
    drop: set[str] = set()
    for k in knockouts:
        if k in FEATURE_FAMILIES:
            drop.update(FEATURE_FAMILIES[k])
        elif k in FEATURE_NAMES:
            drop.add(k)
        else:
            raise KeyError(f"unknown feature family/name: {k!r}")
    return np.array([name not in drop for name in FEATURE_NAMES])


def pack_frames(
    feats_list: list[np.ndarray], top_k: int = 24
) -> tuple[np.ndarray, np.ndarray]:
    """Pad per-frame ``(K_t, F)`` features to ``(N, top_k, F)`` + bool mask ``(N, top_k)``."""
    n, f = len(feats_list), feats_list[0].shape[1] if feats_list else 0
    feats = np.zeros((n, top_k, f), np.float32)
    mask = np.zeros((n, top_k), bool)
    for i, x in enumerate(feats_list):
        k = min(len(x), top_k)
        feats[i, :k] = x[:k]
        mask[i, :k] = True
    return feats, mask


# ---------------------------------------------------------------------------
# Numpy inference for the trained listwise net (selector_net_npz/1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SelectorNet:
    """Exported listwise-selector weights (``selector_net_npz/1``).

    Layer shapes mirror ``training/models/selector_net.py::build_selector_net``:
    phi = Linear(F,H)+ReLU -> Linear(H,H)+ReLU -> Linear(H,E)+ReLU;
    head = Linear(E,1) per candidate; none_head = Linear(2E,1) on [mean, max]
    pooled embeddings; logits / temperature."""

    w0: np.ndarray
    b0: np.ndarray
    w1: np.ndarray
    b1: np.ndarray
    w2: np.ndarray
    b2: np.ndarray
    head_w: np.ndarray
    head_b: np.ndarray
    none_w: np.ndarray
    none_b: np.ndarray
    temperature: float
    keep: np.ndarray  # bool mask over FEATURE_NAMES the net was trained on

    @property
    def n_features(self) -> int:
        return int(self.w0.shape[1])


def load_selector(path) -> SelectorNet:
    """Load a ``selector_net_npz/1`` file (see ``training/cli/export_ball_selector``)."""
    d = np.load(path, allow_pickle=False)
    schema = str(d["schema"]) if "schema" in d else ""
    if schema != "selector_net_npz/1":
        raise ValueError(f"{path}: expected selector_net_npz/1, got {schema!r}")
    # Feature-schema guard: build_features emits columns in FEATURE_NAMES order and
    # `keep` selects from them, so a same-length REORDER of FEATURE_NAMES would
    # silently feed mislabeled features into a model trained on the old order (the
    # module docstring's "features must match exactly" failure). Newer exports
    # embed the feature schema; assert it matches. (A feature COUNT change is
    # already caught downstream when the boolean `keep` index length-mismatches.)
    if "feature_names" in d:
        names = tuple(str(x) for x in d["feature_names"])
        if names != FEATURE_NAMES:
            raise ValueError(
                f"{path}: selector was trained on a different feature schema "
                f"({len(names)} features) than the current FEATURE_NAMES "
                f"({len(FEATURE_NAMES)}); order/set drift -> re-export the selector"
            )
    return SelectorNet(
        w0=d["w0"].astype(np.float32),
        b0=d["b0"].astype(np.float32),
        w1=d["w1"].astype(np.float32),
        b1=d["b1"].astype(np.float32),
        w2=d["w2"].astype(np.float32),
        b2=d["b2"].astype(np.float32),
        head_w=d["head_w"].astype(np.float32),
        head_b=d["head_b"].astype(np.float32),
        none_w=d["none_w"].astype(np.float32),
        none_b=d["none_b"].astype(np.float32),
        temperature=float(d["temperature"]),
        keep=d["keep"].astype(bool),
    )


def predict_probs(net: SelectorNet, feats: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """``(N, K+1)`` calibrated probabilities (last column = none visible).

    Bit-for-bit mirror of the torch forward (masked phi embeddings, mean+max
    pooling, masked candidate logits, temperature, softmax)."""
    feats = np.asarray(feats, np.float32)
    m = np.asarray(mask, bool)
    e = np.maximum(feats @ net.w0.T + net.b0, 0.0)
    e = np.maximum(e @ net.w1.T + net.b1, 0.0)
    e = np.maximum(e @ net.w2.T + net.b2, 0.0)  # (N, K, E)
    e = e * m[..., None]
    cand = (e @ net.head_w.T + net.head_b)[..., 0]  # (N, K)
    cand = np.where(m, cand, -np.inf)
    denom = np.maximum(m.sum(axis=1, keepdims=True).astype(np.float32), 1.0)
    mean = e.sum(axis=1) / denom
    mx = np.where(m[..., None], e, -np.inf).max(axis=1)
    mx = np.where(np.isfinite(mx), mx, 0.0)
    none = np.concatenate([mean, mx], axis=-1) @ net.none_w.T + net.none_b  # (N, 1)
    logits = np.concatenate([cand, none], axis=1) / max(net.temperature, 1e-6)
    # The none logit is always finite, so the row max is finite; exp(-inf) -> 0
    # reproduces torch's masked softmax exactly.
    z = logits - logits.max(axis=1, keepdims=True)
    ez = np.exp(z)
    return ez / np.maximum(ez.sum(axis=1, keepdims=True), 1e-12)
