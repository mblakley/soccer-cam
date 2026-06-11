import sys
from pathlib import Path
from unittest.mock import MagicMock

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

from video_grouper.service.main import VideoGrouperService, _resolve_storage_cwd


class TestVideoGrouperService:
    def test_initialization(self):
        """Test basic initialization of the service."""
        service = VideoGrouperService(args=())
        mock_win32event.CreateEvent.assert_called()
        assert service.running is False
        assert service.loop is None

    def test_svc_stop(self):
        """Test the SvcStop method."""
        service = VideoGrouperService(args=())
        service.ReportServiceStatus = MagicMock()
        service.SvcStop()
        service.ReportServiceStatus.assert_called_once_with(
            mock_win32service.SERVICE_STOP_PENDING
        )
        mock_win32event.SetEvent.assert_called_once_with(service.hWaitStop)
        assert service.running is False

    def test_svc_do_run(self):
        """Test the SvcDoRun method calls main()."""
        service = VideoGrouperService(args=())
        service.ReportServiceStatus = MagicMock()
        service.main = MagicMock()
        service.SvcDoRun()
        mock_servicemanager.LogMsg.assert_called()
        assert service.running is True
        service.main.assert_called_once()


class TestResolveStorageCwd:
    """Service CWD must follow [STORAGE] path, not config-file location.

    Regression: when these diverged (config under %ProgramData%, storage on
    a separate drive), DirectoryState's bare-basename fallback and any
    relative ``.lock`` write landed under %ProgramData% and every lock
    acquire raised FileNotFoundError on a path that didn't exist.
    """

    def test_returns_configured_storage_path(self):
        config = MagicMock()
        config.storage.path = r"D:\soccer-cam-storage"
        result = _resolve_storage_cwd(config)
        assert result == Path(r"D:\soccer-cam-storage")

    def test_is_independent_of_config_file_location(self, tmp_path):
        """Helper takes only config -- there's no way to accidentally
        derive CWD from the config file's parent directory."""
        config = MagicMock()
        config.storage.path = str(tmp_path / "storage_root")
        result = _resolve_storage_cwd(config)
        assert result == Path(tmp_path / "storage_root")
        # The helper itself must not require the path to exist; that's
        # the caller's job via mkdir(parents=True, exist_ok=True).
        assert not result.exists()
