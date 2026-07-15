"""Unit tests for the v4 heatmap detector + target generation."""

import json

import pytest

from training.data_prep.heatmap_dataset import HeatmapCropDataset, gaussian_heatmap
from training.train_v4_heatmap import require_positive_depth


def test_gaussian_heatmap_peak_at_center():
    g = gaussian_heatmap(64, 64, 30, 20, sigma=4.0)
    assert g.shape == (64, 64)
    assert g[20, 30] == pytest.approx(1.0, abs=1e-5)  # (y, x)
    assert g.max() == pytest.approx(1.0, abs=1e-5)
    assert g[0, 0] < 0.01  # far corner ~0


def test_heatmapnet_forward_and_peak():
    torch = pytest.importorskip("torch")
    from training.models.heatmap_net import HeatmapNet, peak_xy

    net = HeatmapNet(in_frames=3, in_ch_per_frame=1, base=8).eval()
    x = torch.zeros(2, 3, 64, 64)
    y = net(x)
    assert tuple(y.shape) == (2, 1, 64, 64)  # same spatial size, single channel

    hm = torch.full((48, 80), -5.0)
    hm[12, 34] = 9.0
    px, py, score = peak_xy(hm)
    assert (px, py) == (34, 12)
    assert score > 0.99


def _mk_store(tmp_path, summary):
    (tmp_path / "crops").mkdir()
    index = {
        "summary": summary,
        "items": [{"file": "a.npy", "x": 10.0, "y": 12.0, "split": "train"}],
    }
    (tmp_path / "index.json").write_text(json.dumps(index))
    return tmp_path


def test_sigma_explicit_cli_wins_over_store_summary(tmp_path):
    # The footgun this guards against: a σ=4 store summary silently overriding a
    # --sigma 3 run into an exact σ=4 replica.
    store = _mk_store(tmp_path, {"crop": 64, "sigma": 4.0})
    assert HeatmapCropDataset(store, "train", sigma=3.0).sigma == 3.0


def test_sigma_none_defers_to_store_summary(tmp_path):
    store = _mk_store(tmp_path, {"crop": 64, "sigma": 6.0})
    assert HeatmapCropDataset(store, "train").sigma == 6.0


def test_sigma_none_without_summary_defaults_to_4(tmp_path):
    store = _mk_store(tmp_path, {"crop": 64})
    assert HeatmapCropDataset(store, "train").sigma == 4.0


def test_require_positive_depth_rejects_partial_store():
    # Partial coverage is the dangerous case: the store LOOKS depth-ready.
    items = [
        {"file": "a.npy", "x": 1.0, "y": 2.0, "split": "train", "depth": 0.3},
        {"file": "b.npy", "x": 3.0, "y": 4.0, "split": "train"},
        {"file": "c.npy", "x": None, "y": None, "split": "train"},
    ]
    with pytest.raises(SystemExit, match="1/2 positive"):
        require_positive_depth(items, "store", "--dynamic-sigma")


def test_require_positive_depth_accepts_full_coverage():
    items = [
        {"file": "a.npy", "x": 1.0, "y": 2.0, "split": "train", "depth": 0.3},
        {"file": "c.npy", "x": None, "y": None, "split": "train"},  # negatives exempt
    ]
    require_positive_depth(items, "store", "--dynamic-sigma")
