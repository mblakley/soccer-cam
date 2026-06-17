"""Evaluation for the ball world-model: peak extraction + center-distance recall.

Two pieces, both coordinate-space agnostic (the caller maps warped-band peaks
back to source pixels with ``field_warp.unwarp_points`` before scoring against
source-coord ground truth):

1. :func:`extract_peaks` — turn a 2-D detector heatmap into a small set of
   candidate ``(x, y, score)`` local maxima (NMS by ``min_distance``), the input
   the world-model's track-before-detect consumes.
2. :func:`evaluate_recall` — the honest full-frame metric used to compare against
   AutoCam: a GT ball counts as recalled if the predicted ball position that
   frame is within ``radius_px`` (R=20px). Reports the same splits as the heatmap
   session's ``hm_fulleval.py``: ``all``, ``veryfar`` (gt cy <= threshold), and
   ``acmissed`` (the frames AutoCam missed — recall > 0 here = beating AutoCam on
   the hard far balls), plus ``false_fire`` (a confident prediction in the wrong
   place).

Baselines to compare on the same metric:
- AutoCam (honest full set): all 0.76, **veryfar 0.74**, acmissed 0 (by def).
- Per-frame argmax of champion-J: veryfar ~0.29, false_fire 76% (the wall the
  world-model is meant to break).

Pure numpy + cv2.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from training.world_model.tbd import TBDResult


def extract_peaks(
    heatmap: np.ndarray,
    top_k: int = 24,
    threshold: float = 0.1,
    min_distance: int = 3,
) -> list[tuple[float, float, float]]:
    """Extract up to ``top_k`` local-maxima peaks from a 2-D heatmap.

    Args:
        heatmap: ``(H, W)`` detector response (e.g. sigmoid heatmap), float.
        top_k: Max peaks to return (highest score first).
        threshold: Minimum response to be a candidate (drops background).
        min_distance: NMS radius — peaks closer than this are suppressed via a
            ``(2*min_distance+1)`` dilation; only true local maxima survive.

    Returns:
        List of ``(x, y, score)`` in heatmap pixel coords, score-descending.
    """
    hm = np.asarray(heatmap, dtype=np.float32)
    if hm.ndim != 2:
        raise ValueError(f"heatmap must be 2-D, got shape {hm.shape}")
    ksize = 2 * int(min_distance) + 1
    dilated = cv2.dilate(hm, np.ones((ksize, ksize), np.uint8))
    mask = (hm >= dilated) & (hm >= threshold)
    ys, xs = np.where(mask)
    if ys.size == 0:
        return []
    scores = hm[ys, xs]
    order = np.argsort(scores)[::-1][:top_k]
    return [(float(xs[i]), float(ys[i]), float(scores[i])) for i in order]


@dataclass
class RecallResult:
    """Center-distance recall, split as in the heatmap session's eval."""

    recall_all: float
    recall_veryfar: float
    recall_acmissed: float
    n_all: int
    n_veryfar: int
    n_acmissed: int
    hits_all: int
    hits_veryfar: int
    hits_acmissed: int
    false_fire: int
    n_frames_with_gt: int

    def summary(self) -> str:
        return (
            f"all {self.recall_all:.3f} ({self.hits_all}/{self.n_all}) | "
            f"veryfar {self.recall_veryfar:.3f} ({self.hits_veryfar}/{self.n_veryfar}) | "
            f"acmissed {self.recall_acmissed:.3f} ({self.hits_acmissed}/{self.n_acmissed}) | "
            f"false_fire {self.false_fire}/{self.n_frames_with_gt}"
        )


def track_to_predictions(result: TBDResult) -> dict[int, tuple[float, float]]:
    """Map a TBD track to ``{frame_idx: (x, y)}`` predictions."""
    return {p.frame_idx: (p.x, p.y) for p in result.points}


