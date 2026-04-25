import sys
from unittest.mock import Mock, patch, AsyncMock, MagicMock

# pytest-qt (loaded at plugin discovery) imports PyQt6, whose DLLs collide
# with onnxruntime-gpu's DLL load on Windows. Stub onnxruntime before any
# test module tries to import it — unit tests already mock InferenceSession.
sys.modules.setdefault("onnxruntime", MagicMock())

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
import asyncio  # noqa: E402
import tempfile  # noqa: E402
import configparser  # noqa: E402

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture(autouse=True)
def cleanup_after_test():
    """Cleanup fixture that runs after every test."""
    yield
    # Force cleanup of any remaining resources
    import gc

    gc.collect()

    # Close any remaining loggers
    try:
        from video_grouper.utils.logger import close_loggers

        close_loggers()
    except Exception:
        pass


def _make_mock_av_container(duration_us=100_000_000):
    """Create a mock av container that behaves like av.open(...)."""
    container = MagicMock()
    # Duration in microseconds (100s default)
    container.duration = duration_us
    # Streams
    video_stream = MagicMock()
    video_stream.type = "video"
    video_stream.duration = 100
    video_stream.time_base = 1
    video_stream.index = 0
    video_stream.rate = 30
    video_stream.average_rate = 30
    video_stream.width = 1920
    video_stream.height = 1080
    audio_stream = MagicMock()
    audio_stream.type = "audio"
    audio_stream.rate = 44100
    audio_stream.index = 1
    container.streams = MagicMock()
    container.streams.video = [video_stream]
    container.streams.audio = [audio_stream]
    container.streams.__iter__ = lambda self: iter([video_stream, audio_stream])
    container.demux.return_value = iter([])
    container.decode.return_value = iter([])
    return container


@pytest.fixture(autouse=True)
def mock_ffmpeg():
    """Mock PyAV (av.open) for all tests."""
    container = _make_mock_av_container()
    mock_open = MagicMock(return_value=container)
    container.__enter__ = MagicMock(return_value=container)
    container.__exit__ = MagicMock(return_value=False)
    with patch("av.open", mock_open):
        yield mock_open


@pytest.fixture(autouse=True)
def mock_file_system():
    """Mock file system operations for all tests."""
    with (
        patch("os.path.exists") as mock_exists,
        patch("os.path.getsize") as mock_getsize,
        patch("os.makedirs") as mock_makedirs,
        patch("os.access") as mock_access,
    ):
        mock_exists.return_value = True
        mock_getsize.return_value = 1024 * 1024  # 1MB
        mock_makedirs.return_value = None
        mock_access.return_value = True

        yield {
            "exists": mock_exists,
            "getsize": mock_getsize,
            "makedirs": mock_makedirs,
            "access": mock_access,
        }


@pytest.fixture(autouse=True)
def mock_httpx():
    """Mock httpx client for all tests."""
    with patch("httpx.AsyncClient") as mock:
        client = AsyncMock()
        response = Mock()
        response.status_code = 200
        response.text = "object=123"
        response.headers = {"content-length": "1048576"}
        client.__aenter__.return_value.get.return_value = response
        mock.return_value = client
        yield mock


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def mock_config(temp_storage):
    """Create a mock configuration object."""
    config = configparser.ConfigParser()
    config.add_section("STORAGE")
    config.set("STORAGE", "path", temp_storage)
    config.add_section("APP")
    config.set("APP", "check_interval_seconds", "1")
    config.add_section("CAMERA")
    config.set("CAMERA", "type", "dahua")
    config.set("CAMERA", "device_ip", "192.168.1.100")
    config.set("CAMERA", "username", "admin")
    config.set("CAMERA", "password", "password")
    config.add_section("YOUTUBE")
    config.set("YOUTUBE", "enabled", "true")
    config.add_section("PLAYMETRICS")
    config.set("PLAYMETRICS", "enabled", "true")
    config.set("PLAYMETRICS", "username", "test@example.com")
    config.set("PLAYMETRICS", "password", "testpassword")
    config.set("PLAYMETRICS", "team_id", "123456")
    config.set("PLAYMETRICS", "team_name", "Test Team")
    return config


@pytest_asyncio.fixture(scope="function")
async def cleanup_asyncio_tasks():
    """Automatically cancel all pending asyncio tasks at the end of each test."""
    # Run the test
    yield

    # Get all pending tasks
    try:
        # Check if we're in an event loop
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop running, nothing to clean up
            return

        current_task = asyncio.current_task()
        all_tasks = asyncio.all_tasks(loop)

        # Filter out the current task to avoid cancelling ourselves
        pending_tasks = [
            task for task in all_tasks if task != current_task and not task.done()
        ]

        if pending_tasks:
            # Cancel all pending tasks
            for task in pending_tasks:
                if not task.done():
                    task.cancel()

            # Wait for all tasks to complete (with cancellation)
            try:
                await asyncio.gather(*pending_tasks, return_exceptions=True)
            except Exception:
                # Ignore exceptions during cleanup
                pass

    except (RuntimeError, AttributeError):
        # Event loop might already be closed or no tasks available
        pass
