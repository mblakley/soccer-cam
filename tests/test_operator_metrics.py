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
# Dual rule (referee v3 @ ac1f42c port)
# ---------------------------------------------------------------------------


def test_dual_rule_read_sign_test_hand_computed():
    # 5 a-only single-frame events, no b-only: p_sign = 2*C(5,0)/2^5 = 0.0625
    cap_a = dict.fromkeys((0, 100, 200, 300, 400), True)
    cap_b = dict.fromkeys((0, 100, 200, 300, 400), False)
    r = om.dual_rule_read(cap_a, cap_b)
    assert r["ea"] == 5 and r["eb"] == 0
    assert r["p_sign"] == pytest.approx(0.0625)
    assert r["d_obs"] == pytest.approx(1.0)  # every common frame flips to a
    assert r["direction"] == "a"
    # 6v0 crosses 0.05: p_sign = 2*C(6,0)/2^6 = 0.03125 -> decisive by sign
    cap_a6 = dict.fromkeys((0, 100, 200, 300, 400, 500), True)
    cap_b6 = dict.fromkeys(cap_a6, False)
    r6 = om.dual_rule_read(cap_a6, cap_b6)
    assert r6["p_sign"] == pytest.approx(0.03125)
    assert r6["decisive"] is True and r6["direction"] == "a"


def test_dual_rule_read_all_zero_contribs():
    # identical arms: no flips anywhere -> both tests inert (p = pm = 1.0)
    caps = {0: True, 100: False, 200: True}
    r = om.dual_rule_read(caps, dict(caps))
    assert r["ea"] == 0 and r["eb"] == 0
    assert r["p_sign"] == 1.0 and r["p_mag"] == 1.0
    assert r["d_obs"] == 0.0
    assert r["decisive"] is False and r["direction"] is None
    # no common frames at all: same inert read (n == 0 -> p_sign 1.0)
    r0 = om.dual_rule_read({0: True}, {100: True})
    assert r0["p_sign"] == 1.0 and r0["p_mag"] == 1.0 and r0["decisive"] is False


def test_dual_rule_read_deterministic_with_seed():
    cap_a = dict.fromkeys((0, 100, 200, 300, 400), True)
    cap_b = dict.fromkeys((0, 100, 200, 300, 400), False)
    r1 = om.dual_rule_read(cap_a, cap_b)
    r2 = om.dual_rule_read(cap_a, cap_b)
    assert r1 == r2  # default_rng(seed) -> bit-identical reads
    # pinned seeded permutation value (seed 11, 4000 flips) for this geometry
    assert r1["p_mag"] == pytest.approx(0.06425)
    # a different seed draws different flips
    r3 = om.dual_rule_read(cap_a, cap_b, seed=12)
    assert r3["p_mag"] != r1["p_mag"]


def test_dual_rule_read_direction():
    # b the clear winner: d_obs < 0 -> 'b'
    cap_a = dict.fromkeys((0, 100, 200), False)
    cap_b = {0: True, 100: True, 200: False}
    r = om.dual_rule_read(cap_a, cap_b)
    assert r["d_obs"] < 0 and r["direction"] == "b"
    # d_obs == 0 (2 a-only frames vs 2 b-only frames) but ea=2 > eb=1 -> 'a'
    ca = {0: True, 200: True, 400: False, 410: False}
    cb = {0: False, 200: False, 400: True, 410: True}
    rt = om.dual_rule_read(ca, cb)
    assert rt["d_obs"] == 0.0 and rt["ea"] == 2 and rt["eb"] == 1
    assert rt["direction"] == "a"
    # |d_obs| = 0 threshold makes the permutation inert
    assert rt["p_mag"] == 1.0


# ---------------------------------------------------------------------------
# Framing metrics
# ---------------------------------------------------------------------------


def test_framing_events_min_frames():
    frames = [0, 8, 16, 24, 32, 200, 208, 216, 224, 900]
    # gap-64 events: [0..32] (5 fr), [200..224] (4 fr), [900] (1 fr)
    assert om.framing_events(frames) == [[0, 8, 16, 24, 32]]
    assert om.framing_events(frames, min_frames=4) == [
        [0, 8, 16, 24, 32],
        [200, 208, 216, 224],
    ]
    assert om.framing_events(frames, min_frames=1) == om.cluster_events(frames)
    assert om.framing_events([]) == []


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
# Amended hold unit (EXP-OP-02 final correction)
# ---------------------------------------------------------------------------


def test_hold_clusters_amended_unit():
    # gap-40 clustering: 40 apart joins, 41 splits
    fx = {0: 0.0, 40: 10.0, 80: 20.0, 120: 30.0}
    assert om.hold_clusters(fx) == [[0, 40, 80, 120]]
    fxsplit = {0: 0.0, 41: 0.0, 82: 0.0, 123: 0.0}  # 41-gaps -> 4 singletons
    assert om.hold_clusters(fxsplit) == []
    # n = 3 cluster excluded (min_frames 4)
    assert om.hold_clusters({0: 0.0, 8: 1.0, 16: 2.0}) == []
    # GT swing exactly 200 px excluded -- STRICT < (the (h) script's rule)
    fx200 = {0: 1000.0, 8: 1200.0, 16: 1000.0, 24: 1000.0}
    assert om.hold_clusters(fx200) == []
    fx199 = {0: 1000.0, 8: 1199.9, 16: 1000.0, 24: 1000.0}
    assert om.hold_clusters(fx199) == [[0, 8, 16, 24]]


