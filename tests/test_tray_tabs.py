"""
Tests for ConfigWindow tab UI content.

Covers:
- Empty-state messages for each dynamic queue tab
- Data display for each queue tab when mock data is present
- Locked-file handling (busy message)
- Match info key fix verification (team_name, not my_team_name)
"""

import pytest
import sys
import os
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ---------------------------------------------------------------------------
# Stub out Windows-only native modules BEFORE importing anything from
# video_grouper.  Importing video_grouper.tray.config_ui triggers
# video_grouper/__init__.py → VideoGrouperApp → ball_tracking_processor →
# autocam_automation → pywinauto, which requires UIAutomationCore.dll.
# That COM DLL is unavailable in the test environment, so we stub the whole
# chain here at module load time.
# ---------------------------------------------------------------------------
_STUB_MODULES = [
    "pywinauto",
    "pywinauto.Desktop",
    "pywinauto.application",
    "comtypes",
    "comtypes.client",
    "win32gui",
    "win32serviceutil",
    "win32service",
    "win32api",
    "win32con",
    "winerror",
    "pywintypes",
]
for _mod in _STUB_MODULES:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# Now it is safe to import from video_grouper.  The module is cached after
# this point, so the patch() calls in fixtures resolve the already-loaded
# object rather than re-importing it.
from video_grouper.tray.config_ui import ConfigWindow  # noqa: E402
from video_grouper.tray.match_info_item_widget import MatchInfoItemWidget  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402


# ---------------------------------------------------------------------------
# QApplication fixture (module-scoped to avoid re-creating it per test)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


# ---------------------------------------------------------------------------
# Config file fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def config_file(tmp_path):
    """Write a minimal config.ini and return its path."""
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()

    content = f"""[CAMERA.default]
name = default
type = reolink
device_ip = 192.168.1.100
username = admin
password = test

[STORAGE]
path = {storage_dir}

[RECORDING]

[PROCESSING]

[LOGGING]

[APP]
timezone = UTC

[TEAMSNAP]
enabled = false

[PLAYMETRICS]
enabled = false

[NTFY]
enabled = false

[YOUTUBE]
enabled = false

[AUTOCAM]
enabled = false

[CLOUD_SYNC]
enabled = false

[TTT]
enabled = false
"""
    cfg = tmp_path / "config.ini"
    cfg.write_text(content)
    return cfg


# ---------------------------------------------------------------------------
# ConfigWindow fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def config_window(qapp, config_file, mock_file_system, tmp_path):
    """
    Create a ConfigWindow for testing.

    - Patches get_shared_data_path to tmp_path so queue-file reads land there.
    - Stops the auto-refresh timer immediately after construction.
    - mock_file_system (autouse conftest) is accepted so it is active; we mock
      _read_json_file directly in individual tests instead of fighting path
      resolution.
    """
    with patch(
        "video_grouper.tray.config_ui.get_shared_data_path",
        return_value=tmp_path,
    ):
        window = ConfigWindow(config_path=str(config_file))
        window.queue_timer.stop()
        yield window
        window.close()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _get_list_texts(list_widget):
    """Return all plain-text strings from a QListWidget (non-widget items only)."""
    texts = []
    for i in range(list_widget.count()):
        item = list_widget.item(i)
        if item is not None:
            texts.append(item.text())
    return texts


# ===========================================================================
# 1. Empty-state tests
# ===========================================================================


class TestEmptyStates:
    """When no data files exist, each tab shows the correct empty-state message."""

    def test_download_queue_empty(self, config_window):
        config_window._read_json_file = MagicMock(return_value=None)
        config_window.refresh_download_queue_display()
        texts = _get_list_texts(config_window.download_queue_list)
        assert texts == ["No downloads queued."]

    def test_download_queue_empty_list(self, config_window):
        config_window._read_json_file = MagicMock(return_value=[])
        config_window.refresh_download_queue_display()
        texts = _get_list_texts(config_window.download_queue_list)
        assert texts == ["No downloads queued."]

    def test_processing_queue_empty(self, config_window):
        config_window._read_json_file = MagicMock(return_value=None)
        config_window.refresh_processing_queue_display()
        texts = _get_list_texts(config_window.processing_queue_list)
        assert texts == ["No processing tasks queued."]

    def test_processing_queue_empty_list(self, config_window):
        config_window._read_json_file = MagicMock(return_value=[])
        config_window.refresh_processing_queue_display()
        texts = _get_list_texts(config_window.processing_queue_list)
        assert texts == ["No processing tasks queued."]

    def test_autocam_queue_empty(self, config_window):
        config_window._read_json_file = MagicMock(return_value=None)
        config_window.refresh_ball_tracking_queue_display()
        texts = _get_list_texts(config_window.ball_tracking_queue_list)
        assert texts == ["No processing tasks queued."]

    def test_autocam_queue_empty_list(self, config_window):
        config_window._read_json_file = MagicMock(return_value=[])
        config_window.refresh_ball_tracking_queue_display()
        texts = _get_list_texts(config_window.ball_tracking_queue_list)
        assert texts == ["No processing tasks queued."]

    def test_youtube_upload_empty(self, config_window):
        config_window._read_json_file = MagicMock(return_value=None)
        config_window.refresh_youtube_upload_display()
        texts = _get_list_texts(config_window.youtube_upload_list)
        assert texts == ["No uploads queued."]

    def test_youtube_upload_empty_dict(self, config_window):
        config_window._read_json_file = MagicMock(
            return_value={"in_progress": None, "queue": []}
        )
        config_window.refresh_youtube_upload_display()
        texts = _get_list_texts(config_window.youtube_upload_list)
        assert texts == ["No uploads queued."]

    def test_skipped_files_empty(self, config_window):
        """No skipped files in storage dir → empty-state message."""
        storage_path = config_window.config.storage.path
        # Ensure storage dir exists but has no group dirs
        os.makedirs(storage_path, exist_ok=True)
        with (
            patch("os.path.isdir", return_value=True),
            patch("os.listdir", return_value=[]),
        ):
            config_window.refresh_skipped_files_display()
        texts = _get_list_texts(config_window.skipped_list)
        assert texts == ["No skipped files."]

    def test_cleanup_empty(self, config_window, tmp_path):
        """No cleanup state file → empty-state message."""
        with patch("video_grouper.tray.config_ui.os.path.exists", return_value=False):
            config_window.refresh_cleanup_display()
        texts = _get_list_texts(config_window.cleanup_list)
        assert texts == ["No files pending cleanup."]

    def test_match_info_empty(self, config_window):
        """No group dirs with combined status → empty-state message."""
        with (
            patch("os.path.isdir", return_value=True),
            patch("os.listdir", return_value=[]),
        ):
            config_window.refresh_match_info_display()
        texts = _get_list_texts(config_window.match_info_list)
        assert texts == ["No videos awaiting match info."]


