from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime

from ..utils.locking import FileLock
from ..utils.paths import get_state_file_path
from .recording_file import RecordingFile

logger = logging.getLogger(__name__)

# A recording is settled when we will never need to fetch it from the camera
# again: either we have the bytes (or something derived from them), or we
# deliberately gave up on it. The camera poller's watermark may only advance
# over settled recordings, which is what makes "at or before the watermark"
# mean "done" rather than merely "seen".
#
# Kept as one name shared with the poller's _file_needs_download so the two
# can't drift: a status that means "done" to one and "needs download" to the
# other would make the poller re-queue everything behind the watermark.
#
# Split in two because the halves answer different questions. A file in
# DOWNLOADED_FILE_STATUSES got here by succeeding; one in
# GAVE_UP_FILE_STATUSES got here by failing and being let go. skip_download
# refuses the first (there is nothing to give up on) and accepts the second.
DOWNLOADED_FILE_STATUSES = frozenset(
    {
        "downloaded",
        "combined",
        "trimmed",
        "ball_tracking_complete",
        "pipeline_complete",
        "complete",
        # Uploaded and moved off local disk. The bytes are deliberately gone;
        # their absence must never be read as "never downloaded".
        "archived",
    }
)
# The terminal status a *person* sets on a download they have chosen to give
# up on (the dashboard's "Downloads that keep failing" Skip button, or the TTT
# command that calls the same function). Nothing else sets it.
SKIPPED_FILE_STATUS = "skipped"
GAVE_UP_FILE_STATUSES = frozenset(
    {
        SKIPPED_FILE_STATUS,
        # Retries exhausted and the camera confirms it no longer has the file.
        # Settled in the sense that matters here: we are never fetching it, so
        # it must not hold the watermark (and every later game) hostage.
        "abandoned",
    }
)
SETTLED_FILE_STATUSES = DOWNLOADED_FILE_STATUSES | GAVE_UP_FILE_STATUSES
# Group-level statuses that settle every file inside them regardless of the
# individual file status — the group as a whole is finished with the camera.
#
# This is exactly the set of groups the StateAuditor treats as past the
# download phase (it imports this name), and DownloadProcessor refuses to
# fetch into them too. The two must agree: a stale non-terminal file entry in
# a combined group (the camera re-listing an in-progress "_000000_" segment
# under its final name) is never re-queued and never abandoned, so if the
# watermark still waited on it, it would wait forever — and hold every later
# game's archive with it. Nothing is lost by passing it: nothing can download
# into a group at these statuses anyway.
SETTLED_GROUP_STATUSES = frozenset(
    {
        "combined",
        "trimmed",
        "ball_tracking_complete",
        "pipeline_complete",
        "complete",
        "not_a_game",
        # Uploaded and moved off local disk. Its files are *supposed* to be
        # missing locally; re-fetching them would undo the archive.
        "archived",
    }
)
# File statuses a person may skip. Anything with bytes is refused (nothing to
# give up on); "downloading" is refused because an attempt is in flight right
# now — wait for it to fail. "abandoned" is accepted so a person can put their
# name on a give-up the downloader already made.
SKIPPABLE_FILE_STATUSES = frozenset({"pending", "download_failed", "abandoned"})

