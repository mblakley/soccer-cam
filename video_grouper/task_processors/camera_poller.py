import os
import logging
from datetime import datetime, timedelta
from typing import Optional, List
import pytz

from video_grouper.cameras.base import Camera
from video_grouper.task_processors.download_processor import DownloadProcessor
from video_grouper.utils.config import Config
from .base_polling_processor import PollingProcessor
from video_grouper.models import DirectoryState
from video_grouper.models import RecordingFile

logger = logging.getLogger(__name__)

# Constants
LATEST_VIDEO_FILE = "latest_video.txt"
default_date_format = "%Y-%m-%d %H:%M:%S"


def create_directory(path):
    """Create a directory if it doesn't exist."""
    os.makedirs(path, exist_ok=True)


def find_group_directory(
    file_start_time: datetime, storage_path: str, existing_dirs: List[str]
) -> str:
    """
    Finds or creates a group directory for a video file based on its start time.
    A new group is created if the file's start time is more than 5 seconds after the previous file's end time.
    """
    # Check existing directories to find a match
    for group_dir_path in sorted(existing_dirs, reverse=True):
        state_file_path = os.path.join(group_dir_path, "state.json")
        if os.path.exists(state_file_path):
            try:
                dir_state = DirectoryState(group_dir_path)
                first_file = dir_state.get_first_file()
                last_file = dir_state.get_last_file()
                if last_file and last_file.end_time:
                    # Check if file appends to end of group (within 5s gap)
                    time_after_end = (
                        file_start_time - last_file.end_time
                    ).total_seconds()
                    if 0 <= time_after_end <= 5:
                        logger.info(
                            f"Found matching group directory {os.path.basename(group_dir_path)} for file starting at {file_start_time}, with time matching file end time {last_file.end_time}"
                        )
                        return group_dir_path

                    # Check if file falls within the group's existing time range
                    # (handles re-discovery on subsequent polls)
                    if (
                        first_file
                        and first_file.start_time
                        and first_file.start_time
                        <= file_start_time
                        <= last_file.end_time
                    ):
                        logger.info(
                            f"Found matching group directory {os.path.basename(group_dir_path)} for file starting at {file_start_time} (within group range {first_file.start_time} - {last_file.end_time})"
                        )
                        return group_dir_path
            except Exception as e:
                logger.error(f"Error reading state for {group_dir_path}: {e}")

    # No matching directory found, create a new one
    new_dir_name = file_start_time.strftime("%Y.%m.%d-%H.%M.%S")
    new_dir_path = os.path.join(storage_path, new_dir_name)
    create_directory(new_dir_path)
    logger.info(
        f"Created new group directory {new_dir_path} for file starting at {file_start_time}"
    )
    return new_dir_path


