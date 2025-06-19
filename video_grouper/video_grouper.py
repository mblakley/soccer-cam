import os
import re
import json
import asyncio
import logging
import configparser
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, List, Any
import aiofiles

from video_grouper.ffmpeg_utils import async_convert_file, get_video_duration, create_screenshot, trim_video
from video_grouper.directory_state import DirectoryState
from video_grouper.models import RecordingFile
from video_grouper.cameras.dahua import DahuaCamera

# Constants
LATEST_VIDEO_FILE = "latest_video.txt"
DOWNLOAD_QUEUE_STATE_FILE = "download_queue_state.json"
FFMPEG_QUEUE_STATE_FILE = "ffmpeg_queue_state.json"
DEFAULT_STORAGE_PATH = "./shared_data"
default_date_format = "%Y-%m-%d %H:%M:%S"

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s'
)
logger = logging.getLogger(__name__)

def create_directory(path):
    """Create a directory if it doesn't exist."""
    os.makedirs(path, exist_ok=True)

def find_group_directory(file_start_time: datetime, storage_path: str, existing_dirs: List[str]) -> str:
    """
    Finds or creates a group directory for a video file based on its start time.
    A new group is created if the file's start time is more than 15 seconds after the previous file's end time.
    """
    # Check existing directories to find a match
    for group_dir_path in sorted(existing_dirs, reverse=True):
        state_file_path = os.path.join(group_dir_path, "state.json")
        if os.path.exists(state_file_path):
            try:
                dir_state = DirectoryState(group_dir_path)
                last_file = dir_state.get_last_file()
                if last_file and last_file.end_time:
                    # Dahua cameras can have a small gap between files of the same recording
                    if 0 <= (file_start_time - last_file.end_time).total_seconds() <= 15:
                        logger.info(f"Found matching group directory {os.path.basename(group_dir_path)} for file starting at {file_start_time}")
                        return group_dir_path
            except Exception as e:
                logger.error(f"Error reading state for {group_dir_path}: {e}")

    # No matching directory found, create a new one
    new_dir_name = file_start_time.strftime("%Y.%m.%d-%H.%M.%S")
    new_dir_path = os.path.join(storage_path, new_dir_name)
    create_directory(new_dir_path)
    logger.info(f"Created new group directory {new_dir_path} for file starting at {file_start_time}")
    return new_dir_path

