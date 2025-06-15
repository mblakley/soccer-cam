import json
import os
from datetime import datetime
from typing import List, Tuple, Dict, Any, Optional

import aiofiles
import httpx
import logging

from .base import Camera
from video_grouper.models import RecordingFile

logger = logging.getLogger(__name__)

class DahuaCamera(Camera):
    """Dahua camera implementation."""
    
    def __init__(self, config: Dict[str, str], client=None):
        """Initialize the Dahua camera with configuration."""
        self.ip = config['device_ip']
        self.username = config['username']
        self.password = config['password']
        self.storage_path = config['storage_path']
        self._is_connected = False
        self._connection_events = []
        self._state_file = os.path.join(config['storage_path'], "camera_state.json")
        self._client = client
        self._load_state()
    
    def _load_state(self):
        """Load camera state from file."""
        try:
            if os.path.exists(self._state_file):
                with open(self._state_file, 'r') as f:
                    state = json.load(f)
                    self._connection_events = [(datetime.fromisoformat(t), e) for t, e in state.get('connection_events', [])]
                    self._is_connected = state.get('is_connected', False)
        except Exception as e:
            logger.error(f"Error loading camera state: {e}")
    
    def _save_state(self):
        """Save camera state to file."""
        try:
            state = {
                'connection_events': [(t.isoformat(), e) for t, e in self._connection_events],
                'is_connected': self._is_connected
            }
            os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
            with open(self._state_file, 'w') as f:
                json.dump(state, f)
        except Exception as e:
            logger.error(f"Error saving camera state: {e}")
    
    async def check_availability(self) -> bool:
        """Check if the camera is available."""
        try:
            auth = httpx.DigestAuth(self.username, self.password)
            url = f"http://{self.ip}/cgi-bin/recordManager.cgi?action=getCaps"
            logger.info(f"Checking availability at {url}")
            
            # Use the provided client if available, otherwise create a new one
            if self._client:
                client = self._client
                close_client = False
            else:
                client = httpx.AsyncClient()
                close_client = True
                
            try:
                print(f"\nMaking request to: {url}")
                response = await client.get(url, auth=auth)
                print(f"Got response with status {response.status_code}")
                logger.info(f"Got response with status {response.status_code}")
                
                if response.status_code == 200:
                    if not self._is_connected:
                        self._is_connected = True
                        self._connection_events.append((datetime.now(), "connected"))
                        self._save_state()
                    return True
                else:
                    if self._is_connected:
                        self._is_connected = False
                        self._connection_events.append((datetime.now(), f"connection failed: {response.status_code}"))
                        self._save_state()
                    return False
            except Exception as e:
                print(f"Request failed with error: {e}")
                logger.error(f"Request failed with error: {e}")
                raise
            finally:
                if close_client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error checking camera availability: {e}")
            if self._is_connected:
                self._is_connected = False
                self._connection_events.append((datetime.now(), f"connection error: {str(e)}"))
                self._save_state()
            return False
    
    async def get_file_list(self) -> List[Dict[str, str]]:
        """Get list of recording files from the camera."""
        try:
            auth = httpx.DigestAuth(self.username, self.password)
            url = f"http://{self.ip}/cgi-bin/loadfile.cgi?action=findFile&object=1"
            
            # Use the provided client if available, otherwise create a new one
            if self._client:
                client = self._client
                close_client = False
            else:
                client = httpx.AsyncClient()
                close_client = True
            
            try:
                response = await client.get(url, auth=auth)
                if response.status_code == 200:
                    files = []
                    for line in response.text.strip().split('\n'):
                        if not line.startswith('object='):
                            parts = {}
                            for part in line.split('&'):
                                if '=' in part:
                                    key, value = part.split('=', 1)
                                    parts[key] = value
                            if parts:
                                files.append(parts)
                    return files
                return []
            finally:
                if close_client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error getting file list: {e}")
            return []
    
    async def get_file_size(self, file_path: str) -> int:
        """Get size of a file on the camera."""
        try:
            auth = httpx.DigestAuth(self.username, self.password)
            url = f"http://{self.ip}/cgi-bin/loadfile.cgi?action=getFileSize&object={file_path}"
            
            # Use the provided client if available, otherwise create a new one
            if self._client:
                client = self._client
                close_client = False
            else:
                client = httpx.AsyncClient()
                close_client = True
            
            try:
                response = await client.get(url, auth=auth)
                if response.status_code == 200:
                    return int(response.text.split('=')[1])
                return 0
            finally:
                if close_client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error getting file size: {e}")
            return 0
    
    async def download_file(self, remote_path: str, local_path: str) -> bool:
        """Download a file from the camera."""
        try:
            auth = httpx.DigestAuth(self.username, self.password)
            url = f"http://{self.ip}/cgi-bin/loadfile.cgi?action=downloadFile&object={remote_path}"
            
            # Use the provided client if available, otherwise create a new one
            if self._client:
                client = self._client
                close_client = False
            else:
                client = httpx.AsyncClient()
                close_client = True
            
            try:
                response = await client.get(url, auth=auth)
                if response.status_code == 200:
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    async with aiofiles.open(local_path, 'wb') as f:
                        async for chunk in response.aiter_bytes():
                            await f.write(chunk)
                    return True
                return False
            finally:
                if close_client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error downloading file: {e}")
            return False
    
    async def stop_recording(self) -> bool:
        """Stop recording on the camera."""
        try:
            auth = httpx.DigestAuth(self.username, self.password)
            url = f"http://{self.ip}/cgi-bin/configManager.cgi?action=setConfig&RecordMode[0].Mode=0"
            
            # Use the provided client if available, otherwise create a new one
            if self._client:
                client = self._client
                close_client = False
            else:
                client = httpx.AsyncClient()
                close_client = True
            
            try:
                response = await client.get(url, auth=auth)
                return response.status_code == 200
            finally:
                if close_client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error stopping recording: {e}")
            return False
    
    async def get_recording_status(self) -> bool:
        """Get recording status from the camera."""
        try:
            auth = httpx.DigestAuth(self.username, self.password)
            url = f"http://{self.ip}/cgi-bin/configManager.cgi?action=getConfig&name=RecordMode"
            
            # Use the provided client if available, otherwise create a new one
            if self._client:
                client = self._client
                close_client = False
            else:
                client = httpx.AsyncClient()
                close_client = True
            
            try:
                response = await client.get(url, auth=auth)
                if response.status_code == 200:
                    return "RecordMode[0].Mode=1" in response.text
                return False
            finally:
                if close_client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error getting recording status: {e}")
            return False
    
    async def get_device_info(self) -> Dict[str, Any]:
        """Get device information from the camera."""
        try:
            auth = httpx.DigestAuth(self.username, self.password)
            url = f"http://{self.ip}/cgi-bin/magicBox.cgi?action=getSystemInfo"
            
            # Use the provided client if available, otherwise create a new one
            if self._client:
                client = self._client
                close_client = False
            else:
                client = httpx.AsyncClient()
                close_client = True
            
            try:
                response = await client.get(url, auth=auth)
                if response.status_code == 200:
                    info = {}
                    for line in response.text.split('\n'):
                        if '=' in line:
                            key, value = line.split('=', 1)
                            info[key.strip()] = value.strip()
                    return info
                return {}
            finally:
                if close_client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error getting device info: {e}")
            return {}
    
    @property
    def connection_events(self) -> List[Tuple[datetime, str]]:
        """Get list of connection events."""
        return self._connection_events
    
    @property
    def is_connected(self) -> bool:
        """Get connection status."""
        return self._is_connected 