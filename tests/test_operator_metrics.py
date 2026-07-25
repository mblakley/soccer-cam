"""W1 operator scoreboard (EXP-OP-01): metric unit tests + CLI smoke tests.

Pure synthetic data — the gap-64 event clustering, capture / flip reads and the
framing metrics are checked against hand-computed values, and the scoreboard CLI
runs end-to-end on a tiny synthetic set in tmp_path (including the hard-fail
guard paths, which must exit non-zero per CLAUDE.md rule 8)."""

import json
import math

import numpy as np
import pytest

from training.world_model import operator_metrics as om

# ---------------------------------------------------------------------------
# Event clustering
# ---------------------------------------------------------------------------


def test_cluster_events_gap_boundary():
    # exactly 64 apart = SAME event (f - ev[-1][-1] <= GAP joins, EXP-72 rule)
    assert om.cluster_events([0, 64]) == [[0, 64]]
    # 65 apart = new event
    assert om.cluster_events([0, 65]) == [[0], [65]]
    # chains extend from the LAST frame of the event, input order irrelevant
    assert om.cluster_events([139, 10, 74]) == [[10, 74], [139]]
    assert om.cluster_events([]) == []


def test_segment_series_matches_cluster_events():
    frames = [0, 8, 64, 200, 265, 400]
    assert om.segment_series(frames) == om.cluster_events(frames)
    assert om.segment_series(frames, gap=7) == om.cluster_events(frames, gap=7)


# ---------------------------------------------------------------------------
# Capture stats + pair flip read
# ---------------------------------------------------------------------------


def test_capture_stats_hand_computed():
    deltas = [(0, 100.0), (1, 300.0), (2, 500.0), (3, 700.0)]
    st = om.capture_stats(deltas)
    assert st["n"] == 4
    assert st["capture"][300] == pytest.approx(0.5)  # 100, 300 (<= is captured)
    assert st["capture"][600] == pytest.approx(0.75)
    assert st["median"] == pytest.approx(400.0)
    assert st["p90"] == pytest.approx(np.percentile([100, 300, 500, 700], 90))


def test_capture_stats_empty():
    st = om.capture_stats([])
    assert st["n"] == 0
    assert st["capture"][300] is None and st["capture"][600] is None
    assert st["median"] is None and st["p90"] is None


def test_pair_flip_read_hand_computed():
    cap_a = {0: True, 1: True, 100: True, 200: False, 999: True}
    cap_b = {0: False, 1: True, 100: False, 200: True}
    pf = om.pair_flip_read(cap_a, cap_b)
    assert pf["n_common"] == 4  # 999 is not common
    assert pf["a_only_frames"] == 2  # 0 and 100
    assert pf["a_only_events"] == 2  # 100 - 0 = 100 > 64 -> two events
    assert pf["b_only_frames"] == 1  # 200
    assert pf["b_only_events"] == 1


# ---------------------------------------------------------------------------
# Framing metrics
# ---------------------------------------------------------------------------


def test_pan_velocity_stride_normalized():
    cx = {0: 0.0, 10: 50.0, 20: 150.0}
    # dt from the ACTUAL frame gap: 10 frames @ 20 fps = 0.5 s
    vels = om.pan_velocity(cx, [0, 5, 10, 20], fps=20.0)  # frame 5 has no data
    assert vels == pytest.approx([100.0, 200.0])
    s = om.velocity_summary(vels)
    assert s["n"] == 2
    assert s["median"] == pytest.approx(150.0)
    assert s["p90"] == pytest.approx(np.percentile([100.0, 200.0], 90))
    assert om.velocity_summary([]) == {"n": 0, "median": None, "p90": None}


