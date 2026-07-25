"""plan_camera step: trajectory in, camera_path/1 artifact out (dumb-renderer split)."""

import json

import numpy as np

from video_grouper.pipeline.steps.plan_camera import (
    PlanCameraStepConfig,
    _depth01,
    _plan,
)


def test_plan_writes_camera_path_artifact(tmp_path):
    traj = [[1000.0 + 10.0 * t, 900.0] for t in range(200)] + [None] * 20
    tp = tmp_path / "trajectory.json"
    tp.write_text(json.dumps(traj))
    out = tmp_path / "camera_path.json"
    n = _plan(str(tp), None, str(out), 7680, 2160, 20.0, PlanCameraStepConfig())
    assert n == 220
    art = json.loads(out.read_text())
    assert art["schema"] == "camera_path/1"
    assert art["src_w"] == 7680 and len(art["frames"]) == 220
    cx, cy, hfov = art["frames"][100]
    assert 0 < cx < 7680 and 0 < cy < 2160 and 20 < hfov < 90


def test_plan_accepts_all_trajectory_schemas_identically(tmp_path):
    """Legacy bare list, trajectory/1 and trajectory/2 with the same points must
    plan the same camera path (default config: the FSM is off, states/disp are
    carried but ignored)."""
    pts = [[1000.0 + 10.0 * t, 900.0] for t in range(150)] + [None] * 10
    n = len(pts)
    variants = {
        "bare": pts,
        "v1": {"schema": "trajectory/1", "g_start": 0, "fps": 20.0, "points": pts},
        "v2": {
            "schema": "trajectory/2",
            "g_start": 0,
            "fps": 20.0,
            "points": pts,
            "state": ["T" if p is not None else "M" for p in pts],
            "conf": [1.0 if p is not None else 0.0 for p in pts],
            "disp": [50.0] * n,  # below hold_dispersion_px: must be ignored
        },
    }
    arts = {}
    for name, payload in variants.items():
        tp = tmp_path / f"{name}.trajectory.json"
        tp.write_text(json.dumps(payload))
        out = tmp_path / f"{name}.camera_path.json"
        got = _plan(str(tp), None, str(out), 7680, 2160, 20.0, PlanCameraStepConfig())
        assert got == n
        arts[name] = json.loads(out.read_text())
    assert arts["bare"]["frames"] == arts["v1"]["frames"] == arts["v2"]["frames"]


def test_depth01_far_to_near():
    near_x = np.linspace(100.0, 1900.0, 5)
    far_x = np.linspace(1600.0, 400.0, 5)
    poly = np.concatenate(
        [
            np.column_stack([near_x, np.full(5, 1000.0)]),
            np.column_stack([far_x, np.full(5, 200.0)]),
        ]
    )
    d = _depth01([(500.0, 200.0), (500.0, 1000.0), None], poly)
    assert d[0] == 0.0 and d[1] == 1.0 and d[2] is None


def test_step_registered():
    from video_grouper.pipeline import _STEP_REGISTRY, register_steps  # noqa: F401

    assert "plan_camera" in _STEP_REGISTRY
