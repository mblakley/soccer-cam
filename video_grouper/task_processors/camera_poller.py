import json
import os
import logging
from datetime import datetime, timedelta
from typing import Optional, List
import pytz

from video_grouper.cameras.base import Camera
from video_grouper.task_processors.download_processor import DownloadProcessor
from video_grouper.utils.config import Config
from video_grouper.utils.paths import get_camera_state_path, get_home_cleanup_state_path
from .base_polling_processor import PollingProcessor
from video_grouper.models import DirectoryState
from video_grouper.models import RecordingFile

logger = logging.getLogger(__name__)

# Constants
default_date_format = "%Y-%m-%d %H:%M:%S"

# Max gap between the end of one recording and the start of the next for
# them to be treated as the same continuous group. The camera segments a
# continuous recording into back-to-back files with sub-second gaps, so
# anything beyond this is a separate recording session.
GROUP_GAP_SECONDS = 5
# A recording shorter than this is only meaningful as a runt when it also
# has no neighbors — see _identify_runt_recordings.
MIN_SEGMENT_SECONDS = 5


def create_directory(path):
    """Create a directory if it doesn't exist."""
    os.makedirs(path, exist_ok=True)


def find_existing_group_for(
    file_start_time: datetime, existing_dirs: List[str]
) -> Optional[str]:
    """Return the existing group directory a file belongs to, or None.

    A file joins a group if its start time is within GROUP_GAP_SECONDS
    after the group's last file end time, or falls within the group's
    existing [first.start, last.end] range (re-discovery on later polls).
    Pure lookup — never creates a directory.
    """
    for group_dir_path in sorted(existing_dirs, reverse=True):
        state_file_path = os.path.join(group_dir_path, "state.json")
        if not os.path.exists(state_file_path):
            continue
        try:
            dir_state = DirectoryState(group_dir_path)
            first_file = dir_state.get_first_file()
            last_file = dir_state.get_last_file()
            if last_file and last_file.end_time:
                # File appends to the end of the group (within the gap).
                time_after_end = (file_start_time - last_file.end_time).total_seconds()
                if 0 <= time_after_end <= GROUP_GAP_SECONDS:
                    return group_dir_path
                # File falls within the group's existing time range.
                if (
                    first_file
                    and first_file.start_time
                    and first_file.start_time <= file_start_time <= last_file.end_time
                ):
                    return group_dir_path
        except Exception as e:
            logger.error(f"Error reading state for {group_dir_path}: {e}")
    return None


def find_group_directory(
    file_start_time: datetime, storage_path: str, existing_dirs: List[str]
) -> str:
    """
    Finds or creates a group directory for a video file based on its start time.
    A new group is created if the file's start time is more than 5 seconds after the previous file's end time.
    """
    existing = find_existing_group_for(file_start_time, existing_dirs)
    if existing is not None:
        logger.info(
            f"Found matching group directory {os.path.basename(existing)} "
            f"for file starting at {file_start_time}"
        )
        return existing

    # No matching directory found, create a new one
    new_dir_name = file_start_time.strftime("%Y.%m.%d-%H.%M.%S")
    new_dir_path = os.path.join(storage_path, new_dir_name)
    create_directory(new_dir_path)
    logger.info(
        f"Created new group directory {new_dir_path} for file starting at {file_start_time}"
    )
    return new_dir_path


def _parse_recording_times(file_info: dict):
    """Parse a file's start/end strings into datetimes.

    Returns (start, end). ``end`` is None when the camera left it blank or
    malformed (e.g. a ``..._000000_...`` aborted recording whose end time
    is all zeros), which strptime can't parse. ``start`` is None only if
    even the start time is unusable, in which case the caller leaves the
    file for the normal processing loop to handle.
    """
    try:
        start = datetime.strptime(file_info["startTime"], default_date_format)
    except (ValueError, KeyError, TypeError):
        return None, None
    try:
        end = datetime.strptime(file_info["endTime"], default_date_format)
    except (ValueError, KeyError, TypeError):
        end = None
    return start, end


