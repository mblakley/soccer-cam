"""IDeviceLink: this install's live link to whoever manages it remotely.

The app announces what this install can do (``register``), checks in on a
fixed cadence (``heartbeat``), and runs the commands the reply carries,
acknowledging each before it runs and reporting its result after
(``acknowledge_command`` / ``complete_command``). The reply also says which of
this install's cameras it should be polling: a camera can be moved to another
computer remotely. The app also reports the downloads that keep failing here
(``report_stuck_downloads``), so a person elsewhere can give up on one.

Best-effort throughout: no method raises, and with nobody on the other end the
reply says nothing (every camera stays enabled, no commands).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from video_grouper.integrations.registry import Provider


@dataclass(frozen=True)
class DeviceCommand:
    """A remote command for this install (``type`` e.g. ``"restart"``)."""

    id: str
    type: str
    parameters: dict[str, Any] = field(default_factory=dict)
    camera: str | None = None  # the camera it targets, when known


@dataclass(frozen=True)
class DeviceCamera:
    """One of this install's cameras, as it identifies itself."""

    name: str
    serial: str
    camera_type: str
    model: str | None = None
    # Read from the camera itself: with the serial, evidence that this install
    # actually has the camera (a printed serial alone isn't enough to join it).
    mac_address: str | None = None


@dataclass(frozen=True)
class HeartbeatReply:
    # camera name -> enabled on this install. None: no news this beat (keep
    # what you knew). A camera missing from the map is enabled.
    camera_enabled: dict[str, bool] | None = None
    commands: list[DeviceCommand] = field(default_factory=list)


@dataclass(frozen=True)
class DeviceHealth:
    """How this install is doing, sent with each heartbeat. Every field is
    optional; a link sends what it knows how to store."""

    cpu_usage_percent: float | None = None
    memory_usage_percent: float | None = None
    disk_free_gb: float | None = None
    uptime_seconds: int | None = None
    version: str | None = None
    last_error: str | None = None
    error_count_24h: int | None = None
    active_job_count: int | None = None
    queue_depth: int | None = None


@dataclass(frozen=True)
class StuckDownload:
    """A download that keeps failing and holds up its camera's watermark, as
    ``directory_state.list_stuck_downloads`` finds it. A person elsewhere can
    give up on it with a ``skip_download`` command."""

    camera: str | None  # this install's camera name (None: not recorded)
    group: str  # the recording group directory's name
    file_name: str
    recorded_at: str | None = None  # the recording's start, local ISO time
    failed_attempts: int = 0
    error: str | None = None  # the last failure
    last_failed_at: str | None = None  # local ISO time


class IDeviceLink(Provider, ABC):
    # Seconds between heartbeats; the link decides how often it wants to hear.
    heartbeat_interval: int = 30

    @abstractmethod
    async def register(self, capabilities: dict[str, Any]) -> None:
        """Announce this install and what it can do. Idempotent: once it has
        succeeded, later calls do nothing; until then each call retries."""

    @property
    def registers_cameras(self) -> bool:
        """Whether :meth:`register_cameras` can do anything right now (cheap,
        no network). When False the caller doesn't ask the cameras to describe
        themselves at all."""
        return True

    @abstractmethod
    async def register_cameras(self, cameras: list[DeviceCamera]) -> dict[str, str]:
        """Make this install and its cameras known; idempotent. Returns the
        link's id for each camera by name ({} when it can't say right now)."""

    @abstractmethod
    async def heartbeat(self, health: DeviceHealth | None = None) -> HeartbeatReply:
        """Check in ("online"); the reply carries camera state and commands."""

    @abstractmethod
    async def stitch_profiles(self) -> dict[str, dict[str, Any]]:
        """Stitch profiles saved for this install's cameras elsewhere (e.g. a
        web calibration tool), by camera name; cameras without one are absent."""

    @abstractmethod
    async def acknowledge_command(self, command_id: str) -> None:
        """Tell the other end a command was received (before it runs)."""

    @abstractmethod
    async def complete_command(self, command_id: str, result: dict[str, Any]) -> None:
        """Report a command's result (``{"success": bool, "message": str}``)."""

    @abstractmethod
    async def report_stuck_downloads(self, downloads: list[StuckDownload]) -> bool:
        """Replace the other end's list of this install's stuck downloads with
        ``downloads`` (``[]`` clears it). True once it has them; False when it
        could not take them right now (the caller sends them again)."""
