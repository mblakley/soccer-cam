"""
Factory for creating NTFY tasks from metadata.

This module provides a factory class that can create the appropriate NTFY task
based on task type and metadata.
"""

import logging
from typing import Dict, Any, Optional

from video_grouper.utils.config import Config
from video_grouper.task_processors.services.ntfy_service import NtfyService
from .base_ntfy_task import BaseNtfyTask
from .game_start_task import GameStartTask
from .game_end_task import GameEndTask
from .team_info_task import TeamInfoTask

logger = logging.getLogger(__name__)


class NtfyTaskFactory:
    """
    Factory for creating NTFY tasks.

    This factory can create the appropriate task type based on task type
    and metadata.
    """

    @staticmethod
    def create_task(
        task_type: str,
        group_dir: str,
        config: Config,
        ntfy_service: NtfyService,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[BaseNtfyTask]:
        """
        Create a task of the specified type.

        Args:
            task_type: Type of task to create
            group_dir: Directory associated with the task
            config: Configuration object (can be None if using metadata-based config)
            ntfy_service: NTFY service instance
            metadata: Additional metadata for the task

        Returns:
            Created task instance, or None if task type is unknown
        """
        metadata = metadata or {}

        # If no config provided, try to create one from metadata
        if not config and "config" in metadata:
            from video_grouper.utils.config import NtfyConfig

            ntfy_config_data = metadata["config"].get("ntfy", {})
            ntfy_config = NtfyConfig(**ntfy_config_data)

            # Create a minimal config with just the NTFY section
            class MinimalConfig:
                def __init__(self, ntfy_config):
                    self.ntfy = ntfy_config

            config = MinimalConfig(ntfy_config)

        from ..ntfy_enums import NtfyInputType

        if task_type == NtfyInputType.GAME_START_TIME.value:
            combined_video_path = metadata.get("combined_video_path")
            time_offset = metadata.get("time_offset", "00:00")
            time_seconds = metadata.get("time_seconds", 0)

            if not combined_video_path:
                logger.error("Missing combined_video_path for game start task")
                return None

            return GameStartTask(
                group_dir=group_dir,
                config=config,
                ntfy_service=ntfy_service,
                combined_video_path=combined_video_path,
                time_offset=time_offset,
                time_seconds=time_seconds,
            )

        elif task_type == NtfyInputType.GAME_END_TIME.value:
            combined_video_path = metadata.get("combined_video_path")
            start_time_offset = metadata.get("start_time_offset")
            time_offset = metadata.get("time_offset")
            time_seconds = metadata.get("time_seconds")

            if not combined_video_path:
                logger.error("Missing combined_video_path for game end task")
                return None

            if not start_time_offset:
                logger.error("Missing start_time_offset for game end task")
                return None

            return GameEndTask(
                group_dir=group_dir,
                config=config,
                ntfy_service=ntfy_service,
                combined_video_path=combined_video_path,
                start_time_offset=start_time_offset,
                time_offset=time_offset,
                time_seconds=time_seconds,
            )

        elif task_type == NtfyInputType.TEAM_INFO.value:
            combined_video_path = metadata.get("combined_video_path")
            existing_info = metadata.get("existing_info", {})

            if not combined_video_path:
                logger.error("Missing combined_video_path for team info task")
                return None

            return TeamInfoTask(
                group_dir=group_dir,
                config=config,
                ntfy_service=ntfy_service,
                combined_video_path=combined_video_path,
                existing_info=existing_info,
            )

        else:
            logger.error(f"Unknown task type: {task_type}")
            return None

    @staticmethod
    def create_game_start_task(
        group_dir: str,
        config: Config,
        ntfy_service: NtfyService,
        combined_video_path: str,
    ) -> GameStartTask:
        """
        Create a game start task.

        Args:
            group_dir: Directory associated with the task
            config: Configuration object
            ntfy_service: NTFY service instance
            combined_video_path: Path to the combined video file

        Returns:
            GameStartTask instance
        """
        return GameStartTask(
            group_dir=group_dir,
            config=config,
            ntfy_service=ntfy_service,
            combined_video_path=combined_video_path,
        )

    @staticmethod
    def create_game_end_task(
        group_dir: str,
        config: Config,
        ntfy_service: NtfyService,
        combined_video_path: str,
        start_time_offset: str,
    ) -> GameEndTask:
        """
        Create a game end task.

        Args:
            group_dir: Directory associated with the task
            config: Configuration object
            ntfy_service: NTFY service instance
            combined_video_path: Path to the combined video file
            start_time_offset: The game start time offset

        Returns:
            GameEndTask instance
        """
        return GameEndTask(
            group_dir=group_dir,
            config=config,
            ntfy_service=ntfy_service,
            combined_video_path=combined_video_path,
            start_time_offset=start_time_offset,
        )

    @staticmethod
    def create_team_info_task(
        group_dir: str,
        config: Config,
        ntfy_service: NtfyService,
        combined_video_path: str,
        existing_info: Optional[Dict[str, str]] = None,
    ) -> TeamInfoTask:
        """
        Create a team info task.

        Args:
            group_dir: Directory associated with the task
            config: Configuration object
            ntfy_service: NTFY service instance
            combined_video_path: Path to the combined video file
            existing_info: Existing team information if any

        Returns:
            TeamInfoTask instance
        """
        return TeamInfoTask(
            group_dir=group_dir,
            config=config,
            ntfy_service=ntfy_service,
            combined_video_path=combined_video_path,
            existing_info=existing_info,
        )
