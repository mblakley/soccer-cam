import json
import logging
import os
from datetime import datetime, timedelta

import pytz

from video_grouper.cameras.base import Camera
from video_grouper.models import DirectoryState, RecordingFile
from video_grouper.task_processors.download_processor import DownloadProcessor
from video_grouper.utils.config import Config
from video_grouper.utils.locking import FileLock
from video_grouper.utils.paths import get_camera_state_path, get_home_cleanup_state_path

from .base_polling_processor import PollingProcessor

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
# How far past its start a still-open (never-closed) "connected" session is
# allowed to extend when classifying home footage in the skip block below.
# A trailing `connected` event with no matching `disconnected` used to be
# treated as open-ended (frame_end or now), so one stale connect event
# blanketed every recording made since as "recorded at home" and the poller
# skipped real game footage forever — the 2026-06-15 loss. Bound the open
# session to this horizon past its start instead of letting it run to now.
OPEN_CONNECTED_SESSION_HORIZON_HOURS = 12
# How far back the very first poll of a fresh install looks. There is no
# watermark yet, and neither camera backend can list "everything" — both
# require an explicit (start, end) and silently return [] if handed None — so
# scanning the whole card has to be spelled out as a deliberately wide window.
# The cost is one Search per *active recording day* found (Reolink walks the
# status bitmap), not per calendar day, so an empty year is nearly free.
FIRST_RUN_SCAN_DAYS = 365
# A recording is settled when we will never need to fetch it from the camera
# again: either we have the bytes (or something derived from them), or we
# deliberately gave up on it. The watermark may only advance over settled
# recordings, which is what makes "at or before the watermark" mean "done"
# rather than merely "seen".
#
# Kept as one name shared with _file_needs_download so the two can't drift:
# a status that means "done" to one and "needs download" to the other would
# make the poller re-queue everything behind the watermark.
SETTLED_FILE_STATUSES = frozenset(
    {
        "downloaded",
        "combined",
        "trimmed",
        "ball_tracking_complete",
        "pipeline_complete",
        "complete",
        "skipped",
        # Uploaded and moved off local disk. The bytes are deliberately gone;
        # their absence must never be read as "never downloaded".
        "archived",
        # Retries exhausted — the camera no longer has it, or it is corrupt.
        # Settled in the sense that matters here: we are never fetching it, so
        # it must not hold the watermark (and every later game) hostage.
        "abandoned",
    }
)
# Group-level statuses that settle every file inside them regardless of the
# individual file status — the group as a whole is finished with the camera.
SETTLED_GROUP_STATUSES = frozenset({"not_a_game", "complete", "archived"})


def create_directory(path):
    """Create a directory if it doesn't exist."""
    os.makedirs(path, exist_ok=True)


def find_existing_group_for(
    file_start_time: datetime, existing_dirs: list[str]
) -> str | None:
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
    file_start_time: datetime, storage_path: str, existing_dirs: list[str]
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


