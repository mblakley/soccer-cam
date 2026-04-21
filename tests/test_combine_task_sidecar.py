"""Tests that CombineTask writes a stitch sidecar JSON when configured."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


from video_grouper.task_processors.tasks.video.combine_task import CombineTask


def _make_task(
    tmp_path: Path, *, enabled: bool, profile_path: str | None
) -> CombineTask:
    task = CombineTask(group_dir=str(tmp_path))
    task.storage_path = str(tmp_path)
    task.config = SimpleNamespace(
        processing=SimpleNamespace(
            seam_realign_enabled=enabled,
            seam_realign_profile_path=profile_path,
        )
    )
    return task


def _profile_json() -> dict:
    return {
        "source_width": 7680,
        "source_height": 2160,
        "seam_x": 3840,
        "dx_anchors": [[0, -10], [477, -20], [657, -35], [1500, 0], [2160, 0]],
    }


def test_sidecar_written_when_enabled(tmp_path: Path) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(_profile_json()))
    task = _make_task(tmp_path, enabled=True, profile_path=str(profile))

    output = tmp_path / "combined.mp4"
    task._write_stitch_sidecar(str(output))

    sidecar = tmp_path / "combined.mp4.stitch.json"
    assert sidecar.exists()
    got = json.loads(sidecar.read_text())
    assert got == _profile_json()


def test_sidecar_not_written_when_disabled(tmp_path: Path) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(_profile_json()))
    task = _make_task(tmp_path, enabled=False, profile_path=str(profile))
    task._write_stitch_sidecar(str(tmp_path / "combined.mp4"))
    assert not (tmp_path / "combined.mp4.stitch.json").exists()


def test_sidecar_not_written_when_no_profile_path(tmp_path: Path) -> None:
    task = _make_task(tmp_path, enabled=True, profile_path=None)
    task._write_stitch_sidecar(str(tmp_path / "combined.mp4"))
    assert not (tmp_path / "combined.mp4.stitch.json").exists()


def test_sidecar_not_written_when_profile_missing_on_disk(tmp_path: Path) -> None:
    task = _make_task(tmp_path, enabled=True, profile_path=str(tmp_path / "nope.json"))
    task._write_stitch_sidecar(str(tmp_path / "combined.mp4"))
    assert not (tmp_path / "combined.mp4.stitch.json").exists()


def test_sidecar_write_failure_is_non_fatal(tmp_path: Path, monkeypatch) -> None:
    """If the write fails, the method should log and return, not raise."""
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(_profile_json()))
    task = _make_task(tmp_path, enabled=True, profile_path=str(profile))

    import video_grouper.task_processors.tasks.video.combine_task as mod

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "write_profile", _boom)
    # Should not raise
    task._write_stitch_sidecar(str(tmp_path / "combined.mp4"))


def test_config_without_processing_is_safe(tmp_path: Path) -> None:
    """Older code paths may pass a bare object without .processing — stay defensive."""
    task = CombineTask(group_dir=str(tmp_path))
    task.storage_path = str(tmp_path)
    task.config = SimpleNamespace()  # no `.processing`
    task._write_stitch_sidecar(str(tmp_path / "combined.mp4"))
    assert not (tmp_path / "combined.mp4.stitch.json").exists()
