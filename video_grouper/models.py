from datetime import datetime
from typing import Optional, Any
import logging

logger = logging.getLogger(__name__)

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