def _identify_runt_recordings(files: List[dict], existing_dirs: List[str]) -> set:
    """Return the set of file paths that are *isolated* runt recordings.

    A recording is a runt to skip only when it is BOTH:
      - short/aborted: end time missing/unparseable, end <= start, or
        duration < MIN_SEGMENT_SECONDS; and
      - isolated: no other recording in this batch is contiguous with it
        (within GROUP_GAP_SECONDS, mirroring the grouping rule), and it is
        not adjacent to an already-persisted group.

    This drops a lone startup stub (e.g. the camera powered on at home,
    recorded a few seconds, then idled) while preserving a short power-off
    tail that belongs to a real game — that tail sits within the gap of
    the game's other segments, so it is not isolated.
    """
    parsed = []  # (path, start, effective_end)
    for fi in files:
        start, end = _parse_recording_times(fi)
        if start is None:
            continue  # unusable start — leave it for the main loop
        # Use start as the effective end when the end time is invalid, so a
        # zero/aborted end doesn't blow up the proximity math.
        eff_end = end if (end is not None and end > start) else start
        aborted = end is None or end <= start
        short = (
            (end - start).total_seconds() < MIN_SEGMENT_SECONDS if not aborted else True
        )
        parsed.append((fi.get("path", ""), start, eff_end, aborted or short))

    parsed.sort(key=lambda t: t[1])
    runts: set = set()

    for i, (path, start, eff_end, is_short_or_aborted) in enumerate(parsed):
        if not is_short_or_aborted:
            continue

        adjacent = False
        for j, (_, ostart, o_eff_end, _) in enumerate(parsed):
            if j == i:
                continue
            # Contiguous if a neighbor ends within the gap before this one
            # starts, starts within the gap after this one ends, or overlaps.
            if (
                0 <= (start - o_eff_end).total_seconds() <= GROUP_GAP_SECONDS
                or 0 <= (ostart - eff_end).total_seconds() <= GROUP_GAP_SECONDS
                or (ostart <= eff_end and o_eff_end >= start)
            ):
                adjacent = True
                break

        if not adjacent and find_existing_group_for(start, existing_dirs) is not None:
            adjacent = True

        if not adjacent:
            dur = (eff_end - start).total_seconds()
            logger.info(
                f"CAMERA_POLLER: Skipping isolated runt recording "
                f"{os.path.basename(path)}: dur={dur:.0f}s, no adjacent recordings"
            )
            runts.add(path)

    return runts


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
        self._last_poll_found_files = True
        self._unplug_notified = False
        self.ntfy_service = None
        self.ttt_reporter = None
        self._cleanup_state_path = get_home_cleanup_state_path(storage_path)

    async def discover_work(self) -> None:
        """
        Poll camera for new files and group them into directories.
        """
        try:
            # Check if this camera is enabled on this machine (TTT multi-computer)
            if self.ttt_reporter and not self.ttt_reporter.is_camera_enabled(
                self.camera.name
            ):
                logger.debug(
                    "CAMERA_POLLER: Camera %s disabled on this machine, skipping",
                    self.camera.name,
                )
                return

            # Check if camera is available
            is_available = await self.camera.check_availability()
            if not is_available:
                self._unplug_notified = False
                self._clear_cleanup_state()
                self._last_poll_found_files = True
                logger.debug("CAMERA_POLLER: Camera not available, skipping file sync")
                return

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

        # Drop isolated runt recordings (e.g. a few-second startup stub the
        # camera writes before idling at home) before they reach the queue.
        # A short tail that belongs to a real game is contiguous with its
        # other segments, so it is not flagged here.
        runt_paths = _identify_runt_recordings(files, existing_dirs)

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

        files_to_delete = []
        # Track newly discovered files per group for TTT registration
        new_files_by_group: dict[str, list] = {}

        for file_info in files:
            try:
                filename = os.path.basename(file_info["path"])

                # Isolated runt (handled before parsing end time, which may
                # be unparseable for an aborted recording).
                if file_info["path"] in runt_paths:
                    continue

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
                    files_to_delete.append(file_info["path"])
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

                # Store camera identity in metadata for downstream use
                file_info["camera_name"] = self.camera.name
                file_info["camera_type"] = self.camera.config.type

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

                # Track for TTT registration (best-effort, done after main loop)
                if not recording_file.skip:
                    if group_dir not in new_files_by_group:
                        new_files_by_group[group_dir] = []
                    new_files_by_group[group_dir].append(recording_file)

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

        # Register newly discovered files with TTT (best-effort, per group)
        if self.ttt_reporter and new_files_by_group:
            for group_dir, group_files in new_files_by_group.items():
                try:
                    registered = await self.ttt_reporter.register_recordings(
                        group_files
                    )
                    if registered:
                        # Use the first registration ID for the group
                        ttt_id = registered[0].get("id") if registered else None
                        if ttt_id:
                            dir_state = DirectoryState(group_dir)
                            await dir_state.set_ttt_recording_id(ttt_id)
                except Exception as e:
                    logger.warning(
                        f"CAMERA_POLLER: TTT registration failed for {os.path.basename(group_dir)}: {e}"
                    )

        if latest_end_time:
            await self._update_latest_processed_time(latest_end_time)
            logger.info(
                f"CAMERA_POLLER: File sync complete. New high-water mark set to: {latest_end_time}"
            )

    async def _get_latest_processed_time(self) -> Optional[datetime]:
        """Get the timestamp of the last processed video file for this camera."""
        state_path = get_camera_state_path(self.storage_path)
        if not os.path.exists(state_path):
            return None
        try:
            with open(state_path, "r") as f:
                all_state = json.load(f)
            cam_state = all_state.get(self.camera.name, {})
            timestamp_str = cam_state.get("latest_video_time")
            if not timestamp_str:
                return None
            return datetime.strptime(timestamp_str.strip(), default_date_format)
        except Exception as e:
            logger.error(f"CAMERA_POLLER: Could not read latest video timestamp: {e}")
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
        logger.info("CAMERA_POLLER: All downloads complete.")

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

    # ── Home recording cleanup state file ─────────────────────────────

    def _read_cleanup_state(self) -> dict:
        """Read the home cleanup state file."""
        from pathlib import Path

        try:
            path = Path(self._cleanup_state_path)
            if path.exists():
                return json.loads(path.read_text())
        except Exception as e:
            logger.debug(f"CAMERA_POLLER: Error reading cleanup state: {e}")
        return {}

    def _write_cleanup_state(
        self,
        file_paths: List[str],
        file_infos: list,
        deletion_supported: bool = True,
    ) -> None:
        """Write home files pending cleanup to the state file."""
        # Build a lookup of file info by path for display metadata
        info_by_path = {}
        for fi in file_infos:
            info_by_path[fi["path"]] = fi

        files = []
        for path in file_paths:
            entry = {"path": path}
            info = info_by_path.get(path, {})
            if "startTime" in info:
                entry["startTime"] = info["startTime"]
            if "endTime" in info:
                entry["endTime"] = info["endTime"]
            if "size" in info:
                entry["size"] = info["size"]
            files.append(entry)

        state = {
            "files": files,
            "approved": False,
            "deletion_supported": deletion_supported,
            "updated_at": datetime.now().isoformat(),
        }
        try:
            with open(self._cleanup_state_path, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"CAMERA_POLLER: Error writing cleanup state: {e}")

    def _clear_cleanup_state(self) -> None:
        """Remove the cleanup state file."""
        from pathlib import Path

        try:
            path = Path(self._cleanup_state_path)
            if path.exists():
                path.unlink()
        except Exception as e:
            logger.debug(f"CAMERA_POLLER: Error clearing cleanup state: {e}")

    # ── Deletion notification (NTFY) ───────────────────────────────────

    async def _send_deletion_notification(self, file_count: int) -> None:
        """Send an NTFY notification about home recordings found."""
        if not self.ntfy_service:
            return

        try:
            topic = self.ntfy_service.config.topic
            server_url = self.ntfy_service.config.server_url
            publish_url = f"{server_url}/{topic}"

            actions = [
                {
                    "action": "http",
                    "label": "Yes, delete",
                    "url": publish_url,
                    "method": "POST",
                    "headers": {"Content-Type": "text/plain"},
                    "body": "yes, delete home recordings",
                    "clear": True,
                },
                {
                    "action": "http",
                    "label": "No, keep",
                    "url": publish_url,
                    "method": "POST",
                    "headers": {"Content-Type": "text/plain"},
                    "body": "no, keep home recordings",
                    "clear": True,
                },
            ]

            success = await self.ntfy_service.send_notification(
                title="Home Recordings Found",
                message=(
                    f"Found {file_count} recording(s) made while the camera "
                    f"was connected at home. These are not game footage. "
                    f"Delete them from the camera's SD card?"
                ),
                tags=["warning"],
                priority=4,
                actions=actions,
            )

            if success:
                self.ntfy_service.register_response_handler(
                    "delete home recordings",
                    self._handle_deletion_response,
                )
                self.ntfy_service.register_response_handler(
                    "keep home recordings",
                    self._handle_deletion_response,
                )
                logger.info(
                    "CAMERA_POLLER: Sent home recording deletion confirmation request"
                )
        except Exception as e:
            logger.warning(f"CAMERA_POLLER: Error sending deletion notification: {e}")

    async def _handle_deletion_response(self, response: str) -> None:
        """Handle the user's NTFY response to the deletion request."""
        response_lower = response.lower()
        approved = "yes" in response_lower or "delete" in response_lower
        if approved:
            # Set approved in state file so next poll deletes
            state = self._read_cleanup_state()
            state["approved"] = True
            try:
                with open(self._cleanup_state_path, "w") as f:
                    json.dump(state, f, indent=2)
            except Exception as e:
                logger.error(f"CAMERA_POLLER: Error updating cleanup state: {e}")
            logger.info("CAMERA_POLLER: User approved home recording deletion")
        else:
            self._clear_cleanup_state()
            logger.info("CAMERA_POLLER: User denied home recording deletion")
        # Unregister handlers
        if self.ntfy_service:
            self.ntfy_service.unregister_response_handler("delete home recordings")
            self.ntfy_service.unregister_response_handler("keep home recordings")

    async def _update_latest_processed_time(self, timestamp: datetime):
        """Update the high-water mark for this camera in camera_state.json."""
        try:
            state_path = get_camera_state_path(self.storage_path)
            all_state = {}
            if os.path.exists(state_path):
                with open(state_path, "r") as f:
                    all_state = json.load(f)
            cam_state = all_state.setdefault(self.camera.name, {})
            cam_state["latest_video_time"] = timestamp.strftime(default_date_format)
            with open(state_path, "w") as f:
                json.dump(all_state, f, indent=4)
            logger.debug(
                f"CAMERA_POLLER: Updated latest processed time to: {timestamp}"
            )
        except Exception as e:
            logger.error(f"CAMERA_POLLER: Error updating latest processed time: {e}")