class CameraPoller(PollingProcessor):
    """
    Task processor for camera file discovery and grouping.
    Polls the camera for new files and groups them into appropriate directories.
    """

    def __init__(
        self,
        storage_path: str,
        config: Config,
        camera: Camera,
        download_processor: DownloadProcessor,
        poll_interval: int = 60,
    ):
        super().__init__(storage_path, config, poll_interval)
        self.camera = camera
        self.download_processor = download_processor
        self._last_processed_time = None
        self._recording_stopped = False
        self._last_poll_found_files = True
        self._unplug_notified = False
        self.ntfy_service = None

    async def discover_work(self) -> None:
        """
        Poll camera for new files and group them into directories.
        """
        try:
            # Check if camera is available
            is_available = await self.camera.check_availability()
            if not is_available:
                self._recording_stopped = False
                self._unplug_notified = False
                self._last_poll_found_files = True
                logger.debug("CAMERA_POLLER: Camera not available, skipping file sync")
                return

            # Stop recording on first connection if configured
            if not self._recording_stopped and self.config.camera.auto_stop_recording:
                logger.info("CAMERA_POLLER: Camera connected, stopping recording")
                success = await self.camera.stop_recording()
                if success:
                    logger.info("CAMERA_POLLER: Successfully stopped camera recording")
                else:
                    logger.warning("CAMERA_POLLER: Failed to stop camera recording")
                self._recording_stopped = True

            await self._sync_files_from_camera()

            # Check if all downloads are complete and notify to unplug
            await self._check_downloads_complete()

        except Exception as e:
            logger.error(f"CAMERA_POLLER: Error during camera polling: {e}")

    async def _sync_files_from_camera(self) -> None:
        """Sync files from camera and group them."""
        start_time = await self._get_latest_processed_time()
        if start_time:
            start_time -= timedelta(minutes=1)

        # Clamp start_time so we never look back further than max_lookback_hours
        max_lookback = getattr(self.config.app, "max_lookback_hours", 48)
        earliest_allowed = datetime.now() - timedelta(hours=max_lookback)
        if start_time is None or start_time < earliest_allowed:
            start_time = earliest_allowed

        end_time = datetime.now()

        # Optional end date cap (e.g. "2025-07-23" to restrict to a specific game)
        recording_end = getattr(self.config.app, "recording_end_date", None)
        if recording_end:
            try:
                cap = datetime.strptime(str(recording_end), "%Y-%m-%d")
                if cap < end_time:
                    end_time = cap
            except ValueError:
                pass

        logger.info(
            f"CAMERA_POLLER: Looking for new files from: {start_time} to {end_time}"
        )

        files = await self.camera.get_file_list(
            start_time=start_time, end_time=end_time
        )

        if not files:
            self._last_poll_found_files = False
            logger.debug(
                "CAMERA_POLLER: No new files found on the camera since last sync."
            )
            return

        self._last_poll_found_files = True

        # Cap the number of files per poll to avoid overwhelming the pipeline
        max_files = getattr(self.config.app, "max_files_per_poll", 50)
        if len(files) > max_files:
            logger.warning(
                f"CAMERA_POLLER: Found {len(files)} files, truncating to {max_files}"
            )
            files = files[:max_files]

        logger.info(f"CAMERA_POLLER: Found {len(files)} new files to process.")
        existing_dirs = [
            os.path.join(self.storage_path, d)
            for d in os.listdir(self.storage_path)
            if os.path.isdir(os.path.join(self.storage_path, d))
        ]

        latest_end_time = None

        # Get connected timeframes for filtering
        connected_timeframes = self.camera.get_connected_timeframes()

        # Get timezone from config for proper time conversion
        timezone_str = (
            getattr(self.config.app, "timezone", "America/New_York")
            if hasattr(self.config, "app")
            else "America/New_York"
        )
        try:
            local_tz = pytz.timezone(timezone_str)
        except pytz.UnknownTimeZoneError:
            logger.warning(f"Unknown timezone '{timezone_str}', falling back to UTC")
            local_tz = pytz.utc

        for file_info in files:
            try:
                filename = os.path.basename(file_info["path"])
                file_start_time = datetime.strptime(
                    file_info["startTime"], default_date_format
                )
                file_end_time = datetime.strptime(
                    file_info["endTime"], default_date_format
                )

                # Check if the file overlaps with any connected timeframe
                should_skip = False
                if connected_timeframes:
                    # Convert file timestamps from local time to UTC for comparison with connected timeframes
                    # File timestamps from camera are in local time, connection events are stored in UTC
                    file_start_local = local_tz.localize(file_start_time)
                    file_end_local = local_tz.localize(file_end_time)
                    file_start_utc = file_start_local.astimezone(pytz.utc)
                    file_end_utc = file_end_local.astimezone(pytz.utc)

                    for frame_start, frame_end in connected_timeframes:
                        frame_end_or_now = frame_end or datetime.now(pytz.utc)

                        # Check for overlap: if file starts before frame ends AND file ends after frame starts
                        logger.info(
                            f"CAMERA_POLLER: Checking if file {filename} with start time {file_start_local} and end time {file_end_local} overlaps with connected timeframe from {frame_start} to {frame_end_or_now.astimezone(local_tz) if frame_end_or_now else 'ongoing'}"
                        )
                        if (
                            file_start_utc < frame_end_or_now
                            and file_end_utc > frame_start
                        ):
                            logger.info(
                                f"CAMERA_POLLER: Skipping file {filename} as it overlaps with connected timeframe from {frame_start} to {frame_end_or_now.astimezone(local_tz) if frame_end_or_now else 'ongoing'}"
                            )
                            should_skip = True
                            break

                if should_skip:
                    continue

                # Track high-water mark from non-skipped files only
                if latest_end_time is None or file_end_time > latest_end_time:
                    latest_end_time = file_end_time

                group_dir = find_group_directory(
                    file_start_time, self.storage_path, existing_dirs
                )
                if group_dir not in existing_dirs:
                    existing_dirs.append(group_dir)

                local_path = os.path.join(group_dir, filename)

                dir_state = DirectoryState(group_dir)
                if dir_state.is_file_in_state(local_path):
                    logger.debug(
                        f"CAMERA_POLLER: File {filename} is already known. Skipping."
                    )
                    continue

                recording_file = RecordingFile(
                    start_time=file_start_time,
                    end_time=file_end_time,
                    file_path=local_path,
                    metadata=file_info,
                )

                # Preserve skip status if file already existed in some state
                existing_file_obj = dir_state.get_file_by_path(local_path)
                if existing_file_obj:
                    recording_file.skip = existing_file_obj.skip

                await dir_state.add_file(local_path, recording_file)

                # Add to download queue if not skipped
                if not recording_file.skip and self.download_processor:
                    await self.download_processor.add_work(recording_file)
                else:
                    logger.info(
                        f"CAMERA_POLLER: Skipping download for {os.path.basename(local_path)} as per state file."
                    )

            except Exception as e:
                logger.error(
                    f"CAMERA_POLLER: Error processing file info {file_info}: {e}"
                )

        if latest_end_time:
            await self._update_latest_processed_time(latest_end_time)
            logger.info(
                f"CAMERA_POLLER: File sync complete. New high-water mark set to: {latest_end_time}"
            )

    async def _get_latest_processed_time(self) -> Optional[datetime]:
        """Get the timestamp of the last processed video file."""
        file_path = os.path.join(self.storage_path, LATEST_VIDEO_FILE)
        if not os.path.exists(file_path):
            return None
        try:
            with open(file_path, "r") as f:
                timestamp_str = f.read()
                return datetime.strptime(timestamp_str.strip(), default_date_format)
        except Exception as e:
            logger.error(
                f"CAMERA_POLLER: Could not read or parse latest video file timestamp: {e}"
            )
            return None

    async def _check_downloads_complete(self) -> None:
        """Check if all downloads are complete and send unplug notification."""
        if self._unplug_notified:
            return
        if not self.camera.is_connected:
            return
        if self._last_poll_found_files:
            return
        if not self.download_processor:
            return
        if self.download_processor.get_queue_size() > 0:
            return
        if self.download_processor._in_progress_item is not None:
            return

        self._unplug_notified = True
        logger.info(
            "CAMERA_POLLER: All downloads complete. Sending unplug notification."
        )

        if self.ntfy_service and self.config.ntfy.unplug_notification:
            try:
                await self.ntfy_service.send_notification(
                    title="Downloads Complete",
                    message="All files have been downloaded from the camera. You can safely unplug it now.",
                    tags=["white_check_mark"],
                    priority=4,
                )
            except Exception as e:
                logger.error(f"CAMERA_POLLER: Failed to send unplug notification: {e}")

    async def _update_latest_processed_time(self, timestamp: datetime):
        """Update the high-water mark for file processing."""
        try:
            latest_file_path = os.path.join(self.storage_path, LATEST_VIDEO_FILE)
            with open(latest_file_path, "w") as f:
                f.write(timestamp.strftime(default_date_format))
            logger.debug(
                f"CAMERA_POLLER: Updated latest processed time to: {timestamp}"
            )
        except Exception as e:
            logger.error(f"CAMERA_POLLER: Error updating latest processed time: {e}")
