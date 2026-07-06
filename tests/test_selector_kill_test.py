"""Selector kill-test plumbing: context features, teacher-snap labels, listwise net.

Synthetic trapezoid field (parallel touchlines + linear spacing = exact affine
homography). Net tests skip when torch isn't installed (it's an optional extra;
training runs on the GPU box)."""

import numpy as np
import pytest

from training.cli.build_selector_labels import snap_teacher_to_candidates
from training.cli.kill_test_selector import split_train_pair
from training.world_model.geometry import build_field_geometry
from training.world_model.selector_features import (
    FEATURE_FAMILIES,
    FEATURE_NAMES,
    build_features,
    feature_mask,
)
from training.world_model.tbd import Candidate


def _geom():
    near_x = np.linspace(100.0, 1900.0, 5)
    far_x = np.linspace(1600.0, 400.0, 5)
    poly = np.concatenate(
        [
            np.column_stack([near_x, np.full(5, 1000.0)]),
            np.column_stack([far_x, np.full(5, 200.0)]),
        ]
    )
    geom = build_field_geometry(poly)
    assert geom.valid
    return geom


def _cand(x, y, score, size=None):
    return Candidate(x=x, y=y, score=score, size_px=size)


class TestFeatures:
    def test_shapes_and_names(self):
        geom = _geom()
        frames = [
            [_cand(1000, 900, 0.9), _cand(500, 950, 0.4)],
            [],
            [_cand(1010, 905, 0.7)],
        ]
        feats = build_features(frames, geom)
        assert len(feats) == 3
        assert feats[0].shape == (2, len(FEATURE_NAMES))
        assert feats[1].shape == (0, len(FEATURE_NAMES))
        assert all(np.isfinite(x).all() for x in feats)

    def test_rank_and_pct(self):
        geom = _geom()
        frames = [[_cand(1000, 900, 0.9), _cand(500, 950, 0.4), _cand(700, 920, 0.1)]]
        f = build_features(frames, geom)[0]
        i_rank = FEATURE_NAMES.index("rank_norm")
        i_pct = FEATURE_NAMES.index("pct_frame")
        assert f[0, i_rank] == 0.0  # top-scored candidate
        assert f[0, i_pct] == 1.0
        assert f[2, i_pct] < f[1, i_pct] < f[0, i_pct]

    def test_window_continuity(self):
        geom = _geom()
        # candidate 0 persists across frames; candidate 1 appears once far away
        frames = [
            [_cand(1000, 900, 0.5)],
            [_cand(1005, 902, 0.5), _cand(300, 300, 0.5)],
            [_cand(1010, 904, 0.5)],
        ]
        f = build_features(frames, geom)[1]
        i_p1 = FEATURE_NAMES.index("cont_p1")
        i_m1 = FEATURE_NAMES.index("cont_m1")
        assert f[0, i_p1] < f[1, i_p1]  # persistent candidate has support next frame
        assert f[0, i_m1] < f[1, i_m1]

    def test_window_continuity_stride_invariant(self):
        """selector-v2 regression: stride-8 training dumps vs stride-4 eval dumps must
        produce the SAME continuity feature for the same physical motion when ef is
        given (meters per FRAME, not per dump step)."""
        geom = _geom()
        # same ball drifting 10 px/frame along the near touchline, sampled two ways
        s8 = [[_cand(1000 + 80 * i, 900, 0.5)] for i in range(3)]
        s4 = [[_cand(1000 + 40 * i, 900, 0.5)] for i in range(3)]
        f8 = build_features(s8, geom, ef=[0, 8, 16])[1]
        f4 = build_features(s4, geom, ef=[0, 4, 8])[1]
        i_p1 = FEATURE_NAMES.index("cont_p1")
        i_m1 = FEATURE_NAMES.index("cont_m1")
        assert np.isclose(f8[0, i_p1], f4[0, i_p1], rtol=1e-5)
        assert np.isclose(f8[0, i_m1], f4[0, i_m1], rtol=1e-5)
        # without ef the two strides disagree (the old, broken behavior)
        g8 = build_features(s8, geom)[1]
        g4 = build_features(s4, geom)[1]
        assert not np.isclose(g8[0, i_p1], g4[0, i_p1], rtol=1e-2)

    def test_feature_mask(self):
        keep = feature_mask(["score"])
        assert keep.sum() == len(FEATURE_NAMES) - len(FEATURE_FAMILIES["score"])
        assert not keep[FEATURE_NAMES.index("pct_depth")]
        assert keep[FEATURE_NAMES.index("persistence")]

    def test_feature_mask_single_feature(self):
        keep = feature_mask(["size_ratio"])
        assert not keep[FEATURE_NAMES.index("size_ratio")]
        assert keep.sum() == len(FEATURE_NAMES) - 1
        with pytest.raises(KeyError):
            feature_mask(["not_a_feature"])