# Group directory names, as the camera poller creates them.
_GROUP_DIR_FORMAT = "%Y.%m.%d-%H.%M.%S"


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
        try:
            with FileLock(self.state_file_path):
                # Read under the same lock as the write, so nothing written in
                # between (skip_download, a sibling helper) is lost.
                state_data: dict = {}
                if os.path.exists(self.state_file_path):
                    try:
                        with open(self.state_file_path) as f:
                            state_data = json.load(f)
                    except (json.JSONDecodeError, FileNotFoundError):
                        state_data = {}
                self._keep_skips_from_disk(state_data)
                state_data["status"] = self.status
                state_data["error_message"] = self.error_message
                state_data["files"] = {
                    fp: fs.to_dict() for fp, fs in self.files.items()
                }
                if self.ttt_recording_id is not None:
                    state_data["ttt_recording_id"] = self.ttt_recording_id
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

    def _keep_skips_from_disk(self, state_data: dict) -> None:
        """A person's skip on disk wins over a stale in-memory copy.

        This object's ``files`` is a snapshot from whenever it was loaded, and
        every save writes the whole map back. The download processor holds one
        for the length of a download, so a file skipped from the dashboard in
        the meantime would be silently un-skipped by the next save of any
        sibling — the person clicks Skip, and ten minutes later the file is
        back to ``download_failed``, holding the watermark again. Nothing in
        the pipeline un-skips a file, so an on-disk ``skipped`` entry is
        always the newer truth: adopt it.
        """
        disk_files = state_data.get("files")
        if not isinstance(disk_files, dict):
            return
        for fp, entry in disk_files.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("status") != SKIPPED_FILE_STATUS:
                continue
            mine = self.files.get(fp)
            if mine is None or mine.status == SKIPPED_FILE_STATUS:
                continue
            try:
                kept = RecordingFile.from_dict(entry)
            except (KeyError, TypeError, ValueError) as e:
                logger.error(
                    "Unreadable skipped entry %s in %s: %s",
                    fp,
                    self.state_file_path,
                    e,
                )
                continue
            kept.file_path = fp
            self.files[fp] = kept

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

    def get_uploaded_videos(self) -> dict:
        """Map of ``kind`` -> YouTube video id already uploaded for this group.

        Kinds are ``"processed"`` and ``"raw"``. Absent key means not yet
        uploaded. Used to make YoutubeUploadTask resumable per file: the
        task uploads both videos and only marks the group complete when
        BOTH succeed, so any failure of the second one re-ran the first
        on the next attempt. That cost a duplicate video on YouTube and
        1,600 quota units -- a sixth of the daily allowance, which is
        only six uploads/day (2026-08-03: 07.06's processed video went
        up twice, and the wasted unit is what pushed the last raw upload
        past the quota).
        """
        try:
            with FileLock(self.state_file_path):
                if os.path.exists(self.state_file_path):
                    with open(self.state_file_path) as f:
                        state_data = json.load(f)
                    value = state_data.get("uploaded_videos")
                    if isinstance(value, dict):
                        return value
        except (json.JSONDecodeError, FileNotFoundError, TimeoutError):
            pass
        return {}

    def record_uploaded_video(self, kind: str, video_id: str) -> None:
        """Remember that *kind* is already on YouTube as *video_id*."""
        existing = self.get_uploaded_videos()
        existing[kind] = video_id
        self._update_state_field("uploaded_videos", existing)

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


# ----------------------------------------------------------------------
# Giving up on a download, deliberately
# ----------------------------------------------------------------------
# A file the camera keeps listing but that never downloads (corrupt on the SD
# card, say) is left ``download_failed``: the camera has not confirmed it gone,
# so the downloader will not abandon it, and the StateAuditor retries it every
# pass. It holds that camera's watermark -- and the archiving of every later
# game -- until someone decides. These two functions are that decision: one to
# see what is stuck, one to let a file go. The dashboard calls them; the TTT
# command will call the same ones.


class SkipDownloadError(ValueError):
    """``skip_download`` refused: an unknown group or file, or a file there is
    nothing to give up on. The message says which, for a person to read."""


@dataclass(frozen=True)
class SkipResult:
    group: str
    file_name: str
    previous_status: str
    # False when the file was already skipped (a repeat request is a no-op).
    changed: bool


def _base_name(path: str) -> str:
    """Last component of a path written with either separator.

    state.json keys are local paths, and a request can name a file by its
    Windows path or its POSIX one; both must find the same entry.
    """
    return str(path).replace(os.sep, "/").replace("\\", "/").rsplit("/", 1)[-1]


