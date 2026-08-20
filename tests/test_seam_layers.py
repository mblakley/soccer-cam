"""The pre-blend layer pair: its geometry, and the line between the two surfaces.

The camera transport is not exercised here -- it needs the hardware, and the
live pull is recorded in STITCH_CALIBRATION.md. What is pinned here is
everything that decides whether a *different* source can be dropped in without
touching the UI, plus the one claim the tool rests on: that lining the layers up
by hand descends a real objective.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

VPE = Path(__file__).resolve().parents[1] / "reolink-firmware-patching" / "vpe"
if str(VPE) not in sys.path:
    sys.path.insert(0, str(VPE))

seam_layers = pytest.importorskip("seam_layers")


def _shift_rows(img: np.ndarray, d: np.ndarray) -> np.ndarray:
    """Resample each row horizontally by d[row]. The curve, applied."""
    h, w = img.shape
    x = np.arange(w)
    out = np.empty((h, w), dtype=np.float64)
    for y in range(h):
        out[y] = np.interp(x - d[y], x, img[y].astype(np.float64))
    return out


class TestGeometryIsDerived:
    """No caller writes 128 or 3776, so a new source cannot break the UI."""

    def test_seam_is_the_centre_of_the_overlap_not_half_the_buffer(self):
        # The strip arrives as one 256-wide packed buffer whose halves are the
        # two layers. Half of 256 is 128, but the cross-fade is at 3840 --
        # pano_x0 + 64. Deriving the seam from the overlap gets this right;
        # halving the packed width gets 3904, which is off by a full layer.
        pair = seam_layers.split_packed(bytes(256 * 8), width=256, height=8)
        assert pair.overlap == (3776, 3904)
        assert pair.seam_x == 3840

    def test_the_two_strip_halves_share_panorama_columns(self):
        # The surprising invariant: these are not adjacent strips, they are two
        # views of the SAME columns. A reader who assumed adjacency would place
        # the right layer 128 px too far over.
        pair = seam_layers.split_packed(bytes(256 * 8), width=256, height=8)
        assert pair.left_x0 == pair.right_x0 == 3776
        assert pair.left.shape == pair.right.shape == (8, 128)

    def test_full_frame_shaped_layers_derive_a_narrow_overlap(self):
        # The source that does not exist yet, in the shape it would arrive in:
        # two wide layers with DIFFERENT origins overlapping in a narrow band.
        # Nothing in LayerPair needs changing for this, which is the whole
        # point of the descriptor being the contract.
        left = np.zeros((16, 3904), dtype=np.uint8)
        right = np.zeros((16, 3904), dtype=np.uint8)
        pair = seam_layers.LayerPair(
            left=left, right=right, left_x0=0, right_x0=3776, source="hypothetical"
        )
        assert pair.overlap == (3776, 3904)
        assert pair.seam_x == 3840  # same seam, from wildly different inputs
        api = pair.to_api()
        assert api["left"]["x0"] == 0
        assert api["right"]["x0"] == 3776
        assert api["overlap"]["w"] == 128

    def test_layers_that_do_not_overlap_are_refused(self):
        with pytest.raises(seam_layers.LayerCaptureError, match="do not overlap"):
            seam_layers.LayerPair(
                left=np.zeros((4, 10), dtype=np.uint8),
                right=np.zeros((4, 10), dtype=np.uint8),
                left_x0=0,
                right_x0=500,
            )

    def test_mismatched_heights_are_refused(self):
        with pytest.raises(seam_layers.LayerCaptureError, match="not the same moment"):
            seam_layers.LayerPair(
                left=np.zeros((4, 10), dtype=np.uint8),
                right=np.zeros((8, 10), dtype=np.uint8),
                left_x0=0,
                right_x0=0,
            )

    def test_a_short_buffer_is_refused_rather_than_reshaped(self):
        with pytest.raises(seam_layers.LayerCaptureError, match="expected at least"):
            seam_layers.split_packed(bytes(100), width=256, height=8)


class TestTheObjectiveIsReal:
    """Hand-alignment has to descend something, or it is just dragging."""

    def test_the_hidden_answer_is_the_minimum(self):
        # The archived real pair cannot be aligned -- a 0.3 m desk scene where
        # one sensor is blown out and the other near-black, zero keypoints. So
        # the claim is tested where the answer is known by construction.
        pair = seam_layers.synthetic(dx=6.0, roll=12.0, height=256)
        h = pair.height
        mid = (h - 1) / 2.0
        left = pair.left.astype(np.float64)

        def mad(dx: float, roll: float) -> float:
            d = dx + roll * (np.arange(h) - mid) / (h - 1)
            return float(np.abs(left - _shift_rows(pair.right, d)).mean())

        truth = mad(6.0, 12.0)
        assert truth < mad(0.0, 0.0) / 4  # far better than doing nothing
        assert truth < mad(6.0, 0.0)  # translation alone is not enough
        assert truth < mad(0.0, 12.0)  # nor is rotation alone
        # and it is a genuine minimum, not a plateau: half a pixel either way
        # is measurably worse, which is what makes the number steerable.
        assert truth < mad(6.5, 12.0)
        assert truth < mad(5.5, 12.0)
        assert truth < mad(6.0, 13.0)

    def test_registration_reports_both_measures_over_the_overlap_only(self):
        pair = seam_layers.synthetic(dx=0.0, roll=0.0, height=64)
        reg = pair.registration()
        # dx=0, roll=0 means the layers are identical but for sensor noise.
        assert reg["mad"] < 6.0
        assert reg["ncc"] > 0.99
        assert reg["n"] == 64 * 128

    def test_synthetic_carries_its_answer_for_the_operator_to_check_against(self):
        pair = seam_layers.synthetic(dx=3.5, roll=-8.0, height=32)
        assert pair.truth == {"dx": 3.5, "roll": -8.0}
        assert pair.to_api()["truth"] == {"dx": 3.5, "roll": -8.0}


class TestTheTwoSurfacesStayApart:
    """A LayerPair is a measurement. SensorViews are not. Enforced by type."""

    def test_a_layer_pair_is_authoritative_and_in_panorama_space(self):
        api = seam_layers.synthetic(height=8).to_api()
        assert api["space"] == "panorama"
        assert api["authoritative"] is True

    def test_sensor_views_are_never_authoritative(self):
        views = seam_layers.SensorViews(
            left=b"", right=b"", width=3840, height=2160, stamp="x"
        )
        api = views.to_api()
        assert api["space"] == "sensor"
        assert api["authoritative"] is False

    def test_sensor_views_expose_no_panorama_mapping_at_all(self):
        # The guard against someone "just adding" x0/overlap to SensorViews and
        # quietly making it look alignable. If this ever needs updating, the
        # question to answer first is what warp turned sensor columns into
        # panorama columns -- see the class docstring.
        api = seam_layers.SensorViews(
            left=b"", right=b"", width=3840, height=2160, stamp="x"
        ).to_api()
        for forbidden in ("overlap", "seam_x", "left", "right", "pano_w"):
            assert forbidden not in api

    def test_jpeg_size_is_read_from_the_file_not_taken_on_trust(self):
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (640, 480), "black").save(buf, format="JPEG")
        assert seam_layers._jpeg_size(buf.getvalue()) == (640, 480)

    def test_a_non_jpeg_is_refused(self):
        with pytest.raises(seam_layers.LayerCaptureError, match="not a JPEG"):
            seam_layers._jpeg_size(b"\xff\xd8" + b"\x00" * 64)


class TestArchivedDumps:
    def test_a_dump_round_trips_through_load_file(self, tmp_path):
        pair = seam_layers.synthetic(dx=2.0, roll=0.0, height=16)
        packed = np.concatenate([pair.left, pair.right], axis=1)
        p = tmp_path / "pair.bin"
        p.write_bytes(packed.tobytes())
        back = seam_layers.load_file(p, width=256, height=16)
        assert np.array_equal(back.left, pair.left)
        assert np.array_equal(back.right, pair.right)
        assert back.source == "file"

    def test_a_missing_dump_says_so(self, tmp_path):
        with pytest.raises(seam_layers.LayerCaptureError, match="no such layer dump"):
            seam_layers.load_file(tmp_path / "nope.bin")