# ===========================================================================
# 2. Data display tests
# ===========================================================================


class TestDataDisplay:
    """Verify correct items appear when data is present."""

    def test_autocam_queue_shows_items(self, config_window):
        data = [
            {"group_name": "2024.01.15-09.00.00", "status": "pending"},
            {"group_name": "2024.01.16-10.30.00", "status": "processing"},
        ]
        config_window._read_json_file = MagicMock(return_value=data)
        config_window.refresh_ball_tracking_queue_display()

        texts = _get_list_texts(config_window.ball_tracking_queue_list)
        assert len(texts) == 2
        assert "2024.01.15-09.00.00" in texts[0]
        assert "pending" in texts[0]
        assert "2024.01.16-10.30.00" in texts[1]
        assert "processing" in texts[1]

    def test_youtube_upload_new_format(self, config_window):
        data = {
            "in_progress": {"group_dir": "/storage/2024.01.15-09.00.00"},
            "queue": [
                {"group_dir": "/storage/2024.01.16-10.30.00"},
                {"group_dir": "/storage/2024.01.17-11.00.00"},
            ],
        }
        config_window._read_json_file = MagicMock(return_value=data)
        config_window.refresh_youtube_upload_display()

        texts = _get_list_texts(config_window.youtube_upload_list)
        assert len(texts) == 3
        assert texts[0] == "[Uploading] 2024.01.15-09.00.00"
        assert texts[1] == "[Pending] 2024.01.16-10.30.00"
        assert texts[2] == "[Pending] 2024.01.17-11.00.00"

    def test_youtube_upload_legacy_format(self, config_window):
        """Legacy list format (no 'in_progress' key) still renders."""
        data = [
            {"group_dir": "/storage/2024.01.15-09.00.00"},
            {"group_dir": "/storage/2024.01.16-10.30.00"},
        ]
        config_window._read_json_file = MagicMock(return_value=data)
        config_window.refresh_youtube_upload_display()

        texts = _get_list_texts(config_window.youtube_upload_list)
        assert len(texts) == 2
        assert texts[0] == "[Pending] 2024.01.15-09.00.00"
        assert texts[1] == "[Pending] 2024.01.16-10.30.00"

    def test_download_queue_shows_items(self, config_window):
        """Download queue with 2 items creates 2 widget-backed list entries."""
        data = [
            {
                "file_path": "/storage/2024.01.15-09.00.00/video1.dav",
                "status": "pending",
            },
            {
                "file_path": "/storage/2024.01.16-10.30.00/video2.dav",
                "status": "pending",
            },
        ]
        config_window._read_json_file = MagicMock(return_value=data)
        config_window.refresh_download_queue_display()

        # Items use setItemWidget so item.text() is empty; count is the measure.
        assert config_window.download_queue_list.count() == 2

    def test_processing_queue_shows_items(self, config_window):
        """Processing queue with two convert tasks creates 2 widget-backed entries."""
        data = [
            ["convert", "/storage/2024.01.15-09.00.00/video1.dav"],
            ["convert", "/storage/2024.01.16-10.30.00/video2.dav"],
        ]
        config_window._read_json_file = MagicMock(return_value=data)
        config_window.refresh_processing_queue_display()

        assert config_window.processing_queue_list.count() == 2

    def test_cleanup_display_shows_files(self, config_window, tmp_path):
        """Cleanup state with files renders one widget per file."""
        storage_path = config_window.config.storage.path
        state = {
            "deletion_supported": True,
            "files": [
                {
                    "path": "/camera/home/rec1.mp4",
                    "startTime": "2024-01-15 09:00:00",
                    "endTime": "2024-01-15 10:00:00",
                    "size": 1048576,
                },
                {
                    "path": "/camera/home/rec2.mp4",
                    "startTime": "2024-01-16 09:00:00",
                    "endTime": "2024-01-16 10:00:00",
                    "size": 2097152,
                },
            ],
        }
        cleanup_path = Path(storage_path) / "home_cleanup_state.json"
        cleanup_path.write_text(json.dumps(state))

        with patch("video_grouper.tray.config_ui.os.path.exists", return_value=True):
            config_window.refresh_cleanup_display()

        # Widget-backed items: count should equal number of files
        assert config_window.cleanup_list.count() == 2

    def test_connection_history_shows_timeframes(self, config_window):
        """Connection events are parsed and displayed as timeframe strings."""
        storage_path = config_window.config.storage.path
        state = {
            "cam1": {
                "connection_events": [
                    {
                        "event_datetime": "2024-01-15T09:00:00+00:00",
                        "event_type": "connected",
                        "message": "",
                    },
                    {
                        "event_datetime": "2024-01-15T10:30:00+00:00",
                        "event_type": "disconnected",
                        "message": "signal lost",
                    },
                ]
            }
        }
        camera_state_path = Path(storage_path) / "camera_state.json"
        camera_state_path.write_text(json.dumps(state))

        # Patch isdir and exists for the storage path checks
        real_exists = Path.exists

        def _exists(p):
            return real_exists(p)

        with patch.object(Path, "exists", _exists):
            config_window.refresh_connection_events_display()

        texts = _get_list_texts(config_window.connection_events_list)
        assert len(texts) == 1
        assert "[cam1]" in texts[0]
        assert "Connected:" in texts[0]
        assert "Disconnected:" in texts[0]

    def test_connection_history_no_events(self, config_window):
        """Camera state exists but has no connection_events → empty message."""
        storage_path = config_window.config.storage.path
        state = {"cam1": {"connection_events": []}}
        camera_state_path = Path(storage_path) / "camera_state.json"
        camera_state_path.write_text(json.dumps(state))

        real_exists = Path.exists

        def _exists(p):
            return real_exists(p)

        with patch.object(Path, "exists", _exists):
            config_window.refresh_connection_events_display()

        texts = _get_list_texts(config_window.connection_events_list)
        assert texts == ["No connection events recorded."]

    def test_skipped_files_show_items(self, config_window, tmp_path):
        """Group dir with a skipped file appears in the skipped list."""
        storage_path = config_window.config.storage.path
        group_dir_name = "2024.01.15-09.00.00"
        group_dir = Path(storage_path) / group_dir_name
        group_dir.mkdir(parents=True, exist_ok=True)

        file_path = str(group_dir / "video1.dav")
        state = {
            "status": "downloading",
            "error_message": None,
            "files": {
                file_path: {
                    "file_path": file_path,
                    "status": "downloaded",
                    "skip": True,
                    "total_size": 1024,
                }
            },
        }
        # DirectoryState reads from the state file path resolved via get_state_file_path.
        # The simplest approach: write state.json directly in the group dir.
        state_file = group_dir / "state.json"
        state_file.write_text(json.dumps(state))

        # Patch os.path.isdir to return True for our storage and group dirs,
        # and os.listdir to return only our group dir.
        real_isdir = os.path.isdir

        def _isdir(p):
            return real_isdir(p)

        with (
            patch("os.path.isdir", side_effect=_isdir),
            patch("os.listdir", return_value=[group_dir_name]),
        ):
            config_window.refresh_skipped_files_display()

        # Widget-backed items: count should be 1
        assert config_window.skipped_list.count() == 1

    def test_match_info_shows_pending_dir(self, config_window, tmp_path):
        """Group dir with status=combined and no match_info.ini appears in match info list."""
        storage_path = config_window.config.storage.path
        group_dir_name = "2024.01.15-09.00.00"
        group_dir = Path(storage_path) / group_dir_name
        group_dir.mkdir(parents=True, exist_ok=True)

        # Write a state.json with status=combined
        state = {
            "status": "combined",
            "error_message": None,
            "files": {},
        }
        state_file = group_dir / "state.json"
        state_file.write_text(json.dumps(state))

        real_isdir = os.path.isdir

        def _isdir(p):
            return real_isdir(p)

        # No match_info.ini → MatchInfo.from_file returns incomplete info
        with (
            patch("os.path.isdir", side_effect=_isdir),
            patch("os.listdir", return_value=[group_dir_name]),
        ):
            config_window.refresh_match_info_display()

        # Widget-backed list item count should be 1
        assert config_window.match_info_list.count() == 1


