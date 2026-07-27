"""Composite-reference instrument tests (DECISIONS 2026-07-26 (w)).

Builder tier precedence / 600px gates / corroboration / hard-fails on
synthetic tmp_path fixtures; the scoreboard's composite MATCH/BEAT/overall
cells hand-computed on a tiny synthetic composite; and the planned-view
containment formula (the score_plan convention) edge cases — a point exactly
on the rectangle edge is INSIDE. All hard-fail guards must exit non-zero per
CLAUDE.md rule 8."""

import json

import pytest

from training.world_model import operator_metrics as om

# ---------------------------------------------------------------------------
# Planned-view containment formula (om)
# ---------------------------------------------------------------------------


def test_planned_view_half_extents_score_plan_convention():
    # half_w = src_w * (hfov/180)/2; half_h = half_w * (1080/1920)
    hw, hh = om.planned_view_half_extents(42.0, 3840.0)
    assert hw == pytest.approx(448.0)
    assert hh == pytest.approx(252.0)
    hw180, hh180 = om.planned_view_half_extents(180.0, 7680.0)
    assert hw180 == pytest.approx(3840.0)  # full-width view at hfov 180
    assert hh180 == pytest.approx(3840.0 * 1080.0 / 1920.0)


def test_planned_view_contains_edges_inclusive():
    # cam (1000, 500), hfov 42, src_w 3840 -> half_w 448, half_h 252
    view = (1000.0, 500.0, 42.0, 3840.0)
    assert om.planned_view_contains(1448.0, 500.0, *view)  # exactly on +x edge
    assert om.planned_view_contains(552.0, 500.0, *view)  # exactly on -x edge
    assert om.planned_view_contains(1000.0, 752.0, *view)  # exactly on +y edge
    assert om.planned_view_contains(1000.0, 248.0, *view)  # exactly on -y edge
    assert om.planned_view_contains(1448.0, 752.0, *view)  # exact corner
    assert not om.planned_view_contains(1448.1, 500.0, *view)
    assert not om.planned_view_contains(1000.0, 752.1, *view)
    assert not om.planned_view_contains(1448.1, 752.1, *view)


def test_capture_contain_stats_hand_computed():
    refs = {
        0: (1000.0, 500.0),
        1: (1601.0, 500.0),
        2: (1500.0, None),  # view-tier row without y: no containment
        3: (99999.0, 500.0),  # arm does not cover this frame
    }
    plans = {
        0: (1000.0, 500.0, 42.0),
        1: (1000.0, 500.0, 42.0),
        2: (1000.0, 500.0, 42.0),
    }
    st = om.capture_contain_stats(refs, plans, 3840.0, [0, 1, 2, 3])
    assert st["n"] == 3
    assert st["cap600"] == pytest.approx(2 / 3)  # |0| yes, |601| no, |500| yes
    assert st["n_contain"] == 2  # frame 2 has no ref y
    assert st["contain"] == pytest.approx(0.5)  # 0 inside; 1 outside (601 > 448)
    # arm without planned hfov (the AC viewport arm): capture works, containment
    # is None with n_contain 0 -- never a faked number
    ac = {0: (1000.0, 500.0, None), 1: (1000.0, None, None)}
    st = om.capture_contain_stats(refs, ac, 3840.0, [0, 1])
    assert st["n"] == 2 and st["cap600"] == pytest.approx(0.5)
    assert st["n_contain"] == 0 and st["contain"] is None
    # empty column
    assert om.capture_contain_stats(refs, plans, 3840.0, []) == {
        "n": 0,
        "cap600": None,
        "n_contain": 0,
        "contain": None,
    }


# ---------------------------------------------------------------------------
# Builder fixtures
# ---------------------------------------------------------------------------


def _write_game(tmp_path, w=3840, name="game"):
    gd = tmp_path / name
    gd.mkdir(exist_ok=True)
    (gd / "game.json").write_text(
        json.dumps({"segments": [{"seg": "s0", "global_offset": 0, "w": w, "h": 1080}]})
    )
    return gd


def _write_aim(path, rows):
    with open(path, "w") as fh:
        for f, x, y in rows:
            fh.write(json.dumps({"f": f, "x": x, "y": y}) + "\n")


def _write_viewport_jsonl(path, rows):
    with open(path, "w") as fh:
        for f, x, y in rows:
            fh.write(json.dumps({"seg": "s0", "f": f, "x": x, "y": y}) + "\n")


def _write_ball_labels(gd, balls=(), novis=()):
    with open(gd / "ball_labels.jsonl", "w") as fh:
        for f, x, y in balls:
            fh.write(json.dumps({"seg": "s0", "f": f, "a": "ball", "p": [x, y]}) + "\n")
        for f in novis:
            fh.write(json.dumps({"seg": "s0", "f": f, "a": "not_visible"}) + "\n")