class TestSnap:
    def _setup(self):
        geom = _geom()
        ef = [100, 104, 108, 112]
        cands = {
            100: [(1000.0, 900.0, 0.9, None), (500.0, 950.0, 0.4, None)],
            104: [(1010.0, 902.0, 0.8, None)],
            108: [(300.0, 300.0, 0.5, None)],
            112: [(1030.0, 906.0, 0.7, None)],
        }
        return geom, ef, cands

    def test_snap_and_none(self):
        geom, ef, cands = self._setup()
        teacher = {100: (1001.0, 901.0), 104: (1011.0, 903.0), 112: (1031.0, 907.0)}
        labels, stats = snap_teacher_to_candidates(
            ef, cands, teacher, {}, {108}, geom, [], stability_k=0
        )
        assert labels[0][0] == 0 and labels[1][0] == 0
        assert labels[2] == (-1, 20.0)  # human not_visible -> none @ gold weight
        assert stats["ball"] == 3 and stats["none"] == 1

    def test_detector_miss_skipped(self):
        geom, ef, cands = self._setup()
        teacher = {108: (1500.0, 900.0)}  # far from the only 108 candidate (300, 300)
        labels, stats = snap_teacher_to_candidates(
            ef, cands, teacher, {}, set(), geom, [], stability_k=0
        )
        assert 2 not in labels
        assert stats["skip_missed"] == 1

    def test_gold_weight_and_play_gate(self):
        geom, ef, cands = self._setup()
        teacher = {100: (1000.0, 900.0), 104: (1010.0, 902.0)}
        labels, stats = snap_teacher_to_candidates(
            ef,
            cands,
            teacher,
            {100: (1000.0, 900.0)},
            set(),
            geom,
            [(0, 102)],  # only frame 100 is in active play
            stability_k=0,
        )
        assert labels[0] == (0, 20.0) and stats["gold"] == 1
        assert 1 not in labels and stats["skip_outofplay"] >= 1

    def test_phase_offset_grid_interpolates(self):
        """The marathon's detections sit on the 0-mod-4 grid; a dump's ef grid can be
        phase-shifted (Cleveland: ef ≡ 2 mod 4 -> ZERO exact hits). The teacher must
        interpolate onto the ef frames instead of exact-key matching."""
        geom = _geom()
        ef = [102, 106]  # ≡ 2 mod 4
        cands = {
            102: [(1010.0, 901.0, 0.8, None)],
            106: [(1030.0, 903.0, 0.7, None)],
        }
        # teacher on the 0-mod-4 grid, ball drifting +5px/frame
        teacher = {100: (1000.0, 900.0), 104: (1020.0, 902.0), 108: (1040.0, 904.0)}
        labels, stats = snap_teacher_to_candidates(
            ef, cands, teacher, {}, set(), geom, [], stability_k=0
        )
        assert labels[0][0] == 0 and labels[1][0] == 0
        assert stats["ball"] == 2 and stats["skip_nocover"] == 0

    def test_interp_span_gate(self):
        """No interpolation across a teacher gap wider than interp_max_span."""
        geom = _geom()
        ef = [102]
        cands = {102: [(1010.0, 901.0, 0.8, None)]}
        teacher = {100: (1000.0, 900.0), 120: (1100.0, 910.0)}  # 20-frame gap
        labels, stats = snap_teacher_to_candidates(
            ef, cands, teacher, {}, set(), geom, [], stability_k=0
        )
        assert labels == {}
        assert stats["skip_nocover"] == 1

    def test_unstable_dropped(self):
        geom, ef, cands = self._setup()
        # teacher jumps ~60 m between 104 and 108 -> discontinuity; both ends dropped
        teacher = {100: (1000.0, 900.0), 104: (1010.0, 902.0), 108: (300.0, 300.0)}
        labels, stats = snap_teacher_to_candidates(
            ef, cands, teacher, {}, set(), geom, [], stability_k=1
        )
        assert 2 not in labels
        assert stats["skip_unstable"] >= 1


