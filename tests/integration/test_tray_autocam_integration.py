"""Integration tests for the tray app with AutocamProcessor."""

import pytest
import os
from unittest.mock import patch, AsyncMock
from pathlib import Path
import tempfile
from PyQt6.QtWidgets import QApplication

from video_grouper.tray.main import SystemTrayIcon
from video_grouper.utils.config import (
    Config,
    CameraConfig,
    StorageConfig,
    RecordingConfig,
    ProcessingConfig,
    LoggingConfig,
    AppConfig,
    TeamSnapConfig,
    PlayMetricsConfig,
    NtfyConfig,
    YouTubeConfig,
    AutocamConfig,
    CloudSyncConfig,
)


@pytest.fixture(scope="session")
def qt_app():
    """Create a QApplication for the test session."""
    app = QApplication([])
    yield app
    app.quit()


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield Path(temp_dir)


@pytest.fixture
def mock_config(temp_storage):
    """Create a real Config object for integration tests."""
    return Config(
        camera=CameraConfig(
            type="dahua", device_ip="127.0.0.1", username="admin", password="password"
        ),
        storage=StorageConfig(path=str(temp_storage)),
        recording=RecordingConfig(),
        processing=ProcessingConfig(),
        logging=LoggingConfig(),
        app=AppConfig(
            storage_path=str(temp_storage), update_url="https://test-updates.com"
        ),
        teamsnap=TeamSnapConfig(enabled=True, team_id="1", my_team_name="Team A"),
        teamsnap_teams=[],
        playmetrics=PlayMetricsConfig(
            enabled=True, username="user", password="password", team_name="Team A"
        ),
        playmetrics_teams=[],
        ntfy=NtfyConfig(enabled=True, server_url="http://ntfy.sh", topic="soccercam"),
        youtube=YouTubeConfig(enabled=True),
        autocam=AutocamConfig(enabled=True, executable="test_autocam.exe"),
        cloud_sync=CloudSyncConfig(enabled=True),
    )


class TestTrayAutocamIntegration:
    """Test integration between tray app and AutocamProcessor."""

    def test_tray_app_autocam_processor_initialization(
        self, qt_app, mock_config, temp_storage
    ):
        """Test that the tray app properly initializes AutocamProcessor."""
        # Mock the config loading and other dependencies
        with (
            patch("video_grouper.tray.main.load_config", return_value=mock_config),
            patch(
                "video_grouper.tray.main.get_shared_data_path",
                return_value=temp_storage,
            ),
            patch("video_grouper.tray.main.Path.exists", return_value=True),
            patch("video_grouper.tray.main.SystemTrayIcon.init_ui"),
            patch("video_grouper.tray.main.SystemTrayIcon.start_update_checker"),
        ):
            # Create tray app
            tray_app = SystemTrayIcon()

            # Verify AutocamProcessor was created
            assert hasattr(tray_app, "autocam_processor")
            assert tray_app.autocam_processor is not None
            assert isinstance(tray_app.autocam_processor.storage_path, str)
            assert os.path.exists(tray_app.autocam_processor.storage_path)
            assert tray_app.autocam_processor.config == mock_config

    @pytest.mark.asyncio
    async def test_tray_app_async_initialization(
        self, qt_app, mock_config, temp_storage
    ):
        """Test async initialization of the tray app."""
        # Mock the config loading and other dependencies
        with (
            patch("video_grouper.tray.main.load_config", return_value=mock_config),
            patch(
                "video_grouper.tray.main.get_shared_data_path",
                return_value=temp_storage,
            ),
            patch("video_grouper.tray.main.Path.exists", return_value=True),
            patch("video_grouper.tray.main.SystemTrayIcon.init_ui"),
            patch("video_grouper.tray.main.SystemTrayIcon.start_update_checker"),
            patch("video_grouper.tray.main.QThreadPool"),
        ):
            # Create tray app
            tray_app = SystemTrayIcon()

            # Mock the autocam processor on the instance
            mock_processor = AsyncMock()
            mock_processor.start = AsyncMock()
            mock_processor.stop = AsyncMock()
            tray_app.autocam_processor = mock_processor

            # Test async initialization
            await tray_app.initialize()

            # Verify autocam processor was started
            mock_processor.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_tray_app_async_shutdown(self, qt_app, mock_config, temp_storage):
        """Test async shutdown of the tray app."""
        # Mock the config loading and other dependencies
        with (
            patch("video_grouper.tray.main.load_config", return_value=mock_config),
            patch(
                "video_grouper.tray.main.get_shared_data_path",
                return_value=temp_storage,
            ),
            patch("video_grouper.tray.main.Path.exists", return_value=True),
            patch("video_grouper.tray.main.SystemTrayIcon.init_ui"),
            patch("video_grouper.tray.main.SystemTrayIcon.start_update_checker"),
            patch("video_grouper.tray.main.QThreadPool"),
        ):
            # Create tray app
            tray_app = SystemTrayIcon()

            # Mock the autocam processor on the instance
            mock_processor = AsyncMock()
            mock_processor.start = AsyncMock()
            mock_processor.stop = AsyncMock()
            tray_app.autocam_processor = mock_processor

            # Test async shutdown
            await tray_app.shutdown()

            # Verify autocam processor was stopped
            mock_processor.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_tray_app_shutdown_without_processor(
        self, qt_app, mock_config, temp_storage
    ):
        """Test shutdown when autocam processor doesn't exist."""
        # Mock the config loading and other dependencies
        with (
            patch("video_grouper.tray.main.load_config", return_value=mock_config),
            patch(
                "video_grouper.tray.main.get_shared_data_path",
                return_value=temp_storage,
            ),
            patch("video_grouper.tray.main.Path.exists", return_value=True),
            patch("video_grouper.tray.main.SystemTrayIcon.init_ui"),
            patch("video_grouper.tray.main.SystemTrayIcon.start_update_checker"),
            patch("video_grouper.tray.main.QThreadPool"),
        ):
            # Create tray app
            tray_app = SystemTrayIcon()

            # Remove autocam processor to test graceful handling
            delattr(tray_app, "autocam_processor")

            # Test async shutdown (should not raise an error)
            await tray_app.shutdown()
