"""Gates on the v2 calibration artifact.

Two producers write this curve -- the interactive tool and the automatic
solver -- and one shipped consumer reads it without knowing v2 exists. The
tests below pin the three properties that keeps working:

  * a v2 artifact is still a valid v1 profile, byte-for-byte compatible with
    the loader that ships today;
  * the reader takes whatever shape a producer emits, including the solver's
    bare ``[[y, dx], ...]`` plus a metadata dict;
  * the combinations that would double-correct are refused, not warned about.
"""

from __future__ import annotations

import pytest

from video_grouper.utils.stitch_remap import (
    SCHEMA_V2,
    SeamCalibrationError,
    StitchProfile,
    build_dx_lookup,
    build_v2_profile,
    read_dx_anchors,
    validate_v2_profile,
)

ANCHORS = [(0.0, -6.0), (540.0, -3.0), (1080.0, 0.0), (1620.0, 3.0), (2159.0, 6.0)]


def _v2(anchors=None, **kw):
    base = {"correction_owner": "camera_mesh", "calibration_id": "duo3-test"}
    base.update(kw)
    return build_v2_profile(anchors if anchors is not None else ANCHORS, **base)


# -- backward compatibility --------------------------------------------------


def test_a_v2_artifact_is_still_a_v1_profile():
    """The whole reason the schema extends in place instead of wrapping."""
    profile = StitchProfile.from_dict(_v2())
    assert profile.source_width == 7680
    assert profile.seam_x == 3840
    assert profile.dx_anchors == [(0, -6), (540, -3), (1080, 0), (1620, 3), (2159, 6)]


def test_sub_pixel_anchors_round_rather_than_truncate():
    """The mesh is quarter-pixel; `int()` would bias every anchor toward zero.

    -6.75 truncates to -6 and 6.75 to 6 -- a systematic under-correction of up
    to a pixel, applied before `build_dx_lookup` does its own rounding, on
    exactly the small objects at the seam this exists to help.
    """
    d = _v2(anchors=[(0.0, -6.75), (2159.0, 6.75)])
    assert StitchProfile.from_dict(d).dx_anchors == [(0, -7), (2159, 7)]


def test_the_curve_survives_the_round_trip_into_the_shipped_lookup():
    profile = StitchProfile.from_dict(_v2())
    lut = build_dx_lookup(profile, 7680, 2160)
    assert lut[0] == -6
    assert lut[1080] == 0
    assert lut[2159] == 6
    # monotone, because the authored curve is
    assert all(b >= a for a, b in zip(lut, lut[1:], strict=False))


# -- the permissive reader ---------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        [[0, -4], [2159, 4]],
        {"dx_anchors": [[0, -4], [2159, 4]]},
        {
            "dx_anchors": [[0, -4], [2159, 4]],
            "metadata": {"solver": "huber", "r2": 0.98},
        },
        {"source_width": 7680, "seam_x": 3840, "dx_anchors": [(0, -4), (2159, 4)]},
    ],
)
def test_the_reader_accepts_whatever_shape_a_producer_emits(payload):
    """The automatic solver was told not to touch the schema; it emits plain
    pairs plus a metadata dict. That has to just work."""
    assert read_dx_anchors(payload) == [(0.0, -4.0), (2159.0, 4.0)]


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"dx_anchors": []},
        [[0, 1], [0, 2]],  # np.interp goes silently wrong on unsorted x
        [[100, 1], [50, 2]],
        [[0, 1, 2]],
        [["a", 1]],
    ],
)
def test_the_reader_refuses_what_would_silently_misbehave(payload):
    with pytest.raises(SeamCalibrationError):
        read_dx_anchors(payload)


# -- the anti-double-correction rules ----------------------------------------


def test_a_curve_under_an_owner_that_cannot_apply_it_is_refused():
    with pytest.raises(SeamCalibrationError, match="cannot express a per-row shear"):
        build_v2_profile(ANCHORS, correction_owner="camera_scalars", calibration_id="x")


def test_a_zero_curve_under_the_scalars_owner_is_fine():
    """Scalars can own the correction; they just cannot own a *shear*."""
    d = build_v2_profile(
        [(0.0, 0.0), (2159.0, 0.0)],
        correction_owner="camera_scalars",
        calibration_id="x",
    )
    assert d["correction_owner"] == "camera_scalars"


def test_downstream_ownership_with_an_applied_mesh_stage_is_refused():
    """The double-correct: the camera already fixed it and the pipeline is
    about to fix it again. This is the one case that must never be a warning."""
    d = _v2(correction_owner="downstream")
    d["stages"] = [{"surface": "camera_mesh", "state": "applied"}]
    with pytest.raises(SeamCalibrationError, match="double-correction"):
        validate_v2_profile(d)


def test_an_unknown_owner_is_refused():
    with pytest.raises(SeamCalibrationError):
        build_v2_profile(ANCHORS, correction_owner="magic", calibration_id="x")


def test_dropping_dy_downstream_has_to_be_recorded():
    """The downstream corrector is horizontal-only. Silently discarding a
    measured vertical component is how calibrations become mysterious."""
    d = _v2(correction_owner="downstream")
    d["dy_anchors"] = [[0, 1.5]]
    with pytest.raises(SeamCalibrationError, match="dropped"):
        validate_v2_profile(d)
    d["dropped"] = ["dy_anchors"]
    validate_v2_profile(d)


def test_a_legacy_v1_profile_is_not_second_guessed():
    """No schema, no correction_owner: it means downstream and always did."""
    validate_v2_profile(
        {
            "source_width": 7680,
            "source_height": 2160,
            "seam_x": 3840,
            "dx_anchors": [[0, -10], [2159, 5]],
        }
    )


# -- the sign convention, carried in the artifact ----------------------------


def test_the_artifact_states_its_own_sign_convention():
    """One paragraph that prevents a sign error, and a sign error here applies
    the misregistration twice instead of removing it."""
    d = _v2()
    assert d["schema"] == SCHEMA_V2
    assert d["sense"]["dx_means"] == (
        "px the RIGHT half must move right, at row y, to register with the left"
    )
    assert d["sense"]["downstream_moves"] == "right_half"
    assert d["sense"]["camera_mesh_moves"] == "left_half_with_opposite_sense"
    assert d["geometry"]["warped_half"] == "left"
    assert d["geometry"]["blend_window"] == [3712, 3968]
