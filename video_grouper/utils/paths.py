from pathlib import Path

def get_project_root() -> Path:
    """Returns the project root directory by finding the parent of the video_grouper package."""
    # Assumes this file is in video_grouper/
    return Path(__file__).parent.parent.resolve()

def get_shared_data_path() -> Path:
    """Returns the path to the shared_data directory."""
    path = get_project_root() / "shared_data"
    path.mkdir(parents=True, exist_ok=True)
    return path 