def test_split_train_pair_windows_paths():
    """Drive colons must not be mistaken for the pair separator (the stage-2 crash)."""
    d, la = split_train_pair(r"G:\sel\cands_a.pkl;G:\sel\labels_a.json")
    assert d == r"G:\sel\cands_a.pkl" and la == r"G:\sel\labels_a.json"
    with pytest.raises(SystemExit):
        split_train_pair(r"G:\sel\cands_a.pkl:G:\sel\labels_a.json")


class TestNet:
    def test_train_and_predict(self):
        pytest.importorskip("torch")
        from training.models.selector_net import (
            pack_frames,
            predict_probs,
            train_selector,
        )

        rng = np.random.default_rng(0)
        n, k, f = 400, 4, 3
        feats_list, labels = [], []
        for _ in range(n):
            x = rng.normal(size=(k, f)).astype(np.float32)
            j = int(rng.integers(k))
            x[j, 0] += 3.0  # feature 0 marks the ball
            feats_list.append(x)
            labels.append(j)
        feats, mask = pack_frames(feats_list, top_k=k)
        net, hist = train_selector(
            feats,
            mask,
            np.asarray(labels),
            np.ones(n, np.float32),
            epochs=30,
            seed=0,
        )
        probs = predict_probs(net, feats, mask)
        assert probs.shape == (n, k + 1)
        assert np.allclose(probs.sum(axis=1), 1.0, atol=1e-4)
        acc = (probs[:, :k].argmax(axis=1) == np.asarray(labels)).mean()
        assert acc > 0.9, f"separable toy task should be learned, got {acc}"


def test_mine_index_roundtrip_both_forms(tmp_path):
    """hn1/hn2 mining crashed on the dict-form index (append on a dict) and silently
    added ZERO crops — both store forms must round-trip and preserve the summary."""
    from training.cli.mine_hard_negatives import load_index, save_index

    d = tmp_path / "store_dict"
    d.mkdir()
    (d / "index.json").write_text(
        '{"summary": {"crop": 256, "samples": 2}, "items": [{"file": "a"}, {"file": "b"}]}'
    )
    items, wrapper = load_index(d)
    items.append({"file": "c"})
    save_index(d, items, wrapper)
    import json

    back = json.loads((d / "index.json").read_text())
    assert back["summary"]["samples"] == 3
    assert [r["file"] for r in back["items"]] == ["a", "b", "c"]

    ls = tmp_path / "store_list"
    ls.mkdir()
    (ls / "index.json").write_text('[{"file": "a"}]')
    items, wrapper = load_index(ls)
    assert wrapper is None
    items.append({"file": "b"})
    save_index(ls, items, wrapper)
    assert json.loads((ls / "index.json").read_text()) == [
        {"file": "a"},
        {"file": "b"},
    ]


def test_context_entries_rank_colors_and_inverse_warp():
    """Top-5 candidates render blue (df<0), rest orange; band->source mapping matches
    the miner's inverse warp (sx = hx/scale, sy = hy/scale + y_top)."""
    from types import SimpleNamespace

    from training.cli.inject_set_candidates import context_entries

    warp = SimpleNamespace(scale=2.0, y_top=100.0)
    peaks = [(200.0, 50.0, 0.9)] + [(10.0 * i, 10.0, 0.5) for i in range(6)]
    ctx, cands = context_entries(peaks, warp)
    assert ctx[0] == {"x": 100.0, "y": 125.0, "df": -1}
    assert cands[0] == [100.0, 125.0, 0.9]
    assert [c["df"] for c in ctx] == [-1] * 5 + [1] * 2


