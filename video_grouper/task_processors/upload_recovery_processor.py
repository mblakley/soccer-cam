"""Polling discovery processor that recovers missed YouTube uploads.

Scans the storage tree on an interval for groups at status
``ball_tracking_complete`` and enqueues a ``YoutubeUploadTask`` to the
shared ``UploadProcessor``. This is the cross-app handoff point for
autocam_gui setups, where ball-tracking runs in the tray (which has no
``UploadProcessor``) and only flips state.json.

For homegrown setups the in-process ``BallTrackingProcessor`` already
queues uploads directly; this processor's per-cycle dedupe set + the
upload queue's own dedupe make double-enqueue a no-op there.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .base_polling_processor import PollingProcessor
from .upload_processor import UploadProcessor
from video_grouper.utils.config import Config

logger = logging.getLogger(__name__)


class UploadRecoveryProcessor(PollingProcessor):
    def __init__(
        self,
        storage_path: str,
        config: Config,
        upload_processor: UploadProcessor,
        poll_interval: int = 60,
    ):
        super().__init__(storage_path, config, poll_interval)
        self.upload_processor = upload_processor
        self._recovered: set[str] = set()

    async def discover_work(self) -> None:
        if not self.config.youtube.enabled:
            return
        groups_dir = Path(self.storage_path)
        for group_dir in groups_dir.iterdir():
            if not group_dir.is_dir():
                continue
            state_file = group_dir / "state.json"
            if not state_file.exists():
                continue
            try:
                with open(state_file, "r") as f:
                    status = json.load(f).get("status")
            except (json.JSONDecodeError, OSError) as e:
                logger.error(
                    "UPLOAD_RECOVERY: error reading state.json for %s: %s",
                    group_dir.name,
                    e,
                )
                continue
            if status == "ball_tracking_complete":
                await self._recover_upload(group_dir)

    async def _recover_upload(self, group_dir: Path) -> None:
        key = str(group_dir)
        if key in self._recovered:
            return
        from .tasks.upload import YoutubeUploadTask

        relative = os.path.relpath(str(group_dir), self.storage_path)
        task = YoutubeUploadTask(group_dir=relative)
        await self.upload_processor.add_work(task)
        self._recovered.add(key)
        logger.info("UPLOAD_RECOVERY: queued YouTube upload for %s", group_dir.name)