def _write_view_set(tmp_path, name, rows):
    sd = tmp_path / name
    sd.mkdir(exist_ok=True)
    (sd / "labels.json").write_text(
        json.dumps(
            [
                {"frame_idx": f, "action": "view", "fx": fx, "fy": fy}
                for f, fx, fy in rows
            ]
        )
    )
    return sd


def _build(tmp_path, gd, ac_src, sets=(), out_name="composite.jsonl", force=False):
    from training.cli import build_composite_reference as bcr

    out = tmp_path / out_name
    argv = ["--game-dir", str(gd), "--ac-source", str(ac_src), "--out", str(out)]
    for s in sets:
        argv += ["--viewport-set-dir", str(s)]
    if force:
        argv.append("--force")
    bcr.main(argv)
    lines = [json.loads(ln) for ln in out.read_text().splitlines() if ln.strip()]
    meta = lines[0]["_meta"]
    return {r["g"]: r for r in lines[1:]}, meta


AIM_10 = [(f, 1000.0, 500.0) for f in range(10)]


# ---------------------------------------------------------------------------
# Builder: tiers, gates, corroboration
# ---------------------------------------------------------------------------


def test_builder_dense_ac_tier_from_aim(tmp_path):
    gd = _write_game(tmp_path)
    aim = tmp_path / "autocam_aim.jsonl"
    _write_aim(aim, AIM_10)
    rows, meta = _build(tmp_path, gd, aim)
    assert sorted(rows) == list(range(10))
    assert all(r["tier"] == "ac" for r in rows.values())
    assert all(r["src"] == "autocam_aim.jsonl" for r in rows.values())
    assert not any(r.get("corroborated") for r in rows.values())
    assert meta["schema"] == "composite_reference/1"
    assert meta["ac_format"] == "aim"
    assert meta["gate_px"] == 600.0
    assert meta["ball_gate_window_frames"] == 20
    assert meta["counts"] == {
        "ball": 0,
        "view": 0,
        "ac": 10,
        "corroborated": 0,
        "novis_removed": 0,
    }


def test_builder_view_gate_600(tmp_path):
    gd = _write_game(tmp_path)
    aim = tmp_path / "autocam_aim.jsonl"
    _write_aim(aim, AIM_10)
    # |fx - ac_x|: 600 exactly -> NOT an override (gate is strict >); 600.5 ->
    # override; frame 20 is beyond AC coverage -> GT stands on its own
    vs = _write_view_set(
        tmp_path,
        "vset",
        [(2, 1600.0, 400.0), (3, 1600.5, 400.0), (20, 2000.0, None)],
    )
    rows, meta = _build(tmp_path, gd, aim, sets=[vs])
    assert rows[2]["tier"] == "ac" and rows[2]["corroborated"] is True
    assert rows[2]["x"] == 1000.0  # AC's dense signal stands, not the GT point
    assert rows[3] == {
        "g": 3,
        "x": 1600.5,
        "y": 400.0,
        "tier": "view",
        "src": "vset",
    }
    assert rows[20]["tier"] == "view" and rows[20]["y"] is None
    assert meta["counts"]["view"] == 2 and meta["counts"]["corroborated"] == 1
    assert meta["viewport_sets"] == ["vset"]


def test_builder_ball_gate_and_precedence_over_view(tmp_path):
    gd = _write_game(tmp_path)
    aim = tmp_path / "autocam_aim.jsonl"
    _write_aim(aim, AIM_10)
    _write_ball_labels(
        gd,
        balls=[
            (4, 1650.0, 480.0),  # diverges > 600 -> ball override
            (5, 1400.0, 480.0),  # agrees -> AC stands, corroborated
            (6, 1100.0, 480.0),  # agrees, view diverges -> ball's verdict WINS
            (7, 2000.0, 480.0),  # both diverge -> ball beats view
            (21, 3000.0, 900.0),  # beyond AC coverage -> ball stands alone
        ],
    )
    vs = _write_view_set(
        tmp_path, "vset", [(4, 1000.0, 500.0), (6, 5000.0, 500.0), (7, 5000.0, 500.0)]
    )
    rows, meta = _build(tmp_path, gd, aim, sets=[vs])
    assert rows[4]["tier"] == "ball" and rows[4]["x"] == 1650.0
    assert rows[5]["tier"] == "ac" and rows[5]["corroborated"] is True
    # ball agrees with AC while the view label diverges: tier 1 outranks tier 2
    assert rows[6]["tier"] == "ac" and rows[6]["corroborated"] is True
    assert rows[7]["tier"] == "ball" and rows[7]["x"] == 2000.0
    assert rows[21] == {
        "g": 21,
        "x": 3000.0,
        "y": 900.0,
        "tier": "ball",
        "src": "ball_labels.jsonl",
    }
    assert meta["counts"]["ball"] == 3
    assert meta["counts"]["view"] == 0
    assert meta["counts"]["corroborated"] == 2


