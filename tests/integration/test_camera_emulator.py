"""Integration tests for the camera emulator.

Starts the emulator in-process and exercises real DahuaCamera/ReolinkCamera
clients against it. No Docker required.
"""

import os
import sys

import pytest
import pytest_asyncio
from aiohttp import web
from datetime import datetime, timedelta

# Add camera_emulator to path so its modules can import each other
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "camera_emulator"))

from video_grouper.cameras.dahua import DahuaCamera
from video_grouper.cameras.reolink import ReolinkCamera
from video_grouper.utils.config import CameraConfig

from tests.camera_emulator.server import create_app


@pytest.fixture(autouse=True)
def mock_httpx():
    """Override the global mock_httpx fixture to allow real HTTP calls."""
    yield None


@pytest.fixture(autouse=True)
def mock_file_system():
    """Override the global mock_file_system fixture to allow real file I/O."""
    yield None


@pytest.fixture(autouse=True)
def mock_ffmpeg():
    """Override the global mock_ffmpeg fixture -- not needed here."""
    yield None


@pytest.fixture
def clips_dir():
    """Find the test clips directory."""
    clips = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "e2e", "test_clips")
    )
    if not os.path.isdir(clips):
        pytest.skip("Test clips not available")
    mp4s = [f for f in os.listdir(clips) if f.endswith(".mp4")]
    if not mp4s:
        pytest.skip("No MP4 test clips found")
    return clips


