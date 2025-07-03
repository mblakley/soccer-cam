from pathlib import Path


def get_project_root() -> Path:
    """Returns the project root directory by finding the parent of the video_grouper package."""
    # This file is in video_grouper/utils/paths.py
    current_file = Path(__file__).resolve()

    # Go up two levels from utils/ to get to video_grouper/, then up one more to get to project root
    return current_file.parent.parent.parent.resolve()


def get_shared_data_path() -> Path:
    """Returns the path to the shared_data directory."""
    path = get_project_root() / "shared_data"
    path.mkdir(parents=True, exist_ok=True)
    return path
