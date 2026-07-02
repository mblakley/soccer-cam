"""Unit tests for parent event-tap phase anchors (the confidence-tiered trust model)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from video_grouper.inference.event_tap_anchors import (
    CLUSTER_WINDOW_S,
    REACTION_LAG_S,
    Anchor,
    build_anchors,
)

START = datetime(2026, 3, 20, 15, 0, 0, tzinfo=UTC)  # recording start (video t=0)


def _tap(label: str, seconds_into_video: float) -> dict:
    """A tap whose wall-clock is `seconds_into_video` after the recording start.

    The event actually happened REACTION_LAG_S earlier than the tap, so a tap
    placed at raw video-second S yields an anchor at S - REACTION_LAG_S."""
    return {
        "label": label,
        "device_timestamp": START + timedelta(seconds=seconds_into_video),
    }


def test_no_taps_returns_empty():
    assert build_anchors([], START) == {}


def test_no_recording_start_returns_empty():
    assert build_anchors([_tap("kickoff", 600)], None) == {}


def test_single_tap_is_low_confidence():
    anchors = build_anchors([_tap("kickoff", 600)], START)
    a = anchors["kickoff"]
    assert a.confidence == "low"
    assert a.n_taps == 1
    assert not a.is_high
    # 600s tap, minus reaction lag.
    assert a.video_time == 600 - REACTION_LAG_S


def test_label_to_boundary_mapping():
    taps = [
        _tap("kickoff", 600),
        _tap("halftime_start", 3000),
        _tap("halftime_end", 3300),
        _tap("game_end", 6000),
    ]
    anchors = build_anchors(taps, START)
    assert set(anchors) == {"kickoff", "halftime", "second_half", "end"}


def test_agreeing_cluster_is_high_confidence_at_median():
    # 3 parents within CLUSTER_WINDOW_S at the kickoff -> trusted consensus.
    taps = [_tap("kickoff", 600), _tap("kickoff", 604), _tap("kickoff", 608)]
    a = build_anchors(taps, START)["kickoff"]
    assert a.confidence == "high"
    assert a.n_taps == 3
    assert a.video_time == 604 - REACTION_LAG_S  # median of the cluster
    assert a.spread_s == 8.0


def test_scattered_taps_are_low_confidence():
    # Two taps far apart (> window) with no agreeing pair -> lowest quality.
    taps = [_tap("kickoff", 600), _tap("kickoff", 900)]
    a = build_anchors(taps, START)["kickoff"]
    assert a.confidence == "low"


def test_cluster_of_two_beats_a_lone_outlier():
    # Two parents agree (within window); a third is a wild outlier -> high conf
    # from the agreeing pair, outlier excluded from the estimate.
    taps = [
        _tap("kickoff", 600),
        _tap("kickoff", 605),
        _tap("kickoff", 1200),  # outlier
    ]
    a = build_anchors(taps, START)["kickoff"]
    assert a.confidence == "high"
    assert a.n_taps == 2
    assert a.video_time == 602.5 - REACTION_LAG_S  # median of the agreeing pair


def test_cluster_window_boundary_inclusive():
    # Exactly CLUSTER_WINDOW_S apart still counts as agreeing.
    taps = [_tap("kickoff", 600), _tap("kickoff", 600 + CLUSTER_WINDOW_S)]
    a = build_anchors(taps, START)["kickoff"]
    assert a.confidence == "high"
    assert a.n_taps == 2


def test_iso_string_timestamp_parsed():
    taps = [
        {
            "label": "game_end",
            "device_timestamp": (START + timedelta(seconds=6000)).isoformat(),
        }
    ]
    a = build_anchors(taps, START)["end"]
    assert a.video_time == 6000 - REACTION_LAG_S


def test_video_time_seconds_used_directly_without_recording_start():
    # A TTT-computed video_time_seconds places the tap directly (no tz math), so
    # it works even with no recording_start.
    taps = [{"label": "kickoff", "video_time_seconds": 600.0}]
    a = build_anchors(taps, None)["kickoff"]
    assert a.video_time == 600.0
    assert a.confidence == "low"


def test_video_time_seconds_preferred_over_device_timestamp():
    taps = [
        {
            "label": "game_end",
            "video_time_seconds": 5000.0,
            "device_timestamp": START + timedelta(seconds=9999),
        }
    ]
    a = build_anchors(taps, START)["end"]
    assert a.video_time == 5000.0


def test_tap_before_recording_start_dropped():
    # A tap whose wall-clock precedes the recording start is implausible.
    taps = [{"label": "kickoff", "device_timestamp": START - timedelta(seconds=30)}]
    assert build_anchors(taps, START) == {}


def test_unknown_label_ignored():
    taps = [
        {
            "label": "some_other_event",
            "device_timestamp": START + timedelta(seconds=600),
        }
    ]
    assert build_anchors(taps, START) == {}


def test_naive_timestamp_mismatch_dropped_not_crash():
    # A naive datetime vs an aware recording_start can't be subtracted; drop it.
    taps = [{"label": "kickoff", "device_timestamp": datetime(2026, 3, 20, 15, 10, 0)}]
    assert build_anchors(taps, START) == {}


def test_anchor_dataclass_is_hashable_frozen():
    a = Anchor(
        boundary="kickoff", video_time=599.0, confidence="low", n_taps=1, spread_s=0.0
    )
    assert hash(a) is not None  # frozen dataclass
