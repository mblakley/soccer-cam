"""ball_select step: candidates/1 + polygon -> dense trajectory.json."""

from __future__ import annotations

import json

import numpy as np
import pytest

from video_grouper.inference.ball_selector import FEATURE_NAMES
from video_grouper.pipeline import _STEP_REGISTRY  # noqa: PLC2701 (test-only)
from video_grouper.pipeline.steps.ball_select import (
    BallSelectStepConfig,
    _run_selection,
)

POLY = [
    [100.0, 1000.0],
    [500.0, 1010.0],
    [960.0, 1015.0],
    [1420.0, 1010.0],
    [1820.0, 1000.0],
    [1600.0, 300.0],
    [1280.0, 295.0],
    [960.0, 290.0],
    [640.0, 295.0],
    [320.0, 300.0],
]


def _write_selector_npz(path, n_features):
    rng = np.random.default_rng(0)
    hidden, emb = 4, 2
    np.savez(
        path,
        schema="selector_net_npz/1",
        w0=rng.normal(scale=0.3, size=(hidden, n_features)).astype(np.float32),
        b0=np.zeros(hidden, np.float32),
        w1=rng.normal(scale=0.3, size=(hidden, hidden)).astype(np.float32),
        b1=np.zeros(hidden, np.float32),
        w2=rng.normal(scale=0.3, size=(emb, hidden)).astype(np.float32),
        b2=np.zeros(emb, np.float32),
        head_w=rng.normal(scale=0.3, size=(1, emb)).astype(np.float32),
        head_b=np.zeros(1, np.float32),
        none_w=rng.normal(scale=0.3, size=(1, 2 * emb)).astype(np.float32),
        none_b=np.full(1, -2.0, np.float32),  # none stays unlikely
        temperature=np.float32(1.0),
        keep=np.ones(len(FEATURE_NAMES), bool),
    )


def _write_candidates(path, stride=4, n=25):
    frames = {}
    for i in range(n):
        g = i * stride
        ball = [400.0 + 15.0 * i, 700.0, 0.5]
        static = [1200.0, 650.0, 0.9]
        frames[str(g)] = [ball, static]
    art = {
        "schema": "candidates/1",
        "stride": stride,
        "src_w": 1920,
        "src_h": 1080,
        "fps": 20.0,
        "n_frames": n * stride,
        "frames": frames,
    }
    path.write_text(json.dumps(art))


def test_select_writes_dense_trajectory(tmp_path):
    det = tmp_path / "detections.json"
    _write_candidates(det)
    poly = tmp_path / "field.json"
    poly.write_text(json.dumps({"polygon": POLY}))
    net = tmp_path / "sel.npz"
    _write_selector_npz(net, len(FEATURE_NAMES))
    out = tmp_path / "trajectory.json"

    cfg = BallSelectStepConfig(select_model_path=str(net))
    populated = _run_selection(str(det), str(poly), str(out), cfg)
    traj = json.loads(out.read_text())
    assert populated > 0
    assert len(traj) == 24 * 4 + 1  # dense from frame 0 through the last sample
    xs = [p[0] for p in traj if p is not None]
    # the physics stack must follow the moving ball, not the bright static
    on_ball = sum(1 for x in xs if abs(x - 1200.0) > 50.0)
    assert on_ball >= 0.8 * len(xs)


def test_select_rejects_wrong_schema(tmp_path):
    det = tmp_path / "detections.json"
    det.write_text(json.dumps({"schema": "nope", "frames": {}}))
    poly = tmp_path / "field.json"
    poly.write_text(json.dumps({"polygon": POLY}))
    cfg = BallSelectStepConfig(select_model_path="x.npz")
    with pytest.raises(RuntimeError, match="candidates/1"):
        _run_selection(str(det), str(poly), str(tmp_path / "t.json"), cfg)


def test_select_requires_valid_homography(tmp_path):
    det = tmp_path / "detections.json"
    _write_candidates(det)
    poly = tmp_path / "field.json"
    poly.write_text(json.dumps({"polygon": [[0, 0], [10, 0], [10, 10], [0, 10]]}))
    net = tmp_path / "sel.npz"
    _write_selector_npz(net, len(FEATURE_NAMES))
    cfg = BallSelectStepConfig(select_model_path=str(net))
    with pytest.raises(RuntimeError, match="homography"):
        _run_selection(str(det), str(poly), str(tmp_path / "t.json"), cfg)


def test_step_registered():
    import video_grouper.pipeline.register_steps  # noqa: F401

    assert "ball_select" in _STEP_REGISTRY
    assert "track" not in _STEP_REGISTRY
