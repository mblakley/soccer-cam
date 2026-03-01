"""Unit tests for the timestamp matcher — pure offset computation functions."""

from datetime import datetime, timezone

import pytest

from video_grouper.models import RecordingFile
from video_grouper.task_processors.services.timestamp_matcher import (
    compute_clip_boundaries,
    compute_combined_offset,
    compute_trimmed_offset,
)


def _make_recording(start_str: str, end_str: str, skip: bool = False) -> RecordingFile:
    """Helper to create a RecordingFile with naive datetimes (camera-local)."""
    fmt = "%Y-%m-%d %H:%M:%S"
    return RecordingFile(
        start_time=datetime.strptime(start_str, fmt),
        end_time=datetime.strptime(end_str, fmt),
        file_path=f"/cam/{start_str.replace(' ', '_')}.dav",
        skip=skip,
    )


# ---------------------------------------------------------------------------
# compute_combined_offset
# ---------------------------------------------------------------------------


class TestComputeCombinedOffset:
    def test_tag_in_first_file(self):
        files = [
            _make_recording("2026-01-15 10:00:00", "2026-01-15 10:30:00"),
            _make_recording("2026-01-15 10:30:00", "2026-01-15 11:00:00"),
        ]
        # Tag at 10:10 UTC (= 10:10 local since we use same tz)
        tag = datetime(2026, 1, 15, 10, 10, 0, tzinfo=timezone.utc)
        offset = compute_combined_offset(tag, files, camera_timezone="UTC")
        assert offset == pytest.approx(600.0)  # 10 minutes

    def test_tag_in_second_file(self):
        files = [
            _make_recording("2026-01-15 10:00:00", "2026-01-15 10:30:00"),
            _make_recording("2026-01-15 10:30:00", "2026-01-15 11:00:00"),
        ]
        tag = datetime(2026, 1, 15, 10, 45, 0, tzinfo=timezone.utc)
        offset = compute_combined_offset(tag, files, camera_timezone="UTC")
        # 30 min (first file) + 15 min into second file = 45 min = 2700s
        assert offset == pytest.approx(2700.0)

    def test_tag_before_all_files_returns_none(self):
        files = [_make_recording("2026-01-15 10:00:00", "2026-01-15 10:30:00")]
        tag = datetime(2026, 1, 15, 9, 50, 0, tzinfo=timezone.utc)
        assert compute_combined_offset(tag, files, camera_timezone="UTC") is None

    def test_tag_after_all_files_returns_none(self):
        files = [_make_recording("2026-01-15 10:00:00", "2026-01-15 10:30:00")]
        tag = datetime(2026, 1, 15, 11, 0, 0, tzinfo=timezone.utc)
        assert compute_combined_offset(tag, files, camera_timezone="UTC") is None

    def test_tag_in_gap_within_tolerance_snaps(self):
        files = [
            _make_recording("2026-01-15 10:00:00", "2026-01-15 10:30:00"),
            _make_recording("2026-01-15 10:30:03", "2026-01-15 11:00:00"),
        ]
        # Tag falls in the 3-second gap
        tag = datetime(2026, 1, 15, 10, 30, 1, tzinfo=timezone.utc)
        offset = compute_combined_offset(tag, files, camera_timezone="UTC")
        # Should snap to end of first file = 1800s
        assert offset == pytest.approx(1800.0)

    def test_skipped_files_are_excluded(self):
        files = [
            _make_recording("2026-01-15 10:00:00", "2026-01-15 10:10:00"),
            _make_recording("2026-01-15 10:10:00", "2026-01-15 10:20:00", skip=True),
            _make_recording("2026-01-15 10:20:00", "2026-01-15 10:30:00"),
        ]
        # Tag at 10:25 — skipped file excluded, so combined = 10min (file1) + 5min into file3
        tag = datetime(2026, 1, 15, 10, 25, 0, tzinfo=timezone.utc)
        offset = compute_combined_offset(tag, files, camera_timezone="UTC")
        assert offset == pytest.approx(900.0)  # 600 + 300

    def test_empty_files_returns_none(self):
        assert (
            compute_combined_offset(
                datetime.now(timezone.utc), [], camera_timezone="UTC"
            )
            is None
        )

    def test_tag_at_exact_file_boundary(self):
        files = [
            _make_recording("2026-01-15 10:00:00", "2026-01-15 10:30:00"),
            _make_recording("2026-01-15 10:30:00", "2026-01-15 11:00:00"),
        ]
        tag = datetime(2026, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        offset = compute_combined_offset(tag, files, camera_timezone="UTC")
        # Exactly at boundary — falls in both files, first match wins (end of file 1)
        assert offset is not None
        assert offset == pytest.approx(1800.0)

    def test_tag_just_after_last_file_within_tolerance(self):
        files = [_make_recording("2026-01-15 10:00:00", "2026-01-15 10:30:00")]
        tag = datetime(2026, 1, 15, 10, 30, 2, tzinfo=timezone.utc)
        offset = compute_combined_offset(
            tag, files, camera_timezone="UTC", gap_tolerance_seconds=5.0
        )
        # Within 5s tolerance after last file end — snaps to end
        assert offset == pytest.approx(1800.0)


# ---------------------------------------------------------------------------
# compute_trimmed_offset
# ---------------------------------------------------------------------------


class TestComputeTrimmedOffset:
    def test_basic_offset(self):
        # Combined offset at 1800s, trim starts at 600s → trimmed = 1200s
        assert compute_trimmed_offset(1800.0, "00:10:00") == pytest.approx(1200.0)

    def test_before_trim_start_returns_none(self):
        # Combined offset at 300s, but trim starts at 600s → moment cut away
        assert compute_trimmed_offset(300.0, "00:10:00") is None

    def test_zero_trim_start(self):
        assert compute_trimmed_offset(500.0, "00:00:00") == pytest.approx(500.0)

    def test_empty_trim_start(self):
        assert compute_trimmed_offset(500.0, "") == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# compute_clip_boundaries
# ---------------------------------------------------------------------------


class TestComputeClipBoundaries:
    def test_normal_boundaries(self):
        start, end = compute_clip_boundaries(100.0, buffer_seconds=15.0)
        assert start == pytest.approx(85.0)
        assert end == pytest.approx(115.0)

    def test_clamps_start_to_zero(self):
        start, end = compute_clip_boundaries(5.0, buffer_seconds=15.0)
        assert start == pytest.approx(0.0)
        assert end == pytest.approx(20.0)

    def test_clamps_end_to_duration(self):
        start, end = compute_clip_boundaries(
            95.0, buffer_seconds=15.0, video_duration=100.0
        )
        assert start == pytest.approx(80.0)
        assert end == pytest.approx(100.0)

    def test_no_duration_no_clamp(self):
        start, end = compute_clip_boundaries(95.0, buffer_seconds=15.0)
        assert end == pytest.approx(110.0)

    def test_custom_buffer(self):
        start, end = compute_clip_boundaries(60.0, buffer_seconds=30.0)
        assert start == pytest.approx(30.0)
        assert end == pytest.approx(90.0)