def test_reversal_rate_triangle_wave():
    # triangle wave, period 60: apexes at f=30, 60, 90 -> exactly 3 reversals;
    # every step is +/-20 px/frame @ 30 fps = |v| 600 px/s
    def tri(f):
        ph = f % 60
        return 20.0 * ph if ph <= 30 else 20.0 * (60 - ph)

    seg = list(range(120))
    cx = {f: tri(f) for f in seg}
    rr = om.reversal_rate(cx, seg, fps=30.0, v_thresh=100.0)
    assert rr["flips"] == 3
    assert rr["minutes"] == pytest.approx(119 / 30 / 60)
    assert rr["rate"] == pytest.approx(3 / (119 / 30 / 60))
    # threshold above the leg speed: no step qualifies -> no flips
    assert om.reversal_rate(cx, seg, fps=30.0, v_thresh=700.0)["flips"] == 0
    # a static series never reverses
    static = dict.fromkeys(seg, 500.0)
    assert om.reversal_rate(static, seg, fps=30.0, v_thresh=0.0)["flips"] == 0
    # single available frame: zero duration -> rate is None
    assert om.reversal_rate({0: 1.0}, [0], fps=30.0, v_thresh=0.0)["rate"] is None


def test_gt_velocity_threshold():
    vels = list(range(1, 101))  # 1..100
    assert om.gt_velocity_threshold(vels) == pytest.approx(np.percentile(vels, 95))
    assert om.gt_velocity_threshold(vels, pct=50) == pytest.approx(50.5)
    with pytest.raises(ValueError):
        om.gt_velocity_threshold([])


def test_hold_fidelity_swing_ratio():
    seg = list(range(10))
    # GT wobbles 100 px (a hold, <= 400); arm drifts 270 px -> ratio 2.7
    gt = {f: 1000.0 + 100.0 * (f % 2) for f in seg}
    arm = {f: 1000.0 + 30.0 * f for f in seg}  # 0..270 -> swing 270
    rows, med = om.hold_fidelity(arm, gt, [seg])
    assert len(rows) == 1
    _frames, gt_swing, arm_swing, ratio = rows[0]
    assert gt_swing == pytest.approx(100.0)
    assert arm_swing == pytest.approx(270.0)
    assert ratio == pytest.approx(2.7)
    assert med == pytest.approx(2.7)
    # GT swing above 400 px: segment does not qualify
    gt_moving = {f: 1000.0 + 100.0 * f for f in seg}
    assert om.hold_fidelity(arm, gt_moving, [seg]) == ([], None)
    # zero GT swing: ratio 0 when the arm also held, inf when it swung
    gt_static = dict.fromkeys(seg, 1000.0)
    rows, med = om.hold_fidelity(dict.fromkeys(seg, 2000.0), gt_static, [seg])
    assert rows[0][3] == 0.0 and med == 0.0
    rows, _med = om.hold_fidelity(arm, gt_static, [seg])
    assert math.isinf(rows[0][3])


# ---------------------------------------------------------------------------
# Split-half null
# ---------------------------------------------------------------------------


def test_split_half_null_deterministic_with_seed():
    events = [[i * 200 + j for j in range(4)] for i in range(6)]
    values = {f: float(i) for i, ev in enumerate(events) for f in ev}

    def metric(frames):
        return float(np.mean([values[f] for f in frames]))

    r1 = om.split_half_null(events, values, metric, reps=50, seed=7)
    r2 = om.split_half_null(events, values, metric, reps=50, seed=7)
    assert r1["deltas"] == r2["deltas"]  # deterministic with the seed
    assert r1["reps_valid"] == 50 and r1["n_events"] == 6
    assert len(set(r1["deltas"])) > 1  # real variation across splits
    # the null is symmetric: the central-95% band straddles 0
    assert r1["band"][0] <= 0.0 <= r1["band"][1]


def test_split_half_null_zero_band_for_identical_halves():
    events = [[i * 200 + j for j in range(4)] for i in range(6)]
    values = {f: 5.0 for ev in events for f in ev}  # identical everywhere

    def metric(frames):
        return float(np.mean([values[f] for f in frames]))

    r = om.split_half_null(events, values, metric, reps=50, seed=3)
    # identical halves -> every delta is 0 -> the band collapses onto 0
    assert r["band"] == (0.0, 0.0)
    assert r["band"][0] <= 0.0 <= r["band"][1]


def test_split_half_null_too_few_events():
    events = [[0, 1, 2]]
    values = {0: 1.0, 1: 2.0, 2: 3.0}
    r = om.split_half_null(events, values, lambda fs: 1.0, reps=10, seed=1)
    assert r["band"] is None
    assert r["n_events"] == 1
    assert "fewer than 2" in r["reason"]
    # events with no covered frames drop out entirely
    r = om.split_half_null([[0], [99], [98]], {0: 1.0}, lambda fs: 1.0, reps=10, seed=1)
    assert r["band"] is None and r["n_events"] == 1


