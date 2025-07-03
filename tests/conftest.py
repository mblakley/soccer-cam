import pytest
import pytest_asyncio
import asyncio
from unittest.mock import Mock, patch, AsyncMock
import tempfile
import configparser


@pytest.fixture(autouse=True)
def mock_ffmpeg():
    """Mock ffmpeg command for all tests."""
    with patch("asyncio.create_subprocess_exec") as mock:
        process = Mock()
        process.communicate = Mock(return_value=(b"100.0", b""))
        process.returncode = 0
        mock.return_value = process
        yield mock


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


@pytest_asyncio.fixture(scope="function", autouse=True)
async def cleanup_asyncio_tasks():
    """Automatically cancel all pending asyncio tasks at the end of each test."""
    # Run the test
    yield

    # Get all pending tasks
    try:
        current_task = asyncio.current_task()
        all_tasks = asyncio.all_tasks()

        # Filter out the current task to avoid cancelling ourselves
        pending_tasks = [
            task for task in all_tasks if task != current_task and not task.done()
        ]

        if pending_tasks:
            # Cancel all pending tasks
            for task in pending_tasks:
                task.cancel()

            # Wait for all tasks to complete (with cancellation)
            try:
                await asyncio.gather(*pending_tasks, return_exceptions=True)
            except Exception:
                # Ignore exceptions during cleanup
                pass

    except RuntimeError:
        # Event loop might already be closed
        pass
