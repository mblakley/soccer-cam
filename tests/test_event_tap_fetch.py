"""Event-tap anchors for the phase detector come from the phase-hint port.

Tested against the interface (FakePhaseHintSource via ``fake_integrations``);
the TTT and Null implementations are tested in tests/integrations/phase_hints/.
"""

from __future__ import annotations

import pytest

from video_grouper.task_processors.phase_game_start import _fetch_event_tap_anchors

GROUP = "/tmp/2026.06.01-10.00.00"


@pytest.mark.asyncio
async def test_no_hints_means_detector_only(fake_integrations):
    assert await _fetch_event_tap_anchors(GROUP, None) == {}
    assert fake_integrations.phase_hints.asked == ["2026.06.01-10.00.00"]


@pytest.mark.asyncio
async def test_taps_become_anchors(fake_integrations):
    fake_integrations.phase_hints.taps["2026.06.01-10.00.00"] = [
        {"anchor_type": "event_tap", "label": "kickoff", "video_time_seconds": 600.0},
        {"anchor_type": "event_tap", "label": "kickoff", "video_time_seconds": 604.0},
        {"anchor_type": "event_tap", "label": "game_end", "video_time_seconds": 6000.0},
    ]
    anchors = await _fetch_event_tap_anchors(GROUP, None)
    assert set(anchors) == {"kickoff", "end"}
    assert anchors["kickoff"].confidence == "high"  # 2 agreeing taps
    assert anchors["kickoff"].video_time == 602.0  # median of the cluster
    assert anchors["end"].confidence == "low"  # lone tap


@pytest.mark.asyncio
async def test_a_failing_hint_source_never_breaks_detection(fake_integrations):
    async def boom(group_dir):
        raise RuntimeError("network down")

    fake_integrations.phase_hints.event_taps = boom
    assert await _fetch_event_tap_anchors(GROUP, None) == {}
