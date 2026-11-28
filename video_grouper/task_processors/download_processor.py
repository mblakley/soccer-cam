import logging
import os
from datetime import datetime

from video_grouper.cameras.base import Camera, CameraUnreachableError
from video_grouper.integrations.recordings import IRecordingSink, StepReport
from video_grouper.integrations.recordings.sources.null import NullRecordingSink
from video_grouper.models import DirectoryState, RecordingFile
from video_grouper.models.directory_state import SKIPPED_FILE_STATUS
from video_grouper.task_processors.video_processor import VideoProcessor
from video_grouper.utils.config import Config
from video_grouper.utils.disk_space import check_disk_space

from .base_queue_processor import QueueProcessor
from .queue_type import QueueType
from .tasks.video import CombineTask

logger = logging.getLogger(__name__)


async def _check_camera(camera: Camera) -> bool:
    """Run ``camera.check_availability`` regardless of sync/async signature.

    Tests mock the camera with a vanilla ``Mock`` whose attribute calls
    return a Mock object (not a coroutine), so an unconditional ``await``
    blows up. Detect a coroutine return value and only ``await`` then.
    Any exception inside the probe is treated as "unreachable" — we'd
    rather fail safe and stall the queue than burn retries on a
    transient probe error.
    """
    import inspect

    try:
        result = camera.check_availability()
        if inspect.iscoroutine(result):
            result = await result
        return bool(result)
    except Exception as exc:
        logger.info(
            f"DOWNLOAD: camera availability probe raised {exc!r}; "
            f"treating as unreachable"
        )
        return False


# Stored when a camera's download_file returns False without saying why.
NO_FAILURE_REASON = "the camera reported a failure with no reason"


class CameraReportedFailure(RuntimeError):
    """download_file returned False. ``reason`` is the camera's explanation
    (``Camera.last_download_error``), or NO_FAILURE_REASON."""

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


def _camera_failure_reason(camera: Camera) -> str:
    reason = getattr(camera, "last_download_error", None)
    # A str check, not truthiness: a test double's attribute is a Mock.
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return NO_FAILURE_REASON


