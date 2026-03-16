import sys
from unittest.mock import MagicMock
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
