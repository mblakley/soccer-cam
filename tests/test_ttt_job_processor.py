"""Tests for the TTTJobProcessor."""

import asyncio
import tempfile
from unittest.mock import Mock, AsyncMock, patch

import pytest

from video_grouper.task_processors.ttt_job_processor import TTTJobProcessor
from video_grouper.utils.config import (
    Config,
    CameraConfig,
    TeamSnapConfig,
    PlayMetricsConfig,
    NtfyConfig,
    YouTubeConfig,
    AutocamConfig,
    CloudSyncConfig,
    AppConfig,
    StorageConfig,
    RecordingConfig,
    ProcessingConfig,
    LoggingConfig,
    TTTConfig,
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
    """Create a TTTJobProcessor instance."""
    return TTTJobProcessor(
        storage_path=ttt_storage,
        config=ttt_config,
        ttt_client=mock_ttt_client,
        poll_interval=1,
    )


class TestTTTJobProcessorInit:
    """Tests for TTTJobProcessor initialization."""

    def test_init_stores_config(self, processor, ttt_config):
        assert processor.config is ttt_config

    def test_init_stores_ttt_client(self, processor, mock_ttt_client):
        assert processor.ttt_client is mock_ttt_client

    def test_init_empty_processing_jobs(self, processor):
        assert len(processor._processing_jobs) == 0

    def test_init_no_service_id(self, processor):
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
            ttt_client=mock_ttt_client,
            camera=camera,
            download_processor=download,
            video_processor=video,
            upload_processor=upload,
        )
        assert proc.camera is camera
        assert proc.download_processor is download
        assert proc.video_processor is video
        assert proc.upload_processor is upload


class TestServiceRegistration:
    """Tests for service registration."""

    @pytest.mark.asyncio
    async def test_register_service_success(self, processor, mock_ttt_client):
        await processor._register_service()
        mock_ttt_client.register_service.assert_called_once_with(
            "test-machine",
            {
                "ffmpeg": True,
                "autocam": False,
                "camera_type": "dahua",
                "camera_ip": "127.0.0.1",
            },
        )
        assert processor._service_id == "service-123"

    @pytest.mark.asyncio
    async def test_register_service_not_authenticated(self, processor, mock_ttt_client):
        mock_ttt_client.is_authenticated.return_value = False
        await processor._register_service()
        mock_ttt_client.register_service.assert_not_called()
        assert processor._service_id is None

    @pytest.mark.asyncio
    async def test_register_service_api_error(self, processor, mock_ttt_client):
        mock_ttt_client.register_service.side_effect = Exception("API error")
        await processor._register_service()
        assert processor._service_id is None

    @pytest.mark.asyncio
    async def test_register_service_uses_platform_node_fallback(
        self, ttt_storage, mock_ttt_client
    ):
        config = Config(
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
            ttt=TTTConfig(enabled=True, job_polling_enabled=True, machine_name=""),
        )
        proc = TTTJobProcessor(
            storage_path=ttt_storage,
            config=config,
            ttt_client=mock_ttt_client,
            poll_interval=1,
        )
        with patch(
            "video_grouper.task_processors.ttt_job_processor.platform"
        ) as mock_platform:
            mock_platform.node.return_value = "my-hostname"
            await proc._register_service()
        call_args = mock_ttt_client.register_service.call_args
        assert call_args[0][0] == "my-hostname"


class TestHeartbeat:
    """Tests for heartbeat loop."""

    @pytest.mark.asyncio
    async def test_heartbeat_sends_when_service_registered(
        self, processor, mock_ttt_client
    ):
        processor._service_id = "service-123"
        task = asyncio.create_task(processor._heartbeat_loop())
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_heartbeat_skips_when_no_service_id(self, processor, mock_ttt_client):
        processor._service_id = None
        task = asyncio.create_task(processor._heartbeat_loop())
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        mock_ttt_client.send_heartbeat.assert_not_called()


class TestDiscoverWork:
    """Tests for discover_work polling."""

    @pytest.mark.asyncio
    async def test_discover_work_not_authenticated(self, processor, mock_ttt_client):
        mock_ttt_client.is_authenticated.return_value = False
        await processor.discover_work()
        mock_ttt_client.get_pending_jobs.assert_not_called()

    @pytest.mark.asyncio
    async def test_discover_work_no_jobs(self, processor, mock_ttt_client):
        mock_ttt_client.get_pending_jobs.return_value = []
        await processor.discover_work()
        mock_ttt_client.get_pending_jobs.assert_called_once()

    @pytest.mark.asyncio
    async def test_discover_work_api_error(self, processor, mock_ttt_client):
        mock_ttt_client.get_pending_jobs.side_effect = Exception("Network error")
        await processor.discover_work()
        # Should not raise

    @pytest.mark.asyncio
    async def test_discover_work_spawns_task_for_new_job(
        self, processor, mock_ttt_client
    ):
        mock_ttt_client.get_pending_jobs.return_value = [{"id": "job-1", "config": {}}]
        with patch.object(processor, "_process_job", new_callable=AsyncMock):
            await processor.discover_work()
            assert "job-1" in processor._processing_jobs

    @pytest.mark.asyncio
    async def test_discover_work_deduplication(self, processor, mock_ttt_client):
        processor._processing_jobs.add("job-1")
        mock_ttt_client.get_pending_jobs.return_value = [{"id": "job-1", "config": {}}]
        with patch.object(
            processor, "_process_job", new_callable=AsyncMock
        ) as mock_process:
            await processor.discover_work()
            mock_process.assert_not_called()

    @pytest.mark.asyncio
    async def test_discover_work_skips_jobs_without_id(
        self, processor, mock_ttt_client
    ):
        mock_ttt_client.get_pending_jobs.return_value = [{"config": {}}]
        with patch.object(
            processor, "_process_job", new_callable=AsyncMock
        ) as mock_process:
            await processor.discover_work()
            mock_process.assert_not_called()


