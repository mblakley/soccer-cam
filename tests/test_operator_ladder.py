"""W1 oracle ladder (EXP-OP-01): unit tests for the pure oracle transforms and
CLI end-to-end runs of run-c (freeze-pan) and run-d (lookahead pricing) on
synthetic artifacts in tmp_path. run-a / run-b need a candidate dump + a valid
field geometry and are exercised on banked data, not here. Hard-fail guard
paths must exit non-zero (CLAUDE.md rule 8)."""

import json
import math

import numpy as np
import pytest

from training.cli import operator_ladder as ol

# ---------------------------------------------------------------------------
# Pure transforms
# ---------------------------------------------------------------------------


def test_shift_trajectory_semantics():
    pts = [(0.0, 0.0), None, (2.0, 0.0), (3.0, 0.0)]
    assert ol.shift_trajectory(pts, 0) == pts
    # input[t] = points[min(t + shift, N-1)]; nulls stay nulls; tail clamps
    assert ol.shift_trajectory(pts, 1) == [None, (2.0, 0.0), (3.0, 0.0), (3.0, 0.0)]
    assert ol.shift_trajectory(pts, 10) == [(3.0, 0.0)] * 4
    assert ol.shift_trajectory([], 3) == []


def test_zero_phase_ema_constant_and_smoothing():
    const = [5.0] * 50
    assert ol.zero_phase_ema(const, 0.08) == pytest.approx(const)
    noisy = [math.sin(i / 5.0) * 100.0 + (25.0 if i % 2 else -25.0) for i in range(200)]
    smooth = ol.zero_phase_ema(noisy, 0.08)

    def tv(xs):
        return sum(abs(b - a) for a, b in zip(xs, xs[1:], strict=False))

    assert tv(smooth) < tv(noisy)  # zero-phase smoothing reduces total variation


def test_freeze_campath_spans_and_guard():
    frames = [[float(i), 100.0 + i, 47.0 + 0.01 * i] for i in range(100)]
    frozen = ol.freeze_campath(frames, [[10, 14, 18, 22]])
    for f in range(10, 23):  # every frame in [first, last], labeled or not
        assert frozen[f][0] == 10.0 and frozen[f][1] == 110.0
        assert frozen[f][2] == frames[f][2]  # hfov untouched
    assert frozen[9] == frames[9] and frozen[23] == frames[23]
    assert frames[15][0] == 15.0  # input not mutated
    with pytest.raises(SystemExit):
        ol.freeze_campath(frames, [[90, 120]])  # span past the campath end


# ---------------------------------------------------------------------------
# trajectory/2 artifacts (W2 seam) + hold-knob parsing
# ---------------------------------------------------------------------------


def test_save_load_trajectory_v2_roundtrip(tmp_path):
    traj = [(100.0, 50.0), None, (120.0, 55.0)]
    state = ["T", "M", "C"]
    conf = [0.91234, 0.0, 0.0]
    disp = [40.26, None, 12.0]
    p = tmp_path / "g.trajectory.json"
    ol.save_trajectory(p, traj, g_start=8, fps=20.0, state=state, conf=conf, disp=disp)
    raw = json.loads(p.read_text())
    assert raw["schema"] == "trajectory/2"
    art = ol.load_trajectory(p)
    assert art["g_start"] == 8 and art["fps"] == 20.0
    assert art["points"] == [(100.0, 50.0), None, (120.0, 55.0)]
    assert art["state"] == state
    assert art["conf"] == [0.9123, 0.0, 0.0]  # rounded at save
    assert art["disp"] == [40.3, None, 12.0]


def test_save_trajectory_v1_without_state_and_neutral_load(tmp_path):
    traj = [(100.0, 50.0), None, (120.0, 55.0)]
    p = tmp_path / "g.trajectory.json"
    ol.save_trajectory(p, traj, g_start=0, fps=20.0)
    assert json.loads(p.read_text())["schema"] == "trajectory/1"
    art = ol.load_trajectory(p)  # v1 read -> neutral all-'T' channels
    assert art["state"] == ["T", "M", "T"]
    assert art["conf"] == [1.0, 0.0, 1.0]
    assert art["disp"] is None


def test_save_trajectory_misaligned_channels_hard_fail(tmp_path):
    p = tmp_path / "g.trajectory.json"
    with pytest.raises(SystemExit):
        ol.save_trajectory(
            p, [(1.0, 2.0), (3.0, 4.0)], g_start=0, fps=20.0, state=["T"]
        )


def test_load_trajectory_rejects_bare_list(tmp_path):
    p = tmp_path / "bare.trajectory.json"
    p.write_text(json.dumps([[1.0, 2.0], [3.0, 4.0]]))
    with pytest.raises(SystemExit):
        ol.load_trajectory(p)


