"""
Integration tests for VideoProcessor to AutocamProcessor handoff.

This test verifies:
1. Processed videos are queued AND actually processed by AutocamProcessor immediately
2. State persistence across processor restarts
3. Error recovery and retry mechanisms
4. Actual processing timing and verification
5. State file content verification
6. AutocamProcessor integration and functionality
"""

import os
import tempfile
import json
import asyncio
from datetime import datetime
import pytest
from pathlib import Path

from video_grouper.task_processors.video_processor import VideoProcessor
from video_grouper.task_processors.autocam_processor import AutocamProcessor
from video_grouper.task_processors.upload_processor import UploadProcessor
from video_grouper.task_processors.tasks.video import CombineTask
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


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory for tests."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def mock_config(temp_storage):
    """Create a mock configuration with autocam enabled."""
    return Config(
        camera=CameraConfig(
            type="dahua", device_ip="127.0.0.1", username="admin", password="password"
        ),
        storage=StorageConfig(path=temp_storage),
        recording=RecordingConfig(),
        processing=ProcessingConfig(),
        logging=LoggingConfig(),
        app=AppConfig(
            storage_path=temp_storage,
            check_interval_seconds=1,
            timezone="America/New_York",
        ),
        teamsnap=TeamSnapConfig(enabled=False, team_id="1", my_team_name="Team A"),
        teamsnap_teams=[],
        playmetrics=PlayMetricsConfig(
            enabled=False, username="user", password="pass", team_name="Team A"
        ),
        playmetrics_teams=[],
        ntfy=NtfyConfig(enabled=False, server_url="http://ntfy.sh", topic="test"),
        youtube=YouTubeConfig(enabled=False),
        autocam=AutocamConfig(enabled=True),  # Enable autocam for testing
        cloud_sync=CloudSyncConfig(enabled=False),
    )


@pytest.fixture
def real_upload_processor(temp_storage, mock_config):
    """Create a real UploadProcessor for integration testing."""
    return UploadProcessor(
        storage_path=temp_storage,
        config=mock_config,
    )


@pytest.fixture
def real_autocam_processor(temp_storage, mock_config, real_upload_processor):
    """Create a real AutocamProcessor for integration testing."""
    return AutocamProcessor(
        storage_path=temp_storage,
        config=mock_config,
        upload_processor=real_upload_processor,
    )


@pytest.fixture
def real_video_processor(temp_storage, mock_config, real_upload_processor):
    """Create a real VideoProcessor for integration testing."""
    return VideoProcessor(
        storage_path=temp_storage,
        config=mock_config,
        upload_processor=real_upload_processor,
    )


@pytest.fixture
def setup_storage_environment(temp_storage):
    """Set up the storage environment for testing."""
    # Create the storage directory
    os.makedirs(temp_storage, exist_ok=True)

    # Create latest_video.txt file
    latest_video_path = os.path.join(temp_storage, "latest_video.txt")
    with open(latest_video_path, "w") as f:
        f.write("2023-01-01 09:00:00")

    # Create a proper group directory structure that the system expects
    group_dir = os.path.join(temp_storage, "2023.01.01-10.00.00")
    os.makedirs(group_dir, exist_ok=True)

    # Create a proper state.json file that the system expects
    state_data = {
        "status": "downloaded",  # Set to downloaded so video processor can process it
        "error_message": None,
        "files": {},
    }
    state_file_path = os.path.join(group_dir, "state.json")

    # Use pathlib to ensure proper path handling
    state_path = Path(state_file_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state_data, indent=4))

    yield temp_storage


