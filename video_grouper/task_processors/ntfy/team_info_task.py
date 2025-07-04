"""
Team information NTFY task.

This task handles asking users about team information for a match.
"""

import logging
from typing import Dict, Any, Optional, List

from .base_ntfy_task import BaseNtfyTask, NtfyTaskResult
from video_grouper.utils.config import Config

logger = logging.getLogger(__name__)


class TeamInfoTask(BaseNtfyTask):
    """
    Task for collecting team information.

    This task asks users to provide team information for a match,
    such as team names and location.
    """

    def __init__(
        self,
        group_dir: str,
        config: Config,
        combined_video_path: str,
        existing_info: Optional[Dict[str, str]] = None,
    ):
        """
        Initialize the team info task.

        Args:
            group_dir: Directory associated with the task
            config: Configuration object
            combined_video_path: Path to the combined video file
            existing_info: Existing team information if any
        """
        super().__init__(
            group_dir,
            config,
            {
                "combined_video_path": combined_video_path,
                "existing_info": existing_info or {},
            },
        )
        self.combined_video_path = combined_video_path
        self.existing_info = existing_info or {}

    def get_task_type(self) -> str:
        """Get the task type identifier."""
        return "team_info"

    async def create_question(self) -> Dict[str, Any]:
        """
        Create the question data for asking about team information.

        Returns:
            Dictionary containing question data
        """
        # Determine what information is missing
        missing_fields = []
        if (
            "team_name" not in self.existing_info
            and "my_team_name" not in self.existing_info
        ):
            missing_fields.append("team name")
        if (
            "opponent_name" not in self.existing_info
            and "opponent_team_name" not in self.existing_info
        ):
            missing_fields.append("opponent team name")
        if "location" not in self.existing_info:
            missing_fields.append("game location")

        if not missing_fields:
            # All information is already available
            return {}

        missing_fields_str = ", ".join(missing_fields)

        return {
            "message": f"Missing match information: {missing_fields_str}. Please update match_info.ini manually.",
            "title": "Missing Match Information",
            "tags": ["warning", "info"],
            "priority": 4,
            "image_path": None,  # No screenshot for team info
            "actions": [],  # No action buttons for team info
            "metadata": {
                "combined_video_path": self.combined_video_path,
                "existing_info": self.existing_info,
                "missing_fields": missing_fields,
            },
        }

    async def process_response(self, response: str) -> NtfyTaskResult:
        """
        Process a response to the team info question.

        Args:
            response: The user's response

        Returns:
            NtfyTaskResult indicating success and whether to continue
        """
        # For team info, we assume the user has manually updated match_info.ini
        # We just need to check if it's populated

        match_info = self.get_match_info()
        if match_info and match_info.is_populated():
            logger.info(f"Team info populated for {self.group_dir}")
            return NtfyTaskResult(
                success=True,
                should_continue=False,
                message="Team information completed",
            )
        else:
            logger.info(f"Team info still missing for {self.group_dir}")
            return NtfyTaskResult(
                success=False,
                should_continue=True,
                message="Team information still missing",
            )

    def get_missing_fields(self) -> List[str]:
        """
        Get a list of missing team information fields.

        Returns:
            List of missing field names
        """
        missing_fields = []
        if (
            "team_name" not in self.existing_info
            and "my_team_name" not in self.existing_info
        ):
            missing_fields.append("team_name")
        if (
            "opponent_name" not in self.existing_info
            and "opponent_team_name" not in self.existing_info
        ):
            missing_fields.append("opponent_name")
        if "location" not in self.existing_info:
            missing_fields.append("location")
        return missing_fields