# ===========================================================================
# 3. Locked-file tests
# ===========================================================================


class TestLockedFile:
    """_read_json_file returning 'locked' shows the busy message."""

    def test_download_queue_locked(self, config_window):
        config_window._read_json_file = MagicMock(return_value="locked")
        config_window.refresh_download_queue_display()
        texts = _get_list_texts(config_window.download_queue_list)
        assert texts == ["Queue file is busy, will retry..."]

    def test_processing_queue_locked(self, config_window):
        config_window._read_json_file = MagicMock(return_value="locked")
        config_window.refresh_processing_queue_display()
        texts = _get_list_texts(config_window.processing_queue_list)
        assert texts == ["Queue file is busy, will retry..."]

    def test_autocam_queue_locked(self, config_window):
        config_window._read_json_file = MagicMock(return_value="locked")
        config_window.refresh_ball_tracking_queue_display()
        texts = _get_list_texts(config_window.ball_tracking_queue_list)
        assert texts == ["Queue file is busy, will retry..."]

    def test_youtube_upload_locked(self, config_window):
        config_window._read_json_file = MagicMock(return_value="locked")
        config_window.refresh_youtube_upload_display()
        texts = _get_list_texts(config_window.youtube_upload_list)
        assert texts == ["Queue file is busy, will retry..."]


# ===========================================================================
# 4. Match info key fix verification
# ===========================================================================


