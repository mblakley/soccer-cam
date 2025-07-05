"""
YouTube upload task for uploading videos to YouTube.
"""

import os
import logging
from typing import Dict, Any, Optional, Callable, Awaitable
from dataclasses import dataclass, field

from video_grouper.task_processors.services.ntfy_service import NtfyService

from .base_upload_task import BaseUploadTask
from video_grouper.models import MatchInfo
from video_grouper.models import DirectoryState
from video_grouper.utils.config import YouTubeConfig

logger = logging.getLogger(__name__)


@dataclass(unsafe_hash=True)
class YoutubeUploadTask(BaseUploadTask):
    """
    Task for uploading videos to YouTube.

    Handles the upload process including authentication and metadata.
    """

    group_dir: str
    youtube_config: YouTubeConfig = field(compare=False, hash=False)
    ntfy_service: NtfyService = field(compare=False, hash=False)

    def get_platform(self) -> str:
        """Return the platform identifier."""
        return "youtube"

    def get_item_path(self) -> str:
        """Return the group directory path."""
        return self.group_dir

    def serialize(self) -> Dict[str, Any]:
        """
        Serialize the task for state persistence.

        Returns:
            Dictionary containing task data
        """
        return {"task_type": self.task_type, "group_dir": self.group_dir}

    async def execute(
        self, queue_task: Optional[Callable[[Any], Awaitable[None]]] = None
    ) -> bool:
        """
        Execute the YouTube upload task.

        Args:
            queue_task: Function to queue additional tasks

        Returns:
            True if upload succeeded, False otherwise
        """
        try:
            # Import here to avoid circular import
            from video_grouper.utils.youtube_upload import (
                YouTubeUploader,
                get_youtube_paths,
            )
            # from video_grouper.task_processors.services.ntfy_service import NtfyService

            # Get storage path from group directory
            storage_path = os.path.dirname(self.group_dir)
            while storage_path and not os.path.exists(
                os.path.join(storage_path, "config.ini")
            ):
                parent = os.path.dirname(storage_path)
                if parent == storage_path:  # Reached root
                    storage_path = os.path.dirname(self.group_dir)
                    break
                storage_path = parent

            if self.ntfy_service is None:
                raise ValueError("ntfy_service must be provided to YoutubeUploadTask")

            # Get credentials and token file paths
            credentials_file, token_file = get_youtube_paths(storage_path)

            # Check if credentials file exists
            if not os.path.exists(credentials_file):
                logger.error(f"YouTube credentials file not found: {credentials_file}")
                return False

            logger.info(f"Starting YouTube upload for {self.group_dir}")

            # Load match info to get team name
            match_info_path = os.path.join(self.group_dir, "match_info.ini")
            if not os.path.exists(match_info_path):
                logger.error(f"match_info.ini not found in {self.group_dir}")
                return False

            match_info = MatchInfo.from_file(match_info_path)
            if not match_info:
                logger.error(f"Could not load match info from {match_info_path}")
                return False

            # Get playlist names using coordination logic
            processed_playlist_name, raw_playlist_name = await self._get_playlist_names(
                match_info, self.youtube_config, self.ntfy_service, storage_path
            )

            # If we don't have playlist names and a request was sent, skip for now
            if not processed_playlist_name and not raw_playlist_name:
                logger.info(f"Waiting for playlist name response for {self.group_dir}")
                return False  # Will be retried later

            # Get privacy status from config
            privacy_status = self.youtube_config.privacy_status

            # Initialize YouTube uploader
            uploader = YouTubeUploader(credentials_file, token_file)

            success = True

            # Find the subdirectory containing the videos
            subdirs = [
                d
                for d in os.listdir(self.group_dir)
                if os.path.isdir(os.path.join(self.group_dir, d))
            ]
            if len(subdirs) == 1:
                video_dir = os.path.join(self.group_dir, subdirs[0])
            else:
                logger.error(
                    f"Expected exactly one subdirectory in {self.group_dir}, found {len(subdirs)}. Cannot locate video files."
                )
                return False

            # Find the raw video file (ends with '-raw.mp4')
            raw_video_path = None
            processed_video_path = None
            for fname in os.listdir(video_dir):
                if fname.endswith("-raw.mp4"):
                    raw_video_path = os.path.join(video_dir, fname)
                    processed_video_path = os.path.join(
                        video_dir, fname.replace("-raw.mp4", ".mp4")
                    )
                    break

            if not raw_video_path or not os.path.exists(raw_video_path):
                logger.error(
                    f"No raw video file ending with '-raw.mp4' found in {video_dir}"
                )
                return False

            # Upload processed (trimmed) video
            if processed_video_path and os.path.exists(processed_video_path):
                logger.info(f"Uploading processed video: {processed_video_path}")
                title = match_info.get_youtube_title("processed")
                description = match_info.get_youtube_description("processed")
                playlist_id = None

                if processed_playlist_name:
                    playlist_id = uploader.get_or_create_playlist(
                        processed_playlist_name, description
                    )

                video_id = uploader.upload_video(
                    processed_video_path,
                    title,
                    description,
                    privacy_status=privacy_status,
                    playlist_id=playlist_id,
                )

                if not video_id:
                    logger.error(
                        f"Failed to upload processed video: {processed_video_path}"
                    )
                    success = False

            # Upload raw (untrimmed) video
            if raw_video_path and os.path.exists(raw_video_path):
                logger.info(f"Uploading raw video: {raw_video_path}")
                title = match_info.get_youtube_title("raw")
                description = match_info.get_youtube_description("raw")
                playlist_id = None

                if raw_playlist_name:
                    playlist_id = uploader.get_or_create_playlist(
                        raw_playlist_name, description
                    )

                video_id = uploader.upload_video(
                    raw_video_path,
                    title,
                    description,
                    privacy_status=privacy_status,
                    playlist_id=playlist_id,
                )

                if not video_id:
                    logger.error(f"Failed to upload raw video: {raw_video_path}")
                    success = False

            if success:
                logger.info(
                    f"Successfully uploaded videos for {self.group_dir} to YouTube"
                )
                return True
            else:
                logger.error(f"Failed to upload videos for {self.group_dir} to YouTube")
                return False

        except ImportError as e:
            logger.error(f"YouTube upload functionality not available: {e}")
            return False
        except Exception as e:
            logger.error(f"Error during YouTube upload for {self.group_dir}: {e}")
            return False

    async def _get_playlist_names(
        self,
        match_info: MatchInfo,
        config: YouTubeConfig,
        ntfy_service,
        storage_path: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Get playlist names for processed and raw videos.

        Returns:
            Tuple of (processed_playlist_name, raw_playlist_name)
        """
        dir_state = DirectoryState(self.group_dir)

        # Log the team name being searched for
        logger.info(f"Looking up playlist for team: '{match_info.my_team_name}'")

        # Log the available mappings
        if hasattr(config, "playlist_map") and config.playlist_map:
            logger.info(f"YOUTUBE.PLAYLIST_MAP: {config.playlist_map.root}")
        else:
            logger.info("YOUTUBE.PLAYLIST_MAP is not set or empty.")

        # Check if playlist name is already in state
        base_playlist_name = dir_state.get_youtube_playlist_name()

        # Only use the strongly-typed playlist_map
        mapped = config.playlist_map.get(match_info.my_team_name)
        logger.info(f"Result of playlist_map lookup: {mapped}")
        if mapped:
            base_playlist_name = mapped
            logger.info(
                f"Found playlist '{base_playlist_name}' for team '{match_info.my_team_name}' in YOUTUBE.PLAYLIST_MAP section."
            )

        # Log the final result before requesting via NTFY
        if not base_playlist_name:
            logger.warning(
                f"No playlist mapping found for team '{match_info.my_team_name}'. Sending NTFY request."
            )
        else:
            logger.info(f"Final playlist name to use: {base_playlist_name}")

        # If still no mapping, and no request pending, ask the user
        if not base_playlist_name and not ntfy_service.is_waiting_for_input(
            self.group_dir
        ):
            await ntfy_service.request_playlist_name(
                self.group_dir, match_info.my_team_name
            )
            # Return None for now, upload will be retried later when user responds
            return None, None
        elif base_playlist_name and not dir_state.get_youtube_playlist_name():
            # If we found a name in the config, but not in the state file, update the state file
            dir_state.set_youtube_playlist_name(base_playlist_name)

        # Return playlist names
        processed_playlist_name = base_playlist_name
        raw_playlist_name = (
            f"{base_playlist_name} - Full Field" if base_playlist_name else None
        )

        return processed_playlist_name, raw_playlist_name

    def __str__(self) -> str:
        """String representation of the task."""
        return f"YoutubeUploadTask({os.path.basename(self.group_dir)})"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "YoutubeUploadTask":
        """
        Create a YoutubeUploadTask from serialized data.

        Args:
            data: Dictionary containing task data

        Returns:
            YoutubeUploadTask instance
        """
        # Handle both 'group_dir' and 'item_path' for backward compatibility
        group_dir = data.get("group_dir") or data.get("item_path")
        return cls(group_dir=group_dir)
