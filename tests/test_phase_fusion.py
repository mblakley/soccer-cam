"""Behavior fixtures for the game-phase detector fusion.

Each fixture is a real cached signal set (player curve + whistle blasts + ball restarts) captured
from the training box, paired with the golden phase boundaries that the pre-refactor detector wrote
to <gid>.fit.json. The test proves the extracted ``fuse_phases`` reproduces those boundaries exactly
(within 0.05s) from the cached signals -- a behavior gate for the S0 move into video_grouper."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from video_grouper.inference.event_tap_anchors import Anchor
from video_grouper.inference.phase_detector import fuse_phases

FIX = Path(__file__).parent / "fixtures" / "phase"
GIDS = [
    "heat__2026.06.08_vs_Hilton_Heat_Flaitz_away",
    "heat__2026.06.06_vs_Lakefront_SC_Sullivan_away",
    "heat__2026.05.28_vs_Fairport_away",
]


def _load(gid):
    data = json.loads((FIX / f"{gid}.json").read_text(encoding="utf-8"))
    fx = data["fixture"]
    # rebuild the signals dict exactly as the CLI does before fusing (ts/cnt as np arrays, poly
    # injected from the upright source-px field polygon).
    signals = {
        "ts": np.array(fx["ts"]),
        "cnt": np.array(fx["cnt"], np.float32),
        "dur": fx["dur"],
        "blasts": fx["blasts"],
        "multis": fx["multis"],
        "blast_loud": fx["blast_loud"],
        "ball_ev": fx["ball_ev"],
        "ball_center": fx["ball_center"],
        "sr": fx["sr"],
        "poly": fx["poly"],
    }
    return signals, data["golden_times"]


@pytest.mark.parametrize("gid", GIDS)
def test_fuse_phases_matches_golden(gid):
    signals, golden = _load(gid)
    result = fuse_phases(signals)
    assert result is not None
    times = dict(result["times"])
    times["kickoff"] = max(0.0, times["kickoff"])  # CLI clamps max(0, ko) at output
    for key in ("kickoff", "halftime", "second_half", "end"):
        assert abs(times[key] - golden[key]) <= 0.05, (
            f"{gid} {key}: {times[key]} != golden {golden[key]}"
        )


@pytest.mark.parametrize("gid", GIDS)
def test_truncated_start_pins_kickoff_to_zero(gid):
    """A human-confirmed truncated start (NTFY "already started") pins KO to the
    file head and trusts it for the trim; HT/2H stay detector-driven."""
    signals, _ = _load(gid)
    result = fuse_phases(signals, truncated_start=True)
    assert result is not None
    assert result["times"]["kickoff"] == 0.0
    assert result["ko_trustworthy"] is True
    assert result["truncated_start"] is True
    # HT is still a real detection (not pinned), so it sits well into the game.
    assert result["times"]["halftime"] > 60.0


@pytest.mark.parametrize("gid", GIDS)
def test_truncated_end_pins_end_to_file_end(gid):
    """A human-confirmed truncated end (NTFY "still playing") pins END to the file end."""
    signals, _ = _load(gid)
    dur = float(np.asarray(signals["ts"], dtype=float)[-1])
    result = fuse_phases(signals, truncated_end=True)
    assert result is not None
    assert result["times"]["end"] == dur
    assert result["truncated_end"] is True


def test_non_truncated_flags_default_false():
    """Default (non-truncated) results carry both flags False (byte-identical path)."""
    signals, _ = _load(GIDS[0])
    result = fuse_phases(signals)
    assert result["truncated_start"] is False
    assert result["truncated_end"] is False


# --- parent event-tap anchors ---------------------------------------------


def _hi(boundary, video_time):
    return Anchor(
        boundary=boundary,
        video_time=video_time,
        confidence="high",
        n_taps=3,
        spread_s=6.0,
    )


def _lo(boundary, video_time):
    return Anchor(
        boundary=boundary,
        video_time=video_time,
        confidence="low",
        n_taps=1,
        spread_s=0.0,
    )


def test_anchors_none_is_noop():
    """Explicit anchors={} / None matches the no-anchor fusion exactly."""
    signals, _ = _load(GIDS[0])
    base = fuse_phases(signals)
    withnone = fuse_phases(signals, anchors=None)
    assert withnone["times"] == base["times"]
    assert withnone["anchored"] == {}


def test_high_confidence_kickoff_snaps_to_nearby_whistle():
    """An agreeing parent cluster near a whistle snaps KO to that whistle and
    trusts it for the trim."""
    signals, _ = _load(GIDS[0])
    b0 = float(signals["blasts"][0])
    result = fuse_phases(signals, anchors={"kickoff": _hi("kickoff", b0 + 6.0)})
    assert result["times"]["kickoff"] == b0  # snapped to the whistle
    assert result["anchored"]["kickoff"] == {"mode": "snap", "confidence": "high"}
    assert result["ko_anchor"] == "event_tap"
    assert result["ko_trustworthy"] is True


def test_uncorroborated_cluster_does_not_override_confident_ko():
    """A high cluster with NO whistle near it must NOT move a confident detector
    KO -- guards the 'confidently wrong cluster' case the GT sim surfaced."""
    signals, _ = _load(GIDS[0])
    base = fuse_phases(signals)
    if base.get("ko_anchor") == "sym":
        pytest.skip("fixture KO is weak; this guards the confident-KO case")
    far = float(max(signals["blasts"])) + 500.0  # no blast within ANCHOR_SNAP_SEC
    result = fuse_phases(signals, anchors={"kickoff": _hi("kickoff", far)})
    assert result["times"]["kickoff"] == base["times"]["kickoff"]  # unchanged
    assert "kickoff" not in result["anchored"]


def test_structural_sanity_rejects_cross_boundary_anchor():
    """A 'kickoff' cluster mis-tapped at the 2nd-half time must be rejected
    (it can't cross halftime) -- guards the wrong-button case."""
    signals, _ = _load(GIDS[0])
    base = fuse_phases(signals)
    at = float(base["times"]["second_half"])  # a whistle near the 2nd-half restart
    result = fuse_phases(signals, anchors={"kickoff": _hi("kickoff", at)})
    assert result["times"]["kickoff"] == base["times"]["kickoff"]  # KO not dragged
    assert "kickoff" not in result["anchored"]


def test_low_confidence_does_not_override_confident_detector_ko():
    """A lone/scattered tap must not move a confident (whistle/ball) detector KO."""
    signals, _ = _load(GIDS[0])
    base = fuse_phases(signals)
    if base.get("ko_anchor") == "sym":
        pytest.skip("this fixture's KO is symmetric-prior; covered by the fill-in test")
    b0 = float(signals["blasts"][0])
    result = fuse_phases(signals, anchors={"kickoff": _lo("kickoff", b0 + 6.0)})
    assert result["times"]["kickoff"] == base["times"]["kickoff"]  # unchanged
    assert "kickoff" not in result["anchored"]


def test_high_confidence_end_snaps_to_nearby_whistle():
    """A parent 'Final Whistle' cluster near a whistle snaps END to it."""
    signals, _ = _load(GIDS[0])
    bl = float(signals["blasts"][-1])
    result = fuse_phases(signals, anchors={"end": _hi("end", bl + 4.0)})
    assert result["times"]["end"] == bl
    assert result["anchored"]["end"] == {"mode": "snap", "confidence": "high"}