def _resolve_group_dir(storage_path: str, group_dir: str) -> str:
    """Return the real group directory for *group_dir* (a name or a path).

    Refuses anything that is not a recording group directly under
    *storage_path* -- a name the poller would not have made, or a path that
    escapes the storage root.
    """
    raw = str(group_dir or "").strip()
    if not raw:
        raise SkipDownloadError("No recording group given.")
    name = os.path.basename(os.path.normpath(raw))
    try:
        datetime.strptime(name, _GROUP_DIR_FORMAT)
    except ValueError:
        raise SkipDownloadError(
            f"{raw!r} is not a recording group (expected a name like "
            "2026.07.01-10.00.00)."
        ) from None
    candidate = raw if os.path.isabs(raw) else os.path.join(storage_path, raw)
    root = os.path.normcase(os.path.realpath(storage_path))
    parent = os.path.normcase(os.path.dirname(os.path.realpath(candidate)))
    if parent != root:
        raise SkipDownloadError(
            f"{raw!r} is not a recording group in this storage folder."
        )
    if not os.path.isdir(candidate):
        raise SkipDownloadError(f"No recording group {name} in storage.")
    return candidate


def skip_download(
    storage_path: str,
    group_dir: str,
    file_path: str,
    by: str,
    reason: str | None = None,
) -> SkipResult:
    """Give up on one download, because a person said so.

    Marks the file ``skipped`` (and ``skip``), which every part of the
    pipeline treats as settled: the camera poller's watermark passes it, the
    StateAuditor stops re-queuing it, the download processor will not fetch
    it, and the rest of its group may combine without it. Records who, when
    and why on the file entry. Logged at WARNING -- footage is being given up.

    Args:
        storage_path: The storage root holding the group directories.
        group_dir: The group, by directory name (``2026.07.01-10.00.00``) or
            full path. Must sit directly under ``storage_path``.
        file_path: The file, by name or by its full local path.
        by: Who decided, e.g. ``"dashboard"`` or a TTT user. Required.
        reason: Optional note, kept on the file entry.

    Returns:
        A ``SkipResult``; ``changed`` is False if it was already skipped.

    Raises:
        SkipDownloadError: unknown group or file, no ``by``, an unreadable
            state file, or a file that is not a failed download -- one that
            already downloaded (there is nothing to give up on), is
            downloading right now, or sits in a group that is already past
            the download phase.
    """
    who = (by or "").strip()
    if not who:
        raise SkipDownloadError("Say who is skipping this download (by=...).")
    group_path = _resolve_group_dir(storage_path, group_dir)
    group = os.path.basename(os.path.normpath(group_path))
    file_name = _base_name(file_path or "")
    if not file_name:
        raise SkipDownloadError("No file given.")

    state_path = get_state_file_path(group_path, storage_path)
    if not os.path.isfile(state_path):
        raise SkipDownloadError(f"Recording group {group} has no state file.")

    note = (reason or "").strip() or None
    with FileLock(state_path):
        try:
            with open(state_path, encoding="utf-8") as f:
                state_data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise SkipDownloadError(
                f"Could not read the state of {group}: {e}"
            ) from None
        files = state_data.get("files") if isinstance(state_data, dict) else None
        if not isinstance(files, dict):
            files = {}
        key = next((fp for fp in files if _base_name(fp) == file_name), None)
        if key is None or not isinstance(files[key], dict):
            raise SkipDownloadError(f"{file_name} is not a recording in {group}.")
        entry = files[key]
        status = str(entry.get("status") or "pending")

        if status == SKIPPED_FILE_STATUS:
            logger.info(
                "DOWNLOAD: %s/%s is already skipped (by %s at %s); nothing to do.",
                group,
                file_name,
                entry.get("skipped_by"),
                entry.get("skipped_at"),
            )
            return SkipResult(group, file_name, status, changed=False)

        group_status = state_data.get("status")
        if group_status in SETTLED_GROUP_STATUSES:
            raise SkipDownloadError(
                f"{group} is already {group_status}; its files are past "
                "downloading and hold nothing up."
            )
        if status in DOWNLOADED_FILE_STATUSES:
            raise SkipDownloadError(
                f"{file_name} is already {status}. Skip is for downloads that "
                "keep failing; this one succeeded."
            )
        if status == "downloading":
            raise SkipDownloadError(
                f"{file_name} is downloading right now. Skip it if this attempt fails."
            )
        if status not in SKIPPABLE_FILE_STATUSES:
            raise SkipDownloadError(
                f"{file_name} has status {status!r}, which cannot be skipped."
            )

        now = datetime.now().isoformat()
        entry["status"] = SKIPPED_FILE_STATUS
        entry["skip"] = True
        entry["skipped_at"] = now
        entry["skipped_by"] = who
        entry["skip_reason"] = note
        entry["last_updated"] = now

        temp_path = state_path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(state_data, f, indent=4)
        os.replace(temp_path, state_path)

    logger.warning(
        "DOWNLOAD: %s/%s SKIPPED by %s (was %s, %s failed round(s)%s). It will "
        "not be downloaded; the camera watermark may now pass it.",
        group,
        file_name,
        who,
        status,
        entry.get("failed_attempts") or 0,
        f"; reason: {note}" if note else "",
    )
    return SkipResult(group, file_name, status, changed=True)