class TestStaticDistractors:
    def test_static_cluster_found_and_mover_ignored(self):
        from training.cli.mine_static_distractors import find_static_clusters

        rng = np.random.default_rng(0)
        det = {}
        for g in range(100):
            pts = [[10.0 + rng.normal(0, 0.2), 20.0 + rng.normal(0, 0.2)]]  # static
            pts.append([g * 1.0, 30.0])  # mover: new cell every other frame
            det[g] = np.asarray(pts)
        cl = find_static_clusters(det, min_occ=0.5)
        assert len(cl) == 1
        assert abs(cl[0]["wx"] - 10.0) < 0.5 and cl[0]["occupancy"] > 0.9

    def test_restart_dwell_below_threshold(self):
        from training.cli.mine_static_distractors import find_static_clusters

        det = {g: np.asarray([[50.0, 30.0]]) for g in range(10)}  # short dwell
        det.update({g: np.asarray([[g * 2.0, 5.0]]) for g in range(10, 100)})
        assert find_static_clusters(det, min_occ=0.15) == []

    def test_confirm_drops_ball_visited_cells(self):
        from training.cli.mine_static_distractors import confirm_clusters

        clusters = [
            {"wx": 10.0, "wy": 20.0, "occupancy": 0.9, "n_dets": 90, "n_frames": 90},
            {"wx": 40.0, "wy": 30.0, "occupancy": 0.5, "n_dets": 50, "n_frames": 50},
        ]
        ball = np.asarray([[40.5, 30.2]])  # the game ball visits the 2nd cell
        out = confirm_clusters(clusters, ball, np.zeros((0, 2)))
        assert len(out) == 1
        assert out[0]["wx"] == 10.0 and out[0]["confirmed_by"] == "label_confirmed"

    def test_teacher_only_tier(self):
        from training.cli.mine_static_distractors import confirm_clusters

        clusters = [
            {"wx": 10.0, "wy": 20.0, "occupancy": 0.9, "n_dets": 90, "n_frames": 90}
        ]
        out = confirm_clusters(clusters, np.zeros((0, 2)), np.asarray([[70.0, 40.0]]))
        assert out[0]["confirmed_by"] == "teacher_only"


class TestFullGameDump:
    def test_sample_grid_aligned_and_ranged(self):
        from training.cli.dump_game_candidates import sample_grid

        grid = sample_grid([(10, 50), (100, 120)], stride=8, total_frames=200)
        assert all(g % 8 == 0 for g in grid)
        assert grid[0] == 16 and grid[-1] == 112
        assert all(10 <= g < 50 or 100 <= g < 120 for g in grid)
        # no ranges -> whole game
        assert sample_grid([], 8, 40) == [0, 8, 16, 24, 32]

    def test_chunk_spans(self):
        from training.cli.dump_game_candidates import chunk_spans

        grid = list(range(0, 80, 8))
        spans = chunk_spans(grid, chunk=4)
        assert [(s, e) for s, e, _ in spans] == [(0, 24), (32, 56), (64, 72)]
        assert sum(len(f) for _, _, f in spans) == len(grid)


def test_gold_anchors_to_nearest_grid_frame():
    """Cleveland regression: ALL its human labels sit ≡4 mod 8 — zero exact hits on a
    stride-8 grid. Gold must anchor at the nearest grid frame within ±gold_tol."""
    geom = _geom()
    ef = [100, 108]
    cands = {
        100: [(1000.0, 900.0, 0.9, None)],
        108: [(1020.0, 902.0, 0.8, None)],
    }
    teacher = {100: (1000.0, 900.0), 108: (1020.0, 902.0)}
    # human ball click at 104 (off-grid), position matches the ball's path
    labels, stats = snap_teacher_to_candidates(
        ef, cands, teacher, {104: (1010.0, 901.0)}, set(), geom, [], stability_k=0
    )
    assert stats["gold"] == 1
    assert labels[0][1] == 20.0 or labels[1][1] == 20.0


