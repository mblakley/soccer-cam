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


def _write_candidates(path, stride=4, n=25, schema="candidates/1"):
    frames = {}
    for i in range(n):
        g = i * stride
        ball = [400.0 + 15.0 * i, 700.0, 0.5]
        static = [1200.0, 650.0, 0.9]
        if schema == "candidates/2":  # v2 rows carry size_px
            ball = ball + [9.0]
            static = static + [12.0]
        frames[str(g)] = [ball, static]
    art = {
        "schema": schema,
        "stride": stride,
        "src_w": 1920,
        "src_h": 1080,
        "fps": 20.0,
        "n_frames": n * stride,
        "frames": frames,
    }
    path.write_text(json.dumps(art))


def test_select_accepts_candidates_v2(tmp_path):
    """ball_detect writes candidates/2; the select gate must accept it (was a
    live mismatch: the gate demanded candidates/1 while the parser handled both)."""
    det = tmp_path / "detections.json"
    _write_candidates(det, schema="candidates/2")
    poly = tmp_path / "field.json"
    poly.write_text(json.dumps({"polygon": POLY}))
    net = tmp_path / "sel.npz"
    _write_selector_npz(net, len(FEATURE_NAMES))
    out = tmp_path / "trajectory.json"

    populated = _run_selection(
        str(det), str(poly), str(out), BallSelectStepConfig(select_model_path=str(net))
    )
    assert populated > 0
    assert json.loads(out.read_text())["schema"] == "trajectory/2"


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
    art = json.loads(out.read_text())
    assert art["schema"] == "trajectory/2"
    assert art["g_start"] == 0 and art["fps"] == 20.0
    traj = art["points"]
    assert populated > 0
    assert len(traj) == 24 * 4 + 1  # dense from frame 0 through the last sample
    xs = [p[0] for p in traj if p is not None]
    # the physics stack must follow the moving ball, not the bright static
    on_ball = sum(1 for x in xs if abs(x - 1200.0) > 50.0)
    assert on_ball >= 0.8 * len(xs)
    # W2 seam channels: aligned 1:1 with points, sane values
    state, conf, disp = art["state"], art["conf"], art["disp"]
    assert len(state) == len(conf) == len(disp) == len(traj)
    assert set(state) <= {"T", "C", "M"}
    assert any(s == "T" for s in state)
    for s, c, p in zip(state, conf, traj, strict=True):
        assert 0.0 <= c <= 1.0
        assert (p is None) == (s == "M")  # M exactly where there is no point
        if s != "T":
            assert c == 0.0
    # dispersion: the two candidates sit ~802 px apart at frame 0 -> RMS is half
    d0 = float(np.hypot(1200.0 - 400.0, 650.0 - 700.0)) / 2.0
    assert disp[0] == pytest.approx(d0, abs=0.5)


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


def test_select_depr_hold_requires_src_dims(tmp_path):
    """The depression-conditioned far-hold (dcB, default-on) hard-fails on an
    artifact without src_w/src_h — never a silent fallback."""
    det, poly, net = tmp_path / "d.json", tmp_path / "p.json", tmp_path / "n.npz"
    _write_selector_npz(net, len(FEATURE_NAMES))
    poly.write_text(json.dumps({"polygon": POLY}))
    _write_candidates(det)
    art = json.loads(det.read_text())
    del art["src_w"], art["src_h"]
    det.write_text(json.dumps(art))
    cfg = BallSelectStepConfig(select_model_path=str(net))
    with pytest.raises(RuntimeError, match="src_w/src_h"):
        _run_selection(str(det), str(poly), str(tmp_path / "t.json"), cfg)


def test_select_depr_hold_disabled_accepts_missing_dims(tmp_path):
    """near_deg <= far_deg disables the hold: legacy artifacts without src dims
    still select (flat select_pnone_scale, the pre-dcB behavior)."""
    det, poly, net = tmp_path / "d.json", tmp_path / "p.json", tmp_path / "n.npz"
    _write_selector_npz(net, len(FEATURE_NAMES))
    poly.write_text(json.dumps({"polygon": POLY}))
    _write_candidates(det)
    art = json.loads(det.read_text())
    del art["src_w"], art["src_h"]
    det.write_text(json.dumps(art))
    cfg = BallSelectStepConfig(
        select_model_path=str(net), select_pnone_depr_near_deg=0.0
    )
    populated = _run_selection(str(det), str(poly), str(tmp_path / "t.json"), cfg)
    assert populated > 0


def test_select_depr_hold_runs_by_default(tmp_path):
    """dcB default-on: a candidates/2 artifact with src dims selects end-to-end
    through the depression-conditioned miss-cost path."""
    det, poly, net = tmp_path / "d.json", tmp_path / "p.json", tmp_path / "n.npz"
    _write_selector_npz(net, len(FEATURE_NAMES))
    poly.write_text(json.dumps({"polygon": POLY}))
    _write_candidates(det, schema="candidates/2")
    cfg = BallSelectStepConfig(select_model_path=str(net))
    assert cfg.select_pnone_far_scale == 2.0  # dcB adopted defaults
    assert cfg.select_pnone_depr_far_deg == 7.0
    assert cfg.select_pnone_depr_near_deg == 16.0
    populated = _run_selection(str(det), str(poly), str(tmp_path / "o.json"), cfg)
    assert populated > 0
