import json
import os
import httpx
import logging
import time
import aiofiles
from datetime import datetime
from typing import List, Tuple, Dict, Any
import asyncio

from .base import Camera

logger = logging.getLogger(__name__)

class DahuaCamera(Camera):
    """Dahua camera implementation."""
    
    def __init__(self, device_ip: str, username: str, password: str, storage_path: str, client=None):
        """Initialize the Dahua camera with configuration."""
        self.ip = device_ip
        self.username = username
        self.password = password
        self.storage_path = storage_path
        self._is_connected = False
        self._connection_events = []
        self._state_file = os.path.join(self.storage_path, "camera_state.json")
        self._log_dir = os.path.join(self.storage_path, "camera_http_logs")
        os.makedirs(self._log_dir, exist_ok=True)
        self._client = client
        self.logger = logging.getLogger(__name__)
        self._load_state()
    
    async def _log_http_call(self, name: str, request: httpx.Request, response: httpx.Response = None, error: Exception = None, stream_response: bool = False):
        """Logs the details of an HTTP request and its response to files if LOG_LEVEL is 'debug'."""
        if os.environ.get("LOG_LEVEL", "").lower() != "debug":
            return
            
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        
        # Log Request
        req_filename = os.path.join(self._log_dir, f"{timestamp}_{name}_request.log")
        async with aiofiles.open(req_filename, 'w') as f:
            await f.write(f"URL: {request.method} {request.url}\n")
            await f.write("Headers:\n")
            for key, value in request.headers.items():
                await f.write(f"  {key}: {value}\n")
            if request.content:
                await f.write("\nBody:\n")
                await f.write(request.content.decode('utf-8', errors='ignore'))

        # Log Response
        res_filename = os.path.join(self._log_dir, f"{timestamp}_{name}_response.log")
        async with aiofiles.open(res_filename, 'w') as f:
            if response:
                await f.write(f"Status Code: {response.status_code}\n")
                await f.write("Headers:\n")
                for key, value in response.headers.items():
                    await f.write(f"  {key}: {value}\n")
                await f.write("\nBody:\n")
                # Handle streamed content differently
                if stream_response:
                     await f.write("[Streamed content not logged]")
                else:
                    await f.write(response.text)
            elif error:
                await f.write(f"Error: {type(error).__name__}\n")
                await f.write(str(error))

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
            logger.info(f"Checking availability of camera at {self.ip}")
            
            # Use the provided client if available, otherwise create a new one
            if self._client:
                client = self._client
                close_client = False
            else:
                client = httpx.AsyncClient()
                close_client = True
                
            try:
                logger.debug(f"Making request to: {url}")
                response = await client.get(url, auth=auth)
                logger.debug(f"Got response with status {response.status_code}")
                
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
                        logger.info(f"Camera is not available at {self.ip}")
                        self._save_state()
                    return False
            except httpx.ConnectError as e:
                logger.info(f"Unable to connect to camera at {self.ip}: {e}")
                if self._is_connected:
                    self._is_connected = False
                    self._connection_events.append((datetime.now(), f"connection failed: {e}"))
                    self._save_state()
                return False
            except httpx.RequestError:
                logger.info(f"Camera is not available at {self.ip} : {e}")
                if self._is_connected:
                    self._is_connected = False
                    self._connection_events.append((datetime.now(), "connection error"))
                    self._save_state()
                return False
            except Exception as e:
                logger.error(f"Request to {url} failed with error: {e}")
                raise
            finally:
                if close_client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error checking camera availability: {e}", exc_info=True)
            if self._is_connected:
                self._is_connected = False
                self._connection_events.append((datetime.now(), f"connection error: {str(e)}"))
                self._save_state()
            return False
    
    async def get_file_list(self, start_time: datetime, end_time: datetime) -> List[Dict[str, Any]]:
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
                await self._log_http_call("get_file_list_factory", response.request, response)

                if response.status_code != 200:
                    logger.error(f"Failed to create media file finder factory: {response.status_code}")
                    return []
                
                object_id = response.text.split('=')[1].strip()
                
                # Now find files using the object ID
                start_time_str = start_time.strftime("%Y-%m-%d%%20%H:%M:%S")
                end_time_str = end_time.strftime("%Y-%m-%d%%20%H:%M:%S")
                
                findfile_url = f"http://{self.ip}/cgi-bin/mediaFileFind.cgi?action=findFile&object={object_id}&condition.Channel=1&condition.Types[0]=dav&condition.StartTime={start_time_str}&condition.EndTime={end_time_str}&condition.VideoStream=Main"
                response = await client.get(findfile_url, auth=auth)
                await self._log_http_call("get_file_list_find", response.request, response)
                
                if response.status_code != 200:
                    logger.error(f"Failed to find media files: {response.status_code}")
                    return []
                
                # Get the next files
                nextfile_url = f"http://{self.ip}/cgi-bin/mediaFileFind.cgi?action=findNextFile&object={object_id}&count=100"
                response = await client.get(nextfile_url, auth=auth)
                await self._log_http_call("get_file_list_next", response.request, response)

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
                await self._log_http_call("get_file_size", response.request, response)
                if response.status_code == 200:
                    return int(response.headers.get('content-length', 0))
                return 0
            finally:
                if close_client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error getting file size: {e}")
            return 0
    
    async def download_file(self, file_path: str, local_path: str) -> bool:
        """Downloads a file from the camera to a local path."""
        try:
            # Create directory if it doesn't exist
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            
            # Get file size first for progress tracking
            file_size = await self.get_file_size(file_path)
            if file_size == 0:
                logger.warning(f"File {file_path} is empty, skipping download.")
                return False
            
            # Get directory name for better logging
            dir_name = os.path.basename(os.path.dirname(local_path))
            file_name = os.path.basename(local_path)
            
            self.logger.info(f"Downloading {file_name} to directory '{dir_name}' ({file_size/1024/1024:.1f}MB)")
            
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
                async with client.stream("GET", url, auth=auth) as response:
                    await self._log_http_call("download_file", response.request, response, stream_response=True)
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
                        self.logger.info(f"Download complete: {os.path.basename(file_path)}")
                        return True
                    else:
                        self.logger.error(f"Download incomplete: {os.path.basename(file_path)} ({downloaded}/{file_size} bytes)")
                        return False
                        
            finally:
                if close_client:
                    await client.aclose()
        except Exception as e:
            self.logger.error(f"Error downloading {file_path}: {e}")
            # Clean up partial download if it exists
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                    self.logger.info(f"Removed partial download: {local_path}")
                except Exception as e:
                    self.logger.error(f"Failed to remove partial download {local_path}: {e}")
            return False
    
    async def start_recording(self):
        """Starts video recording on the camera."""
        try:
            url = f"http://{self.ip}/cgi-bin/configManager.cgi?action=setConfig&ManualRec.Enable=true"
            client = self._client or httpx.AsyncClient()
            try:
                response = await client.get(url, auth=httpx.DigestAuth(self.username, self.password))
                await self._log_http_call("start_recording", response.request, response)
                return response.status_code == 200
            finally:
                if not self._client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error starting recording: {e}")
            return False
    
    async def stop_recording(self):
        """Stops video recording on the camera."""
        try:
            url = f"http://{self.ip}/cgi-bin/configManager.cgi?action=setConfig&RecordMode[0].Mode=2"
            client = self._client or httpx.AsyncClient()
            try:
                response = await client.get(url, auth=httpx.DigestAuth(self.username, self.password))
                await self._log_http_call("stop_recording", response.request, response)
                return response.status_code == 200
            finally:
                if not self._client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error stopping recording: {e}")
            return False
    
    async def get_recording_status(self) -> bool:
        """Get recording status from the camera."""
        try:
            url = f"http://{self.ip}/cgi-bin/configManager.cgi?action=getConfig&name=RecordMode"
            client = self._client or httpx.AsyncClient()
            try:
                response = await client.get(url, auth=httpx.DigestAuth(self.username, self.password))
                await self._log_http_call("get_recording_status", response.request, response)
                if response.status_code == 200:
                    return "RecordMode[0].Mode=1" in response.text
                return False
            finally:
                if not self._client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error getting recording status: {e}")
            return False
    
    async def get_device_info(self) -> Dict[str, Any]:
        """Get device information from the camera."""
        try:
            url = f"http://{self.ip}/cgi-bin/magicBox.cgi?action=getSystemInfo"
            client = self._client or httpx.AsyncClient()
            try:
                response = await client.get(url, auth=httpx.DigestAuth(self.username, self.password))
                await self._log_http_call("get_device_info", response.request, response)
                if response.status_code == 200:
                    lines = response.text.strip().split('\n')
                    info = {}
                    for line in lines:
                        if '=' in line:
                            key, value = line.split('=', 1)
                            info[key.strip()] = value.strip()
                    return info
                return {}
            finally:
                if not self._client:
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
    
    async def get_screenshot(self, server_path: str, output_path: str) -> bool:
        """Get a screenshot from a video file on the camera.
        
        Args:
            server_path: The path to the file on the camera
            output_path: The path to save the screenshot to
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # Create directory if it doesn't exist
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            
            # Use ffmpeg to extract a frame from the video
            # First download a small part of the file
            temp_file = os.path.join(os.path.dirname(output_path), "temp_screenshot.dav")
            
            auth = httpx.DigestAuth(self.username, self.password)
            url = f"http://{self.ip}/cgi-bin/RPC_Loadfile{server_path}"
            
            # Use the provided client if available, otherwise create a new one
            if self._client:
                client = self._client
                close_client = False
            else:
                client = httpx.AsyncClient()
                close_client = True
                
            try:
                # Download just the first 1MB of the file (should be enough for a frame)
                headers = {"Range": "bytes=0-1048576"}
                async with client.stream('GET', url, auth=auth, headers=headers) as response:
                    await self._log_http_call("get_screenshot", response.request, response, stream_response=True)
                    if response.status_code not in [200, 206]:
                        logger.error(f"Screenshot download failed with status {response.status_code}")
                        return False
                        
                    # Save to temp file
                    async with aiofiles.open(temp_file, 'wb') as f:
                        async for chunk in response.aiter_bytes():
                            await f.write(chunk)
                
                # Use ffmpeg to extract the first frame
                cmd = [
                    'ffmpeg',
                    '-i', temp_file,
                    '-ss', '00:00:01',  # Skip to 1 second in
                    '-vframes', '1',    # Extract 1 frame
                    '-q:v', '2',        # High quality
                    output_path
                ]
                
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                stdout, stderr = await process.communicate()
                
                # Clean up temp file
                if os.path.exists(temp_file):
                    os.remove(temp_file)
                
                if process.returncode == 0 and os.path.exists(output_path):
                    logger.info(f"Successfully created screenshot at {output_path}")
                    return True
                else:
                    logger.error(f"Failed to create screenshot: {stderr.decode()}")
                    return False
                    
            finally:
                if close_client:
                    await client.aclose()
                    
        except Exception as e:
            logger.error(f"Error getting screenshot: {e}")
            return False 