def test_builder_ball_gate_windowed_trailing_vs_parked(tmp_path):
    # EXP-OP-13 amendment: AC TRAILS the ball by ~1 s, so the tier-1 gate is
    # windowed over k in [-20, +20]. A trailing follower: AC parked at 1000
    # until f=40, then caught up to 1950.
    gd = _write_game(tmp_path, name="game_trail")
    aim = tmp_path / "aim_trail.jsonl"
    _write_aim(aim, [(f, 1000.0 if f < 40 else 1950.0, 500.0) for f in range(46)])
    _write_ball_labels(gd, balls=[(20, 2000.0, 480.0)])
    rows, _meta = _build(tmp_path, gd, aim, out_name="comp_trail.jsonl")
    # instantaneous |2000 - 1000| = 1000 > 600, but at k=+20 (f=40) AC is at
    # 1950 -> windowed min 50 <= 600: the follower stays -> AC row, corroborated
    assert rows[20]["tier"] == "ac" and rows[20]["corroborated"] is True
    assert rows[20]["x"] == 1000.0

    # a park on the wrong object: AC flat 1000 at EVERY lag -> override stands
    gd2 = _write_game(tmp_path, name="game_park")
    aim2 = tmp_path / "aim_park.jsonl"
    _write_aim(aim2, [(f, 1000.0, 500.0) for f in range(46)])
    _write_ball_labels(gd2, balls=[(20, 2000.0, 480.0)])
    rows2, _m2 = _build(tmp_path, gd2, aim2, out_name="comp_park.jsonl")
    assert rows2[20]["tier"] == "ball" and rows2[20]["x"] == 2000.0


def test_builder_ball_gate_window_boundary(tmp_path):
    # agreement exactly at k=+20 or k=-20 is INSIDE the window ...
    gd = _write_game(tmp_path, name="game_b20")
    aim = tmp_path / "aim_b20.jsonl"
    _write_aim(
        aim,
        [(f, 1950.0 if f in (25, 40) else 1000.0, 500.0) for f in range(46)],
    )
    _write_ball_labels(gd, balls=[(20, 2000.0, 480.0), (45, 2000.0, 480.0)])
    rows, _m = _build(tmp_path, gd, aim, out_name="comp_b20.jsonl")
    assert rows[20]["tier"] == "ac" and rows[20]["corroborated"] is True  # k=+20
    assert rows[45]["tier"] == "ac" and rows[45]["corroborated"] is True  # k=-20

    # ... while agreement only at k=+21 is OUTSIDE: the override stands
    gd2 = _write_game(tmp_path, name="game_b21")
    aim2 = tmp_path / "aim_b21.jsonl"
    _write_aim(
        aim2,
        [(f, 1950.0 if f == 41 else 1000.0, 500.0) for f in range(46)],
    )
    _write_ball_labels(gd2, balls=[(20, 2000.0, 480.0)])
    rows2, _m2 = _build(tmp_path, gd2, aim2, out_name="comp_b21.jsonl")
    assert rows2[20]["tier"] == "ball" and rows2[20]["x"] == 2000.0


def test_builder_not_visible_frames_removed(tmp_path):
    gd = _write_game(tmp_path)
    aim = tmp_path / "autocam_aim.jsonl"
    _write_aim(aim, AIM_10)
    _write_ball_labels(gd, novis=[8])
    rows, meta = _build(tmp_path, gd, aim)
    assert 8 not in rows
    assert len(rows) == 9
    assert meta["counts"]["novis_removed"] == 1


def test_builder_campath_source_excludes_pad(tmp_path):
    gd = _write_game(tmp_path)
    cp = tmp_path / "campath.json"
    cp.write_text(
        json.dumps(
            {
                "schema": "camera_path/1",
                "g_start": 2,
                "src_w": 3840,
                "src_h": 1080,
                "fps": 30.0,
                "frames": [[1000.0, 500.0, 42.0]] * 3,
            }
        )
    )
    rows, meta = _build(tmp_path, gd, cp)
    # the constant-pad head [0, g_start) is NOT reference data
    assert sorted(rows) == [2, 3, 4]
    assert meta["ac_format"] == "camera_path"
    assert rows[2] == {"g": 2, "x": 1000.0, "y": 500.0, "tier": "ac", "src": cp.name}


def test_builder_validated_viewport_jsonl_allowed_on_dahua(tmp_path):
    gd = _write_game(tmp_path, w=4096)  # PIT-class (Dahua) validated source
    vp = gd / "autocam_viewport.jsonl"
    _write_viewport_jsonl(vp, [(f, 1000.0, 500.0) for f in range(5)])
    rows, meta = _build(tmp_path, gd, vp)
    assert meta["ac_format"] == "viewport"
    assert sorted(rows) == list(range(5))


# ---------------------------------------------------------------------------
# Builder: hard-fail guards (rule 8)
# ---------------------------------------------------------------------------


