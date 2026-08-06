"""Tests for the archive step.

The ordering guarantee is the thing under test: copy -> verify -> record ->
delete. An earlier ad-hoc version deleted first and recorded nothing, and an
interrupted run left a group that the pipeline read as mid-processing and
began reprocessing — a game already published on YouTube.
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from video_grouper.models import DirectoryState, RecordingFile
from video_grouper.task_processors.archive_processor import ArchiveProcessor
from video_grouper.task_processors.tasks.archive import ArchiveTask
from video_grouper.utils.config import (
    AppConfig,
    ArchiveConfig,
    AutocamConfig,
    CameraConfig,
    CloudSyncConfig,
    Config,
    LoggingConfig,
    NtfyConfig,
    PlayMetricsConfig,
    ProcessingConfig,
    RecordingConfig,
    StorageConfig,
    TeamSnapConfig,
    YouTubeConfig,
)
from video_grouper.utils.paths import get_camera_state_path


@pytest.fixture(autouse=True)
def real_filesystem(mock_file_system):
    """Undo conftest's global filesystem mocks for this module.

    These tests copy, hash and delete actual bytes — the point is that the
    ordering holds against a real disk. The autouse ``mock_file_system``
    fixture makes ``os.path.exists`` always True, ``getsize`` always 1 MB and
    ``makedirs`` a no-op, which would make every assertion here meaningless.
    """
    mock_file_system["exists"].side_effect = os.path.lexists
    mock_file_system["getsize"].side_effect = lambda p: os.stat(p).st_size
    mock_file_system["makedirs"].side_effect = lambda p, *a, **kw: Path(p).mkdir(
        parents=True, exist_ok=True
    )
    mock_file_system["access"].side_effect = lambda *a, **kw: True
    return mock_file_system


@pytest.fixture
def archive_root(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()
    return str(root)


@pytest.fixture
def archive_config(temp_storage):
    """A real Config — conftest's mock_config is a ConfigParser stand-in."""
    return Config(
        cameras=[
            CameraConfig(
                name="default",
                type="dahua",
                device_ip="127.0.0.1",
                username="admin",
                password="password",
            )
        ],
        storage=StorageConfig(path=temp_storage),
        archive=ArchiveConfig(),
        recording=RecordingConfig(),
        processing=ProcessingConfig(),
        logging=LoggingConfig(),
        app=AppConfig(storage_path=temp_storage, check_interval_seconds=1),
        teamsnap=TeamSnapConfig(enabled=False, team_id="1", my_team_name="Team A"),
        teamsnap_teams=[],
        playmetrics=PlayMetricsConfig(
            enabled=False, username="u", password="p", team_name="Team A"
        ),
        playmetrics_teams=[],
        ntfy=NtfyConfig(enabled=False, server_url="http://ntfy.sh", topic="test"),
        youtube=YouTubeConfig(enabled=False),
        autocam=AutocamConfig(enabled=False),
        cloud_sync=CloudSyncConfig(enabled=False),
    )


async def _make_group(
    storage, name, *, status="complete", newest=None, payload=b"x" * 4096
):
    """A completed group with one real video file on disk."""
    group_dir = os.path.join(storage, name)
    # Path.mkdir, not os.makedirs — conftest patches the latter to a no-op.
    Path(group_dir).mkdir(parents=True, exist_ok=True)
    video = os.path.join(group_dir, "game.mp4")
    Path(video).write_bytes(payload)

    newest = newest or datetime(2026, 7, 12, 10, 35, 15)
    dir_state = DirectoryState(group_dir)
    await dir_state.add_file(
        video,
        RecordingFile(
            start_time=newest - timedelta(minutes=30),
            end_time=newest,
            file_path=video,
            status="complete",
        ),
    )
    await dir_state.update_group_status(status)

    match_info = os.path.join(group_dir, "match_info.ini")
    Path(match_info).write_text(
        "[MATCH]\nmy_team_name = Heat\nopponent_team_name = Kenmore\nlocation = Away\n",
        encoding="utf-8",
    )
    return group_dir


