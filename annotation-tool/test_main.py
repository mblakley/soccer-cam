import pytest
import cv2
import numpy as np
from PyQt5.QtWidgets import QApplication
from video_annotation_tool import VideoAnnotationTool


@pytest.fixture(scope="module")
def app():
    """Set up a QApplication for the test session."""
    app = QApplication([])
    yield app
    app.quit()


@pytest.fixture
def real_video_frame():
    """Load the first frame from a real video."""
    video_path = "./data/clip_1.mp4"  # Path to the real video
    cap = cv2.VideoCapture(video_path)
    ret, frame = cap.read()
    cap.release()

    assert ret, f"Failed to read the first frame from {video_path}"
    return frame


@pytest.fixture
def mock_tool(app, real_video_frame):
    """Set up VideoAnnotationTool and mock original_frame with the real video frame."""
    video_path = "./data/clip_1.mp4"  # Path to the real video
    tool = VideoAnnotationTool(video_path)  # Initialize without a video path
    tool.original_frame = real_video_frame  # Mock the original frame with the real video frame
    return tool


def test_mock_original_frame(mock_tool):
    """Test if the mocked original_frame is correctly set."""
    # Verify the mocked original_frame
    frame_height, frame_width, _ = mock_tool.original_frame.shape
    assert frame_height > 0 and frame_width > 0, "Mocked original frame has invalid dimensions."

    # Validate against the video resolution
    assert frame_height == 1800, "Expected height of 1800, but got a different value."
    assert frame_width == 4096, "Expected width of 4096, but got a different value."


def test_calculate_zoom_region(mock_tool):
    """Test the calculate_zoom_region function."""
    # Test 1: Standard case
    center_x, center_y = 150, 100
    zoom_width, zoom_height = 100, 50
    frame_w, frame_h = mock_tool.original_frame.shape[1], mock_tool.original_frame.shape[0]
    x1, y1, x2, y2 = mock_tool.calculate_zoom_region(center_x, center_y, zoom_width, zoom_height)

    assert x1 == 100
    assert y1 == 75
    assert x2 == 200
    assert y2 == 125

    # Test 2: Clamp to boundaries
    center_x, center_y = 10, 10
    zoom_width, zoom_height = 300, 300  # Oversized zoom
    x1, y1, x2, y2 = mock_tool.calculate_zoom_region(center_x, center_y, zoom_width, zoom_height)

    assert x1 == 0
    assert y1 == 0
    assert x2 == 300
    assert y2 == 300  # Updated to match the clamped value

    # Test 3: Minimum zoom
    center_x, center_y = 150, 100
    zoom_width, zoom_height = 10, 10  # Below minimum
    x1, y1, x2, y2 = mock_tool.calculate_zoom_region(center_x, center_y, zoom_width, zoom_height)

    # Enforces minimum width (aspect ratio preserved)
    assert x2 - x1 >= 20
    assert y2 - y1 >= 20


def test_map_to_original(mock_tool):
    """Test the map_to_original function."""
    # Set up zoomed region and QLabel
    mock_tool.current_zoom_region = (50, 50, 250, 150)  # Zoomed region in original frame

    # Display coordinates within QLabel
    x_disp, y_disp = 200, 100  # Display center
    x1, y1, x2, y2 = mock_tool.map_to_original(x_disp - 50, y_disp - 50, x_disp + 50, y_disp + 50)

    # Validate results
    assert x1 >= mock_tool.current_zoom_region[0]
    assert x2 <= mock_tool.current_zoom_region[2]
    assert y1 >= mock_tool.current_zoom_region[1]
    assert y2 <= mock_tool.current_zoom_region[3]


def test_apply_zoom(mock_tool):
    """Test the apply_zoom function."""
    # Set up a zoom region
    mock_tool.current_zoom_region = (50, 50, 250, 150)

    # Apply zoom
    mock_tool.apply_zoom()

    # Validate that zoom_frame matches the zoom region
    zoom_frame = mock_tool.zoom_frame
    assert zoom_frame.shape[0] == 100  # Height of the zoomed region
    assert zoom_frame.shape[1] == 200  # Width of the zoomed region
    assert np.array_equal(
        zoom_frame,
        mock_tool.original_frame[50:150, 50:250],
    ), "Zoomed frame does not match expected region"


def test_display_frame(mock_tool):
    """Test the display_frame function."""
    # Set up zoomed frame and QLabel width
    mock_tool.current_zoom_region = (0, 0, 200, 100)
    mock_tool.label.setFixedWidth(200)  # Set expected QLabel width

    mock_tool.apply_zoom()
    mock_tool.display_frame()

    # Validate that the displayed frame width matches QLabel
    assert mock_tool.label.width() == 200
    assert mock_tool.zoom_frame.shape[1] == 200
