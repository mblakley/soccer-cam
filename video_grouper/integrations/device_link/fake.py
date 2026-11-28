"""In-memory IDeviceLink for tests: replies with what it was given and records
every call. Not registered."""

from __future__ import annotations

from typing import Any

from .base import (
    DeviceCamera,
    DeviceCommand,
    DeviceHealth,
    HeartbeatReply,
    IDeviceLink,
    StuckDownload,
)


class FakeDeviceLink(IDeviceLink):
    """Each heartbeat pops the next of ``replies`` (the last one repeats; none
    -> an empty reply). ``errors`` maps a verb to the exception it raises --
    for proving consumers survive a link that breaks its no-raise promise."""

    name = "fake"

    def __init__(
        self,
        replies: list[HeartbeatReply] | None = None,
        *,
        heartbeat_interval: int = 30,
        errors: dict[str, BaseException] | None = None,
        registers_cameras: bool = True,
    ):
        self.can_register_cameras = registers_cameras
        self.replies = list(replies or [])
        self.heartbeat_interval = heartbeat_interval
        self.errors = dict(errors or {})
        self.registered: list[dict[str, Any]] = []
        self.health_reports: list[DeviceHealth | None] = []
        self.registered_cameras: list[list[DeviceCamera]] = []
        self.camera_ids: dict[str, str] = {}
        self.profiles: dict[str, dict[str, Any]] = {}
        self.heartbeats = 0
        # ("ack", id) / ("complete", id, result), in call order
        self.command_calls: list[tuple[Any, ...]] = []
        # Every stuck-download list it was given; False: refuse them (as a
        # link that could not deliver them would).
        self.stuck_reports: list[list[StuckDownload]] = []
        self.accepts_stuck_reports = True

    @classmethod
    def from_context(cls, context) -> None:
        return None

    @classmethod
    def replying(
        cls,
        camera_enabled: dict[str, bool] | None = None,
        commands: list[DeviceCommand] | None = None,
        **kwargs: Any,
    ) -> FakeDeviceLink:
        return cls([HeartbeatReply(camera_enabled, list(commands or []))], **kwargs)

    def _maybe_raise(self, verb: str) -> None:
        if verb in self.errors:
            raise self.errors[verb]

    async def register(self, capabilities: dict[str, Any]) -> None:
        self.registered.append(dict(capabilities))
        self._maybe_raise("register")

    @property
    def registers_cameras(self) -> bool:
        return self.can_register_cameras

    async def register_cameras(self, cameras: list[DeviceCamera]) -> dict[str, str]:
        self.registered_cameras.append(list(cameras))
        self._maybe_raise("register_cameras")
        return {
            c.name: self.camera_ids[c.name]
            for c in cameras
            if c.name in self.camera_ids
        }

    async def heartbeat(self, health: DeviceHealth | None = None) -> HeartbeatReply:
        self.heartbeats += 1
        self.health_reports.append(health)
        self._maybe_raise("heartbeat")
        if not self.replies:
            return HeartbeatReply()
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

    async def stitch_profiles(self) -> dict[str, dict[str, Any]]:
        self._maybe_raise("stitch_profiles")
        return dict(self.profiles)

    async def acknowledge_command(self, command_id: str) -> None:
        self.command_calls.append(("ack", command_id))
        self._maybe_raise("acknowledge_command")

    async def complete_command(self, command_id: str, result: dict[str, Any]) -> None:
        self.command_calls.append(("complete", command_id, result))
        self._maybe_raise("complete_command")

    async def report_stuck_downloads(self, downloads: list[StuckDownload]) -> bool:
        self.stuck_reports.append(list(downloads))
        self._maybe_raise("report_stuck_downloads")
        return self.accepts_stuck_reports
