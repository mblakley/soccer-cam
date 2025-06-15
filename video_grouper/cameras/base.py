from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime

class Camera(ABC):
    """Base class for camera implementations."""
    
    @abstractmethod
    async def check_availability(self) -> bool:
        """Check if the camera is available."""
        pass
    
    @abstractmethod
    async def get_file_list(self) -> List[Dict[str, str]]:
        """Get list of recording files from the camera."""
        pass
    
    @abstractmethod
    async def get_file_size(self, file_path: str) -> int:
        """Get size of a file on the camera."""
        pass
    
    @abstractmethod
    async def download_file(self, remote_path: str, local_path: str) -> bool:
        """Download a file from the camera."""
        pass
    
    @abstractmethod
    async def stop_recording(self) -> bool:
        """Stop recording on the camera."""
        pass
    
    @abstractmethod
    async def get_recording_status(self) -> bool:
        """Get recording status from the camera."""
        pass
    
    @abstractmethod
    async def get_device_info(self) -> Dict[str, Any]:
        """Get device information from the camera."""
        pass
    
    @property
    @abstractmethod
    def connection_events(self) -> List[Tuple[datetime, str]]:
        """Get list of connection events."""
        pass
    
    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Get connection status."""
        pass 