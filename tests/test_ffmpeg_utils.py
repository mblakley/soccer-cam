import pytest
from unittest.mock import patch, MagicMock
from video_grouper.utils.ffmpeg_utils import (
    async_convert_file,
    combine_videos,
    get_video_duration,
    trim_video,
    verify_ffmpeg_install,
    create_screenshot,
)


@pytest.fixture
def mock_logger():
    """Mocks the logger used in ffmpeg_utils to prevent console output during tests."""
    with patch("video_grouper.utils.ffmpeg_utils.logger", MagicMock()) as mock:
        yield mock


def _make_mock_container(duration_us=123_450_000):
    """Create a mock av container with configurable duration."""
    container = MagicMock()
    container.duration = duration_us

    video_stream = MagicMock()
    video_stream.type = "video"
    video_stream.duration = 12345
    video_stream.time_base = 0.01
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
    container.__enter__ = MagicMock(return_value=container)
    container.__exit__ = MagicMock(return_value=False)
    return container


@pytest.fixture
def mock_av_open():
    """Mocks av.open to avoid actual file I/O."""
    container = _make_mock_container()
    with patch(
        "video_grouper.utils.ffmpeg_utils.av.open", return_value=container
    ) as mock:
        yield mock


@pytest.fixture
def mock_file_ops():
    """Mocks file system operations to isolate tests from the actual filesystem."""
    with (
        patch("os.path.exists", return_value=True) as mock_exists,
        patch("os.remove") as mock_remove,
        patch("os.path.getsize", return_value=1024) as mock_getsize,
    ):
        yield {"exists": mock_exists, "remove": mock_remove, "getsize": mock_getsize}


@pytest.mark.asyncio
async def test_get_video_duration_success(mock_av_open):
    """Verifies that get_video_duration correctly reads container duration."""
    # duration is in microseconds, av.time_base = 1_000_000
    mock_av_open.return_value.duration = 123_450_000
    mock_av_open.return_value.__enter__.return_value = mock_av_open.return_value

    duration = await get_video_duration("dummy.mp4")

    assert duration == pytest.approx(123.45, abs=0.01)
    mock_av_open.assert_called_once()


@pytest.mark.asyncio
async def test_get_video_duration_failure(mock_av_open):
    """Ensures get_video_duration returns None when av.open fails."""
    import av

    mock_av_open.side_effect = av.error.FFmpegError(0, "test error")

    duration = await get_video_duration("dummy.mp4")

    assert duration is None


@pytest.mark.asyncio
async def test_async_convert_file_success(mock_av_open, mock_file_ops, mock_logger):
    """Tests the successful conversion of a .dav file to .mp4."""
    dav_path = "test.dav"
    mock_file_ops["exists"].return_value = True

    # Mock the output container as well (second call to av.open)
    output_container = _make_mock_container()
    mock_av_open.side_effect = [mock_av_open.return_value, output_container]

    result_path = await async_convert_file(dav_path)

    assert result_path == "test.mp4"


@pytest.mark.asyncio
async def test_async_convert_file_failure(mock_av_open, mock_file_ops, mock_logger):
    """Tests that the function returns None if PyAV fails."""
    import av

    mock_av_open.side_effect = av.error.FFmpegError(0, "test error")
    mock_file_ops["exists"].return_value = True

    result_path = await async_convert_file("test.dav")

    assert result_path is None


@pytest.mark.asyncio
async def test_verify_ffmpeg_install_success():
    """Tests that PyAV installation is correctly verified."""
    with patch("video_grouper.utils.ffmpeg_utils.av.codec.Codec") as mock_codec:
        mock_codec.return_value = MagicMock()
        assert await verify_ffmpeg_install() is True


@pytest.mark.asyncio
async def test_verify_ffmpeg_install_failure():
    """Tests that verification fails when codec is not available."""
    with patch(
        "video_grouper.utils.ffmpeg_utils.av.codec.Codec",
        side_effect=Exception("not found"),
    ):
        assert await verify_ffmpeg_install() is False


@pytest.mark.asyncio
async def test_create_screenshot_success(mock_av_open, mock_file_ops):
    """Tests the successful creation of a video screenshot."""
    video_path = "test.mp4"
    screenshot_path = "test.jpg"

    # Set up frame decode with realistic image mock
    mock_frame = MagicMock()
    mock_image = MagicMock()
    mock_image.size = (1920, 1080)
    # getextrema returns per-channel (min, max) tuples; wide range = not corrupt
    mock_image.crop.return_value = mock_image
    mock_image.getextrema.return_value = ((0, 255), (0, 255), (0, 255))
    mock_frame.to_image.return_value = mock_image
    container = mock_av_open.return_value
    container.__enter__.return_value = container
    container.decode.return_value = iter([mock_frame])

    success = await create_screenshot(video_path, screenshot_path)

    assert success is True


@pytest.mark.asyncio
async def test_create_screenshot_failure(mock_av_open, mock_file_ops):
    """Tests that screenshot creation returns False when PyAV fails."""
    import av

    mock_av_open.side_effect = av.error.FFmpegError(0, "test error")

    success = await create_screenshot("test.mp4", "test.jpg")

    assert success is False


@pytest.mark.asyncio
async def test_combine_videos_success(mock_av_open):
    """Tests successful video combination."""
    # Set up mock for multiple av.open calls (probe + each input + output)
    containers = [_make_mock_container() for _ in range(4)]
    mock_av_open.side_effect = containers

    result = await combine_videos(["file1.dav", "file2.dav"], "output.mp4")

    assert result is True


@pytest.mark.asyncio
async def test_trim_video_success(mock_av_open):
    """Tests successful video trimming."""
    containers = [_make_mock_container() for _ in range(2)]
    mock_av_open.side_effect = containers

    result = await trim_video("input.mp4", "output.mp4", "00:05:00", "01:00:00")

    assert result is True
