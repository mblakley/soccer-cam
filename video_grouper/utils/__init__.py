"""
Video Grouper Utilities

This package contains utility modules used throughout the video_grouper application.
"""

# Make key utilities easily accessible at package level
from .directory_state import DirectoryState
from .ffmpeg_utils import (
    async_convert_file, 
    get_video_duration, 
    create_screenshot, 
    trim_video,
    verify_ffmpeg_install
)
from .locking import FileLock
from .paths import get_project_root, get_shared_data_path
from .time_utils import (
    get_all_timezones, 
    convert_utc_to_local, 
    parse_utc_from_string,
    parse_dt_from_string_with_tz
)
from .youtube_upload import (
    YouTubeUploader, 
    upload_group_videos, 
    get_youtube_paths,
    authenticate_youtube
)

__all__ = [
    'DirectoryState',
    'async_convert_file',
    'get_video_duration', 
    'create_screenshot',
    'trim_video',
    'verify_ffmpeg_install',
    'FileLock',
    'get_project_root',
    'get_shared_data_path',
    'get_all_timezones',
    'convert_utc_to_local',
    'parse_utc_from_string',
    'parse_dt_from_string_with_tz',
    'YouTubeUploader',
    'upload_group_videos',
    'get_youtube_paths',
    'authenticate_youtube',
] 