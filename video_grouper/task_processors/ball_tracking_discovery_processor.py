"""Discovery processor that scans for trimmed groups and queues ball-tracking.

Replaces the old ``AutocamDiscoveryProcessor``.

Each ``trimmed`` group gets a ``BallTrackingTask`` whose provider is resolved
via ``config.ball_tracking.resolve_provider_for(team_name)``. Existing
``ball_tracking_complete`` groups missing a YouTube upload are recovered
once per discovery cycle.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .base_polling_processor import PollingProcessor
from .tasks.ball_tracking import BallTrackingTask
from .tasks.ball_tracking.utils import get_ball_tracking_io_paths
from video_grouper.utils.config import Config

logger = logging.getLogger(__name__)


class BallTrackingDiscoveryProcessor(PollingProcessor):
    def __init__(
        self,
        storage_path: str,
        config: Config,
        ball_tracking_processor=None,
        poll_interval: int = 30,
    ):
        super().__init__(storage_path, config, poll_interval)
        self.ball_tracking_processor = ball_tracking_processor
        self._recovered_uploads: set[str] = set()

    async def discover_work(self) -> None:
        logger.info("BALL_TRACKING_DISCOVERY: scanning for trimmed groups")
        groups_dir = Path(self.storage_path)
        if not self.ball_tracking_processor:
            logger.warning("BALL_TRACKING_DISCOVERY: no processor available; skipping")
            return

        for group_dir in groups_dir.iterdir():
            if not group_dir.is_dir():
                continue

            state_file = group_dir / "state.json"
            if not state_file.exists():
                continue

            try:
                with open(state_file, "r") as f:
                    state_data = json.load(f)
                status = state_data.get("status")
            except (json.JSONDecodeError, OSError) as e:
                logger.error(
                    "BALL_TRACKING_DISCOVERY: error reading state.json for %s: %s",
                    group_dir.name,
                    e,
                )
                continue

            if status == "trimmed":
                await self._enqueue_for(group_dir)
            elif status == "ball_tracking_complete":
                await self._recover_upload(group_dir)

        logger.info("BALL_TRACKING_DISCOVERY: discovery complete")

    async def _enqueue_for(self, group_dir: Path) -> None:
        try:
            input_path, output_path = get_ball_tracking_io_paths(
                group_dir, output_ext="mp4"
            )
        except FileNotFoundError as e:
            logger.error(
                "BALL_TRACKING_DISCOVERY: cannot enqueue %s: %s",
                group_dir.name,
                e,
            )
            return

        team_name = self._read_team_name(group_dir)
        provider_name, provider_cfg = self.config.ball_tracking.resolve_provider_for(
            team_name
        )
        task = BallTrackingTask(
            group_dir=group_dir,
            input_path=input_path,
            output_path=output_path,
            provider_name=provider_name,
            provider_config=provider_cfg.model_dump(),
            team_name=team_name,
            storage_path=str(self.storage_path),
        )
        try:
            await self.ball_tracking_processor.add_work(task)
            logger.info(
                "BALL_TRACKING_DISCOVERY: enqueued %s (provider=%s)",
                group_dir.name,
                provider_name,
            )
        except Exception as e:
            logger.error(
                "BALL_TRACKING_DISCOVERY: failed to enqueue %s: %s",
                group_dir.name,
                e,
            )

    @staticmethod
    def _read_team_name(group_dir: Path) -> str | None:
        """Read team_name from match_info.ini if present, else None."""
        match_info_path = group_dir / "match_info.ini"
        if not match_info_path.exists():
            return None
        try:
            import configparser

            parser = configparser.ConfigParser()
            parser.read(match_info_path, encoding="utf-8")
            if parser.has_option("MATCH", "team_name"):
                return parser.get("MATCH", "team_name") or None
        except Exception as e:
            logger.debug(
                "BALL_TRACKING_DISCOVERY: could not read team_name from %s: %s",
                match_info_path,
                e,
            )
        return None

    async def _recover_upload(self, group_dir: Path) -> None:
        """Queue YouTube upload for completed groups missing an upload.

        Tracks recovered dirs across cycles to avoid duplicate uploads.
        """
        group_key = str(group_dir)
        if group_key in self._recovered_uploads:
            return
        if not self.config.youtube.enabled:
            return
        upload_processor = getattr(
            self.ball_tracking_processor, "upload_processor", None
        )
        if not upload_processor:
            return
        from .tasks.upload import YoutubeUploadTask

        relative_group_dir = os.path.relpath(str(group_dir), self.storage_path)
        youtube_task = YoutubeUploadTask(group_dir=relative_group_dir)
        await upload_processor.add_work(youtube_task)
        self._recovered_uploads.add(group_key)
        logger.info(
            "BALL_TRACKING_DISCOVERY: recovered YouTube upload for %s",
            group_dir.name,
        )
