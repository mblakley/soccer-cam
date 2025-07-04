"""
PlayMetrics service for match information lookup.
"""

import logging
from typing import Dict, List, Optional, Any
from datetime import datetime

from video_grouper.api_integrations.playmetrics import PlayMetricsAPI
from video_grouper.utils.config import PlayMetricsConfig

logger = logging.getLogger(__name__)


class PlayMetricsService:
    """
    Service for PlayMetrics API integration.
    Handles multiple team configurations and game lookups.
    """

    def __init__(self, configs: List[PlayMetricsConfig], app_config=None):
        """
        Initialize PlayMetrics service.

        Args:
            configs: List of PlayMetrics configuration objects
            app_config: Application configuration object containing timezone settings
        """
        self.configs = configs
        self.app_config = app_config
        self.playmetrics_apis = []
        self.enabled = False

        self._initialize_apis()

    def _initialize_apis(self) -> None:
        """Initialize PlayMetrics API instances for all configured teams."""
        for config in self.configs:
            if config.enabled:
                try:
                    logger.info(
                        f"Initializing PlayMetrics team: {config.team_name or 'Default'}"
                    )
                    api = PlayMetricsAPI(config, self.app_config)
                    if api.login():
                        self.playmetrics_apis.append(api)
                        self.enabled = True
                except Exception as e:
                    logger.error(
                        f"Error creating PlayMetrics API for {config.team_name or 'Default'}: {e}"
                    )

        if self.enabled:
            logger.info(
                f"PlayMetrics service enabled with {len(self.playmetrics_apis)} teams"
            )
        else:
            logger.info("PlayMetrics service disabled - no valid configurations")

    def find_game_for_recording(
        self, recording_start: datetime, recording_end: datetime
    ) -> Optional[Dict[str, Any]]:
        """
        Find a game that matches the recording timespan.

        Args:
            recording_start: Start time of recording
            recording_end: End time of recording

        Returns:
            Game information dict or None if not found
        """
        if not self.enabled:
            return None

        for api in self.playmetrics_apis:
            try:
                game = api.find_game_for_recording(recording_start, recording_end)
                if game:
                    # Add source and team info
                    game["source"] = "PlayMetrics"
                    game["team_name"] = api.team_name
                    logger.info(
                        f"Found PlayMetrics game for team {api.team_name}: "
                        f"{game.get('title', 'Unknown')} vs {game.get('opponent', 'Unknown')}"
                    )
                    return game
            except Exception as e:
                logger.error(f"Error finding game in PlayMetrics: {e}")

        return None

    def populate_match_info(
        self, group_dir: str, recording_start: datetime, recording_end: datetime
    ) -> bool:
        """
        Populate match_info.ini with PlayMetrics data if a game is found.

        Args:
            group_dir: Directory to create match_info.ini in
            recording_start: Start time of recording
            recording_end: End time of recording

        Returns:
            True if match info was populated, False otherwise
        """
        game = self.find_game_for_recording(recording_start, recording_end)
        if not game:
            return False

        try:
            from video_grouper.models import MatchInfo

            # Convert PlayMetrics game to match info format
            team_info = {
                "my_team_name": game.get("team_name", ""),
                "opponent_team_name": game.get("opponent", ""),
                "location": game.get("location", ""),
            }

            # Add date/time if available
            if "start_time" in game and game["start_time"]:
                try:
                    start_time = game["start_time"]
                    team_info["date"] = start_time.strftime("%Y-%m-%d")
                    team_info["time"] = start_time.strftime("%H:%M")
                except Exception:
                    pass

            # Update match info file
            MatchInfo.update_team_info(group_dir, team_info)
            logger.info(f"Updated match_info.ini with PlayMetrics data for {group_dir}")
            return True

        except Exception as e:
            logger.error(f"Error updating match info with PlayMetrics data: {e}")
            return False

    def close(self) -> None:
        """Close all PlayMetrics API connections."""
        for api in self.playmetrics_apis:
            try:
                api.close()
            except Exception as e:
                logger.error(f"Error closing PlayMetrics API: {e}")
