"""Tests for the recording_locator helpers."""

from __future__ import annotations

import os

from video_grouper.task_processors import recording_locator
from video_grouper.task_processors.recording_locator import (
    find_combined_video,
    find_processed_video,
    resolve_recording_dir,
)


def test_resolve_returns_none_when_dir_is_blank(tmp_path):
    assert resolve_recording_dir(str(tmp_path), None) is None
    assert resolve_recording_dir(str(tmp_path), "") is None


def test_resolve_uses_absolute_path_when_it_exists(tmp_path):
    abs_dir = tmp_path / "recording-abs"
    abs_dir.mkdir()

    result = resolve_recording_dir(str(tmp_path / "storage"), str(abs_dir))

    assert result == str(abs_dir)


def test_resolve_falls_back_to_storage_path(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    group = storage / "2026.03.08 - Flash vs IYSA (home)"
    group.mkdir()

    result = resolve_recording_dir(str(storage), "2026.03.08 - Flash vs IYSA (home)")

    assert result == str(group)


def test_resolve_returns_none_when_nothing_matches(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()

    result = resolve_recording_dir(str(storage), "missing-dir")

    assert result is None


def test_find_combined_video_direct_hit(tmp_path):
    group = tmp_path / "group"
    group.mkdir()
    combined = group / "combined.mp4"
    combined.write_bytes(b"x")

    assert find_combined_video(str(group)) == str(combined)


def test_find_combined_video_in_subdir(tmp_path):
    group = tmp_path / "group"
    group.mkdir()
    sub = group / "trimmed"
    sub.mkdir()
    combined = sub / "combined.mp4"
    combined.write_bytes(b"x")

    assert find_combined_video(str(group)) == str(combined)


def test_find_combined_video_missing(tmp_path):
    group = tmp_path / "group"
    group.mkdir()

    assert find_combined_video(str(group)) is None


def test_find_combined_video_unreadable_dir(tmp_path):
    """A nonexistent directory returns None instead of raising."""
    missing = tmp_path / "does-not-exist"

    assert find_combined_video(str(missing)) is None


def test_find_combined_video_ignores_nested_subdirs(tmp_path):
    """Only one level of subdirectory is searched."""
    group = tmp_path / "group"
    group.mkdir()
    deep = group / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "combined.mp4").write_bytes(b"x")

    assert find_combined_video(str(group)) is None


def test_find_combined_video_subdir_no_combined(tmp_path):
    """Subdirectories without combined.mp4 are skipped."""
    group = tmp_path / "group"
    group.mkdir()
    (group / "metadata").mkdir()
    (group / "metadata" / "state.json").write_text("{}")

    assert find_combined_video(str(group)) is None


def test_resolve_storage_path_join_handles_windows_separators(tmp_path):
    """Paths use os.path.join so the result is OS-native."""
    storage = tmp_path / "storage"
    nested = storage / "sub" / "group"
    nested.mkdir(parents=True)

    result = resolve_recording_dir(str(storage), os.path.join("sub", "group"))

    assert result == str(nested)


def test_find_processed_video_in_subdir(tmp_path):
    """The processed <slug>.mp4 sibling of <slug>-raw.mp4 is found one dir deep."""
    group = tmp_path / "group"
    sub = group / "2026.03.08 - Flash vs IYSA (home)"
    sub.mkdir(parents=True)
    (sub / "flash-iysa-home-03-08-2026-raw.mp4").write_bytes(b"x")
    processed = sub / "flash-iysa-home-03-08-2026.mp4"
    processed.write_bytes(b"x")

    assert find_processed_video(str(group)) == str(processed)


def test_find_processed_video_direct_hit(tmp_path):
    group = tmp_path / "group"
    group.mkdir()
    (group / "flash-iysa-home-03-08-2026-raw.mp4").write_bytes(b"x")
    processed = group / "flash-iysa-home-03-08-2026.mp4"
    processed.write_bytes(b"x")

    assert find_processed_video(str(group)) == str(processed)


def test_find_processed_video_missing_when_only_raw(tmp_path):
    """A raw full-field input with no processed sibling yields None."""
    group = tmp_path / "group"
    sub = group / "sub"
    sub.mkdir(parents=True)
    (sub / "flash-iysa-home-03-08-2026-raw.mp4").write_bytes(b"x")

    assert find_processed_video(str(group)) is None


def test_find_processed_video_ignores_combined_only(tmp_path):
    """combined.mp4 alone is not a processed video."""
    group = tmp_path / "group"
    sub = group / "sub"
    sub.mkdir(parents=True)
    (sub / "combined.mp4").write_bytes(b"x")

    assert find_processed_video(str(group)) is None


def test_find_processed_video_unreadable_dir(tmp_path):
    assert find_processed_video(str(tmp_path / "does-not-exist")) is None


def _group_with(tmp_path, status):
    import json

    (tmp_path / "state.json").write_text(json.dumps({"status": status}))
    raw = tmp_path / "heat-vs-rivals-home-12-05-2026-raw.mp4"
    raw.write_bytes(b"0")
    return raw, tmp_path / "heat-vs-rivals-home-12-05-2026.mp4"


def test_ready_broadcast_is_used_once_rendered_and_current(tmp_path):
    raw, broadcast = _group_with(tmp_path, "pipeline_complete")
    # Before it exists: clip from the trimmed full-field video.
    assert recording_locator.ready_broadcast_for(str(raw), str(tmp_path)) == str(raw)
    broadcast.write_bytes(b"0")
    # Finished + newer than the trim: the broadcast view (decided 2026-11-26).
    assert recording_locator.ready_broadcast_for(str(raw), str(tmp_path)) == str(
        broadcast
    )


def test_a_broadcast_mid_render_is_not_ready(tmp_path):
    raw, broadcast = _group_with(tmp_path, "trimmed")
    broadcast.write_bytes(b"0")
    assert recording_locator.ready_broadcast_for(str(raw), str(tmp_path)) == str(raw)


def test_a_stale_broadcast_is_not_ready(tmp_path):
    import os

    raw, broadcast = _group_with(tmp_path, "pipeline_complete")
    broadcast.write_bytes(b"0")
    st = os.stat(raw)
    os.utime(broadcast, (st.st_atime, st.st_mtime - 60))
    assert recording_locator.ready_broadcast_for(str(raw), str(tmp_path)) == str(raw)


def test_ready_broadcast_leaves_other_paths_alone(tmp_path):
    other = tmp_path / "combined.mp4"
    assert recording_locator.ready_broadcast_for(str(other), str(tmp_path)) == str(
        other
    )


def test_find_trimmed_raw_video_in_game_subdir(tmp_path):
    game = tmp_path / "2026.12.05 - Heat vs Rivals (home)"
    game.mkdir()
    (tmp_path / "combined.mp4").write_bytes(b"0")
    raw = game / "heat-vs-rivals-home-12-05-2026-raw.mp4"
    raw.write_bytes(b"0")
    assert recording_locator.find_trimmed_raw_video(str(tmp_path)) == str(raw)
    assert recording_locator.find_trimmed_raw_video(str(game / "missing")) is None
