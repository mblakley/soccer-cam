import json
import os
import re
import httpx
import logging
import time
import aiofiles
from datetime import datetime
from typing import List, Tuple, Dict, Any, Optional

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
        self.logger = logging.getLogger(__name__)
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
            url = f"http://{self.ip}/cgi-bin/mediaFileFind.cgi?action=factory.create"
            
            # Use the provided client if available, otherwise create a new one
            if self._client:
                client = self._client
                close_client = False
            else:
                client = httpx.AsyncClient()
                close_client = True
            
            try:
                # First create the media file finder factory
                response = await client.get(url, auth=auth)
                if response.status_code != 200:
                    logger.error(f"Failed to create media file finder factory: {response.status_code}")
                    return []
                
                object_id = response.text.split('=')[1].strip()
                
                # Now find files using the object ID
                from datetime import datetime
                start_time = datetime.now().replace(hour=0, minute=0, second=0).strftime("%Y-%m-%d%%20%H:%M:%S")
                end_time = datetime.now().strftime("%Y-%m-%d%%20%H:%M:%S")
                
                findfile_url = f"http://{self.ip}/cgi-bin/mediaFileFind.cgi?action=findFile&object={object_id}&condition.Channel=1&condition.Types[0]=dav&condition.StartTime={start_time}&condition.EndTime={end_time}&condition.VideoStream=Main"
                response = await client.get(findfile_url, auth=auth)
                if response.status_code != 200:
                    logger.error(f"Failed to find media files: {response.status_code}")
                    return []
                
                # Get the next files
                response = await client.get(f"http://{self.ip}/cgi-bin/mediaFileFind.cgi?action=findNextFile&object={object_id}&count=100", auth=auth)
                if response.status_code == 200:
                    files = []
                    current_file = {}
                    
                    for line in response.text.strip().split('\n'):
                        if line.startswith("items["):
                            key, value = line.split("=", 1)
                            key = key.strip()
                            value = value.strip()
                            
                            file_index = key.split('[')[1].split(']')[0]
                            field = key.split('.')[1]
                            
                            # Initialize dict for this file index if needed
                            if file_index not in current_file:
                                current_file[file_index] = {}
                            
                            # Map the fields to our expected output format
                            if field == "FilePath":
                                current_file[file_index]["path"] = value
                            elif field == "StartTime":
                                current_file[file_index]["startTime"] = value
                            elif field == "EndTime":
                                current_file[file_index]["endTime"] = value
                    
                    # Convert our dict of dicts to a list of dicts
                    for _, file_data in current_file.items():
                        if "path" in file_data and "startTime" in file_data and "endTime" in file_data:
                            files.append(file_data)
                    
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
            url = f"http://{self.ip}/cgi-bin/RPC_Loadfile{file_path}"
            
            # Use the provided client if available, otherwise create a new one
            if self._client:
                client = self._client
                close_client = False
            else:
                client = httpx.AsyncClient()
                close_client = True
            
            try:
                response = await client.head(url, auth=auth)
                if response.status_code == 200:
                    return int(response.headers.get('content-length', 0))
                return 0
            finally:
                if close_client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error getting file size: {e}")
            return 0
    
    async def download_file(self, server_path: str, local_path: str) -> bool:
        """Download a file from the camera to the local filesystem with progress tracking."""
        try:
            # Create directory if it doesn't exist
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            
            # Get file size first for progress tracking
            file_size = await self.get_file_size(server_path)
            if file_size <= 0:
                self.logger.error(f"Invalid file size for {server_path}: {file_size}")
                return False
            
            # Get directory name for better logging
            dir_name = os.path.basename(os.path.dirname(local_path))
            file_name = os.path.basename(local_path)
            
            self.logger.info(f"Downloading {file_name} to directory '{dir_name}' ({file_size/1024/1024:.1f}MB)")
            
            async with httpx.AsyncClient() as client:
                url = f"http://{self.ip}/cgi-bin/RPC_Loadfile{server_path}"
                auth = httpx.DigestAuth(self.username, self.password)
                
                async with client.stream('GET', url, auth=auth) as response:
                    if response.status_code != 200:
                        self.logger.error(f"Download failed with status {response.status_code}: {response.text}")
                        return False
                    
                    # Open file for writing and stream the download
                    async with aiofiles.open(local_path, 'wb') as f:
                        downloaded = 0
                        last_update = time.time()
                        last_downloaded = 0
                        
                        async for chunk in response.aiter_bytes():
                            await f.write(chunk)
                            downloaded += len(chunk)
                            
                            # Update progress every second
                            current_time = time.time()
                            if current_time - last_update >= 1.0:
                                speed = (downloaded - last_downloaded) / (current_time - last_update)
                                progress = downloaded / file_size * 100
                                bar_length = 20
                                filled_length = int(bar_length * downloaded // file_size)
                                bar = '█' * filled_length + '░' * (bar_length - filled_length)
                                self.logger.info(f"Downloading {file_name} to directory '{dir_name}': [{bar}] {progress:.1f}% ({downloaded/1024/1024:.1f}MB/{file_size/1024/1024:.1f}MB) @ {speed/1024/1024:.1f}MB/s")
                                last_update = current_time
                                last_downloaded = downloaded
                    
                    # Verify the download is complete
                    if downloaded == file_size:
                        self.logger.info(f"Download complete: {os.path.basename(server_path)}")
                        return True
                    else:
                        self.logger.error(f"Download incomplete: {os.path.basename(server_path)} ({downloaded}/{file_size} bytes)")
                        return False
                        
        except Exception as e:
            self.logger.error(f"Error downloading {server_path}: {e}")
            # Clean up partial download if it exists
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                    self.logger.info(f"Removed partial download: {local_path}")
                except Exception as e:
                    self.logger.error(f"Failed to remove partial download {local_path}: {e}")
            return False
    
    async def stop_recording(self) -> bool:
        """Stop recording on the camera."""
        try:
            auth = httpx.DigestAuth(self.username, self.password)
            url = f"http://{self.ip}/cgi-bin/configManager.cgi?action=setConfig&RecordMode[0].Mode=2"
            
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