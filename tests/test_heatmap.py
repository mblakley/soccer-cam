"""Unit tests for the v4 heatmap detector + target generation."""

import json

import numpy as np
import pytest

from training.data_prep.heatmap_dataset import HeatmapCropDataset, gaussian_heatmap
from training.train_v4_heatmap import require_positive_depth

# torch MUST be imported at collection time: conftest's autouse mock_file_system
# patches os.path.exists globally, and torch's Windows DLL-path setup calls it
# during first import — importing torch inside a test therefore crashes.
try:
    import torch
except ImportError:
    torch = None

needs_torch = pytest.mark.skipif(torch is None, reason="torch not installed")


def test_gaussian_heatmap_peak_at_center():
    g = gaussian_heatmap(64, 64, 30, 20, sigma=4.0)
    assert g.shape == (64, 64)
    assert g[20, 30] == pytest.approx(1.0, abs=1e-5)  # (y, x)
    assert g.max() == pytest.approx(1.0, abs=1e-5)
    assert g[0, 0] < 0.01  # far corner ~0


@needs_torch
def test_heatmapnet_forward_and_peak():
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


@needs_torch
def test_encoding_prelude_gray3_is_identity():
    from training.models.heatmap_net import EncodingPrelude

    x = torch.rand(2, 3, 16, 16)
    out = EncodingPrelude("gray3")(x)
    assert out is x  # exact identity — the byte-identical default path


@needs_torch
def test_encoding_prelude_diff3_math():
    from training.models.heatmap_net import EncodingPrelude

    x = torch.rand(2, 3, 16, 16)
    g0, g1, g2 = x[:, 0:1], x[:, 1:2], x[:, 2:3]
    out = EncodingPrelude("diff3")(x)
    assert torch.equal(out[:, 0:1], g2)  # appearance = frame t
    assert torch.equal(out[:, 1:2], g1 - g0)
    assert torch.equal(out[:, 2:3], g2 - g1)


@needs_torch
def test_encoding_prelude_rejects_unknown():
    from training.models.heatmap_net import EncodingPrelude

    with pytest.raises(ValueError, match="unknown encoding"):
        EncodingPrelude("rgb9")


@needs_torch
def test_default_encoding_byte_identical_to_bare_net():
    from training.models.heatmap_net import (
        DetectorWithEncoding,
        EncodingPrelude,
        HeatmapNet,
    )

    net = HeatmapNet(in_frames=3, in_ch_per_frame=1, base=8).eval()
    x = torch.rand(1, 3, 32, 32)
    with torch.no_grad():
        assert torch.equal(
            DetectorWithEncoding(EncodingPrelude("gray3"), net)(x), net(x)
        )


@needs_torch
def test_load_detector_checkpoint_roundtrip(tmp_path):
    from training.models.heatmap_net import HeatmapNet, load_detector_checkpoint

    net = HeatmapNet(in_frames=3, in_ch_per_frame=1, base=8).eval()
    ck = tmp_path / "best.pt"
    torch.save(
        {
            "model": net.state_dict(),
            "epoch": 3,
            "encoding": "diff3",
            "base": 8,
            "out_ch": 1,
        },
        ck,
    )
    model, meta = load_detector_checkpoint(ck)
    assert meta == {"encoding": "diff3", "base": 8, "out_ch": 1}
    x = torch.rand(1, 3, 32, 32)
    with torch.no_grad():
        # composed model = net(prelude(x)), on raw frames
        assert torch.equal(model(x), net(model.prelude(x)))

    # legacy checkpoint (bare state dict, no metadata) -> gray3
    ck2 = tmp_path / "legacy.pt"
    torch.save(net.state_dict(), ck2)
    model2, meta2 = load_detector_checkpoint(ck2)
    assert meta2["encoding"] == "gray3"
    with torch.no_grad():
        assert torch.equal(model2(x), net(x))

    # explicit base contradicting the state dict is a hard error
    with pytest.raises(ValueError, match="base=8"):
        load_detector_checkpoint(ck, base=24)


def test_person_center_heatmap():
    from training.data_prep.heatmap_dataset import person_center_heatmap

    # empty -> zeros
    assert person_center_heatmap(64, 64, []).max() == 0.0
    # one 40px-tall box -> peak 1.0 at its center, sigma = 40/8 = 5
    hm = person_center_heatmap(64, 64, [[20.0, 10.0, 40.0, 50.0, 0.9]])
    assert hm[30, 30] == pytest.approx(1.0, abs=1e-5)  # center (30, 30) = (y, x)
    g = gaussian_heatmap(64, 64, 30, 30, 5.0)
    assert np.allclose(hm, g, atol=1e-6)
    # two boxes max-composite: both peaks are 1.0
    hm2 = person_center_heatmap(64, 64, [[0, 0, 10, 16, 0.5], [40, 40, 60, 62, 0.8]])
    assert hm2[8, 5] == pytest.approx(1.0, abs=1e-5)
    assert hm2[51, 50] == pytest.approx(1.0, abs=1e-5)
    # tiny box: sigma clamps at 4; huge box clamps at 12
    tiny = person_center_heatmap(64, 64, [[0, 0, 8, 8, 0.5]])
    assert np.allclose(tiny, gaussian_heatmap(64, 64, 4, 4, 4.0), atol=1e-6)


