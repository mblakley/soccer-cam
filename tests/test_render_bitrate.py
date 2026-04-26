"""Tests for the bitrate parser used by the render stage."""

from __future__ import annotations

import pytest

from video_grouper.ball_tracking.providers.homegrown.stages.render import (
    _parse_bitrate,
)


class TestParseBitrate:
    def test_megabit_suffix(self):
        assert _parse_bitrate("8M") == 8_000_000

    def test_megabit_lowercase(self):
        assert _parse_bitrate("10m") == 10_000_000

    def test_megabit_fractional(self):
        assert _parse_bitrate("2.5M") == 2_500_000

    def test_kilobit_suffix(self):
        assert _parse_bitrate("500k") == 500_000

    def test_no_suffix(self):
        assert _parse_bitrate("8000000") == 8_000_000

    def test_whitespace_stripped(self):
        assert _parse_bitrate("  6M  ") == 6_000_000

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            _parse_bitrate("not-a-number")
