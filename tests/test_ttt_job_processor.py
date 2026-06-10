"""Tests for the TTTJobProcessor."""

import tempfile
from unittest.mock import Mock

import pytest

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
def mock_ttt_client():
    """Create a mock TTT API client."""
    client = Mock()
    client.is_authenticated.return_value = True
    client.register_service.return_value = {"id": "service-123"}
    client.send_heartbeat.return_value = None
    client.get_pending_jobs.return_value = []
    client.claim_job.return_value = None
    client.update_job_progress.return_value = None
    client.complete_job.return_value = None
    client.fail_job.return_value = None
    return client


@pytest.fixture
def processor(ttt_storage, ttt_config, mock_ttt_client):
    """Create a TTTJobProcessor instance. Back-compat shim attaches the
    TTT mock as ``handler.ttt_client`` so existing tests can keep
    reading it; the live TTTPoller passes it via :py:meth:`poll`."""
    h = TTTJobProcessor(
        storage_path=ttt_storage,
        config=ttt_config,
    )
    h.ttt_client = mock_ttt_client
    return h


class TestTTTJobProcessorInit:
    """Tests for TTTJobProcessor initialization."""

    def test_init_stores_config(self, processor, ttt_config):
        assert processor.config is ttt_config

    def test_init_stores_ttt_client(self, processor, mock_ttt_client):
        assert processor.ttt_client is mock_ttt_client

    def test_init_no_service_id(self, processor):
        # Service registration moved to TTTPoller; the processor itself
        # doesn't track service id anymore. Sanity check the attr still
        # exists on the (rare) path that sets it.
        assert processor._service_id is None

    def test_init_stores_optional_processors(
        self, ttt_storage, ttt_config, mock_ttt_client
    ):
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
        proc.ttt_client = mock_ttt_client
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
    from video_grouper.task_processors.tasks.ttt.ttt_job_task import TTTJobTask

    await processor.add_work(TTTJobTask(ttt_id="job-1", payload={"id": "job-1"}))
    await processor.add_work(TTTJobTask(ttt_id="job-1", payload={"id": "job-1"}))
    await processor.add_work(TTTJobTask(ttt_id="job-2", payload={"id": "job-2"}))
    assert processor._queue.qsize() == 2


class TestProcessJob:
    """Tests for _process_job."""

    @pytest.mark.asyncio
    async def test_process_job_claim_failure(self, processor, mock_ttt_client):
        mock_ttt_client.claim_job.side_effect = Exception("Already claimed")
        await processor._process_job(
            processor.ttt_client, {"id": "job-1", "config": {}}
        )
        mock_ttt_client.update_job_progress.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_job_no_group_dir(self, processor, mock_ttt_client):
        job = {"id": "job-1", "config": {}}
        await processor._process_job(processor.ttt_client, job)
        mock_ttt_client.fail_job.assert_called_once_with(
            "job-1", "Could not resolve recording group directory"
        )


# NOTE: ``_processing_jobs`` set is gone — QueueProcessor's
# ``_queued_items`` dedup handles it now. See test_add_work_dedup below.


class TestProgressAndFailure:
    """Tests for progress reporting and failure handling."""

    @pytest.mark.asyncio
    async def test_update_progress(self, processor, mock_ttt_client):
        await processor._update_progress(
            processor.ttt_client, "job-1", "downloading", {"percent": 50}
        )
        mock_ttt_client.update_job_progress.assert_called_once_with(
            "job-1", "downloading", {"percent": 50}
        )

    @pytest.mark.asyncio
    async def test_update_progress_error_does_not_raise(
        self, processor, mock_ttt_client
    ):
        mock_ttt_client.update_job_progress.side_effect = Exception("API down")
        await processor._update_progress(
            processor.ttt_client, "job-1", "downloading", {}
        )
        # Should not raise

    @pytest.mark.asyncio
    async def test_fail_job(self, processor, mock_ttt_client):
        await processor._fail_job(processor.ttt_client, "job-1", "Something went wrong")
        mock_ttt_client.fail_job.assert_called_once_with(
            "job-1", "Something went wrong"
        )

    @pytest.mark.asyncio
    async def test_fail_job_error_does_not_raise(self, processor, mock_ttt_client):
        mock_ttt_client.fail_job.side_effect = Exception("API down")
        await processor._fail_job(processor.ttt_client, "job-1", "error")
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