class TestArchiveOrdering:
    @pytest.mark.asyncio
    async def test_group_is_recorded_archived_before_anything_is_deleted(
        self, temp_storage, archive_root
    ):
        """The interruption that nearly re-published a game.

        If the process dies mid-delete, whatever survives must still read as
        finished. That only holds if the terminal status is written first.
        """
        group_dir = await _make_group(temp_storage, "2026.07.12-10.00.00")
        task = ArchiveTask(group_dir=group_dir)

        recorded_when_first_delete_happened = {}
        real_remove = os.remove

        def spy_remove(path, *a, **kw):
            # Ignore FileLock's own .lock cleanup — that is bookkeeping, not
            # a deletion of the game's content.
            if not str(path).endswith(".lock") and (
                "status" not in recorded_when_first_delete_happened
            ):
                state_file = os.path.join(group_dir, "state.json")
                with open(state_file) as fh:
                    recorded_when_first_delete_happened["status"] = json.load(fh).get(
                        "status"
                    )
            return real_remove(path, *a, **kw)

        os.remove = spy_remove
        try:
            ok = await task.execute(
                archive_root=archive_root,
                watermark=datetime(2026, 7, 12, 10, 35, 15),
            )
        finally:
            os.remove = real_remove

        assert ok
        assert recorded_when_first_delete_happened["status"] == "archived", (
            "status must already be 'archived' before the first file is removed"
        )

    @pytest.mark.asyncio
    async def test_verified_copy_lands_and_local_space_is_reclaimed(
        self, temp_storage, archive_root
    ):
        group_dir = await _make_group(temp_storage, "2026.07.12-10.00.00")

        ok = await ArchiveTask(group_dir=group_dir).execute(
            archive_root=archive_root, watermark=datetime(2026, 7, 12, 10, 35, 15)
        )

        assert ok
        assert not os.path.exists(group_dir)
        archived = os.path.join(
            archive_root, "Heat", "2026.07.12 - vs Kenmore (away)", "game.mp4"
        )
        assert os.path.exists(archived)
        assert os.path.getsize(archived) == 4096

    @pytest.mark.asyncio
    async def test_delete_after_verify_off_keeps_the_local_copy(
        self, temp_storage, archive_root
    ):
        group_dir = await _make_group(temp_storage, "2026.07.12-10.00.00")

        ok = await ArchiveTask(group_dir=group_dir).execute(
            archive_root=archive_root,
            delete_after_verify=False,
            watermark=datetime(2026, 7, 12, 10, 35, 15),
        )

        assert ok
        assert os.path.exists(os.path.join(group_dir, "game.mp4"))
        assert DirectoryState(group_dir).status == "archived"

    @pytest.mark.asyncio
    async def test_rerunning_an_archived_group_is_a_no_op(
        self, temp_storage, archive_root
    ):
        group_dir = await _make_group(
            temp_storage, "2026.07.12-10.00.00", status="archived"
        )

        ok = await ArchiveTask(group_dir=group_dir).execute(
            archive_root=archive_root, watermark=datetime(2026, 7, 12, 10, 35, 15)
        )

        assert ok
        assert os.path.exists(os.path.join(group_dir, "game.mp4")), (
            "an already-archived group must not be touched again"
        )


class TestWatermarkGuard:
    @pytest.mark.asyncio
    async def test_refuses_to_archive_ahead_of_the_watermark(
        self, temp_storage, archive_root
    ):
        """Archiving a game the poller has not finished with would let it be
        rediscovered as new and downloaded again — the whole bug."""
        group_dir = await _make_group(temp_storage, "2026.07.12-10.00.00")

        ok = await ArchiveTask(group_dir=group_dir).execute(
            archive_root=archive_root,
            # Watermark is BEHIND the group's newest recording.
            watermark=datetime(2026, 7, 12, 9, 0, 0),
        )

        assert ok is False
        assert os.path.exists(os.path.join(group_dir, "game.mp4"))
        assert DirectoryState(group_dir).status == "complete", (
            "a refused archive must not mark the group archived"
        )

    @pytest.mark.asyncio
    async def test_refuses_when_there_is_no_watermark_at_all(
        self, temp_storage, archive_root
    ):
        group_dir = await _make_group(temp_storage, "2026.07.12-10.00.00")

        ok = await ArchiveTask(group_dir=group_dir).execute(
            archive_root=archive_root, watermark=None
        )

        assert ok is False
        assert os.path.exists(os.path.join(group_dir, "game.mp4"))

    @pytest.mark.asyncio
    async def test_processor_takes_the_slowest_camera_watermark(
        self, temp_storage, archive_config
    ):
        """With several cameras, only the slowest one's mark is safe: a fast
        camera must not license deleting footage another is still behind on."""
        state_path = get_camera_state_path(temp_storage)
        Path(state_path).write_text(
            json.dumps(
                {
                    "is_connected": False,  # legacy top-level key, must be skipped
                    "fast_cam": {"latest_video_time": "2026-07-12 10:35:15"},
                    "slow_cam": {"latest_video_time": "2026-07-01 08:00:00"},
                }
            ),
            encoding="utf-8",
        )

        processor = ArchiveProcessor(temp_storage, archive_config)

        assert processor._read_watermark() == datetime(2026, 7, 1, 8, 0, 0)


class TestArchiveIsOptIn:
    @pytest.mark.asyncio
    async def test_disabled_by_default_does_nothing(self, temp_storage, archive_config):
        assert archive_config.archive.enabled is False
        processor = ArchiveProcessor(temp_storage, archive_config)
        task = Mock(spec=ArchiveTask)
        task.group_dir = os.path.join(temp_storage, "grp")
        task.execute = AsyncMock()

        await processor.process_item(task)
        task.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_archive_path_refuses_rather_than_guessing(
        self, temp_storage
    ):
        group_dir = await _make_group(temp_storage, "2026.07.12-10.00.00")

        ok = await ArchiveTask(group_dir=group_dir).execute(
            archive_root="", watermark=datetime(2026, 7, 12, 10, 35, 15)
        )

        assert ok is False
        assert os.path.exists(os.path.join(group_dir, "game.mp4"))