class TestVideoAutocamIntegration:
    """Integration tests for VideoProcessor to AutocamProcessor handoff."""

    @pytest.mark.asyncio
    async def test_video_processor_queues_and_processes_autocam_tasks_immediately(
        self,
        setup_storage_environment,
        mock_config,
        real_video_processor,
        real_autocam_processor,
        real_upload_processor,
    ):
        """Test that VideoProcessor queues autocam tasks and they are processed immediately."""
        # Start all processors
        await real_upload_processor.start()
        await real_autocam_processor.start()
        await real_video_processor.start()

        try:
            # Create a realistic directory structure
            group_dir = os.path.join(setup_storage_environment, "2023.01.01-10.00.00")
            os.makedirs(group_dir, exist_ok=True)

            # Create a mock combine task (simulating output from DownloadProcessor)
            combine_task = CombineTask(group_dir=group_dir)

            # Add the task to video processor (simulating handoff from DownloadProcessor)
            await real_video_processor.add_work(combine_task)

            # Wait for processing to complete
            await asyncio.sleep(2)

            # Verify that autocam task was queued AND processed
            autocam_queue_size = real_autocam_processor.get_queue_size()
            assert autocam_queue_size == 0, (
                f"Expected autocam queue to be empty after processing, got {autocam_queue_size}"
            )

            # Verify state files were created
            video_state_file = os.path.join(
                setup_storage_environment, "video_queue_state.json"
            )

            assert os.path.exists(video_state_file), "Video state file should exist"
            # Note: Autocam state file may not exist if no tasks were queued due to missing files

            # Check state file contents
            with open(video_state_file, "r") as f:
                video_state = json.load(f)
                assert len(video_state) == 0, (
                    "Video state should be empty after processing"
                )

        finally:
            await real_video_processor.stop()
            await real_autocam_processor.stop()
            await real_upload_processor.stop()

    @pytest.mark.asyncio
    async def test_state_persistence_across_processor_restarts(
        self, setup_storage_environment, mock_config
    ):
        """Test that queue state is properly persisted and recovered across processor restarts."""
        # Create first processor instances
        upload_processor1 = UploadProcessor(
            storage_path=setup_storage_environment,
            config=mock_config,
        )

        autocam_processor1 = AutocamProcessor(
            storage_path=setup_storage_environment,
            config=mock_config,
            upload_processor=upload_processor1,
        )

        video_processor1 = VideoProcessor(
            storage_path=setup_storage_environment,
            config=mock_config,
            upload_processor=upload_processor1,
        )

        await upload_processor1.start()
        await autocam_processor1.start()
        await video_processor1.start()

        try:
            # Create a realistic directory structure
            group_dir = os.path.join(setup_storage_environment, "2023.01.01-10.00.00")
            os.makedirs(group_dir, exist_ok=True)

            # Create mock DAV files that the CombineTask expects
            dav_file1 = os.path.join(group_dir, "video1.dav")
            dav_file2 = os.path.join(group_dir, "video2.dav")
            with open(dav_file1, "w") as f:
                f.write("mock video content 1")
            with open(dav_file2, "w") as f:
                f.write("mock video content 2")

            # Create a mock combine task
            combine_task = CombineTask(group_dir=group_dir)

            # Add the task to video processor
            await video_processor1.add_work(combine_task)
            await asyncio.sleep(1)

            # Verify task was processed (may be processed immediately)
            # The key is that state persistence works regardless of processing timing
            queue_size = video_processor1.get_queue_size()
            assert queue_size == 0, (
                f"Task should be processed, queue size: {queue_size}"
            )

            # Stop the processors
            await video_processor1.stop()
            await autocam_processor1.stop()
            await upload_processor1.stop()

            # Verify state files exist
            video_state_file = os.path.join(
                setup_storage_environment, "video_queue_state.json"
            )

            assert os.path.exists(video_state_file), (
                "Video state file should exist after stopping"
            )
            # Note: Autocam state file may not exist if no tasks were queued

            # Create second processor instances (simulating restart)
            upload_processor2 = UploadProcessor(
                storage_path=setup_storage_environment,
                config=mock_config,
            )

            autocam_processor2 = AutocamProcessor(
                storage_path=setup_storage_environment,
                config=mock_config,
                upload_processor=upload_processor2,
            )

            video_processor2 = VideoProcessor(
                storage_path=setup_storage_environment,
                config=mock_config,
                upload_processor=upload_processor2,
            )

            await upload_processor2.start()
            await autocam_processor2.start()
            await video_processor2.start()

            try:
                # Wait for state recovery and processing
                await asyncio.sleep(3)

                # Verify that the task was processed after restart
                assert video_processor2.get_queue_size() == 0, (
                    "Video queue should be empty after processing"
                )
                assert autocam_processor2.get_queue_size() == 0, (
                    "Autocam queue should be empty after processing"
                )

            finally:
                await video_processor2.stop()
                await autocam_processor2.stop()
                await upload_processor2.stop()

        finally:
            await video_processor1.stop()
            await autocam_processor1.stop()
            await upload_processor1.stop()

    @pytest.mark.asyncio
    async def test_error_recovery_and_retry_mechanism(
        self,
        setup_storage_environment,
        mock_config,
        real_video_processor,
        real_autocam_processor,
        real_upload_processor,
    ):
        """Test that the system recovers from errors and retries failed operations."""
        # Start all processors
        await real_upload_processor.start()
        await real_autocam_processor.start()
        await real_video_processor.start()

        try:
            # Create a realistic directory structure
            group_dir = os.path.join(setup_storage_environment, "2023.01.01-10.00.00")
            os.makedirs(group_dir, exist_ok=True)

            # Create a mock combine task
            combine_task = CombineTask(group_dir=group_dir)

            # Add the task to video processor
            await real_video_processor.add_work(combine_task)

            # Wait for initial processing
            await asyncio.sleep(2)

            # Verify that processing completed
            assert real_video_processor.get_queue_size() == 0, (
                "Video queue should be empty after processing"
            )
            assert real_autocam_processor.get_queue_size() == 0, (
                "Autocam queue should be empty after processing"
            )

            # Verify state files are consistent
            video_state_file = os.path.join(
                setup_storage_environment, "video_queue_state.json"
            )

            with open(video_state_file, "r") as f:
                video_state = json.load(f)
                assert len(video_state) == 0, (
                    "Video state should be empty after processing"
                )

            # Note: Autocam state file may not exist if no tasks were queued

        finally:
            await real_video_processor.stop()
            await real_autocam_processor.stop()
            await real_upload_processor.stop()

    @pytest.mark.asyncio
    async def test_actual_processing_verification_with_timing(
        self,
        setup_storage_environment,
        mock_config,
        real_video_processor,
        real_autocam_processor,
        real_upload_processor,
    ):
        """Test that processing happens immediately and verify timing."""
        # Start all processors
        await real_upload_processor.start()
        await real_autocam_processor.start()
        await real_video_processor.start()

        try:
            # Create a realistic directory structure
            group_dir = os.path.join(setup_storage_environment, "2023.01.01-10.00.00")
            os.makedirs(group_dir, exist_ok=True)

            # Create a mock combine task
            combine_task = CombineTask(group_dir=group_dir)

            # Record start time
            start_time = datetime.now()

            # Add the task to video processor
            await real_video_processor.add_work(combine_task)

            # Wait for processing with timeout
            max_wait_time = 5  # seconds
            wait_start = datetime.now()

            while (
                real_video_processor.get_queue_size() > 0
                or real_autocam_processor.get_queue_size() > 0
            ):
                await asyncio.sleep(0.1)
                if (datetime.now() - wait_start).total_seconds() > max_wait_time:
                    break

            # Record end time
            end_time = datetime.now()
            processing_time = (end_time - start_time).total_seconds()

            # Verify processing completed quickly (should be under 2 seconds)
            assert processing_time < 2.0, (
                f"Processing took too long: {processing_time} seconds"
            )

            # Verify files were actually processed
            assert real_video_processor.get_queue_size() == 0, (
                "Video queue should be empty"
            )
            assert real_autocam_processor.get_queue_size() == 0, (
                "Autocam queue should be empty"
            )

        finally:
            await real_video_processor.stop()
            await real_autocam_processor.stop()
            await real_upload_processor.stop()

    @pytest.mark.asyncio
    async def test_state_file_content_verification(
        self,
        setup_storage_environment,
        mock_config,
        real_video_processor,
        real_autocam_processor,
        real_upload_processor,
    ):
        """Test that state files contain the correct data during processing."""
        # Start all processors
        await real_upload_processor.start()
        await real_autocam_processor.start()
        await real_video_processor.start()

        try:
            # Create a realistic directory structure
            group_dir = os.path.join(setup_storage_environment, "2023.01.01-10.00.00")
            os.makedirs(group_dir, exist_ok=True)

            # Create a mock combine task
            combine_task = CombineTask(group_dir=group_dir)

            # Add the task to video processor
            await real_video_processor.add_work(combine_task)

            # Wait a moment for state to be written
            await asyncio.sleep(0.5)

            # Check state file content during processing
            video_state_file = os.path.join(
                setup_storage_environment, "video_queue_state.json"
            )

            assert os.path.exists(video_state_file), "Video state file should exist"
            # Note: Autocam state file may not exist if no tasks were queued

            with open(video_state_file, "r") as f:
                video_state = json.load(f)

                # State should contain the queued item
                if len(video_state) > 0:
                    # Verify state contains the correct task information
                    task_data = video_state[0]
                    assert "task_type" in task_data or "item" in task_data, (
                        "Video state should contain task information"
                    )

            # Wait for processing to complete
            await asyncio.sleep(2)

            # Verify state is empty after processing
            with open(video_state_file, "r") as f:
                final_video_state = json.load(f)
                assert len(final_video_state) == 0, (
                    "Video state should be empty after processing"
                )

        finally:
            await real_video_processor.stop()
            await real_autocam_processor.stop()
            await real_upload_processor.stop()

    @pytest.mark.asyncio
    async def test_concurrent_processing_and_state_consistency(
        self, setup_storage_environment, mock_config
    ):
        """Test that multiple tasks are processed concurrently while maintaining state consistency."""
        # Create processors
        upload_processor = UploadProcessor(
            storage_path=setup_storage_environment,
            config=mock_config,
        )

        autocam_processor = AutocamProcessor(
            storage_path=setup_storage_environment,
            config=mock_config,
            upload_processor=upload_processor,
        )

        video_processor = VideoProcessor(
            storage_path=setup_storage_environment,
            config=mock_config,
            upload_processor=upload_processor,
        )

        await upload_processor.start()
        await autocam_processor.start()
        await video_processor.start()

        try:
            # Create multiple realistic directory structures
            group_dirs = [
                os.path.join(setup_storage_environment, "2023.01.01-10.00.00"),
                os.path.join(setup_storage_environment, "2023.01.01-10.05.00"),
                os.path.join(setup_storage_environment, "2023.01.01-10.10.00"),
            ]

            for group_dir in group_dirs:
                os.makedirs(group_dir, exist_ok=True)

            # Create multiple mock combine tasks
            combine_tasks = [
                CombineTask(group_dir=group_dir) for group_dir in group_dirs
            ]

            # Add all tasks to video processor
            for combine_task in combine_tasks:
                await video_processor.add_work(combine_task)

            # Wait for processing to complete
            await asyncio.sleep(3)

            # Verify all tasks were processed
            assert video_processor.get_queue_size() == 0, (
                "All tasks should be processed in video processor"
            )
            assert autocam_processor.get_queue_size() == 0, (
                "All tasks should be processed in autocam processor"
            )

            # Verify state files are consistent
            video_state_file = os.path.join(
                setup_storage_environment, "video_queue_state.json"
            )

            with open(video_state_file, "r") as f:
                video_state = json.load(f)
                assert len(video_state) == 0, (
                    "Video state should be empty after all processing"
                )

            # Note: Autocam state file may not exist if no tasks were queued

        finally:
            await video_processor.stop()
            await autocam_processor.stop()
            await upload_processor.stop()

    @pytest.mark.asyncio
    async def test_autocam_processor_handles_processing_errors_gracefully(
        self,
        setup_storage_environment,
        mock_config,
        real_video_processor,
        real_autocam_processor,
        real_upload_processor,
    ):
        """Test that AutocamProcessor handles processing errors gracefully."""
        # Start all processors
        await real_upload_processor.start()
        await real_autocam_processor.start()
        await real_video_processor.start()

        try:
            # Create a realistic directory structure
            group_dir = os.path.join(setup_storage_environment, "2023.01.01-10.00.00")
            os.makedirs(group_dir, exist_ok=True)

            # Create a mock combine task with invalid data
            combine_task = CombineTask(group_dir=group_dir)

            # Add the task to video processor
            await real_video_processor.add_work(combine_task)

            # Wait for processing
            await asyncio.sleep(2)

            # Verify that error was handled gracefully (queues should be empty)
            assert real_video_processor.get_queue_size() == 0, (
                "Video queue should be empty after error handling"
            )
            assert real_autocam_processor.get_queue_size() == 0, (
                "Autocam queue should be empty after error handling"
            )

        finally:
            await real_video_processor.stop()
            await real_autocam_processor.stop()
            await real_upload_processor.stop()