def test_hold_fidelity_amended():
    frames = [0, 8, 16, 24]
    gt = {0: 1000.0, 8: 1050.0, 16: 1000.0, 24: 1050.0}  # swing 50 < 200
    arm = {f: 1000.0 + 2.0 * f for f in frames}  # swing 48 -> ratio 0.96
    rows, med, n = om.hold_fidelity_amended(arm, gt)
    assert n == 1 and med == pytest.approx(48.0 / 50.0)
    cl, gt_swing, arm_swing, ratio = rows[0]
    assert cl == frames and gt_swing == 50.0 and arm_swing == 48.0
    assert ratio == pytest.approx(0.96)
    # arm covering < 2 cluster frames -> cluster skipped (uncovered)
    assert om.hold_fidelity_amended({0: 1.0}, gt) == ([], None, 0)
    # zero GT swing: ratio 0.0 when the arm also held, inf when it swung
    gt0 = dict.fromkeys(frames, 1000.0)
    _r, med0, n0 = om.hold_fidelity_amended(dict.fromkeys(frames, 5.0), gt0)
    assert med0 == 0.0 and n0 == 1
    rinf, _m, _n = om.hold_fidelity_amended({f: float(f) for f in frames}, gt0)
    assert math.isinf(rinf[0][3])
    # NO conditioning on the arm's swing: a wildly swinging arm still qualifies
    wild = {0: 0.0, 8: 5000.0, 16: 0.0, 24: 5000.0}
    _rows, med_wild, n_wild = om.hold_fidelity_amended(wild, gt)
    assert n_wild == 1 and med_wild == pytest.approx(5000.0 / 50.0)


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


def _write_synthetic(
    tmp_path, campath_len=1000, ac_frames=500, skip_labels=False, champ_cx=1100.0
):
    """Tiny synthetic set dir + game dir + campath: champ cx constant
    ``champ_cx``, AutoCam x constant 1000 over the first ``ac_frames`` frames."""
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
                "frames": [[champ_cx, 500.0, 42.0]] * campath_len,
            }
        )
    )
    return sd, gd, cp


# 4 framing events of 5 frames each (gap-64; separations 168 > 64) which are
# also 4 amended hold clusters (gap-40, n=5 >= 4, GT swing 50 < 200): dense
# enough that every amended-unit metric and null band is computable.
DENSE_EVENTS = [list(range(s, s + 40, 8)) for s in (0, 200, 400, 600)]
DENSE_FRAMES = [f for ev in DENSE_EVENTS for f in ev]


