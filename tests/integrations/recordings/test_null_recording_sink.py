"""NullRecordingSink: TTT off -- every report is accepted and dropped."""

from datetime import datetime

import pytest

from video_grouper.integrations.recordings import StepReport
from video_grouper.integrations.recordings.sources.null import NullRecordingSink


@pytest.mark.asyncio
async def test_accepts_everything_and_records_nothing():
    sink = NullRecordingSink()
    assert await sink.recording_found("g", []) is None
    assert await sink.progress("g", StepReport("x", "x", "X", "running")) is None
    assert await sink.published("g", "url", datetime(2026, 1, 1)) is None
    assert await sink.phases("g", {"times": {"kickoff": 1.0}}) is False
    assert await sink.phase_verified("g", "kickoff", "correct") is False


def test_always_accepts():
    assert isinstance(NullRecordingSink.from_context(object()), NullRecordingSink)