class TestProcessJob:
    """Tests for _process_job."""

    @pytest.mark.asyncio
    async def test_process_job_claim_failure(self, processor, mock_ttt_client):
        mock_ttt_client.claim_job.side_effect = Exception("Already claimed")
        await processor._process_job({"id": "job-1", "config": {}})
        mock_ttt_client.update_job_progress.assert_not_called()
        assert "job-1" not in processor._processing_jobs

    @pytest.mark.asyncio
    async def test_process_job_no_group_dir(self, processor, mock_ttt_client):
        job = {"id": "job-1", "config": {}}
        await processor._process_job(job)
        mock_ttt_client.fail_job.assert_called_once_with(
            "job-1", "Could not resolve recording group directory"
        )
        assert "job-1" not in processor._processing_jobs

    @pytest.mark.asyncio
    async def test_process_job_discards_from_set_on_completion(
        self, processor, mock_ttt_client, ttt_storage
    ):
        import os

        group_dir = os.path.join(ttt_storage, "2025-01-01_game")
        os.makedirs(group_dir, exist_ok=True)
        combined = os.path.join(group_dir, "combined.mp4")
        with open(combined, "w") as f:
            f.write("fake")

        job = {
            "id": "job-2",
            "config": {"recording_group_dir": "2025-01-01_game"},
        }

        processor._processing_jobs.add("job-2")

        with (
            patch(
                "video_grouper.task_processors.ttt_job_processor.os.path.isdir",
                return_value=True,
            ),
            patch(
                "video_grouper.task_processors.ttt_job_processor.os.path.isfile",
                return_value=True,
            ),
            patch(
                "video_grouper.utils.paths.get_combined_video_path",
                return_value=combined,
            ),
        ):
            await processor._process_job(job)

        assert "job-2" not in processor._processing_jobs

    @pytest.mark.asyncio
    async def test_process_job_discards_from_set_on_error(
        self, processor, mock_ttt_client
    ):
        mock_ttt_client.claim_job.side_effect = Exception("Boom")
        processor._processing_jobs.add("job-err")
        await processor._process_job({"id": "job-err", "config": {}})
        assert "job-err" not in processor._processing_jobs


class TestProgressAndFailure:
    """Tests for progress reporting and failure handling."""

    @pytest.mark.asyncio
    async def test_update_progress(self, processor, mock_ttt_client):
        await processor._update_progress("job-1", "downloading", {"percent": 50})
        mock_ttt_client.update_job_progress.assert_called_once_with(
            "job-1", "downloading", {"percent": 50}
        )

    @pytest.mark.asyncio
    async def test_update_progress_error_does_not_raise(
        self, processor, mock_ttt_client
    ):
        mock_ttt_client.update_job_progress.side_effect = Exception("API down")
        await processor._update_progress("job-1", "downloading", {})
        # Should not raise

    @pytest.mark.asyncio
    async def test_fail_job(self, processor, mock_ttt_client):
        await processor._fail_job("job-1", "Something went wrong")
        mock_ttt_client.fail_job.assert_called_once_with(
            "job-1", "Something went wrong"
        )

    @pytest.mark.asyncio
    async def test_fail_job_error_does_not_raise(self, processor, mock_ttt_client):
        mock_ttt_client.fail_job.side_effect = Exception("API down")
        await processor._fail_job("job-1", "error")
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


class TestStartStop:
    """Tests for start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_registers_service_and_starts_heartbeat(
        self, processor, mock_ttt_client
    ):
        with patch.object(
            processor, "_register_service", new_callable=AsyncMock
        ) as mock_reg:
            await processor.start()
            mock_reg.assert_called_once()
            assert processor._heartbeat_task is not None
            assert processor._processor_task is not None
            await processor.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_heartbeat(self, processor):
        with patch.object(processor, "_register_service", new_callable=AsyncMock):
            await processor.start()
            assert processor._heartbeat_task is not None
            await processor.stop()
            assert (
                processor._heartbeat_task.cancelled()
                or processor._heartbeat_task.done()
            )
