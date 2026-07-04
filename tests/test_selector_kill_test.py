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

    def test_feature_mask(self):
        keep = feature_mask(["score"])
        assert keep.sum() == len(FEATURE_NAMES) - len(FEATURE_FAMILIES["score"])
        assert not keep[FEATURE_NAMES.index("pct_depth")]
        assert keep[FEATURE_NAMES.index("persistence")]


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