# ---------------------------------------------------------------------------
# Range bands
# ---------------------------------------------------------------------------


def test_band_of_edges():
    # pre-registered: far < 8, mid 8-15, near > 15 — both edges belong to MID
    # (the strict inequalities are far's and near's; see band_of docstring)
    assert om.band_of(7.9) == "far"
    assert om.band_of(8.0) == "mid"
    assert om.band_of(15.0) == "mid"
    assert om.band_of(15.1) == "near"
    assert om.band_of(0.5) == "far"
    assert om.band_of(40.0) == "near"


# ---------------------------------------------------------------------------
# CLI smoke tests (synthetic set in tmp_path)
# ---------------------------------------------------------------------------

LABEL_FRAMES = [0, 8, 16, 24, 100, 108, 900, 908]
LABEL_FX = [1000.0, 1050.0, 1000.0, 1050.0, 2000.0, 2050.0, 3000.0, 3050.0]


def _write_synthetic(tmp_path, campath_len=1000, ac_frames=500, skip_labels=False):
    """Tiny synthetic set dir + game dir + campath: champ cx constant 1100,
    AutoCam x constant 1000 over the first ``ac_frames`` frames."""
    sd = tmp_path / "synth_set"
    sd.mkdir(exist_ok=True)
    rows = [
        {"frame_idx": f, "action": "skip", "fx": None, "fy": None}
        if skip_labels
        else {"frame_idx": f, "action": "view", "fx": fx, "fy": None}
        for f, fx in zip(LABEL_FRAMES, LABEL_FX, strict=True)
    ]
    (sd / "labels.json").write_text(json.dumps(rows))
    kinds = ["?"] * 6 + ["ext-div"] * 2
    (sd / "manifest.json").write_text(
        json.dumps(
            {
                "set": "synth_set",
                "n_frames": len(LABEL_FRAMES),
                "frames": [
                    {"frame_idx": f, "kind": k}
                    for f, k in zip(LABEL_FRAMES, kinds, strict=True)
                ],
            }
        )
    )
    gd = tmp_path / "game"
    gd.mkdir(exist_ok=True)
    (gd / "game.json").write_text(
        json.dumps({"segments": [{"seg": "s0", "global_offset": 0}]})
    )
    with open(gd / "autocam_viewport.jsonl", "w") as fh:
        for f in range(ac_frames):
            fh.write(json.dumps({"seg": "s0", "f": f, "x": 1000.0, "y": 500.0}) + "\n")
    cp = tmp_path / "campath.json"
    cp.write_text(
        json.dumps(
            {
                "schema": "camera_path/1",
                "g_start": 0,
                "src_w": 3840,
                "src_h": 1080,
                "fps": 30.0,
                "frames": [[1100.0, 500.0, 42.0]] * campath_len,
            }
        )
    )
    return sd, gd, cp


def _run_scoreboard(tmp_path, sd, gd, cp, extra=()):
    from training.cli import operator_scoreboard as osb

    out = tmp_path / "report.json"
    osb.main(
        [
            "--set-dir",
            str(sd),
            "--game-dir",
            str(gd),
            "--campath",
            f"champ={cp}",
            "--out",
            str(out),
            *extra,
        ]
    )
    return json.loads(out.read_text())