class TestMatchInfoWidget:
    """MatchInfoItemWidget.on_save_clicked sends 'team_name', not 'my_team_name'."""

    def test_save_uses_team_name_key(
        self, qapp, config_file, mock_file_system, tmp_path
    ):
        """The info_dict passed to the callback must use 'team_name' key."""
        # Create a group dir with a valid datetime name
        group_dir = tmp_path / "2024.01.15-09.00.00"
        group_dir.mkdir()

        # Write an empty state.json so DirectoryState does not fail
        (group_dir / "state.json").write_text(
            json.dumps({"status": "combined", "error_message": None, "files": {}})
        )

        received = {}

        def _callback(path, info_dict):
            received.update(info_dict)

        widget = MatchInfoItemWidget(
            group_dir_path=str(group_dir),
            refresh_callback=_callback,
            timezone_str="UTC",
        )

        # Fill in the fields
        widget.my_team_name.setText("Red Hawks")
        widget.opponent_team_name.setText("Blue Eagles")
        widget.location.setText("Home Field")
        widget.start_time_offset.setText("05:00")

        widget.on_save_clicked()

        assert "team_name" in received, (
            "Callback dict must use 'team_name' key, not 'my_team_name'"
        )
        assert received["team_name"] == "Red Hawks"
        assert received["opponent_team_name"] == "Blue Eagles"
        assert received["location"] == "Home Field"
        assert "my_team_name" not in received, (
            "'my_team_name' key must not appear in callback dict"
        )


# ===========================================================================
# 5. Real processor state format tests
# ===========================================================================


class TestRealProcessorStateFormat:
    """Verify each queue tab handles the real save_state() dict format:
    {"queue": [{"priority": N, "seq": N, ...task_fields...}], "in_progress": {...} | null}
    """

    # -- Download queue --

    def test_download_queue_real_format(self, config_window):
        """Download queue must parse the dict format from DownloadProcessor.save_state()."""
        data = {
            "queue": [
                {
                    "priority": 2,
                    "seq": 0,
                    "task_type": "recording_file",
                    "file_path": "/storage/2024.01.15-09.00.00/video1.dav",
                    "start_time": "2024-01-15T09:00:00",
                    "end_time": "2024-01-15T09:30:00",
                    "status": "pending",
                    "metadata": {},
                    "skip": False,
                },
                {
                    "priority": 2,
                    "seq": 1,
                    "task_type": "recording_file",
                    "file_path": "/storage/2024.01.15-09.00.00/video2.dav",
                    "start_time": "2024-01-15T09:30:00",
                    "end_time": "2024-01-15T10:00:00",
                    "status": "pending",
                    "metadata": {},
                    "skip": False,
                },
            ],
            "in_progress": None,
        }
        config_window._read_json_file = MagicMock(return_value=data)
        config_window.refresh_download_queue_display()
        assert config_window.download_queue_list.count() == 2

    def test_download_queue_real_format_with_in_progress(self, config_window):
        """Download queue with one item in-progress shows both in-progress and queued."""
        data = {
            "queue": [
                {
                    "priority": 2,
                    "seq": 1,
                    "task_type": "recording_file",
                    "file_path": "/storage/2024.01.15-09.00.00/video2.dav",
                    "start_time": "2024-01-15T09:30:00",
                    "end_time": "2024-01-15T10:00:00",
                    "status": "pending",
                    "metadata": {},
                    "skip": False,
                },
            ],
            "in_progress": {
                "task_type": "recording_file",
                "file_path": "/storage/2024.01.15-09.00.00/video1.dav",
                "start_time": "2024-01-15T09:00:00",
                "end_time": "2024-01-15T09:30:00",
                "status": "downloading",
                "metadata": {},
                "skip": False,
            },
        }
        config_window._read_json_file = MagicMock(return_value=data)
        config_window.refresh_download_queue_display()
        # 1 in-progress + 1 queued = 2
        assert config_window.download_queue_list.count() == 2

    def test_download_queue_real_format_empty(self, config_window):
        """Download queue with empty queue list and no in-progress shows empty message."""
        data = {"queue": [], "in_progress": None}
        config_window._read_json_file = MagicMock(return_value=data)
        config_window.refresh_download_queue_display()
        texts = _get_list_texts(config_window.download_queue_list)
        assert texts == ["No downloads queued."]

    # -- Processing queue --

    def test_processing_queue_real_format(self, config_window):
        """Processing queue must parse the dict format from VideoProcessor.save_state()."""
        data = {
            "queue": [
                {
                    "priority": 2,
                    "seq": 0,
                    "task_type": "combine",
                    "group_dir": "/storage/2024.01.15-09.00.00",
                },
                {
                    "priority": 2,
                    "seq": 1,
                    "task_type": "trim",
                    "group_dir": "/storage/2024.01.16-10.30.00",
                    "start_time": "00:05:00",
                    "end_time": "01:35:00",
                },
            ],
            "in_progress": None,
        }
        config_window._read_json_file = MagicMock(return_value=data)
        config_window.refresh_processing_queue_display()
        assert config_window.processing_queue_list.count() == 2

    def test_processing_queue_real_format_with_in_progress(self, config_window):
        """Processing queue with in-progress combine task shows both."""
        data = {
            "queue": [
                {
                    "priority": 2,
                    "seq": 1,
                    "task_type": "combine",
                    "group_dir": "/storage/2024.01.16-10.30.00",
                },
            ],
            "in_progress": {
                "task_type": "combine",
                "group_dir": "/storage/2024.01.15-09.00.00",
            },
        }
        config_window._read_json_file = MagicMock(return_value=data)
        config_window.refresh_processing_queue_display()
        # 1 in-progress + 1 queued = 2
        assert config_window.processing_queue_list.count() == 2

    def test_processing_queue_real_format_empty(self, config_window):
        """Processing queue dict format with empty queue shows empty message."""
        data = {"queue": [], "in_progress": None}
        config_window._read_json_file = MagicMock(return_value=data)
        config_window.refresh_processing_queue_display()
        texts = _get_list_texts(config_window.processing_queue_list)
        assert texts == ["No processing tasks queued."]

    # -- Autocam queue --

    def test_autocam_queue_real_format(self, config_window):
        """Autocam queue must parse the dict format from AutocamProcessor.save_state()."""
        data = {
            "queue": [
                {
                    "priority": 2,
                    "seq": 0,
                    "task_type": "ball_tracking_process",
                    "group_dir": "/storage/2024.01.15-09.00.00",
                    "input_path": "/storage/2024.01.15-09.00.00/trimmed/video-raw.mp4",
                    "output_path": "/storage/2024.01.15-09.00.00/trimmed/video.mp4",
                    "provider_name": "autocam_gui",
                    "provider_config": {"executable": "C:/autocam/autocam.exe"},
                },
            ],
            "in_progress": None,
        }
        config_window._read_json_file = MagicMock(return_value=data)
        config_window.refresh_ball_tracking_queue_display()
        texts = _get_list_texts(config_window.ball_tracking_queue_list)
        assert len(texts) == 1
        assert "2024.01.15-09.00.00" in texts[0]

    def test_autocam_queue_real_format_with_in_progress(self, config_window):
        """Autocam queue with in-progress item shows it."""
        data = {
            "queue": [],
            "in_progress": {
                "task_type": "ball_tracking_process",
                "group_dir": "/storage/2024.01.15-09.00.00",
                "input_path": "/storage/2024.01.15-09.00.00/trimmed/video-raw.mp4",
                "output_path": "/storage/2024.01.15-09.00.00/trimmed/video.mp4",
                "provider_name": "autocam_gui",
                "provider_config": {"executable": "C:/autocam/autocam.exe"},
            },
        }
        config_window._read_json_file = MagicMock(return_value=data)
        config_window.refresh_ball_tracking_queue_display()
        texts = _get_list_texts(config_window.ball_tracking_queue_list)
        assert len(texts) == 1
        assert "2024.01.15-09.00.00" in texts[0]

    # -- YouTube upload (regression) --

    def test_youtube_upload_real_format_with_priority(self, config_window):
        """YouTube upload handles queue items that have priority/seq fields from save_state()."""
        data = {
            "queue": [
                {
                    "priority": 2,
                    "seq": 0,
                    "task_type": "youtube_upload",
                    "group_dir": "/storage/2024.01.16-10.30.00",
                },
            ],
            "in_progress": {
                "task_type": "youtube_upload",
                "group_dir": "/storage/2024.01.15-09.00.00",
            },
        }
        config_window._read_json_file = MagicMock(return_value=data)
        config_window.refresh_youtube_upload_display()
        texts = _get_list_texts(config_window.youtube_upload_list)
        assert len(texts) == 2
        assert texts[0] == "[Uploading] 2024.01.15-09.00.00"
        assert texts[1] == "[Pending] 2024.01.16-10.30.00"


