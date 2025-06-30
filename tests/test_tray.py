import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
import sys

# Add the project root to the Python path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication
from video_grouper.tray.main import SystemTrayIcon
from video_grouper.utils.config import Config, AppConfig, StorageConfig


# We need a QApplication instance to test PyQt components
@pytest.fixture(scope="session")
def qapp():
    """Session-wide QApplication instance."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


@pytest.fixture
def mock_config(tmp_path):
    """Create a mock pydantic Config object."""
    config = MagicMock(spec=Config)
    config.app = MagicMock(spec=AppConfig)
    config.app.update_url = "http://fake-update-url.com"
    config.storage = MagicMock(spec=StorageConfig)
    config.storage.path = str(tmp_path)
    config.paths = MagicMock()
    config.paths.shared_data_path = str(tmp_path / "shared")
    return config


@patch("video_grouper.tray.main.get_version", return_value="1.0.0")
@patch("video_grouper.tray.main.get_full_version", return_value="1.0.0-test")
@patch("video_grouper.tray.main.get_shared_data_path")
@patch("video_grouper.tray.main.load_config")
@patch("video_grouper.tray.main.QSystemTrayIcon.__init__")
@patch("video_grouper.tray.main.SystemTrayIcon.init_ui")
@patch("video_grouper.tray.main.SystemTrayIcon.start_update_checker")
def test_system_tray_icon_initialization(
    mock_start_update_checker,
    mock_init_ui,
    mock_super_init,
    mock_load_config,
    mock_get_shared_data_path,
    mock_get_full_version,
    mock_get_version,
    mock_config,
    tmp_path,
    qapp,
):
    """Test the initialization of the SystemTrayIcon."""
    mock_data_path = tmp_path / "shared"
    mock_data_path.mkdir()
    config_file = mock_data_path / "config.ini"
    config_file.touch()

    mock_get_shared_data_path.return_value = mock_data_path
    mock_load_config.return_value = mock_config

    # Instantiate the class
    tray_icon = SystemTrayIcon()

    # Assertions
    mock_super_init.assert_called_once()
    assert tray_icon.version == "1.0.0"
    assert tray_icon.full_version == "1.0.0-test"
    assert tray_icon.config_path == config_file
    mock_load_config.assert_called_once_with(config_file)
    assert tray_icon.config == mock_config
    assert tray_icon.update_url == "http://fake-update-url.com"
    mock_init_ui.assert_called_once()
    mock_start_update_checker.assert_called_once()


@patch("win32serviceutil.StartService")
def test_start_service_success(mock_start_service):
    """Test the start_service method for success."""
    with patch("video_grouper.tray.main.SystemTrayIcon.__init__", lambda x: None):
        tray_icon = SystemTrayIcon()
        tray_icon.showMessage = MagicMock()
        tray_icon.start_service()
        mock_start_service.assert_called_once_with("VideoGrouperService")
        tray_icon.showMessage.assert_called_once_with(
            "Service", "Service started successfully"
        )


@patch("win32serviceutil.StartService", side_effect=Exception("Test Error"))
def test_start_service_failure(mock_start_service):
    """Test the start_service method for failure."""
    with patch("video_grouper.tray.main.SystemTrayIcon.__init__", lambda x: None):
        tray_icon = SystemTrayIcon()
        tray_icon.showMessage = MagicMock()

        tray_icon.start_service()

        mock_start_service.assert_called_once_with("VideoGrouperService")
        tray_icon.showMessage.assert_called_with(
            "Service",
            "Failed to start service: Test Error",
            3,  # Corresponds to QSystemTrayIcon.MessageIcon.Critical
        )