def test_builder_legacy_reolink_viewport_hard_fails(tmp_path):
    # quarantined legacy class with NOTHING to verify a remap against (no
    # match_info.ini) -> hard-fail, not silent naive admission (EXP-OP-15)
    gd = _write_game(tmp_path, w=7680)
    vp = gd / "autocam_viewport.jsonl"
    _write_viewport_jsonl(vp, [(f, 1000.0, 500.0) for f in range(5)])
    with pytest.raises(SystemExit) as ei:
        _build(tmp_path, gd, vp)
    assert "EXP-OP-05" in str(ei.value)
    assert "match_info" in str(ei.value)
    assert ei.value.code not in (0, None)


def test_builder_legacy_ban_is_format_based_too(tmp_path):
    # a seg-keyed viewport jsonl under ANY name is quarantined on a 7680-wide
    # game (format-based, not name-based)
    gd = _write_game(tmp_path, w=7680)
    vp = tmp_path / "renamed_viewport.jsonl"
    _write_viewport_jsonl(vp, [(f, 1000.0, 500.0) for f in range(5)])
    with pytest.raises(SystemExit) as ei:
        _build(tmp_path, gd, vp)
    assert "EXP-OP-05" in str(ei.value)
    # ... while the AIM format on the same 7680-wide game is fine
    aim = tmp_path / "autocam_aim.jsonl"
    _write_aim(aim, AIM_10)
    rows, meta = _build(tmp_path, gd, aim, out_name="composite_aim.jsonl")
    assert meta["ac_format"] == "aim" and len(rows) == 10


# ---------------------------------------------------------------------------
# Builder: legacy trim-aware remap admission (EXP-OP-15)
# ---------------------------------------------------------------------------


def _x_true(g):
    # incommensurate periods: the autocorrelation peaks ONLY at shift 0, so
    # the offset fit has a single unambiguous argmax
    import math

    return 1000.0 + 500.0 * math.sin(g / 30.0) + 300.0 * math.sin(g / 7.3)


def _write_legacy_game(tmp_path, trim="00:10", frames=2000, fps=20.0):
    """7680-wide single-segment game with a trim offset (D_pred = 10s x 20fps
    = 200 fr) and >=100 ball-GT anchors on the TRUE timeline."""
    gd = tmp_path / "legacy_game"
    gd.mkdir(exist_ok=True)
    (gd / "game.json").write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "seg": "s0",
                        "global_offset": 0,
                        "frames": frames,
                        "w": 7680,
                        "h": 2160,
                        "fps": fps,
                    }
                ]
            }
        )
    )
    (gd / "match_info.ini").write_text(
        f"[MATCH]\nstart_time_offset = {trim}\ntotal_duration = \n"
    )
    return gd


def _write_legacy_viewport(gd, d_true, frames=2000, name="autocam_viewport.jsonl"):
    """Legacy recording: trimmed-timeline content chunked under the untrimmed
    segment label — row f=t holds the signal of TRUE frame t + d_true."""
    vp = gd / name
    _write_viewport_jsonl(
        vp, [(t, _x_true(t + d_true), 500.0) for t in range(frames - d_true)]
    )
    return vp


def _write_true_anchors(gd, lo=700, hi=1900, step=8):
    _write_ball_labels(gd, balls=[(g, _x_true(g), 400.0) for g in range(lo, hi, step)])


def test_builder_legacy_remap_admission(tmp_path):
    gd = _write_legacy_game(tmp_path)
    vp = _write_legacy_viewport(gd, d_true=200)
    _write_true_anchors(gd)
    rows, meta = _build(tmp_path, gd, vp)
    assert meta["ac_format"] == "viewport_trim_remapped"
    rm = meta["legacy_remap"]
    assert rm["d_pred"] == 200 and rm["d_fit"] == 200
    assert rm["pooled_r"] > 0.99 and rm["n_anchors"] >= 100
    assert rm["pooled_r_naive"] is None or rm["pooled_r_naive"] < 0.7
    assert rm["per_seg_r"] and rm["per_seg_r"][0]["seg"] == "s0"
    assert rm["per_seg_r"][0]["r"] > 0.99
    # rows live on the TRUE timeline: t + 200 for t in [0, 1800)
    assert min(rows) == 200 and max(rows) == 1999
    # exact-agreeing ball anchors corroborate the remapped AC tier
    assert meta["counts"]["ball"] == 0
    assert meta["counts"]["corroborated"] >= 100


def test_builder_legacy_remap_fit_drift_hard_fails(tmp_path):
    # recorded at a REAL offset of 500 while match_info predicts 200: the fit
    # confirms 500 (r ~1) but the drift gate rejects the unexplained trim
    gd = _write_legacy_game(tmp_path)
    vp = _write_legacy_viewport(gd, d_true=500)
    _write_true_anchors(gd)
    with pytest.raises(SystemExit) as ei:
        _build(tmp_path, gd, vp)
    assert "drifts" in str(ei.value) and "EXP-OP-05" in str(ei.value)