# ===========================================================================
# 6. In-progress display tests
# ===========================================================================


class TestInProgressDisplay:
    """Verify in-progress items are displayed with a visible status indicator."""

    def test_download_in_progress_shows_downloading_prefix(self, config_window):
        """Download queue shows [Downloading] prefix for in-progress item."""
        data = {
            "queue": [],
            "in_progress": {
                "task_type": "recording_file",
                "file_path": "/storage/2024.01.15-09.00.00/video1.dav",
                "start_time": "2024-01-15T09:00:00",
                "end_time": "2024-01-15T09:30:00",
                "status": "downloading",
                "metadata": {},
                "skip": False,
            },
        }
        config_window._read_json_file = MagicMock(return_value=data)
        config_window.refresh_download_queue_display()
        assert config_window.download_queue_list.count() == 1

    def test_processing_in_progress_shows_status(self, config_window):
        """Processing queue shows in-progress combine task."""
        data = {
            "queue": [],
            "in_progress": {
                "task_type": "combine",
                "group_dir": "/storage/2024.01.15-09.00.00",
            },
        }
        config_window._read_json_file = MagicMock(return_value=data)
        config_window.refresh_processing_queue_display()
        assert config_window.processing_queue_list.count() == 1

    def test_autocam_in_progress_shows_status(self, config_window):
        """Autocam queue shows in-progress task with status indicator."""
        data = {
            "queue": [],
            "in_progress": {
                "task_type": "ball_tracking_process",
                "group_dir": "/storage/2024.01.15-09.00.00",
                "input_path": "/storage/2024.01.15-09.00.00/trimmed/video-raw.mp4",
                "output_path": "/storage/2024.01.15-09.00.00/trimmed/video.mp4",
                "provider_name": "autocam_gui",
                "provider_config": {"executable": "C:/autocam/autocam.exe"},
            },
        }
        config_window._read_json_file = MagicMock(return_value=data)
        config_window.refresh_ball_tracking_queue_display()
        texts = _get_list_texts(config_window.ball_tracking_queue_list)
        assert len(texts) == 1
        assert "2024.01.15-09.00.00" in texts[0]

    def test_youtube_in_progress_only(self, config_window):
        """YouTube shows only in-progress item when queue is empty."""
        data = {
            "queue": [],
            "in_progress": {
                "task_type": "youtube_upload",
                "group_dir": "/storage/2024.01.15-09.00.00",
            },
        }
        config_window._read_json_file = MagicMock(return_value=data)
        config_window.refresh_youtube_upload_display()
        texts = _get_list_texts(config_window.youtube_upload_list)
        assert len(texts) == 1
        assert texts[0] == "[Uploading] 2024.01.15-09.00.00"