def _write_synthetic_dense(tmp_path):
    """Denser synthetic set: champ cx constant 1100, AutoCam covers everything,
    GT fx alternates 1000/1050 inside each event."""
    sd = tmp_path / "synth_dense"
    sd.mkdir(exist_ok=True)
    rows = [
        {"frame_idx": f, "action": "view", "fx": 1000.0 + 50.0 * (i % 2), "fy": None}
        for i, f in enumerate(DENSE_FRAMES)
    ]
    (sd / "labels.json").write_text(json.dumps(rows))
    (sd / "manifest.json").write_text(
        json.dumps(
            {
                "set": "synth_dense",
                "n_frames": len(DENSE_FRAMES),
                "frames": [{"frame_idx": f, "kind": "?"} for f in DENSE_FRAMES],
            }
        )
    )
    gd = tmp_path / "game_dense"
    gd.mkdir(exist_ok=True)
    (gd / "game.json").write_text(
        json.dumps({"segments": [{"seg": "s0", "global_offset": 0}]})
    )
    with open(gd / "autocam_viewport.jsonl", "w") as fh:
        for f in range(700):
            fh.write(json.dumps({"seg": "s0", "f": f, "x": 1000.0, "y": 500.0}) + "\n")
    cp = tmp_path / "campath_dense.json"
    cp.write_text(
        json.dumps(
            {
                "schema": "camera_path/1",
                "g_start": 0,
                "src_w": 3840,
                "src_h": 1080,
                "fps": 30.0,
                "frames": [[1100.0, 500.0, 42.0]] * 700,
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
    # framing on the AMENDED units: the sparse set has NO >=5-frame gap-64 event
    # (events are 4/2/2 frames), so velocity/reversal are STATED non-computable
    fr = blk["framing"]
    assert fr["computable"] is False and "no framing event" in fr["note"]
    assert fr["n_segments"] == 3 and fr["n_framing_events"] == 0
    assert "champ" in fr["arms"] and "AC" in fr["arms"]
    assert "velocity" not in fr["arms"]["champ"]  # absent, not faked
    # ONE amended hold cluster ([0,8,16,24], GT swing 50 < 200); champ constant
    # -> arm swing 0 -> ratio 0.0; old whole-event hold kept as the diagnostic
    assert fr["n_hold_clusters"] == 1
    champ_fr = fr["arms"]["champ"]
    assert champ_fr["hold"]["n_clusters"] == 1
    assert champ_fr["hold"]["median_ratio"] == pytest.approx(0.0)
    assert champ_fr["hold_wholeevent"]["n_segments"] == 3
    assert champ_fr["hold_wholeevent"]["median_ratio"] == pytest.approx(0.0)
    # pair flips exist per subset
    assert "champ_vs_AC" in blk["pair_flips"]["ALL"]
    # no --referee flag -> no referee block
    assert "referee" not in blk
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
    # dense set: 4 framing events (>=5 frames) = 4 amended hold clusters
    sd, gd, cp = _write_synthetic_dense(tmp_path)
    report = _run_scoreboard(
        tmp_path, sd, gd, cp, extra=["--null-calibration", "--seed", "5"]
    )
    blk = report["sets"]["synth_dense"]
    assert blk["framing"]["computable"] is True
    assert blk["framing"]["n_framing_events"] == 4
    assert blk["framing"]["n_hold_clusters"] == 4
    assert blk["framing"]["gt"]["v_thresh"] > 0
    # champ is constant -> zero pan velocity everywhere
    assert blk["framing"]["arms"]["champ"]["velocity"]["median"] == pytest.approx(0.0)
    nc = report["null_calibration"]["synth_dense"]
    assert nc["arm"] == "champ" and nc["reps"] == 300 and nc["seed"] == 5
    # champ is constant -> every framing metric is 0 on every half-split
    assert nc["pan_velocity_median"]["band"] == [0.0, 0.0]
    assert nc["reversal_rate"]["band"] == [0.0, 0.0]
    assert nc["hold_fidelity_median_ratio"]["band"] == [0.0, 0.0]
    # velocity/reversal split over the 4 framing events; hold over the 4 clusters
    assert nc["pan_velocity_median"]["n_events"] == 4
    assert nc["hold_fidelity_median_ratio"]["n_events"] == 4
    assert "framing_events" in nc["units"]["velocity_reversal"]
    assert "hold_clusters" in nc["units"]["hold"]


def test_cli_null_calibration_power_floors_on_sparse_set(tmp_path):
    # the sparse set has 0 framing events (>=5 fr) and 1 amended hold cluster:
    # every band is incomputable and must record its power floor EXPLICITLY
    sd, gd, cp = _write_synthetic(tmp_path)
    report = _run_scoreboard(tmp_path, sd, gd, cp, extra=["--null-calibration"])
    nc = report["null_calibration"]["synth_set"]
    assert nc["pan_velocity_median"]["band"] is None
    assert "power_floor" in nc["pan_velocity_median"]
    # no framing event -> no GT velocity threshold -> reversal power floor
    assert nc["reversal_rate"]["band"] is None
    assert "no GT velocity threshold" in nc["reversal_rate"]["power_floor"]["reason"]
    # 1 amended hold cluster -> fewer than 2 events -> power floor
    hf = nc["hold_fidelity_median_ratio"]
    assert hf["band"] is None
    assert hf["power_floor"]["n_events"] == 1
    assert "fewer than 2" in hf["power_floor"]["reason"]


def test_cli_referee_dual_rule_reads(tmp_path):
    # champ cx 2000: captures@600 exactly {100, 108}; AC (covers frames < 500)
    # captures {0, 8, 16, 24}. Common frames {0,8,16,24,100,108} -> 2 events;
    # contribs (0-4) and (2-0) -> d_obs = -2/6; ea=1, eb=1 -> p_sign = 1.0;
    # permutation of (-4, +2): |sum|/6 >= 1/3 for all 4 patterns -> p_mag = 1.0.
    sd, gd, cp = _write_synthetic(tmp_path, champ_cx=2000.0)
    report = _run_scoreboard(tmp_path, sd, gd, cp, extra=["--referee"])
    ref = report["sets"]["synth_set"]["referee"]
    r = ref["ALL"]["all"]["champ_vs_AC"]
    assert r["ea"] == 1 and r["eb"] == 1
    assert r["p_sign"] == 1.0
    assert r["d_obs"] == pytest.approx(-2.0 / 6.0)
    assert r["p_mag"] == 1.0
    assert r["decisive"] is False and r["direction"] == "b"
    # subsets and the 'all' band always present (no fy -> no geometry bands)
    assert set(ref.keys()) == {"ALL", "original", "ext-div"}
    assert list(ref["ALL"].keys()) == ["all"]
    # ext-div frames (900, 908) are beyond AC coverage -> no common frames
    rx = ref["ext-div"]["all"]["champ_vs_AC"]
    assert rx["ea"] == 0 and rx["eb"] == 0 and rx["p_sign"] == 1.0


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