@pytest_asyncio.fixture
async def dahua_server(clips_dir):
    """Start an in-process Dahua emulator server."""
    app = create_app(
        camera_type="dahua",
        username="admin",
        password="testpass",
        clips_dir=clips_dir,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    # Get the actual bound port
    port = site._server.sockets[0].getsockname()[1]
    yield f"127.0.0.1:{port}"
    await runner.cleanup()


@pytest_asyncio.fixture
async def reolink_server(clips_dir):
    """Start an in-process ReoLink emulator server."""
    app = create_app(
        camera_type="reolink",
        username="admin",
        password="testpass",
        clips_dir=clips_dir,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"127.0.0.1:{port}"
    await runner.cleanup()


# ── Dahua emulator tests ──────────────────────────────────────────────


@pytest.mark.integration
class TestDahuaEmulator:
    @pytest.mark.asyncio
    async def test_check_availability(self, dahua_server, tmp_path):
        config = CameraConfig(
            name="default",
            type="dahua",
            device_ip=dahua_server,
            username="admin",
            password="testpass",
        )
        camera = DahuaCamera(config=config, storage_path=str(tmp_path))
        result = await camera.check_availability()
        assert result is True
        assert camera.is_connected is True

    @pytest.mark.asyncio
    async def test_check_availability_wrong_password(self, dahua_server, tmp_path):
        config = CameraConfig(
            name="default",
            type="dahua",
            device_ip=dahua_server,
            username="admin",
            password="wrongpass",
        )
        camera = DahuaCamera(config=config, storage_path=str(tmp_path))
        result = await camera.check_availability()
        assert result is False

    @pytest.mark.asyncio
    async def test_get_file_list(self, dahua_server, tmp_path):
        config = CameraConfig(
            name="default",
            type="dahua",
            device_ip=dahua_server,
            username="admin",
            password="testpass",
        )
        camera = DahuaCamera(config=config, storage_path=str(tmp_path))
        start = datetime.utcnow() - timedelta(hours=24)
        end = datetime.utcnow()
        files = await camera.get_file_list(start, end)
        assert len(files) == 6
        for f in files:
            assert "path" in f
            assert "startTime" in f
            assert "endTime" in f

    @pytest.mark.asyncio
    async def test_get_file_size(self, dahua_server, tmp_path):
        config = CameraConfig(
            name="default",
            type="dahua",
            device_ip=dahua_server,
            username="admin",
            password="testpass",
        )
        camera = DahuaCamera(config=config, storage_path=str(tmp_path))
        start = datetime.utcnow() - timedelta(hours=24)
        end = datetime.utcnow()
        files = await camera.get_file_list(start, end)
        assert len(files) > 0

        size = await camera.get_file_size(files[0]["path"])
        assert size > 0

    @pytest.mark.asyncio
    async def test_download_file(self, dahua_server, tmp_path):
        config = CameraConfig(
            name="default",
            type="dahua",
            device_ip=dahua_server,
            username="admin",
            password="testpass",
        )
        camera = DahuaCamera(config=config, storage_path=str(tmp_path))
        start = datetime.utcnow() - timedelta(hours=24)
        end = datetime.utcnow()
        files = await camera.get_file_list(start, end)
        assert len(files) > 0

        local_path = os.path.join(str(tmp_path), "downloaded.dav")
        result = await camera.download_file(files[0]["path"], local_path)
        assert result is True
        assert os.path.exists(local_path)
        assert os.path.getsize(local_path) > 0

    @pytest.mark.asyncio
    async def test_get_device_info(self, dahua_server, tmp_path):
        config = CameraConfig(
            name="default",
            type="dahua",
            device_ip=dahua_server,
            username="admin",
            password="testpass",
        )
        camera = DahuaCamera(config=config, storage_path=str(tmp_path))
        info = await camera.get_device_info()
        assert info["manufacturer"] == "Dahua"
        assert info["device_name"] == "CameraEmulator"


# ── ReoLink emulator tests ───────────────────────────────────────────


@pytest.mark.integration
class TestReolinkEmulator:
    @pytest.mark.asyncio
    async def test_check_availability(self, reolink_server, tmp_path):
        config = CameraConfig(
            name="default",
            type="reolink",
            device_ip=reolink_server,
            username="admin",
            password="testpass",
            channel=0,
        )
        camera = ReolinkCamera(config=config, storage_path=str(tmp_path))
        result = await camera.check_availability()
        assert result is True
        assert camera.is_connected is True

    @pytest.mark.asyncio
    async def test_check_availability_wrong_password(self, reolink_server, tmp_path):
        config = CameraConfig(
            name="default",
            type="reolink",
            device_ip=reolink_server,
            username="admin",
            password="wrongpass",
            channel=0,
        )
        camera = ReolinkCamera(config=config, storage_path=str(tmp_path))
        result = await camera.check_availability()
        assert result is False

    @pytest.mark.asyncio
    async def test_get_file_list(self, reolink_server, tmp_path):
        config = CameraConfig(
            name="default",
            type="reolink",
            device_ip=reolink_server,
            username="admin",
            password="testpass",
            channel=0,
        )
        camera = ReolinkCamera(config=config, storage_path=str(tmp_path))
        start = datetime.utcnow() - timedelta(hours=24)
        end = datetime.utcnow()
        files = await camera.get_file_list(start, end)
        assert len(files) == 6
        for f in files:
            assert "path" in f
            assert "startTime" in f
            assert "endTime" in f

    @pytest.mark.asyncio
    async def test_get_file_size(self, reolink_server, tmp_path):
        config = CameraConfig(
            name="default",
            type="reolink",
            device_ip=reolink_server,
            username="admin",
            password="testpass",
            channel=0,
        )
        camera = ReolinkCamera(config=config, storage_path=str(tmp_path))
        start = datetime.utcnow() - timedelta(hours=24)
        end = datetime.utcnow()
        files = await camera.get_file_list(start, end)
        assert len(files) > 0

        size = await camera.get_file_size(files[0]["path"])
        assert size > 0

    @pytest.mark.asyncio
    async def test_download_file(self, reolink_server, tmp_path):
        config = CameraConfig(
            name="default",
            type="reolink",
            device_ip=reolink_server,
            username="admin",
            password="testpass",
            channel=0,
        )
        camera = ReolinkCamera(config=config, storage_path=str(tmp_path))
        start = datetime.utcnow() - timedelta(hours=24)
        end = datetime.utcnow()
        files = await camera.get_file_list(start, end)
        assert len(files) > 0

        local_path = os.path.join(str(tmp_path), "downloaded.mp4")
        result = await camera.download_file(files[0]["path"], local_path)
        assert result is True
        assert os.path.exists(local_path)
        assert os.path.getsize(local_path) > 0

    @pytest.mark.asyncio
    async def test_get_device_info(self, reolink_server, tmp_path):
        config = CameraConfig(
            name="default",
            type="reolink",
            device_ip=reolink_server,
            username="admin",
            password="testpass",
            channel=0,
        )
        camera = ReolinkCamera(config=config, storage_path=str(tmp_path))
        info = await camera.get_device_info()
        assert info["manufacturer"] == "Reolink"
        assert info["device_name"] == "CameraEmulator"
        assert info["model"] == "RLC-810A"

    @pytest.mark.asyncio
    async def test_get_recording_status(self, reolink_server, tmp_path):
        config = CameraConfig(
            name="default",
            type="reolink",
            device_ip=reolink_server,
            username="admin",
            password="testpass",
            channel=0,
        )
        camera = ReolinkCamera(config=config, storage_path=str(tmp_path))
        status = await camera.get_recording_status()
        assert status is False

    @pytest.mark.asyncio
    async def test_stop_recording(self, reolink_server, tmp_path):
        config = CameraConfig(
            name="default",
            type="reolink",
            device_ip=reolink_server,
            username="admin",
            password="testpass",
            channel=0,
        )
        camera = ReolinkCamera(config=config, storage_path=str(tmp_path))
        result = await camera.stop_recording()
        assert result is True
