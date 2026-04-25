"""Generic ball-tracking task that delegates to a registered provider.

Replaces the old AutocamTask. The discovery processor stamps the resolved
provider name + its serialized config onto each task so execution is
self-contained and survives restart via the JSON queue state file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict

import av

from video_grouper.utils.ffmpeg_utils import av_open_read

from ..base_task import BaseTask
from ...queue_type import QueueType

logger = logging.getLogger(__name__)


class BallTrackingTask(BaseTask):
    """Run ball-tracking on a trimmed panoramic input via a configured provider."""

    def __init__(
        self,
        group_dir: Path,
        input_path: str,
        output_path: str,
        provider_name: str,
        provider_config: Dict[str, Any],
        team_name: str | None = None,
        storage_path: str | None = None,
        ttt_config: Dict[str, Any] | None = None,
    ):
        """
        Args:
            group_dir: Directory containing the video group.
            input_path: Path to the trimmed panoramic source (``-raw.mp4``).
            output_path: Destination broadcast-style mp4.
            provider_name: Registry key (e.g. ``"autocam_gui"``, ``"homegrown"``).
            provider_config: Pydantic-dumped dict of the provider's config.
            team_name: Optional team identifier (passed through ``ProviderContext``).
            storage_path: Storage root (passed through ``ProviderContext``);
                falls back to ``group_dir.parent`` when not supplied.
            ttt_config: Pydantic-dumped TTTConfig dict, used by providers
                that fetch licensed artifacts from TTT (homegrown). Pass
                ``None`` when TTT integration is disabled.
        """
        self.group_dir = group_dir
        self.input_path = input_path
        self.output_path = output_path
        self.provider_name = provider_name
        self.provider_config = provider_config
        self.team_name = team_name
        self.storage_path = storage_path
        self.ttt_config = ttt_config

    @classmethod
    def queue_type(cls) -> QueueType:
        return QueueType.BALL_TRACKING

    @property
    def task_type(self) -> str:
        return "ball_tracking_process"

    def get_item_path(self) -> str:
        return str(self.group_dir)

    def serialize(self) -> Dict[str, object]:
        return {
            "task_type": self.task_type,
            "group_dir": str(self.group_dir),
            "input_path": self.input_path,
            "output_path": self.output_path,
            "provider_name": self.provider_name,
            "provider_config": dict(self.provider_config),
            "team_name": self.team_name,
            "storage_path": self.storage_path,
            "ttt_config": dict(self.ttt_config) if self.ttt_config else None,
        }

    @classmethod
    def deserialize(cls, data: Dict[str, object]) -> "BallTrackingTask":
        ttt_cfg = data.get("ttt_config")
        return cls(
            group_dir=Path(data["group_dir"]),
            input_path=data["input_path"],
            output_path=data["output_path"],
            provider_name=data["provider_name"],
            provider_config=dict(data.get("provider_config") or {}),
            team_name=data.get("team_name"),
            storage_path=data.get("storage_path"),
            ttt_config=dict(ttt_cfg) if ttt_cfg else None,
        )

    def _validate_video_file(self, path: str) -> bool:
        """Use PyAV to verify the source file exists, has size, and decodes."""
        if not os.path.isfile(path):
            logger.error("BALL_TRACKING: input file does not exist: %s", path)
            return False

        file_size = os.path.getsize(path)
        if file_size < 10_000:
            logger.error(
                "BALL_TRACKING: input file too small (%d bytes): %s", file_size, path
            )
            return False

        try:
            with av_open_read(path) as container:
                duration = None
                if container.duration is not None:
                    duration = container.duration / av.time_base
                else:
                    for stream in container.streams.video:
                        if stream.duration is not None and stream.time_base is not None:
                            duration = float(stream.duration * stream.time_base)
                            break
                if duration is None or duration <= 0:
                    logger.error(
                        "BALL_TRACKING: invalid duration (%s) for %s", duration, path
                    )
                    return False
                logger.info(
                    "BALL_TRACKING: input validated — duration=%.1fs, size=%.1fMB: %s",
                    duration,
                    file_size / (1024 * 1024),
                    path,
                )
                return True
        except (ValueError, av.error.FFmpegError) as e:
            logger.error("BALL_TRACKING: error validating input file: %s", e)
            return False

    async def execute(self) -> bool:
        try:
            logger.info(
                "BALL_TRACKING: processing group=%s provider=%s",
                self.group_dir.name,
                self.provider_name,
            )

            if not self._validate_video_file(self.input_path):
                return False

            # Ensure built-in providers are registered.
            from video_grouper.ball_tracking import (  # noqa: F401
                register_providers,
            )
            from video_grouper.ball_tracking import create_provider
            from video_grouper.ball_tracking.base import ProviderContext

            provider = create_provider(self.provider_name, self.provider_config)
            ctx = ProviderContext(
                group_dir=self.group_dir,
                team_name=self.team_name,
                storage_path=Path(self.storage_path or self.group_dir.parent),
                ttt_config=self.ttt_config,
            )
            success = await provider.run(self.input_path, self.output_path, ctx)
            if success:
                logger.info("BALL_TRACKING: completed group=%s", self.group_dir.name)
            else:
                logger.error(
                    "BALL_TRACKING: provider returned False for group=%s",
                    self.group_dir.name,
                )
            return success
        except Exception:
            logger.exception(
                "BALL_TRACKING: error processing group=%s", self.group_dir.name
            )
            return False

    def __str__(self) -> str:
        return (
            f"BallTrackingTask(group_dir={self.group_dir}, "
            f"provider={self.provider_name}, input={self.input_path})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BallTrackingTask):
            return False
        return (
            self.group_dir == other.group_dir
            and self.input_path == other.input_path
            and self.output_path == other.output_path
            and self.provider_name == other.provider_name
            and self.provider_config == other.provider_config
        )

    def __hash__(self) -> int:
        # provider_config is a dict (unhashable) — hash via sorted-items tuple.
        cfg_hashable = (
            tuple(sorted(self.provider_config.items())) if self.provider_config else ()
        )
        return hash(
            (
                self.group_dir,
                self.input_path,
                self.output_path,
                self.provider_name,
                cfg_hashable,
            )
        )

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "BallTrackingTask":
        return cls.deserialize(data)