def _identify_runt_recordings(files: list[dict], existing_dirs: list[str]) -> set[str]:
    """Return the set of file paths that are *isolated* runt recordings.

    A recording is a runt to skip only when it is BOTH:
      - positively short: a parseable duration < MIN_SEGMENT_SECONDS (a
        parseable end <= start, the common aborted-stub case, counts via its
        negative duration). An *unparseable* end means the length is unknown,
        so it is NOT treated as short — better to keep a file of unknown
        length than drop a real (possibly still-recording) segment; and
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
        # Positively-short evidence only: a parseable end < MIN_SEGMENT_SECONDS
        # after start (a parseable end <= start gives a negative duration and
        # still counts). An UNPARSEABLE end -> unknown length -> not short
        # (keep), so an isolated still-recording segment isn't dropped.
        is_short = (
            end is not None and (end - start).total_seconds() < MIN_SEGMENT_SECONDS
        )
        parsed.append((fi.get("path", ""), start, eff_end, is_short))

    parsed.sort(key=lambda t: t[1])
    runts: set[str] = set()

    for i, (path, start, eff_end, is_short) in enumerate(parsed):
        if not is_short:
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
        # Reconciliation pass: re-scan the window between the watermark and
        # now when the download queue is idle, and re-queue anything missing
        # or short on disk. This heals downloads that died partway; the
        # watermark itself handles "never discovered". Tracked so we don't
        # issue a wide camera search on every poll while the queue is idle.
        self._last_reconcile_time: datetime | None = None
        self._reconcile_min_interval_seconds = 3600

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

            # Self-healing reconciliation. When the download queue is idle,
            # re-scan from the watermark forward and re-queue anything still
            # on the camera that is missing or incomplete on disk — the
            # safety net for downloads that died partway (the 2026-06-15
            # loss). Behind the watermark the same check would be actively
            # wrong: an archived game is meant to be gone locally.
            if self._should_reconcile():
                await self._reconcile_files_from_camera()

            # Check if all downloads are complete and notify to unplug
            await self._check_downloads_complete()

        except Exception as e:
            logger.error(f"CAMERA_POLLER: Error during camera polling: {e}")

    def _should_reconcile(self) -> bool:
        """Whether to run a full reconcile pass on this poll.

        Gate: the download queue must be idle (nothing queued, nothing in
        progress) so we don't fight the incremental sync for camera bandwidth,
        AND at least ``_reconcile_min_interval_seconds`` must have elapsed
        since the last reconcile so an idle queue doesn't trigger a wide
        camera search on every single poll.
        """
        dp = self.download_processor
        if dp is None:
            return False
        try:
            if dp.get_queue_size() > 0:
                return False
            if getattr(dp, "_in_progress_item", None) is not None:
                return False
        except Exception:
            return False

        if self._last_reconcile_time is None:
            return True
        elapsed = (datetime.now() - self._last_reconcile_time).total_seconds()
        return elapsed >= self._reconcile_min_interval_seconds

    async def _sync_files_from_camera(self) -> None:
        """Sync files from camera and group them."""
        # Read once per poll. Only _advance_completion_watermark writes it,
        # and that runs after this loop, so it cannot move underneath us.
        watermark = await self._get_latest_processed_time()
        start_time = watermark
        if start_time is None:
            # No watermark yet, so this install has never ingested anything:
            # scan the whole card. Both backends require an explicit range
            # (get_file_list(start, end) is mandatory on Dahua and Reolink,
            # and passing None fails *silently* as "no files" through their
            # blanket except), so "everything the camera holds" has to be
            # spelled out as a deliberately wide window.
            start_time = datetime.now() - timedelta(days=FIRST_RUN_SCAN_DAYS)
            logger.info(
                "CAMERA_POLLER: No watermark yet -- first-run scan over the "
                "last %d days.",
                FIRST_RUN_SCAN_DAYS,
            )
        else:
            # The watermark means "every recording at or before this is
            # settled", so it is the whole answer to what we still need.
            # Deliberately NOT clamped to a lookback window: clamping skips
            # a stale watermark forward, and anything it skipped over was
            # never fetched and was never queried again. That is how the
            # 2026-06-15 game was lost, and how archived games got
            # re-downloaded once the window happened to reach them.
            logger.debug("CAMERA_POLLER: Resuming from watermark %s", start_time)

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

        # Cap the number of files per poll to avoid overwhelming the pipeline.
        # Sort by start time FIRST: the camera's own ordering is not
        # guaranteed, so an unsorted slice keeps an arbitrary subset — which a
        # wide first-run scan makes very visible. Sorting makes the cap mean
        # "the oldest N", so ingest proceeds in recording order and the
        # watermark can actually advance through the backlog poll by poll.
        # startTime is zero-padded "%Y-%m-%d %H:%M:%S", so lexicographic
        # ordering is chronological.
        files.sort(key=lambda f: str(f.get("startTime") or ""))
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

        # Per-poll diagnostics so watermark stalls can be root-caused after
        # the fact: classify every file the camera returned and emit a single
        # summary line at end of poll. Without this, an HWM-stuck
        # incident (5/30 runt stuck at 19:44:06 for 14+ hours, 2026-05-30
        # tournament day) requires DEBUG logs to reconstruct.
        counts = {
            "new_added": 0,
            "already_known": 0,
            "runt": 0,
            "home_connected": 0,
            "unparseable": 0,
            "settled": 0,
        }

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
                    counts["runt"] += 1
                    continue

                file_start_time, file_end_time = _parse_recording_times(file_info)
                if file_start_time is None:
                    logger.warning(
                        f"CAMERA_POLLER: unparseable start time for {filename}; skipping"
                    )
                    counts["unparseable"] += 1
                    continue
                if file_end_time is None or file_end_time <= file_start_time:
                    # Aborted/unparseable end on a KEPT (contiguous) file — fall
                    # back to a zero-length entry so it still queues and groups
                    # sanely, instead of crashing strptime or being dropped.
                    file_end_time = file_start_time

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
                        # Bound an open-ended (never-closed) connected session
                        # to a short horizon past its start rather than letting
                        # it run to "now" and blanket every later recording as
                        # home footage. A session still within its horizon is
                        # genuinely current, so it extends to now; a stale one
                        # (horizon already past) closes at its horizon.
                        if frame_end is not None:
                            frame_end_or_now = frame_end
                        else:
                            now_utc = datetime.now(pytz.utc)
                            horizon = frame_start + timedelta(
                                hours=OPEN_CONNECTED_SESSION_HORIZON_HOURS
                            )
                            frame_end_or_now = now_utc if horizon > now_utc else horizon

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
                    counts["home_connected"] += 1
                    files_to_delete.append(file_info["path"])
                    continue

                # The watermark decides this, and nothing else does. Every
                # other "do we already have it?" test asks the local disk —
                # is_file_in_state reads the group's state.json, and reconcile
                # stats the .mp4 — and archiving deletes both on purpose, so
                # both answer "never seen it" for a game that is finished and
                # published. Enforced here rather than relying on the query
                # range: cameras return recordings that merely *overlap* the
                # window, so a game straddling the boundary comes back anyway.
                if watermark is not None and file_end_time <= watermark:
                    counts["settled"] += 1
                    logger.debug(
                        "CAMERA_POLLER: %s (end=%s) is at or before the "
                        "watermark; already handled.",
                        filename,
                        file_end_time,
                    )
                    continue

                group_dir = find_group_directory(
                    file_start_time, self.storage_path, existing_dirs
                )
                if group_dir not in existing_dirs:
                    existing_dirs.append(group_dir)

                local_path = os.path.join(group_dir, filename)

                dir_state = DirectoryState(group_dir)
                if dir_state.is_file_in_state(local_path):
                    counts["already_known"] += 1
                    logger.info(
                        "CAMERA_POLLER: File %s (end=%s) already known; "
                        "HWM advanced past it, no re-download.",
                        filename,
                        file_end_time,
                    )
                    continue

                counts["new_added"] += 1

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

        total_seen = sum(counts.values())
        logger.info(
            "CAMERA_POLLER: poll summary -- %d files seen "
            "(new=%d already_known=%d settled=%d runt=%d home_connected=%d "
            "unparseable=%d)",
            total_seen,
            counts["new_added"],
            counts["already_known"],
            counts["settled"],
            counts["runt"],
            counts["home_connected"],
            counts["unparseable"],
        )

        if (
            total_seen > 0
            and not counts["new_added"]
            and not counts["already_known"]
            and not counts["settled"]
        ):
            # Every file the camera returned was discarded as a runt, home
            # footage, or unparseable. Nothing was queued, so nothing can ever
            # settle, so the watermark cannot move and the next poll re-queries
            # this exact window — forever. Worth a warning: this is the shape
            # of the 2026-05-30 stall, where the mark stuck on a runt for 14+
            # hours and only DEBUG logs could explain why.
            logger.warning(
                "CAMERA_POLLER: %d file(s) seen but none actionable "
                "(all runt/home-connected/unparseable). Nothing queued, so "
                "the watermark cannot advance; next poll re-queries the same "
                "window.",
                total_seen,
            )

        # Advance the watermark over settled work only. Deliberately NOT to
        # latest_end_time: that is merely "the camera told us this file
        # exists", which is what the mark used to record. It moved forward
        # the instant a file was queued -- before a single byte was
        # downloaded -- so a recording that never finished downloading was
        # never queried again (the 2026-06-15 loss).
        await self._advance_completion_watermark()

    async def _file_needs_download(
        self, local_path: str, dir_state: DirectoryState, expected_size: int | None
    ) -> bool:
        """Decide whether a reconcile-discovered file must be (re)queued.

        Returns True when the on-disk reality does NOT match a completed
        download, REGARDLESS of whether the file is already known in state.
        Treating "known in state" as "done" is exactly the bug that lost the
        2026-06-15 game — a file can be in state as ``pending``/
        ``download_failed`` (or its bytes can be missing/short) yet never get
        re-queued. We check the actual disk instead:

          - the local file is missing -> needs download
          - the local file is present but its size doesn't match the
            camera-reported size (within 1%, to tolerate remux container
            framing differences) -> needs download
          - the file's recorded state is not a terminal "have the bytes"
            state (downloaded/combined/trimmed/complete/...) -> needs download
        """
        if not os.path.exists(local_path):
            return True

        if expected_size and expected_size > 0:
            try:
                actual_size = os.path.getsize(local_path)
            except OSError:
                actual_size = 0
            if actual_size <= 0:
                return True
            if abs(actual_size - expected_size) / expected_size >= 0.01:
                return True

        existing = dir_state.get_file_by_path(local_path)
        if existing is not None and existing.status not in SETTLED_FILE_STATUSES:
            return True

        return False

    async def _reconcile_files_from_camera(self) -> None:
        """Heal partial downloads: re-queue anything incomplete on disk.

        Decides purely on on-disk completeness rather than on "known in
        state", so a file recorded as ``pending``/``download_failed`` whose
        bytes are missing or short gets re-fetched. It never advances the
        watermark.

        Bounded below by the watermark. On-disk completeness is the right
        question only for recordings we still owe work on; behind the
        watermark it is the wrong question entirely, because a game archived
        off local disk is *supposed* to be missing. Scanning past the
        watermark is what re-downloaded five published July games after they
        were moved to the archive.
        """
        self._last_reconcile_time = datetime.now()

        reconcile_days = getattr(self.config.app, "reconcile_lookback_days", 14)
        end_time = datetime.now()
        start_time = end_time - timedelta(days=reconcile_days)

        watermark = await self._get_latest_processed_time()
        if watermark is not None and watermark > start_time:
            start_time = watermark

        if start_time >= end_time:
            logger.debug(
                "CAMERA_POLLER: Nothing to reconcile — watermark %s is current.",
                watermark,
            )
            return

        logger.info(
            "CAMERA_POLLER: Reconcile pass scanning %s to %s (watermark=%s)",
            start_time,
            end_time,
            watermark,
        )

        files = await self.camera.get_file_list(
            start_time=start_time, end_time=end_time
        )
        if not files:
            logger.debug("CAMERA_POLLER: Reconcile found no files on the camera.")
            return

        existing_dirs = [
            os.path.join(self.storage_path, d)
            for d in os.listdir(self.storage_path)
            if os.path.isdir(os.path.join(self.storage_path, d))
        ]

        # Reuse the runt filter so the reconcile pass doesn't re-queue lone
        # startup stubs the incremental sync already (correctly) drops.
        runt_paths = _identify_runt_recordings(files, existing_dirs)

        requeued = 0
        for file_info in files:
            try:
                if file_info["path"] in runt_paths:
                    continue

                file_start_time, file_end_time = _parse_recording_times(file_info)
                if file_start_time is None:
                    continue
                if file_end_time is None or file_end_time <= file_start_time:
                    file_end_time = file_start_time

                filename = os.path.basename(file_info["path"])
                group_dir = find_group_directory(
                    file_start_time, self.storage_path, existing_dirs
                )
                if group_dir not in existing_dirs:
                    existing_dirs.append(group_dir)
                local_path = os.path.join(group_dir, filename)

                # Prefer the camera-reported size from search metadata; fall
                # back to an explicit size probe so a short/partial local file
                # is detected even when the search result omitted size.
                expected_size = file_info.get("size")
                if not expected_size:
                    try:
                        expected_size = await self.camera.get_file_size(
                            file_info["path"]
                        )
                    except Exception:
                        expected_size = None

                dir_state = DirectoryState(group_dir)
                if not await self._file_needs_download(
                    local_path, dir_state, expected_size
                ):
                    continue

                file_info["camera_name"] = self.camera.name
                file_info["camera_type"] = self.camera.config.type
                if expected_size:
                    file_info["size"] = expected_size

                recording_file = RecordingFile(
                    start_time=file_start_time,
                    end_time=file_end_time,
                    file_path=local_path,
                    metadata=file_info,
                )

                existing_file_obj = dir_state.get_file_by_path(local_path)
                if existing_file_obj:
                    recording_file.skip = existing_file_obj.skip

                await dir_state.add_file(local_path, recording_file)

                if not recording_file.skip and self.download_processor:
                    await self.download_processor.add_work(recording_file)
                    requeued += 1
                    logger.info(
                        "CAMERA_POLLER: Reconcile re-queued %s (missing/incomplete "
                        "on disk)",
                        filename,
                    )
            except Exception as e:
                logger.error(
                    "CAMERA_POLLER: Reconcile error on file %s: %s", file_info, e
                )

        logger.info(
            "CAMERA_POLLER: Reconcile pass complete -- %d file(s) re-queued of "
            "%d scanned.",
            requeued,
            len(files),
        )

    async def _get_latest_processed_time(self) -> datetime | None:
        """Read this camera's watermark: everything at or before it is settled.

        Lives in ``camera_state.json`` at the *storage root*, deliberately not
        inside any group directory — it has to outlive the videos it describes,
        since archiving deletes whole group directories (and the per-group
        ``state.json`` with them).

        Reads under the same FileLock the writers use, so it can never observe
        a half-written file. Writers write atomically (temp + os.replace).
        Returning None means "fresh install" and triggers a full first-run
        scan, so read defensively — a spurious None here re-downloads
        everything the camera still holds.
        """
        state_path = get_camera_state_path(self.storage_path)
        if not os.path.exists(state_path):
            return None
        try:
            with FileLock(state_path):
                with open(state_path) as f:
                    all_state = json.load(f)
            cam_state = all_state.get(self.camera.name, {})
            timestamp_str = cam_state.get("latest_video_time")
            if not timestamp_str:
                return None
            return datetime.strptime(timestamp_str.strip(), default_date_format)
        except Exception as e:
            logger.error(f"CAMERA_POLLER: Could not read latest video timestamp: {e}")
            return None

    def _collect_known_recordings(self) -> list[tuple[datetime, bool, str, str]]:
        """Every recording this install knows about, as (end, settled, name, status).

        Read from the per-group ``state.json`` files. Note these live *inside*
        each group directory, so they disappear when a game is archived off
        local disk — which is precisely why they may only ever push the
        watermark forward and can never be used to re-derive it. See
        :meth:`_advance_completion_watermark`.
        """
        recordings: list[tuple[datetime, bool, str, str]] = []
        try:
            entries = os.listdir(self.storage_path)
        except OSError as e:
            logger.error("CAMERA_POLLER: Cannot list storage path: %s", e)
            return recordings

        for entry in entries:
            group_dir = os.path.join(self.storage_path, entry)
            if not os.path.isdir(group_dir):
                continue
            try:
                dir_state = DirectoryState(group_dir)
            except Exception as e:  # noqa: BLE001 — one bad group must not stall the watermark
                logger.warning("CAMERA_POLLER: Cannot read state for %s: %s", entry, e)
                continue

            group_settled = dir_state.status in SETTLED_GROUP_STATUSES
            for file_path, rec in dir_state.files.items():
                end_time = getattr(rec, "end_time", None)
                if not isinstance(end_time, datetime):
                    continue
                settled = (
                    group_settled
                    or bool(getattr(rec, "skip", False))
                    or getattr(rec, "status", None) in SETTLED_FILE_STATUSES
                )
                recordings.append(
                    (
                        end_time,
                        settled,
                        os.path.basename(file_path),
                        str(getattr(rec, "status", "?")),
                    )
                )
        return recordings

    async def _advance_completion_watermark(self) -> None:
        """Move the watermark over the settled prefix of known recordings.

        The watermark is the newest recording end time ``T`` such that *every*
        recording at or before ``T`` is settled. That contiguous-prefix rule is
        what lets "at or before the watermark" mean "finished", so the poller
        can stop asking the disk whether it still holds the bytes.

        Two properties carry the correctness:

        * **Advance only over settled work.** The old mark moved the instant a
          file was queued, before a byte was downloaded, so a download that
          never finished was never queried again (the 2026-06-15 loss).
        * **Monotonic.** The stored value is a floor and is never lowered. The
          per-group ``state.json`` files this walks are deleted when a game is
          archived, so a recompute-from-disk would see nothing and hand back
          ``None`` — which reads as "fresh install" and re-downloads every
          archived game. Archived work sits behind the floor, so its absence
          is unobservable.
        """
        stored = await self._get_latest_processed_time()

        recordings = self._collect_known_recordings()
        if not recordings:
            return

        recordings.sort(key=lambda r: r[0])

        candidate: datetime | None = None
        blocker: tuple[str, str] | None = None
        for end_time, settled, name, status in recordings:
            if not settled:
                blocker = (name, status)
                break
            candidate = end_time

        if candidate is not None and (stored is None or candidate > stored):
            await self._update_latest_processed_time(candidate)
            logger.info(
                "CAMERA_POLLER: Watermark advanced to %s%s",
                candidate,
                ""
                if blocker is None
                else f" (held there by {blocker[0]}, status={blocker[1]})",
            )
        elif blocker is not None:
            # Not an error: work in flight is the normal reason the watermark
            # sits still. It matters when it persists, so name what is holding
            # it — that is the file to fix, and the one auto-retire will
            # eventually settle if it can never complete.
            logger.info(
                "CAMERA_POLLER: Watermark held at %s by %s (status=%s)",
                stored,
                blocker[0],
                blocker[1],
            )

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
        file_paths: list[str],
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
        """Update the high-water mark for this camera in camera_state.json.

        Atomic, read-merge write under FileLock. ReolinkCamera._save_state
        writes connection_events/is_connected into the SAME file from a
        different code path; a plain truncate-then-write here could race that
        writer (or the reader) and leave camera_state.json empty/half-written,
        which resets the HWM and silently re-downloads or drops files. Read
        the current state under the lock, merge our field, write to a temp
        file, then os.replace so the on-disk file is always complete.
        """
        try:
            state_path = get_camera_state_path(self.storage_path)
            os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
            with FileLock(state_path):
                all_state = {}
                if os.path.exists(state_path):
                    try:
                        with open(state_path) as f:
                            all_state = json.load(f)
                    except (json.JSONDecodeError, FileNotFoundError):
                        all_state = {}
                cam_state = all_state.setdefault(self.camera.name, {})
                cam_state["latest_video_time"] = timestamp.strftime(default_date_format)
                temp_path = state_path + ".tmp"
                with open(temp_path, "w") as f:
                    json.dump(all_state, f, indent=4)
                os.replace(temp_path, state_path)
            logger.debug(
                f"CAMERA_POLLER: Updated latest processed time to: {timestamp}"
            )
        except Exception as e:
            logger.error(f"CAMERA_POLLER: Error updating latest processed time: {e}")
