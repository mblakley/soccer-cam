"""
Match information service that coordinates between different data sources.
"""

import logging
import os
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime

from .teamsnap_service import TeamSnapService
from .playmetrics_service import PlayMetricsService
from .ntfy_service import NtfyService
from video_grouper.models import DirectoryState
from video_grouper.models import MatchInfo

logger = logging.getLogger(__name__)


class MatchInfoService:
    """
    Service for coordinating match information gathering from multiple sources.
    Handles API lookups, conflict resolution, and NTFY fallbacks.
    """

    def __init__(
        self,
        teamsnap_service: TeamSnapService,
        playmetrics_service: PlayMetricsService,
        ntfy_service: NtfyService,
    ):
        """
        Initialize match info service.

        Args:
            teamsnap_service: TeamSnap service instance
            playmetrics_service: PlayMetrics service instance
            ntfy_service: NTFY service instance
        """
        self.teamsnap_service = teamsnap_service
        self.playmetrics_service = playmetrics_service
        self.ntfy_service = ntfy_service

    def _get_recording_timespan(
        self, group_dir: str
    ) -> Optional[Tuple[datetime, datetime]]:
        """
        Get the recording timespan from directory state.

        Args:
            group_dir: Directory path

        Returns:
            Tuple of (start_time, end_time) or None if not available
        """
        logger.info(f"Getting recording timespan for directory: {group_dir}")
        try:
            dir_state = DirectoryState(group_dir)

            # Get files from the files attribute
            files = list(dir_state.files.values())
            logger.info(f"Found {len(files)} files in directory state")

            if not files:
                logger.warning("No files found in directory state")
                return None

            # Sort files by start time to get first and last
            files.sort(key=lambda f: f.start_time)
            first_file = files[0]
            last_file = files[-1]

            logger.info(
                f"First file: {first_file.file_path} at {first_file.start_time}"
            )
            logger.info(
                f"Last file: {last_file.file_path} at {last_file.end_time or last_file.start_time}"
            )

            recording_start = first_file.start_time
            recording_end = last_file.end_time or last_file.start_time

            logger.info(f"Recording timespan: {recording_start} to {recording_end}")
            return recording_start, recording_end

        except Exception as e:
            logger.error(f"Error getting recording timespan for {group_dir}: {e}")
            return None

    def _collect_games_from_apis(
        self, recording_start: datetime, recording_end: datetime
    ) -> List[Dict[str, Any]]:
        """
        Collect games from all available APIs.

        Args:
            recording_start: Start time of recording
            recording_end: End time of recording

        Returns:
            List of game dictionaries from all sources
        """
        logger.info(
            f"Collecting games from APIs for timespan: {recording_start} to {recording_end}"
        )
        games = []

        # Try TeamSnap
        logger.info(f"TeamSnap service enabled: {self.teamsnap_service.enabled}")
        if self.teamsnap_service.enabled:
            try:
                logger.info("Querying TeamSnap for games...")
                teamsnap_game = self.teamsnap_service.find_game_for_recording(
                    recording_start, recording_end
                )
                if teamsnap_game:
                    logger.info(
                        f"Found TeamSnap game: {teamsnap_game.get('team_name', 'Unknown')} vs {teamsnap_game.get('opponent_name', 'Unknown')} at {teamsnap_game.get('location_name', 'Unknown')}"
                    )
                    games.append(teamsnap_game)
                else:
                    logger.info("No TeamSnap game found for the recording timespan")
            except Exception as e:
                logger.error(f"Error getting game from TeamSnap: {e}")

        # Try PlayMetrics
        logger.info(f"PlayMetrics service enabled: {self.playmetrics_service.enabled}")
        if self.playmetrics_service.enabled:
            try:
                logger.info("Querying PlayMetrics for games...")
                playmetrics_game = self.playmetrics_service.find_game_for_recording(
                    recording_start, recording_end
                )
                if playmetrics_game:
                    logger.info(
                        f"Found PlayMetrics game: {playmetrics_game.get('team_name', 'Unknown')} vs {playmetrics_game.get('opponent', 'Unknown')} at {playmetrics_game.get('location', 'Unknown')}"
                    )
                    games.append(playmetrics_game)
                else:
                    logger.info("No PlayMetrics game found for the recording timespan")
            except Exception as e:
                logger.error(f"Error getting game from PlayMetrics: {e}")

        logger.info(f"Total games found from APIs: {len(games)}")
        for i, game in enumerate(games):
            logger.info(f"Game {i + 1}: {game}")

        return games

    def _select_best_game(
        self, games: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """
        Select the best game from multiple options.

        Args:
            games: List of game dictionaries

        Returns:
            Best game dictionary or None
        """
        if not games:
            return None

        if len(games) == 1:
            return games[0]

        # For now, prefer TeamSnap over PlayMetrics
        # TODO: Implement more sophisticated selection logic
        for game in games:
            if game.get("source") == "TeamSnap":
                logger.info(
                    f"Selected TeamSnap game over {len(games) - 1} other options"
                )
                return game

        # Fall back to first game
        logger.info(f"Selected first game from {len(games)} options")
        return games[0]

    def _convert_game_to_match_info(self, game: Dict[str, Any]) -> Dict[str, str]:
        """
        Convert a game dictionary to match info format.

        Args:
            game: Game dictionary from API

        Returns:
            Match info dictionary
        """
        logger.info(f"Converting game to match info: {game}")
        source = game.get("source", "Unknown")
        logger.info(f"Game source: {source}")

        if source == "TeamSnap":
            team_info = {
                "my_team_name": game.get("team_name", ""),
                "opponent_team_name": game.get("opponent_name", ""),
                "location": game.get("location_name", ""),
            }
            logger.info(f"TeamSnap conversion result: {team_info}")
            return team_info
        elif source == "PlayMetrics":
            match_info = {
                "my_team_name": game.get("team_name", ""),
                "opponent_team_name": game.get("opponent", ""),
                "location": game.get("location", ""),
            }

            # Add date/time if available
            if "start_time" in game and game["start_time"]:
                try:
                    start_time = game["start_time"]
                    match_info["date"] = start_time.strftime("%Y-%m-%d")
                    match_info["time"] = start_time.strftime("%H:%M")
                    logger.info(
                        f"Added date/time from PlayMetrics: {match_info['date']} {match_info['time']}"
                    )
                except Exception as e:
                    logger.warning(f"Error adding date/time from PlayMetrics: {e}")

            logger.info(f"PlayMetrics conversion result: {match_info}")
            return match_info

        logger.warning(f"Unknown game source: {source}")
        return {}

    async def ensure_match_info_exists(self, group_dir: str) -> bool:
        """
        Ensure match_info.ini exists in the group directory.

        Args:
            group_dir: Directory path

        Returns:
            True if match info file was created/updated, False otherwise
        """
        try:
            match_info, config = MatchInfo.get_or_create(group_dir)
            return True
        except Exception as e:
            logger.error(f"Error ensuring match info exists for {group_dir}: {e}")
            return False

    async def populate_match_info_from_apis(self, group_dir: str) -> bool:
        """
        Populate match info from API sources if not already populated.

        Args:
            group_dir: Directory path

        Returns:
            True if match info was populated, False otherwise
        """
        logger.info(f"Attempting to populate match info from APIs for: {group_dir}")

        # Check if already populated
        match_info_path = os.path.join(group_dir, "match_info.ini")
        logger.info(f"Checking for existing match_info.ini at: {match_info_path}")

        if os.path.exists(match_info_path):
            logger.info("match_info.ini file exists, checking if populated...")
            match_info = MatchInfo.from_file(match_info_path)
            if match_info and match_info.is_populated():
                logger.info(
                    f"Match info already populated for {group_dir}, skipping API lookup"
                )
                return False
            else:
                logger.info("match_info.ini exists but not populated, will try APIs")
        else:
            logger.info("No match_info.ini found, will create from API data")

        # Get recording timespan
        logger.info("Determining recording timespan...")
        timespan = self._get_recording_timespan(group_dir)
        if not timespan:
            logger.warning(f"Could not determine recording timespan for {group_dir}")
            return False

        recording_start, recording_end = timespan
        logger.info(
            f"Recording timespan determined: {recording_start} to {recording_end}"
        )

        # Collect games from APIs
        logger.info("Collecting games from APIs...")
        games = self._collect_games_from_apis(recording_start, recording_end)

        if not games:
            logger.info(f"No games found from APIs for {group_dir}")
            return False

        # Select best game
        logger.info("Selecting best game from available options...")
        selected_game = self._select_best_game(games)
        if not selected_game:
            logger.warning("No game selected from available options")
            return False

        logger.info(f"Selected game: {selected_game}")

        # Convert to match info format
        logger.info("Converting game data to match info format...")
        team_info = self._convert_game_to_match_info(selected_game)
        logger.info(f"Converted team info: {team_info}")

        # Update match info file
        try:
            logger.info("Updating match_info.ini file...")
            MatchInfo.update_team_info(group_dir, team_info)
            source = selected_game.get("source", "Unknown")
            logger.info(
                f"Successfully updated match_info.ini with {source} data for {group_dir}: "
                f"{team_info.get('my_team_name', 'Unknown')} vs {team_info.get('opponent_team_name', 'Unknown')}"
            )
            return True
        except Exception as e:
            logger.error(f"Error updating match info for {group_dir}: {e}")
            return False

    async def process_combined_directory(
        self, group_dir: str, combined_path: str, force: bool = False
    ) -> bool:
        """
        Process a combined directory for match information.

        Args:
            group_dir: Directory path
            combined_path: Path to combined video
            force: Force processing even if already done

        Returns:
            True if processing was successful or initiated, False otherwise
        """
        logger.info(f"=== Starting match info processing for: {group_dir} ===")
        logger.info(f"Combined video path: {combined_path}")
        logger.info(f"Force processing: {force}")

        # First, ensure match_info.ini exists
        logger.info("Ensuring match_info.ini exists...")
        await self.ensure_match_info_exists(group_dir)

        # Try to populate from APIs first
        logger.info("=== Attempting API-based match info population ===")
        api_success = await self.populate_match_info_from_apis(group_dir)

        if api_success:
            logger.info(
                f"✓ Successfully populated match info from APIs for {group_dir}"
            )
            return True

        logger.info("✗ API-based population failed or returned no data")

        # If APIs didn't work and NTFY is enabled, try NTFY
        logger.info(f"NTFY service enabled: {self.ntfy_service.enabled}")
        if self.ntfy_service.enabled:
            logger.info(f"=== Falling back to NTFY for {group_dir} ===")
            ntfy_success = await self.ntfy_service.process_combined_directory(
                group_dir, combined_path, force
            )

            if ntfy_success:
                logger.info(f"✓ Initiated NTFY processing for {group_dir}")
                return True
            else:
                logger.warning(f"✗ NTFY processing failed for {group_dir}")

        logger.warning(
            f"✗ Could not process match info for {group_dir} - no APIs found games and NTFY not available"
        )
        return False

    def is_waiting_for_user_input(self, group_dir: str) -> bool:
        """
        Check if we're waiting for user input for a directory.

        Args:
            group_dir: Directory path

        Returns:
            True if waiting for user input, False otherwise
        """
        return self.ntfy_service.is_waiting_for_input(group_dir)

    def get_pending_inputs(self) -> Dict[str, Dict[str, Any]]:
        """Get all pending user inputs."""
        return self.ntfy_service.get_pending_inputs()

    async def shutdown(self) -> None:
        """Shutdown all services."""
        if self.playmetrics_service:
            self.playmetrics_service.close()

        if self.ntfy_service:
            await self.ntfy_service.shutdown()
