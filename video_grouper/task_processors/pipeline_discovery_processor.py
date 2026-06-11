"""Discovery processor that scans for trimmed groups and queues the pipeline.

Each ``trimmed`` group gets a :class:`PipelineTask`; the
:class:`PipelineProcessor` then resolves and runs the configured steps.

Stuck-upload recovery accepts BOTH completion statuses — ``pipeline_complete``
(this path) AND ``ball_tracking_complete`` (legacy path) — so an install that
straddles the migration recovers either kind of completed-but-unuploaded group.
"""

from __future__ import annotations

import configparser
import json
import logging
import os
from pathlib import Path

from video_grouper.utils.config import Config

from .base_polling_processor import PollingProcessor
from .tasks.pipeline import PipelineTask
from .tasks.pipeline.utils import get_ball_tracking_io_paths

logger = logging.getLogger(__name__)

# Group statuses that mean "the broadcast output exists but the YouTube upload
# may not have happened" — recover those once per processor lifetime.
_COMPLETE_STATUSES = ("pipeline_complete", "ball_tracking_complete")


class PipelineDiscoveryProcessor(PollingProcessor):
    def __init__(
        self,
        storage_path: str,
        config: Config,
        pipeline_processor=None,
        poll_interval: int = 30,
    ):
        super().__init__(storage_path, config, poll_interval)
        self.pipeline_processor = pipeline_processor
        self._recovered_uploads: set[str] = set()

    async def discover_work(self) -> None:
        logger.info("PIPELINE_DISCOVERY: scanning for trimmed groups")
        groups_dir = Path(self.storage_path)
        if not self.pipeline_processor:
            logger.warning("PIPELINE_DISCOVERY: no processor available; skipping")
            return

        for group_dir in groups_dir.iterdir():
            if not group_dir.is_dir():
                continue

            state_file = group_dir / "state.json"
            if not state_file.exists():
                continue

            try:
                with open(state_file) as f:
                    state_data = json.load(f)
                status = state_data.get("status")
            except (json.JSONDecodeError, OSError) as e:
                logger.error(
                    "PIPELINE_DISCOVERY: error reading state.json for %s: %s",
                    group_dir.name,
                    e,
                )
                continue

            if status == "trimmed":
                await self._enqueue_for(group_dir)
            elif status in _COMPLETE_STATUSES:
                await self._recover_upload(group_dir)

        logger.info("PIPELINE_DISCOVERY: discovery complete")

    async def _enqueue_for(self, group_dir: Path) -> None:
        try:
            input_path, output_path = get_ball_tracking_io_paths(
                group_dir, output_ext="mp4"
            )
        except FileNotFoundError as e:
            logger.error("PIPELINE_DISCOVERY: cannot enqueue %s: %s", group_dir.name, e)
            return

        team_name = self._read_team_name(group_dir)
        # Pass the TTT config through when integration is enabled — a step that
        # acquires a TTT-licensed asset reads credentials from it.
        ttt_dump = (
            self.config.ttt.model_dump()
            if getattr(self.config, "ttt", None) and self.config.ttt.enabled
            else None
        )
        task = PipelineTask(
            group_dir=group_dir,
            input_path=input_path,
            output_path=output_path,
            team_name=team_name,
            storage_path=str(self.storage_path),
            ttt_config=ttt_dump,
        )
        try:
            await self.pipeline_processor.add_work(task)
            logger.info("PIPELINE_DISCOVERY: enqueued %s", group_dir.name)
        except Exception as e:
            logger.error(
                "PIPELINE_DISCOVERY: failed to enqueue %s: %s", group_dir.name, e
            )

    @staticmethod
    def _read_team_name(group_dir: Path) -> str | None:
        """Read team_name from match_info.ini if present, else None."""
        match_info_path = group_dir / "match_info.ini"
        if not match_info_path.exists():
            return None
        try:
            parser = configparser.ConfigParser()
            parser.read(match_info_path, encoding="utf-8")
            if parser.has_option("MATCH", "team_name"):
                return parser.get("MATCH", "team_name") or None
        except Exception as e:
            logger.debug(
                "PIPELINE_DISCOVERY: could not read team_name from %s: %s",
                match_info_path,
                e,
            )
        return None

    async def _recover_upload(self, group_dir: Path) -> None:
        """Queue YouTube upload for completed groups that haven't been uploaded.

        Dedups two ways so the same group is never uploaded twice: an in-memory
        set guards against re-queuing within a process lifetime (across discovery
        cycles), and the on-disk ``complete`` status guards against re-queuing a
        group that already finished uploading (this survives a tray/service
        restart, when the in-memory set is empty again).
        """
        group_key = str(group_dir)
        if group_key in self._recovered_uploads:
            return
        if not self.config.youtube.enabled:
            return
        # If the group already reached "complete", the upload succeeded — skip.
        state_file = group_dir / "state.json"
        try:
            with open(state_file) as f:
                state_data = json.load(f)
            if state_data.get("status") == "complete":
                return
        except (json.JSONDecodeError, OSError):
            return

        upload_processor = getattr(self.pipeline_processor, "upload_processor", None)
        if not upload_processor:
            return

        from .tasks.upload import YoutubeUploadTask

        relative_group_dir = os.path.relpath(str(group_dir), self.storage_path)
        youtube_task = YoutubeUploadTask(group_dir=relative_group_dir)
        await upload_processor.add_work(youtube_task)
        self._recovered_uploads.add(group_key)
        logger.info(
            "PIPELINE_DISCOVERY: recovered YouTube upload for %s", group_dir.name
        )
