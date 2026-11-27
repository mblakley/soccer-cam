"""Keeps this install's device link alive: announce, heartbeat, run commands.

Each tick (every ``link.heartbeat_interval`` seconds): ``register`` this
install's capabilities when it offers to take processing jobs (the link makes
that idempotent), ``heartbeat``, remember which cameras the reply says this
install should poll, then run each command the reply carries -- acknowledge,
execute, report the result. The camera pollers ask :meth:`is_camera_enabled`
before every poll.

Talks only to :class:`IDeviceLink`; with TTT off that is the Null link and
every tick is a no-op that leaves every camera enabled.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..integrations.device_link import (
    DeviceCommand,
    DeviceHealth,
    HeartbeatReply,
    IDeviceLink,
)
from ..utils.config import Config
from .base_polling_processor import PollingProcessor

logger = logging.getLogger(__name__)


def job_capabilities(config: Config) -> dict[str, Any]:
    """What this install announces when it takes remote processing jobs."""
    return {
        "ffmpeg": True,
        "pipeline": config.pipeline.is_active(),
        "camera_type": config.camera.type,
        "camera_ip": config.camera.device_ip,
    }


class DeviceLinkProcessor(PollingProcessor):
    def __init__(
        self,
        storage_path: str,
        config: Config,
        link: IDeviceLink,
        *,
        command_executor: Any = None,
        capabilities: dict[str, Any] | None = None,
        error_tracker: Any = None,
    ):
        super().__init__(storage_path, config, link.heartbeat_interval)
        self.link = link
        self.command_executor = command_executor
        # None: this install does not announce itself (takes no remote jobs).
        self.capabilities = capabilities
        # Recent errors, reported with each heartbeat (None: none tracked).
        self.error_tracker = error_tracker
        # camera name -> enabled here. Empty: nothing heard yet, all enabled.
        self._camera_enabled: dict[str, bool] = {}

    def _health(self) -> DeviceHealth:
        """This install's health for the heartbeat (blocking: psutil samples)."""
        from video_grouper.utils.system_metrics import get_system_metrics
        from video_grouper.version import __version__

        metrics = get_system_metrics()
        tracker = self.error_tracker
        return DeviceHealth(
            cpu_usage_percent=metrics.get("cpu_usage_percent"),
            memory_usage_percent=metrics.get("memory_usage_percent"),
            disk_free_gb=metrics.get("disk_free_gb"),
            uptime_seconds=metrics.get("uptime_seconds"),
            version=__version__,
            last_error=tracker.get_last_error() if tracker else None,
            error_count_24h=tracker.get_error_count_24h() if tracker else None,
        )

    def is_camera_enabled(self, camera_name: str) -> bool:
        """False only when the link said this camera is not polled here."""
        return self._camera_enabled.get(camera_name, True)

    async def discover_work(self) -> None:
        """One tick. Called again after ``poll_interval`` whatever happens."""
        if self.capabilities is not None:
            await self.link.register(self.capabilities)
        reply = await self.link.heartbeat(await asyncio.to_thread(self._health))
        self._apply_camera_state(reply)
        for command in reply.commands:
            await self._run_command(command)

    def _apply_camera_state(self, reply: HeartbeatReply) -> None:
        new_state = reply.camera_enabled
        if new_state is None or new_state == self._camera_enabled:
            return
        for name, was_enabled in self._camera_enabled.items():
            if was_enabled and not new_state.get(name, True):
                logger.info("DEVICE_LINK: camera %s disabled on this machine", name)
        self._camera_enabled = dict(new_state)

    async def _run_command(self, command: DeviceCommand) -> None:
        await self.link.acknowledge_command(command.id)
        if self.command_executor is not None:
            result = await self.command_executor.execute(command)
        else:
            result = {"success": False, "message": "Command executor not available"}
        await self.link.complete_command(command.id, result)
