"""
Match information model for soccer game metadata.
"""

import os
import re
import logging
import configparser
from datetime import datetime
from typing import Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass  # Removed frozen=True to allow direct field assignment
class MatchInfo:
    """Represents match information from match_info.ini file."""

    my_team_name: str
    opponent_team_name: str
    location: str
    start_time_offset: str = ""
    total_duration: str = ""
    date: Optional[datetime] = None

    def __hash__(self):
        """Make MatchInfo hashable so it can be used in sets and as dictionary keys."""
        return hash(
            (
                self.my_team_name,
                self.opponent_team_name,
                self.location,
                self.start_time_offset,
                self.total_duration,
            )
        )

    def __eq__(self, other):
        """Define equality for MatchInfo objects."""
        if not isinstance(other, MatchInfo):
            return False
        return (
            self.my_team_name == other.my_team_name
            and self.opponent_team_name == other.opponent_team_name
            and self.location == other.location
            and self.start_time_offset == other.start_time_offset
            and self.total_duration == other.total_duration
        )

    @classmethod
    def from_config(cls, config: configparser.ConfigParser) -> Optional["MatchInfo"]:
        """Create a MatchInfo object from a ConfigParser object.

        Args:
            config: The ConfigParser object containing match information

        Returns:
            A MatchInfo object or None if the config is invalid
        """
        try:
            if not config.has_section("MATCH"):
                return None

            return cls(
                my_team_name=config.get("MATCH", "my_team_name", fallback=""),
                opponent_team_name=config.get(
                    "MATCH", "opponent_team_name", fallback=""
                ),
                location=config.get("MATCH", "location", fallback=""),
                start_time_offset=config.get("MATCH", "start_time_offset", fallback=""),
                total_duration=config.get("MATCH", "total_duration", fallback=""),
            )
        except (configparser.Error, KeyError) as e:
            logger.error(f"Error creating MatchInfo from config: {e}")
            return None

    @classmethod
    def from_file(cls, file_path: str) -> Optional["MatchInfo"]:
        """Create a MatchInfo object from a match_info.ini file.

        Args:
            file_path: The path to the match_info.ini file

        Returns:
            A MatchInfo object or None if the file is invalid
        """
        if not os.path.exists(file_path):
            logger.error(f"Match info file not found: {file_path}")
            return None

        config = configparser.ConfigParser()
        try:
            read_files = config.read(file_path)
            if not read_files:
                logger.error(f"Failed to read match info file: {file_path}")
                return None

            return cls.from_config(config)
        except configparser.Error as e:
            logger.error(f"Error parsing match info file {file_path}: {e}")
            return None

    @classmethod
    def get_or_create(
        cls, group_dir: str
    ) -> Tuple[Optional["MatchInfo"], configparser.ConfigParser]:
        """Get an existing MatchInfo object or create a new one with default values.

        Args:
            group_dir: The group directory path

        Returns:
            A tuple of (MatchInfo object or None, ConfigParser object)
        """
        match_info_path = os.path.join(group_dir, "match_info.ini")
        config = configparser.ConfigParser()

        # Create the file if it doesn't exist
        if not os.path.exists(match_info_path):
            if not os.path.exists(group_dir):
                os.makedirs(group_dir)

            # Try to copy from dist file if available
            source_dist_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "match_info.ini.dist",
            )
            if os.path.exists(source_dist_path):
                try:
                    with (
                        open(source_dist_path, "r") as src,
                        open(match_info_path, "w") as dest,
                    ):
                        dest.write(src.read())
                except Exception as e:
                    logger.error(f"Failed to create match_info.ini from dist: {e}")
            else:
                # Create empty file
                with open(match_info_path, "w") as f:
                    config.write(f)

        # Read the config
        config.read(match_info_path)

        # Ensure the MATCH section exists
        if "MATCH" not in config:
            config["MATCH"] = {}
            with open(match_info_path, "w") as f:
                config.write(f)

        # Create the MatchInfo object
        match_info = cls.from_config(config)
        return match_info, config

    @classmethod
    def update_team_info(cls, group_dir: str, team_info: dict) -> Optional["MatchInfo"]:
        """Update team information in the match_info.ini file.

        Args:
            group_dir: The group directory path
            team_info: Dictionary with team information (team_name, opponent_name, location)

        Returns:
            Updated MatchInfo object or None if the update failed
        """
        match_info_path = os.path.join(group_dir, "match_info.ini")
        match_info, config = cls.get_or_create(group_dir)

        # Update the config with team information
        if team_info:
            if "my_team_name" in team_info:
                config["MATCH"]["my_team_name"] = team_info["my_team_name"]
                logger.info(
                    f"Updated match_info.ini with my_team_name: {team_info['my_team_name']}"
                )

            if "opponent_team_name" in team_info:
                config["MATCH"]["opponent_team_name"] = team_info["opponent_team_name"]
                logger.info(
                    f"Updated match_info.ini with opponent_team_name: {team_info['opponent_team_name']}"
                )

            if "location" in team_info:
                config["MATCH"]["location"] = team_info["location"]
                logger.info(
                    f"Updated match_info.ini with location: {team_info['location']}"
                )

        # Ensure required fields exist with default values if not set
        if "my_team_name" not in config["MATCH"]:
            config["MATCH"]["my_team_name"] = "My Team"
        if "opponent_team_name" not in config["MATCH"]:
            config["MATCH"]["opponent_team_name"] = "Opponent"
        if "location" not in config["MATCH"]:
            config["MATCH"]["location"] = "Location"

        # Write the updated config back to the file
        with open(match_info_path, "w") as f:
            config.write(f)

        # Return the updated MatchInfo object
        return cls.from_config(config)

    @classmethod
    def update_game_times(
        cls,
        group_dir: str,
        start_time_offset: Optional[str] = None,
        total_duration: Optional[str] = None,
    ) -> Optional["MatchInfo"]:
        """Update game timing information in the match_info.ini file.

        Args:
            group_dir: The group directory path
            start_time_offset: The start time offset string (e.g., "00:05:30")
            total_duration: The total duration string (e.g., "01:30:00")

        Returns:
            Updated MatchInfo object or None if the update failed
        """
        match_info_path = os.path.join(group_dir, "match_info.ini")
        match_info, config = cls.get_or_create(group_dir)

        # Update the config with timing information
        if start_time_offset is not None:
            config["MATCH"]["start_time_offset"] = start_time_offset
            logger.info(
                f"Updated match_info.ini with start_time_offset: {start_time_offset}"
            )

        if total_duration is not None:
            config["MATCH"]["total_duration"] = total_duration
            logger.info(f"Updated match_info.ini with total_duration: {total_duration}")

        # Write the updated config back to the file
        with open(match_info_path, "w") as f:
            config.write(f)

        # Return the updated MatchInfo object
        return cls.from_config(config)

    def get_team_info(self) -> dict:
        """Get team information as a dictionary (keys expected by tests)."""
        return {
            "team_name": self.my_team_name,
            "opponent_name": self.opponent_team_name,
            "location": self.location,
        }

    def is_populated(self) -> bool:
        """Check if all required fields are populated.

        Returns:
            True if all fields are filled, False otherwise
        """
        required_fields = [
            self.my_team_name,
            self.opponent_team_name,
            self.location,
            self.start_time_offset,
        ]
        return all(field.strip() for field in required_fields)

    def get_total_duration_seconds(self) -> int:
        """Get the total duration in seconds.

        Returns:
            Total duration in seconds, or 0 if not set or invalid
        """
        if not self.total_duration:
            return 90 * 60  # default
        try:
            # Parse duration in format HH:MM:SS
            parts = self.total_duration.split(":")
            if len(parts) == 3:
                hours, minutes, seconds = map(int, parts)
                return hours * 3600 + minutes * 60 + seconds
            elif len(parts) == 2:
                minutes, seconds = map(int, parts)
                return minutes * 60 + seconds
            else:
                return int(self.total_duration)
        except (ValueError, TypeError):
            logger.warning(f"Invalid total_duration format: {self.total_duration}")
            return 90 * 60

    def get_start_offset(self) -> str:
        """Get the start offset in a standard format.

        Returns:
            Start offset string in HH:MM:SS format
        """
        if not self.start_time_offset:
            return ""
        parts = self.start_time_offset.split(":")
        if len(parts) == 2:
            return f"00:{self.start_time_offset}"
        elif len(parts) == 3:
            return self.start_time_offset
        else:
            return ""

    def get_sanitized_names(self) -> Tuple[str, str, str]:
        """Get sanitized team names and location for use in file names.

        Returns:
            Tuple of (my_team_name, opponent_team_name, location) with special characters removed
        """

        def sanitize(name: str) -> str:
            # Remove or replace special characters that aren't valid in file names
            return re.sub(r'[<>:"/\\|?*]', "_", name)

        return (
            sanitize(self.my_team_name),
            sanitize(self.opponent_team_name),
            sanitize(self.location),
        )

    def get_youtube_title(self, video_type: str) -> str:
        """Generate a YouTube title for the video.

        Args:
            video_type: Type of video ("processed", "raw", etc.)

        Returns:
            YouTube title string
        """
        suffix = " - Full Field" if video_type == "raw" else ""
        return f"{self.my_team_name} vs {self.opponent_team_name}{suffix}"

    def get_youtube_description(self, video_type: str) -> str:
        """Generate a YouTube description for the video.

        Args:
            video_type: Type of video ("processed", "raw", etc.)

        Returns:
            YouTube description string
        """
        description_parts = [
            f"Soccer match: {self.my_team_name} vs {self.opponent_team_name}",
            f"Location: {self.location}",
        ]

        if video_type == "raw":
            description_parts.append("Full field view - unedited footage")
        else:
            description_parts.append("Processed with automated camera tracking")

        return "\n".join(description_parts)

    def save(self) -> None:
        """Save the current state back to the match_info.ini file."""
        match_info_path = (
            os.path.join(self.group_dir, "match_info.ini")
            if hasattr(self, "group_dir")
            else None
        )
        if not match_info_path:
            # If we don't have group_dir, we can't save
            logger.warning("Cannot save MatchInfo: no group_dir available")
            return

        config = configparser.ConfigParser()
        config["MATCH"] = {
            "my_team_name": self.my_team_name,
            "opponent_team_name": self.opponent_team_name,
            "location": self.location,
            "start_time_offset": self.start_time_offset,
            "total_duration": self.total_duration,
        }

        with open(match_info_path, "w") as f:
            config.write(f)

        logger.info(
            f"Saved match_info.ini with start_time_offset: {self.start_time_offset}"
        )