@needs_torch
def test_heatmapnet_out_ch2_forward_and_loader(tmp_path):
    from training.models.heatmap_net import HeatmapNet, load_detector_checkpoint

    net = HeatmapNet(in_frames=3, in_ch_per_frame=1, base=8, out_ch=2).eval()
    x = torch.rand(1, 3, 32, 32)
    assert tuple(net(x).shape) == (1, 2, 32, 32)

    ck = tmp_path / "ph.pt"
    torch.save(
        {
            "model": net.state_dict(),
            "epoch": 1,
            "encoding": "diff3",
            "base": 8,
            "out_ch": 2,
        },
        ck,
    )
    model, meta = load_detector_checkpoint(ck)
    assert meta["out_ch"] == 2
    with torch.no_grad():
        out = model(x)
    assert tuple(out.shape) == (1, 2, 32, 32)


def test_dataset_variants_are_picklable(tmp_path):
    # Windows DataLoader workers spawn + pickle the dataset: a class defined
    # inside main() dies with "Can't get local object". Guard module-level-ness.
    import pickle

    from training.train_v4_heatmap import (
        _DepthDataset,
        _DynSigmaDataset,
        _PersonDataset,
    )

    store = _mk_store(tmp_path, {"crop": 64, "sigma": 4.0})
    for cls in (_DepthDataset, _DynSigmaDataset, _PersonDataset):
        ds = cls(store, "train")
        blob = pickle.dumps(ds)
        assert pickle.loads(blob).crop == 64


def test_store_versioning_freeze_and_resolve(tmp_path):
    import json as _json

    from training.data_prep.store_versions import (
        canonical_sha,
        freeze_index,
        resolve_index,
    )

    # sha is key-order independent
    assert canonical_sha({"a": 1, "b": 2}) == canonical_sha({"b": 2, "a": 1})

    store = _mk_store(tmp_path, {"crop": 64, "sigma": 4.0})
    v1, s1 = freeze_index(store)
    assert v1 == 1
    # idempotent: same content -> same version
    assert freeze_index(store) == (v1, s1)

    # mutate the store in place (the EXP-DIST-55 hazard) -> next freeze = v2
    d = _json.loads((store / "index.json").read_text())
    d["items"].append({"file": "b.npy", "x": None, "y": None, "split": "train"})
    (store / "index.json").write_text(_json.dumps(d))
    v2, s2 = freeze_index(store)
    assert v2 == 2 and s2 != s1

    # v1 is immutable and still resolvable to the PRE-mutation content
    old, ver, sha = resolve_index(store, version=1)
    assert ver == 1 and sha == s1
    assert len(old["items"]) == 1


def test_dataset_records_and_honors_index_version(tmp_path):
    import json as _json

    store = _mk_store(tmp_path, {"crop": 64, "sigma": 4.0})
    ds = HeatmapCropDataset(store, "train")
    assert ds.index_version == 1 and len(ds.index_sha) == 16
    assert len(ds.items) == 1

    # mutate the store; a pinned-version dataset must still see the OLD data
    d = _json.loads((store / "index.json").read_text())
    d["items"].append({"file": "c.npy", "x": None, "y": None, "split": "train"})
    (store / "index.json").write_text(_json.dumps(d))

    ds_new = HeatmapCropDataset(store, "train")  # current -> v2, sees 2 items
    assert ds_new.index_version == 2 and len(ds_new.items) == 2
    ds_pinned = HeatmapCropDataset(store, "train", index_version=1)
    assert ds_pinned.index_version == 1 and len(ds_pinned.items) == 1


@needs_torch
def test_encoding_prelude_diff5_keeps_grays_and_adds_diffs():
    from training.models.heatmap_net import (
        DetectorWithEncoding,
        EncodingPrelude,
        HeatmapNet,
    )

    x = torch.rand(2, 3, 16, 16)
    p = EncodingPrelude("diff5")
    out = p(x)
    assert p.out_channels == 5 and out.shape[1] == 5
    assert torch.equal(out[:, 0:3], x)  # all gray frames preserved
    assert torch.equal(out[:, 3:4], x[:, 1:2] - x[:, 0:1])
    assert torch.equal(out[:, 4:5], x[:, 2:3] - x[:, 1:2])

    # 5-ch net composes on RAW 3-frame input, and round-trips via the loader
    net = HeatmapNet(in_frames=5, in_ch_per_frame=1, base=8).eval()
    model = DetectorWithEncoding(p, net)
    with torch.no_grad():
        assert tuple(model(x).shape) == (2, 1, 16, 16)