def test_builder_legacy_remap_low_r_hard_fails(tmp_path):
    # decorrelated anchors (hash-pattern x): no offset in the window aligns
    gd = _write_legacy_game(tmp_path)
    vp = _write_legacy_viewport(gd, d_true=200)
    _write_ball_labels(
        gd,
        balls=[(g, float(200 + (g * 997) % 7000), 400.0) for g in range(700, 1900, 8)],
    )
    with pytest.raises(SystemExit) as ei:
        _build(tmp_path, gd, vp)
    assert "REJECTED" in str(ei.value)


def test_builder_legacy_remap_insufficient_anchors_hard_fails(tmp_path):
    gd = _write_legacy_game(tmp_path)
    vp = _write_legacy_viewport(gd, d_true=200)
    _write_true_anchors(gd, lo=700, hi=1100, step=8)  # 50 anchors < 100
    with pytest.raises(SystemExit) as ei:
        _build(tmp_path, gd, vp)
    assert "anchors" in str(ei.value)


def test_builder_raw_cli_aim_xy_rows(tmp_path):
    # the raw Once.Autocam CLI capture: BOM, console-output dicts and plain
    # text interleaved with {"xy": [x, y], "f": n, "t": s} data rows
    gd = _write_game(tmp_path, w=7680)
    aim = tmp_path / "autocam_aim.jsonl"
    lines = [
        '{"lines": ["Once Autocam 3.0.7"]}',
        '{"cwd": "C:\\\\somewhere"}',
        "Image for marking the playing field taken from timestamp: 00:4:04.000",
        '{"Reader": "hevc"}',
    ] + [
        json.dumps({"xy": [1000 + f, 500], "f": f, "t": f * 0.05}) for f in range(1, 8)
    ]
    # utf-8-sig: the real capture starts with a BOM
    aim.write_text("\n".join(lines), encoding="utf-8-sig")
    rows, meta = _build(tmp_path, gd, aim)
    assert meta["ac_format"] == "aim"
    assert sorted(rows) == list(range(1, 8))
    assert rows[3]["x"] == 1003.0 and rows[3]["y"] == 500.0


def test_parse_trim_offset_seconds(tmp_path):
    from training.data_prep import distill_dataset as dd

    p = tmp_path / "match_info.ini"
    for raw, want in [("01:00", 60.0), ("06:00", 360.0), ("1:02:03", 3723.0)]:
        p.write_text(f"[MATCH]\nstart_time_offset = {raw}\n")
        assert dd.parse_trim_offset_seconds(p) == want
    p.write_text("[MATCH]\nstart_time_offset = \n")
    assert dd.parse_trim_offset_seconds(p) is None
    p.write_text("[MATCH]\nstart_time_offset = garbage\n")
    assert dd.parse_trim_offset_seconds(p) is None
    assert dd.parse_trim_offset_seconds(tmp_path / "nope.ini") is None


def test_pick_ac_source_precedence(tmp_path):
    from training.cli.build_composite_reference import pick_ac_source

    gd = tmp_path / "pgame"
    gd.mkdir()
    assert pick_ac_source(gd) is None
    _write_viewport_jsonl(gd / "autocam_viewport.jsonl", [(0, 1.0, 2.0)])
    assert pick_ac_source(gd).name == "autocam_viewport.jsonl"
    # a stub aim (console lines, ZERO data rows — fair's case) must NOT win
    (gd / "autocam_aim.jsonl").write_text('{"lines": ["Once Autocam 3.0.7"]}\n')
    assert pick_ac_source(gd).name == "autocam_viewport.jsonl"
    # a usable aim wins ((x) precedence)
    (gd / "autocam_aim.jsonl").write_text('{"xy": [1, 2], "f": 3, "t": 0.1}\n')
    assert pick_ac_source(gd).name == "autocam_aim.jsonl"


# ---------------------------------------------------------------------------
# Scoreboard AC arm through the (x) source policy
# ---------------------------------------------------------------------------


def _write_label_set(tmp_path, name, frames_fx):
    sd = tmp_path / name
    sd.mkdir(exist_ok=True)
    (sd / "labels.json").write_text(
        json.dumps(
            [
                {"frame_idx": f, "action": "view", "fx": fx, "fy": None}
                for f, fx in frames_fx
            ]
        )
    )
    (sd / "manifest.json").write_text(
        json.dumps(
            {
                "set": name,
                "n_frames": len(frames_fx),
                "frames": [{"frame_idx": f, "kind": "?"} for f, _ in frames_fx],
            }
        )
    )
    return sd


def _write_campath(tmp_path, src_w=7680, n=1000):
    cp = tmp_path / "campath.json"
    cp.write_text(
        json.dumps(
            {
                "schema": "camera_path/1",
                "g_start": 0,
                "src_w": src_w,
                "src_h": 2160,
                "fps": 30.0,
                "frames": [[1100.0, 500.0, 42.0]] * n,
            }
        )
    )
    return cp