def test_cli_smoke_report_cells(tmp_path):
    report = _run_scoreboard(tmp_path, *_write_synthetic(tmp_path))
    assert report["arms"] == ["champ", "AC"]
    blk = report["sets"]["synth_set"]
    # no fy and no polygon -> band 'all' only, and the report says so
    assert blk["banding"] == "all-only"
    assert "no field_polygon" in blk["banding_note"]
    assert list(blk["cells"]["ALL"].keys()) == ["all"]
    # champ covers all 8 labels: |1100 - fx| = 100,50,100,50,900,950,1900,1950
    champ = blk["cells"]["ALL"]["all"]["arms"]["champ"]
    assert champ["n"] == 8
    assert champ["capture"]["300"] == pytest.approx(0.5)
    assert champ["capture"]["600"] == pytest.approx(0.5)
    assert champ["median"] == pytest.approx(500.0)
    # AC only covers frames < 500: deltas 0,50,0,50,1000,1050
    ac = blk["cells"]["ALL"]["all"]["arms"]["AC"]
    assert ac["n"] == 6
    assert ac["capture"]["600"] == pytest.approx(4 / 6)
    assert blk["coverage"]["AC"]["n_covered"] == 6
    # ext-div present -> original / ext-div subsets appear with the right ns
    assert blk["cells"]["ext-div"]["all"]["n_labeled"] == 2
    assert blk["cells"]["original"]["all"]["n_labeled"] == 6
    # framing computable: 3 contiguous segments, GT-derived threshold present
    assert blk["framing"]["computable"] is True
    assert blk["framing"]["n_segments"] == 3
    assert blk["framing"]["gt"]["v_thresh"] > 0
    assert "champ" in blk["framing"]["arms"] and "AC" in blk["framing"]["arms"]
    # champ is constant -> zero pan velocity everywhere
    assert blk["framing"]["arms"]["champ"]["velocity"]["median"] == pytest.approx(0.0)
    # pair flips exist per subset
    assert "champ_vs_AC" in blk["pair_flips"]["ALL"]
    # pooled table mirrors the single set
    assert report["pooled"]["ALL"]["all"]["arms"]["champ"]["n"] == 8


def test_cli_short_campath_reports_coverage(tmp_path):
    sd, gd, cp = _write_synthetic(tmp_path, campath_len=500)
    report = _run_scoreboard(tmp_path, sd, gd, cp)
    cov = report["sets"]["synth_set"]["coverage"]["champ"]
    assert cov["short"] is True
    assert cov["n_covered"] == 6  # frames 900, 908 beyond the campath
    assert cov["campath_len"] == 500 and cov["max_label_frame"] == 908
    # the covered n (not the labeled n) is what the cell reports
    champ = report["sets"]["synth_set"]["cells"]["ALL"]["all"]["arms"]["champ"]
    assert champ["n"] == 6
    assert report["sets"]["synth_set"]["cells"]["ALL"]["all"]["n_labeled"] == 8


def test_cli_null_calibration_bands(tmp_path):
    sd, gd, cp = _write_synthetic(tmp_path)
    report = _run_scoreboard(
        tmp_path, sd, gd, cp, extra=["--null-calibration", "--seed", "5"]
    )
    nc = report["null_calibration"]["synth_set"]
    assert nc["arm"] == "champ" and nc["reps"] == 300 and nc["seed"] == 5
    # champ is constant -> every framing metric is 0 on every half-split
    assert nc["pan_velocity_median"]["band"] == [0.0, 0.0]
    assert nc["reversal_rate"]["band"] == [0.0, 0.0]
    assert nc["hold_fidelity_median_ratio"]["band"] == [0.0, 0.0]
    assert nc["pan_velocity_median"]["n_events"] == 3


def test_cli_empty_labels_hard_fails(tmp_path):
    sd, gd, cp = _write_synthetic(tmp_path, skip_labels=True)
    with pytest.raises(SystemExit) as ei:
        _run_scoreboard(tmp_path, sd, gd, cp)
    assert ei.value.code not in (0, None)


def test_cli_missing_autocam_jsonl_hard_fails(tmp_path):
    sd, gd, cp = _write_synthetic(tmp_path)
    (gd / "autocam_viewport.jsonl").unlink()
    with pytest.raises(SystemExit) as ei:
        _run_scoreboard(tmp_path, sd, gd, cp)
    assert ei.value.code not in (0, None)


def test_cli_fixture_mismatch_exits_nonzero(tmp_path):
    # synthetic cells cannot reproduce the EXP-72 numbers -> hard fail
    sd, gd, cp = _write_synthetic(tmp_path)
    with pytest.raises(SystemExit) as ei:
        _run_scoreboard(tmp_path, sd, gd, cp, extra=["--fixture-exp72"])
    assert ei.value.code not in (0, None)
