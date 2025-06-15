import os
import re
import json
import time
import asyncio
import logging
import configparser
from datetime import datetime
import httpx
from typing import List, Tuple, Dict, Optional
from .ffmpeg_utils import verify_mp4_duration, run_ffmpeg, async_convert_file

# Constants
LATEST_VIDEO_FILE = "latest_video.txt"
STATUS_FILE = "status.txt"
default_date_format = "%Y-%m-%d %H:%M:%S"

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s'
)
logger = logging.getLogger(__name__)

# Global locks
ffmpeg_lock = asyncio.Lock()

class RecordingFile:
    def __init__(self, start_time: datetime, end_time: datetime, file_path: str):
        self.start_time = start_time
        self.end_time = end_time
        self.file_path = file_path

    @classmethod
    def from_response(cls, response_text: str) -> list["RecordingFile"]:
        files = []
        for line in response_text.strip().split('\n'):
            if not line.strip():
                continue
            try:
                # Parse the line format: "path=xxx.dav&startTime=HH:MM:SS&endTime=HH:MM:SS"
                parts = dict(part.split('=') for part in line.split('&'))
                path = parts.get('path', '')
                if not path.endswith('.dav'):
                    continue
                
                start_time = datetime.strptime(parts.get('startTime', ''), '%H:%M:%S')
                end_time = datetime.strptime(parts.get('endTime', ''), '%H:%M:%S')
                
                # Set the date to today
                today = datetime.now().date()
                start_time = start_time.replace(year=today.year, month=today.month, day=today.day)
                end_time = end_time.replace(year=today.year, month=today.month, day=today.day)
                
                files.append(cls(start_time, end_time, path))
            except Exception as e:
                logger.error(f"Error parsing recording file: {e}")
                continue
        return files

class ProcessingState:
    def __init__(self, storage_path: str):
        self.storage_path = storage_path
        self.files: Dict[str, FileState] = {}
        self.state_file = os.path.join(storage_path, "processing_state.json")
        self.load_state()

    def load_state(self):
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                    self.files = {
                        path: FileState.from_dict(data)
                        for path, data in state.get('files', {}).items()
                    }
        except Exception as e:
            logger.error(f"Error loading state: {e}")

    def save_state(self):
        try:
            state = {
                'files': {
                    path: file_state.to_dict()
                    for path, file_state in self.files.items()
                }
            }
            with open(self.state_file, 'w') as f:
                json.dump(state, f)
        except Exception as e:
            logger.error(f"Error saving state: {e}")

    def update_file_state(self, file_path: str, **kwargs):
        if file_path not in self.files:
            self.files[file_path] = FileState(file_path, find_group_directory(file_path, self.storage_path, self))
        
        for key, value in kwargs.items():
            setattr(self.files[file_path], key, value)
        
        self.files[file_path].last_updated = datetime.now()
        self.save_state()

    def get_pending_files(self):
        return [f for f in self.files.values() if f.status == "pending"]

    def get_unconverted_files(self):
        return [f for f in self.files.values() if f.status == "downloaded"]

    def is_file_processed(self, file_path: str) -> bool:
        return file_path in self.files and self.files[file_path].status == "converted"

class DirectoryPlan:
    """Represents a plan for organizing files in a directory."""
    def __init__(self, path: str):
        self.path = path
        self.files = []

    def add_file(self, file: RecordingFile):
        """Add a file to the plan."""
        self.files.append(file)

def create_directory(path):
    os.makedirs(path, exist_ok=True)

async def async_convert_file(file_path, latest_file_path, end_time, filename):
    """Convert a single file asynchronously."""
    async with ffmpeg_lock:
        try:
            start_time = time.time()
            
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"Input file not found: {file_path}")
            
            if not os.access(file_path, os.R_OK):
                raise PermissionError(f"Cannot read input file: {file_path}")
                
            mp4_path = file_path.replace('.dav', '.mp4')
            
            output_dir = os.path.dirname(mp4_path)
            if not os.access(output_dir, os.W_OK):
                raise PermissionError(f"Cannot write to output directory: {output_dir}")
            
            command = [
                "ffmpeg", "-i", file_path,
                "-vcodec", "copy", "-acodec", "alac",
                "-threads", "0", "-async", "1",
                "-progress", "pipe:1",
                mp4_path
            ]
            
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            last_logged_percentage = 0
            error_output = []
            
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                    
                line = line.decode().strip()
                if "out_time_ms=" in line:
                    try:
                        time_ms = int(line.split("=")[1])
                        percentage = min(100, int((time_ms / 1000000) * 100))
                        if percentage > last_logged_percentage + 9:
                            logger.info(f"Converting {filename}: {percentage}%")
                            last_logged_percentage = percentage
                    except (ValueError, IndexError):
                        pass
            
            await process.wait()
            
            if process.returncode != 0:
                error = await process.stderr.read()
                error_output.append(error.decode())
                raise Exception(f"FFmpeg conversion failed: {''.join(error_output)}")
            
            # Update latest video file
            with open(latest_file_path, "w") as f:
                f.write(end_time.strftime(default_date_format))
            
            duration = time.time() - start_time
            logger.info(f"Converted {filename} in {duration:.2f} seconds")
            
        except Exception as e:
            logger.error(f"Error converting {filename}: {e}")
            raise