def test_scoreboard_ac_arm_from_remapped_legacy(tmp_path):
    # 7680-wide game, verifiable legacy viewport: the AC arm must come from the
    # verified remap, with the remap record in coverage (EXP-OP-14's note)
    gd = _write_legacy_game(tmp_path)
    _write_legacy_viewport(gd, d_true=200)
    _write_true_anchors(gd)
    sd = _write_label_set(tmp_path, "lset", [(300, 1000.0), (500, 1050.0)])
    report = _run_scoreboard(tmp_path, sd, gd, _write_campath(tmp_path))
    cov = report["sets"]["lset"]["coverage"]["AC"]
    assert cov["source"] == "autocam_viewport.jsonl"
    assert cov["format"] == "viewport_trim_remapped"
    assert cov["legacy_remap"]["d_pred"] == 200
    assert cov["n_covered"] == 2


def test_scoreboard_ac_arm_prefers_usable_aim(tmp_path):
    gd = _write_legacy_game(tmp_path)
    _write_legacy_viewport(gd, d_true=200)
    _write_true_anchors(gd)
    _write_aim(gd / "autocam_aim.jsonl", [(f, 900.0, 450.0) for f in range(600)])
    sd = _write_label_set(tmp_path, "aset", [(300, 1000.0), (500, 1050.0)])
    report = _run_scoreboard(tmp_path, sd, gd, _write_campath(tmp_path))
    cov = report["sets"]["aset"]["coverage"]["AC"]
    assert cov["source"] == "autocam_aim.jsonl"
    assert cov["format"] == "aim"
    assert "legacy_remap" not in cov


def test_scoreboard_ac_arm_unverifiable_legacy_hard_fails(tmp_path):
    # 7680-wide game, legacy viewport, no match_info/anchors: the scoreboard
    # must NOT silently score the broken timeline as "AC" (rule 8)
    gd = tmp_path / "badgame"
    gd.mkdir()
    (gd / "game.json").write_text(
        json.dumps(
            {"segments": [{"seg": "s0", "global_offset": 0, "w": 7680, "h": 2160}]}
        )
    )
    _write_viewport_jsonl(
        gd / "autocam_viewport.jsonl", [(f, 1000.0, 500.0) for f in range(600)]
    )
    sd = _write_label_set(tmp_path, "bset", [(300, 1000.0), (500, 1050.0)])
    with pytest.raises(SystemExit) as ei:
        _run_scoreboard(tmp_path, sd, gd, _write_campath(tmp_path))
    assert "EXP-OP-05" in str(ei.value)


def test_builder_missing_or_bad_ac_source_hard_fails(tmp_path):
    gd = _write_game(tmp_path)
    with pytest.raises(SystemExit) as ei:
        _build(tmp_path, gd, tmp_path / "nope.jsonl")
    assert ei.value.code not in (0, None)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    with pytest.raises(SystemExit):
        _build(tmp_path, gd, empty)
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"schema": "trajectory/1", "frames": []}))
    with pytest.raises(SystemExit):
        _build(tmp_path, gd, other)


def test_builder_zero_rows_hard_fails(tmp_path):
    gd = _write_game(tmp_path)
    aim = tmp_path / "autocam_aim.jsonl"
    _write_aim(aim, [(0, 1000.0, 500.0)])
    _write_ball_labels(gd, novis=[0])  # the only AC frame is not_visible
    with pytest.raises(SystemExit) as ei:
        _build(tmp_path, gd, aim)
    assert "ZERO rows" in str(ei.value)


def test_builder_idempotent_without_force(tmp_path):
    gd = _write_game(tmp_path)
    aim = tmp_path / "autocam_aim.jsonl"
    _write_aim(aim, AIM_10)
    _build(tmp_path, gd, aim)
    with pytest.raises(SystemExit) as ei:
        _build(tmp_path, gd, aim)
    assert "--force" in str(ei.value)
    rows, _meta = _build(tmp_path, gd, aim, force=True)
    assert len(rows) == 10


def test_builder_empty_view_set_hard_fails(tmp_path):
    gd = _write_game(tmp_path)
    aim = tmp_path / "autocam_aim.jsonl"
    _write_aim(aim, AIM_10)
    sd = tmp_path / "empty_set"
    sd.mkdir()
    (sd / "labels.json").write_text(json.dumps([{"frame_idx": 0, "action": "skip"}]))
    with pytest.raises(SystemExit):
        _build(tmp_path, gd, aim, sets=[sd])


# ---------------------------------------------------------------------------
# Scoreboard composite cells (hand-computed)
# ---------------------------------------------------------------------------

# champ plan: (1100, 500, hfov 42) @ src_w 3840 -> half_w 448, half_h 252.
COMP_ROWS = [
    {"g": 0, "x": 1000.0, "y": 500.0, "tier": "ac", "src": "aim"},
    # dx to champ exactly 448 = half_w -> ON the edge -> inside
    {"g": 8, "x": 1548.0, "y": 500.0, "tier": "ac", "src": "aim", "corroborated": True},
    {"g": 16, "x": 1549.0, "y": 500.0, "tier": "ac", "src": "aim"},  # 449 -> outside
    {"g": 24, "x": 1800.0, "y": 753.0, "tier": "ac", "src": "aim"},  # cap600 misses
    {"g": 100, "x": 1699.5, "y": 500.0, "tier": "ball", "src": "ball_labels.jsonl"},
    {"g": 108, "x": 5000.0, "y": None, "tier": "view", "src": "vset"},
    # dy to champ exactly 252 = half_h -> ON the edge -> inside
    {"g": 200, "x": 1100.0, "y": 752.0, "tier": "ball", "src": "ball_labels.jsonl"},
]


