"""Tests for the GitHub Releases-based update manager."""

import os
import os.path
import sys
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import httpx
import pytest

from video_grouper.update.update_manager import (
    NetworkError,
    UpdateManager,
    check_and_update,
)

# Save real functions BEFORE conftest's autouse mock_file_system patches them.
# Must be after all imports but at module level so they run before fixtures.
_real_exists = os.path.exists
_real_getsize = os.path.getsize

# Sample GitHub API release response
SAMPLE_RELEASE = {
    "tag_name": "v0.2.0",
    "published_at": "2026-03-22T12:00:00Z",
    "body": "Release notes here",
    "assets": [
        {
            "name": "VideoGrouperService.exe",
            "browser_download_url": "https://github.com/mblakley/soccer-cam/releases/download/v0.2.0/VideoGrouperService.exe",
            "size": 50000000,
        },
        {
            "name": "VideoGrouperTray.exe",
            "browser_download_url": "https://github.com/mblakley/soccer-cam/releases/download/v0.2.0/VideoGrouperTray.exe",
            "size": 40000000,
        },
        {
            "name": "VideoGrouperSetup.exe",
            "browser_download_url": "https://github.com/mblakley/soccer-cam/releases/download/v0.2.0/VideoGrouperSetup.exe",
            "size": 60000000,
        },
    ],
}


def _make_response(status_code=200, json_data=None):
    """Create a mock httpx response."""
    response = Mock()
    response.status_code = status_code
    response.json.return_value = json_data or {}
    response.raise_for_status = Mock()
    if status_code >= 400:
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=Mock(), response=response
        )
    return response


@pytest.fixture
def update_manager(tmp_path):
    with patch(
        "video_grouper.update.update_manager.tempfile.mkdtemp",
        return_value=str(tmp_path),
    ):
        manager = UpdateManager(
            current_version="0.1.0",
            github_repo="mblakley/soccer-cam",
        )
    return manager


@pytest.fixture
def real_filesystem(mock_file_system):
    """Override autouse mock_file_system with real os functions for tests
    that need actual file I/O."""
    mock_file_system["exists"].side_effect = _real_exists
    mock_file_system["getsize"].side_effect = _real_getsize
    return mock_file_system


# --- Version Comparison Tests ---


