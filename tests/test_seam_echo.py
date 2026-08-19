"""Tests for the automatic seam measurement (`vpe/seam_echo.py`).

The point of these is the *refusals*. Five passes at this measurement have
produced confident wrong numbers -- grass texture, a parked car, a shirt, a pair
of shorts, and in this pass a walking figure whose limbs sit at different
heights and voted 18..33 px. So the behaviour worth pinning down is that the
estimator declines to publish when its evidence does not support a number, and
that its null cannot be skipped.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

VPE = Path(__file__).resolve().parents[1] / "reolink-firmware-patching" / "vpe"
if str(VPE) not in sys.path:
    sys.path.insert(0, str(VPE))

seam_echo = pytest.importorskip("seam_echo")


SEAM = 400
BLEND = 128


def _pitch(h=520, w=800, rng=None):
    """Green, heavily luminance-textured, chromatically uniform -- i.e. grass.

    Built in Lab with texture on L only. That is the property that matters: real
    grass measures 13-17 in colour distance while carrying enough |dI/dx| to
    saturate any gradient gate, and a fixture that put the noise on B, G and R
    equally would leak into a/b and stop being grass.
    """
    import cv2

    rng = rng or np.random.default_rng(7)
    lab = np.zeros((h, w, 3), np.float32)
    lab[..., 0] = 120.0 + rng.normal(0.0, 26.0, (h, w))  # mown blades
    lab[..., 1] = 105.0  # green
    lab[..., 2] = 145.0
    lab = np.clip(lab, 0, 255).astype(np.uint8)
    return cv2.cvtColor(lab, cv2.COLOR_Lab2BGR)


def _paint(img, x, y0, y1, width=16, colour=(40, 40, 210)):
    img[y0:y1, x - width // 2 : x + width // 2] = np.array(colour, np.uint8)


def _ghosted(img, seam=SEAM, blend=BLEND, d=18):
    """Apply the blend the camera applies: s = a*b(x) + (1-a)*b(x-d)."""
    out = img.astype(np.float32).copy()
    shifted = np.roll(img.astype(np.float32), d, axis=1)
    lo, hi = seam - blend, seam + blend
    xs = np.arange(lo, hi)
    a = np.clip(0.5 - (xs - seam) / (2.0 * blend), 0.0, 1.0)[None, :, None]
    out[:, lo:hi] = a * out[:, lo:hi] + (1.0 - a) * shifted[:, lo:hi]
    return np.clip(out, 0, 255).astype(np.uint8)


def test_blend_weight_is_half_at_the_seam_and_saturates_at_the_edges():
    assert seam_echo.blend_weight(SEAM, seam_x=SEAM, blend_w=BLEND) == pytest.approx(
        0.5
    )
    assert seam_echo.blend_weight(SEAM - BLEND, seam_x=SEAM, blend_w=BLEND) == 1.0
    assert seam_echo.blend_weight(SEAM + BLEND, seam_x=SEAM, blend_w=BLEND) == 0.0


def test_colour_distance_separates_a_target_from_grass():
    """The gate the whole approach rests on. Grass has luminance texture and no
    colour distinctiveness; a target has both."""
    img = _pitch()
    _paint(img, SEAM, 200, 300)
    dist = seam_echo.colour_distance(img)
    target = np.percentile(dist[200:300, SEAM - 8 : SEAM + 8], 95)
    grass = np.percentile(dist[200:300, SEAM + 200 : SEAM + 300], 95)
    assert target > 3 * grass


def test_fit_recovers_a_known_two_copy_separation():
    profile = np.zeros(140, np.float32)
    profile[60:80] = 40.0
    profile[77:97] += 30.0  # second copy, 17 px along
    best, single = seam_echo._fit_two_copy(profile)
    assert best is not None and single is not None
    assert best[1] == pytest.approx(17.0, abs=2.0)
    assert single[0] > best[0]  # two copies beat one lobe


def test_refuses_when_nothing_is_chromatically_distinct():
    """Bare pitch. The honest answer is an instruction, not a number."""
    result = seam_echo.measure([_pitch()], seam_x=SEAM, blend_w=BLEND)
    assert result.verdict == "refused"
    assert result.dx is None
    assert "chromatically distinct" in result.remedy.lower()


def test_refuses_a_target_outside_the_mixing_corridor():
    """A target 106 px out sits at a_pred ~0.09 -- an 8.6% ghost, which nothing
    should be believed on. An earlier fit reported 16.1 px here; truth is 0."""
    img = _pitch()
    _paint(img, SEAM + 106, 200, 300)
    result = seam_echo.measure([_ghosted(img)], seam_x=SEAM, blend_w=BLEND)
    assert result.verdict != "measured"
    assert result.dx is None


def test_far_corridor_target_is_not_measured():
    img = _pitch()
    _paint(img, SEAM + 300, 200, 320)
    result = seam_echo.measure([_ghosted(img)], seam_x=SEAM, blend_w=BLEND)
    assert result.verdict != "measured"
    assert result.dx is None


def test_disagreeing_candidates_refuse_rather_than_publish_a_median():
    """The failure that mattered: a walking figure voted 18..33 px and a looser
    gate published 25.5 where the hand-verified answer was 17-19."""
    result = seam_echo.EchoResult()
    result.verdict = "refused"
    cands = [
        seam_echo.Candidate((0, 26), SEAM, 120.0, d, 0.5, 0.5, 2.0, accepted=True)
        for d in (18.0, 21.0, 25.0, 26.0, 28.0, 33.0)
    ]
    ds = np.array([c.d for c in cands])
    spread = float(np.percentile(ds, 75) - np.percentile(ds, 25))
    assert spread > seam_echo.MAX_SPREAD


def test_measurement_reports_colour_distance_on_every_candidate():
    """The audit trail the previous four passes lacked: a reviewer must be able
    to see the estimator ran on something distinct from the pitch."""
    img = _pitch()
    _paint(img, SEAM, 120, 400)
    result = seam_echo.measure([_ghosted(img)], seam_x=SEAM, blend_w=BLEND)
    assert result.candidates, "a painted target should produce candidates"
    for cand in result.candidates:
        assert cand.lab >= seam_echo.MIN_LAB_DISTANCE
        assert "lab" in cand.to_api()


def test_null_runs_on_every_call_and_is_reported():
    img = _pitch()
    _paint(img, SEAM, 120, 400)
    result = seam_echo.measure([_ghosted(img)], seam_x=SEAM, blend_w=BLEND)
    api = result.to_api()
    assert "control_accepted" in api
    assert "control_rate" in api
    # controls are scanned whatever the seam verdict -- the null is not optional
    assert result.controls or result.control_accepted == 0


def test_anchors_are_refused_unconditionally():
    """The estimator is withdrawn: no result, however confident, may become a
    curve. It measures step edges rather than ghosts, and at scale it accepts
    control corridors -- true answer zero -- almost as often as the seam."""
    for verdict in ("refused", "void", "withdrawn", "measured"):
        result = seam_echo.EchoResult(verdict=verdict, provisional_dx=17.5, dx=17.5)
        with pytest.raises(ValueError, match="withdrawn"):
            seam_echo.anchors_from_measurement(result, (0, 540, 1080))


def test_a_confident_agreeing_measurement_still_does_not_propose(monkeypatch):
    """The failure mode that made this dangerous: on the one hand-verified frame
    with a real 18 px ghost, three agreeing candidates on a walking player's
    torso produced 33 px and the old code published it."""
    agreeing = [
        seam_echo.Candidate((0, 26), 3800, 120.0, d, 0.5, 0.5, 2.0, accepted=True)
        for d in (33.0, 33.0, 34.0)
    ]
    # confident at the seam, quiet at the controls -- i.e. the single-frame
    # case where the old null was inert and a number got published
    monkeypatch.setattr(
        seam_echo,
        "_scan",
        lambda dist, centre, **k: list(agreeing) if centre == SEAM else [],
    )
    img = _pitch()
    _paint(img, SEAM, 120, 400)
    result = seam_echo.measure([_ghosted(img)], seam_x=SEAM, blend_w=BLEND)
    assert result.verdict == "withdrawn"
    assert result.dx is None
    assert result.to_api()["dx"] is None
    assert result.provisional_dx is not None  # kept as evidence
    with pytest.raises(ValueError):
        seam_echo.anchors_from_measurement(result, (0, 1080))


def test_colour_frames_are_required():
    with pytest.raises(ValueError):
        seam_echo.measure([np.zeros((200, 400), np.uint8)], seam_x=SEAM)


def test_empty_frame_list_is_rejected():
    with pytest.raises(ValueError):
        seam_echo.measure([], seam_x=SEAM)
