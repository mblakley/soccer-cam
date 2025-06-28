from datetime import datetime, timedelta
from typing import Optional, Any, TypedDict, Tuple, Union, List
import logging
import configparser
import os
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

class ConnectionEvent(TypedDict):
    """Represents a single camera connection event."""
    event_datetime: str
    event_type: str # "connected" or "disconnected"
    message: str

@dataclass(frozen=True)  # frozen=True makes the dataclass immutable and hashable
class MatchInfo:
    """Represents match information from match_info.ini file."""
    my_team_name: str
    opponent_team_name: str
    location: str
    start_time_offset: str = '00:00:00'
    total_duration: str = '01:30:00'
    
    def __hash__(self):
        """Make MatchInfo hashable so it can be used in sets and as dictionary keys."""
        return hash((self.my_team_name, self.opponent_team_name, self.location, 
                    self.start_time_offset, self.total_duration))
    
    def __eq__(self, other):
        """Define equality for MatchInfo objects."""
        if not isinstance(other, MatchInfo):
            return False
        return (self.my_team_name == other.my_team_name and
                self.opponent_team_name == other.opponent_team_name and
                self.location == other.location and
                self.start_time_offset == other.start_time_offset and
                self.total_duration == other.total_duration)
    
    @classmethod
    def from_config(cls, config: configparser.ConfigParser) -> Optional['MatchInfo']:
        """Create a MatchInfo object from a ConfigParser object.
        
        Args:
            config: The ConfigParser object containing match information
            
        Returns:
            A MatchInfo object or None if the config is invalid
        """
        try:
            if not config.has_section('MATCH'):
                return None
            
            return cls(
                my_team_name=config.get('MATCH', 'my_team_name'),
                opponent_team_name=config.get('MATCH', 'opponent_team_name'),
                location=config.get('MATCH', 'location'),
                start_time_offset=config.get('MATCH', 'start_time_offset', fallback='00:00:00'),
                total_duration=config.get('MATCH', 'total_duration', fallback='01:30:00')
            )
        except (configparser.Error, KeyError) as e:
            logger.error(f"Error creating MatchInfo from config: {e}")
            return None
    
    @classmethod
    def from_file(cls, file_path: str) -> Optional['MatchInfo']:
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
    def get_or_create(cls, group_dir: str) -> Tuple[Optional['MatchInfo'], configparser.ConfigParser]:
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
            source_dist_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "match_info.ini.dist")
            if os.path.exists(source_dist_path):
                try:
                    with open(source_dist_path, 'r') as src, open(match_info_path, 'w') as dest:
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
    def update_team_info(cls, group_dir: str, team_info: dict) -> Optional['MatchInfo']:
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
            if 'team_name' in team_info:
                config["MATCH"]["my_team_name"] = team_info['team_name']
                logger.info(f"Updated match_info.ini with team_name: {team_info['team_name']}")
                
            if 'opponent_name' in team_info:
                config["MATCH"]["opponent_team_name"] = team_info['opponent_name']
                logger.info(f"Updated match_info.ini with opponent_name: {team_info['opponent_name']}")
                
            if 'location' in team_info:
                config["MATCH"]["location"] = team_info['location']
                logger.info(f"Updated match_info.ini with location: {team_info['location']}")
        
        # Ensure required fields exist with default values if not set
        if "my_team_name" not in config["MATCH"]:
            config["MATCH"]["my_team_name"] = "My Team"
        if "opponent_team_name" not in config["MATCH"]:
            config["MATCH"]["opponent_team_name"] = "Opponent"
        if "location" not in config["MATCH"]:
            config["MATCH"]["location"] = "Unknown"
        if "start_time_offset" not in config["MATCH"]:
            config["MATCH"]["start_time_offset"] = "00:00:00"
        if "total_duration" not in config["MATCH"]:
            config["MATCH"]["total_duration"] = "01:30:00"
        
        # Save the config
        with open(match_info_path, "w") as f:
            config.write(f)
        
        # Return the updated MatchInfo object
        return cls.from_file(match_info_path)
    
    @classmethod
    def update_game_times(cls, group_dir: str, start_time_offset: Optional[str] = None, 
                         total_duration: Optional[str] = None) -> Optional['MatchInfo']:
        """Update game time information in the match_info.ini file.
        
        Args:
            group_dir: The group directory path
            start_time_offset: The start time offset (HH:MM:SS format)
            total_duration: The total duration (HH:MM:SS format)
            
        Returns:
            Updated MatchInfo object or None if the update failed
        """
        match_info_path = os.path.join(group_dir, "match_info.ini")
        match_info, config = cls.get_or_create(group_dir)
        
        # Update the config with game time information
        if start_time_offset:
            config["MATCH"]["start_time_offset"] = start_time_offset
            logger.info(f"Updated match_info.ini with start_time_offset: {start_time_offset}")
            
        if total_duration:
            config["MATCH"]["total_duration"] = total_duration
            logger.info(f"Updated match_info.ini with total_duration: {total_duration}")
        
        # Save the config
        with open(match_info_path, "w") as f:
            config.write(f)
        
        # Ensure required fields exist with default values if not set
        if "my_team_name" not in config["MATCH"]:
            config["MATCH"]["my_team_name"] = "My Team"
        if "opponent_team_name" not in config["MATCH"]:
            config["MATCH"]["opponent_team_name"] = "Opponent"
        if "location" not in config["MATCH"]:
            config["MATCH"]["location"] = "Unknown"
            
        # Save the config again with defaults
        with open(match_info_path, "w") as f:
            config.write(f)
        
        # Return the updated MatchInfo object
        return cls.from_file(match_info_path)
    
    def get_team_info(self) -> dict:
        """Get team information as a dictionary.
        
        Returns:
            Dictionary with team information
        """
        return {
            'team_name': self.my_team_name,
            'opponent_name': self.opponent_team_name,
            'location': self.location
        }
    
    def is_populated(self) -> bool:
        """Check if the match info is populated with non-default values.
        
        Returns:
            True if all required fields are populated, False otherwise
        """
        required_fields = [self.my_team_name, self.opponent_team_name, self.location, self.start_time_offset]
        return all(field.strip() for field in required_fields)
    
    def get_total_duration_seconds(self) -> int:
        """Convert total_duration from MM:SS or HH:MM:SS to seconds."""
        if not self.total_duration or not self.total_duration.strip():
            logger.warning("Empty total_duration, using default of 90 minutes")
            return 90 * 60  # Default to 90 minutes
            
        try:
            parts = self.total_duration.split(':')
            
            if len(parts) == 2:
                # MM:SS format
                m, s = map(int, parts)
                return int(timedelta(minutes=m, seconds=s).total_seconds())
            elif len(parts) == 3:
                # HH:MM:SS format
                h, m, s = map(int, parts)
                return int(timedelta(hours=h, minutes=m, seconds=s).total_seconds())
            else:
                logger.warning(f"Invalid time format: {self.total_duration}, using default of 90 minutes")
                return 90 * 60  # Default to 90 minutes
        except (ValueError, TypeError) as e:
            logger.warning(f"Error parsing duration '{self.total_duration}': {e}, using default of 90 minutes")
            return 90 * 60  # Default to 90 minutes
    
    def get_start_offset(self) -> str:
        """Get start_time_offset in HH:MM:SS format."""
        if not self.start_time_offset or not self.start_time_offset.strip():
            logger.warning("Empty start_time_offset, using default of 00:00:00")
            return "00:00:00"
            
        try:
            parts = self.start_time_offset.split(':')
            
            if len(parts) == 2:
                # Convert MM:SS to HH:MM:SS
                return f"00:{parts[0].zfill(2)}:{parts[1].zfill(2)}"
            elif len(parts) == 3:
                # Already in HH:MM:SS format
                return self.start_time_offset
            else:
                logger.warning(f"Invalid time format: {self.start_time_offset}, using default of 00:00:00")
                return "00:00:00"
        except (ValueError, TypeError) as e:
            logger.warning(f"Error parsing start offset '{self.start_time_offset}': {e}, using default of 00:00:00")
            return "00:00:00"
    
    def get_sanitized_names(self) -> Tuple[str, str, str]:
        """Get sanitized team and location names for file naming."""
        my_team_sanitized = re.sub(r'[^a-zA-Z0-9]', '', self.my_team_name).lower()
        opponent_sanitized = re.sub(r'[^a-zA-Z0-9]', '', self.opponent_team_name).lower()
        location_sanitized = re.sub(r'[^a-zA-Z0-9]', '', self.location).lower()
        
        return my_team_sanitized, opponent_sanitized, location_sanitized

