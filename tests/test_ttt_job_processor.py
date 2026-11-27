"""Tests for the TTTJobProcessor, against the work-source interface
(FakeWorkSource). The TTT mapping (claim_job / update_job_progress /
complete_job / fail_job) is tested in
tests/integrations/work/test_ttt_work_source.py."""

import os
import tempfile
from unittest.mock import Mock

import pytest

from video_grouper.integrations.work import PROCESS_RECORDING, WorkItem
from video_grouper.integrations.work.fake import FakeWorkSource, WorkCall
from video_grouper.integrations.work.sources.null import NullWorkSource
from video_grouper.task_processors.tasks.ttt.ttt_job_task import TTTJobTask
from video_grouper.task_processors.ttt_job_processor import TTTJobProcessor
from video_grouper.utils.config import (
    AppConfig,
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
    TTTConfig,
    YouTubeConfig,
)


@pytest.fixture(autouse=True)
def mock_file_system():
    """Override the global autouse mock_file_system to use real filesystem."""
    yield {}


@pytest.fixture
def ttt_storage():
    """Create a temporary storage directory for TTT job tests."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def ttt_config(ttt_storage):
    """Create a configuration with TTT enabled."""
    return Config(
        cameras=[
            CameraConfig(
                name="test-camera",
                type="dahua",
                device_ip="127.0.0.1",
                username="admin",
                password="password",
            )
        ],
        storage=StorageConfig(path=ttt_storage),
        recording=RecordingConfig(),
        processing=ProcessingConfig(),
        logging=LoggingConfig(),
        app=AppConfig(
            storage_path=ttt_storage,
            check_interval_seconds=1,
            timezone="America/New_York",
        ),
        teamsnap=TeamSnapConfig(enabled=False, team_id="1", my_team_name="Team A"),
        playmetrics=PlayMetricsConfig(
            enabled=False, username="user", password="pass", team_name="Team A"
        ),
        ntfy=NtfyConfig(enabled=False, server_url="http://ntfy.sh", topic="test"),
        youtube=YouTubeConfig(enabled=False),
        autocam=AutocamConfig(enabled=False),
        cloud_sync=CloudSyncConfig(enabled=False),
        ttt=TTTConfig(
            enabled=True,
            job_polling_enabled=True,
            machine_name="test-machine",
            supabase_url="https://test.supabase.co",
            anon_key="test-key",
            api_base_url="https://ttt.example.com",
        ),
    )


@pytest.fixture
def work():
    """An in-memory work source that records every report."""
    return FakeWorkSource()


@pytest.fixture
def processor(ttt_storage, ttt_config, work):
    """A TTTJobProcessor reporting to the fake work source."""
    return TTTJobProcessor(storage_path=ttt_storage, config=ttt_config, work=work)


def _job_item(job_id="job-1"):
    return WorkItem(PROCESS_RECORDING, job_id, {"id": job_id})


class TestTTTJobProcessorInit:
    """Tests for TTTJobProcessor initialization."""

    def test_init_stores_config(self, processor, ttt_config):
        assert processor.config is ttt_config

    def test_init_stores_work_source(self, processor, work):
        assert processor.work is work

    def test_init_defaults_to_no_work_source(self, ttt_storage, ttt_config):
        proc = TTTJobProcessor(storage_path=ttt_storage, config=ttt_config)
        assert isinstance(proc.work, NullWorkSource)

    def test_init_stores_optional_processors(self, ttt_storage, ttt_config):
        download = Mock()
        video = Mock()
        upload = Mock()
        camera = Mock()
        proc = TTTJobProcessor(
            storage_path=ttt_storage,
            config=ttt_config,
            camera=camera,
            download_processor=download,
            video_processor=video,
            upload_processor=upload,
        )
        assert proc.camera is camera
        assert proc.download_processor is download
        assert proc.video_processor is video
        assert proc.upload_processor is upload


# Service registration + heartbeat + polling moved to TTTPoller —
# their coverage now lives in test_ttt_poller.py.


@pytest.mark.asyncio
async def test_add_work_dedups_on_ttt_id(processor):
    """``add_work`` keys dedup on ``ttt_id`` so the same job queued
    twice only produces one queue entry."""
    await processor.add_work(TTTJobTask(ttt_id="job-1", payload={"id": "job-1"}))
    await processor.add_work(TTTJobTask(ttt_id="job-1", payload={"id": "job-1"}))
    await processor.add_work(TTTJobTask(ttt_id="job-2", payload={"id": "job-2"}))
    assert processor._queue.qsize() == 2


class TestProcessJob:
    """Tests for _process_job."""

    @pytest.mark.asyncio
    async def test_process_job_claim_failure(self, processor, work):
        work.errors["claim"] = Exception("Already claimed")
        await processor._process_job({"id": "job-1", "config": {}})
        assert [c.verb for c in work.calls] == ["claim"]

    @pytest.mark.asyncio
    async def test_process_job_lost_claim_is_skipped(self, processor, work):
        work.claims["job-1"] = None
        await processor._process_job({"id": "job-1", "config": {}})
        assert [c.verb for c in work.calls] == ["claim"]

    @pytest.mark.asyncio
    async def test_process_job_no_group_dir(self, processor, work):
        job = {"id": "job-1", "config": {}}
        await processor._process_job(job)
        assert work.called("fail") == [
            WorkCall(
                "fail",
                PROCESS_RECORDING,
                "job-1",
                ("Could not resolve recording group directory",),
            )
        ]

    @pytest.mark.asyncio
    async def test_process_job_success_reports_each_step_then_completes(
        self, processor, work, ttt_storage
    ):
        group = os.path.join(ttt_storage, "2025-01-01_game")
        os.makedirs(group)
        with open(os.path.join(group, "combined.mp4"), "wb") as f:
            f.write(b"\x00")

        await processor.process_item(
            TTTJobTask(
                ttt_id="job-1",
                payload={
                    "id": "job-1",
                    "config": {"recording_group_dir": "2025-01-01_game"},
                },
            )
        )

        assert [c.verb for c in work.calls] == [
            "claim",
            "progress",
            "progress",
            "progress",
            "complete",
        ]
        assert [c.args for c in work.called("progress")] == [
            ("downloading", 0, {"message": "Starting download from camera"}),
            ("combining", 30, {"message": "Combining video files"}),
            ("uploading", 70, {"message": "Uploading to YouTube"}),
        ]
        # YouTube uploads are off in this config -> no URL in the result.
        assert work.called("complete")[0].args == ({},)

    @pytest.mark.asyncio
    async def test_process_job_error_fails_the_job(self, processor, work):
        processor._resolve_or_create_group = Mock(side_effect=RuntimeError("disk gone"))
        await processor._process_job({"id": "job-1", "config": {}})
        assert work.called("fail")[0].args == ("disk gone",)


# NOTE: ``_processing_jobs`` set is gone — QueueProcessor's
# ``_queued_items`` dedup handles it now. See test_add_work_dedup below.


class TestProgressAndFailure:
    """Tests for progress reporting and failure handling."""

    @pytest.mark.asyncio
    async def test_update_progress(self, processor, work):
        await processor._update_progress(_job_item(), "downloading", 50, "Halfway")
        assert work.called("progress") == [
            WorkCall(
                "progress",
                PROCESS_RECORDING,
                "job-1",
                ("downloading", 50, {"message": "Halfway"}),
            )
        ]

    @pytest.mark.asyncio
    async def test_update_progress_error_does_not_raise(self, processor, work):
        work.errors["progress"] = Exception("API down")
        await processor._update_progress(_job_item(), "downloading", 0, "")
        # Should not raise

    @pytest.mark.asyncio
    async def test_fail_job(self, processor, work):
        await processor._fail_job(_job_item(), "Something went wrong")
        assert work.called("fail")[0].args == ("Something went wrong",)

    @pytest.mark.asyncio
    async def test_fail_job_error_does_not_raise(self, processor, work):
        work.errors["fail"] = Exception("API down")
        await processor._fail_job(_job_item(), "error")
        # Should not raise


class TestResolveGroup:
    """Tests for _resolve_or_create_group."""

    @pytest.mark.asyncio
    async def test_resolve_relative_dir(self, processor, ttt_storage):
        import os

        group_dir = os.path.join(ttt_storage, "2025-01-01_game")
        os.makedirs(group_dir, exist_ok=True)

        result = await processor._resolve_or_create_group(
            {"id": "job-1"},
            {"recording_group_dir": "2025-01-01_game"},
        )
        assert result == group_dir

    @pytest.mark.asyncio
    async def test_resolve_absolute_dir(self, processor, ttt_storage):
        import os

        group_dir = os.path.join(ttt_storage, "abs_game")
        os.makedirs(group_dir, exist_ok=True)

        result = await processor._resolve_or_create_group(
            {"id": "job-1"},
            {"recording_group_dir": group_dir},
        )
        assert result == group_dir

    @pytest.mark.asyncio
    async def test_resolve_missing_dir_returns_none(self, processor):
        result = await processor._resolve_or_create_group(
            {"id": "job-1"},
            {"recording_group_dir": "nonexistent"},
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_resolve_no_recording_dir_key(self, processor):
        result = await processor._resolve_or_create_group({"id": "job-1"}, {})
        assert result is None


# start/stop is now QueueProcessor's, with its own coverage in
# test_base_queue_processor.py. The heartbeat is owned by TTTPoller —
# its coverage lives in test_ttt_poller.py.
