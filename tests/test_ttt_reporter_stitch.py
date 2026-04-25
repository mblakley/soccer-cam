"""Tests for TTTReporter._persist_stitch_profile — soccer-cam's half of Phase 4 sync.

TTT serves the profile on the device-link config response. When soccer-cam pulls
config, any stitch_profile is written to the path configured at
`processing.seam_realign_profile_path` so CombineTask can find it.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from video_grouper.api_integrations.ttt_reporter import TTTReporter


SAMPLE_PROFILE = {
    "source_width": 7680,
    "source_height": 2160,
    "seam_x": 3840,
    "dx_anchors": [[0, -10], [477, -20], [657, -35], [1500, 0], [2160, 0]],
}


def _make_reporter(profile_path: str | None) -> TTTReporter:
    """Build a reporter with a config object shaped like video_grouper's Config."""
    config = SimpleNamespace(
        ttt=SimpleNamespace(camera_id=None, ttt_sync_enabled=False),
        processing=SimpleNamespace(
            seam_realign_enabled=True,
            seam_realign_profile_path=profile_path,
        ),
    )
    return TTTReporter(ttt_client=None, config=config)


def test_writes_profile_to_disk(tmp_path: Path) -> None:
    target = tmp_path / "stitch_profile.json"
    reporter = _make_reporter(str(target))
    reporter._persist_stitch_profile(dict(SAMPLE_PROFILE))
    assert target.exists()
    assert json.loads(target.read_text()) == SAMPLE_PROFILE


def test_creates_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir" / "stitch_profile.json"
    reporter = _make_reporter(str(target))
    reporter._persist_stitch_profile(dict(SAMPLE_PROFILE))
    assert target.exists()


def test_clears_file_when_profile_is_none(tmp_path: Path) -> None:
    target = tmp_path / "stitch_profile.json"
    target.write_text(json.dumps(SAMPLE_PROFILE))
    reporter = _make_reporter(str(target))
    reporter._persist_stitch_profile(None)
    assert not target.exists()


def test_noop_when_no_profile_and_no_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "stitch_profile.json"
    reporter = _make_reporter(str(target))
    reporter._persist_stitch_profile(None)  # should not raise
    assert not target.exists()


def test_noop_when_no_target_path_configured() -> None:
    reporter = _make_reporter(profile_path=None)
    reporter._persist_stitch_profile(dict(SAMPLE_PROFILE))  # should not raise


def test_does_not_rewrite_unchanged_content(tmp_path: Path) -> None:
    target = tmp_path / "stitch_profile.json"
    reporter = _make_reporter(str(target))
    reporter._persist_stitch_profile(dict(SAMPLE_PROFILE))
    mtime1 = target.stat().st_mtime_ns

    # Second call with identical profile should not re-write (atomic replace
    # would update mtime even for identical content; we suppress via content cmp).
    reporter._persist_stitch_profile(dict(SAMPLE_PROFILE))
    mtime2 = target.stat().st_mtime_ns
    assert mtime1 == mtime2


def test_handles_config_without_processing_section() -> None:
    reporter = TTTReporter(
        ttt_client=None,
        config=SimpleNamespace(
            ttt=SimpleNamespace(camera_id=None, ttt_sync_enabled=False)
        ),
    )
    # No `.processing` on config — must be a silent no-op, not an AttributeError
    reporter._persist_stitch_profile(dict(SAMPLE_PROFILE))