# Base FFmpeg task class
@dataclass(frozen=True)
class FFmpegTask:
    """Base class for FFmpeg tasks."""
    task_type: str
    item_path: str
    
    def __hash__(self):
        """Make FFmpegTask hashable so it can be used in sets and as dictionary keys."""
        return hash((self.task_type, self.item_path))
    
    def __eq__(self, other):
        """Define equality for FFmpegTask objects."""
        if not isinstance(other, FFmpegTask):
            return False
        return (self.task_type == other.task_type and
                self.item_path == other.item_path)
    
    def to_dict(self) -> dict:
        """Convert task to a dictionary for serialization."""
        return {
            "task_type": self.task_type,
            "item_path": self.item_path
        }

@dataclass(frozen=True)
class ConvertTask(FFmpegTask):
    """Task for converting a video file."""
    def __init__(self, file_path: str):
        super().__init__("convert", file_path)

@dataclass(frozen=True)
class CombineTask(FFmpegTask):
    """Task for combining multiple video files."""
    def __init__(self, group_dir: str):
        super().__init__("combine", group_dir)

@dataclass(frozen=True)
class TrimTask(FFmpegTask):
    """Task for trimming a video file."""
    match_info: MatchInfo
    
    def __init__(self, group_dir: str, match_info: MatchInfo):
        super().__init__("trim", group_dir)
        object.__setattr__(self, 'match_info', match_info)
    
    def __hash__(self):
        """Make TrimTask hashable so it can be used in sets and as dictionary keys."""
        return hash((self.task_type, self.item_path, self.match_info))
    
    def __eq__(self, other):
        """Define equality for TrimTask objects."""
        if not isinstance(other, TrimTask):
            return False
        return (super().__eq__(other) and
                self.match_info == other.match_info)
    
    def to_dict(self) -> dict:
        """Convert task to a dictionary for serialization."""
        # We only serialize the basic info, match_info will be loaded from file when needed
        return {
            "task_type": self.task_type,
            "item_path": self.item_path,
            # Include minimal match info for debugging purposes
            "match_info_summary": {
                "my_team_name": self.match_info.my_team_name,
                "opponent_team_name": self.match_info.opponent_team_name
            }
        }
    
    @classmethod
    def from_path(cls, group_dir: str) -> Optional['TrimTask']:
        """Create a TrimTask from a group directory path."""
        match_info_path = os.path.join(group_dir, "match_info.ini")
        match_info = MatchInfo.from_file(match_info_path)
        if match_info is None:
            return None
        return cls(group_dir, match_info)

