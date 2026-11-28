"""No device link (TTT off): nothing is sent, every camera stays enabled and
no commands ever arrive."""

from __future__ import annotations

from typing import Any

from video_grouper.integrations.registry import NULL_PRIORITY, provides

from ..base import (
    DeviceCamera,
    DeviceHealth,
    HeartbeatReply,
    IDeviceLink,
    StuckDownload,
)


@provides(IDeviceLink)
class NullDeviceLink(IDeviceLink):
    name = "none"
    priority = NULL_PRIORITY

    @classmethod
    def from_context(cls, context) -> NullDeviceLink:
        return cls()

    async def register(self, capabilities: dict[str, Any]) -> None:
        return None

    @property
    def registers_cameras(self) -> bool:
        return False  # nobody to register them with

    async def register_cameras(self, cameras: list[DeviceCamera]) -> dict[str, str]:
        return {}

    async def heartbeat(self, health: DeviceHealth | None = None) -> HeartbeatReply:
        return HeartbeatReply()

    async def stitch_profiles(self) -> dict[str, dict[str, Any]]:
        return {}

    async def acknowledge_command(self, command_id: str) -> None:
        return None

    async def complete_command(self, command_id: str, result: dict[str, Any]) -> None:
        return None

    async def report_stuck_downloads(self, downloads: list[StuckDownload]) -> bool:
        return False  # nobody to tell