def _write_scoreboard_fixture(tmp_path, comp_rows, meta=True):
    sd = tmp_path / "cset"
    sd.mkdir(exist_ok=True)
    (sd / "labels.json").write_text(
        json.dumps(
            [
                {"frame_idx": 0, "action": "view", "fx": 1000.0, "fy": None},
                {"frame_idx": 8, "action": "view", "fx": 1050.0, "fy": None},
            ]
        )
    )
    (sd / "manifest.json").write_text(
        json.dumps(
            {
                "set": "cset",
                "n_frames": 2,
                "frames": [
                    {"frame_idx": 0, "kind": "?"},
                    {"frame_idx": 8, "kind": "?"},
                ],
            }
        )
    )
    gd = tmp_path / "cgame"
    gd.mkdir(exist_ok=True)
    (gd / "game.json").write_text(
        json.dumps(
            {"segments": [{"seg": "s0", "global_offset": 0, "w": 3840, "h": 1080}]}
        )
    )
    with open(gd / "autocam_viewport.jsonl", "w") as fh:
        for f in range(500):
            fh.write(json.dumps({"seg": "s0", "f": f, "x": 1000.0, "y": 500.0}) + "\n")
    cp = tmp_path / "ccampath.json"
    cp.write_text(
        json.dumps(
            {
                "schema": "camera_path/1",
                "g_start": 0,
                "src_w": 3840,
                "src_h": 1080,
                "fps": 30.0,
                "frames": [[1100.0, 500.0, 42.0]] * 1000,
            }
        )
    )
    comp = tmp_path / "composite.jsonl"
    with open(comp, "w") as fh:
        if meta:
            fh.write(
                json.dumps(
                    {"_meta": {"schema": "composite_reference/1", "ac_source": "aim"}}
                )
                + "\n"
            )
        for r in comp_rows:
            fh.write(json.dumps(r) + "\n")
    return sd, gd, cp, comp


def _run_scoreboard(tmp_path, sd, gd, cp, composite=None, extra=()):
    from training.cli import operator_scoreboard as osb

    out = tmp_path / "report.json"
    argv = [
        "--set-dir",
        str(sd),
        "--game-dir",
        str(gd),
        "--campath",
        f"champ={cp}",
        "--out",
        str(out),
    ]
    if composite is not None:
        argv += ["--composite", str(composite)]
    argv += list(extra)
    osb.main(argv)
    return json.loads(out.read_text())


def test_scoreboard_composite_cells_hand_computed(tmp_path):
    sd, gd, cp, comp_path = _write_scoreboard_fixture(tmp_path, COMP_ROWS)
    report = _run_scoreboard(tmp_path, sd, gd, cp, composite=comp_path)
    comp = report["sets"]["cset"]["composite"]
    assert comp["ac_source"] == "aim"
    assert comp["n_rows"] == 7 and comp["n_match"] == 4 and comp["n_beat"] == 3
    assert comp["n_corroborated"] == 1
    assert comp["banding"] == "all-only"  # no field_polygon
    assert list(comp["cells"].keys()) == ["all"]
    cell = comp["cells"]["all"]
    assert cell["n_match"] == 4 and cell["n_beat"] == 3

    ch = cell["arms"]["champ"]
    # MATCH (tier ac, g 0/8/16/24): |dx| = 100, 448, 449, 700 -> cap600 3/4;
    # containment: in, in (x edge), out (449 > 448), out -> 2/4
    assert ch["match"]["n"] == 4
    assert ch["match"]["cap600"] == pytest.approx(0.75)
    assert ch["match"]["n_contain"] == 4
    assert ch["match"]["contain"] == pytest.approx(0.5)
    # BEAT (g 100/108/200): |dx| = 599.5, 3900, 0 -> cap600 2/3; containment
    # eligible only where the ref has y (g 100 out, g 200 in on the y edge)
    assert ch["beat"]["n"] == 3
    assert ch["beat"]["cap600"] == pytest.approx(2 / 3)
    assert ch["beat"]["n_contain"] == 2
    assert ch["beat"]["contain"] == pytest.approx(0.5)
    # OVERALL pools both columns
    assert ch["overall"]["n"] == 7
    assert ch["overall"]["cap600"] == pytest.approx(5 / 7)
    assert ch["overall"]["n_contain"] == 6
    assert ch["overall"]["contain"] == pytest.approx(0.5)

    # AC viewport arm (x=1000, y=500, no hfov): capture computes, containment
    # is None (no planned view), n_contain 0
    ac = cell["arms"]["AC"]
    assert ac["match"]["n"] == 4
    assert ac["match"]["cap600"] == pytest.approx(0.75)  # 0, 548, 549 in; 800 out
    assert ac["match"]["n_contain"] == 0 and ac["match"]["contain"] is None
    assert ac["beat"]["cap600"] == pytest.approx(1 / 3)  # only g 200 (|100|)
    # the GT-label cells are untouched by the composite instrument
    assert report["sets"]["cset"]["cells"]["ALL"]["all"]["arms"]["champ"]["n"] == 2


