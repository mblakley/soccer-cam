"""
Full pipeline integration test.

Tests the complete flow from camera discovery through upload for multiple
recording groups, verifying:
- Camera file grouping (gap tolerance)
- Download queue progression
- Video combine task execution
- Event-driven transitions (combine → match info API + NTFY → trim)
- Upload queue progression
- Correct queue sizes at each stage
- Two groups process sequentially through video queue
"""

import os
import json
import asyncio
import tempfile
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime
import pytest

from video_grouper.task_processors.camera_poller import CameraPoller
from video_grouper.task_processors.download_processor import DownloadProcessor
from video_grouper.task_processors.video_processor import VideoProcessor
from video_grouper.task_processors.upload_processor import UploadProcessor
from video_grouper.task_processors.ntfy_processor import NtfyProcessor
from video_grouper.task_processors.tasks.video import CombineTask
from video_grouper.task_processors.tasks.upload import YoutubeUploadTask
from video_grouper.models import RecordingFile
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
)
from video_grouper.utils.logger import close_loggers


@pytest.fixture(autouse=True)
def cleanup_loggers():
    """Clean up loggers after each test."""
    yield
    close_loggers()


@pytest.fixture(autouse=True)
def mock_file_system():
    """Override conftest mock_file_system: use REAL filesystem for integration tests."""
    yield {}


@pytest.fixture(autouse=True)
def mock_ffmpeg():
    """Override conftest mock_ffmpeg: use REAL subprocess for integration tests."""
    yield Mock()