# ===========================================================================
# 7. Error state tests
# ===========================================================================


class TestErrorState:
    """Verify each queue tab handles _read_json_file returning 'error'."""

    def test_download_queue_error(self, config_window):
        config_window._read_json_file = MagicMock(return_value="error")
        config_window.refresh_download_queue_display()
        texts = _get_list_texts(config_window.download_queue_list)
        assert texts == ["Error reading download queue."]

    def test_processing_queue_error(self, config_window):
        config_window._read_json_file = MagicMock(return_value="error")
        config_window.refresh_processing_queue_display()
        texts = _get_list_texts(config_window.processing_queue_list)
        assert texts == ["Error reading processing queue state."]

    def test_autocam_queue_error(self, config_window):
        config_window._read_json_file = MagicMock(return_value="error")
        config_window.refresh_ball_tracking_queue_display()
        texts = _get_list_texts(config_window.ball_tracking_queue_list)
        assert texts == ["Error reading ball-tracking queue."]

    def test_youtube_upload_error(self, config_window):
        config_window._read_json_file = MagicMock(return_value="error")
        config_window.refresh_youtube_upload_display()
        texts = _get_list_texts(config_window.youtube_upload_list)
        assert texts == ["Error reading upload queue."]


# ===========================================================================
# 8. Refresh cycle tests
# ===========================================================================


class TestRefreshCycle:
    """Verify display updates correctly across consecutive refresh calls."""

    def test_download_queue_data_changes(self, config_window):
        """Refreshing with new data replaces old display."""
        data_v1 = {
            "queue": [
                {
                    "priority": 2,
                    "seq": 0,
                    "task_type": "recording_file",
                    "file_path": "/storage/group-a/video1.dav",
                    "start_time": "2024-01-15T09:00:00",
                    "end_time": "2024-01-15T09:30:00",
                    "status": "pending",
                    "metadata": {},
                    "skip": False,
                },
            ],
            "in_progress": None,
        }
        config_window._read_json_file = MagicMock(return_value=data_v1)
        config_window.refresh_download_queue_display()
        assert config_window.download_queue_list.count() == 1

        # Second refresh: queue drained
        data_v2 = {"queue": [], "in_progress": None}
        config_window._read_json_file = MagicMock(return_value=data_v2)
        config_window.refresh_download_queue_display()
        texts = _get_list_texts(config_window.download_queue_list)
        assert texts == ["No downloads queued."]

    def test_processing_queue_item_moves_to_in_progress(self, config_window):
        """Item transitions from queued to in-progress between refreshes."""
        data_v1 = {
            "queue": [
                {
                    "priority": 2,
                    "seq": 0,
                    "task_type": "combine",
                    "group_dir": "/storage/group-a",
                },
                {
                    "priority": 2,
                    "seq": 1,
                    "task_type": "combine",
                    "group_dir": "/storage/group-b",
                },
            ],
            "in_progress": None,
        }
        config_window._read_json_file = MagicMock(return_value=data_v1)
        config_window.refresh_processing_queue_display()
        assert config_window.processing_queue_list.count() == 2

        # Second refresh: 1 moved to in-progress
        data_v2 = {
            "queue": [
                {
                    "priority": 2,
                    "seq": 1,
                    "task_type": "combine",
                    "group_dir": "/storage/group-b",
                },
            ],
            "in_progress": {"task_type": "combine", "group_dir": "/storage/group-a"},
        }
        config_window._read_json_file = MagicMock(return_value=data_v2)
        config_window.refresh_processing_queue_display()
        assert config_window.processing_queue_list.count() == 2

    def test_youtube_upload_completes_between_refreshes(self, config_window):
        """Upload finishes between refreshes, list goes from items to empty."""
        data_v1 = {
            "queue": [],
            "in_progress": {
                "task_type": "youtube_upload",
                "group_dir": "/storage/group-a",
            },
        }
        config_window._read_json_file = MagicMock(return_value=data_v1)
        config_window.refresh_youtube_upload_display()
        texts = _get_list_texts(config_window.youtube_upload_list)
        assert len(texts) == 1
        assert "[Uploading]" in texts[0]

        # Second refresh: upload complete
        data_v2 = {"queue": [], "in_progress": None}
        config_window._read_json_file = MagicMock(return_value=data_v2)
        config_window.refresh_youtube_upload_display()
        texts = _get_list_texts(config_window.youtube_upload_list)
        assert texts == ["No uploads queued."]


# ===========================================================================
# 9. Pipeline progression test
# ===========================================================================


