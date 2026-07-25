"""Round-trip + index tests for the v4 warped-frame shard format.

Torch-free and video-free: exercises the pure-numpy/cv2 shard writer + reader
core (`write_shard` / `WarpedShard`). The torch `WarpedShardDataset` and the
PyAV `build_warped_shard` integration path are validated on the GPU server.
"""

import json

import numpy as np
import pytest

from training.data_prep import warped_pack as wp


def _frames(n, h, w, c=3, seed=0):
    rng = np.random.default_rng(seed)
    return [rng.integers(0, 256, size=(h, w, c), dtype=np.uint8) for _ in range(n)]


def test_raw_roundtrip_exact(tmp_path):
    # 64x96 are already stride-multiples, so no padding: pixels round-trip exactly.
    frames = _frames(5, 64, 96)
    info = wp.write_shard(frames, tmp_path, "shard0", storage="raw")
    assert info.storage == "raw"
    assert info.n == 5
    assert (info.frame_h, info.frame_w, info.channels) == (64, 96, 3)
    assert info.data_path.name == "shard0.dat"

    shard = wp.WarpedShard(info.path)
    assert len(shard) == 5
    for i, f in enumerate(frames):
        np.testing.assert_array_equal(shard.get_frame(i), f)
    shard.close()


def test_compressed_roundtrip_exact(tmp_path):
    # Compressed mode uses lossless PNG, so it must round-trip exactly too.
    frames = _frames(4, 64, 64, seed=7)
    info = wp.write_shard(frames, tmp_path, "shardc", storage="compressed")
    assert info.storage == "compressed"
    assert info.data_path.name == "shardc.blobs"
    assert len(info.blob_offsets) == 4

    shard = wp.WarpedShard(info.path)
    for i, f in enumerate(frames):
        np.testing.assert_array_equal(shard.get_frame(i), f)
    shard.close()


def test_pad_to_stride(tmp_path):
    # 100x200 -> padded up to 128x224 (next multiples of 32), bottom/right zeros.
    frame = np.full((100, 200, 3), 200, dtype=np.uint8)
    info = wp.write_shard([frame], tmp_path, "p", storage="raw")
    assert (info.frame_h, info.frame_w) == (128, 224)

    shard = wp.WarpedShard(info.path)
    out = shard.get_frame(0)
    np.testing.assert_array_equal(out[:100, :200], frame)  # original preserved
    assert out[100:].sum() == 0  # bottom padding is zero
    assert out[:, 200:].sum() == 0  # right padding is zero
    shard.close()


def test_no_pad_when_disabled(tmp_path):
    frame = np.full((100, 200, 3), 7, dtype=np.uint8)
    info = wp.write_shard([frame], tmp_path, "np", storage="raw", pad_to_stride=False)
    assert (info.frame_h, info.frame_w) == (100, 200)


def test_frame_indices_stored(tmp_path):
    frames = _frames(3, 64, 64)
    info = wp.write_shard(
        frames, tmp_path, "idx", storage="raw", frame_indices=[0, 8, 16]
    )
    assert info.frame_indices == [0, 8, 16]
    # Persisted to the JSON index too.
    d = json.loads(info.path.read_text())
    assert d["frame_indices"] == [0, 8, 16]


def test_frame_indices_length_mismatch(tmp_path):
    frames = _frames(3, 64, 64)
    with pytest.raises(ValueError, match="length"):
        wp.write_shard(frames, tmp_path, "bad", storage="raw", frame_indices=[0, 8])


def test_shape_mismatch_raises(tmp_path):
    frames = [np.zeros((64, 64, 3), np.uint8), np.zeros((64, 96, 3), np.uint8)]
    with pytest.raises(ValueError, match="share a shape"):
        wp.write_shard(frames, tmp_path, "mix", storage="raw")


def test_bad_storage_raises(tmp_path):
    with pytest.raises(ValueError, match="storage"):
        wp.write_shard(_frames(1, 64, 64), tmp_path, "x", storage="lz4")


def test_empty_raises(tmp_path):
    with pytest.raises(ValueError, match="no frames"):
        wp.write_shard([], tmp_path, "empty", storage="raw")


def test_warp_meta_and_target_width(tmp_path):
    frames = _frames(2, 64, 64)
    meta = {"y_top": 400, "y_bot": 1100, "final_h": 350, "target_width": 7680}
    info = wp.write_shard(
        frames,
        tmp_path,
        "m",
        storage="raw",
        target_width=7680,
        warp_meta=meta,
        camera="reolink",
    )
    assert info.target_width == 7680
    assert info.camera == "reolink"
    assert info.warp["y_top"] == 400
    assert info.bytes_per_frame_raw == 64 * 64 * 3


def test_dtype_validation(tmp_path):
    with pytest.raises(ValueError, match="uint8"):
        wp.write_shard(
            [np.zeros((64, 64, 3), np.float32)], tmp_path, "f", storage="raw"
        )
