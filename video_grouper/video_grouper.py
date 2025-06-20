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
                    time_difference = (file_start_time - last_file.end_time).total_seconds()
                    if 0 <= time_difference <= 15:
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
                            logger.info(f"AUDIT: Found pending/failed download in {group_dir}, re-adding to download queue: {file_obj.file_path}")
                            # Reconstruct a RecordingFile object to pass to the queue
                            recording_file = RecordingFile(
                                start_time=file_obj.start_time,
                                end_time=file_obj.end_time,
                                file_path=file_obj.file_path,
                                metadata=file_obj.metadata,
                                status=file_obj.status,
                                skip=file_obj.skip
                            )
                            await self.add_to_download_queue(recording_file)
                            
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

                    await dir_state.add_file(local_path, recording_file)
                    
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
                    
                    if self.camera.is_connected:
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
        """
        Handles the download of a single file from the camera.
        Updates the file's state upon success or failure.
        """
        file_path = recording_file.file_path
        group_dir = os.path.dirname(file_path)
        dir_state = DirectoryState(group_dir)

        try:
            logger.info(f"Starting download of {os.path.basename(file_path)}")
            await dir_state.update_file_state(file_path, status="downloading")
            
            # This is a placeholder for the actual download logic
            download_successful = await self.camera.download_file(
                file_path=recording_file.metadata['path'],
                local_path=file_path
            )

            if download_successful:
                await dir_state.update_file_state(file_path, status="downloaded")
                logger.info(f"Successfully downloaded {os.path.basename(file_path)}")
                
                # After successful download, add to FFmpeg queue
                await self.add_to_ffmpeg_queue(('convert', file_path))

                # Now that it's handled, remove it from the queue state
                self.queued_for_download.remove(file_path)
                await self._save_download_queue_state()
            else:
                await dir_state.update_file_state(file_path, status="download_failed")
                logger.error(f"Download failed for {os.path.basename(file_path)}")

        except Exception as e:
            logger.error(f"An error occurred during download of {os.path.basename(file_path)}: {e}", exc_info=True)
            await dir_state.update_file_state(file_path, status="download_failed")

    async def process_download_queue(self):
        """Continuously processes files from the download queue."""
        while True:
            recording_file = await self.download_queue.get()
            await self.handle_download_task(recording_file)
            self.download_queue.task_done()

    async def process_ffmpeg_queue(self):
        """Continuously processes tasks from the FFmpeg queue."""
        while True:
            task = await self.ffmpeg_queue.get()
            
            task_type, item_path = task
            try:
                if task_type == 'convert':
                    await self._handle_conversion_task(item_path)
                elif task_type == 'combine':
                    await self._handle_combine_task(item_path)
                elif task_type == 'trim':
                    await self._handle_trim_task(item_path)
                else:
                    logger.warning(f"Unknown ffmpeg task type: {task_type}")

                # If no exception, task is successful, remove from state
                self.queued_for_ffmpeg.remove(task)
                await self._save_ffmpeg_queue_state()

            except Exception as e:
                logger.error(f"Error processing FFmpeg task {task}, it will be retried on next run. Error: {e}", exc_info=True)
                # Task failed, do not remove from state, it will be re-queued on next startup
            
            self.ffmpeg_queue.task_done()

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
                await dir_state.set_file_status(file_path, "conversion_failed")
                logger.error(f"Conversion failed for {file_path}")
        except Exception as e:
            await dir_state.set_file_status(file_path, "conversion_failed")
            logger.error(f"An unexpected error occurred during conversion of {file_path}: {e}", exc_info=True)

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
                await dir_state.update_group_status("combine_failed", error_message="ffmpeg combine command failed.")
        except Exception as e:
            logger.error(f"Error during combine for {group_dir}: {e}")
            await dir_state.update_group_status("combine_failed", error_message=str(e))
        finally:
            if os.path.exists(file_list_path):
                os.remove(file_list_path)

    async def _handle_trim_task(self, group_dir: str, match_info_config: configparser.ConfigParser = None):
        """
        Handles trimming of a combined video file based on match_info.ini.
        If the match info is valid and the combined file exists, it will trim the video.
        """
        logger.info(f"TRIM: Handling trim task for {group_dir}")

        dir_state = DirectoryState(group_dir)
        match_config = match_info_config

        if match_config is None:
            if not self.is_match_info_populated(group_dir):
                logger.warning(f"TRIM: Match info for {group_dir} is not populated. Re-queueing.")
                await self._requeue_ffmpeg_task_later(('trim', group_dir), delay_seconds=60)
                return

            match_info_path = os.path.join(group_dir, "match_info.ini")
            match_config = configparser.ConfigParser()
            try:
                read_files = match_config.read(match_info_path)
                if not read_files:
                    logger.error(f"TRIM: Failed to read match_info.ini at {match_info_path}")
                    await dir_state.update_group_status("trim_failed", error_message="Failed to read match_info.ini")
                    return
            except configparser.Error as e:
                logger.error(f"TRIM: Error parsing match_info.ini for {group_dir}: {e}")
                await dir_state.update_group_status("trim_failed", error_message=f"Error parsing match_info.ini: {e}")
                return

        combined_path = os.path.join(group_dir, "combined.mp4")
        if not os.path.exists(combined_path):
            logger.error(f"TRIM: Combined video not found at {combined_path}. Cannot trim.")
            await dir_state.update_group_status("trim_failed", error_message="Combined video not found for trimming.")
            return

        try:
            my_team_name = match_config.get('MATCH', 'my_team_name')
            opponent_team_name = match_config.get('MATCH', 'opponent_team_name')
            location = match_config.get('MATCH', 'location')
            start_offset = match_config.get('MATCH', 'start_time_offset', fallback='00:00:00')
            total_duration_str = match_config.get('MATCH', 'total_duration')

            # Convert total_duration from HH:MM:SS to seconds
            h, m, s = map(int, total_duration_str.split(':'))
            total_duration_seconds = timedelta(hours=h, minutes=m, seconds=s).total_seconds()
            
            # Sanitize names for filename
            my_team_sanitized = re.sub(r'[^a-zA-Z0-9]', '', my_team_name).lower()
            opponent_sanitized = re.sub(r'[^a-zA-Z0-9]', '', opponent_team_name).lower()
            location_sanitized = re.sub(r'[^a-zA-Z0-9]', '', location).lower()

            output_filename = f"{my_team_sanitized}-{opponent_sanitized}-{location_sanitized}.mp4"
            output_path = os.path.join(os.path.dirname(group_dir), output_filename) # Save to parent directory of group

            logger.info(f"TRIM: Preparing to trim {combined_path} to {output_path} with offset {start_offset} and duration {total_duration_seconds}s")

            trim_successful = await trim_video(
                input_path=combined_path,
                output_path=output_path,
                start_offset=start_offset,
                duration=str(int(total_duration_seconds))
            )

            if trim_successful:
                logger.info(f"TRIM: Successfully trimmed video to {output_path}")
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
        """Adds a file to the download queue if it's not already present."""
        if recording_file.file_path not in self.queued_for_download:
            await self.download_queue.put(recording_file)
            self.queued_for_download.add(recording_file.file_path)
            logger.info(f"Added to download queue: {os.path.basename(recording_file.file_path)}")
            await self._save_download_queue_state()
        else:
            logger.debug(f"File {recording_file.file_path} is already in the download queue.")

    async def add_to_ffmpeg_queue(self, task: Tuple[str, Any]):
        """Adds a task to the FFmpeg queue if it's not already present."""
        if task not in self.queued_for_ffmpeg:
            await self.ffmpeg_queue.put(task)
            self.queued_for_ffmpeg.add(task)
            logger.info(f"Added task to FFmpeg queue: {task}")
            await self._save_ffmpeg_queue_state()
        else:
            logger.debug(f"Task {task} is already in the FFmpeg queue.")

    async def _save_download_queue_state(self):
        """Saves the current download queue to a JSON file."""
        queue_path = os.path.join(self.storage_path, DOWNLOAD_QUEUE_STATE_FILE)
        try:
            queue_items = []
            # Create a temporary copy of the queue to iterate over
            temp_queue = asyncio.Queue()
            while not self.download_queue.empty():
                item = await self.download_queue.get()
                queue_items.append(item)
                await temp_queue.put(item)
            
            # Restore the original queue
            self.download_queue = temp_queue

            # Now, `queue_items` contains all items from the queue
            # and they are also back in the queue for processing.
            data_to_save = [item.to_dict() for item in queue_items]

            async with aiofiles.open(queue_path, 'w') as f:
                await f.write(json.dumps(data_to_save, indent=4))
            logger.info(f"Saved download queue state with {len(data_to_save)} items.")
        except Exception as e:
            logger.error(f"Failed to save download queue state: {e}", exc_info=True)

    async def _save_ffmpeg_queue_state(self):
        """Saves the current FFmpeg queue to a JSON file."""
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
            logger.error(f"Failed to save FFmpeg queue state: {e}", exc_info=True)

    async def _load_queues_from_state(self):
        """Loads the download and FFmpeg queues from their state files."""
        # Load Download Queue
        download_queue_path = os.path.join(self.storage_path, DOWNLOAD_QUEUE_STATE_FILE)
        if os.path.exists(download_queue_path):
            try:
                async with aiofiles.open(download_queue_path, 'r') as f:
                    content = await f.read()
                    if not content.strip():
                        logger.warning("LOAD: download_queue_state.json is empty, skipping.")
                        return

                    items = json.loads(content)
                    if not isinstance(items, list):
                        logger.error(f"LOAD: download_queue_state.json is not a list, but a {type(items)}. Skipping.")
                        return

                    for item_data in items:
                        try:
                            # Reconstruct RecordingFile object from dict
                            recording_file = RecordingFile.from_dict(item_data)
                            await self.add_to_download_queue(recording_file)
                        except KeyError as e:
                            logger.warning(f"LOAD: Skipping malformed item in download_queue_state.json (missing key: {e}): {item_data}")
                    logger.info(f"LOAD: Loaded {len(items)} items from download_queue_state.json")

            except Exception as e:
                logger.error(f"Failed to load download queue state: {e}", exc_info=True)

        # Load FFmpeg Queue (existing logic)
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