def evaluate_recall(
    predictions: dict[int, tuple[float, float]],
    gt: list[tuple[int, float, float]],
    radius_px: float = 20.0,
    far_y_threshold: float = 450.0,
    acmissed_frames: set[int] | None = None,
) -> RecallResult:
    """Center-distance recall of per-frame ball predictions against GT.

    Args:
        predictions: ``{frame_idx: (x, y)}`` predicted ball position per frame
            (e.g. from :func:`track_to_predictions`). A frame absent here means
            no prediction (counts as a miss).
        gt: ground-truth balls as ``(frame_idx, x, y)`` in the same coord space
            (one ball per frame — the single game ball).
        radius_px: hit radius R (default 20, matching AutoCam eval).
        far_y_threshold: GT with ``y <= threshold`` are the ``veryfar`` split.
        acmissed_frames: frames AutoCam missed; recall on this subset is the
            headline "beat AutoCam on the hard far balls".

    Returns:
        A :class:`RecallResult`.
    """
    acmissed_frames = acmissed_frames or set()
    n_all = n_vf = n_ac = 0
    hits_all = hits_vf = hits_ac = 0
    false_fire = 0
    for frame_idx, gx, gy in gt:
        n_all += 1
        is_vf = gy <= far_y_threshold
        is_ac = frame_idx in acmissed_frames
        n_vf += int(is_vf)
        n_ac += int(is_ac)
        pred = predictions.get(frame_idx)
        hit = pred is not None and math.hypot(pred[0] - gx, pred[1] - gy) <= radius_px
        if hit:
            hits_all += 1
            hits_vf += int(is_vf)
            hits_ac += int(is_ac)
        elif pred is not None:
            false_fire += 1

    def _r(h: int, n: int) -> float:
        return h / n if n else 0.0

    return RecallResult(
        recall_all=_r(hits_all, n_all),
        recall_veryfar=_r(hits_vf, n_vf),
        recall_acmissed=_r(hits_ac, n_ac),
        n_all=n_all,
        n_veryfar=n_vf,
        n_acmissed=n_ac,
        hits_all=hits_all,
        hits_veryfar=hits_vf,
        hits_acmissed=hits_ac,
        false_fire=false_fire,
        n_frames_with_gt=n_all,
    )


def evaluate_recall_metric(
    predictions: dict[int, tuple[float, float]],
    gt: list[tuple[int, float, float]],
    geom,
    radius_m: float = 5.0,
) -> tuple[float, float, int]:
    """Perspective-FAIR recall: distance measured in **meters on the field plane**
    (via the homography), not source pixels.

    Source-pixel radii badly flatter the far field: on the fixed 180° panorama a
    far corner is heavily compressed, so 400 px ≈ 13 m there vs ~5 m near the
    touchline (the same px radius is a very different real distance). Meters are
    uniform, so this is the honest viewport-accuracy metric. A few px of detector
    error at a far corner becomes ~10 m on the field — which is why a "within
    400px" far-corner track can still look badly off the ball.

    Args:
        predictions: ``{frame_idx: (x, y)}`` predicted ball position (source px).
        gt: ground-truth ``(frame_idx, x, y)`` in source px.
        geom: a :class:`FieldGeometry` with a **valid** homography.
        radius_m: hit radius in meters (default 5 m ≈ a tight viewport).

    Returns:
        ``(recall, median_error_m, n)`` — fraction within ``radius_m``, the median
        miss distance in meters (a miss/absent frame counts as ``inf``), and the
        GT count.
    """
    if not getattr(geom, "valid", False):
        raise ValueError(
            "evaluate_recall_metric requires a valid (non-neutral) homography"
        )
    hits = 0
    dists: list[float] = []
    for frame_idx, gx, gy in gt:
        pred = predictions.get(frame_idx)
        if pred is None:
            dists.append(float("inf"))
            continue
        gw = geom.image_to_world(np.array([[gx, gy]]))[0]
        pw = geom.image_to_world(np.array([[pred[0], pred[1]]]))[0]
        d = float(math.hypot(pw[0] - gw[0], pw[1] - gw[1]))
        dists.append(d)
        hits += int(d <= radius_m)
    n = len(gt)
    median = float(np.median(dists)) if dists else float("inf")
    return (hits / n if n else 0.0, median, n)
