"""Tests for world-model evaluation: peak extraction + recall + end-to-end lift."""

from __future__ import annotations

import numpy as np

from training.world_model.eval import (
    evaluate_recall,
    extract_peaks,
    track_to_predictions,
)
from training.world_model.geometry import build_field_geometry
from training.world_model.tbd import Candidate, run_tbd

NEUTRAL = build_field_geometry(None)


def _add_blob(hm: np.ndarray, cx: int, cy: int, amp: float, sigma: float = 3.0) -> None:
    h, w = hm.shape
    yy, xx = np.mgrid[0:h, 0:w]
    blob = amp * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma**2))
    np.maximum(hm, blob, out=hm)


def test_extract_peaks_finds_blobs_in_score_order():
    hm = np.zeros((200, 300), dtype=np.float32)
    _add_blob(hm, cx=50, cy=80, amp=0.5)
    _add_blob(hm, cx=220, cy=120, amp=0.9)
    peaks = extract_peaks(hm, top_k=10, threshold=0.1)
    assert len(peaks) == 2
    # Brightest first.
    assert peaks[0][:2] == (220.0, 120.0)
    assert peaks[1][:2] == (50.0, 80.0)
    assert peaks[0][2] > peaks[1][2]


def test_extract_peaks_threshold_and_topk():
    hm = np.zeros((100, 100), dtype=np.float32)
    _add_blob(hm, 30, 30, 0.2)
    _add_blob(hm, 70, 70, 0.05)  # below threshold
    assert len(extract_peaks(hm, threshold=0.1)) == 1
    # top_k cap.
    hm2 = np.zeros((100, 200), dtype=np.float32)
    for i, cx in enumerate(range(20, 180, 20)):
        _add_blob(hm2, cx, 50, 0.2 + 0.01 * i)
    assert len(extract_peaks(hm2, top_k=3, threshold=0.1)) == 3


def test_evaluate_recall_splits():
    gt = [(0, 100.0, 100.0), (1, 110.0, 100.0), (2, 100.0, 800.0)]
    # frame 0 hit, frame 1 wrong (false fire), frame 2 (near) hit.
    preds = {0: (105.0, 100.0), 1: (400.0, 100.0), 2: (100.0, 795.0)}
    res = evaluate_recall(
        preds, gt, radius_px=20.0, far_y_threshold=450.0, acmissed_frames={0}
    )
    assert res.n_all == 3 and res.hits_all == 2
    assert abs(res.recall_all - 2 / 3) < 1e-9
    # veryfar = frames 0,1 (y<=450); only frame 0 hit.
    assert res.n_veryfar == 2 and res.hits_veryfar == 1
    # acmissed = {0}, hit.
    assert res.n_acmissed == 1 and res.recall_acmissed == 1.0
    assert res.false_fire == 1


def test_world_model_beats_per_frame_argmax_on_distractor_sequence():
    """The crux: a brighter distractor beats the ball per-frame, but TBD wins.

    Each frame has a bright distractor peak (argmax picks it) and a dimmer true
    ball on a smooth path. The distractor jumps > teleport each frame so it can't
    form a track; the world-model follows the ball.
    """
    h, w = 300, 900
    frames_cands: list[list[Candidate]] = []
    gt: list[tuple[int, float, float]] = []
    argmax_preds: dict[int, tuple[float, float]] = {}
    for t in range(10):
        hm = np.zeros((h, w), dtype=np.float32)
        true_cx, true_cy = 60 + 18 * t, 150
        dist_cx = 120 if t % 2 == 0 else 780  # 660px jump => > teleport
        _add_blob(hm, true_cx, true_cy, amp=0.5)
        _add_blob(hm, dist_cx, 40, amp=0.9)
        peaks = extract_peaks(hm, top_k=10, threshold=0.1)
        frames_cands.append([Candidate(x, y, s) for (x, y, s) in peaks])
        gt.append((t, float(true_cx), float(true_cy)))
        argmax_preds[t] = peaks[0][:2]  # brightest = distractor

    # Per-frame argmax tracks the bright distractor -> recall collapses.
    argmax_res = evaluate_recall(argmax_preds, gt, radius_px=20.0)
    assert argmax_res.recall_all < 0.2

    # World-model track-before-detect follows the smooth ball -> high recall.
    tbd_res = run_tbd(frames_cands, NEUTRAL)
    wm_res = evaluate_recall(track_to_predictions(tbd_res), gt, radius_px=20.0)
    assert wm_res.recall_all > 0.8
    assert wm_res.recall_all > argmax_res.recall_all + 0.6
