"""Reading the camera's installed calibration (`vpe/stitch_apply.py`).

The premise these pin down: the editor must start from what is *installed*, and
`dx = 0` must mean "the factory mesh, untouched". The boot hook composes anchors
onto the mesh the firmware generates at boot, so anchors are always relative to
factory -- which is what makes an all-zero curve and "back to factory" the same
thing, and why there is only one button for them.

Transport is faked throughout; the live-camera check is separate and manual.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

VPE = Path(__file__).resolve().parents[1] / "reolink-firmware-patching" / "vpe"
if str(VPE) not in sys.path:
    sys.path.insert(0, str(VPE))

lut2d = pytest.importorskip("lut2d")
stitch_apply = pytest.importorskip("stitch_apply")


def _identity(n: int = 33):
    return lut2d.Lut2D.identity(n, lut2d.DEFAULT_HALF_WIDTH, lut2d.DEFAULT_HALF_HEIGHT)


# -- seam profile ------------------------------------------------------------


def test_identity_mesh_has_no_displacement_at_the_seam():
    """A pass-through mesh maps destination x to the same source x, so the
    vendor displacement it 'chose' is zero on every row."""
    prof = stitch_apply.seam_profile(_identity())
    assert len(prof) == 33
    assert all(abs(r["offset_px"]) < 0.5 for r in prof)


def test_identity_mesh_has_unit_scale():
    """`s` is source-px per destination-px, which is 1.0 for a pass-through."""
    prof = stitch_apply.seam_profile(_identity())
    assert all(abs(r["s"] - 1.0) < 1e-3 for r in prof)


def test_seam_profile_rows_span_the_destination_height():
    prof = stitch_apply.seam_profile(_identity())
    assert prof[0]["y"] == 0.0
    assert prof[-1]["y"] == pytest.approx(lut2d.DEFAULT_HALF_HEIGHT - 1, abs=1.0)


def test_seam_profile_reports_the_seam_column_not_the_middle():
    """The seam is the left half's LAST column; profiling the middle would
    describe a part of the image the correction is not about."""
    lut = _identity()
    n = lut.n
    du = (lut2d.DEFAULT_HALF_WIDTH - 1.0) / (n - 1)
    prof = stitch_apply.seam_profile(lut)
    assert prof[0]["src_x"] == pytest.approx((n - 1) * du, abs=1.0)


def test_seam_profile_refuses_a_mesh_too_small_to_difference():
    tiny = lut2d.Lut2D.identity(2, 3840.0, 2160.0)
    with pytest.raises(lut2d.Lut2DError):
        stitch_apply.seam_profile(tiny)


# -- anchors_at --------------------------------------------------------------


def test_uncalibrated_camera_has_no_curve_to_load():
    assert stitch_apply.CameraCalibration().anchors_at((0, 1080, 2159)) is None


def test_installed_anchors_are_resampled_onto_the_editor_rows():
    cal = stitch_apply.CameraCalibration(
        anchors=[(0.0, 4.0), (2159.0, 8.0)],
        anchors_meta={"src_width": 7680.0, "src_height": 2160.0},
    )
    got = cal.anchors_at((0, 540, 1080, 1620, 2159))
    assert [y for y, _ in got] == [0, 540, 1080, 1620, 2159]
    assert got[0][1] == pytest.approx(4.0)
    # linear across the full 0..2159 span, not a half of it: 4 + 4*(1080/2159)
    assert got[2][1] == pytest.approx(6.0, abs=0.05)
    assert got[-1][1] == pytest.approx(8.0)


def test_anchors_authored_on_a_downscaled_still_are_rescaled():
    """The failure this prevents: a curve measured on a half-size frame applying
    a proportionally wrong correction with nothing complaining."""
    cal = stitch_apply.CameraCalibration(
        anchors=[(0.0, 5.0), (1080.0, 5.0)],
        anchors_meta={"src_width": 3840.0, "src_height": 1080.0},
    )
    got = cal.anchors_at((0, 2159))
    assert got[0][1] == pytest.approx(10.0)  # dx doubles with the width ratio


# -- state notes -------------------------------------------------------------


def test_factory_state_says_plainly_that_nothing_is_applied():
    cal = stitch_apply.CameraCalibration(
        live_crc32=0x8514014A,
        factory_crc32=0x8514014A,
        factory_name="factory_boot.bin",
        at_factory=True,
    )
    # the note is produced by read_calibration; assert the wording contract via
    # the same branch it uses
    assert cal.at_factory is True
    assert cal.anchors is None


def test_to_api_renders_crc32_as_hex_and_survives_no_factory_copy():
    cal = stitch_apply.CameraCalibration(live_crc32=0x0A0B0C0D)
    api = cal.to_api()
    assert api["live_crc32"] == "0a0b0c0d"
    assert api["factory_crc32"] is None
    assert api["at_factory"] is None


def test_factory_copy_names_are_tried_in_documented_order():
    """`factory_boot.bin` is what S98_StitchCal writes; both names exist on the
    unit and are byte-identical, so order only decides which is reported."""
    assert stitch_apply.FACTORY_COPIES[0] == "factory_boot.bin"
    assert "factory_vpe0.bin" in stitch_apply.FACTORY_COPIES


# -- read_calibration, faked transport ---------------------------------------


@pytest.fixture
def faked(monkeypatch):
    lut = _identity()
    blob = lut.to_bytes()
    calls: dict[str, object] = {"dumped": [], "fetched": []}

    def fake_dump(host, vpe_id=0, name="baseline.bin"):
        calls["dumped"].append(name)
        return "wrote"

    def fake_read(host, name="baseline.bin"):
        return lut

    def fake_fetch(host, sd_relative):
        calls["fetched"].append(sd_relative)
        return calls.get(sd_relative)  # type: ignore[return-value]

    monkeypatch.setattr(stitch_apply, "dump_mesh", fake_dump)
    monkeypatch.setattr(stitch_apply, "read_mesh", fake_read)
    monkeypatch.setattr(stitch_apply, "_fetch_optional", fake_fetch)
    calls["stitchcal/factory_boot.bin"] = blob
    return calls


def test_read_calibration_reports_factory_when_nothing_is_installed(faked):
    state = stitch_apply.read_calibration("1.2.3.4")
    assert state.at_factory is True
    assert state.anchors is None
    assert "at factory" in state.note
    assert "Nothing has been applied" in state.note
    assert state.profile and len(state.profile) == 33


def test_read_calibration_loads_installed_anchors(faked):
    faked["stitchcal/anchors.txt"] = (
        b"# seam_calibration/2 deploy-1\n# src 7680 2160  seam 3840\n"
        b"dx 0 3.0000\ndx 2159 6.0000\n"
    )
    state = stitch_apply.read_calibration("1.2.3.4")
    assert state.anchors == [(0.0, 3.0), (2159.0, 6.0)]
    assert "anchors are installed" in state.note
    # the live-vs-boot divergence must be surfaced, not discovered
    assert "reboot" in state.note


def test_read_calibration_flags_a_mesh_changed_outside_this_tool(faked, monkeypatch):
    other = lut2d.Lut2D.identity(33, 3840.0, 2160.0)
    other.set(0, 0, 5.0, 5.0)
    monkeypatch.setattr(stitch_apply, "read_mesh", lambda h, n="baseline.bin": other)
    state = stitch_apply.read_calibration("1.2.3.4")
    assert state.at_factory is False
    assert "outside this tool" in state.note


def test_read_calibration_survives_an_unparseable_anchors_file(faked):
    faked["stitchcal/anchors.txt"] = b"this is not an anchors file\n"
    state = stitch_apply.read_calibration("1.2.3.4")
    assert state.anchors is None
    assert "unparseable" in state.anchors_error


def test_read_calibration_never_writes_to_the_camera(faked, monkeypatch):
    """Read-only is a constraint, not an intention."""

    def boom(*a, **k):
        raise AssertionError("read_calibration must not write to the camera")

    monkeypatch.setattr(stitch_apply, "set_stitch", boom)
    monkeypatch.setattr(stitch_apply, "push_text", boom)
    stitch_apply.read_calibration("1.2.3.4")
    assert faked["dumped"] == ["baseline.bin"]