class FileState:
    def __init__(self, file_path: str, group_dir: str, total_size: int = 0, downloaded_bytes: int = 0, status: str = "pending", start_time: datetime = None, end_time: datetime = None):
        self.file_path = file_path
        self.group_dir = group_dir
        self.total_size = total_size
        self.downloaded_bytes = downloaded_bytes
        self.status = status
        self.last_updated = datetime.now()
        self.error_message = None
        self.mp4_path = file_path.replace('.dav', '.mp4')
        self.start_time = start_time
        self.end_time = end_time

    def to_dict(self):
        return {
            'file_path': self.file_path,
            'group_dir': self.group_dir,
            'total_size': self.total_size,
            'downloaded_bytes': self.downloaded_bytes,
            'status': self.status,
            'last_updated': self.last_updated.isoformat(),
            'error_message': self.error_message,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'end_time': self.end_time.isoformat() if self.end_time else None
        }

    @classmethod
    def from_dict(cls, data):
        state = cls(
            file_path=data['file_path'],
            group_dir=data['group_dir'],
            total_size=data['total_size'],
            downloaded_bytes=data['downloaded_bytes'],
            status=data['status']
        )
        state.last_updated = datetime.fromisoformat(data['last_updated'])
        state.error_message = data['error_message']
        if data['start_time']:
            state.start_time = datetime.fromisoformat(data['start_time'])
        if data['end_time']:
            state.end_time = datetime.fromisoformat(data['end_time'])
        return state

def find_group_directory(file_path: str, storage_path: str, processing_state: ProcessingState) -> str:
    filename = os.path.basename(file_path)
    time_match = re.match(r"(\d{2})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})", filename)
    if not time_match:
        return os.path.dirname(file_path)

    start_h, start_m, start_s, end_h, end_m, end_s = map(int, time_match.groups())
    file_start_time = datetime.now().replace(hour=start_h, minute=start_m, second=start_s)

    for state in processing_state.files.values():
        if state.end_time and 0 <= (file_start_time - state.end_time).total_seconds() <= 5:
            logger.info(f"Found matching group directory {state.group_dir} for {filename}")
            return state.group_dir

    new_dir = os.path.join(storage_path, file_start_time.strftime("%Y.%m.%d-%H.%M.%S"))
    create_directory(new_dir)
    logger.info(f"Created new group directory {new_dir} for {filename}")
    return new_dir

async def scan_for_unprocessed_files(storage_path: str, processing_state: ProcessingState):
    latest_file_path = os.path.join(storage_path, LATEST_VIDEO_FILE)
    latest_processed_date = None
    
    if os.path.exists(latest_file_path):
        with open(latest_file_path, "r") as latest_file:
            latest_processed_date = datetime.strptime(latest_file.read().strip(), default_date_format)
    
    logger.info("Scanning for existing MP4 files...")
    for root, _, files in os.walk(storage_path):
        for file in files:
            if file.endswith('.mp4'):
                mp4_path = os.path.join(root, file)
                dav_path = mp4_path.replace('.mp4', '.dav')
                if dav_path not in processing_state.files:
                    logger.info(f"Found existing MP4 at {mp4_path}, adding to state")
                    processing_state.update_file_state(
                        dav_path,
                        status="converted",
                        group_dir=os.path.dirname(mp4_path)
                    )

