import os
import re
import json
import asyncio
import logging
import configparser
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, List, Any, Set, Union
import aiofiles
import pytz
import shutil
import time
import traceback

from video_grouper.ffmpeg_utils import async_convert_file, get_video_duration, create_screenshot, trim_video
from video_grouper.directory_state import DirectoryState
from video_grouper.models import RecordingFile, MatchInfo, FFmpegTask, ConvertTask, CombineTask, TrimTask, YouTubeUploadTask, create_ffmpeg_task, task_from_dict
from video_grouper.cameras.dahua import DahuaCamera
from video_grouper.youtube_upload import upload_group_videos, get_youtube_paths
from video_grouper.api_integrations.teamsnap import TeamSnapAPI
from video_grouper.api_integrations.ntfy import NtfyAPI
from video_grouper.api_integrations.playmetrics.api import PlayMetricsAPI

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
        
        # Initialize TeamSnap API if enabled
        self.teamsnap_api = None
        if self.config.has_section('TEAMSNAP') and self.config.getboolean('TEAMSNAP', 'enabled', fallback=False):
            logger.info("Initializing TeamSnap API integration")
            config_path = os.path.join("shared_data", "config.ini")
            self.teamsnap_api = TeamSnapAPI(config_path)
        
        # Initialize PlayMetrics API if enabled
        self.playmetrics_api = None
        if self.config.has_section('PLAYMETRICS') and self.config.getboolean('PLAYMETRICS', 'enabled', fallback=False):
            logger.info("Initializing PlayMetrics API integration")
            config_path = os.path.join("shared_data", "config.ini")
            self.playmetrics_api = PlayMetricsAPI(config_path)
        
        self.ntfy_api = None  # NTFY API client
        self.download_queue = asyncio.Queue()
        self.ffmpeg_queue = asyncio.Queue()
        self.camera_connected = asyncio.Event()
        
        self.queued_for_download = set()
        self.queued_for_ffmpeg = set()
        
        self.poll_interval = self.config.getint('APP', 'check_interval_seconds', fallback=60)
        
        self._queues_loaded = False
        self.camera_was_connected = False
        self._last_processed_time = None
        self._last_connected_check = None
        self._connected_timeframes = []
        self._camera_initialized = False
        self._teamsnap_initialized = False
        self._playmetrics_initialized = False
        self._ntfy_initialized = False  # NTFY initialization flag
        self._tasks = []
        self._shutdown_event = asyncio.Event()
        
        # Track processed directories to avoid duplicate processing
        self._ntfy_processed_dirs = set()
        self._teamsnap_processed_dirs = set()
        self._playmetrics_processed_dirs = set()

    async def initialize(self):
        """Initialize the application by scanning existing files and populating queues."""
        logger.info("Initializing VideoGrouperApp")
        create_directory(self.storage_path)
        
        # Filter existing state files to remove recordings that overlap with connected timeframes
        await self._filter_existing_state_files()

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

                        if file_obj.status == "downloaded" and file_obj.file_path not in self.queued_for_download:
                            task = ConvertTask(file_obj.file_path)
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

                        elif file_obj.status == "conversion_failed" and file_obj.file_path not in self.queued_for_ffmpeg:
                            logger.info(f"AUDIT: Found failed conversion in {group_dir}, re-queuing for conversion: {file_obj.file_path}")
                            await self.add_to_ffmpeg_queue(ConvertTask(file_obj.file_path))

                    if dir_state.is_ready_for_combining() and group_dir not in self.queued_for_ffmpeg:
                        combined_path = os.path.join(group_dir, "combined.mp4")
                        if not os.path.exists(combined_path):
                            task = CombineTask(group_dir)
                            logger.info(f"AUDIT: All files converted in {group_dir}, adding combine task to FFmpeg queue: {task}")
                            await self.add_to_ffmpeg_queue(task)

                    # New audit for trimming combined videos with populated info
                    if dir_state.status == "combined":
                        await self._ensure_match_info_exists(group_dir)
                        combined_path = os.path.join(group_dir, "combined.mp4")
                        if os.path.exists(combined_path):
                            # Check if NTFY integration is enabled and match info is not populated
                            if self.ntfy_api and self.ntfy_api.enabled and not self.is_match_info_populated(group_dir):
                                logger.info(f"AUDIT: Found combined video in {group_dir} that needs NTFY processing")
                                # Create a task to process this combined video with NTFY
                                asyncio.create_task(self._process_combined_directory(group_dir, combined_path))
                            elif self.is_match_info_populated(group_dir):
                                # Create a MatchInfo object for the task
                                match_info_path = os.path.join(group_dir, "match_info.ini")
                                match_info = MatchInfo.from_file(match_info_path)
                                if match_info:
                                    # Create a TrimTask
                                    task = TrimTask(group_dir, match_info)
                                    if task not in self.queued_for_ffmpeg:
                                        logger.info(f"AUDIT: Found combined group with populated match info and combined.mp4 in {group_dir}. Queueing trim: {task}")
                                        await self.add_to_ffmpeg_queue(task)
                                else:
                                    logger.warning(f"AUDIT: Failed to create MatchInfo object for {group_dir}. Not queueing trim task.")
                            else:
                                logger.info(f"AUDIT: Found combined group with no match_info.ini in {group_dir}. Not ready to trim.")
                        else:
                            logger.info(f"AUDIT: Found combined group with missing combined.mp4 in {group_dir}. Please check the group directory.")

                    # Check for autocam_complete groups and add them to YouTube upload queue
                    if dir_state.status == "autocam_complete":
                        # Check if YouTube upload is enabled in config
                        if self.config.has_section('YOUTUBE') and self.config.getboolean('YOUTUBE', 'enabled', fallback=False):
                            youtube_task = YouTubeUploadTask(group_dir)
                            if youtube_task not in self.queued_for_ffmpeg:
                                logger.info(f"AUDIT: Found autocam_complete group in {group_dir}. Queueing for YouTube upload.")
                                await self.add_to_ffmpeg_queue(youtube_task)
                        else:
                            logger.info(f"AUDIT: Found autocam_complete group in {group_dir}, but YouTube upload is not enabled in config.")

                except Exception as e:
                    logger.error(f"Error during state audit for {group_dir}: {e}")
        
        # Initialize TeamSnap API
        if not self._teamsnap_initialized:
            self._initialize_teamsnap()
            self._teamsnap_initialized = True
            
        # Initialize NTFY API
        if not self._ntfy_initialized:
            self._initialize_ntfy()
            self._ntfy_initialized = True

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
        """Shut down the application."""
        logger.info("Shutting down VideoGrouperApp")
        self._shutdown_event.set()
        
        # Shutdown NTFY API if initialized
        if self.ntfy_api and self.ntfy_api.enabled:
            await self.ntfy_api.shutdown()
        
        # Cancel all running tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()
                
        # Close camera connection if open
        if self.camera:
            if hasattr(self.camera, 'close') and callable(self.camera.close):
                await self.camera.close()
            
        logger.info("VideoGrouperApp shutdown complete")

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

            # Get connected timeframes for filtering
            connected_timeframes = self.camera.get_connected_timeframes()
            
            for file_info in files:
                try:
                    filename = os.path.basename(file_info['path'])
                    file_start_time = datetime.strptime(file_info['startTime'], default_date_format)
                    file_end_time = datetime.strptime(file_info['endTime'], default_date_format)

                    if latest_end_time is None or file_end_time > latest_end_time:
                        latest_end_time = file_end_time

                    # Check if the file overlaps with any connected timeframe
                    should_skip = False
                    if connected_timeframes:
                        # Convert to UTC for comparison with connected timeframes
                        file_start_utc = pytz.utc.localize(file_start_time) if file_start_time.tzinfo is None else file_start_time
                        file_end_utc = pytz.utc.localize(file_end_time) if file_end_time.tzinfo is None else file_end_time
                        
                        for frame_start, frame_end in connected_timeframes:
                            frame_end_or_now = frame_end or datetime.now(pytz.utc)
                            
                            # Check for overlap: if file starts before frame ends AND file ends after frame starts
                            if file_start_utc < frame_end_or_now and file_end_utc > frame_start:
                                logger.info(f"Skipping file {filename} as it overlaps with connected timeframe from {frame_start} to {frame_end_or_now}")
                                should_skip = True
                                break
                    
                    if should_skip:
                        continue

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
                await self.add_to_ffmpeg_queue(ConvertTask(file_path))

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
        """Process tasks in the FFmpeg queue."""
        logger.info("Starting FFmpeg queue processor")
        while True:
            try:
                task = await self.ffmpeg_queue.get()
                logger.info(f"Processing FFmpeg task: {task}")
                
                if task.task_type == "convert":
                    await self._handle_conversion_task(task.item_path)
                elif task.task_type == "combine":
                    await self._handle_combine_task(task.item_path)
                elif task.task_type == "trim":
                    if isinstance(task, TrimTask):
                        await self._handle_trim_task(task.item_path, task.match_info)
                    else:
                        await self._handle_trim_task(task.item_path)
                elif task.task_type == "youtube_upload":
                    await self._handle_youtube_upload_task(task.item_path)
                else:
                    logger.warning(f"Unknown task type: {task.task_type}")
                
                self.ffmpeg_queue.task_done()
                self.queued_for_ffmpeg.discard(task)
                await self._save_ffmpeg_queue_state()
            except Exception as e:
                logger.error(f"Error processing FFmpeg task: {e}")
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

                await dir_state.update_file_state(file_path, status="converted", screenshot_path=final_screenshot_path)

                # Create match_info.ini if it doesn't exist
                await self._ensure_match_info_exists(group_dir)

                await self.cleanup_dav_files(group_dir)

                if dir_state.is_ready_for_combining():
                    logger.info(f"Group {os.path.basename(group_dir)} is ready for combining.")
                    await self.add_to_ffmpeg_queue(CombineTask(group_dir))
            else:
                await dir_state.update_file_state(file_path, status="conversion_failed")
                logger.error(f"Conversion failed for {file_path}")
        except Exception as e:
            await dir_state.update_file_state(file_path, status="conversion_failed")
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
                
                # Check if NTFY integration is enabled to send notifications
                if self.ntfy_api and self.ntfy_api.enabled:
                    logger.info("Using NTFY to send notifications about match information")
                    await self._process_combined_directory(group_dir, combined_path)
                else:
                    # Check for match_info.ini and queue trim task if it's populated (original behavior)
                    match_info_path = os.path.join(group_dir, "match_info.ini")
                    if os.path.exists(match_info_path) and self.is_match_info_populated(group_dir):
                        match_info = MatchInfo.from_file(match_info_path)
                        if match_info:
                            task = TrimTask(group_dir, match_info)
                            await self.add_to_ffmpeg_queue(task)
            else:
                logger.error(f"Failed to combine videos in {group_dir}")
                await dir_state.update_group_status("combine_failed", error_message="ffmpeg combine command failed.")
        except Exception as e:
            logger.error(f"Error during combine for {group_dir}: {e}")
            await dir_state.update_group_status("combine_failed", error_message=str(e))
        finally:
            if os.path.exists(file_list_path):
                os.remove(file_list_path)

    async def _handle_trim_task(self, group_dir: str, match_info: Optional[MatchInfo] = None):
        """
        Handles trimming of a combined video file based on match_info.ini.
        If the match info is valid and the combined file exists, it will trim the video.
        
        Args:
            group_dir: The directory containing the combined video
            match_info: Optional MatchInfo object. If None, it will be loaded from match_info.ini
        """
        logger.info(f"TRIM: Handling trim task for {group_dir}")

        dir_state = DirectoryState(group_dir)
        
        # Load match info if not provided
        if match_info is None:
            if not self.is_match_info_populated(group_dir):
                logger.warning(f"TRIM: Match info for {group_dir} is not populated. Re-queueing.")
                await self._requeue_ffmpeg_task_later(TrimTask(group_dir), delay_seconds=60)
                return

            match_info_path = os.path.join(group_dir, "match_info.ini")
            match_info = MatchInfo.from_file(match_info_path)
            if match_info is None:
                logger.error(f"TRIM: Failed to read match_info.ini at {match_info_path}")
                await dir_state.update_group_status("trim_failed", error_message="Failed to read match_info.ini")
                return

        combined_path = os.path.join(group_dir, "combined.mp4")
        if not os.path.exists(combined_path):
            logger.error(f"TRIM: Combined video not found at {combined_path}. Cannot trim.")
            await dir_state.update_group_status("trim_failed", error_message="Combined video not found for trimming.")
            return

        try:
            my_team_name = match_info.my_team_name
            opponent_team_name = match_info.opponent_team_name
            location = match_info.location
            start_offset = match_info.start_time_offset
            total_duration_seconds = match_info.get_total_duration_seconds()
            
            # Get sanitized names for filename
            my_team_sanitized, opponent_sanitized, location_sanitized = match_info.get_sanitized_names()

            # Get date from group directory name to add to filename
            group_name = os.path.basename(group_dir)
            date_str_ymd = "nodate"
            date_str_mdy = "nodate"
            try:
                date_part = group_name.split('-')[0]
                dt_obj = datetime.strptime(date_part, '%Y.%m.%d')
                date_str_ymd = dt_obj.strftime('%Y.%m.%d')
                date_str_mdy = dt_obj.strftime('%m-%d-%Y')
            except (ValueError, IndexError) as e:
                logger.warning(f"Could not parse date from group name '{group_name}'. Using generic date string. Error: {e}")

            # Create the subdirectory for the trimmed file
            sub_dir_name = f"{date_str_ymd} - {my_team_name} vs {opponent_team_name} ({location})"
            sub_dir_path = os.path.join(group_dir, sub_dir_name)
            os.makedirs(sub_dir_path, exist_ok=True)

            # Define the output filename and path
            output_filename = f"{my_team_sanitized}-{opponent_sanitized}-{location_sanitized}-{date_str_mdy}-raw.mp4"
            output_path = os.path.join(sub_dir_path, output_filename)

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
                await dir_state.update_group_status("trim_failed", error_message="FFmpeg trim command failed")

        except Exception as e:
            logger.error(f"Error during trim task for {group_dir}: {e}")
            await dir_state.update_group_status("trim_failed", error_message=str(e))
            
        return True # Handled

    async def _ensure_match_info_exists(self, group_dir: str):
        """Creates match_info.ini in the group directory if it doesn't exist and populates it with TeamSnap data if available."""
        # Use the new MatchInfo.get_or_create method
        match_info, config = MatchInfo.get_or_create(group_dir)
        
        # Check if TeamSnap integration is enabled and if match info is not already populated
        if (self.teamsnap_api and 
            self.teamsnap_api.enabled and 
            not self.is_match_info_populated(group_dir)):
            
            try:
                # Get the directory state to find the recording timespan
                dir_state = DirectoryState(group_dir)
                
                # Get the first file's start time and last file's end time to determine the timespan
                first_file = dir_state.get_first_file()
                last_file = dir_state.get_last_file()
                
                if not first_file or not first_file.start_time or not last_file or not last_file.end_time:
                    logger.warning(f"Cannot find recording timespan for {group_dir}, skipping TeamSnap lookup")
                    return
                
                start_time = first_file.start_time
                end_time = last_file.end_time
                
                logger.info(f"Looking up TeamSnap game information for recording timespan from {start_time} to {end_time}")
                
                # Populate match info using TeamSnap API
                match_info_dict = {}
                success = self.teamsnap_api.populate_match_info(match_info_dict, start_time, end_time)
                
                if success and match_info_dict:
                    logger.info(f"Found TeamSnap game: {match_info_dict.get('home_team')} vs {match_info_dict.get('away_team')} at {match_info_dict.get('location')}")
                    
                    # Convert TeamSnap match info format to our format
                    team_info = {
                        'team_name': match_info_dict.get('home_team', ''),
                        'opponent_name': match_info_dict.get('away_team', ''),
                        'location': match_info_dict.get('location', '')
                    }
                    
                    # Update match info with team information
                    MatchInfo.update_team_info(group_dir, team_info)
                    
                    logger.info(f"Updated match_info.ini with TeamSnap data for {group_dir}")
                else:
                    logger.info(f"No TeamSnap game information found for recording from {start_time} to {end_time}")
            
            except Exception as e:
                logger.error(f"Error retrieving TeamSnap game information: {e}", exc_info=True)

    def is_match_info_populated(self, group_dir: str) -> bool:
        """Checks if the match_info.ini file exists and is populated with non-default values."""
        match_info_path = os.path.join(group_dir, "match_info.ini")
        if not os.path.exists(match_info_path):
            return False
        
        match_info = MatchInfo.from_file(match_info_path)
        if match_info is None:
            return False
        
        # Use the new is_populated method
        return match_info.is_populated()

    def parse_match_info(self, file_path: str) -> Optional[MatchInfo]:
        """Parse match info file."""
        return MatchInfo.from_file(file_path)

    async def add_to_download_queue(self, recording_file: RecordingFile):
        """Adds a file to the download queue if it's not already present."""
        if recording_file.file_path not in self.queued_for_download:
            await self.download_queue.put(recording_file)
            self.queued_for_download.add(recording_file.file_path)
            logger.info(f"Added to download queue: {os.path.basename(recording_file.file_path)}")
            await self._save_download_queue_state()
        else:
            logger.debug(f"File {recording_file.file_path} is already in the download queue.")

    async def add_to_ffmpeg_queue(self, task: FFmpegTask):
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
            data_to_save = []
            for item in items:
                data_to_save.append(item.to_dict())

            logger.info(f"Saving FFmpeg queue state with {len(data_to_save)} items: {data_to_save}")
            async with aiofiles.open(queue_path, 'w') as f:
                await f.write(json.dumps(data_to_save, indent=2))
            
            # Refill the queue with original items
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

                    # Get connected timeframes for filtering
                    connected_timeframes = self.camera.get_connected_timeframes()

                    for item_data in items:
                        try:
                            # Reconstruct RecordingFile object from dict
                            recording_file = RecordingFile.from_dict(item_data)
                            
                            # Check if the recording overlaps with any connected timeframe
                            should_skip = False
                            if connected_timeframes:
                                file_start_utc = pytz.utc.localize(recording_file.start_time) if recording_file.start_time.tzinfo is None else recording_file.start_time
                                file_end_utc = pytz.utc.localize(recording_file.end_time) if recording_file.end_time.tzinfo is None else recording_file.end_time
                                
                                for frame_start, frame_end in connected_timeframes:
                                    frame_end_or_now = frame_end or datetime.now(pytz.utc)
                                    
                                    # Check for overlap: if file starts before frame ends AND file ends after frame starts
                                    if file_start_utc < frame_end_or_now and file_end_utc > frame_start:
                                        logger.info(f"Not loading {os.path.basename(recording_file.file_path)} from queue state as it overlaps with connected timeframe from {frame_start} to {frame_end_or_now}")
                                        should_skip = True
                                        break
                            
                            if not should_skip:
                                await self.add_to_download_queue(recording_file)
                        except KeyError as e:
                            logger.warning(f"LOAD: Skipping malformed item in download_queue_state.json (missing key: {e}): {item_data}")
                    logger.info(f"LOAD: Loaded {len(items)} items from download_queue_state.json")

            except Exception as e:
                logger.error(f"Failed to load download queue state: {e}", exc_info=True)

        # Load FFmpeg Queue
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
                        if isinstance(item, dict):
                            # New dictionary format
                            task = task_from_dict(item)
                            if task:
                                logger.info(f"LOAD: Adding task to FFmpeg queue from dict: {task}")
                                await self.add_to_ffmpeg_queue(task)
                            else:
                                logger.warning(f"LOAD: Failed to create task from dict: {item}")
                        elif isinstance(item, list) and len(item) >= 2:
                            # Legacy list format
                            task_type, item_path = item[0], item[1]
                            
                            # Create the appropriate task object
                            task = create_ffmpeg_task(task_type, item_path)
                            if task:
                                logger.info(f"LOAD: Adding task to FFmpeg queue from list: {task}")
                                await self.add_to_ffmpeg_queue(task)
                            else:
                                logger.warning(f"LOAD: Failed to create task from list: {item}")
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

    async def _requeue_ffmpeg_task_later(self, task: FFmpegTask, delay_seconds: int):
        """Waits for a delay and then adds a task back to the FFmpeg queue."""
        await asyncio.sleep(delay_seconds)
        logger.info(f"Re-queueing delayed task: {task}")
        await self.ffmpeg_queue.put(task)

    async def _handle_youtube_upload_task(self, group_dir: str):
        """Handle a YouTube upload task."""
        logger.info(f"Processing YouTube upload task for {group_dir}")
        
        try:
            # Get the credentials and token file paths using the helper function
            credentials_file, token_file = get_youtube_paths(self.storage_path)
            
            # Check if credentials file exists
            if not os.path.exists(credentials_file):
                logger.error(f"YouTube credentials file not found: {credentials_file}")
                return
            
            # Get playlist configuration
            playlist_config = None
            if self.config.has_section('youtube.playlist.processed') and self.config.has_section('youtube.playlist.raw'):
                playlist_config = {
                    "processed": {
                        "name_format": self.config.get('youtube.playlist.processed', 'name_format', fallback="{my_team_name} 2013s"),
                        "description": self.config.get('youtube.playlist.processed', 'description', fallback="Processed videos"),
                        "privacy_status": self.config.get('youtube.playlist.processed', 'privacy_status', fallback="unlisted")
                    },
                    "raw": {
                        "name_format": self.config.get('youtube.playlist.raw', 'name_format', fallback="{my_team_name} 2013s - Full Field"),
                        "description": self.config.get('youtube.playlist.raw', 'description', fallback="Raw videos"),
                        "privacy_status": self.config.get('youtube.playlist.raw', 'privacy_status', fallback="unlisted")
                    }
                }
                logger.info(f"Using playlist configuration: {playlist_config}")
            else:
                logger.info("No playlist configuration found in config file, using defaults")
            
            # Upload the videos with playlist configuration
            success = upload_group_videos(group_dir, credentials_file, token_file, playlist_config)
            
            if success:
                logger.info(f"Successfully uploaded videos for {group_dir} to YouTube")
            else:
                logger.error(f"Failed to upload videos for {group_dir} to YouTube")
                # Re-queue the task for later
                await self._requeue_ffmpeg_task_later(YouTubeUploadTask(group_dir), 300)  # Try again in 5 minutes
        except Exception as e:
            logger.error(f"Error during YouTube upload for {group_dir}: {e}")
            # Re-queue the task for later
            await self._requeue_ffmpeg_task_later(YouTubeUploadTask(group_dir), 300)  # Try again in 5 minutes

    async def _filter_existing_state_files(self):
        """Filter existing state.json files to remove recordings that overlap with connected timeframes."""
        # Get connected timeframes
        connected_timeframes = self.camera.get_connected_timeframes()
        if not connected_timeframes:
            logger.info("No connected timeframes found. Skipping state file filtering.")
            return
            
        logger.info(f"Filtering existing state files for recordings that overlap with connected timeframes.")
        
        # Get all directories in the storage path
        try:
            dirs = [os.path.join(self.storage_path, d) for d in os.listdir(self.storage_path) 
                   if os.path.isdir(os.path.join(self.storage_path, d))]
        except FileNotFoundError:
            logger.warning(f"Storage directory {self.storage_path} not found.")
            return
            
        filtered_count = 0
        
        # Process each directory
        for group_dir in dirs:
            try:
                state_file_path = os.path.join(group_dir, "state.json")
                if not os.path.exists(state_file_path):
                    continue
                    
                dir_state = DirectoryState(group_dir)
                
                # Get all files in the directory state
                files_to_filter = []
                
                for file_path, recording in list(dir_state.files.items()):
                    # Check if the recording overlaps with any connected timeframe
                    if recording.start_time and recording.end_time:
                        file_start_utc = pytz.utc.localize(recording.start_time) if recording.start_time.tzinfo is None else recording.start_time
                        file_end_utc = pytz.utc.localize(recording.end_time) if recording.end_time.tzinfo is None else recording.end_time
                        
                        for frame_start, frame_end in connected_timeframes:
                            frame_end_or_now = frame_end or datetime.now(pytz.utc)
                            
                            # Check for overlap: if file starts before frame ends AND file ends after frame starts
                            if file_start_utc < frame_end_or_now and file_end_utc > frame_start:
                                logger.info(f"Filtering out {os.path.basename(file_path)} from {os.path.basename(group_dir)} as it overlaps with connected timeframe from {frame_start} to {frame_end_or_now}")
                                files_to_filter.append(file_path)
                                filtered_count += 1
                                break
                
                # Remove filtered files from the directory state
                for file_path in files_to_filter:
                    if file_path in dir_state.files:
                        dir_state.files.pop(file_path)
                
                # Save the updated directory state
                if files_to_filter:
                    await dir_state.save_state()
                    
            except Exception as e:
                logger.error(f"Error filtering directory state for {group_dir}: {e}")
                
        if filtered_count > 0:
            logger.info(f"Filtered out {filtered_count} recordings that overlap with connected timeframes.")

    def _initialize_teamsnap(self):
        """Initialize the TeamSnap API integration."""
        logger.info("Initializing TeamSnap API integration")
        config_path = os.path.join(self.storage_path, "config.ini")
        
        # Check for team configurations (TEAMSNAP.TEAM.*)
        team_configs = [section for section in self.config.sections() if section.startswith('TEAMSNAP.TEAM.')]
        
        # Initialize the list of TeamSnap API instances
        self.teamsnap_apis = []
        
        # If no team configurations are found, use the legacy configuration
        if not team_configs:
            if self.config.has_section('TEAMSNAP') and self.config.getboolean('TEAMSNAP', 'enabled', fallback=False):
                logger.info("Using legacy TeamSnap configuration")
                self.teamsnap_api = TeamSnapAPI(config_path)
                if self.teamsnap_api.enabled:
                    self.teamsnap_apis.append(self.teamsnap_api)
            else:
                logger.info("TeamSnap API integration disabled - no configuration found")
                self.teamsnap_api = None
            return
            
        # Process each enabled team configuration
        for section in team_configs:
            if self.config.getboolean(section, 'enabled', fallback=False):
                logger.info(f"Initializing TeamSnap team configuration: {section}")
                
                # Create a temporary config for this team
                temp_config = configparser.ConfigParser()
                temp_config.add_section('TEAMSNAP')
                
                # First, copy base TeamSnap settings (credentials, etc.)
                if self.config.has_section('TEAMSNAP'):
                    for key, value in self.config['TEAMSNAP'].items():
                        if key not in ['team_id', 'team_name']:  # Skip team-specific settings
                            temp_config['TEAMSNAP'][key] = value
                
                # Then, copy team-specific settings
                temp_config['TEAMSNAP']['enabled'] = 'true'
                for key, value in self.config[section].items():
                    if key in ['team_id', 'team_name']:  # Only copy team-specific settings
                        temp_config['TEAMSNAP'][key] = value
                
                # Save the temporary config to a file
                temp_config_path = os.path.join(self.storage_path, f"temp_teamsnap_config_{section}.ini")
                with open(temp_config_path, 'w') as f:
                    temp_config.write(f)
                
                # Initialize TeamSnap API with the temporary config
                teamsnap_api = TeamSnapAPI(temp_config_path)
                
                # Clean up the temporary config file
                try:
                    os.remove(temp_config_path)
                except:
                    pass
                    
                if teamsnap_api.enabled:
                    logger.info(f"TeamSnap API integration enabled for {section}")
                    self.teamsnap_apis.append(teamsnap_api)
                else:
                    logger.info(f"TeamSnap API integration disabled for {section}")
        
        # Set the primary TeamSnap API instance (for backward compatibility)
        if self.teamsnap_apis:
            self.teamsnap_api = self.teamsnap_apis[0]
        else:
            logger.info("No enabled TeamSnap team configurations found")
            self.teamsnap_api = None

    def _initialize_playmetrics(self):
        """Initialize PlayMetrics integration."""
        logger.info("Initializing PlayMetrics integration")

        # Check for team configurations (PLAYMETRICS.TEAM.*)
        team_configs = [section for section in self.config.sections() if section.startswith('PLAYMETRICS.')]
        
        # Initialize list to store PlayMetrics API instances
        self.playmetrics_apis = []
        
        # First check if we have any team-specific configurations
        if team_configs:
            for section in team_configs:
                # Skip if this is the base PLAYMETRICS section
                if section == 'PLAYMETRICS':
                    continue
                    
                # Check if this team is enabled
                if not self.config.getboolean(section, 'enabled', fallback=False):
                    logger.info(f"PlayMetrics team configuration {section} is disabled, skipping")
                    continue
                
                logger.info(f"Initializing PlayMetrics team: {section}")
                
                # Create a temporary config for this team
                temp_config = configparser.ConfigParser()
                temp_config.add_section('PLAYMETRICS')
                
                # First, copy base PlayMetrics settings (credentials, etc.)
                if self.config.has_section('PLAYMETRICS'):
                    for key, value in self.config['PLAYMETRICS'].items():
                        if key not in ['team_id', 'team_name']:  # Skip team-specific settings
                            temp_config['PLAYMETRICS'][key] = value
                
                # Then, override with team-specific settings
                temp_config['PLAYMETRICS']['enabled'] = 'true'
                for key, value in self.config[section].items():
                    if key in ['team_id', 'team_name', 'username', 'password']:  # Only copy team-specific settings
                        temp_config['PLAYMETRICS'][key] = value
                
                # Save the temporary config to a file
                temp_config_path = os.path.join(self.storage_path, f"temp_playmetrics_{section}_config.ini")
                os.makedirs(os.path.dirname(temp_config_path), exist_ok=True)
                with open(temp_config_path, 'w') as f:
                    temp_config.write(f)
                
                # Initialize PlayMetrics API with the temporary config
                playmetrics_api = PlayMetricsAPI(temp_config_path)
                
                # Clean up the temporary config file
                try:
                    os.remove(temp_config_path)
                except Exception as e:
                    logger.warning(f"Could not remove temporary config file: {e}")
                
                # Check if enabled
                if not playmetrics_api or not playmetrics_api.enabled:
                    logger.error(f"PlayMetrics team {section} is not properly configured")
                    continue
                    
                # Login to PlayMetrics
                if not playmetrics_api.login():
                    logger.error(f"Failed to login to PlayMetrics for team {section}")
                    continue
                    
                logger.info(f"PlayMetrics team {section} initialized successfully")
                self.playmetrics_apis.append(playmetrics_api)
        
        # If no team configs found or no teams were successfully initialized, try legacy approach
        if not self.playmetrics_apis and self.config.has_section('PLAYMETRICS') and self.config.getboolean('PLAYMETRICS', 'enabled', fallback=False):
            logger.info("No team-specific PlayMetrics configurations found, using legacy configuration")
            
            # Create a temporary config for PlayMetrics
            temp_config = configparser.ConfigParser()
            temp_config.add_section('PLAYMETRICS')
            
            # Copy all PlayMetrics settings
            for key, value in self.config['PLAYMETRICS'].items():
                temp_config['PLAYMETRICS'][key] = value
            
            # Save the temporary config to a file
            temp_config_path = os.path.join(self.storage_path, "temp_playmetrics_config.ini")
            os.makedirs(os.path.dirname(temp_config_path), exist_ok=True)
            with open(temp_config_path, 'w') as f:
                temp_config.write(f)
            
            # Initialize PlayMetrics API with the temporary config
            self.playmetrics_api = PlayMetricsAPI(temp_config_path)
            
            # Clean up the temporary config file
            try:
                os.remove(temp_config_path)
            except Exception as e:
                logger.warning(f"Could not remove temporary config file: {e}")
            
            # Check if enabled
            if not self.playmetrics_api or not self.playmetrics_api.enabled:
                logger.error("PlayMetrics integration is not properly configured")
                self.playmetrics_api = None
                return
                
            # Login to PlayMetrics
            if not self.playmetrics_api.login():
                logger.error("Failed to login to PlayMetrics")
                self.playmetrics_api = None
                return
                
            # Add to the list for consistent access
            self.playmetrics_apis.append(self.playmetrics_api)
            
            logger.info("PlayMetrics integration initialized successfully (legacy mode)")
        
        # Set initialization flag
        self._playmetrics_initialized = len(self.playmetrics_apis) > 0
        
        if self._playmetrics_initialized:
            logger.info(f"PlayMetrics integration initialized with {len(self.playmetrics_apis)} teams")
        else:
            logger.info("PlayMetrics integration not initialized - no valid configurations found")

    async def _process_combined_directory(self, group_dir, combined_path, force=False):
        """Process a directory with a combined video file."""
        logger.info(f"Processing combined directory: {group_dir}")
        
        # Check if match info is already populated
        if self.is_match_info_populated(group_dir) and not force:
            logger.info(f"Match info already populated for {group_dir}, skipping processing")
            return True
            
        # Get the first file in the group to determine the recording date
        dir_state = DirectoryState(group_dir)
        files = dir_state.get_files()
        
        if not files:
            logger.warning(f"No files found in {group_dir}, skipping processing")
            return False
            
        # Get the recording date from the first file
        first_file = files[0]
        recording_date = first_file.date_time
        recording_start = first_file.start_time
        last_file = files[-1]
        recording_end = last_file.end_time or recording_date
        
        # Collect games from both TeamSnap and PlayMetrics
        games = []
        
        # Try to get game info from TeamSnap
        if not self._teamsnap_initialized:
            self._initialize_teamsnap()
            self._teamsnap_initialized = True
        
        # Check all enabled TeamSnap teams
        if hasattr(self, 'teamsnap_apis') and self.teamsnap_apis:
            for teamsnap_api in self.teamsnap_apis:
                if teamsnap_api and teamsnap_api.enabled:
                    teamsnap_game = teamsnap_api.find_game_for_recording(recording_start, recording_end)
                    if teamsnap_game:
                        # Add source information and team name
                        teamsnap_game['source'] = 'TeamSnap'
                        teamsnap_game['team_name'] = teamsnap_api.my_team_name
                        games.append(teamsnap_game)
                        logger.info(f"Found TeamSnap game for team {teamsnap_api.my_team_name}: {teamsnap_game.get('team_name', 'Unknown')} vs {teamsnap_game.get('opponent_name', 'Unknown')}")
        # Fallback to legacy TeamSnap API instance
        elif self.teamsnap_api and self.teamsnap_api.enabled:
            teamsnap_game = self.teamsnap_api.find_game_for_recording(recording_start, recording_end)
            if teamsnap_game:
                # Add source information
                teamsnap_game['source'] = 'TeamSnap'
                games.append(teamsnap_game)
                logger.info(f"Found TeamSnap game: {teamsnap_game.get('team_name', 'Unknown')} vs {teamsnap_game.get('opponent_name', 'Unknown')}")
        
        # Try to get game info from PlayMetrics
        if not self._playmetrics_initialized:
            self._initialize_playmetrics()
                
        # Check all enabled PlayMetrics teams
        if hasattr(self, 'playmetrics_apis') and self.playmetrics_apis:
            for playmetrics_api in self.playmetrics_apis:
                if playmetrics_api and playmetrics_api.enabled:
                    playmetrics_game = playmetrics_api.find_game_for_recording(recording_start, recording_end)
                    if playmetrics_game:
                        # Add source information and team name
                        playmetrics_game['source'] = 'PlayMetrics'
                        playmetrics_game['team_name'] = playmetrics_api.team_name
                        games.append(playmetrics_game)
                        logger.info(f"Found PlayMetrics game for team {playmetrics_api.team_name}: {playmetrics_game.get('title', 'Unknown')} vs {playmetrics_game.get('opponent', 'Unknown')}")
        # Fallback to legacy PlayMetrics API instance
        elif self.playmetrics_api and self.playmetrics_api.enabled:
            playmetrics_game = self.playmetrics_api.find_game_for_recording(recording_start, recording_end)
            if playmetrics_game:
                # Add source information
                playmetrics_game['source'] = 'PlayMetrics'
                games.append(playmetrics_game)
                logger.info(f"Found PlayMetrics game: {playmetrics_game.get('title', 'Unknown')} vs {playmetrics_game.get('opponent', 'Unknown')}")
        
        # Check if we found any games
        if not games:
            logger.warning(f"No games found for recording date {recording_date}")
            
            # If no games found and NTFY is enabled, try NTFY as a fallback
            if self.ntfy_api and self.ntfy_api.enabled:
                await self._process_combined_with_ntfy(group_dir, combined_path, force)
            
            return False
            
        # If we found multiple games, select the most likely match
        # For now, we'll just use the first one found
        # TODO: Implement a better selection algorithm if needed
        game = games[0]
        source = game.get('source', 'Unknown')
        
        if source == 'TeamSnap':
            # Create team info dictionary for TeamSnap
            match_info = {
                'home_team': game.get('team_name', self.teamsnap_api.my_team_name if self.teamsnap_api else 'Home Team'),
                'away_team': game.get('opponent_name', ''),
                'location': game.get('location_name', ''),
                'date': '',
                'time': ''
            }
            
            # Parse the game date
            game_start_str = game.get('start_date')
            if game_start_str:
                try:
                    game_start = datetime.fromisoformat(game_start_str.replace('Z', '+00:00'))
                    match_info['date'] = game_start.strftime('%Y-%m-%d')
                    match_info['time'] = game_start.strftime('%H:%M')
                except (ValueError, TypeError) as e:
                    logger.error(f"Error parsing game date: {e}")
        elif source == 'PlayMetrics':
            # Create team info dictionary for PlayMetrics
            match_info = {
                'home_team': self.playmetrics_api.team_name if self.playmetrics_api else 'Home Team',
                'away_team': game.get('opponent', ''),
                'location': game.get('location', ''),
                'date': '',
                'time': ''
            }
            
            # Parse the game date
            game_start = game.get('start_time')
            if game_start:
                try:
                    match_info['date'] = game_start.strftime('%Y-%m-%d')
                    match_info['time'] = game_start.strftime('%H:%M')
                except (ValueError, TypeError) as e:
                    logger.error(f"Error parsing game date: {e}")
        
        # Save match info to file
        match_info_path = os.path.join(group_dir, 'match_info.ini')
        match_info_config = configparser.ConfigParser()
        match_info_config['MATCH'] = match_info
        
        with open(match_info_path, 'w') as f:
            match_info_config.write(f)
            
        logger.info(f"Saved match info to {match_info_path}: {match_info}")
        
        # Process with the match info
        await self._handle_trim_task(group_dir, MatchInfo(**match_info))
        
        return True

    def _initialize_ntfy(self):
        """Initialize the NTFY API integration."""
        logger.info("Initializing NTFY API integration")
        self.ntfy_api = NtfyAPI(self.config)
        logger.info(f"NTFY API enabled: {self.ntfy_api.enabled}, topic: {self.ntfy_api.topic}")
        if self.ntfy_api and self.ntfy_api.enabled:
            logger.info("NTFY API integration enabled")
            asyncio.create_task(self.ntfy_api.initialize())
        else:
            logger.info("NTFY API integration disabled")

    async def _process_combined_with_ntfy(self, group_dir: str, combined_path: str, force=False):
        """Process a combined video with NTFY to determine game start and end times."""
        logger.info(f"Processing combined video with NTFY: {group_dir}")
        
        # Skip if we've already processed this directory
        if group_dir in self._ntfy_processed_dirs and not force:
            logger.info(f"Directory {group_dir} already processed with NTFY, skipping")
            return
            
        # Check if match info is already populated
        if self.is_match_info_populated(group_dir) and not force:
            logger.info(f"Match info already populated for {group_dir}, skipping NTFY processing")
            self._ntfy_processed_dirs.add(group_dir)
            return
            
        # Add to processed set to avoid duplicate processing
        self._ntfy_processed_dirs.add(group_dir)
        
        if not self.ntfy_api or not self.ntfy_api.enabled:
            logger.warning("NTFY API not enabled, skipping game time detection")
            return
            
        # Get existing match info
        match_info, config = MatchInfo.get_or_create(group_dir)
        
        # Extract existing team info
        existing_info = {}
        if match_info:
            existing_info = match_info.get_team_info()
            
        # Send notifications about missing team information
        await self.ntfy_api.ask_team_info(combined_path, existing_info)
        
        # Send notification about setting game start time
        await self.ntfy_api.ask_game_start_time(combined_path, group_dir)
        
        # If we have a start time in the config, send notification about setting game end time
        if match_info and match_info.start_time_offset:
            await self.ntfy_api.ask_game_end_time(combined_path, group_dir, match_info.start_time_offset)
            
        # Log that notifications have been sent
        logger.info(f"Sent notifications about match information for {group_dir}")
        
        # Note: We don't queue a trim task here since the user needs to manually update match_info.ini

    def _time_to_seconds(self, time_str: str) -> int:
        """Convert a time string in format HH:MM:SS to seconds."""
        h, m, s = map(int, time_str.split(':'))
        return h * 3600 + m * 60 + s

    async def process_combined_directory_with_ntfy(self, group_dir_name: str, force=False):
        """
        Manually process a specific combined directory with NTFY.
        
        Args:
            group_dir_name: The name of the directory to process (e.g., "2025.06.14-10.37.25")
            force: If True, process with NTFY even if match_info is already populated
        """
        logger.info(f"Manually triggering NTFY processing for {os.path.join(self.storage_path, group_dir_name)}")
        group_dir = os.path.join(self.storage_path, group_dir_name)
        if not os.path.exists(group_dir):
            logger.error(f"Directory {group_dir} does not exist")
            return False
            
        combined_path = os.path.join(group_dir, "combined.mp4")
        if not os.path.exists(combined_path):
            logger.error(f"Combined video not found at {combined_path}")
            return False
            
        dir_state = DirectoryState(group_dir)
        if dir_state.status != "combined":
            logger.error(f"Directory {group_dir} is not in 'combined' state")
            return False
            
        # Remove from processed set if forcing
        if force and group_dir in self._ntfy_processed_dirs:
            self._ntfy_processed_dirs.remove(group_dir)
            
        # Process with NTFY
        await self._process_combined_with_ntfy(group_dir, combined_path, force)
        return True