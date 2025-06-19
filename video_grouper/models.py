from datetime import datetime
from typing import List, Optional, Dict, Any

class RecordingFile:
    """Represents a recording file from a camera."""
    
    def __init__(self, start_time: datetime, end_time: datetime, file_path: str, metadata: Optional[Dict[str, Any]] = None):
        """Initialize a recording file.
        
        Args:
            start_time: The start time of the recording
            end_time: The end time of the recording
            file_path: The path to the file on the camera
            metadata: Optional additional metadata about the file
        """
        self.start_time = start_time
        self.end_time = end_time
        self.file_path = file_path
        self.metadata = metadata or {}
        self.screenshot_path = None
        self.skip = False
        self.status = "pending"
        self.group_dir = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert the recording file to a dictionary for serialization."""
        return {
            'file_path': self.file_path,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'end_time': self.end_time.isoformat() if self.end_time else None,
            'metadata': self.metadata,
            'screenshot_path': self.screenshot_path,
            'skip': self.skip,
            'status': self.status,
            'group_dir': self.group_dir
        }
        
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'RecordingFile':
        """Create a RecordingFile from a dictionary."""
        start_time = datetime.fromisoformat(data['start_time']) if data.get('start_time') else None
        end_time = datetime.fromisoformat(data['end_time']) if data.get('end_time') else None
        
        file = cls(
            start_time=start_time,
            end_time=end_time,
            file_path=data['file_path'],
            metadata=data.get('metadata', {})
        )
        
        file.screenshot_path = data.get('screenshot_path')
        file.skip = data.get('skip', False)
        file.status = data.get('status', 'pending')
        file.group_dir = data.get('group_dir')
        
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
                
                files.append(cls(start_time, end_time, path, metadata))
            except Exception as e:
                print(f"Error parsing recording file: {e}")
                continue
        return files 