class DownloadProcessor(QueueProcessor):
    """
    Task processor for downloading files from the camera.
    Processes download queue sequentially, one file at a time.
    """

    def __init__(
        self,
        storage_path: str,
        config: Config,
        camera: Camera,
        video_processor: VideoProcessor,
    ):
        super().__init__(storage_path, config)
        self.camera = camera
        self.video_processor = video_processor
        # Where step progress is reported; the app wires the real sink.
        self.recordings: IRecordingSink = NullRecordingSink()
        self.error_tracker = None

    @property
    def queue_type(self) -> QueueType:
        """Return the queue type for this processor."""
        return QueueType.DOWNLOAD

    def _get_priority(self, item) -> int:
        """Prioritize files for partially-downloaded groups."""
        group_dir = os.path.dirname(getattr(item, "file_path", ""))
        if group_dir and os.path.isdir(group_dir):
            if any(
                f.endswith(".mp4") and not f.endswith(".tmp.mp4")
                for f in os.listdir(group_dir)
            ):
                return 1  # Partially downloaded group — complete it first
        return 2

    async def process_item(self, item: RecordingFile) -> None:
        """
        Download a single file from the camera.

        Args:
            item: RecordingFile object to download
        """
        file_path = item.file_path
        group_dir = os.path.dirname(file_path)
        dir_state = DirectoryState(group_dir)
        file_name = os.path.basename(file_path)

        # Defense-in-depth: skip download if group has already progressed
        # past the download stage to avoid overwriting during combining.
        skip_statuses = {
            "combined",
            "trimmed",
            "ball_tracking_complete",
            "pipeline_complete",
            "complete",
        }
        if dir_state.status in skip_statuses:
            logger.info(
                f"DOWNLOAD: Skipping {file_name} - group already at status '{dir_state.status}'"
            )
            return

        # A person gave up on this file (skip_download). It may still be in
        # this queue from before they did -- queued by the auditor's last
        # pass, or restored from the persisted queue -- so check the file's
        # current state, not the item's copy of it.
        current = dir_state.get_file_by_path(file_path)
        if current is not None and (
            getattr(current, "status", None) == SKIPPED_FILE_STATUS
            or getattr(current, "skip", False) is True
        ):
            logger.info(
                f"DOWNLOAD: Not downloading {file_name} - it was skipped "
                f"(by {getattr(current, 'skipped_by', None) or 'a person'})."
            )
            return

        # Skip if the file already exists on disk at (or near) the expected
        # size. State.json can get reset (manual edit, version upgrade,
        # service restart hitting a race) — re-downloading bytes that are
        # already on the local disk would waste 10+ minutes per file at the
        # patched-firmware HTTP rate of ~80 Mbps. We accept anything within
        # 1% of the expected camera-reported size to tolerate small remux
        # differences in container framing.
        expected_size = item.metadata.get("size") if item.metadata else None
        if expected_size and os.path.exists(file_path):
            try:
                actual_size = os.path.getsize(file_path)
            except OSError:
                actual_size = 0
            if (
                actual_size > 0
                and abs(actual_size - expected_size) / expected_size < 0.01
            ):
                logger.info(
                    f"DOWNLOAD: Skipping {file_name} — already on disk "
                    f"({actual_size / 1024 / 1024:.1f}MB, expected "
                    f"{expected_size / 1024 / 1024:.1f}MB)"
                )
                await dir_state.update_file_state(file_path, status="downloaded")
                if dir_state.is_ready_for_combining() and self.video_processor:
                    combine_task = CombineTask(group_dir=group_dir)
                    add_work_result = self.video_processor.add_work(combine_task)
                    import inspect

                    if inspect.isawaitable(add_work_result):
                        await add_work_result
                    logger.info(
                        f"DOWNLOAD: Group {os.path.basename(group_dir)} ready "
                        f"for combining (skip-download path)."
                    )
                return

        # Camera-availability gate. If the camera is unreachable we want
        # to stall the queue, not burn retry strikes per file. Without
        # this gate, an offline camera makes every queued download fail
        # immediately, bumping each item's per-file retry count, and
        # within a few seconds every queued file is at 3/3 and gets
        # marked `download_failed` permanently — see CameraUnreachable
        # Error's docstring for the 2026-05-09 incident this prevents.
        camera_up = await _check_camera(self.camera)
        if not camera_up:
            raise CameraUnreachableError(
                f"camera {self.camera.name} unreachable before downloading {file_name}"
            )

        # Check disk space before downloading
        min_free_gb = self.config.storage.min_free_gb
        has_space, free_gb = check_disk_space(self.storage_path, min_free_gb)
        if not has_space:
            error_msg = (
                f"Insufficient disk space: {free_gb:.1f} GB free, need {min_free_gb} GB"
            )
            logger.error(
                f"DOWNLOAD: Low disk space ({free_gb:.1f} GB free, "
                f"minimum {min_free_gb} GB required). "
                f"Skipping download of {file_name}."
            )
            if self.error_tracker:
                self.error_tracker.record("download", error_msg, {"file": file_name})
            raise RuntimeError(error_msg)

        # The camera handles its own staging (writes to <file>.partial,
        # atomic-renames to the final path on success). We just pass the
        # destination and inspect what's on disk afterwards. The previous
        # double-wrapping (download_processor adding .tmp on top of the
        # camera's own .http.tmp / .raw.tmp) made for unreadable
        # filenames like RecM09_xxxxx.mp4.tmp.http.tmp.
        try:
            logger.info(f"DOWNLOAD: Starting download of {os.path.basename(file_path)}")
            await dir_state.update_file_state(file_path, status="downloading")
            await self.recordings.progress(
                dir_state.directory_path,
                StepReport(
                    step_id="download",
                    step_type="download",
                    label="Download",
                    status="running",
                ),
            )

            # The camera implementation may expose either a synchronous or an
            # asynchronous `download_file` method depending on the concrete
            # subclass or the way it is mocked in unit-tests.  Detect the
            # return type and `await` only when necessary so that both styles
            # are supported seamlessly.

            download_result = self.camera.download_file(
                file_path=item.metadata["path"], local_path=file_path
            )

            import asyncio

            if asyncio.iscoroutine(download_result):
                download_successful = await download_result
            else:
                download_successful = download_result

            if download_successful:
                await dir_state.update_file_state(file_path, status="downloaded")

                # "downloaded" must mean FULLY downloaded. download_file can
                # report success on a short read — it received all the bytes the
                # server offered, but the camera may serve a truncated file (the
                # boot-time first-segment race). Verify the bytes on disk match
                # the camera-reported size; if not, the file is not actually
                # downloaded — leave it pending and re-queue so it is fetched
                # again, and never let it reach the combine step.
                file_obj = dir_state.get_file_by_path(file_path)
                if file_obj is not None and not dir_state.is_file_fully_downloaded(
                    file_obj
                ):
                    actual = (
                        os.path.getsize(file_path) if os.path.exists(file_path) else 0
                    )
                    logger.warning(
                        f"DOWNLOAD: {file_name} incomplete — {actual} bytes on disk, "
                        f"camera reports {dir_state.expected_size(file_obj)}; "
                        f"re-queueing for download."
                    )
                    await dir_state.update_file_state(file_path, status="pending")
                    await self.add_work(item)
                    return

                await self.recordings.progress(
                    dir_state.directory_path,
                    StepReport(
                        step_id="download",
                        step_type="download",
                        label="Download",
                        status="complete",
                    ),
                )
                logger.info(
                    f"DOWNLOAD: Successfully downloaded {os.path.basename(file_path)}"
                )

                # Re-queue any sibling already marked 'downloaded' whose bytes
                # don't match the camera size (e.g. truncated before this guard
                # existed) so the group waits for a clean copy before combining.
                for incomplete in dir_state.get_incomplete_downloads():
                    logger.warning(
                        f"DOWNLOAD: re-queueing incomplete "
                        f"{os.path.basename(incomplete.file_path)} (size mismatch)"
                    )
                    await dir_state.update_file_state(
                        incomplete.file_path, status="pending"
                    )
                    await self.add_work(incomplete)

                # Check if all files in the group are downloaded and ready for combining
                if dir_state.is_ready_for_combining():
                    logger.info(
                        f"DOWNLOAD: Group {os.path.basename(group_dir)} is ready for combining."
                    )
                    if self.video_processor:
                        combine_task = CombineTask(group_dir=group_dir)

                        # Support both synchronous and asynchronous `add_work` implementations.
                        add_work_result = self.video_processor.add_work(combine_task)
                        import asyncio
                        import inspect

                        if inspect.isawaitable(add_work_result):
                            await add_work_result

                        logger.info(
                            f"DOWNLOAD: Handed off combine task to video processor: {combine_task}"
                        )
                    else:
                        logger.warning(
                            f"DOWNLOAD: No video processor available to queue combine task for {group_dir}"
                        )
            else:
                # Mid-download failures: if the camera dropped offline mid-
                # stream the camera class returned False because the network
                # went away, not because the file is bad. Probe once more
                # so the queue can pause without burning a retry strike.
                # Leave the file's status at `pending` (not download_failed)
                # so it gets retried cleanly when the camera returns.
                camera_still_up = await _check_camera(self.camera)
                if not camera_still_up:
                    await dir_state.update_file_state(file_path, status="pending")
                    raise CameraUnreachableError(
                        f"camera {self.camera.name} went offline while "
                        f"downloading {file_name}"
                    )
                await dir_state.update_file_state(file_path, status="download_failed")
                await self.recordings.progress(
                    dir_state.directory_path,
                    StepReport(
                        step_id="download",
                        step_type="download",
                        label="Download",
                        status="failed",
                    ),
                )
                reason = _camera_failure_reason(self.camera)
                raise CameraReportedFailure(
                    f"Download failed for {os.path.basename(file_path)}: {reason}",
                    reason=reason,
                )

        except Exception as e:
            logger.error(
                f"DOWNLOAD: An error occurred during download of {file_name}: {e}",
                exc_info=True,
            )
            # The camera's download_file owns its own staging files (.partial,
            # .partial.video, .partial.audio) and cleans them up on failure;
            # nothing to do here. StateAuditor mops up any orphans on the next
            # scan if the camera couldn't reach its own finally block.
            # Keep the error on the file, so the dashboard can say why a
            # download keeps failing without anyone reading logs.
            await dir_state.update_file_state(
                file_path,
                status="download_failed",
                # The camera's own reason, not "Download failed for <name>":
                # the dashboard already shows the name, and this is what a
                # person reads before giving up on the footage for good.
                error_message=(
                    e.reason
                    if isinstance(e, CameraReportedFailure)
                    else str(e) or type(e).__name__
                ),
                last_failed_at=datetime.now().isoformat(),
            )
            await self.recordings.progress(
                dir_state.directory_path,
                StepReport(
                    step_id="download",
                    step_type="download",
                    label="Download",
                    status="failed",
                    error=str(e),
                ),
            )
            if self.error_tracker:
                self.error_tracker.record("download", str(e), {"file": file_name})
            raise

    async def on_item_permanently_failed(self, item: RecordingFile) -> None:
        """Abandon a download only when the camera confirms the file is gone.

        The base class drops the item from the queue after its fast retries
        are exhausted, whatever the cause. The file stays at
        ``download_failed``, which is not settled, so it holds the camera
        poller's watermark. Marking it ``abandoned`` settles it and the
        watermark passes it for good — correct for a recording the camera has
        since overwritten, and a silent loss of real footage for one that
        merely hit a full disk, a timeout or a flaky link.

        So the camera is asked. Only when it answers a listing for the file's
        time window and the file is not in it is the loss confirmed; then it
        is marked ``abandoned`` and logged at ERROR, named so it can be
        chased by hand. Anything else — camera unreachable, listing failed or
        came back empty (both backends return ``[]`` on error, so empty is
        ambiguous), or the file is still listed — leaves it ``download_failed``.
        The StateAuditor re-queues that on its next pass, so it keeps being
        retried at the audit interval, holding the watermark meanwhile. When
        in doubt, keep footage.
        """
        file_path = getattr(item, "file_path", None)
        if not file_path:
            return
        name = os.path.basename(file_path)
        group_dir = os.path.dirname(file_path)
        dir_state = DirectoryState(group_dir)

        current = dir_state.get_file_by_path(file_path)
        if current is not None and current.status == SKIPPED_FILE_STATUS:
            # A person settled it while this round was failing. Theirs is the
            # decision on record; do not count, and never overwrite it.
            logger.info(
                "DOWNLOAD: %s was skipped while its retries ran out; leaving "
                "it skipped.",
                name,
            )
            return

        # One exhausted retry round, counted on the file. The StateAuditor
        # re-queues a download_failed file every pass, so this climbs for as
        # long as the file keeps failing -- the number a person looks at to
        # decide whether to skip it.
        attempts = (current.failed_attempts if current is not None else 0) + 1
        await dir_state.update_file_state(file_path, failed_attempts=attempts)

        if not await self._camera_confirms_file_gone(item):
            logger.error(
                "DOWNLOAD: %s failed %d attempts (retry round %d), but the "
                "camera has not confirmed it is gone; leaving it retryable "
                "(download_failed). It holds the camera watermark until it "
                "downloads, the camera stops listing it, or a person skips it "
                "from the dashboard's 'Downloads that keep failing' list.",
                name,
                self._max_retries,
                attempts,
            )
            return

        logger.error(
            "DOWNLOAD: Abandoning %s after %d failed attempts — the camera no "
            "longer lists it, so it can never be fetched. Marked terminal so "
            "it stops blocking the camera watermark.",
            name,
            self._max_retries,
        )
        await dir_state.update_file_state(file_path, status="abandoned")

    async def _camera_confirms_file_gone(self, item: RecordingFile) -> bool:
        """True only if the camera positively answers that it no longer has
        *item*. Every uncertain outcome is False."""
        metadata = getattr(item, "metadata", None) or {}
        remote_path = metadata.get("path") if isinstance(metadata, dict) else None
        start = getattr(item, "start_time", None)
        end = getattr(item, "end_time", None)
        if not remote_path or not isinstance(start, datetime):
            return False
        if not isinstance(end, datetime) or end < start:
            end = start

        if not await _check_camera(self.camera):
            return False

        try:
            # The file's own window first. Cameras return recordings that
            # overlap the window, so if it is still there it is listed.
            listing = await self.camera.get_file_list(start_time=start, end_time=end)
            if not listing:
                # Empty is ambiguous: both backends swallow errors as []. Ask
                # again up to now — a card that overwrote this file still
                # lists the newer recordings that replaced it.
                listing = await self.camera.get_file_list(
                    start_time=start, end_time=datetime.now()
                )
        except Exception as e:  # noqa: BLE001 — uncertainty means keep it
            logger.warning(
                "DOWNLOAD: could not list the camera to confirm %s is gone: %s",
                os.path.basename(item.file_path),
                e,
            )
            return False

        if not isinstance(listing, list) or not listing:
            return False
        return not any(
            isinstance(entry, dict) and entry.get("path") == remote_path
            for entry in listing
        )

    def get_item_key(self, item: RecordingFile) -> str:
        return f"recording:{item.file_path}"
