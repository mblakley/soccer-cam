"""
TeamSnap service for match information lookup.
"""

import logging
from typing import Dict, List, Optional, Any
from datetime import datetime

from video_grouper.api_integrations.teamsnap import TeamSnapAPI
from video_grouper.utils.config import TeamSnapConfig

logger = logging.getLogger(__name__)


class TeamSnapService:
    """
    Service for TeamSnap API integration.
    Handles multiple team configurations and game lookups.
    """

    def __init__(self, configs: List[TeamSnapConfig]):
        """
        Initialize TeamSnap service.

        Args:
            configs: List of TeamSnap configuration objects
        """
        self.configs = configs
        self.teamsnap_apis = []
        self.enabled = False

        self._initialize_apis()

    def _initialize_apis(self) -> None:
        """Initialize TeamSnap API instances for all configured teams."""
        for config in self.configs:
            if config.enabled:
                try:
                    logger.info(
                        f"Initializing TeamSnap team: {config.my_team_name or 'Default'}"
                    )
                    api = TeamSnapAPI(config)
                    if api.enabled and api.access_token:
                        self.teamsnap_apis.append(api)
                        self.enabled = True
                        logger.info(
                            f"Successfully initialized TeamSnap API for {config.my_team_name or 'Default'}"
                        )
                    else:
                        logger.warning(
                            f"TeamSnap API initialization failed for {config.my_team_name or 'Default'} - no valid token"
                        )
                except Exception as e:
                    logger.error(
                        f"Error creating TeamSnap API for {config.my_team_name or 'Default'}: {e}"
                    )

        if self.enabled:
            logger.info(
                f"TeamSnap service enabled with {len(self.teamsnap_apis)} teams"
            )
        else:
            logger.info("TeamSnap service disabled - no valid configurations")

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

        for api in self.teamsnap_apis:
            try:
                game = api.find_game_for_recording(recording_start, recording_end)
                if game:
                    # Add source and team info
                    game["source"] = "TeamSnap"
                    game["team_name"] = api.my_team_name
                    logger.info(
                        f"Found TeamSnap game for team {api.my_team_name}: "
                        f"{game.get('team_name', 'Unknown')} vs {game.get('opponent_name', 'Unknown')}"
                    )
                    return game
            except Exception as e:
                logger.error(f"Error finding game in TeamSnap: {e}")

        return None

    def populate_match_info(
        self, group_dir: str, recording_start: datetime, recording_end: datetime
    ) -> bool:
        """
        Populate match_info.ini with TeamSnap data if a game is found.

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

            # Convert TeamSnap game to match info format
            team_info = {
                "my_team_name": game.get("team_name", ""),
                "opponent_team_name": game.get("opponent_name", ""),
                "location": game.get("location_name", ""),
            }

            # Update match info file
            MatchInfo.update_team_info(group_dir, team_info)
            logger.info(f"Updated match_info.ini with TeamSnap data for {group_dir}")
            return True

        except Exception as e:
            logger.error(f"Error updating match info with TeamSnap data: {e}")
            return False