class VideoGrouperApp:
    def __init__(self, config):
        self.config = config
        self.storage_path = os.path.abspath(config.get('STORAGE', 'path'))
        
        camera_type = config.get('CAMERA', 'type', fallback='dahua')
        if camera_type == 'dahua':
            from .cameras.dahua import DahuaCamera
            camera_config = {
                'device_ip': config.get('CAMERA', 'device_ip'),
                'username': config.get('CAMERA', 'username'),
                'password': config.get('CAMERA', 'password'),
                'storage_path': self.storage_path
            }
            self.camera = DahuaCamera(camera_config)
        else:
            raise ValueError(f"Unsupported camera type: {camera_type}")
        
        self.processing_state = ProcessingState(self.storage_path)
        
        self.download_queue = asyncio.Queue()
        self.ffmpeg_queue = asyncio.Queue()
        self.queued_files = set()
        self.download_lock = asyncio.Lock()
        self.ffmpeg_lock = asyncio.Lock()
        
        self._queue_state_loaded = False

    async def initialize(self):
        if not self._queue_state_loaded:
            await self.load_queue_state()
            self._queue_state_loaded = True

    async def verify_file_complete(self, file_path: str) -> bool:
        try:
            if not await self.camera.check_availability():
                logger.warning("Camera is not available")
                return False

            server_path = os.path.basename(file_path)
            server_size = await self.camera.get_file_size(server_path)
            local_size = os.path.getsize(file_path)
            
            return server_size == local_size
        except Exception as e:
            logger.error(f"Error verifying file completion: {e}")
            return False

    async def find_and_download_files(self):
        try:
            if not await self.camera.check_availability():
                logger.warning("Camera is not available")
                return

            files = await self.camera.get_file_list()
            for file_info in files:
                if 'path' not in file_info:
                    logger.warning(f"Skipping file without path: {file_info}")
                    continue
                    
                server_path = file_info['path']
                local_path = os.path.join(self.storage_path, os.path.basename(server_path))
                
                if os.path.exists(local_path):
                    server_size = await self.camera.get_file_size(server_path)
                    local_size = os.path.getsize(local_path)
                    
                    if server_size == local_size:
                        logger.info(f"File {local_path} is already complete")
                        continue
                    else:
                        logger.info(f"File {local_path} is incomplete, downloading again")
                
                success = await self.camera.download_file(server_path, local_path)
                if success:
                    logger.info(f"Successfully downloaded {local_path}")
                else:
                    logger.error(f"Failed to download {local_path}")
        except Exception as e:
            logger.error(f"Error finding and downloading files: {e}")

    def parse_match_info(self, file_path):
        match_info_config = configparser.ConfigParser()
        match_info_config.read(file_path)
        return match_info_config["MATCH"] if "MATCH" in match_info_config else None

    def save_queue_state(self):
        try:
            queue_state = {
                'queued_files': list(self.queued_files),
                'ffmpeg_queue': []
            }
            
            temp_queue = asyncio.Queue()
            while not self.ffmpeg_queue.empty():
                try:
                    item = self.ffmpeg_queue.get_nowait()
                    queue_state['ffmpeg_queue'].append(item)
                    temp_queue.put_nowait(item)
                except asyncio.QueueEmpty:
                    break
            
            while not temp_queue.empty():
                try:
                    item = temp_queue.get_nowait()
                    self.ffmpeg_queue.put_nowait(item)
                except asyncio.QueueEmpty:
                    break
            
            queue_state_path = os.path.join(self.storage_path, "ffmpeg_queue_state.json")
            with open(queue_state_path, 'w') as f:
                json.dump(queue_state, f)
            logger.info(f"Saved queue state with {len(queue_state['ffmpeg_queue'])} items")
        except Exception as e:
            logger.error(f"Error saving queue state: {e}")

    async def load_queue_state(self):
        try:
            queue_state_path = os.path.join(self.storage_path, "ffmpeg_queue_state.json")
            if os.path.exists(queue_state_path):
                with open(queue_state_path, 'r') as f:
                    state = json.load(f)
                    self.queued_files = set(state.get('queued_files', []))
                    
                    while not self.ffmpeg_queue.empty():
                        try:
                            self.ffmpeg_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    
                    for item in state.get('ffmpeg_queue', []):
                        await self.ffmpeg_queue.put(item)
                    
                    logger.info(f"Loaded queue state with {len(state.get('ffmpeg_queue', []))} items")
        except Exception as e:
            logger.error(f"Error loading queue state: {e}")
            self.queued_files.clear()
            while not self.ffmpeg_queue.empty():
                try:
                    self.ffmpeg_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

    def load_camera_state(self):
        try:
            camera_state_path = os.path.join(self.storage_path, "camera_state.json")
            if os.path.exists(camera_state_path):
                with open(camera_state_path, 'r') as f:
                    state = json.load(f)
                    self.connection_events = [(datetime.fromisoformat(d), s) for d, s in state.get('connection_events', [])]
                    logger.info(f"Loaded camera state with {len(self.connection_events)} events")
            else:
                self.connection_events = []
        except Exception as e:
            logger.error(f"Error loading camera state: {e}")
            self.connection_events = []

if __name__ == "__main__":
    config = configparser.ConfigParser()
    config.read("config.ini")
    app = VideoGrouperApp(config)
