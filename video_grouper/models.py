from datetime import datetime
from typing import List, Optional

class RecordingFile:
    """Represents a recording file from a camera."""
    
    def __init__(self, start_time: datetime, end_time: datetime, file_path: str):
        """Initialize a recording file.
        
        Args:
            start_time: The start time of the recording
            end_time: The end time of the recording
            file_path: The path to the file on the camera
        """
        self.start_time = start_time
        self.end_time = end_time
        self.file_path = file_path

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
                
                files.append(cls(start_time, end_time, path))
            except Exception as e:
                print(f"Error parsing recording file: {e}")
                continue
        return files 