"""
YouTube upload task for uploading videos to YouTube.
"""

import os
import logging
from typing import Dict, Any, Optional
from dataclasses import dataclass

from video_grouper.utils.paths import resolve_path

from .base_upload_task import BaseUploadTask
from video_grouper.models import MatchInfo, DirectoryState
from video_grouper.utils.config import YouTubeConfig

logger = logging.getLogger(__name__)


@dataclass(unsafe_hash=True)
class YoutubeUploadTask(BaseUploadTask):
    """
    Task for uploading videos to YouTube.

    Handles the upload process including authentication and metadata.
    """

    group_dir: str

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
        self, youtube_config=None, ntfy_service=None, storage_path=None
    ) -> bool:
        """
        Execute the YouTube upload task.

        Args:
            youtube_config: YouTube configuration (provided by processor)
            ntfy_service: NTFY service (provided by processor)

        Returns:
            True if upload succeeded, False otherwise
        """
        try:
            from video_grouper.utils.youtube_upload import (
                YouTubeUploader,
                get_youtube_paths,
            )

            # Get storage path - passed by processor or derive from shared_data
            if storage_path is None:
                from video_grouper.utils.paths import get_shared_data_path

                storage_path = str(get_shared_data_path())

            if ntfy_service is None:
                logger.warning(
                    "YoutubeUploadTask: ntfy_service not provided, "
                    "playlist fallback notifications will be unavailable"
                )

            # Get credentials and token file paths
            credentials_file, token_file = get_youtube_paths(storage_path)

            # Check if credentials file exists
            if not os.path.exists(credentials_file):
                logger.error(f"YouTube credentials file not found: {credentials_file}")
                return False

            # Resolve the group directory path
            resolved_group_dir = str(resolve_path(self.group_dir, storage_path))
            logger.info(
                f"Starting YouTube upload for {self.group_dir} (resolved: {resolved_group_dir})"
            )

            # Load match info to get team name
            match_info_path = str(
                resolve_path(
                    os.path.join(self.group_dir, "match_info.ini"), storage_path
                )
            )
            if not os.path.exists(match_info_path):
                logger.error(f"match_info.ini not found in {resolved_group_dir}")
                return False

            match_info = MatchInfo.from_file(match_info_path)
            if not match_info:
                logger.error(f"Could not load match info from {match_info_path}")
                return False

            # Get playlist names using coordination logic
            processed_playlist_name, raw_playlist_name = await self._get_playlist_names(
                match_info, youtube_config, ntfy_service, storage_path
            )

            # If we don't have playlist names and a request was sent, skip for now
            if not processed_playlist_name and not raw_playlist_name:
                logger.info(
                    f"Waiting for playlist name response for {resolved_group_dir}"
                )
                return False  # Will be retried later

            # Get privacy status from config
            privacy_status = youtube_config.privacy_status

            # Initialize YouTube uploader
            uploader = YouTubeUploader(credentials_file, token_file)

            success = True

            # Find the subdirectory containing the videos
            subdirs = [
                d
                for d in os.listdir(resolved_group_dir)
                if os.path.isdir(os.path.join(resolved_group_dir, d))
            ]
            if len(subdirs) == 1:
                video_dir = os.path.join(resolved_group_dir, subdirs[0])
            else:
                logger.error(
                    f"Expected exactly one subdirectory in {resolved_group_dir}, found {len(subdirs)}. Cannot locate video files."
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
                # Mark directory as fully complete
                try:
                    from video_grouper.models import DirectoryState

                    resolved = str(resolve_path(self.group_dir, storage_path))
                    dir_state = DirectoryState(resolved, storage_path)
                    await dir_state.update_group_status("complete")
                    logger.info(
                        f"YOUTUBE_UPLOAD: Updated state to 'complete' for {self.group_dir}"
                    )
                except Exception as state_err:
                    logger.warning(
                        f"YOUTUBE_UPLOAD: Could not update state: {state_err}"
                    )
                logger.info(
                    f"YOUTUBE_UPLOAD: Task completed successfully for {self.group_dir}"
                )
                return True
            else:
                logger.error(f"Failed to upload videos for {self.group_dir} to YouTube")
                # Force flush the log to ensure the message is written
                for handler in logger.handlers:
                    handler.flush()
                logger.error(f"YOUTUBE_UPLOAD: Task failed for {self.group_dir}")
                return False

        except ImportError as e:
            logger.error(f"YouTube upload functionality not available: {e}")
            return False
        except Exception as e:
            from video_grouper.utils.youtube_upload import YouTubeQuotaError

            if isinstance(e, YouTubeQuotaError):
                raise  # Propagate to processor for deferred retry
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

        Lookup order:
        1. DirectoryState (playlist name stored in state.json)
        2. config.playlist_map (team name → playlist name mapping)
        3. config.processed_playlist / config.raw_playlist (format strings)
        4. Fall back to ntfy request if nothing found

        Returns:
            Tuple of (processed_playlist_name, raw_playlist_name)
        """
        logger.info(f"Looking up playlist for team: '{match_info.my_team_name}'")

        base_playlist_name = None

        # 1. Check DirectoryState for a stored playlist name
        try:
            dir_state = DirectoryState(self.group_dir, storage_path)
            state_playlist = dir_state.get_youtube_playlist_name()
            if state_playlist:
                base_playlist_name = state_playlist
                logger.info(
                    f"Using playlist from directory state: {base_playlist_name}"
                )
        except Exception as e:
            logger.warning(f"Error reading playlist from directory state: {e}")

        # 2. Check config.playlist_map for team-based mapping
        #    Supports exact match or case-insensitive substring match
        #    (e.g. key "13b ecnl-rl rochester" matches team "Western New York Flash - 13B ECNL-RL Rochester")
        if not base_playlist_name:
            if hasattr(config, "playlist_map") and config.playlist_map:
                try:
                    team_lower = match_info.my_team_name.lower()
                    for key, playlist_name in config.playlist_map.items():
                        if key.lower() == team_lower or key.lower() in team_lower:
                            base_playlist_name = playlist_name
                            logger.info(
                                f"Using playlist from config map: {base_playlist_name} (matched key '{key}')"
                            )
                            break
                except Exception as e:
                    logger.warning(f"Error looking up playlist in config map: {e}")

        # 3. If we have a base playlist name, derive processed and raw names
        if base_playlist_name:
            return base_playlist_name, base_playlist_name + " - Full Field"

        # 4. Try format-string based playlist configs
        processed_playlist_name = None
        raw_playlist_name = None

        if hasattr(config, "processed_playlist") and config.processed_playlist:
            try:
                processed_playlist_name = config.processed_playlist.name_format.format(
                    my_team_name=match_info.my_team_name,
                    opponent_team_name=match_info.opponent_team_name,
                    location=match_info.location,
                )
                logger.info(
                    f"Using processed playlist from config: {processed_playlist_name}"
                )
            except Exception as e:
                logger.warning(f"Error formatting processed playlist name: {e}")

        if hasattr(config, "raw_playlist") and config.raw_playlist:
            try:
                raw_playlist_name = config.raw_playlist.name_format.format(
                    my_team_name=match_info.my_team_name,
                    opponent_team_name=match_info.opponent_team_name,
                    location=match_info.location,
                )
                logger.info(f"Using raw playlist from config: {raw_playlist_name}")
            except Exception as e:
                logger.warning(f"Error formatting raw playlist name: {e}")

        if processed_playlist_name or raw_playlist_name:
            return processed_playlist_name, raw_playlist_name

        # 5. No mapping found — request via ntfy if not already waiting
        if ntfy_service and not ntfy_service.is_waiting_for_input(self.group_dir):
            logger.warning(
                f"No playlist mapping found for team '{match_info.my_team_name}'. Sending NTFY request."
            )
            await ntfy_service.request_playlist_name(
                self.group_dir, match_info.my_team_name
            )

        return None, None

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

    @classmethod
    def deserialize(cls, data: Dict[str, object]) -> "YoutubeUploadTask":
        """
        Deserialize a YoutubeUploadTask from its serialized data.

        Args:
            data: Dictionary containing serialized task data

        Returns:
            Deserialized YoutubeUploadTask instance
        """
        return cls.from_dict(data)
