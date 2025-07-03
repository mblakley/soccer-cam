import os
import logging
from datetime import datetime, timedelta
from typing import Any, Optional, List
import pytz
from .polling_processor_base import PollingProcessor
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
                    time_difference = (
                        file_start_time - last_file.end_time
                    ).total_seconds()
                    if 0 <= time_difference <= 15:
                        logger.info(
                            f"Found matching group directory {os.path.basename(group_dir_path)} for file starting at {file_start_time}"
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
        self, storage_path: str, config: Any, camera: Any, poll_interval: int = 60
    ):
        super().__init__(storage_path, config, poll_interval)
        self.camera = camera
        self.download_processor = None
        self._last_processed_time = None

    def set_download_processor(self, download_processor):
        """Set reference to download processor to queue work."""
        self.download_processor = download_processor

    async def discover_work(self) -> None:
        """
        Poll camera for new files and group them into directories.
        """
        try:
            # Check if camera is available
            is_available = await self.camera.check_availability()
            if not is_available:
                logger.debug("CAMERA_POLLER: Camera not available, skipping file sync")
                return

            await self._sync_files_from_camera()

        except Exception as e:
            logger.error(f"CAMERA_POLLER: Error during camera polling: {e}")

    async def _sync_files_from_camera(self) -> None:
        """Sync files from camera and group them."""
        start_time = await self._get_latest_processed_time()
        if start_time:
            start_time -= timedelta(minutes=1)

        end_time = datetime.now()

        logger.info(
            f"CAMERA_POLLER: Looking for new files from: {start_time} to {end_time}"
        )

        files = await self.camera.get_file_list(
            start_time=start_time, end_time=end_time
        )

        if not files:
            logger.debug(
                "CAMERA_POLLER: No new files found on the camera since last sync."
            )
            return

        logger.info(f"CAMERA_POLLER: Found {len(files)} new files to process.")
        existing_dirs = [
            os.path.join(self.storage_path, d)
            for d in os.listdir(self.storage_path)
            if os.path.isdir(os.path.join(self.storage_path, d))
        ]

        latest_end_time = None

        # Get connected timeframes for filtering
        connected_timeframes = self.camera.get_connected_timeframes()

        for file_info in files:
            try:
                filename = os.path.basename(file_info["path"])
                file_start_time = datetime.strptime(
                    file_info["startTime"], default_date_format
                )
                file_end_time = datetime.strptime(
                    file_info["endTime"], default_date_format
                )

                if latest_end_time is None or file_end_time > latest_end_time:
                    latest_end_time = file_end_time

                # Check if the file overlaps with any connected timeframe
                should_skip = False
                if connected_timeframes:
                    # Convert to UTC for comparison with connected timeframes
                    file_start_utc = (
                        pytz.utc.localize(file_start_time)
                        if file_start_time.tzinfo is None
                        else file_start_time
                    )
                    file_end_utc = (
                        pytz.utc.localize(file_end_time)
                        if file_end_time.tzinfo is None
                        else file_end_time
                    )

                    for frame_start, frame_end in connected_timeframes:
                        frame_end_or_now = frame_end or datetime.now(pytz.utc)

                        # Check for overlap: if file starts before frame ends AND file ends after frame starts
                        if (
                            file_start_utc < frame_end_or_now
                            and file_end_utc > frame_start
                        ):
                            logger.info(
                                f"CAMERA_POLLER: Skipping file {filename} as it overlaps with connected timeframe from {frame_start} to {frame_end_or_now}"
                            )
                            should_skip = True
                            break

                if should_skip:
                    continue

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