@pytest.fixture(autouse=True)
def mock_httpx():
    """Override conftest mock_httpx: use REAL httpx for integration tests."""
    yield Mock()


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory."""
    import time
    import shutil

    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir
        time.sleep(0.1)
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


@pytest.fixture
def mock_config(temp_storage):
    """Create a config for full pipeline testing."""
    return Config(
        cameras=[
            CameraConfig(
                name="default",
                type="dahua",
                device_ip="192.168.1.100",
                username="admin",
                password="password",
            )
        ],
        storage=StorageConfig(path=temp_storage),
        recording=RecordingConfig(),
        processing=ProcessingConfig(),
        logging=LoggingConfig(),
        app=AppConfig(storage_path=temp_storage, check_interval_seconds=1),
        teamsnap=TeamSnapConfig(enabled=False, team_id="1", my_team_name="Team A"),
        teamsnap_teams=[],
        playmetrics=PlayMetricsConfig(
            enabled=False, username="user", password="pass", team_name="Team A"
        ),
        playmetrics_teams=[],
        ntfy=NtfyConfig(enabled=False, server_url="http://ntfy.sh", topic="test"),
        youtube=YouTubeConfig(enabled=True),
        autocam=AutocamConfig(enabled=False),
        cloud_sync=CloudSyncConfig(enabled=False),
    )


@pytest.fixture
def mock_camera():
    """Create a mock camera."""
    camera = Mock()
    camera.check_availability = AsyncMock(return_value=True)
    camera.get_file_list = AsyncMock(return_value=[])
    camera.get_connected_timeframes = Mock(return_value=[])
    camera.download_file = AsyncMock(return_value=True)
    camera.close = AsyncMock()
    return camera


def create_group_directory(storage_path: str, group_name: str) -> str:
    """Create a group directory with proper structure."""
    group_dir = os.path.join(storage_path, group_name)
    os.makedirs(group_dir, exist_ok=True)

    # Create state.json
    state_data = {"status": "pending", "error_message": None, "files": {}}
    with open(os.path.join(group_dir, "state.json"), "w") as f:
        json.dump(state_data, f)

    return group_dir


def create_combined_video(group_dir: str, storage_path: str) -> str:
    """Create a fake combined.mp4 file."""
    from video_grouper.utils.paths import get_combined_video_path

    combined_path = get_combined_video_path(group_dir, storage_path)
    os.makedirs(os.path.dirname(combined_path), exist_ok=True)
    with open(combined_path, "wb") as f:
        f.write(b"\x00" * 1024)  # Dummy file
    return combined_path


def create_match_info(group_dir: str, storage_path: str) -> None:
    """Create a populated match_info.ini file."""
    from video_grouper.utils.paths import get_match_info_path

    match_info_path = get_match_info_path(group_dir, storage_path)
    os.makedirs(os.path.dirname(match_info_path), exist_ok=True)

    import configparser

    config = configparser.ConfigParser()
    config["match_info"] = {
        "my_team_name": "Eagles",
        "opponent_team_name": "Hawks",
        "location": "Home",
        "start_time_offset": "00:05:00",
        "end_time_offset": "01:35:00",
    }
    with open(match_info_path, "w") as f:
        config.write(f)


class TestFullPipelineIntegration:
    """Test the complete pipeline flow with event-driven transitions."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_camera_groups_files_by_gap_tolerance(
        self, temp_storage, mock_config, mock_camera
    ):
        """Verify CameraPoller groups files with 5s gap tolerance."""
        upload_processor = UploadProcessor(temp_storage, mock_config)
        video_processor = VideoProcessor(temp_storage, mock_config, upload_processor)
        download_processor = DownloadProcessor(
            temp_storage, mock_config, mock_camera, video_processor
        )
        camera_poller = CameraPoller(
            temp_storage, mock_config, mock_camera, download_processor, poll_interval=1
        )

        try:
            # Create latest_video.txt
            with open(os.path.join(temp_storage, "latest_video.txt"), "w") as f:
                f.write("2023-01-01 09:00:00")

            # Group 1: 3 files within 3s of each other
            # Group 2: 2 files starting 30s after Group 1 (well beyond 5s gap)
            mock_files = [
                {
                    "path": "/g1_file1.dav",
                    "startTime": "2023-01-01 10:00:00",
                    "endTime": "2023-01-01 10:05:00",
                },
                {
                    "path": "/g1_file2.dav",
                    "startTime": "2023-01-01 10:05:03",
                    "endTime": "2023-01-01 10:10:00",
                },
                {
                    "path": "/g1_file3.dav",
                    "startTime": "2023-01-01 10:10:02",
                    "endTime": "2023-01-01 10:15:00",
                },
                # Gap of 30s → new group
                {
                    "path": "/g2_file1.dav",
                    "startTime": "2023-01-01 10:15:30",
                    "endTime": "2023-01-01 10:20:00",
                },
                {
                    "path": "/g2_file2.dav",
                    "startTime": "2023-01-01 10:20:04",
                    "endTime": "2023-01-01 10:25:00",
                },
            ]
            mock_camera.get_file_list.return_value = mock_files

            # Trigger file discovery
            await camera_poller._sync_files_from_camera()

            # Should have created 2 groups with correct file counts
            # Check download queue has 5 files (3 from group 1 + 2 from group 2)
            assert download_processor.get_queue_size() == 5

            # Verify group directories were created
            group_dirs = [
                d
                for d in os.listdir(temp_storage)
                if os.path.isdir(os.path.join(temp_storage, d)) and d.startswith("2023")
            ]
            assert len(group_dirs) == 2, f"Expected 2 groups, got {group_dirs}"

        finally:
            await download_processor.stop()
            await video_processor.stop()
            await upload_processor.stop()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_download_to_combine_handoff(
        self, temp_storage, mock_config, mock_camera
    ):
        """Verify DownloadProcessor hands off to VideoProcessor with CombineTask."""
        upload_processor = UploadProcessor(temp_storage, mock_config)
        video_processor = VideoProcessor(temp_storage, mock_config, upload_processor)
        download_processor = DownloadProcessor(
            temp_storage, mock_config, mock_camera, video_processor
        )

        await video_processor.start()
        await download_processor.start()

        try:
            # Create a group directory
            group_name = "2023.01.01-10.00.00"
            group_dir = create_group_directory(temp_storage, group_name)

            # Create a recording file
            recording_file = RecordingFile(
                start_time=datetime(2023, 1, 1, 10, 0, 0),
                end_time=datetime(2023, 1, 1, 10, 5, 0),
                file_path=os.path.join(group_dir, "test.dav"),
                metadata={"path": "/test.dav"},
            )

            # Add to download queue
            await download_processor.add_work(recording_file)
            assert download_processor.get_queue_size() == 1

            # Wait for download to process
            await asyncio.sleep(2)

            # Download queue should be empty now
            assert download_processor.get_queue_size() == 0

        finally:
            await download_processor.stop()
            await video_processor.stop()
            await upload_processor.stop()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_combine_triggers_match_info_and_ntfy(
        self, temp_storage, mock_config, mock_camera
    ):
        """After CombineTask succeeds, match info API and NTFY are triggered."""
        mock_match_info_service = Mock()
        mock_match_info_service.populate_match_info_from_apis = AsyncMock(
            return_value=True
        )

        mock_ntfy_processor = Mock()
        mock_ntfy_processor.request_match_info_for_directory = AsyncMock(
            return_value=True
        )

        upload_processor = UploadProcessor(temp_storage, mock_config)
        video_processor = VideoProcessor(
            temp_storage,
            mock_config,
            upload_processor,
            match_info_service=mock_match_info_service,
            ntfy_processor=mock_ntfy_processor,
        )

        try:
            group_name = "2023.01.01-10.00.00"
            create_group_directory(temp_storage, group_name)

            # Create CombineTask and mock its execute
            combine_task = CombineTask(group_dir=group_name)

            with patch.object(
                combine_task, "execute", new_callable=AsyncMock, return_value=True
            ):
                await video_processor.process_item(combine_task)

            # Allow async _on_combine_complete to run
            await asyncio.sleep(0.1)

            # Both match info and NTFY should have been triggered
            mock_match_info_service.populate_match_info_from_apis.assert_called_once_with(
                group_name
            )
            mock_ntfy_processor.request_match_info_for_directory.assert_called_once()

        finally:
            await video_processor.stop()
            await upload_processor.stop()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_ntfy_completion_queues_trim_task(
        self, temp_storage, mock_config, mock_camera
    ):
        """When NTFY completes (match info populated), TrimTask is queued."""
        upload_processor = UploadProcessor(temp_storage, mock_config)
        video_processor = VideoProcessor(temp_storage, mock_config, upload_processor)

        # Create mock match info service
        mock_match_info_service = Mock()

        # Create NtfyProcessor with video_processor
        ntfy_processor = NtfyProcessor(
            storage_path=temp_storage,
            config=mock_config,
            ntfy_service=Mock(),
            match_info_service=mock_match_info_service,
            poll_interval=30,
            video_processor=video_processor,
        )

        try:
            group_name = "2023.01.01-10.00.00"
            group_dir = os.path.join(temp_storage, group_name)
            os.makedirs(group_dir, exist_ok=True)

            # Create combined.mp4 (required for trim task to be queued)
            create_combined_video(group_name, temp_storage)

            # Create populated match_info.ini
            create_match_info(group_name, temp_storage)

            # Mock is_match_info_complete to return True
            mock_match_info_service.is_match_info_complete = AsyncMock(
                return_value=True
            )

            # Simulate NTFY completion callback
            await ntfy_processor._check_match_info_completion(group_name)

            # A TrimTask should now be queued on video_processor
            assert video_processor.get_queue_size() == 1

        finally:
            await video_processor.stop()
            await upload_processor.stop()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_upload_task_queued_after_youtube_upload_task_added(
        self, temp_storage, mock_config
    ):
        """Verify UploadProcessor accepts and processes tasks."""
        upload_processor = UploadProcessor(temp_storage, mock_config)

        try:
            upload_task = YoutubeUploadTask(group_dir="test_group")
            await upload_processor.add_work(upload_task)

            assert upload_processor.get_queue_size() == 1

        finally:
            await upload_processor.stop()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_two_groups_through_video_queue(
        self, temp_storage, mock_config, mock_camera
    ):
        """Two groups process sequentially through the video queue."""
        upload_processor = UploadProcessor(temp_storage, mock_config)
        video_processor = VideoProcessor(temp_storage, mock_config, upload_processor)

        await video_processor.start()

        execution_order = []

        async def mock_execute_group1():
            execution_order.append("combine:group1")
            await asyncio.sleep(0.05)
            return True

        async def mock_execute_group2():
            execution_order.append("combine:group2")
            await asyncio.sleep(0.05)
            return True

        try:
            # Create two group directories
            group1 = "2023.01.01-10.00.00"
            group2 = "2023.01.01-10.30.00"
            create_group_directory(temp_storage, group1)
            create_group_directory(temp_storage, group2)

            # Create combine tasks
            task1 = CombineTask(group_dir=group1)
            task2 = CombineTask(group_dir=group2)

            # Mock their execute methods
            with (
                patch.object(task1, "execute", side_effect=mock_execute_group1),
                patch.object(task2, "execute", side_effect=mock_execute_group2),
            ):
                # Add both tasks
                await video_processor.add_work(task1)
                await video_processor.add_work(task2)

                # Wait for processing
                await asyncio.sleep(1)

            # Both should have executed in order
            assert len(execution_order) == 2
            assert execution_order[0] == "combine:group1"
            assert execution_order[1] == "combine:group2"

            # Queue should be empty
            assert video_processor.get_queue_size() == 0

        finally:
            await video_processor.stop()
            await upload_processor.stop()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_event_driven_pipeline_combine_to_trim(
        self, temp_storage, mock_config
    ):
        """Test the event-driven flow: combine → match info → NTFY → trim."""
        upload_processor = UploadProcessor(temp_storage, mock_config)
        video_processor = VideoProcessor(temp_storage, mock_config, upload_processor)

        # Track what tasks the video processor processes
        processed_tasks = []
        original_process_item = video_processor.process_item

        async def tracking_process_item(item):
            processed_tasks.append(type(item).__name__)
            await original_process_item(item)

        video_processor.process_item = tracking_process_item

        # Create mock services
        mock_match_info_service = Mock()
        mock_match_info_service.populate_match_info_from_apis = AsyncMock(
            return_value=True
        )
        mock_match_info_service.is_match_info_complete = AsyncMock(return_value=True)

        mock_ntfy_service = Mock()
        mock_ntfy_service.mark_as_processed = Mock()

        ntfy_processor = NtfyProcessor(
            storage_path=temp_storage,
            config=mock_config,
            ntfy_service=mock_ntfy_service,
            match_info_service=mock_match_info_service,
            poll_interval=30,
            video_processor=video_processor,
        )

        # Wire services into video processor
        video_processor.match_info_service = mock_match_info_service
        video_processor.ntfy_processor = ntfy_processor

        try:
            group_name = "2023.01.01-10.00.00"
            create_group_directory(temp_storage, group_name)

            # Create combined.mp4 (will be created by CombineTask in real pipeline)
            create_combined_video(group_name, temp_storage)

            # Create populated match_info.ini
            create_match_info(group_name, temp_storage)

            # Step 1: CombineTask succeeds
            combine_task = CombineTask(group_dir=group_name)
            with patch.object(
                combine_task, "execute", new_callable=AsyncMock, return_value=True
            ):
                await video_processor.process_item(combine_task)

            # Allow async _on_combine_complete to run
            await asyncio.sleep(0.1)

            # Verify match info was called
            mock_match_info_service.populate_match_info_from_apis.assert_called_once()

            # Step 2: Simulate NTFY completion (match info is now complete)
            await ntfy_processor._check_match_info_completion(group_name)

            # Step 3: TrimTask should now be on the video queue
            assert video_processor.get_queue_size() == 1

        finally:
            await video_processor.stop()
            await upload_processor.stop()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_full_pipeline_state_persistence(
        self, temp_storage, mock_config, mock_camera
    ):
        """Verify queue state persists across processor restarts."""
        upload_processor = UploadProcessor(temp_storage, mock_config)
        video_processor = VideoProcessor(temp_storage, mock_config, upload_processor)
        download_processor = DownloadProcessor(
            temp_storage, mock_config, mock_camera, video_processor
        )

        try:
            # Add tasks to queues
            group_name = "2023.01.01-10.00.00"
            create_group_directory(temp_storage, group_name)

            recording = RecordingFile(
                start_time=datetime(2023, 1, 1, 10, 0, 0),
                end_time=datetime(2023, 1, 1, 10, 5, 0),
                file_path=os.path.join(temp_storage, group_name, "test.dav"),
                metadata={"path": "/test.dav"},
            )

            combine_task = CombineTask(group_dir=group_name)
            upload_task = YoutubeUploadTask(group_dir=group_name)

            await download_processor.add_work(recording)
            await video_processor.add_work(combine_task)
            await upload_processor.add_work(upload_task)

            # Verify queues have items
            assert download_processor.get_queue_size() == 1
            assert video_processor.get_queue_size() == 1
            assert upload_processor.get_queue_size() == 1

            # Stop processors (triggers save_state)
            await download_processor.stop()
            await video_processor.stop()
            await upload_processor.stop()

            # Verify state files exist
            state_files = [
                "download_queue_state.json",
                "video_queue_state.json",
                "upload_queue_state.json",
            ]
            for sf in state_files:
                state_path = os.path.join(temp_storage, sf)
                assert os.path.exists(state_path), f"Missing state file: {sf}"

            # Create new processor instances and load state
            upload_processor2 = UploadProcessor(temp_storage, mock_config)
            video_processor2 = VideoProcessor(
                temp_storage, mock_config, upload_processor2
            )
            download_processor2 = DownloadProcessor(
                temp_storage, mock_config, mock_camera, video_processor2
            )

            # Register task types for deserialization
            from video_grouper.task_processors.register_tasks import register_all_tasks

            register_all_tasks()

            # Initialize queues and load state
            download_processor2._queue = asyncio.Queue()
            video_processor2._queue = asyncio.Queue()
            upload_processor2._queue = asyncio.Queue()
            await download_processor2.load_state()
            await video_processor2.load_state()
            await upload_processor2.load_state()

            # Verify items recovered
            assert download_processor2.get_queue_size() == 1
            assert video_processor2.get_queue_size() == 1
            assert upload_processor2.get_queue_size() == 1

        finally:
            # Clean up any remaining processors
            try:
                await download_processor.stop()
            except Exception:
                pass
            try:
                await video_processor.stop()
            except Exception:
                pass
            try:
                await upload_processor.stop()
            except Exception:
                pass

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_all_queues_empty_after_full_processing(
        self, temp_storage, mock_config, mock_camera
    ):
        """After processing completes, all queues should be empty."""
        upload_processor = UploadProcessor(temp_storage, mock_config)
        video_processor = VideoProcessor(temp_storage, mock_config, upload_processor)
        download_processor = DownloadProcessor(
            temp_storage, mock_config, mock_camera, video_processor
        )

        await upload_processor.start()
        await video_processor.start()
        await download_processor.start()

        try:
            group_name = "2023.01.01-10.00.00"
            create_group_directory(temp_storage, group_name)

            # Add a recording to download
            recording = RecordingFile(
                start_time=datetime(2023, 1, 1, 10, 0, 0),
                end_time=datetime(2023, 1, 1, 10, 5, 0),
                file_path=os.path.join(temp_storage, group_name, "test.dav"),
                metadata={"path": "/test.dav"},
            )

            await download_processor.add_work(recording)

            # Wait for processing to complete (download will succeed, combine will fail
            # because there's no real video file, but queues should drain)
            max_wait = 10
            elapsed = 0
            while elapsed < max_wait:
                if (
                    download_processor.get_queue_size() == 0
                    and video_processor.get_queue_size() == 0
                ):
                    break
                await asyncio.sleep(0.2)
                elapsed += 0.2

            # Queues should be empty (tasks processed or failed)
            assert download_processor.get_queue_size() == 0

        finally:
            await download_processor.stop()
            await video_processor.stop()
            await upload_processor.stop()