def test_scoreboard_without_composite_flag_has_no_composite_block(tmp_path):
    sd, gd, cp, _comp = _write_scoreboard_fixture(tmp_path, COMP_ROWS)
    report = _run_scoreboard(tmp_path, sd, gd, cp)
    assert "composite" not in report["sets"]["cset"]
    assert "null_calibration_composite" not in report


def test_scoreboard_composite_count_mismatch_hard_fails(tmp_path):
    sd, gd, cp, comp_path = _write_scoreboard_fixture(tmp_path, COMP_ROWS)
    with pytest.raises(SystemExit) as ei:
        _run_scoreboard(
            tmp_path,
            sd,
            gd,
            cp,
            composite=comp_path,
            extra=["--composite", str(comp_path)],
        )
    assert ei.value.code not in (0, None)


def test_scoreboard_composite_zero_rows_hard_fails(tmp_path):
    sd, gd, cp, comp_path = _write_scoreboard_fixture(tmp_path, [])
    with pytest.raises(SystemExit) as ei:
        _run_scoreboard(tmp_path, sd, gd, cp, composite=comp_path)
    assert ei.value.code not in (0, None)


def test_scoreboard_composite_unknown_tier_hard_fails(tmp_path):
    bad = [{"g": 0, "x": 1.0, "y": 2.0, "tier": "human", "src": "x"}]
    sd, gd, cp, comp_path = _write_scoreboard_fixture(tmp_path, bad)
    with pytest.raises(SystemExit) as ei:
        _run_scoreboard(tmp_path, sd, gd, cp, composite=comp_path)
    assert ei.value.code not in (0, None)


def test_scoreboard_composite_missing_file_hard_fails(tmp_path):
    sd, gd, cp, _comp = _write_scoreboard_fixture(tmp_path, COMP_ROWS)
    with pytest.raises(SystemExit) as ei:
        _run_scoreboard(tmp_path, sd, gd, cp, composite=tmp_path / "nope.jsonl")
    assert ei.value.code not in (0, None)


# ---------------------------------------------------------------------------
# Composite null calibration (new instrument admission)
# ---------------------------------------------------------------------------

# MATCH: 3 gap-64 singleton events (0/200/400), champ captures + contains all
# -> zero-width bands. BEAT: one event (600) -> explicit power floor.
NULLCAL_ROWS = [
    # MATCH frames span two 600-frame blocks (0 and 1) — the EXP-OP-16
    # amendment's block unit; the old gap-64 event unit would also have split
    # these, but on real dense composites it collapses to ONE event
    {"g": 0, "x": 1100.0, "y": 500.0, "tier": "ac", "src": "aim"},
    {"g": 200, "x": 1100.0, "y": 500.0, "tier": "ac", "src": "aim"},
    {"g": 650, "x": 1100.0, "y": 500.0, "tier": "ac", "src": "aim"},
    {"g": 900, "x": 5000.0, "y": 500.0, "tier": "ball", "src": "ball_labels.jsonl"},
]


def test_block_events_unit():
    assert om.block_events([], 600) == []
    assert om.block_events([1250, 0, 1, 599, 600], 600) == [[0, 1, 599], [600], [1250]]


def test_scoreboard_composite_null_calibration(tmp_path):
    sd, gd, cp, comp_path = _write_scoreboard_fixture(tmp_path, NULLCAL_ROWS)
    report = _run_scoreboard(
        tmp_path, sd, gd, cp, composite=comp_path, extra=["--null-calibration"]
    )
    nc = report["null_calibration_composite"]["cset"]
    assert nc["arm"] == "champ" and nc["reps"] == 300
    assert "block_events(600" in nc["unit"]
    # MATCH column: dense side, BLOCK unit -> 2 blocks, computable; the
    # constant champ makes every half-split identical -> band collapses onto 0
    assert nc["match_capture600"]["band"] == [0.0, 0.0]
    assert nc["match_capture600"]["n_events"] == 2
    assert nc["match_containment"]["band"] == [0.0, 0.0]
    # BEAT column: 1 event -> power floor recorded EXPLICITLY, never silent
    assert nc["beat_capture600"]["band"] is None
    assert nc["beat_capture600"]["power_floor"]["n_events"] == 1
    assert "fewer than 2" in nc["beat_capture600"]["power_floor"]["reason"]
    assert nc["beat_containment"]["band"] is None
    # the framing null-calibration block still exists alongside
    assert "cset" in report["null_calibration"]
