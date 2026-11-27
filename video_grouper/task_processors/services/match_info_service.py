"""
Match information service that coordinates between different data sources.
"""

import logging
import os
import re
from datetime import datetime
from typing import Any

from video_grouper.models import DirectoryState, MatchInfo
from video_grouper.schedule import IScheduleSource, ScheduledGame, pick_game

from .ntfy_service import NtfyService

logger = logging.getLogger(__name__)

# Reolink clip filenames look like:
#   RecM09_DST20260412_154649_155148_0_9D288300000000_1BFBD5ED.mp4
# Groups: (YYYYMMDD)(HHMMSS start)(HHMMSS end)
_REOLINK_CLIP_RE = re.compile(
    r"Rec\w+_DST(?P<date>\d{8})_(?P<start>\d{6})_(?P<end>\d{6})_"
)


class MatchInfoService:
    """
    Service for coordinating match information gathering from multiple sources.
    Handles API lookups, conflict resolution, and NTFY fallbacks.
    """

    def __init__(
        self,
        sources: list[IScheduleSource],
        ntfy_service: NtfyService,
    ):
        """
        Args:
            sources: Every schedule source to consult (see
                ``video_grouper.schedule.build_schedule_sources``). Any
                ``IScheduleSource`` works; this service knows none by name.
            ntfy_service: NTFY service (asks the user when no source matches).
        """
        self.sources: list[IScheduleSource] = sorted(sources, key=lambda s: s.priority)
        self.ntfy_service = ntfy_service

    def add_source(self, source: IScheduleSource) -> None:
        """Add a source that starts later (the TTT schedule, after sign-in)."""
        self.sources = sorted(
            [s for s in self.sources if s.name != source.name] + [source],
            key=lambda s: s.priority,
        )

    def _get_recording_timespan(
        self, group_dir: str
    ) -> tuple[datetime, datetime] | None:
        """
        Get the recording timespan for a group directory.

        Prefers ``DirectoryState.files`` (populated from camera metadata at
        download time), but falls back to scanning filenames on disk when
        state has been emptied (e.g. after the group transitions to
        ``combined`` and the per-file entries get dropped). The fallback is
        what lets a post-combine match_info re-query still succeed.

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

            if files:
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

            # Fallback: parse timestamps from filenames on disk. State is
            # empty post-combine, but the original clips are still present.
            logger.info("No files in directory state, falling back to filename parsing")
            return self._parse_timespan_from_filenames(group_dir)

        except Exception as e:
            logger.error(f"Error getting recording timespan for {group_dir}: {e}")
            return None

    def _parse_timespan_from_filenames(
        self, group_dir: str
    ) -> tuple[datetime, datetime] | None:
        """Scan ``group_dir`` for Reolink clip filenames and extract timespan.

        Reolink cameras produce files like
        ``RecM09_DST20260412_154649_155148_0_...mp4`` where the groups are
        ``YYYYMMDD``, ``HHMMSS`` start, ``HHMMSS`` end. We only need the
        earliest start and the latest end to bound the whole recording.
        """
        try:
            entries = os.listdir(group_dir)
        except OSError as e:
            logger.warning(f"Could not list {group_dir}: {e}")
            return None

        starts: list[datetime] = []
        ends: list[datetime] = []
        for name in entries:
            m = _REOLINK_CLIP_RE.match(name)
            if not m:
                continue
            try:
                start = datetime.strptime(
                    f"{m.group('date')}{m.group('start')}", "%Y%m%d%H%M%S"
                )
                end = datetime.strptime(
                    f"{m.group('date')}{m.group('end')}", "%Y%m%d%H%M%S"
                )
            except ValueError:
                continue
            starts.append(start)
            ends.append(end)

        if not starts:
            logger.warning(
                f"No Reolink clip filenames found in {group_dir} for timespan fallback"
            )
            return None

        recording_start = min(starts)
        recording_end = max(ends)
        logger.info(
            f"Recording timespan (from filenames): {recording_start} to {recording_end}"
        )
        return recording_start, recording_end

    def find_game(
        self, recording_start: datetime, recording_end: datetime
    ) -> ScheduledGame | None:
        """The scheduled game this recording is, from every enabled source.

        An authoritative source's match ends the search (the TTT schedule);
        otherwise the lowest-priority match wins. A failing source is logged
        and skipped.
        """
        logger.info(
            f"Looking up the scheduled game for {recording_start} to {recording_end}"
        )
        found: list[tuple[IScheduleSource, ScheduledGame]] = []
        for source in self.sources:
            if not source.enabled:
                continue
            try:
                game = source.find_game(recording_start, recording_end)
            except Exception as e:
                logger.error(f"Error getting game from {source.name}: {e}")
                continue
            if game is None:
                logger.info(f"No {source.name} game for the recording timespan")
                continue
            logger.info(
                f"Found {source.name} game: {game.team_name} vs {game.opponent}"
            )
            if source.authoritative:
                return game
            found.append((source, game))
        return pick_game(found)

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

        selected_game = self.find_game(recording_start, recording_end)
        if selected_game is None:
            logger.info(f"No scheduled game found for {group_dir}")
            return False

        team_info = selected_game.to_match_info()
        logger.info(f"Match info from {selected_game.source}: {team_info}")

        # Update match info file
        try:
            logger.info("Updating match_info.ini file...")
            MatchInfo.update_team_info(group_dir, team_info)
            source = selected_game.source
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
            logger.info(f"Successfully populated match info from APIs for {group_dir}")
            return True

        logger.info("API-based population failed or returned no data")

        # If APIs didn't work and NTFY is enabled, try NTFY
        logger.info(f"NTFY service enabled: {self.ntfy_service.enabled}")
        if self.ntfy_service.enabled:
            logger.info(f"=== Falling back to NTFY for {group_dir} ===")
            ntfy_success = await self.ntfy_service.process_combined_directory(
                group_dir, combined_path, force
            )

            if ntfy_success:
                logger.info(f"Initiated NTFY processing for {group_dir}")
                return True
            else:
                logger.warning(f"NTFY processing failed for {group_dir}")

        logger.warning(
            f"Could not process match info for {group_dir} - no APIs found games and NTFY not available"
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

    def get_pending_tasks(self) -> dict[str, dict[str, Any]]:
        """Get all pending tasks."""
        return self.ntfy_service.get_pending_tasks()

    async def is_match_info_complete(self, group_dir: str) -> bool:
        """
        Check if match info is complete for a directory.

        Args:
            group_dir: Directory path

        Returns:
            True if match info is complete, False otherwise
        """
        try:
            match_info_path = os.path.join(group_dir, "match_info.ini")
            if not os.path.exists(match_info_path):
                return False

            match_info = MatchInfo.from_file(match_info_path)
            return match_info and match_info.is_populated()
        except Exception as e:
            logger.error(f"Error checking match info completion for {group_dir}: {e}")
            return False

    async def shutdown(self) -> None:
        """Shutdown all services."""
        for source in self.sources:
            try:
                source.close()
            except Exception as e:
                logger.error(f"Error closing schedule source {source.name}: {e}")

        if self.ntfy_service:
            await self.ntfy_service.shutdown()
