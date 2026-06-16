"""Unit tests for the v4 heatmap detector + target generation."""

import pytest

from training.data_prep.heatmap_dataset import gaussian_heatmap


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