class TestNearQueue:
    def test_select_near_frames(self):
        from training.cli.build_far_label_queue import select_near_frames

        geom = _geom()
        # near touchline (y~950) has a big expected diameter, far line (y~250) small
        exp_near = float(
            geom.expected_ball_diameter_px(np.asarray([[1000.0, 950.0]]))[0]
        )
        exp_far = float(
            geom.expected_ball_diameter_px(np.asarray([[1000.0, 250.0]]))[0]
        )
        assert exp_near > exp_far
        near_px = (exp_near + exp_far) / 2.0
        ef = [0, 8, 16, 24, 32]
        cands = {
            # teacher ball (idx 1) OUTSCORED by a distractor -> near_misrank
            0: [(500.0, 950.0, 0.9), (1000.0, 950.0, 0.6)],
            # teacher ball already top-scored -> skipped
            8: [(1000.0, 950.0, 0.9), (500.0, 950.0, 0.5)],
            # no teacher coverage, near candidates exist -> near_unknown
            16: [(900.0, 950.0, 0.7)],
            # teacher ball is FAR -> skipped
            24: [(1000.0, 250.0, 0.4), (900.0, 950.0, 0.3)],
            # misrank but excluded (already human-labeled)
            32: [(500.0, 950.0, 0.9), (1000.0, 950.0, 0.6)],
        }
        labels = {0: (1, 1.0), 1: (0, 1.0), 3: (0, 1.0), 4: (1, 1.0)}
        out = select_near_frames(
            ef, cands, labels, geom, near_px=near_px, target=10, exclude={32}
        )
        by_reason = {e["reason"]: e for e in out}
        assert set(by_reason) == {"near_misrank", "near_unknown"}
        mr = by_reason["near_misrank"]
        assert mr["frame_idx"] == 0 and mr["hint_x"] == 1000.0  # hint = teacher ball
        assert mr["autocam"] is True
        assert by_reason["near_unknown"]["frame_idx"] == 16

    def test_spread_bins_keeps_best_per_bin(self):
        from training.cli.build_far_label_queue import _spread_bins

        pool = [[0, "a", 1.0], [1, "b", 5.0], [100, "c", 2.0]]
        out = _spread_bins(pool, target=2)
        assert [r[1] for r in out] == ["b", "c"]  # b beats a in the first bin


class TestLoadDumpAndGuards:
    def test_load_dump_fullgame_dir(self, tmp_path):
        """v2-scale training pairs are marathon DIRECTORIES: part_*.pkl + meta.json,
        polygon pulled from the game.json that meta.game_dir points at."""
        import pickle

        from training.cli.kill_test_selector import _load_dump

        game_dir = tmp_path / "game"
        game_dir.mkdir()
        near_x = np.linspace(100.0, 1900.0, 5)
        far_x = np.linspace(1600.0, 400.0, 5)
        poly = np.concatenate(
            [
                np.column_stack([near_x, np.full(5, 1000.0)]),
                np.column_stack([far_x, np.full(5, 200.0)]),
            ]
        ).tolist()
        (game_dir / "game.json").write_text(
            __import__("json").dumps({"field_polygon": poly, "segments": []})
        )
        fg = tmp_path / "fullgame"
        fg.mkdir()
        (fg / "meta.json").write_text(
            __import__("json").dumps(
                {"schema": "fullgame_candidates/1", "game_dir": str(game_dir)}
            )
        )
        with open(fg / "part_0000000_0000008.pkl", "wb") as fh:
            pickle.dump({0: [(1000.0, 900.0, 0.9)], 8: [(1020.0, 902.0, 0.8)]}, fh)
        d, frames, geom = _load_dump(str(fg))
        assert d["ef"] == [0, 8]
        assert geom.valid
        assert len(frames) == 2 and frames[0][0].score == 0.9
        assert frames[0][0].size_px is None  # padded, reads as "size unknown"

    def test_held_out_guard(self):
        from training.cli.kill_test_selector import check_not_held_out

        with pytest.raises(SystemExit, match="HELD-OUT"):
            check_not_held_out(
                "G:/x/fullgame/spc", "F:/Heat_2012s/2026.05.31 - vs Spencerport"
            )
        with pytest.raises(SystemExit, match="HELD-OUT"):
            check_not_held_out("G:/x/dump.pkl", "F:/x/2026.06.15 - vs Irondequoit")
        # Irondequoit 06.04 is a legitimate training game — must NOT trip the guard
        check_not_held_out(
            "G:/x/fullgame/iron0604", "F:/Heat_2012s/2026.06.04 - vs Irondequoit (away)"
        )
