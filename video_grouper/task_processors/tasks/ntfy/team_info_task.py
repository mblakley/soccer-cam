"""
Team information NTFY task.

Asks sequential questions for missing match info fields:
1. my_team_name (action buttons if multiple teams configured, auto-filled if only one)
2. opponent_team_name (free text reply)
3. location (free text reply, hint: home/away)

Each response is written to match_info.ini immediately.
"""

import os
import logging
from typing import Dict, Any, Optional, List, Tuple

from .base_ntfy_task import BaseNtfyTask, NtfyTaskResult
from video_grouper.utils.config import Config
from video_grouper.utils.paths import get_combined_video_path, resolve_path
from video_grouper.task_processors.services.ntfy_service import NtfyService
from video_grouper.models.match_info import MatchInfo

logger = logging.getLogger(__name__)

# Fields asked in order. my_team_name is handled specially (buttons).
# Each entry: (field_key in match_info, display label, prompt hint)
FIELD_SEQUENCE = [
    ("my_team_name", "My Team", None),
    ("opponent_team_name", "Opponent Team Name", "e.g. Rochester FC"),
    ("location", "Game Location", "e.g. home, away"),
]


class TeamInfoTask(BaseNtfyTask):
    """
    Interactive task that asks for missing match info fields one at a time.

    The task tracks which field is currently being asked via ``asking_field``.
    When a response arrives it writes the value to match_info.ini and returns
    ``should_continue=True`` with ``next_field`` metadata so the service can
    create the next question.
    """

    def __init__(
        self,
        group_dir: str,
        config: Config,
        ntfy_service: NtfyService,
        combined_video_path: str,
        existing_info: Optional[Dict[str, str]] = None,
        asking_field: Optional[str] = None,
    ):
        metadata = {
            "combined_video_path": combined_video_path,
            "existing_info": existing_info or {},
            "asking_field": asking_field,
            "config": {
                "ntfy": {
                    "topic": config.ntfy.topic,
                    "server_url": config.ntfy.server_url,
                    "enabled": config.ntfy.enabled,
                }
            },
        }
        super().__init__(group_dir, config, ntfy_service, metadata)
        self.combined_video_path = combined_video_path
        self.existing_info = existing_info or {}
        self.asking_field = asking_field

    def get_task_type(self) -> str:
        from .enums import NtfyInputType

        return NtfyInputType.TEAM_INFO.value

    # ------------------------------------------------------------------
    # Field helpers
    # ------------------------------------------------------------------

    def _get_missing_fields(self) -> List[str]:
        """Return field keys from FIELD_SEQUENCE that are still empty."""
        match_info = self._load_match_info()
        missing = []
        for field_key, _, _ in FIELD_SEQUENCE:
            val = getattr(match_info, field_key, "") if match_info else ""
            if not val or not val.strip():
                missing.append(field_key)
        return missing

    def _load_match_info(self) -> Optional[MatchInfo]:
        """Load the current MatchInfo for this group."""
        mi, _ = MatchInfo.get_or_create(self.group_dir)
        if mi:
            mi.group_dir = self.group_dir
        return mi

    def _next_field_to_ask(self) -> Optional[str]:
        """Return the first missing field key, or None if all filled.

        If my_team_name is missing but there's only one configured team,
        auto-fill it and skip to the next field.
        """
        missing = self._get_missing_fields()
        if not missing:
            return None

        # Auto-fill my_team_name if only one team is configured
        if missing[0] == "my_team_name":
            teams = self._get_configured_teams()
            if len(teams) == 1:
                MatchInfo.update_team_info(self.group_dir, {"my_team_name": teams[0]})
                logger.info(
                    f"Auto-filled my_team_name='{teams[0]}' for {self.group_dir}"
                )
                missing = missing[1:]

        return missing[0] if missing else None

    def _field_meta(self, field_key: str) -> Tuple[str, Optional[str]]:
        """Return (label, hint) for a field key."""
        for key, label, hint in FIELD_SEQUENCE:
            if key == field_key:
                return label, hint
        return field_key, None

    # ------------------------------------------------------------------
    # Team names from config
    # ------------------------------------------------------------------

    def _get_configured_teams(self) -> List[str]:
        """Get all configured team names from TeamSnap and PlayMetrics."""
        teams = []
        config = self.config
        # TeamSnap teams
        if hasattr(config, "teamsnap"):
            ts = config.teamsnap
            if hasattr(ts, "teams") and ts.teams:
                for team in ts.teams:
                    if team.team_name and team.team_name not in teams:
                        teams.append(team.team_name)
            elif hasattr(ts, "team_name") and ts.team_name:
                if ts.team_name not in teams:
                    teams.append(ts.team_name)
        # PlayMetrics teams
        if hasattr(config, "playmetrics"):
            pm = config.playmetrics
            if hasattr(pm, "teams") and pm.teams:
                for team in pm.teams:
                    if team.team_name and team.team_name not in teams:
                        teams.append(team.team_name)
            elif hasattr(pm, "team_name") and pm.team_name:
                if pm.team_name not in teams:
                    teams.append(pm.team_name)
        return teams

    async def _get_midpoint_screenshot(self) -> Optional[str]:
        """Take a screenshot from the middle of the combined video."""
        try:
            from video_grouper.utils.ffmpeg_utils import (
                get_video_duration,
                create_screenshot,
            )
            from datetime import datetime

            combined = get_combined_video_path(self.group_dir, self.storage_path)
            if not os.path.exists(combined):
                return None

            duration = await get_video_duration(combined)
            if not duration or duration <= 0:
                return None

            midpoint = duration / 2
            hours = int(midpoint // 3600)
            minutes = int((midpoint % 3600) // 60)
            seconds = int(midpoint % 60)
            offset_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

            group_dir_abs = resolve_path(self.group_dir, self.storage_path)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = os.path.join(group_dir_abs, f"team_screenshot_{ts}.jpg")

            success = await create_screenshot(combined, screenshot_path, offset_str)
            if success and os.path.exists(screenshot_path):
                return screenshot_path
        except Exception as e:
            logger.debug(f"Could not create team info screenshot: {e}")
        return None

    # ------------------------------------------------------------------
    # NTFY question / response
    # ------------------------------------------------------------------

    async def create_question(self) -> Dict[str, Any]:
        # Determine which field to ask about
        field_key = self.asking_field or self._next_field_to_ask()
        if not field_key:
            return {}  # nothing missing

        self.asking_field = field_key
        self.metadata["asking_field"] = field_key

        label, hint = self._field_meta(field_key)
        dir_name = os.path.basename(self.group_dir)

        # Build context line from existing match info
        match_info = self._load_match_info()
        context_parts = []
        if match_info and match_info.my_team_name.strip():
            context_parts.append(f"Team: {match_info.my_team_name}")
        if match_info and match_info.start_time_offset.strip():
            context_parts.append(f"Start: {match_info.start_time_offset}")
        context = " | ".join(context_parts)
        context_line = f"\n{context}" if context else ""

        # Special handling for my_team_name: show action buttons
        if field_key == "my_team_name":
            teams = self._get_configured_teams()
            ntfy_topic = self.config.ntfy.topic
            ntfy_url = f"{self.config.ntfy.server_url}/{ntfy_topic}"
            actions = []
            for team_name in teams:
                actions.append(
                    {
                        "action": "http",
                        "label": team_name,
                        "url": ntfy_url,
                        "method": "POST",
                        "headers": {"Content-Type": "text/plain"},
                        "body": team_name,
                        "clear": True,
                    }
                )
            # Add skip option for non-game recordings
            actions.append(
                {
                    "action": "http",
                    "label": "Skip - Not a Game",
                    "url": ntfy_url,
                    "method": "POST",
                    "headers": {"Content-Type": "text/plain"},
                    "body": "SKIP",
                    "clear": True,
                }
            )
            # Get a screenshot from the middle of the combined video
            image_path = await self._get_midpoint_screenshot()

            message = f"Which team is playing in {dir_name}?{context_line}"
            return {
                "message": message,
                "title": "Match Info - Select Team",
                "tags": ["question", "team_info"],
                "priority": 4,
                "image_path": image_path,
                "actions": actions,
            }

        # Default: free text reply
        hint_text = f" ({hint})" if hint else ""
        message = (
            f"What is the {label.lower()} for the game in {dir_name}?"
            f"{context_line}"
            f"\nReply with the value{hint_text}"
        )

        # For location, add home/away action buttons as shortcuts
        actions = []
        if field_key == "location":
            ntfy_topic = self.config.ntfy.topic
            ntfy_url = f"{self.config.ntfy.server_url}/{ntfy_topic}"
            for loc in ["Home", "Away"]:
                actions.append(
                    {
                        "action": "http",
                        "label": loc,
                        "url": ntfy_url,
                        "method": "POST",
                        "headers": {"Content-Type": "text/plain"},
                        "body": loc,
                        "clear": True,
                    }
                )

        return {
            "message": message,
            "title": f"Match Info - {label}",
            "tags": ["question", "team_info"],
            "priority": 4,
            "image_path": None,
            "actions": actions,
        }

    async def process_response(self, response: str) -> NtfyTaskResult:
        value = response.strip()
        if not value:
            return NtfyTaskResult(
                success=False,
                should_continue=True,
                message="Empty response, asking again",
            )

        # Handle skip/not-a-game response
        if value.upper() == "SKIP":
            from video_grouper.models.directory_state import DirectoryState

            try:
                dir_state = DirectoryState(self.group_dir, self.storage_path)
                dir_state.status = "skipped"
                dir_state.save()
                logger.info(f"Skipped group (not a game): {self.group_dir}")
            except Exception as e:
                logger.warning(f"Could not mark group as skipped: {e}")
            return NtfyTaskResult(
                success=True,
                should_continue=False,
                message=f"Skipped - not a game: {self.group_dir}",
            )

        field_key = self.asking_field or self.metadata.get("asking_field")
        if not field_key:
            field_key = self._next_field_to_ask()
            if not field_key:
                return NtfyTaskResult(
                    success=True,
                    should_continue=False,
                    message="All team info already filled",
                )

        # Write the value to match_info.ini
        update = {field_key: value}
        MatchInfo.update_team_info(self.group_dir, update)
        label, _ = self._field_meta(field_key)
        logger.info(f"Saved {label}='{value}' for {self.group_dir}")

        # Check if there's another field to ask
        next_field = self._next_field_to_ask()
        if next_field:
            return NtfyTaskResult(
                success=True,
                should_continue=True,
                message=f"Saved {label}, next: {next_field}",
                metadata={"next_field": next_field},
            )

        return NtfyTaskResult(
            success=True,
            should_continue=False,
            message="All team information completed",
        )

    # ------------------------------------------------------------------
    # Factory for continuation
    # ------------------------------------------------------------------

    @classmethod
    def create_next_task(
        cls, current_task: "TeamInfoTask", next_field: str
    ) -> "TeamInfoTask":
        """Create a follow-up task for the next missing field."""
        return cls(
            group_dir=current_task.group_dir,
            config=current_task.config,
            ntfy_service=current_task.ntfy_service,
            combined_video_path=current_task.combined_video_path,
            existing_info=current_task.existing_info,
            asking_field=next_field,
        )

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    @classmethod
    def deserialize(cls, data: Dict[str, Any]) -> "TeamInfoTask":
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TeamInfoTask":
        from video_grouper.utils.config import NtfyConfig

        ntfy_config_data = data.get("metadata", {}).get("config", {}).get("ntfy", {})
        ntfy_config = NtfyConfig(**ntfy_config_data)

        class MinimalConfig:
            def __init__(self, ntfy):
                self.ntfy = ntfy

        return cls(
            group_dir=data["group_dir"],
            config=MinimalConfig(ntfy_config),
            ntfy_service=None,
            combined_video_path=data.get("metadata", {}).get("combined_video_path", ""),
            existing_info=data.get("metadata", {}).get("existing_info", {}),
            asking_field=data.get("metadata", {}).get("asking_field"),
        )

    def get_missing_fields(self) -> List[str]:
        return self._get_missing_fields()
