"""Regression: YoutubeUploadTask must not re-upload a video that already landed.

The task uploads processed + raw and marks the group ``complete`` only
when BOTH succeed. So any failure of the second upload re-ran the first
on the next attempt: a duplicate video on YouTube, and 1,600 wasted
quota units out of a daily allowance of exactly six uploads.

Observed live 2026-08-03: 07.06's processed video went up twice
(YgErRwoODek, then _DahHq3Q3VQ after a mid-raw-upload service restart),
and that wasted unit is what pushed the last raw upload of the night
past the quota with two games still unuploaded.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from video_grouper.models import DirectoryState
from video_grouper.task_processors.tasks.upload.youtube_upload_task import (
    YoutubeUploadTask,
)


@pytest.fixture(autouse=True)
def mock_file_system():
    yield None


@pytest.fixture(autouse=True)
def mock_ffmpeg():
    yield None


def _make_group(tmp_path):
    storage = tmp_path
    group = storage / "2026.07.11-10.56.44"
    videos = group / "2026.07.11 - Team vs Opp (Venue)"
    videos.mkdir(parents=True)
    (videos / "g-raw.mp4").write_bytes(b"x" * 16)
    (videos / "g.mp4").write_bytes(b"x" * 16)
    (group / "match_info.ini").write_text(
        "[MATCH]\nmy_team_name = Team\nopponent_team_name = Opp\n"
        "location = Venue\nstart_time_offset = 00:00\ntotal_duration = \n",
        encoding="utf-8",
    )
    # get_youtube_paths(storage) expects a credentials file to exist.
    (storage / "client_secret.json").write_text("{}", encoding="utf-8")
    return storage, group


async def _run(tmp_path, already):
    storage, group = _make_group(tmp_path)
    if already:
        st = DirectoryState(str(group), str(storage))
        for kind, vid in already.items():
            st.record_uploaded_video(kind, vid)

    uploaded: list[str] = []

    def fake_upload(path, title, description, privacy_status=None, playlist_id=None):
        uploaded.append(os.path.basename(path))
        return "NEWID-" + os.path.basename(path)

    cfg = MagicMock()
    cfg.privacy_status = "unlisted"

    with (
        patch(
            "video_grouper.utils.youtube_upload.get_youtube_paths",
            return_value=(
                str(storage / "client_secret.json"),
                str(storage / "tok.json"),
            ),
        ),
        patch("video_grouper.utils.youtube_upload.YouTubeUploader") as uploader_cls,
        patch.object(
            YoutubeUploadTask,
            "_get_playlist_names",
            return_value=("Processed PL", "Raw PL"),
        ),
    ):
        uploader_cls.return_value.upload_video = fake_upload
        uploader_cls.return_value.get_or_create_playlist = lambda *a, **k: "PLID"
        task = YoutubeUploadTask(group_dir=str(group))
        ok = await task.execute(youtube_config=cfg, storage_path=str(storage))
    return ok, uploaded, DirectoryState(str(group), str(storage)).get_uploaded_videos()


@pytest.mark.asyncio
async def test_first_run_uploads_both_and_records_ids(tmp_path):
    ok, uploaded, recorded = await _run(tmp_path, already=None)
    assert ok is True
    assert sorted(uploaded) == ["g-raw.mp4", "g.mp4"]
    assert recorded["processed"] == "NEWID-g.mp4"
    assert recorded["raw"] == "NEWID-g-raw.mp4"


@pytest.mark.asyncio
async def test_retry_skips_the_processed_video_already_on_youtube(tmp_path):
    """The exact 2026-08-03 case: processed landed, raw failed on quota."""
    ok, uploaded, _ = await _run(tmp_path, already={"processed": "ALREADY"})
    assert uploaded == ["g-raw.mp4"], (
        "processed video was re-uploaded despite already being on YouTube -- "
        "that is a duplicate video and 1/6th of the daily quota"
    )
    assert ok is True


@pytest.mark.asyncio
async def test_retry_with_both_recorded_uploads_nothing(tmp_path):
    ok, uploaded, _ = await _run(tmp_path, already={"processed": "A", "raw": "B"})
    assert uploaded == []
    assert ok is True