class TestPipelineProgression:
    """Simulate a video's journey through the pipeline stages via tray display."""

    def test_full_pipeline_progression(self, config_window):
        """Simulate: download queued -> processing -> upload -> done."""
        group = "2024.01.15-09.00.00"

        # --- Stage 1: File appears in download queue ---
        dl_data = {
            "queue": [
                {
                    "priority": 2,
                    "seq": 0,
                    "task_type": "recording_file",
                    "file_path": f"/storage/{group}/video1.dav",
                    "start_time": "2024-01-15T09:00:00",
                    "end_time": "2024-01-15T09:30:00",
                    "status": "pending",
                    "metadata": {},
                    "skip": False,
                },
            ],
            "in_progress": None,
        }
        proc_empty = {"queue": [], "in_progress": None}
        upload_empty = {"queue": [], "in_progress": None}

        def mock_stage1(path, **kw):
            p = str(path)
            if "download" in p:
                return dl_data
            if "video" in p:
                return proc_empty
            if "upload" in p:
                return upload_empty
            return None

        config_window._read_json_file = mock_stage1
        config_window.refresh_download_queue_display()
        config_window.refresh_processing_queue_display()
        config_window.refresh_youtube_upload_display()

        assert config_window.download_queue_list.count() == 1
        assert _get_list_texts(config_window.processing_queue_list) == [
            "No processing tasks queued."
        ]
        assert _get_list_texts(config_window.youtube_upload_list) == [
            "No uploads queued."
        ]

        # --- Stage 2: Download done, combine queued ---
        dl_empty = {"queue": [], "in_progress": None}
        proc_data = {
            "queue": [
                {
                    "priority": 2,
                    "seq": 0,
                    "task_type": "combine",
                    "group_dir": f"/storage/{group}",
                },
            ],
            "in_progress": None,
        }

        def mock_stage2(path, **kw):
            p = str(path)
            if "download" in p:
                return dl_empty
            if "video" in p:
                return proc_data
            if "upload" in p:
                return upload_empty
            return None

        config_window._read_json_file = mock_stage2
        config_window.refresh_download_queue_display()
        config_window.refresh_processing_queue_display()
        config_window.refresh_youtube_upload_display()

        assert _get_list_texts(config_window.download_queue_list) == [
            "No downloads queued."
        ]
        assert config_window.processing_queue_list.count() == 1
        assert _get_list_texts(config_window.youtube_upload_list) == [
            "No uploads queued."
        ]

        # --- Stage 3: Processing done, upload queued ---
        upload_data = {
            "queue": [
                {
                    "priority": 2,
                    "seq": 0,
                    "task_type": "youtube_upload",
                    "group_dir": f"/storage/{group}",
                },
            ],
            "in_progress": None,
        }

        def mock_stage3(path, **kw):
            p = str(path)
            if "download" in p:
                return dl_empty
            if "video" in p:
                return proc_empty
            if "upload" in p:
                return upload_data
            return None

        config_window._read_json_file = mock_stage3
        config_window.refresh_download_queue_display()
        config_window.refresh_processing_queue_display()
        config_window.refresh_youtube_upload_display()

        assert _get_list_texts(config_window.download_queue_list) == [
            "No downloads queued."
        ]
        assert _get_list_texts(config_window.processing_queue_list) == [
            "No processing tasks queued."
        ]
        upload_texts = _get_list_texts(config_window.youtube_upload_list)
        assert len(upload_texts) == 1
        assert f"[Pending] {group}" in upload_texts[0]

        # --- Stage 4: Upload done, all empty ---
        def mock_stage4(path, **kw):
            p = str(path)
            if "download" in p:
                return dl_empty
            if "video" in p:
                return proc_empty
            if "upload" in p:
                return upload_empty
            return None

        config_window._read_json_file = mock_stage4
        config_window.refresh_download_queue_display()
        config_window.refresh_processing_queue_display()
        config_window.refresh_youtube_upload_display()

        assert _get_list_texts(config_window.download_queue_list) == [
            "No downloads queued."
        ]
        assert _get_list_texts(config_window.processing_queue_list) == [
            "No processing tasks queued."
        ]
        assert _get_list_texts(config_window.youtube_upload_list) == [
            "No uploads queued."
        ]


# ===========================================================================
# 10. Cleanup tab variations
# ===========================================================================


class TestCleanupVariations:
    """Test cleanup tab edge cases."""

    def test_cleanup_deletion_not_supported(self, config_window):
        """When deletion_supported=False, shows info message and button stays disabled."""
        storage_path = config_window.config.storage.path
        state = {
            "deletion_supported": False,
            "files": [
                {
                    "path": "/camera/home/rec1.mp4",
                    "startTime": "2024-01-15 09:00:00",
                    "endTime": "2024-01-15 10:00:00",
                    "size": 1048576,
                },
            ],
        }
        cleanup_path = Path(storage_path) / "home_cleanup_state.json"
        cleanup_path.write_text(json.dumps(state))

        with patch("video_grouper.tray.config_ui.os.path.exists", return_value=True):
            config_window.refresh_cleanup_display()

        texts = _get_list_texts(config_window.cleanup_list)
        assert any("does not support remote file deletion" in t for t in texts)
        assert not config_window.cleanup_delete_btn.isEnabled()

    def test_cleanup_deletion_approved(self, config_window):
        """When deletion is already approved, shows waiting message."""
        storage_path = config_window.config.storage.path
        state = {
            "deletion_supported": True,
            "approved": True,
            "files": [
                {
                    "path": "/camera/home/rec1.mp4",
                    "startTime": "2024-01-15 09:00:00",
                    "endTime": "2024-01-15 10:00:00",
                    "size": 1048576,
                },
            ],
        }
        cleanup_path = Path(storage_path) / "home_cleanup_state.json"
        cleanup_path.write_text(json.dumps(state))

        with patch("video_grouper.tray.config_ui.os.path.exists", return_value=True):
            config_window.refresh_cleanup_display()

        texts = _get_list_texts(config_window.cleanup_list)
        assert any("Deletion approved" in t for t in texts)

    def test_cleanup_deletion_supported_enables_button(self, config_window):
        """When deletion_supported=True and not approved, delete button is enabled."""
        storage_path = config_window.config.storage.path
        state = {
            "deletion_supported": True,
            "files": [
                {
                    "path": "/camera/home/rec1.mp4",
                    "startTime": "2024-01-15 09:00:00",
                    "endTime": "2024-01-15 10:00:00",
                    "size": 1048576,
                },
            ],
        }
        cleanup_path = Path(storage_path) / "home_cleanup_state.json"
        cleanup_path.write_text(json.dumps(state))

        with patch("video_grouper.tray.config_ui.os.path.exists", return_value=True):
            config_window.refresh_cleanup_display()

        assert config_window.cleanup_delete_btn.isEnabled()


