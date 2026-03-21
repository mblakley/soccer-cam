from pathlib import Path
import os
import sys
from datetime import datetime


def _is_pyinstaller() -> bool:
    """Return True if running inside a PyInstaller bundle."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def get_project_root() -> Path:
    """Returns the project root directory by finding the parent of the video_grouper package."""
    # This file is in video_grouper/utils/paths.py
    current_file = Path(__file__).resolve()

    # Go up two levels from utils/ to get to video_grouper/, then up one more to get to project root
    return current_file.parent.parent.parent.resolve()


def _get_storage_path_from_registry() -> Path | None:
    """Read StoragePath from the Windows registry (set by the NSIS installer)."""
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"Software\VideoGrouper")
        storage_path = winreg.QueryValueEx(key, "StoragePath")[0]
        winreg.CloseKey(key)
        return Path(storage_path)
    except Exception:
        return None


def _get_storage_path_from_env() -> Path | None:
    """Read storage path from VIDEOGROUPER_CONFIG environment variable.

    The env var should point to a config.ini file; we return its parent directory.
    """
    env_config = os.environ.get("VIDEOGROUPER_CONFIG")
    if env_config:
        config_path = Path(env_config)
        if config_path.exists():
            return config_path.parent
    return None


def get_shared_data_path() -> Path:
    """Returns the path to the shared_data directory.

    Priority: VIDEOGROUPER_CONFIG env var > Windows registry > exe directory > project root.
    """
    # Environment variable override (highest priority)
    env_path = _get_storage_path_from_env()
    if env_path:
        return env_path

    if _is_pyinstaller():
        # Try registry (set by NSIS installer)
        reg_path = _get_storage_path_from_registry()
        if reg_path and reg_path.exists():
            return reg_path
        # Fall back to directory containing the executable
        return Path(os.path.dirname(sys.executable))

    path = get_project_root() / "shared_data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_state_file_path(group_dir: str, storage_path: str) -> str:
    """Get the path to the state.json file in a group directory.

    Args:
        group_dir: The group directory path
        storage_path: The storage path

    Returns:
        Path to the state.json file
    """
    return str(resolve_path(os.path.join(group_dir, "state.json"), storage_path))


def get_combined_video_path(group_dir: str, storage_path: str) -> str:
    """Get the path to the combined.mp4 file in a group directory.

    Args:
        group_dir: The group directory path
        storage_path: The storage path

    Returns:
        Path to the combined.mp4 file
    """
    return str(resolve_path(os.path.join(group_dir, "combined.mp4"), storage_path))


def get_match_info_path(group_dir: str, storage_path: str) -> str:
    """Get the path to the match_info.ini file in a group directory.

    Args:
        group_dir: The group directory path
        storage_path: The storage path

    Returns:
        Path to the match_info.ini file
    """
    return str(resolve_path(os.path.join(group_dir, "match_info.ini"), storage_path))


def get_trimmed_video_path(group_dir: str, match_info, storage_path: str) -> str:
    """
    Create the subdirectory structure and return the path for the trimmed file.

    Args:
        group_dir: The group directory path
        match_info: MatchInfo object with team and location information
        storage_path: The storage path

    Returns:
        Path where the trimmed file should be created
    """
    # Extract date from directory name (format: YYYY.MM.DD-HH.MM.SS)
    dir_name = os.path.basename(group_dir)
    try:
        date_part = dir_name.split("-")[0]  # YYYY.MM.DD
        date_obj = datetime.strptime(date_part, "%Y.%m.%d")
        formatted_date = date_obj.strftime("%m-%d-%Y")
    except Exception:
        # Fallback to current date if parsing fails
        formatted_date = datetime.now().strftime("%m-%d-%Y")

    # Get sanitized team names and location
    my_team, opponent_team, location = match_info.get_sanitized_names()

    # Create subdirectory name: "YYYY.MM.DD - My Team vs Opponent Team (location)"
    subdir_name = f"{date_part} - {my_team} vs {opponent_team} ({location})"
    subdir_path = os.path.join(group_dir, subdir_name)
    abs_subdir_path = resolve_path(subdir_path, storage_path)

    # Create the subdirectory if it doesn't exist
    os.makedirs(abs_subdir_path, exist_ok=True)

    # Create filename: "myteam-opponent-location-MM-DD-YYYY-raw.mp4"
    # Convert team names to lowercase and replace spaces with hyphens
    my_team_slug = my_team.lower().replace(" ", "-")
    opponent_team_slug = opponent_team.lower().replace(" ", "-")
    location_slug = location.lower().replace(" ", "-")

    filename = (
        f"{my_team_slug}-{opponent_team_slug}-{location_slug}-{formatted_date}-raw.mp4"
    )

    return str(abs_subdir_path / filename)


def get_ntfy_service_state_path(storage_path: str) -> str:
    """Get the path to the NTFY service state file.

    Args:
        storage_path: The storage path

    Returns:
        Path to the ntfy_service_state.json file
    """
    return os.path.join(storage_path, "ntfy_service_state.json")


def get_home_cleanup_state_path(storage_path: str) -> str:
    """Get the path to the home recording cleanup state file.

    Args:
        storage_path: The storage path

    Returns:
        Path to the home_cleanup_state.json file
    """
    return os.path.join(storage_path, "home_cleanup_state.json")


def get_camera_state_path(storage_path: str) -> str:
    """Get the path to the camera state file.

    Args:
        storage_path: The storage path

    Returns:
        Path to the camera_state.json file
    """
    return os.path.join(storage_path, "camera_state.json")


def get_match_info_dist_path() -> str:
    """Get the path to the match_info.ini.dist file.

    Returns:
        Path to the match_info.ini.dist file
    """
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "match_info.ini.dist",
    )


def resolve_path(path: str, storage_path: str) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    return Path(storage_path) / p


# WARNING: All path utilities now require storage_path as an argument. If you see a missing argument error, update the call site to pass storage_path from config.
