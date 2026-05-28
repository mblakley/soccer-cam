"""Tests for the GitHub Releases-based update manager."""

import os
import os.path
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from video_grouper.update.update_manager import (
    NetworkError,
    UpdateManager,
    resolve_api_url,
)

# Save real functions BEFORE conftest's autouse mock_file_system patches them.
# Must be after all imports but at module level so they run before fixtures.
_real_exists = os.path.exists
_real_getsize = os.path.getsize

# Sample GitHub API release response. Only VideoGrouperSetup.exe is
# required -- the installer ships service + tray + _internal/ as one
# atomic payload. CI publishes only this single artifact.
SAMPLE_RELEASE = {
    "tag_name": "v0.2.0",
    "published_at": "2026-03-22T12:00:00Z",
    "body": "Release notes here",
    "assets": [
        {
            "name": "VideoGrouperSetup.exe",
            "browser_download_url": "https://github.com/mblakley/soccer-cam/releases/download/v0.2.0/VideoGrouperSetup.exe",
            "size": 90000000,
            "digest": "sha256:abcdef0123456789",
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
        assert len(info["assets"]) == 1
        assert info["assets"][0]["name"] == "VideoGrouperSetup.exe"

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
    async def test_missing_setup_exe_skips_update(self, update_manager):
        """Release exists but doesn't have VideoGrouperSetup.exe yet.

        Happens during a release-in-progress: tag landed, CI hasn't
        finished publishing the installer artifact. We don't want to
        try upgrading to a release that has no installer.
        """
        release = {
            **SAMPLE_RELEASE,
            "assets": [
                {
                    "name": "VideoGrouperService.exe",
                    "browser_download_url": "https://example.com/service.exe",
                    "size": 50000000,
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
    async def test_download_setup_exe(self, update_manager, tmp_path, real_filesystem):
        """Successfully download VideoGrouperSetup.exe."""
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
        assert update_manager.download_file.call_count == 1

        installer_path = os.path.join(update_manager.temp_dir, "VideoGrouperSetup.exe")
        assert os.path.exists(installer_path)

    @pytest.mark.asyncio
    async def test_download_fails_if_setup_missing(self, update_manager):
        """Fail when the release doesn't include VideoGrouperSetup.exe."""
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


# --- Digest Verification Tests ---


class TestVerifyDigest:
    def test_matching_digest_passes(self, update_manager, tmp_path):
        f = tmp_path / "setup.exe"
        f.write_bytes(b"hello world")
        expected = update_manager.compute_sha256(str(f))
        assert update_manager.verify_digest(str(f), expected) is True

    def test_mismatched_digest_fails(self, update_manager, tmp_path):
        f = tmp_path / "setup.exe"
        f.write_bytes(b"hello world")
        assert update_manager.verify_digest(str(f), "sha256:wrong") is False

    def test_missing_expected_passes_with_warning(self, update_manager, tmp_path):
        """Older GitHub releases don't include the digest field. We
        accept the download in that case rather than refusing every
        legacy release."""
        f = tmp_path / "setup.exe"
        f.write_bytes(b"hello world")
        assert update_manager.verify_digest(str(f), None) is True
        assert update_manager.verify_digest(str(f), "") is True

    def test_compute_sha256_returns_prefixed_hex(self, update_manager, tmp_path):
        f = tmp_path / "setup.exe"
        f.write_bytes(b"hello")
        digest = update_manager.compute_sha256(str(f))
        assert digest.startswith("sha256:")
        assert (
            digest
            == "sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        )


# --- Spawn Installer Tests ---


class TestSpawnInstaller:
    def test_spawn_invokes_setup_with_silent_flag(self, update_manager, tmp_path):
        installer = tmp_path / "VideoGrouperSetup.exe"
        installer.write_bytes(b"fake installer")
        update_manager.temp_dir = str(tmp_path)

        with patch(
            "video_grouper.update.update_manager.subprocess.Popen"
        ) as mock_popen:
            mock_popen.return_value.pid = 4321
            pid = update_manager.spawn_installer()

        assert pid == 4321
        args, kwargs = mock_popen.call_args
        cmd = args[0]
        assert cmd[0] == str(installer)
        assert "/S" in cmd

    def test_spawn_missing_installer_raises(self, update_manager, tmp_path):
        update_manager.temp_dir = str(tmp_path / "nonexistent")
        with pytest.raises(Exception):  # noqa: B017
            update_manager.spawn_installer()

    def test_spawn_uses_detached_flags_on_windows(self, update_manager, tmp_path):
        installer = tmp_path / "VideoGrouperSetup.exe"
        installer.write_bytes(b"x")
        update_manager.temp_dir = str(tmp_path)

        with (
            patch("video_grouper.update.update_manager.subprocess.Popen") as mock_popen,
            patch("video_grouper.update.update_manager.sys") as mock_sys,
        ):
            mock_sys.platform = "win32"
            mock_popen.return_value.pid = 1
            update_manager.spawn_installer()

        _, kwargs = mock_popen.call_args
        # 0x208 = DETACHED_PROCESS (0x08) | CREATE_NEW_PROCESS_GROUP (0x200)
        assert kwargs["creationflags"] == 0x208
        assert kwargs["close_fds"] is True


# --- URL Resolution Tests ---


class TestResolveApiUrl:
    def test_default_url(self, monkeypatch):
        monkeypatch.delenv("SOCCER_CAM_UPDATE_API_URL", raising=False)
        url, source = resolve_api_url("mblakley/soccer-cam")
        assert url == "https://api.github.com/repos/mblakley/soccer-cam/releases/latest"
        assert source == "default"

    def test_config_override(self, monkeypatch):
        monkeypatch.delenv("SOCCER_CAM_UPDATE_API_URL", raising=False)
        url, source = resolve_api_url(
            "mblakley/soccer-cam", override="https://example.com/api/releases/latest"
        )
        assert url == "https://example.com/api/releases/latest"
        assert source == "config"

    def test_env_wins_over_config(self, monkeypatch):
        monkeypatch.setenv("SOCCER_CAM_UPDATE_API_URL", "http://127.0.0.1:9876/r/l")
        url, source = resolve_api_url(
            "mblakley/soccer-cam", override="https://example.com/should-be-ignored"
        )
        assert url == "http://127.0.0.1:9876/r/l"
        assert source == "env"