class TestCheckForUpdates:
    @pytest.mark.asyncio
    async def test_newer_version_available(self, update_manager):
        response = _make_response(200, SAMPLE_RELEASE)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            has_update, info = await update_manager.check_for_updates()

        assert has_update is True
        assert info["version"] == "0.2.0"
        assert info["tag_name"] == "v0.2.0"
        assert len(info["assets"]) == 3

    @pytest.mark.asyncio
    async def test_same_version_no_update(self, update_manager):
        update_manager.current_version = "0.2.0"
        response = _make_response(200, SAMPLE_RELEASE)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            has_update, info = await update_manager.check_for_updates()

        assert has_update is False

    @pytest.mark.asyncio
    async def test_older_remote_version_no_update(self, update_manager):
        update_manager.current_version = "1.0.0"
        response = _make_response(200, SAMPLE_RELEASE)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            has_update, info = await update_manager.check_for_updates()

        assert has_update is False

    @pytest.mark.asyncio
    async def test_strips_v_prefix_from_tag(self, update_manager):
        """Tag 'v0.2.0' should be parsed as version '0.2.0'."""
        response = _make_response(200, SAMPLE_RELEASE)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            has_update, info = await update_manager.check_for_updates()

        assert has_update is True
        assert info["version"] == "0.2.0"

    @pytest.mark.asyncio
    async def test_invalid_version_format(self, update_manager):
        release = {**SAMPLE_RELEASE, "tag_name": "not-a-version"}
        response = _make_response(200, release)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            has_update, info = await update_manager.check_for_updates()

        assert has_update is False
        assert info is None

    @pytest.mark.asyncio
    async def test_no_releases_returns_false(self, update_manager):
        """404 from GitHub means no releases exist."""
        response = _make_response(404)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            has_update, info = await update_manager.check_for_updates()

        assert has_update is False
        assert info is None

    @pytest.mark.asyncio
    async def test_missing_required_assets_skips_update(self, update_manager):
        """Release exists but only has the setup exe, not the service/tray."""
        release = {
            **SAMPLE_RELEASE,
            "assets": [
                {
                    "name": "VideoGrouperSetup.exe",
                    "browser_download_url": "https://example.com/setup.exe",
                    "size": 60000000,
                }
            ],
        }
        response = _make_response(200, release)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            has_update, info = await update_manager.check_for_updates()

        assert has_update is False

    @pytest.mark.asyncio
    async def test_network_error_raises(self, update_manager):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=httpx.RequestError("Connection failed"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(NetworkError):
                await update_manager.check_for_updates()

    @pytest.mark.asyncio
    async def test_rate_limit_returns_false(self, update_manager):
        """HTTP 403 (rate limit) should return False, not raise."""
        response = _make_response(403)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            has_update, info = await update_manager.check_for_updates()

        assert has_update is False


# --- Download Tests ---


class TestDownloadUpdate:
    @pytest.mark.asyncio
    async def test_download_both_exes(self, update_manager, tmp_path, real_filesystem):
        """Successfully download both required executables."""
        version_info = {
            "version": "0.2.0",
            "tag_name": "v0.2.0",
            "assets": SAMPLE_RELEASE["assets"],
        }

        async def fake_download(url, file_path):
            with open(file_path, "wb") as f:
                f.write(b"MZ" + b"\x00" * 100)  # Fake PE header
            return True

        update_manager.download_file = AsyncMock(side_effect=fake_download)
        result = await update_manager.download_update(version_info)

        assert result is True
        assert update_manager.download_file.call_count == 2

        # Verify files exist and are named correctly
        service_path = os.path.join(update_manager.temp_dir, "VideoGrouperService.exe")
        tray_path = os.path.join(update_manager.temp_dir, "VideoGrouperTray.exe")
        assert os.path.exists(service_path)
        assert os.path.exists(tray_path)

    @pytest.mark.asyncio
    async def test_download_fails_if_asset_missing(self, update_manager):
        """Fail if a required asset is not in the release."""
        version_info = {
            "version": "0.2.0",
            "tag_name": "v0.2.0",
            "assets": [
                {
                    "name": "VideoGrouperService.exe",
                    "browser_download_url": "https://example.com/service.exe",
                    "size": 50000000,
                }
            ],
        }

        update_manager.download_file = AsyncMock(return_value=True)
        result = await update_manager.download_update(version_info)

        assert result is False

    @pytest.mark.asyncio
    async def test_download_fails_on_empty_file(
        self, update_manager, tmp_path, real_filesystem
    ):
        """Fail if downloaded file is empty."""
        version_info = {
            "version": "0.2.0",
            "tag_name": "v0.2.0",
            "assets": SAMPLE_RELEASE["assets"],
        }

        async def fake_download_empty(url, file_path):
            open(file_path, "wb").close()  # Write nothing
            return True

        update_manager.download_file = AsyncMock(side_effect=fake_download_empty)
        result = await update_manager.download_update(version_info)

        assert result is False


# --- Install Tests ---


class TestInstallUpdate:
    @pytest.fixture
    def mock_win32svc(self):
        """Provide a mock win32serviceutil module."""
        mock_svc = MagicMock()
        return mock_svc

    def test_install_happy_path(
        self, update_manager, tmp_path, real_filesystem, mock_win32svc
    ):
        """Stop service, copy files, start service."""
        # Create fake downloaded files
        for name in ("VideoGrouperService.exe", "VideoGrouperTray.exe"):
            with open(os.path.join(update_manager.temp_dir, name), "wb") as f:
                f.write(b"MZ" + b"\x00" * 100)

        # Use separate install dir so src != dst
        install_dir = tmp_path / "install"
        install_dir.mkdir()

        with (
            patch.dict(sys.modules, {"win32serviceutil": mock_win32svc}),
            patch("video_grouper.update.update_manager.subprocess.run") as mock_run,
            patch("video_grouper.update.update_manager.sys") as mock_sys,
        ):
            mock_sys.executable = str(install_dir / "VideoGrouperService.exe")
            result = update_manager.install_update()

        assert result is True
        mock_win32svc.StopService.assert_called_once_with("VideoGrouperService")
        mock_win32svc.StartService.assert_called_once_with("VideoGrouperService")
        mock_run.assert_called_once()  # taskkill for tray

    def test_install_rollback_on_start_failure(
        self, update_manager, tmp_path, real_filesystem, mock_win32svc
    ):
        """Restore backups if service fails to start."""
        install_dir = tmp_path / "install"
        install_dir.mkdir()

        # Create existing files to be backed up
        for name in ("VideoGrouperService.exe", "VideoGrouperTray.exe"):
            (install_dir / name).write_bytes(b"OLD")

        # Create new files to install
        for name in ("VideoGrouperService.exe", "VideoGrouperTray.exe"):
            with open(os.path.join(update_manager.temp_dir, name), "wb") as f:
                f.write(b"NEW")

        with (
            patch.dict(sys.modules, {"win32serviceutil": mock_win32svc}),
            patch("video_grouper.update.update_manager.subprocess.run"),
            patch("video_grouper.update.update_manager.sys") as mock_sys,
        ):
            mock_sys.executable = str(install_dir / "VideoGrouperService.exe")
            mock_win32svc.StartService.side_effect = Exception("Service start failed")
            result = update_manager.install_update()

        assert result is False
        # Backups should have been restored
        assert (install_dir / "VideoGrouperService.exe").read_bytes() == b"OLD"
        assert (install_dir / "VideoGrouperTray.exe").read_bytes() == b"OLD"

    def test_install_kills_tray_process(
        self, update_manager, tmp_path, real_filesystem, mock_win32svc
    ):
        """Verify taskkill is called for the tray exe."""
        for name in ("VideoGrouperService.exe", "VideoGrouperTray.exe"):
            with open(os.path.join(update_manager.temp_dir, name), "wb") as f:
                f.write(b"MZ" + b"\x00" * 100)

        install_dir = tmp_path / "install"
        install_dir.mkdir()

        with (
            patch.dict(sys.modules, {"win32serviceutil": mock_win32svc}),
            patch("video_grouper.update.update_manager.subprocess.run") as mock_run,
            patch("video_grouper.update.update_manager.sys") as mock_sys,
        ):
            mock_sys.executable = str(install_dir / "VideoGrouperService.exe")
            update_manager.install_update()

        mock_run.assert_called_once_with(
            ["taskkill", "/F", "/IM", "VideoGrouperTray.exe"],
            capture_output=True,
        )


# --- Convenience Function Tests ---


class TestCheckAndUpdate:
    @pytest.mark.asyncio
    async def test_no_update_returns_false(self):
        with patch("video_grouper.update.update_manager.UpdateManager") as MockManager:
            instance = MockManager.return_value
            instance.check_for_updates = AsyncMock(return_value=(False, None))
            instance.cleanup = MagicMock()

            result = await check_and_update("0.2.0", "mblakley/soccer-cam")

        assert result is False

    @pytest.mark.asyncio
    async def test_full_update_flow(self):
        version_info = {
            "version": "0.3.0",
            "tag_name": "v0.3.0",
            "assets": SAMPLE_RELEASE["assets"],
        }

        with patch("video_grouper.update.update_manager.UpdateManager") as MockManager:
            instance = MockManager.return_value
            instance.check_for_updates = AsyncMock(return_value=(True, version_info))
            instance.download_update = AsyncMock(return_value=True)
            instance.install_update = MagicMock(return_value=True)
            instance.cleanup = MagicMock()

            result = await check_and_update("0.1.0", "mblakley/soccer-cam")

        assert result is True
        instance.download_update.assert_awaited_once_with(version_info)
        instance.install_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_network_error_returns_false(self):
        with patch("video_grouper.update.update_manager.UpdateManager") as MockManager:
            instance = MockManager.return_value
            instance.check_for_updates = AsyncMock(side_effect=NetworkError("offline"))
            instance.cleanup = MagicMock()

            result = await check_and_update("0.1.0", "mblakley/soccer-cam")

        assert result is False
