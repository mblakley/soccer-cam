"""Processor for archiving published games off the working drive.

Sits after upload: a group reaches ``complete`` when its videos are on
YouTube, and only then is it safe to reclaim the local footage. Runs on its
own queue because verifying a game means hashing tens of gigabytes, which
must not sit on the upload path.
"""

import logging
import os
from datetime import datetime

from video_grouper.utils.config import Config
from video_grouper.utils.locking import FileLock
from video_grouper.utils.paths import get_camera_state_path

from .base_queue_processor import QueueProcessor
from .queue_type import QueueType
from .tasks.archive import ArchiveTask
from .tasks.base_task import BaseTask

logger = logging.getLogger(__name__)

_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class ArchiveProcessor(QueueProcessor):
    """Copies completed groups to the archive root and reclaims their space."""

    def __init__(self, storage_path: str, config: Config):
        super().__init__(storage_path, config)

    @property
    def queue_type(self) -> QueueType:
        return QueueType.ARCHIVE

    def get_item_key(self, item: BaseTask) -> str:
        return f"archive:{item.get_item_path()}"

    def _read_watermark(self) -> datetime | None:
        """Newest watermark across all cameras in ``camera_state.json``.

        The archive guard needs "has the poller finished with this time
        range?", and with several cameras the answer is only yes when the
        *slowest* one has passed it. Take the minimum across cameras that
        have a watermark, so one lagging camera cannot let a game be archived
        out from under it.
        """
        state_path = get_camera_state_path(self.storage_path)
        if not os.path.exists(state_path):
            return None
        try:
            with FileLock(state_path):
                import json

                with open(state_path) as fh:
                    all_state = json.load(fh)
        except Exception as e:  # noqa: BLE001
            logger.error("ARCHIVE: cannot read camera state: %s", e)
            return None

        marks: list[datetime] = []
        for value in all_state.values():
            if not isinstance(value, dict):
                continue  # legacy top-level connection_events / is_connected
            raw = value.get("latest_video_time")
            if not raw:
                continue
            try:
                marks.append(datetime.strptime(str(raw).strip(), _DATE_FORMAT))
            except ValueError:
                continue
        return min(marks) if marks else None

    async def process_item(self, item: BaseTask) -> None:
        if not isinstance(item, ArchiveTask):
            logger.error("ARCHIVE: unexpected task %r; ignoring.", item)
            return

        archive_cfg = self.config.archive
        if not archive_cfg.enabled:
            logger.debug("ARCHIVE: disabled; skipping %s", item.group_dir)
            return

        ok = await item.execute(
            archive_root=archive_cfg.path,
            delete_after_verify=archive_cfg.delete_after_verify,
            watermark=self._read_watermark(),
        )
        if not ok:
            # Raise so the base class applies its retry/backoff. A refusal on
            # the watermark guard is expected and temporary — the retry will
            # succeed once the poller settles the recordings ahead of it.
            raise RuntimeError(f"Archive incomplete for {item.group_dir}")
