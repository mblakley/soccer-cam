import sys
from unittest.mock import patch, MagicMock, AsyncMock
import pytest
from pathlib import Path

# Add the project root to the Python path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Mock windows specific modules before import
mock_win32serviceutil = MagicMock()
mock_win32serviceutil.ServiceFramework = type(
    "ServiceFramework", (object,), {"__init__": MagicMock(return_value=None)}
)
mock_win32event = MagicMock()
mock_servicemanager = MagicMock()
mock_win32service = MagicMock()

sys.modules["win32serviceutil"] = mock_win32serviceutil
sys.modules["win32event"] = mock_win32event
sys.modules["servicemanager"] = mock_servicemanager
sys.modules["win32service"] = mock_win32service

from video_grouper.service.main import VideoGrouperService
from video_grouper.utils.config import Config, AppConfig


@pytest.fixture
def mock_config_for_service():
    """Create a mock pydantic Config object for service tests."""
    config = MagicMock(spec=Config)
    config.app = MagicMock(spec=AppConfig)
    config.app.update_url = "http://fake-update-url.com"
    return config


@pytest.fixture
def service_instance(mock_config_for_service):
    """Provides a patched VideoGrouperService instance for testing."""
    with (
        patch("video_grouper.service.main.get_version", return_value="1.0.0"),
        patch("video_grouper.service.main.get_full_version", return_value="1.0.0-test"),
        patch("video_grouper.service.main.get_shared_data_path") as mock_get_path,
        patch("video_grouper.service.main.load_config") as mock_load_config,
        patch("video_grouper.service.main.FileLock"),
    ):
        mock_data_path = Path("/fake/path")
        mock_get_path.return_value = mock_data_path

        with patch.object(Path, "exists", return_value=True):
            mock_load_config.return_value = mock_config_for_service
            service = VideoGrouperService(args=())
            service.ReportServiceStatus = MagicMock()
            yield service


class TestVideoGrouperService:
    def test_initialization_success(self, service_instance, mock_config_for_service):
        """Test successful initialization of the service."""
        assert service_instance.config == mock_config_for_service
        assert service_instance.update_url == "http://fake-update-url.com"
        mock_win32event.CreateEvent.assert_called()
        mock_win32serviceutil.ServiceFramework.__init__.assert_called_with(
            service_instance, ()
        )

    def test_initialization_no_config_file(self):
        """Test initialization when config.ini does not exist."""
        with (
            patch("video_grouper.service.main.get_shared_data_path"),
            patch.object(Path, "exists", return_value=False),
        ):
            service = VideoGrouperService(args=())
            assert service.config is None
            assert service.update_url == "https://updates.videogrouper.com"

    def test_initialization_config_load_error(self):
        """Test initialization when load_config raises an exception."""
        with (
            patch("video_grouper.service.main.get_shared_data_path"),
            patch.object(Path, "exists", return_value=True),
            patch(
                "video_grouper.service.main.load_config",
                side_effect=Exception("Bad config"),
            ),
            patch("video_grouper.service.main.FileLock"),
        ):
            service = VideoGrouperService(args=())
            assert service.config is None
            assert service.update_url == "https://updates.videogrouper.com"

    def test_initialization_file_lock_timeout(self):
        """Test initialization when FileLock raises TimeoutError."""
        with (
            patch("video_grouper.service.main.get_shared_data_path"),
            patch.object(Path, "exists", return_value=True),
            patch(
                "video_grouper.service.main.FileLock",
                side_effect=TimeoutError("Locked"),
            ),
        ):
            service = VideoGrouperService(args=())
            assert service.config is None

    def test_svc_stop(self, service_instance):
        """Test the SvcStop method."""
        service_instance.SvcStop()
        service_instance.ReportServiceStatus.assert_called_once_with(
            mock_win32service.SERVICE_STOP_PENDING
        )
        mock_win32event.SetEvent.assert_called_once_with(service_instance.stop_event)
        assert service_instance.running is False

    def test_svc_do_run(self, service_instance):
        """Test the main run loop of the service."""
        with patch("video_grouper.service.main.asyncio.run") as mock_asyncio_run:
            service_instance.SvcDoRun()
            mock_servicemanager.LogMsg.assert_called_once()

            # Check that asyncio.run was called
            mock_asyncio_run.assert_called_once()

            # Close the coroutine to prevent RuntimeWarning
            coro = mock_asyncio_run.call_args.args[0]
            coro.close()

            assert service_instance.running is True

    @pytest.mark.asyncio
    async def test_run_main_service_calls_main_app(self, service_instance):
        """Test that run_main_service calls the main application entry point."""
        with patch("video_grouper.service.main.VideoGrouperApp") as mock_app:
            mock_instance = mock_app.return_value
            mock_instance.run = AsyncMock()
            await service_instance.run_main_service()
            mock_app.assert_called_once_with(service_instance.config)
            mock_instance.run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_check_updates_loop(self, service_instance):
        """Test the update checking loop."""
        with (
            patch(
                "video_grouper.service.main.check_and_update", new_callable=AsyncMock
            ) as mock_check,
            patch(
                "video_grouper.service.main.asyncio.sleep", new_callable=AsyncMock
            ) as mock_sleep,
        ):
            service_instance.running = True

            async def stop_loop(*args):
                service_instance.running = False

            mock_sleep.side_effect = stop_loop

            await service_instance.check_updates()

            mock_check.assert_awaited_once_with(
                service_instance.version, service_instance.update_url
            )
            mock_sleep.assert_awaited_once_with(3600)
