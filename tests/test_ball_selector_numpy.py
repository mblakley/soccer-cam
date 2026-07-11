"""Numpy selector inference: forward math, packing, artifact loading."""

from __future__ import annotations

import numpy as np
import pytest

from video_grouper.inference.ball_selector import (
    FEATURE_NAMES,
    SelectorNet,
    feature_mask,
    load_selector,
    pack_frames,
    predict_probs,
)


def _tiny_net(n_features=3, hidden=4, emb=2, temperature=1.0, seed=0) -> SelectorNet:
    rng = np.random.default_rng(seed)

    def w(o, i):
        return rng.normal(scale=0.5, size=(o, i)).astype(np.float32)

    def b(o):
        return rng.normal(scale=0.1, size=(o,)).astype(np.float32)

    return SelectorNet(
        w0=w(hidden, n_features),
        b0=b(hidden),
        w1=w(hidden, hidden),
        b1=b(hidden),
        w2=w(emb, hidden),
        b2=b(emb),
        head_w=w(1, emb),
        head_b=b(1),
        none_w=w(1, 2 * emb),
        none_b=b(1),
        temperature=temperature,
        keep=np.ones(len(FEATURE_NAMES), bool),
    )


def test_probs_are_a_distribution_and_mask_kills_candidates():
    net = _tiny_net()
    feats = np.random.default_rng(1).normal(size=(5, 4, 3)).astype(np.float32)
    mask = np.ones((5, 4), bool)
    mask[:, 2:] = False  # only 2 candidates live per frame
    probs = predict_probs(net, feats, mask)
    assert probs.shape == (5, 5)
    np.testing.assert_allclose(probs.sum(axis=1), 1.0, atol=1e-5)
    assert np.all(probs[:, 2:4] == 0.0)  # masked candidates get zero probability
    assert np.all(probs[:, 4] > 0.0)  # none-logit always alive


def test_temperature_flattens_the_distribution():
    feats = np.random.default_rng(2).normal(size=(8, 6, 3)).astype(np.float32)
    mask = np.ones((8, 6), bool)
    p1 = predict_probs(_tiny_net(temperature=1.0), feats, mask)
    p4 = predict_probs(_tiny_net(temperature=4.0), feats, mask)
    assert p4.max() < p1.max()


def test_pack_frames_pads_and_masks():
    feats, mask = pack_frames(
        [np.ones((2, 3), np.float32), np.ones((5, 3), np.float32)], top_k=4
    )
    assert feats.shape == (2, 4, 3)
    assert mask.tolist() == [[True, True, False, False], [True] * 4]
    assert feats[1, 3, 0] == 1.0  # 5 candidates truncated to top_k=4


def test_pack_width_must_fit_candidates_to_avoid_priors_misalignment():
    # If the pack width is below a frame's candidate count, pack_frames truncates
    # while the downstream priors are sliced by the full candidate count -> a
    # shorter priors row than the frame's candidates -> misaligned/overrun
    # emission in rerank. ball_select expands the pack width to fit; this locks the
    # invariant that the fit width retains every candidate.
    feats = [np.zeros((3, 4), np.float32)]
    _, mask_small = pack_frames(feats, top_k=2)
    assert mask_small[0].sum() == 2  # HAZARD: 3rd candidate dropped
    pack_k = max(2, max(len(x) for x in feats))  # the step's expand-to-fit rule
    _, mask_fit = pack_frames(feats, top_k=pack_k)
    assert mask_fit[0].sum() == 3  # every candidate retained -> priors align


def test_feature_mask_family_and_single():
    m = feature_mask(["size_ratio"])
    assert m.sum() == len(FEATURE_NAMES) - 1
    m = feature_mask(["window"])
    assert m.sum() == len(FEATURE_NAMES) - 4
    with pytest.raises(KeyError):
        feature_mask(["nope"])


def test_load_selector_rejects_wrong_schema(tmp_path):
    p = tmp_path / "bad.npz"
    np.savez(p, schema="something_else", w0=np.zeros((2, 2)))
    with pytest.raises(ValueError, match="selector_net_npz/1"):
        load_selector(p)


def test_load_selector_round_trip(tmp_path):
    net = _tiny_net()
    p = tmp_path / "net.npz"
    np.savez(
        p,
        schema="selector_net_npz/1",
        w0=net.w0,
        b0=net.b0,
        w1=net.w1,
        b1=net.b1,
        w2=net.w2,
        b2=net.b2,
        head_w=net.head_w,
        head_b=net.head_b,
        none_w=net.none_w,
        none_b=net.none_b,
        temperature=np.float32(net.temperature),
        keep=net.keep,
    )
    loaded = load_selector(p)
    feats = np.random.default_rng(3).normal(size=(4, 3, 3)).astype(np.float32)
    mask = np.ones((4, 3), bool)
    np.testing.assert_allclose(
        predict_probs(loaded, feats, mask), predict_probs(net, feats, mask), atol=1e-6
    )
