import os
import re
import json
import time
import asyncio
import logging
import configparser
from datetime import datetime, timedelta
import httpx
from typing import List, Tuple, Dict, Optional

from video_grouper.ffmpeg_utils import verify_mp4_duration, run_ffmpeg, async_convert_file, get_video_duration
from video_grouper.models import RecordingFile

# Constants
LATEST_VIDEO_FILE = "latest_video.txt"
STATUS_FILE = "status.txt"
DEFAULT_STORAGE_PATH = "./shared_data"
default_date_format = "%Y-%m-%d %H:%M:%S"

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s'
)
logger = logging.getLogger(__name__)

# Global locks
ffmpeg_lock = asyncio.Lock()

class ProcessingState:
    def __init__(self, storage_path: str):
        self.storage_path = storage_path
        self.files: Dict[str, FileState] = {}
        self.state_file = os.path.join(storage_path, "processing_state.json")
        logger.info(f"Processing state file: {os.path.abspath(self.state_file)}")
        self.load_state()

    def load_state(self):
        try:
            if os.path.exists(self.state_file):
                logger.info(f"Loading processing state from {self.state_file}")
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                    self.files = {
                        path: FileState.from_dict(data)
                        for path, data in state.get('files', {}).items()
                    }
                logger.info(f"Loaded {len(self.files)} files from processing state")
            else:
                logger.info(f"No existing state file found at {self.state_file}")
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
            logger.debug(f"Saved processing state with {len(self.files)} files")
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
        self.storage_path = os.path.abspath(config.get('STORAGE', 'path', fallback=DEFAULT_STORAGE_PATH))
        logger.info(f"Using storage path: {self.storage_path}")
        
        camera_type = config.get('CAMERA', 'type', fallback='dahua')
        if camera_type == 'dahua':
            from video_grouper.cameras.dahua import DahuaCamera
            camera_config = {
                'device_ip': config.get('CAMERA', 'device_ip'),
                'username': config.get('CAMERA', 'username'),
                'password': config.get('CAMERA', 'password'),
                'storage_path': self.storage_path
            }
            logger.info(f"Initializing {camera_type} camera with IP: {camera_config['device_ip']}")
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
        """Initialize the application."""
        logger.info("Initializing VideoGrouperApp")
        
        # Create storage directory if it doesn't exist
        os.makedirs(self.storage_path, exist_ok=True)
        logger.info(f"Ensured storage directory exists: {self.storage_path}")
        
        # Clean up any orphaned DAV files at startup
        logger.info("Cleaning up orphaned DAV files...")
        deleted_count = await self.cleanup_dav_files()
        if deleted_count > 0:
            logger.info(f"Cleaned up {deleted_count} orphaned DAV files")
        else:
            logger.info("No orphaned DAV files found")
        
        if not self._queue_state_loaded:
            await self.load_queue_state()
            self._queue_state_loaded = True
            
        # Scan for existing files
        await scan_for_unprocessed_files(self.storage_path, self.processing_state)
        
        logger.info("Initialization complete")

    async def verify_file_complete(self, file_path: str) -> bool:
        """Verify if a file download is complete by checking its size against the server."""
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
        """Find and download new files from the camera."""
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
                filename = os.path.basename(server_path)
                
                # Parse start and end times from file_info
                start_time = None
                end_time = datetime.now()
                if 'startTime' in file_info:
                    try:
                        start_time = datetime.strptime(file_info['startTime'], '%Y-%m-%d %H:%M:%S')
                    except Exception as e:
                        logger.error(f"Error parsing start time: {e}")
                
                if 'endTime' in file_info:
                    try:
                        end_time = datetime.strptime(file_info['endTime'], '%Y-%m-%d %H:%M:%S')
                    except Exception as e:
                        logger.error(f"Error parsing end time: {e}")
                
                # Create a temporary FileState to use for finding the group directory
                temp_file_state = FileState(
                    file_path=os.path.join(self.storage_path, filename),
                    group_dir="",
                    start_time=start_time,
                    end_time=end_time
                )
                
                # Add to processing state temporarily to help with grouping
                self.processing_state.files[temp_file_state.file_path] = temp_file_state
                
                # Find the appropriate group directory for this file
                group_dir = find_group_directory(temp_file_state.file_path, self.storage_path, self.processing_state)
                
                # Remove temporary state if we're not going to keep it
                if temp_file_state.file_path in self.processing_state.files:
                    del self.processing_state.files[temp_file_state.file_path]
                
                # Set the local path to be in the group directory
                local_path = os.path.join(group_dir, filename)
                
                # Check if the file already exists
                if os.path.exists(local_path):
                    server_size = await self.camera.get_file_size(server_path)
                    local_size = os.path.getsize(local_path)
                    
                    if server_size == local_size:
                        logger.info(f"File {local_path} is already complete")
                        continue
                    else:
                        logger.info(f"File {local_path} is incomplete, downloading again")
                
                # Ensure the group directory exists
                os.makedirs(group_dir, exist_ok=True)
                
                # Download the file directly to the group directory
                success = await self.camera.download_file(server_path, local_path)
                if success:
                    logger.info(f"Successfully downloaded {local_path} to group directory {os.path.basename(group_dir)}")
                    
                    # Update the processing state with file metadata
                    self.processing_state.update_file_state(
                        local_path,
                        group_dir=group_dir,
                        status="downloaded",
                        start_time=start_time,
                        end_time=end_time
                    )
                    
                    latest_file_path = os.path.join(self.storage_path, LATEST_VIDEO_FILE)
                    self.queued_files.add((local_path, latest_file_path, end_time))
                    await self.ffmpeg_queue.put((local_path, latest_file_path, end_time))
                    self.save_queue_state()
                else:
                    logger.error(f"Failed to download {local_path}")
        except Exception as e:
            logger.error(f"Error finding and downloading files: {e}")

    def parse_match_info(self, file_path):
        """Parse match info file."""
        match_info_config = configparser.ConfigParser()
        match_info_config.read(file_path)
        return match_info_config["MATCH"] if "MATCH" in match_info_config else None

    def save_queue_state(self):
        """Save the current state of the ffmpeg queue."""
        try:
            # Helper function to handle datetime objects
            def serialize_item(item):
                if isinstance(item, tuple):
                    if len(item) == 3:
                        file_path, latest_file_path, end_time = item
                        return {
                            'type': 'conversion',
                            'file_path': file_path,
                            'latest_file_path': latest_file_path,
                            'end_time': end_time.isoformat() if end_time else None
                        }
                    elif len(item) == 4:
                        input_file, output_file, start_time_offset, total_duration = item
                        return {
                            'type': 'trim',
                            'input_file': input_file,
                            'output_file': output_file,
                            'start_time_offset': start_time_offset,
                            'total_duration': total_duration
                        }
                else:
                    return {
                        'type': 'combining',
                        'directory': item
                    }
            
            # Serialize queued_files
            serialized_queued_files = [serialize_item(item) for item in self.queued_files]
            
            # Serialize ffmpeg_queue (need to preserve the queue)
            temp_queue = asyncio.Queue()
            ffmpeg_queue_items = []
            
            while not self.ffmpeg_queue.empty():
                try:
                    item = self.ffmpeg_queue.get_nowait()
                    ffmpeg_queue_items.append(serialize_item(item))
                    temp_queue.put_nowait(item)
                except asyncio.QueueEmpty:
                    break
            
            # Restore the queue
            while not temp_queue.empty():
                try:
                    item = temp_queue.get_nowait()
                    self.ffmpeg_queue.put_nowait(item)
                except asyncio.QueueEmpty:
                    break
            
            queue_state = {
                'queued_files': serialized_queued_files,
                'ffmpeg_queue': ffmpeg_queue_items
            }
            
            queue_state_path = os.path.join(self.storage_path, "ffmpeg_queue_state.json")
            with open(queue_state_path, 'w') as f:
                json.dump(queue_state, f, indent=2)
            logger.info(f"Saved queue state to {queue_state_path} with {len(ffmpeg_queue_items)} items")
        except Exception as e:
            logger.error(f"Error saving queue state: {e}")

    async def load_queue_state(self):
        """Load the state of the ffmpeg queue from file."""
        try:
            queue_state_path = os.path.join(self.storage_path, "ffmpeg_queue_state.json")
            logger.info(f"Loading queue state from: {queue_state_path}")
            if os.path.exists(queue_state_path):
                with open(queue_state_path, 'r') as f:
                    state = json.load(f)
                    
                    # Helper function to deserialize items
                    def deserialize_item(item):
                        if item['type'] == 'conversion':
                            # Convert ISO format string back to datetime if not None
                            end_time = datetime.fromisoformat(item['end_time']) if item['end_time'] else None
                            return (item['file_path'], item['latest_file_path'], end_time)
                        elif item['type'] == 'trim':
                            return (item['input_file'], item['output_file'], item['start_time_offset'], item['total_duration'])
                        else:  # combining
                            return item['directory']
                    
                    # Clear existing queue
                    self.queued_files.clear()
                    while not self.ffmpeg_queue.empty():
                        try:
                            self.ffmpeg_queue.get_nowait()
                            self.ffmpeg_queue.task_done()
                        except asyncio.QueueEmpty:
                            break
                    
                    # Load queued files
                    for item_data in state.get('queued_files', []):
                        item = deserialize_item(item_data)
                        self.queued_files.add(item)
                    
                    # Load ffmpeg queue
                    for item_data in state.get('ffmpeg_queue', []):
                        item = deserialize_item(item_data)
                        await self.ffmpeg_queue.put(item)
                    
                    logger.info(f"Loaded queue state with {len(state.get('ffmpeg_queue', []))} items")
            else:
                logger.info(f"No queue state file found at {queue_state_path}")
        except Exception as e:
            logger.error(f"Error loading queue state: {e}")
            self.queued_files.clear()
            while not self.ffmpeg_queue.empty():
                try:
                    self.ffmpeg_queue.get_nowait()
                    self.ffmpeg_queue.task_done()
                except asyncio.QueueEmpty:
                    break

    def load_camera_state(self):
        """Load camera state from file."""
        try:
            camera_state_path = os.path.join(self.storage_path, "camera_state.json")
            logger.info(f"Loading camera state from: {camera_state_path}")
            if os.path.exists(camera_state_path):
                with open(camera_state_path, 'r') as f:
                    state = json.load(f)
                    self.connection_events = [(datetime.fromisoformat(d), s) for d, s in state.get('connection_events', [])]
                    logger.info(f"Loaded camera state with {len(self.connection_events)} events")
            else:
                logger.info(f"No camera state file found at {camera_state_path}")
                self.connection_events = []
        except Exception as e:
            logger.error(f"Error loading camera state: {e}")
            self.connection_events = []
            
    async def process_ffmpeg_queue(self):
        """Process the ffmpeg queue continuously."""
        logger.info("Starting ffmpeg queue processor")
        while True:
            try:
                if self.ffmpeg_queue.empty():
                    await asyncio.sleep(5)  # Wait a bit if queue is empty
                    continue
                    
                task = await self.ffmpeg_queue.get()
                
                if isinstance(task, tuple) and len(task) == 3:
                    file_path, latest_file_path, end_time = task
                    filename = os.path.basename(file_path)
                    logger.info(f"Processing {filename} from queue")
                    
                    try:
                        # Use the imported async_convert_file function from ffmpeg_utils
                        await async_convert_file(file_path, latest_file_path, end_time, filename)
                        # Remove from queue after successful processing
                        if (file_path, latest_file_path, end_time) in self.queued_files:
                            self.queued_files.remove((file_path, latest_file_path, end_time))
                            self.save_queue_state()
                    except Exception as e:
                        logger.error(f"Error processing {filename}: {e}")
                        # Requeue after error with a delay
                        await asyncio.sleep(5)
                        await self.ffmpeg_queue.put((file_path, latest_file_path, end_time))
                
                self.ffmpeg_queue.task_done()
            except Exception as e:
                logger.error(f"Error in ffmpeg queue processor: {e}")
                await asyncio.sleep(5)  # Back off on error
    
    async def poll_camera_and_download(self):
        """Poll the camera for availability and download files when available."""
        logger.info("Starting camera polling and download process")
        
        # Create storage directory if it doesn't exist
        os.makedirs(self.storage_path, exist_ok=True)
        
        while True:
            try:
                # Check if camera is available
                logger.info("Checking camera availability...")
                if await self.camera.check_availability():
                    logger.info("Camera is available, looking for files to download")
                    await self.find_and_download_files()
                else:
                    logger.warning("Camera is not available, will retry later")
                
                # Wait before next poll
                poll_interval = 60  # Default to 60 seconds
                if self.config.has_option('APP', 'check_interval_seconds'):
                    poll_interval = self.config.getint('APP', 'check_interval_seconds')
                
                logger.info(f"Waiting {poll_interval} seconds before next poll")
                await asyncio.sleep(poll_interval)
            except Exception as e:
                logger.error(f"Error in camera polling: {e}")
                await asyncio.sleep(30)  # Back off on error

    async def cleanup_dav_files(self, directory: str = None) -> int:
        """Delete any leftover DAV files in the specified directory or all directories.
        Returns the number of files deleted."""
        deleted_count = 0
        try:
            # If directory is specified, only clean that directory
            if directory:
                if not os.path.isdir(directory):
                    logger.error(f"Directory not found: {directory}")
                    return 0
                
                dirs_to_check = [directory]
            else:
                # Otherwise check all directories in storage path
                dirs_to_check = [os.path.join(self.storage_path, d) for d in os.listdir(self.storage_path) 
                               if os.path.isdir(os.path.join(self.storage_path, d))]
            
            # Process each directory
            for dir_path in dirs_to_check:
                try:
                    # Find DAV files with corresponding MP4 files
                    for file in os.listdir(dir_path):
                        if file.endswith('.dav'):
                            dav_path = os.path.join(dir_path, file)
                            mp4_path = dav_path.replace('.dav', '.mp4')
                            
                            # If MP4 exists and has content, we can delete the DAV file
                            if os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0:
                                try:
                                    # Verify MP4 is valid
                                    from video_grouper.ffmpeg_utils import get_video_duration
                                    duration = await get_video_duration(mp4_path)
                                    
                                    if duration and duration > 0:
                                        # Delete the DAV file
                                        try:
                                            os.remove(dav_path)
                                            logger.info(f"Deleted orphaned DAV file: {dav_path}")
                                            deleted_count += 1
                                        except Exception as e:
                                            logger.error(f"Failed to delete DAV file {dav_path}: {e}")
                                except Exception as e:
                                    logger.error(f"Error checking MP4 file {mp4_path}: {e}")
                except Exception as e:
                    logger.error(f"Error processing directory {dir_path}: {e}")
            
            return deleted_count
        except Exception as e:
            logger.error(f"Error in cleanup_dav_files: {e}")
            return 0

if __name__ == "__main__":
    config = configparser.ConfigParser()
    config.read("config.ini")
    app = VideoGrouperApp(config)