def test_planner_config_from_args_knobs():
    import argparse

    from video_grouper.inference.camera_planner import PlannerConfig

    ns = argparse.Namespace(enable_hold=False, hold_knob=[])
    assert ol._planner_config_from_args(ns) is None  # defaults untouched
    ns = argparse.Namespace(
        enable_hold=True,
        hold_knob=["hold_entry_frames=30", "hold_dispersion_px=220.5"],
    )
    cfg = ol._planner_config_from_args(ns)
    assert cfg.enable_hold is True
    assert cfg.hold_entry_frames == 30  # int field stays int
    assert cfg.hold_dispersion_px == 220.5
    assert cfg.hold_exit_frames == PlannerConfig().hold_exit_frames  # untouched
    for bad in (["nope=1"], ["hold_entry_frames=abc"], ["hold_entry_frames"]):
        with pytest.raises(SystemExit):
            ol._planner_config_from_args(
                argparse.Namespace(enable_hold=True, hold_knob=bad)
            )


# ---------------------------------------------------------------------------
# CLI end-to-end (synthetic artifacts in tmp_path)
# ---------------------------------------------------------------------------

POLYGON = [
    [0, 900],
    [960, 900],
    [1920, 900],
    [2880, 900],
    [3840, 900],
    [3840, 200],
    [2880, 200],
    [1920, 200],
    [960, 200],
    [0, 200],
]


def _write_campath(path, frames, g_start=0):
    path.write_text(
        json.dumps(
            {
                "schema": "camera_path/1",
                "g_start": g_start,
                "src_w": 3840,
                "src_h": 1080,
                "fps": 20.0,
                "frames": frames,
            }
        )
    )


def _write_game(tmp_path):
    gd = tmp_path / "game"
    gd.mkdir(exist_ok=True)
    (gd / "game.json").write_text(
        json.dumps(
            {
                "segments": [{"seg": "s0", "global_offset": 0, "w": 3840, "h": 1080}],
                "fps": 20.0,
                "field_polygon": POLYGON,
            }
        )
    )
    return gd


def test_run_c_freezes_amended_clusters_only(tmp_path):
    cp = tmp_path / "g.campath.json"
    frames = [[float(i), 100.0 + i, 47.0] for i in range(300)]
    _write_campath(cp, frames)
    sd = tmp_path / "set"
    sd.mkdir()
    rows = (
        # qualifying cluster: 4 frames at gap 8, GT fx swing 8 < 200
        [
            {"frame_idx": f, "action": "view", "fx": 1000.0 + (f % 16), "fy": None}
            for f in (50, 58, 66, 74)
        ]
        # excluded: swing 960 >= 200
        + [
            {
                "frame_idx": f,
                "action": "view",
                "fx": 1000.0 + 40.0 * (f - 120),
                "fy": None,
            }
            for f in (120, 128, 136, 144)
        ]
        # excluded: singleton (n < 4)
        + [{"frame_idx": 250, "action": "view", "fx": 1500.0, "fy": None}]
    )
    (sd / "labels.json").write_text(json.dumps(rows))
    out = tmp_path / "out"
    ol.main(
        ["run-c", "--campath", str(cp), "--set-dir", str(sd), "--out-dir", str(out)]
    )
    art = json.loads((out / "g.freeze.campath.json").read_text())
    assert art["schema"] == "camera_path/1" and art["g_start"] == 0
    fz = art["frames"]
    assert len(fz) == 300
    for f in range(50, 75):  # frozen at the span-first values, hfov untouched
        assert fz[f][0] == 50.0 and fz[f][1] == 150.0
        assert fz[f][2] == pytest.approx(47.0)
    for f in [*range(0, 50), *range(75, 300)]:  # untouched outside the span
        assert fz[f][0] == pytest.approx(float(f))
        assert fz[f][1] == pytest.approx(100.0 + f)


def test_run_c_no_qualifying_cluster_hard_fails(tmp_path):
    cp = tmp_path / "g.campath.json"
    _write_campath(cp, [[0.0, 0.0, 47.0]] * 100)
    sd = tmp_path / "set"
    sd.mkdir()
    (sd / "labels.json").write_text(
        json.dumps([{"frame_idx": 10, "action": "view", "fx": 100.0, "fy": None}])
    )
    with pytest.raises(SystemExit) as ei:
        ol.main(
            [
                "run-c",
                "--campath",
                str(cp),
                "--set-dir",
                str(sd),
                "--out-dir",
                str(tmp_path / "out"),
            ]
        )
    assert ei.value.code not in (0, None)