# ===========================================================================
# 11. Connection history edge cases
# ===========================================================================


class TestConnectionHistoryEdgeCases:
    """Test connection history with multiple cameras and edge cases."""

    def test_multiple_cameras(self, config_window):
        """Multiple cameras each with events display separately."""
        storage_path = config_window.config.storage.path
        state = {
            "cam1": {
                "connection_events": [
                    {
                        "event_datetime": "2024-01-15T09:00:00+00:00",
                        "event_type": "connected",
                        "message": "",
                    },
                    {
                        "event_datetime": "2024-01-15T10:00:00+00:00",
                        "event_type": "disconnected",
                        "message": "signal lost",
                    },
                ]
            },
            "cam2": {
                "connection_events": [
                    {
                        "event_datetime": "2024-01-15T09:30:00+00:00",
                        "event_type": "connected",
                        "message": "",
                    },
                    {
                        "event_datetime": "2024-01-15T11:00:00+00:00",
                        "event_type": "disconnected",
                        "message": "timeout",
                    },
                ]
            },
        }
        camera_state_path = Path(storage_path) / "camera_state.json"
        camera_state_path.write_text(json.dumps(state))

        real_exists = Path.exists

        def _exists(p):
            return real_exists(p)

        with patch.object(Path, "exists", _exists):
            config_window.refresh_connection_events_display()

        texts = _get_list_texts(config_window.connection_events_list)
        assert len(texts) == 2
        cam_names = [t.split("]")[0].lstrip("[") for t in texts]
        assert "cam1" in cam_names
        assert "cam2" in cam_names

    def test_still_connected_camera(self, config_window):
        """Camera with connected event but no disconnect shows 'Still connected'."""
        storage_path = config_window.config.storage.path
        state = {
            "cam1": {
                "connection_events": [
                    {
                        "event_datetime": "2024-01-15T09:00:00+00:00",
                        "event_type": "connected",
                        "message": "",
                    },
                ]
            },
        }
        camera_state_path = Path(storage_path) / "camera_state.json"
        camera_state_path.write_text(json.dumps(state))

        real_exists = Path.exists

        def _exists(p):
            return real_exists(p)

        with patch.object(Path, "exists", _exists):
            config_window.refresh_connection_events_display()

        texts = _get_list_texts(config_window.connection_events_list)
        assert len(texts) == 1
        assert "(Still connected)" in texts[0]

    def test_camera_with_non_dict_state(self, config_window):
        """Non-dict camera state entry is gracefully skipped."""
        storage_path = config_window.config.storage.path
        state = {
            "version": "1.0",
            "cam1": {
                "connection_events": [
                    {
                        "event_datetime": "2024-01-15T09:00:00+00:00",
                        "event_type": "connected",
                        "message": "",
                    },
                    {
                        "event_datetime": "2024-01-15T10:00:00+00:00",
                        "event_type": "disconnected",
                        "message": "",
                    },
                ]
            },
        }
        camera_state_path = Path(storage_path) / "camera_state.json"
        camera_state_path.write_text(json.dumps(state))

        real_exists = Path.exists

        def _exists(p):
            return real_exists(p)

        with patch.object(Path, "exists", _exists):
            config_window.refresh_connection_events_display()

        texts = _get_list_texts(config_window.connection_events_list)
        assert len(texts) == 1
        assert "[cam1]" in texts[0]


# ===========================================================================
# 12. refresh_all_displays integration
# ===========================================================================


class TestRefreshAllDisplays:
    """Verify refresh_all_displays calls all sub-refresh methods."""

    def test_refresh_all_calls_all_sub_refreshes(self, config_window):
        """refresh_all_displays must invoke every individual refresh method."""
        with (
            patch.object(config_window, "refresh_download_queue_display") as mock_dl,
            patch.object(
                config_window, "refresh_processing_queue_display"
            ) as mock_proc,
            patch.object(
                config_window, "refresh_ball_tracking_queue_display"
            ) as mock_auto,
            patch.object(config_window, "refresh_youtube_upload_display") as mock_yt,
            patch.object(config_window, "refresh_skipped_files_display") as mock_skip,
            patch.object(config_window, "refresh_match_info_display") as mock_match,
            patch.object(
                config_window, "refresh_connection_events_display"
            ) as mock_conn,
            patch.object(config_window, "refresh_cleanup_display") as mock_clean,
        ):
            config_window.refresh_all_displays()

            mock_dl.assert_called_once()
            mock_proc.assert_called_once()
            mock_auto.assert_called_once()
            mock_yt.assert_called_once()
            mock_skip.assert_called_once()
            mock_match.assert_called_once()
            mock_conn.assert_called_once()
            mock_clean.assert_called_once()

    def test_refresh_queue_displays_calls_queue_refreshes(self, config_window):
        """refresh_queue_displays must invoke the four queue refresh methods."""
        with (
            patch.object(config_window, "refresh_download_queue_display") as mock_dl,
            patch.object(
                config_window, "refresh_processing_queue_display"
            ) as mock_proc,
            patch.object(
                config_window, "refresh_ball_tracking_queue_display"
            ) as mock_auto,
            patch.object(config_window, "refresh_youtube_upload_display") as mock_yt,
        ):
            config_window.refresh_queue_displays()

            mock_dl.assert_called_once()
            mock_proc.assert_called_once()
            mock_auto.assert_called_once()
            mock_yt.assert_called_once()
