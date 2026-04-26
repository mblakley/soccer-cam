import logging
from typing import Any, Optional

from .base_queue_processor import QueueProcessor
from .tasks.upload import BaseUploadTask
from .queue_type import QueueType
from video_grouper.utils.config import Config

logger = logging.getLogger(__name__)


class UploadProcessor(QueueProcessor):
    """
    Task processor for upload operations (YouTube, etc.).
    Processes upload tasks sequentially.

    Runs in the main pipeline (headless). Uses stored YouTube OAuth tokens
    for uploads. If the token is expired and cannot be refreshed, sends
    an NTFY notification asking the user to re-authenticate via the tray app.
    """

    def __init__(
        self,
        storage_path: str,
        config: Config,
        ntfy_service: Optional[Any] = None,
    ):
        """Initialize the upload processor.

        Args:
            storage_path: Base storage path
            config: Application configuration
            ntfy_service: Optional NtfyService for playlist requests and auth notifications
        """
        super().__init__(storage_path, config)
        self.config = config
        self.ntfy_service = ntfy_service
        self.ttt_reporter = None

    @property
    def queue_type(self) -> QueueType:
        """Return the queue type for this processor."""
        return QueueType.UPLOAD

    async def process_item(self, item: BaseUploadTask) -> None:
        """
        Process an upload task.

        Validates YouTube token before attempting upload. If the token is
        invalid and cannot be refreshed (headless - no browser), sends an
        NTFY notification and raises an error to trigger retry.

        Args:
            item: BaseUploadTask to process
        """
        try:
            logger.info(f"UPLOAD: Processing task: {item}")

            # Report upload start to TTT (best-effort)
            if self.ttt_reporter:
                group_dir = item.get_item_path()
                try:
                    from video_grouper.models import DirectoryState

                    dir_state = DirectoryState(group_dir)
                    await self.ttt_reporter.update_recording_status(
                        dir_state.ttt_recording_id, "upload", "in_progress"
                    )
                except Exception:
                    pass  # Never block upload on TTT

            # In mock mode, simulate successful upload without actual YouTube API calls
            if self.config.youtube.use_mock:
                logger.info(f"UPLOAD: Mock mode enabled - simulating upload for {item}")
                logger.info(f"UPLOAD: Successfully completed task: {item}")
                return

            # Validate YouTube token (non-interactive, no browser)
            from video_grouper.utils.youtube_upload import (
                get_youtube_paths,
                ensure_valid_token,
            )

            credentials_file, token_file = get_youtube_paths(self.storage_path)
            token_valid, message = ensure_valid_token(credentials_file, token_file)

            if not token_valid:
                logger.error(f"UPLOAD: YouTube auth issue: {message}")
                # Phase 0b: write the cross-process flag so the dashboard
                # banner + (later) tray notification surface a "needs
                # interactive re-auth" state to the user.
                from video_grouper.web.auth_status import write_auth_needed

                write_auth_needed(self.storage_path, "youtube", message)
                # Send NTFY notification if service available
                if self.ntfy_service:
                    try:
                        await self.ntfy_service.send_notification(
                            title="YouTube Authentication Required",
                            message=message,
                        )
                    except Exception as ntfy_err:
                        logger.warning(
                            f"UPLOAD: Failed to send auth notification: {ntfy_err}"
                        )
                raise RuntimeError(f"YouTube authentication failed: {message}")

            # Execute the task with dependencies
            success = await item.execute(
                youtube_config=self.config.youtube,
                ntfy_service=self.ntfy_service,
                storage_path=self.storage_path,
            )

            if success:
                logger.info(f"UPLOAD: Successfully completed task: {item}")
                # Phase 0b: a successful upload proves the YouTube token is
                # valid, so any "needs re-auth" flag is stale. Clear it.
                from video_grouper.web.auth_status import clear_auth_needed

                clear_auth_needed(self.storage_path, "youtube")
                # Report upload complete to TTT (best-effort)
                if self.ttt_reporter:
                    group_dir = item.get_item_path()
                    try:
                        from video_grouper.models import DirectoryState

                        dir_state = DirectoryState(group_dir)
                        await self.ttt_reporter.update_recording_status(
                            dir_state.ttt_recording_id, "upload", "complete"
                        )
                    except Exception:
                        pass  # Never block upload on TTT
            else:
                logger.error(f"UPLOAD: Task execution failed: {item}")
                # Report upload failure to TTT (best-effort)
                if self.ttt_reporter:
                    group_dir = item.get_item_path()
                    try:
                        from video_grouper.models import DirectoryState

                        dir_state = DirectoryState(group_dir)
                        await self.ttt_reporter.update_recording_status(
                            dir_state.ttt_recording_id, "upload", "failed"
                        )
                    except Exception:
                        pass  # Never block upload on TTT

        except Exception as e:
            from video_grouper.utils.youtube_upload import YouTubeQuotaError

            if isinstance(e, YouTubeQuotaError):
                raise  # Let base processor handle quota errors
            logger.error(f"UPLOAD: Error processing task {item}: {e}")

    def get_item_key(self, item: BaseUploadTask) -> str:
        """Get unique key for a BaseUploadTask."""
        return f"{item.task_type}:{item.get_item_path()}"