@dataclass(frozen=True)
class VideoUploadTask(FFmpegTask):
    """Task for uploading videos to YouTube."""
    
    def __init__(self, group_dir: str):
        super().__init__("youtube_upload", group_dir)
    
    def to_dict(self) -> dict:
        """Convert task to a dictionary for serialization."""
        return {
            "task_type": self.task_type,
            "item_path": self.item_path
        }

# Factory function to create the appropriate task type
def create_ffmpeg_task(task_type: str, item_path: str, match_info: Optional[MatchInfo] = None) -> Optional[FFmpegTask]:
    """Create an FFmpeg task of the appropriate type."""
    if task_type == "convert":
        return ConvertTask(item_path)
    elif task_type == "combine":
        return CombineTask(item_path)
    elif task_type == "trim":
        if match_info is None:
            return TrimTask.from_path(item_path)
        return TrimTask(item_path, match_info)
    elif task_type == "youtube_upload":
        return VideoUploadTask(item_path)
    else:
        logger.warning(f"Unknown task type: {task_type}")
        return None

# Function to create a task from a serialized dictionary
def task_from_dict(task_dict: dict) -> Optional[FFmpegTask]:
    """Create an FFmpeg task from a serialized dictionary."""
    task_type = task_dict.get("task_type")
    item_path = task_dict.get("item_path")
    
    if not task_type or not item_path:
        logger.warning(f"Invalid task dictionary: {task_dict}")
        return None
    
    return create_ffmpeg_task(task_type, item_path)

