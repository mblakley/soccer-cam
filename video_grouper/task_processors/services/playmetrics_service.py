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

    def __init__(self, config, app_config=None):
        """
        Initialize PlayMetrics service.

        Args:
            config: PlayMetrics configuration object containing credentials and teams
            app_config: Application configuration object containing timezone settings
        """
        # Accept either a single PlayMetricsConfig or a list for backward compatibility
        if isinstance(config, list):
            self.configs = config
        else:
            self.configs = [config]

        # Use the first config as canonical for credentials if needed
        self.config = self.configs[0] if self.configs else None
        self.app_config = app_config
        self.playmetrics_apis = []
        self.enabled = False

        self._initialize_apis()

    def _initialize_apis(self) -> None:
        """Initialize PlayMetrics API instances for all configured teams. No fallback logic."""
        errors = []
        for cfg in self.configs:
            if not cfg or not hasattr(cfg, 'teams'):
                logger.warning("No PlayMetrics teams found in config.")
                continue

            for team in cfg.teams:
                if not team.enabled:
                    continue
                try:
                    logger.info(f"Initializing PlayMetrics team: {team.team_name or 'Unknown'}")
                    # Create a config for this team using main credentials and team info
                    # We need to create a config object that has team_id and team_name as attributes
                    # Since PlayMetricsConfig doesn't have these fields, we'll create a simple object
                    class TeamConfig:
                        def __init__(self, enabled, username, password, team_id, team_name):
                            self.enabled = enabled
                            self.username = username
                            self.password = password
                            self.team_id = team_id
                            self.team_name = team_name
                    
                    team_config = TeamConfig(
                        enabled=cfg.enabled,
                        username=cfg.username,
                        password=cfg.password,
                        team_id=team.team_id,
                        team_name=team.team_name
                    )
                    api = PlayMetricsAPI(team_config, self.app_config)
                    if api.login():
                        self.playmetrics_apis.append(api)
                        logger.info(f"Successfully initialized PlayMetrics API for {team.team_name or 'Unknown'}")
                    else:
                        error_msg = f"Failed to log in to PlayMetrics for team {team.team_name or 'Unknown'}"
                        logger.error(error_msg)
                        errors.append(error_msg)
                except Exception as e:
                    error_msg = f"Error creating PlayMetrics API for {team.team_name or 'Unknown'}: {e}"
                    logger.error(error_msg)
                    errors.append(error_msg)
        if not self.playmetrics_apis:
            error_summary = "\n".join(errors) if errors else "No valid PlayMetrics team configurations found."
            logger.warning(f"PlayMetrics service unavailable. Errors:\n{error_summary}")
            self.enabled = False
        else:
            self.enabled = True
            logger.info(
                f"PlayMetrics service enabled with {len(self.playmetrics_apis)} teams"
            )

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