class VideoGrouperApp:
    def __init__(self, config, camera=None):
        self.config = config
        self.storage_path = os.path.abspath(config.get('STORAGE', 'path', fallback=DEFAULT_STORAGE_PATH))
        logger.info(f"Using storage path: {self.storage_path}")
        
        if camera:
            self.camera = camera
        else:
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
                self.camera = DahuaCamera(**camera_config)
            else:
                raise ValueError(f"Unsupported camera type: {camera_type}")
        
        self.download_queue = asyncio.Queue()
        self.ffmpeg_queue = asyncio.Queue()
        self.camera_connected = asyncio.Event()
        
        self.queued_for_download = set()
        self.queued_for_ffmpeg = set()
        
        self.poll_interval = self.config.getint('APP', 'check_interval_seconds', fallback=60)
        
        self._queues_loaded = False
        self.camera_was_connected = False

    async def initialize(self):
        """Initialize the application by scanning existing files and populating queues."""
        logger.info("Initializing VideoGrouperApp")
        create_directory(self.storage_path)

        if not self._queues_loaded:
            await self._load_queues_from_state()
            self._queues_loaded = True

        logger.info("Scanning for existing group directories to audit state...")
        for item in os.listdir(self.storage_path):
            group_dir = os.path.join(self.storage_path, item)
            if os.path.isdir(group_dir) and not item.startswith('.'):
                state_file_path = os.path.join(group_dir, "state.json")
                if not os.path.exists(state_file_path):
                    continue

                logger.info(f"Auditing state for {os.path.basename(group_dir)}")
                try:
                    dir_state = DirectoryState(group_dir)
                    files_to_process = list(dir_state.files.values())

                    for file_obj in files_to_process:
                        if file_obj.skip:
                            logger.info(f"AUDIT: Skipping file {file_obj.file_path} in {group_dir} as per state file.")
                            continue

                        if file_obj.status == "downloaded" and file_obj.file_path not in self.queued_for_ffmpeg:
                            task = ('convert', file_obj.file_path)
                            logger.info(f"AUDIT: Found downloaded file in {group_dir}, adding to FFmpeg queue: {task}")
                            await self.add_to_ffmpeg_queue(task)
                        
                        elif file_obj.status in ["pending", "download_failed"] and file_obj.file_path not in self.queued_for_download:
                            logger.info(f"AUDIT: Found pending/failed download in {group_dir}, adding to download queue: {file_obj.file_path}")
                            await self.add_to_download_queue(file_obj)
                            
                        elif file_obj.status == "conversion_failed" and ('convert', file_obj.file_path) not in self.queued_for_ffmpeg:
                            logger.info(f"AUDIT: Found failed conversion in {group_dir}, re-queuing for conversion: {file_obj.file_path}")
                            await self.add_to_ffmpeg_queue(('convert', file_obj.file_path))

                    if dir_state.is_ready_for_combining() and ('combine', group_dir) not in self.queued_for_ffmpeg:
                        combined_path = os.path.join(group_dir, "combined.mp4")
                        if not os.path.exists(combined_path):
                            task = ('combine', group_dir)
                            logger.info(f"AUDIT: All files converted in {group_dir}, adding combine task to FFmpeg queue: {task}")
                            await self.add_to_ffmpeg_queue(task)

                    # New audit for trimming combined videos with populated info
                    if dir_state.status == "combined":
                        await self._ensure_match_info_exists(group_dir)
                        combined_path = os.path.join(group_dir, "combined.mp4")
                        if os.path.exists(combined_path):
                            if self.is_match_info_populated(group_dir):
                                task = ('trim', group_dir)
                                if task not in self.queued_for_ffmpeg:
                                    logger.info(f"AUDIT: Found combined group with populated match info and combined.mp4 in {group_dir}. Queueing trim: {task}")
                                    await self.add_to_ffmpeg_queue(task)
                            else:
                                logger.info(f"AUDIT: Found combined group with no match_info.ini in {group_dir}. Not ready to trim.")
                        else:
                            logger.info(f"AUDIT: Found combined group with missing combined.mp4 in {group_dir}. Please check the group directory.")

                except Exception as e:
                    logger.error(f"Error during state audit for {group_dir}: {e}")
        
        logger.info("Initialization complete")

    async def run(self):
        """Run the application."""
        logger.info("Running VideoGrouperApp")
        await self.initialize()
        
        tasks = [
            asyncio.create_task(self.poll_camera_for_files()),
            asyncio.create_task(self.process_download_queue()),
            asyncio.create_task(self.process_ffmpeg_queue()),
        ]
        
        await asyncio.gather(*tasks)

    async def shutdown(self):
        """Saves the state of all queues to disk."""
        logger.info("Saving all queue states...")
        await self._save_download_queue_state()
        await self._save_ffmpeg_queue_state()
        logger.info("All queue states saved.")

    async def sync_files_from_camera(self):
        start_time = await self._get_latest_processed_time()
        if start_time:
            start_time -= timedelta(minutes=1)
        
        end_time = datetime.now()
        
        logger.info(f"Looking for new files from: {start_time} to {end_time}")
        
        files = await self.camera.get_file_list(start_time=start_time, end_time=end_time)
        
        if not files:
            logger.info("No new files found on the camera since last sync.")
        else:
            logger.info(f"Found {len(files)} new files to process.")
            existing_dirs = [os.path.join(self.storage_path, d) for d in os.listdir(self.storage_path) if os.path.isdir(os.path.join(self.storage_path, d))]
            
            latest_end_time = None

            for file_info in files:
                try:
                    filename = os.path.basename(file_info['path'])
                    file_start_time = datetime.strptime(file_info['startTime'], default_date_format)
                    file_end_time = datetime.strptime(file_info['endTime'], default_date_format)

                    if latest_end_time is None or file_end_time > latest_end_time:
                        latest_end_time = file_end_time

                    group_dir = find_group_directory(file_start_time, self.storage_path, existing_dirs)
                    if group_dir not in existing_dirs:
                        existing_dirs.append(group_dir)

                    local_path = os.path.join(group_dir, filename)
                    
                    dir_state = DirectoryState(group_dir)
                    if dir_state.is_file_in_state(local_path) or local_path in self.queued_for_download:
                        logger.debug(f"File {filename} is already known. Skipping.")
                        continue
                    
                    recording_file = RecordingFile(
                        start_time=file_start_time,
                        end_time=file_end_time,
                        file_path=local_path,
                        metadata=file_info
                    )
                    
                    # Preserve skip status if file already existed in some state
                    existing_file_obj = dir_state.get_file_by_path(local_path)
                    if existing_file_obj:
                        recording_file.skip = existing_file_obj.skip

                    await dir_state.add_file(recording_file)
                    
                    # Add to download queue if not skipped
                    if not recording_file.skip:
                        await self.add_to_download_queue(recording_file)
                    else:
                        logger.info(f"Skipping download for {os.path.basename(local_path)} as per state file.")

                except Exception as e:
                    logger.error(f"Error processing file info {file_info}: {e}")
            
            if latest_end_time:
                await self._update_latest_processed_time(latest_end_time)
                logger.info(f"File sync complete. New high-water mark set to: {latest_end_time}")

    async def poll_camera_for_files(self):
        """Polls the camera for new files and manages the camera_connected event."""
        logger.info("Starting camera poller.")
        while True:
            try:
                is_available = await self.camera.check_availability()
                if is_available:
                    if not self.camera_connected.is_set():
                        logger.info("Camera is connected. Starting downloads.")
                        self.camera_connected.set()
                    
                    if self.camera.connection_events and self.camera.connection_events[-1][1] == "connected":
                        await self.sync_files_from_camera()
                else:
                    if self.camera_connected.is_set():
                        logger.warning("Camera is disconnected. Pausing downloads.")
                        self.camera_connected.clear()
                
                await asyncio.sleep(self.poll_interval)
            except Exception as e:
                logger.error(f"Error polling camera: {e}", exc_info=True)
                if self.camera_connected.is_set():
                    self.camera_connected.clear()
                await asyncio.sleep(self.poll_interval)

    async def handle_download_task(self, recording_file: RecordingFile):
        logger.info(f"Downloading {os.path.basename(recording_file.file_path)}...")
        
        group_dir = os.path.dirname(recording_file.file_path)
        dir_state = DirectoryState(group_dir)

        try:
            # Re-check skip status right before processing
            file_state = dir_state.get_file_by_path(recording_file.file_path)
            if file_state and file_state.skip:
                logger.info(f"Skipping download for {os.path.basename(recording_file.file_path)} because 'skip' is true.")
                return

            server_path = recording_file.metadata.get('path')
            if not server_path:
                raise ValueError("Missing 'path' in recording_file metadata")

            download_successful = await self.camera.download_file(
                file_path=server_path,
                local_path=recording_file.file_path
            )

            if download_successful:
                logger.info(f"Successfully downloaded {os.path.basename(recording_file.file_path)}")
                
                # Verify file size
                if os.path.getsize(recording_file.file_path) == 0:
                    raise Exception("Downloaded file is empty.")

                await dir_state.update_file_status(recording_file.file_path, "downloaded")
                await self.add_to_ffmpeg_queue(('convert', recording_file.file_path))
            else:
                raise Exception("Download failed.")

        except Exception as e:
            logger.error(f"Failed to download {os.path.basename(recording_file.file_path)}: {e}")
            await dir_state.update_file_status(recording_file.file_path, "download_failed")
            
            # Optional: Add retry logic here if desired
            await asyncio.sleep(5) # Wait before allowing a potential retry

        finally:
            self.download_queue.task_done()
            if recording_file.file_path in self.queued_for_download:
                self.queued_for_download.remove(recording_file.file_path)
            await self._save_download_queue_state()

    async def process_download_queue(self):
        """Processes the download queue, waiting for camera connection."""
        logger.info("Starting download queue processor. Waiting for camera connection...")
        while True:
            try:
                await self.camera_connected.wait()
                
                recording_file = await self.download_queue.get()
                
                if not self.camera_connected.is_set():
                    logger.warning("Camera disconnected while fetching from queue. Re-queueing.")
                    await self.download_queue.put(recording_file)
                    continue

                await self.handle_download_task(recording_file)
            except asyncio.CancelledError:
                logger.info("Download queue processing cancelled.")
                break
            except Exception as e:
                logger.error(f"Error in download queue processor: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def process_ffmpeg_queue(self):
        """Processes the ffmpeg queue for converting, combining, and trimming videos."""
        logger.info("Starting FFmpeg queue processor.")
        while True:
            try:
                task = await self.ffmpeg_queue.get()
                
                try:
                    task_type, payload = task
                except ValueError:
                    logger.error(f"Skipping malformed task in FFmpeg queue: {task}")
                    self.ffmpeg_queue.task_done()
                    continue

                task_handled = True
                if task_type == 'convert':
                    await self._handle_conversion_task(payload)
                elif task_type == 'combine':
                    await self._handle_combine_task(payload)
                elif task_type == 'trim':
                    task_handled = await self._handle_trim_task(payload)
                else:
                    logger.warning(f"Unknown ffmpeg task type: {task_type}")

                if task_handled:
                    self.queued_for_ffmpeg.discard(task)
                
                self.ffmpeg_queue.task_done()

            except Exception as e:
                logger.error(f"Error in ffmpeg queue processor: {e}", exc_info=True)
                # If the task itself caused the error, we still need to mark it as done.
                if 'task' in locals() and not self.ffmpeg_queue.empty():
                    self.ffmpeg_queue.task_done()
                await asyncio.sleep(5)

    async def _handle_conversion_task(self, file_path: str):
        """Handle a video conversion task."""
        filename = os.path.basename(file_path)
        group_dir = os.path.dirname(file_path)
        dir_state = DirectoryState(group_dir)
        file_obj = dir_state.files.get(file_path)
                        
        if not file_obj:
            logger.error(f"File {filename} not found in state for conversion. Skipping.")
            return

        # Re-check skip status right before processing
        if file_obj.skip:
            logger.info(f"Skipping conversion for {filename} because 'skip' is true.")
            return
                        
        logger.info(f"Converting {filename} to MP4...")
        try:
            mp4_path = await async_convert_file(file_path)
            if mp4_path and os.path.exists(mp4_path):
                logger.info(f"Successfully converted {filename}.")
                
                # Create screenshot from the converted MP4
                screenshot_path = mp4_path.replace('.mp4', '_screenshot.jpg')
                screenshot_success = await create_screenshot(mp4_path, screenshot_path)
                
                final_screenshot_path = screenshot_path if screenshot_success else None

                await dir_state.update_file_status(file_path, "converted", screenshot_path=final_screenshot_path)

                # Create match_info.ini if it doesn't exist
                await self._ensure_match_info_exists(group_dir)

                await self.cleanup_dav_files(group_dir)

                if dir_state.is_ready_for_combining():
                    logger.info(f"Group {os.path.basename(group_dir)} is ready for combining.")
                    await self.add_to_ffmpeg_queue(('combine', group_dir))
            else:
                raise Exception("Conversion resulted in no output file.")
        except Exception as e:
            logger.error(f"Error during conversion task for {file_path}: {e}")
            await dir_state.update_file_status(file_path, "conversion_failed")

    async def _handle_combine_task(self, group_dir: str):
        """Combines all converted MP4 files in a group directory."""
        logger.info(f"Starting combine task for {group_dir}")
        dir_state = DirectoryState(group_dir)
        
        mp4_files = sorted([f.file_path.replace('.dav', '.mp4') for f in dir_state.get_files_by_status("converted")])
        
        if not mp4_files:
            logger.warning(f"No MP4 files to combine in {group_dir}")
            return

        combined_path = os.path.join(group_dir, "combined.mp4")
        file_list_path = os.path.join(group_dir, "filelist.txt")

        try:
            async with aiofiles.open(file_list_path, 'w') as f:
                for mp4_file in mp4_files:
                    # Format for ffmpeg concat demuxer
                    await f.write(f"file '{os.path.basename(mp4_file)}'\n")
            
            cmd = [
                'ffmpeg',
                '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', file_list_path,
                '-c', 'copy',
                combined_path
            ]
            
            logger.info(f"Running ffmpeg combine command: {' '.join(cmd)}")
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            await process.wait()

            if process.returncode == 0:
                logger.info(f"Successfully combined videos in {group_dir}")
                await dir_state.update_group_status("combined")
                
                # Check for match_info.ini and queue trim task if it exists
                if os.path.exists(os.path.join(group_dir, "match_info.ini")):
                    await self.add_to_ffmpeg_queue(('trim', group_dir))
            else:
                logger.error(f"Failed to combine videos in {group_dir}")

        except Exception as e:
            logger.error(f"Error during combine task for {group_dir}: {e}")
        finally:
            # Clean up the file list
            if os.path.exists(file_list_path):
                os.remove(file_list_path)

    async def _handle_trim_task(self, group_dir: str):
        """Trims the combined video based on match_info.ini."""
        logger.info(f"Starting trim task for {group_dir}")
        dir_state = DirectoryState(group_dir)
        combined_path = os.path.join(group_dir, "combined.mp4")

        if not os.path.exists(combined_path):
            logger.warning(f"Cannot trim: combined.mp4 does not exist in {group_dir}. Was it deleted?")
            return True # Consider this handled, don't requeue
            
        if not self.is_match_info_populated(group_dir):
            logger.info(f"Match info for {group_dir} is not populated. Re-queueing trim task for later.")
            asyncio.create_task(self._requeue_ffmpeg_task_later(('trim', group_dir), 60))
            return False # Not handled, needs requeue

        try:
            match_info_path = os.path.join(group_dir, "match_info.ini")
            match_info = self.parse_match_info(match_info_path)
            if not match_info:
                logger.error(f"Could not parse match_info.ini in {group_dir}")
                return True # Consider this handled, don't requeue
            
            my_team_name = match_info.get('my_team_name')
            opponent_team_name = match_info.get('opponent_team_name')
            location = match_info.get('location')
            start_offset = match_info.get('start_time_offset')
            total_duration_str = match_info.get('total_duration')

            # Extract date from group_dir name (e.g., "2025.06.20" from "2025.06.20-12.30.00")
            try:
                group_date_str = os.path.basename(group_dir).split('-')[0]
                date_for_filename = datetime.strptime(group_date_str, "%Y.%m.%d").strftime("%m-%d-%Y")
            except (ValueError, IndexError):
                logger.warning(f"Could not parse date from group directory name: {os.path.basename(group_dir)}. Using current date as fallback.")
                now = datetime.now()
                group_date_str = now.strftime("%Y.%m.%d")
                date_for_filename = now.strftime("%m-%d-%Y")

            subdir_name = f"{group_date_str} - {my_team_name} vs {opponent_team_name} ({location})"
            output_dir = os.path.join(group_dir, subdir_name)
            create_directory(output_dir)
            
            base_filename = f"{my_team_name}-{opponent_team_name}-{location}-{date_for_filename}-raw.mp4"
            filename = base_filename.lower().replace(' ', '')
            output_path = os.path.join(output_dir, filename)

            total_duration_seconds_str = None
            if total_duration_str and total_duration_str.strip():
                try:
                    parts = total_duration_str.strip().split(':')
                    parts = [int(p) for p in parts]
                    total_duration_seconds = 0
                    if len(parts) == 1:
                        total_duration_seconds = parts[0]
                    elif len(parts) == 2:
                        total_duration_seconds = parts[0] * 60 + parts[1]
                    elif len(parts) == 3:
                        total_duration_seconds = parts[0] * 3600 + parts[1] * 60 + parts[2]
                    
                    if total_duration_seconds > 0:
                        total_duration_seconds_str = str(total_duration_seconds)

                except (ValueError, TypeError):
                    logger.warning(f"Invalid 'total_duration' value '{total_duration_str}' in {os.path.join(group_dir, 'match_info.ini')}. Ignoring.")

            trim_successful = await trim_video(
                input_path=combined_path,
                output_path=output_path,
                start_offset=str(start_offset),
                duration=total_duration_seconds_str
            )

            if trim_successful:
                logger.info(f"Successfully trimmed video for {group_dir}")
                await dir_state.update_group_status("trimmed")
            else:
                logger.error(f"Failed to trim video for {group_dir}")

        except Exception as e:
            logger.error(f"Error during trim task for {group_dir}: {e}")
            
        return True # Handled

    async def _ensure_match_info_exists(self, group_dir: str):
        """Creates match_info.ini in the group directory if it doesn't exist."""
        match_info_path = os.path.join(group_dir, "match_info.ini")
        if not os.path.exists(match_info_path):
            logger.info(f"Creating default match_info.ini in {group_dir}")
            try:
                # Assuming video_grouper.py and match_info.ini.dist are in the same directory
                source_dist_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "match_info.ini.dist")
                async with aiofiles.open(source_dist_path, 'r') as src:
                    content = await src.read()
                    async with aiofiles.open(match_info_path, 'w') as dest:
                        await dest.write(content)
            except Exception as e:
                logger.error(f"Failed to create match_info.ini from dist: {e}")

    def is_match_info_populated(self, group_dir: str) -> bool:
        """Checks if the match_info.ini file exists and is populated with non-default values."""
        match_info_path = os.path.join(group_dir, "match_info.ini")
        if not os.path.exists(match_info_path):
            return False
        
        try:
            config = configparser.ConfigParser()
            config.read(match_info_path)
            
            if not config.has_section('MATCH'):
                return False
            
            required_fields = ['my_team_name', 'opponent_team_name', 'location', 'start_time_offset']
            for field in required_fields:
                value = config.get('MATCH', field, fallback='').strip()
                if not value: # Check if value is an empty string
                    return False
            
            return True
        except configparser.Error:
            logger.error(f"Error parsing match_info.ini in {group_dir}")
            return False

    def parse_match_info(self, file_path: str) -> Optional[Dict[str, str]]:
        """Parse match info file."""
        config = configparser.ConfigParser()
        config.read(file_path)
        return dict(config['MATCH']) if 'MATCH' in config else None

    async def add_to_download_queue(self, recording_file: RecordingFile):
        """Add a file to the download queue and save state."""
        if recording_file.file_path not in self.queued_for_download:
            self.queued_for_download.add(recording_file.file_path)
            await self.download_queue.put(recording_file)
            logger.info(f"Added {os.path.basename(recording_file.file_path)} to download queue.")

    async def add_to_ffmpeg_queue(self, task: Tuple[str, Any]):
        """Add a task to the ffmpeg queue and save state."""
        if task not in self.queued_for_ffmpeg:
            self.queued_for_ffmpeg.add(task)
            await self.ffmpeg_queue.put(task)
            logger.info(f"Added task {task[0]}:{os.path.basename(str(task[1]))} to FFmpeg queue.")

    async def _save_download_queue_state(self):
        """Save the current state of the download queue."""
        queue_path = os.path.join(self.storage_path, DOWNLOAD_QUEUE_STATE_FILE)
        try:
            # Drain queue to a list, serialize, then refill
            items = []
            while not self.download_queue.empty():
                items.append(await self.download_queue.get())

            # Serialize items
            data_to_save = [item.to_dict() for item in items]

            async with aiofiles.open(queue_path, 'w') as f:
                await f.write(json.dumps(data_to_save, indent=2))

            # Refill queue
            for item in items:
                await self.download_queue.put(item)
        except Exception as e:
            logger.error(f"Error saving download queue state: {e}")

    async def _save_ffmpeg_queue_state(self):
        """Save the current state of the ffmpeg queue."""
        queue_path = os.path.join(self.storage_path, FFMPEG_QUEUE_STATE_FILE)
        try:
            items = []
            while not self.ffmpeg_queue.empty():
                items.append(await self.ffmpeg_queue.get())
            
            # Serialize the drained items
            data_to_save = [list(item) for item in items]

            logger.info(f"Saving FFmpeg queue state with {len(data_to_save)} items: {data_to_save}")
            async with aiofiles.open(queue_path, 'w') as f:
                await f.write(json.dumps(data_to_save, indent=2))
            
            # Refill the queue
            for item in items:
                await self.ffmpeg_queue.put(item)
        except Exception as e:
            logger.error(f"Error saving ffmpeg queue state: {e}")

    async def _load_queues_from_state(self):
        """Load queue states from files."""
        # Load download queue
        download_queue_path = os.path.join(self.storage_path, DOWNLOAD_QUEUE_STATE_FILE)
        if os.path.exists(download_queue_path):
            try:
                async with aiofiles.open(download_queue_path, 'r') as f:
                    content = await f.read()
                    items = json.loads(content)
                    for item_data in items:
                        rf = RecordingFile.from_dict(item_data)
                        await self.add_to_download_queue(rf)
                logger.info(f"Loaded {self.download_queue.qsize()} items into download queue.")
            except Exception as e:
                logger.error(f"Error loading download queue state: {e}")

        # Load ffmpeg queue
        ffmpeg_queue_path = os.path.join(self.storage_path, FFMPEG_QUEUE_STATE_FILE)
        if os.path.exists(ffmpeg_queue_path):
            try:
                async with aiofiles.open(ffmpeg_queue_path, 'r') as f:
                    content = await f.read()
                    logger.info(f"LOAD: Reading ffmpeg_queue_state.json raw content: '{content}'")
                    if not content.strip():
                        logger.warning("LOAD: ffmpeg_queue_state.json is empty, skipping.")
                        return
                        
                    items = json.loads(content)
                    logger.info(f"LOAD: Parsed items from ffmpeg_queue_state.json: {items}")
                    if not isinstance(items, list):
                        logger.error(f"LOAD: ffmpeg_queue_state.json is not a list, but a {type(items)}. Skipping.")
                        return

                    for item in items:
                        if isinstance(item, list) and len(item) == 2:
                            logger.info(f"LOAD: Adding task to FFmpeg queue: {tuple(item)}")
                            await self.add_to_ffmpeg_queue(tuple(item))
                        else:
                            logger.warning(f"LOAD: Skipping malformed item in ffmpeg_queue_state.json: {item}")

                logger.info(f"LOAD: Finished loading FFmpeg queue. Total items: {self.ffmpeg_queue.qsize()}")
            except json.JSONDecodeError:
                logger.error(f"LOAD: Error decoding JSON from {ffmpeg_queue_path}. The file might be corrupted.")
            except Exception as e:
                logger.error(f"LOAD: Error loading ffmpeg queue state: {e}")

    async def _get_latest_processed_time(self) -> Optional[datetime]:
        """Get the timestamp of the last processed video file."""
        file_path = os.path.join(self.storage_path, LATEST_VIDEO_FILE)
        if not os.path.exists(file_path):
            return None
        try:
            async with aiofiles.open(file_path, "r") as f:
                timestamp_str = await f.read()
                return datetime.strptime(timestamp_str.strip(), default_date_format)
        except Exception as e:
            logger.error(f"Could not read or parse latest video file timestamp: {e}")
            return None

    async def _update_latest_processed_time(self, timestamp: datetime):
        """Update the high-water mark for file processing."""
        try:
            latest_file_path = os.path.join(self.storage_path, LATEST_VIDEO_FILE)
            async with aiofiles.open(latest_file_path, 'w') as f:
                await f.write(timestamp.strftime(default_date_format))
            logger.info(f"Updated latest processed time to: {timestamp}")
        except Exception as e:
            logger.error(f"Error updating latest processed time: {e}")

    async def cleanup_dav_files(self, directory: Optional[str] = None):
        """
        Removes .dav files if a corresponding valid .mp4 file exists in the same directory.
        If directory is None, scans all subdirectories in the storage path.
        """
        top_level_dir = directory if directory else self.storage_path
        deleted_count = 0
        
        # Determine which directories to scan
        dirs_to_scan = [top_level_dir]
        if directory is None:
            try:
                dirs_to_scan = [os.path.join(top_level_dir, d) for d in os.listdir(top_level_dir) if os.path.isdir(os.path.join(top_level_dir, d))]
            except FileNotFoundError:
                logger.warning(f"Storage directory not found: {top_level_dir}")
                return 0
        
        for dir_path in dirs_to_scan:
            try:
                dav_files = {f: f.replace('.dav', '.mp4') for f in os.listdir(dir_path) if f.endswith('.dav')}
                
                for dav_file, mp4_file in dav_files.items():
                    dav_path = os.path.join(dir_path, dav_file)
                    mp4_path = os.path.join(dir_path, mp4_file)
                    
                    if os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0:
                        try:
                            # Verify MP4 duration is reasonable (e.g., > 0)
                            mp4_duration = await get_video_duration(mp4_path)
                            if mp4_duration and mp4_duration > 0:
                                os.remove(dav_path)
                                deleted_count += 1
                                logger.info(f"Removed orphaned DAV file: {dav_path}")
                        except Exception as e:
                            logger.error(f"Error processing file {dav_path} for cleanup: {e}")
            except Exception as e:
                logger.error(f"Error processing directory {dir_path} for cleanup: {e}")
        return deleted_count

    async def _requeue_ffmpeg_task_later(self, task: Tuple[str, Any], delay_seconds: int):
        """Waits for a delay and then adds a task back to the FFmpeg queue."""
        await asyncio.sleep(delay_seconds)
        logger.info(f"Re-queueing delayed task: {task}")
        await self.ffmpeg_queue.put(task)