def test_run_d_lead_shift_and_zero_phase(tmp_path):
    gd = _write_game(tmp_path)
    pts = [None if 60 <= i < 66 else [1000.0 + 10.0 * i, 500.0 + i] for i in range(120)]
    tp = tmp_path / "g.trajectory.json"
    tp.write_text(
        json.dumps({"schema": "trajectory/1", "g_start": 0, "fps": 20.0, "points": pts})
    )
    camf = [
        [1000.0 + 10.0 * i + (30.0 if i % 2 else -30.0), 500.0, 47.0]
        for i in range(120)
    ]
    cp = tmp_path / "g.campath.json"
    _write_campath(cp, camf)
    out = tmp_path / "out"
    ol.main(
        [
            "run-d",
            "--trajectory",
            str(tp),
            "--game-dir",
            str(gd),
            "--out-dir",
            str(out),
            "--lead-s",
            "0.5",
            "2",
            "--campath",
            str(cp),
        ]
    )
    # (i) each lead artifact == plan_camera on the round(lead*fps)-shifted input
    from training.cli.plan_camera_path import depth_from_polygon
    from video_grouper.inference.camera_planner import plan_camera

    pts_t = [None if p is None else (p[0], p[1]) for p in pts]
    for lead in (0.5, 2.0):
        shifted = ol.shift_trajectory(pts_t, round(lead * 20.0))
        want = plan_camera(
            shifted,
            src_w=3840,
            src_h=1080,
            depth01=depth_from_polygon(shifted, np.asarray(POLYGON, float)),
        )
        art = json.loads((out / f"g.lead{lead:g}.campath.json").read_text())
        assert art["schema"] == "camera_path/1" and art["g_start"] == 0
        assert art["frames"] == [
            [round(cx, 1), round(cy, 1), round(h, 2)] for cx, cy, h in want
        ]
    a = json.loads((out / "g.lead0.5.campath.json").read_text())["frames"]
    b = json.loads((out / "g.lead2.campath.json").read_text())["frames"]
    assert a != b  # different leads genuinely shift the planner's input
    # (ii) zero-phase output: cx total variation drops, hfov untouched
    zp = json.loads((out / "g.zerophase.campath.json").read_text())["frames"]
    assert len(zp) == 120

    def tv(xs):
        return sum(abs(y - x) for x, y in zip(xs, xs[1:], strict=False))

    assert tv([f[0] for f in zp]) < tv([f[0] for f in camf])
    assert all(f[2] == pytest.approx(47.0) for f in zp)


def test_run_d_missing_or_short_inputs_hard_fail(tmp_path):
    gd = _write_game(tmp_path)
    out = str(tmp_path / "out")
    with pytest.raises(SystemExit) as ei:
        ol.main(
            [
                "run-d",
                "--trajectory",
                str(tmp_path / "nope.trajectory.json"),
                "--game-dir",
                str(gd),
                "--out-dir",
                out,
            ]
        )
    assert ei.value.code not in (0, None)
    short = tmp_path / "short.trajectory.json"
    short.write_text(
        json.dumps(
            {
                "schema": "trajectory/1",
                "g_start": 0,
                "fps": 20.0,
                "points": [[1.0, 2.0]],
            }
        )
    )
    with pytest.raises(SystemExit) as ei:
        ol.main(
            [
                "run-d",
                "--trajectory",
                str(short),
                "--game-dir",
                str(gd),
                "--out-dir",
                out,
            ]
        )
    assert ei.value.code not in (0, None)
    # a legacy pickle campath (no metadata) hard-fails the zero-phase arm
    traj = tmp_path / "ok.trajectory.json"
    traj.write_text(
        json.dumps(
            {
                "schema": "trajectory/1",
                "g_start": 0,
                "fps": 20.0,
                "points": [[1.0, 2.0], [3.0, 4.0]],
            }
        )
    )
    pkl = tmp_path / "legacy.pkl"
    pkl.write_bytes(b"\x80\x04not-json")
    with pytest.raises(SystemExit) as ei:
        ol.main(
            [
                "run-d",
                "--trajectory",
                str(traj),
                "--game-dir",
                str(gd),
                "--out-dir",
                out,
                "--lead-s",
                "1",
                "--campath",
                str(pkl),
            ]
        )
    assert ei.value.code not in (0, None)


def test_run_a_missing_dump_hard_fails(tmp_path):
    gd = _write_game(tmp_path)
    with pytest.raises(SystemExit) as ei:
        ol.main(
            [
                "run-a",
                "--fullgame-dir",
                str(tmp_path / "nodump"),
                "--net",
                str(tmp_path / "no.pt"),
                "--game-dir",
                str(gd),
                "--out-dir",
                str(tmp_path / "out"),
            ]
        )
    assert ei.value.code not in (0, None)


def test_run_b_missing_labels_hard_fails(tmp_path):
    gd = _write_game(tmp_path)
    with pytest.raises(SystemExit) as ei:
        ol.main(
            [
                "run-b",
                "--ball-labels",
                str(tmp_path / "no.jsonl"),
                "--game-dir",
                str(gd),
                "--out-dir",
                str(tmp_path / "out"),
            ]
        )
    assert ei.value.code not in (0, None)