def list_stuck_downloads(storage_path: str) -> list[dict]:
    """Downloads that keep failing and are still holding things up.

    A file is listed when its group is still in the download phase, it is not
    settled (downloaded, skipped, abandoned ...) or flagged ``skip``, and it
    has failed: status ``download_failed``, or a failed retry round behind it
    while it waits in ``pending``. A file mid-download is left out -- it is
    being tried right now, and ``skip_download`` would refuse it.

    Each such file holds its camera's watermark, and with it the archiving of
    every later game. Oldest recording first. Reads ``state.json`` directly
    and passes over any group it cannot read, so one bad file never blanks
    the list.
    """
    stuck: list[dict] = []
    try:
        entries = sorted(os.listdir(storage_path))
    except OSError as e:
        logger.error("Cannot list storage %s for stuck downloads: %s", storage_path, e)
        return stuck

    for name in entries:
        try:
            datetime.strptime(name, _GROUP_DIR_FORMAT)
        except ValueError:
            continue
        state_path = os.path.join(storage_path, name, "state.json")
        if not os.path.isfile(state_path):
            continue
        try:
            with open(state_path, encoding="utf-8") as f:
                state_data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Cannot read %s for stuck downloads: %s", state_path, e)
            continue
        if not isinstance(state_data, dict):
            continue
        if state_data.get("status") in SETTLED_GROUP_STATUSES:
            continue
        files = state_data.get("files")
        if not isinstance(files, dict):
            continue
        for fp, entry in files.items():
            if not isinstance(entry, dict) or entry.get("skip"):
                continue
            status = str(entry.get("status") or "pending")
            if status in SETTLED_FILE_STATUSES or status == "downloading":
                continue
            try:
                attempts = int(entry.get("failed_attempts") or 0)
            except (TypeError, ValueError):
                attempts = 0
            if status != "download_failed" and attempts <= 0:
                continue
            metadata = entry.get("metadata")
            camera = metadata.get("camera_name") if isinstance(metadata, dict) else None
            stuck.append(
                {
                    "group": name,
                    "file_name": _base_name(fp),
                    "camera": camera,
                    "status": status,
                    "start_time": entry.get("start_time"),
                    "end_time": entry.get("end_time"),
                    "failed_attempts": attempts,
                    "last_error": entry.get("error_message"),
                    "last_failed_at": entry.get("last_failed_at"),
                }
            )
    stuck.sort(key=lambda s: (str(s["start_time"] or ""), s["group"], s["file_name"]))
    return stuck