class RecordingFile:
    """Represents a recording file from a camera."""
    
    def __init__(self, start_time: datetime, end_time: datetime, file_path: str, status: str = "pending", metadata: Optional[dict[str, Any]] = None, skip: bool = False):
        """Initialize a recording file.
        
        Args:
            start_time: The start time of the recording
            end_time: The end time of the recording
            file_path: The path to the file on the camera
            status: The status of the recording
            metadata: Optional additional metadata about the file
            skip: Whether the recording should be skipped
        """
        self.start_time = start_time
        self.end_time = end_time
        self.file_path = file_path
        self.status = status
        self.metadata = metadata or {}
        self.screenshot_path = None
        self.skip = skip
        self.group_dir = None
        self.last_updated = datetime.now()
        self.error_message: Optional[str] = None

    @property
    def mp4_path(self) -> str:
        """Returns the expected path for the MP4 file."""
        return self.file_path.replace('.dav', '.mp4')

    def to_dict(self) -> dict[str, Any]:
        """Convert the recording file to a dictionary for serialization."""
        return {
            'file_path': self.file_path,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'end_time': self.end_time.isoformat() if self.end_time else None,
            'status': self.status,
            'metadata': self.metadata,
            'skip': self.skip,
            'screenshot_path': self.screenshot_path,
            'group_dir': self.group_dir,
            'last_updated': self.last_updated.isoformat(),
            'error_message': self.error_message
        }
        
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'RecordingFile':
        """Create a RecordingFile from a dictionary."""
        start_time = datetime.fromisoformat(data['start_time']) if data.get('start_time') else None
        end_time = datetime.fromisoformat(data['end_time']) if data.get('end_time') else None
        
        file = cls(
            start_time=start_time,
            end_time=end_time,
            file_path=data['file_path'],
            status=data.get('status', 'pending'),
            metadata=data.get('metadata', {}),
            skip=data.get('skip', False)
        )
        
        file.screenshot_path = data.get('screenshot_path')
        file.group_dir = data.get('group_dir')
        if 'last_updated' in data and data['last_updated']:
            file.last_updated = datetime.fromisoformat(data['last_updated'])
        file.error_message = data.get('error_message')
        
        return file

    @classmethod
    def from_response(cls, response_text: str) -> list["RecordingFile"]:
        """Create a list of RecordingFile objects from a camera response.
        
        Args:
            response_text: The response text from the camera
            
        Returns:
            A list of RecordingFile objects
        """
        files = []
        for line in response_text.strip().split('\n'):
            if not line.strip():
                continue
            try:
                # Parse the line format: "path=xxx.dav&startTime=HH:MM:SS&endTime=HH:MM:SS"
                parts = {}
                for part in line.split('&'):
                    if '=' in part:
                        key, value = part.split('=', 1)
                        parts[key] = value
                
                path = parts.get('path', '')
                if not path.endswith('.dav'):
                    continue
                
                start_time = datetime.strptime(parts.get('startTime', ''), '%Y-%m-%d %H:%M:%S')
                end_time = datetime.strptime(parts.get('endTime', ''), '%Y-%m-%d %H:%M:%S')
                
                # Extract any other metadata from the parts
                metadata = {k: v for k, v in parts.items() if k not in ['path', 'startTime', 'endTime']}
                
                files.append(cls(start_time, end_time, path, metadata=metadata))
            except Exception as e:
                logger.error(f"Error parsing recording file: {e}")
                continue
        return files 