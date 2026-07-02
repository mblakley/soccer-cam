from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime

from ..utils.locking import FileLock
from ..utils.paths import get_state_file_path
from .recording_file import RecordingFile

logger = logging.getLogger(__name__)


class DirectoryState:
    """Represents the state of files in a directory with state tracking.

    The `storage_path` argument was introduced recently to support relative path
    resolution utilities. Unfortunately, many existing call-sites across the
    code-base (including unit-tests) still instantiate `DirectoryState` with a
    single argument.  In order to maintain backward compatibility while the
    migration is completed we make `storage_path` optional – when it is not
    provided we derive it from the parent folder of `directory_path`.
    """

    def __init__(self, directory_path: str, storage_path: str | None = None):
        # Derive storage_path from directory_path if it was omitted.
        if storage_path is None:
            storage_path = os.path.dirname(os.path.abspath(directory_path))

        self.directory_path = directory_path
        self.storage_path = storage_path
        self.state_file_path = get_state_file_path(directory_path, storage_path)
        self.files: dict[str, RecordingFile] = {}
        self._lock = asyncio.Lock()
        self.status: str = "pending"
        self.error_message: str | None = None
        self.ttt_recording_id: str | None = None

        # Validate directory name format before proceeding
        dir_name = os.path.basename(directory_path)
        try:
            datetime.strptime(dir_name, "%Y.%m.%d-%H.%M.%S")
        except ValueError:
            # Not a video group directory, return early
            return

        self._load_state()

    def _load_state(self) -> dict:
        """Load the state from the JSON file.

        Returns the parsed JSON dict (empty dict if no state file exists).
        Callers like ClipDiscoveryProcessor read the raw dict to drive
        decisions on .status / .files without mutating self.
        """
        state_data: dict = {}
        try:
            with FileLock(self.state_file_path):
                if os.path.exists(self.state_file_path):
                    logger.debug(f"Loading directory state from {self.state_file_path}")
                    with open(self.state_file_path) as f:
                        state_data = json.load(f)
                        self.status = state_data.get("status", "pending")
                        self.error_message = state_data.get("error_message")
                        self.ttt_recording_id = state_data.get("ttt_recording_id")

                        loaded_files = state_data.get("files", {})
                        for file_path, file_data in loaded_files.items():
                            # Ensure backward compatibility with older state files
                            file_data.setdefault("total_size", 0)

                            self.files[file_path] = RecordingFile.from_dict(file_data)
                            self.files[
                                file_path
                            ].file_path = file_path  # Ensure file_path is set

                    logger.debug(f"Loaded {len(self.files)} files from directory state")
                else:
                    logger.debug(
                        f"No existing state file found at {self.state_file_path}"
                    )
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Error loading directory state: {e}")
            self.files = {}
            self.status = "pending"
        except TimeoutError as e:
            logger.error(f"Timeout loading state for {self.directory_path}: {e}")
        except Exception as e:
            logger.error(f"Could not load state for {self.directory_path}: {e}")
        return state_data

    def _save_state_nolock(self):
        """Saves the current state to the JSON file without acquiring the lock.

        Reads existing state.json first so out-of-band fields written by
        sibling helpers (set_youtube_playlist_name, set_autocam_run, ...)
        survive a status update. Without this read-merge, calling
        update_group_status after set_autocam_run would wipe the
        autocam_run marker — which would defeat the resume-after-crash
        path during normal operation.
        """
        files_dict = {fp: fs.to_dict() for fp, fs in self.files.items()}
        state_data: dict = {}
        if os.path.exists(self.state_file_path):
            try:
                with open(self.state_file_path) as f:
                    state_data = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                state_data = {}
        state_data["status"] = self.status
        state_data["error_message"] = self.error_message
        state_data["files"] = files_dict
        if self.ttt_recording_id is not None:
            state_data["ttt_recording_id"] = self.ttt_recording_id
        try:
            with FileLock(self.state_file_path):
                # Atomic write: temp file then rename to prevent corruption on crash
                temp_path = self.state_file_path + ".tmp"
                with open(temp_path, "w") as f:
                    json.dump(state_data, f, indent=4)
                os.replace(temp_path, self.state_file_path)
        except TimeoutError as e:
            logger.error(f"Timeout saving state for {self.directory_path}: {e}")
        except Exception as e:
            logger.error(f"Could not save state for {self.directory_path}: {e}")
        logger.debug(
            f"Saved directory state with {len(self.files)} files to {self.state_file_path}"
        )

    async def save_state(self):
        """Asynchronously saves the current state to the JSON file."""
        async with self._lock:
            self._save_state_nolock()

    async def add_file(self, file_path, file_obj: RecordingFile):
        """Adds or updates a file in the directory state."""
        async with self._lock:
            if file_path not in self.files:
                if isinstance(file_obj, RecordingFile):
                    file_obj.group_dir = self.directory_path

                self.files[file_path] = file_obj
                self._save_state_nolock()

    async def update_file_state(self, file_path: str, **kwargs) -> None:
        """Update the state of a file in the plan."""
        async with self._lock:
            if file_path not in self.files:
                logger.warning(
                    f"File {os.path.basename(file_path)} not found in directory state"
                )
                return

            for key, value in kwargs.items():
                setattr(self.files[file_path], key, value)

            # Update last_updated if the file object has this attribute
            file_obj = self.files[file_path]
            if hasattr(file_obj, "last_updated"):
                file_obj.last_updated = datetime.now()

            self._save_state_nolock()
            logger.debug(f"Updated state for {os.path.basename(file_path)}")

    def get_file_by_path(self, file_path: str) -> RecordingFile | None:
        """Get a file by its full path."""
        return self.files.get(file_path)

    def get_last_file(self) -> RecordingFile | None:
        """Returns the last file in the group based on end time."""
        if not self.files:
            return None
        return max(self.files.values(), key=lambda f: f.end_time)

    def get_first_file(self) -> RecordingFile | None:
        """Returns the first file in the group based on start time."""
        if not self.files:
            return None
        return min(self.files.values(), key=lambda f: f.start_time)

    def get_files_by_status(self, status: str) -> list[RecordingFile]:
        """Returns a list of files matching the given status."""
        return [f for f in self.files.values() if f.status == status]

    def is_last_file(self, file_path: str) -> bool:
        """Check if this is the last file in the group to be processed."""
        # If this is the only file, it's the last file
        if len(self.files) == 1:
            return True

        # If there are any files not yet converted, this is not the last file
        for path, file_obj in self.files.items():
            if (
                path != file_path
                and hasattr(file_obj, "status")
                and file_obj.status not in ["converted", "skipped"]
            ):
                return False

        return True

    def is_file_in_state(self, file_path: str) -> bool:
        """Check if a file is already in the directory state."""
        return file_path in self.files

    # A file within this fraction of the camera-reported size counts as
    # complete. The HTTP fast path is byte-identical to the camera file; the
    # Baichuan path remuxes, shifting the size slightly — 1% absorbs that while
    # still catching truncated downloads (a partial first segment is far smaller
    # than its real ~780MB).
    _SIZE_TOLERANCE = 0.01

    def expected_size(self, file_obj: RecordingFile) -> int | None:
        """Camera-reported recorded size for a file, if known (Search metadata)."""
        try:
            size = (file_obj.metadata or {}).get("size")
            return int(size) if size else None
        except (TypeError, ValueError):
            return None

    def is_file_fully_downloaded(self, file_obj: RecordingFile) -> bool:
        """A file is fully downloaded only if its status is 'downloaded' AND the
        bytes on disk match the camera-reported size (within tolerance).

        ``download_file`` can report success after a short read (it received all
        the bytes the server offered — e.g. the boot-time first-segment race
        serves a truncated file), so status alone is not proof of a complete
        file. If the camera never reported a size, fall back to trusting status.
        """
        if file_obj.status != "downloaded":
            return False
        expected = self.expected_size(file_obj)
        if not expected:
            return True
        try:
            actual = os.path.getsize(file_obj.file_path)
        except OSError:
            return False
        return actual > 0 and abs(actual - expected) / expected < self._SIZE_TOLERANCE

    def get_incomplete_downloads(self) -> list[RecordingFile]:
        """Non-skipped files marked 'downloaded' whose on-disk size does not match
        the camera-reported size — truncated/partial, must be re-downloaded."""
        return [
            f
            for f in self.files.values()
            if not f.skip
            and f.status == "downloaded"
            and not self.is_file_fully_downloaded(f)
        ]

    def is_ready_for_combining(self) -> bool:
        """Check if all non-skipped files are downloaded and ready for combining."""
        if not self.files:
            return False

        # Filter out any files that are marked to be skipped.
        files_to_consider = [f for f in self.files.values() if not f.skip]

        # If there are no files left to consider (e.g., all were skipped), we can't combine.
        if not files_to_consider:
            return False

        # Every remaining file must be FULLY downloaded — status 'downloaded'
        # AND its bytes on disk match the camera-reported size. A truncated
        # file must never reach the combine step.
        return all(self.is_file_fully_downloaded(f) for f in files_to_consider)

    async def mark_file_as_skipped(self, file_path: str) -> None:
        """Marks a file to be skipped in future processing, without changing its status."""
        async with self._lock:
            if file_path in self.files:
                self.files[file_path].skip = True
                self._save_state_nolock()

    async def update_group_status(self, status: str, error_message: str | None = None):
        """Update the status of all files in the group."""
        async with self._lock:
            self.status = status
            self.error_message = error_message
            self._save_state_nolock()

    def set_youtube_playlist_name(self, playlist_name: str):
        """Set the YouTube playlist name in the state."""
        try:
            with FileLock(self.state_file_path):
                # Read current state under lock
                state_data = {"files": {}, "status": "pending", "error_message": None}
                if os.path.exists(self.state_file_path):
                    try:
                        with open(self.state_file_path) as f:
                            state_data = json.load(f)
                    except (json.JSONDecodeError, FileNotFoundError):
                        pass

                state_data["youtube_playlist_name"] = playlist_name

                # Ensure directory exists
                os.makedirs(os.path.dirname(self.state_file_path), exist_ok=True)

                # Atomic write
                temp_path = self.state_file_path + ".tmp"
                with open(temp_path, "w") as f:
                    json.dump(state_data, f, indent=4)
                os.replace(temp_path, self.state_file_path)
        except TimeoutError as e:
            logger.error(
                f"Timeout setting playlist name for {self.directory_path}: {e}"
            )
        logger.debug(
            f"Set youtube_playlist_name to '{playlist_name}' in {self.state_file_path}"
        )

    def set_video_loss(self, lost_seconds: float, detail: str) -> None:
        """Durably flag that combine dropped an undecodable region from this game.

        Byte-complete-but-corrupt camera segments are unrecoverable (re-download
        returns identical bytes), so combine cuts the dead span and the game
        still ships — but it must NOT be shipped as if perfect. This persists a
        ``video_loss`` marker on state.json (total seconds lost + a per-segment
        detail string) so the dashboard, state auditor, and the camera manager
        can see the game lost footage to a camera recording error. It's a flag,
        not a status change: the trim/upload flow continues normally.
        """
        try:
            with FileLock(self.state_file_path):
                state_data = {"files": {}, "status": "pending", "error_message": None}
                if os.path.exists(self.state_file_path):
                    try:
                        with open(self.state_file_path) as f:
                            state_data = json.load(f)
                    except (json.JSONDecodeError, FileNotFoundError):
                        pass

                state_data["video_loss"] = {
                    "lost_seconds": round(lost_seconds, 1),
                    "detail": detail,
                }

                os.makedirs(os.path.dirname(self.state_file_path), exist_ok=True)
                temp_path = self.state_file_path + ".tmp"
                with open(temp_path, "w") as f:
                    json.dump(state_data, f, indent=4)
                os.replace(temp_path, self.state_file_path)
        except TimeoutError as e:
            logger.error(f"Timeout setting video_loss for {self.directory_path}: {e}")
        logger.warning(
            "Flagged %.1fs of video loss in %s (%s)",
            lost_seconds,
            self.state_file_path,
            detail,
        )

    def get_video_loss(self) -> dict | None:
        """Return the ``video_loss`` marker (lost_seconds + detail) if any."""
        try:
            with FileLock(self.state_file_path):
                if os.path.exists(self.state_file_path):
                    try:
                        with open(self.state_file_path) as f:
                            return json.load(f).get("video_loss")
                    except (json.JSONDecodeError, FileNotFoundError):
                        return None
        except TimeoutError as e:
            logger.error(f"Timeout reading video_loss for {self.directory_path}: {e}")
        return None

    async def set_ttt_recording_id(self, recording_id: str) -> None:
        """Persist the TTT recording ID for this group to state.json."""
        async with self._lock:
            self.ttt_recording_id = recording_id
            self._save_state_nolock()
        logger.debug(
            "Set ttt_recording_id to '%s' in %s", recording_id, self.state_file_path
        )

    def get_youtube_playlist_name(self) -> str | None:
        """Get the YouTube playlist name from the state."""
        try:
            with FileLock(self.state_file_path):
                if os.path.exists(self.state_file_path):
                    with open(self.state_file_path) as f:
                        state_data = json.load(f)
                    return state_data.get("youtube_playlist_name")
        except (json.JSONDecodeError, FileNotFoundError, TimeoutError):
            pass
        return None

    # ------------------------------------------------------------------
    # AutoCam run tracking
    # ------------------------------------------------------------------
    # The tray's _execute_autocam_gui_automation can take 1-2 hours per
    # game. If the tray dies mid-run, the AutoCam process keeps writing
    # to disk; on tray restart we want to reattach to the running window
    # rather than throw away progress and relaunch from frame 0. These
    # helpers persist the launch's PIDs + paths to state.json so the
    # resume path can validate (a) the processes are still alive,
    # (b) the input_path matches the current task, before reattaching.
    #
    # Sync (not async) because the caller runs inside a thread executor
    # via loop.run_in_executor — the same shape as set_youtube_playlist_name.

    def set_autocam_run(self, run_data: dict) -> None:
        """Persist an active AutoCam run marker to state.json.

        Args:
            run_data: Dict with launcher_pid, gui_pids (list), input_path,
                output_path, started_at (ISO8601). Stored verbatim under
                the "autocam_run" key.
        """
        self._update_state_field("autocam_run", run_data)

    def clear_autocam_run(self) -> None:
        """Remove the autocam_run marker (run finished successfully or failed)."""
        self._update_state_field("autocam_run", None)

    def get_autocam_run(self) -> dict | None:
        """Read the autocam_run marker, or None if no run is recorded."""
        try:
            with FileLock(self.state_file_path):
                if os.path.exists(self.state_file_path):
                    with open(self.state_file_path) as f:
                        state_data = json.load(f)
                    return state_data.get("autocam_run")
        except (json.JSONDecodeError, FileNotFoundError, TimeoutError):
            pass
        return None

    # ------------------------------------------------------------------
    # Game-phase boundaries (kickoff / halftime / second-half / end)
    # ------------------------------------------------------------------
    # The phase_detect pipeline step persists the fused boundaries here so the
    # later TTT push (S2) and the dashboard can read them. Stored verbatim under
    # "game_phases" (source "phase_fused" + the four offsets + the sanity-gate
    # verdict). Sync helpers via _update_state_field, like set_autocam_run.

    def set_game_phases(self, phases: dict) -> None:
        """Persist the fused game-phase boundaries to state.json.

        Args:
            phases: Dict with at least ``source``, ``ok`` and a ``times`` map
                (kickoff / halftime / second_half / end offsets). Stored
                verbatim under the ``game_phases`` key.
        """
        self._update_state_field("game_phases", phases)

    def get_game_phases(self) -> dict | None:
        """Read the ``game_phases`` marker, or None if none is recorded."""
        try:
            with FileLock(self.state_file_path):
                if os.path.exists(self.state_file_path):
                    with open(self.state_file_path) as f:
                        state_data = json.load(f)
                    return state_data.get("game_phases")
        except (json.JSONDecodeError, FileNotFoundError, TimeoutError):
            pass
        return None

    def _update_state_field(self, key: str, value) -> None:
        """Read-modify-write a single state.json field under FileLock.

        Setting *value* to ``None`` deletes the key. Atomic via temp file
        + os.replace so a crash mid-write can't corrupt state.json.
        """
        try:
            with FileLock(self.state_file_path):
                state_data = {"files": {}, "status": "pending", "error_message": None}
                if os.path.exists(self.state_file_path):
                    try:
                        with open(self.state_file_path) as f:
                            state_data = json.load(f)
                    except (json.JSONDecodeError, FileNotFoundError):
                        pass

                if value is None:
                    state_data.pop(key, None)
                else:
                    state_data[key] = value

                os.makedirs(os.path.dirname(self.state_file_path), exist_ok=True)
                temp_path = self.state_file_path + ".tmp"
                with open(temp_path, "w") as f:
                    json.dump(state_data, f, indent=4)
                os.replace(temp_path, self.state_file_path)
        except TimeoutError as e:
            logger.error("Timeout updating %s for %s: %s", key, self.directory_path, e)
        except Exception as e:
            logger.error("Could not update %s for %s: %s", key, self.directory_path, e)
