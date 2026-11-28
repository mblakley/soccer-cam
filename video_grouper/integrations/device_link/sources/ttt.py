"""Team Tech Tools device link.

=====================  ===================================================
port                   TTT client call(s)
=====================  ===================================================
register(capabilities) register_service(machine_name, capabilities), once
                       it returns an id; only while signed in
heartbeat(health)      enhanced_heartbeat(service_id, health) if registered
                       (send_heartbeat(service_id, "online")  + signed in;
                        when there is no health to send)
                       update_camera_status(camera_id, "online")
                                                              if camera_id
                       list_machine_cameras() -> camera_enabled
                                                              if machine_id
                       get_pending_commands(camera_id) -> commands
                                                              if camera_id
acknowledge_command    acknowledge_command(command_id)
complete_command       complete_command(command_id, result)
=====================  ===================================================

"Signed in" is ``ensure_signed_in`` (an expired access token is refreshed
first), checked once per heartbeat; signed out, a heartbeat sends nothing and
reports no news. ``camera_id`` is each id in the shared ``TTTCameraIds`` (the
registered ids, else ``[TTT] camera_id``); ``machine_name`` is
``[TTT] machine_name`` or the host name; ``machine_id`` is this computer's
stable id (``utils.machine_id``), looked up once on the first heartbeat.
Every call is caught and logged on its own, so one failing endpoint never
stops the others or the caller.
"""

from __future__ import annotations

import asyncio
import logging
import platform
from dataclasses import asdict
from typing import Any

from video_grouper.integrations.registry import provides

from ..base import (
    DeviceCamera,
    DeviceCommand,
    DeviceHealth,
    HeartbeatReply,
    IDeviceLink,
)

logger = logging.getLogger(__name__)

_UNSET = object()


@provides(IDeviceLink)
class TTTDeviceLink(IDeviceLink):
    name = "ttt"
    priority = 10

    def __init__(
        self,
        client: Any,
        ttt_config: Any,
        storage_path: str | None = None,
        machine_id: Any = _UNSET,
        camera_ids: Any = None,
    ):
        self._client = client
        self._ttt_config = ttt_config
        self._storage_path = storage_path
        if camera_ids is None:
            from video_grouper.integrations.ttt_cameras import TTTCameraIds

            camera_ids = TTTCameraIds(
                storage_path, getattr(ttt_config, "camera_id", "") or None
            )
        # camera name -> TTT camera id (shared with the TTT recording sink)
        self._cameras = camera_ids
        self._machine_id = machine_id
        self._service_id: str | None = None
        self.heartbeat_interval = int(getattr(ttt_config, "heartbeat_interval", 30))

    @classmethod
    def from_context(cls, context) -> TTTDeviceLink | None:
        if not context.ttt_enabled():
            return None
        return cls(
            context.ttt_client(),
            context.config.ttt,
            context.storage_path,
            camera_ids=context.ttt_cameras(),
        )

    def _machine(self) -> str | None:
        if self._machine_id is _UNSET:
            if not self._storage_path:
                self._machine_id = None  # nowhere to keep a stable id
                return None
            try:
                from video_grouper.utils.machine_id import get_or_create_machine_id

                self._machine_id = get_or_create_machine_id(self._storage_path)
            except Exception:
                self._machine_id = None
        return self._machine_id

    async def _signed_in(self) -> bool:
        """The shared session is usable (refreshed when expired), checked off
        the event loop because a refresh is a network call."""
        try:
            return bool(await asyncio.to_thread(self._client.ensure_signed_in))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # register
    # ------------------------------------------------------------------

    async def register(self, capabilities: dict[str, Any]) -> None:
        if self._service_id is not None:
            return
        if not await self._signed_in():
            logger.debug("TTT: not signed in, service registration deferred")
            return
        machine_name = getattr(self._ttt_config, "machine_name", "") or platform.node()
        try:
            result = await asyncio.to_thread(
                self._client.register_service, machine_name, capabilities
            )
            self._service_id = result.get("id") if result else None
            logger.info(
                "TTT_JOBS: registered service as '%s' (id=%s)",
                machine_name,
                self._service_id,
            )
        except Exception as exc:
            logger.error("TTT_JOBS: failed to register service: %s", exc)

    @property
    def registers_cameras(self) -> bool:
        """A machine id and a session to register with (no network)."""
        try:
            return self._machine() is not None and bool(self._client.has_session())
        except Exception:
            return False

    async def register_cameras(self, cameras: list[DeviceCamera]) -> dict[str, str]:
        """POST /device-link/register: this machine + its cameras (keyed on
        serial by TTT), ids remembered for the recording sink."""
        cameras = [c for c in cameras if c.serial]
        machine_id = self._machine()
        if not cameras or not machine_id or not await self._signed_in():
            return {}
        machine_name = getattr(self._ttt_config, "machine_name", "") or platform.node()
        try:
            result = await asyncio.to_thread(
                self._client.register_device,
                machine_id,
                machine_name,
                [
                    {
                        "name": c.name,
                        "serial": c.serial,
                        "camera_type": c.camera_type,
                        "model": c.model,
                        "mac_address": c.mac_address,
                    }
                    for c in cameras
                ],
            )
        except Exception as e:
            logger.warning("TTT: device registration failed: %s", e)
            return {}
        rows = (result or {}).get("cameras", [])
        for row in rows:
            if row.get("error"):
                logger.warning(
                    "TTT: camera %s (%s) not registered: %s",
                    row.get("name"),
                    row.get("serial"),
                    row["error"],
                )
        ids = {
            str(row["name"]): str(row["camera_id"])
            for row in rows
            if row.get("name") and row.get("camera_id") and not row.get("error")
        }
        self._cameras.update(ids)
        if ids:
            logger.info("TTT: registered %d camera(s): %s", len(ids), sorted(ids))
        return ids

    # ------------------------------------------------------------------
    # heartbeat
    # ------------------------------------------------------------------

    async def heartbeat(self, health: DeviceHealth | None = None) -> HeartbeatReply:
        if not await self._signed_in():
            return HeartbeatReply()  # no news: keep the last camera state
        await self._service_heartbeat(health)
        await self._camera_heartbeat()
        camera_enabled = await self._camera_enabled_state()
        commands = await self._pending_commands()
        return HeartbeatReply(camera_enabled=camera_enabled, commands=commands)

    async def _service_heartbeat(self, health: DeviceHealth | None) -> None:
        if not self._service_id:
            return
        metrics = (
            {k: v for k, v in asdict(health).items() if v is not None} if health else {}
        )
        try:
            if metrics:
                # heartbeat-enhanced also marks the service online, so it
                # replaces the plain heartbeat rather than adding to it.
                await asyncio.to_thread(
                    self._client.enhanced_heartbeat, self._service_id, metrics
                )
            else:
                await asyncio.to_thread(
                    self._client.send_heartbeat, self._service_id, "online"
                )
        except Exception as exc:
            logger.debug("TTT_JOBS: heartbeat failed: %s", exc)

    def _update_camera_status_sync(self, camera_id: str, status: str) -> None:
        self._client.update_camera_status(camera_id=camera_id, status=status)

    async def _camera_heartbeat(self) -> None:
        for camera_id in self._cameras.all_ids().values():
            try:
                await asyncio.to_thread(
                    self._update_camera_status_sync, camera_id, "online"
                )
                logger.debug("TTT: Heartbeat sent for camera %s", camera_id)
            except Exception as e:
                logger.warning("TTT: Heartbeat failed: %s", e)

    async def _camera_enabled_state(self) -> dict[str, bool] | None:
        """Camera name -> enabled on this machine; None when unknown."""
        machine_id = self._machine()
        if not machine_id:
            return None
        try:
            entries = await asyncio.to_thread(self._client.list_machine_cameras)
        except Exception as e:
            logger.warning("TTT: Failed to sync camera enabled state: %s", e)
            return None
        # Only an entry for THIS machine decides. TTT lists every registered
        # machine under every team camera, so no entry for us means this
        # machine isn't registered in TTT -- that must not switch off a local
        # camera that merely shares a TTT camera's name (e.g. "default").
        state: dict[str, bool] = {}
        for entry in entries or []:
            camera_name = entry.get("camera_name", "")
            if not camera_name:
                continue
            mine = [
                m
                for m in entry.get("machines", [])
                if m.get("machine_id") == machine_id
            ]
            if mine:
                state[camera_name] = any(bool(m.get("enabled")) for m in mine)
        return state

    async def _pending_commands(self) -> list[DeviceCommand]:
        commands: list[DeviceCommand] = []
        for name, camera_id in self._cameras.all_ids().items():
            try:
                result = await asyncio.to_thread(
                    self._client.get_pending_commands, camera_id
                )
            except Exception as e:
                logger.warning("TTT: Failed to poll commands: %s", e)
                continue
            if result:
                logger.info("TTT: Found %d pending command(s)", len(result))
            commands.extend(
                DeviceCommand(
                    id=str(cmd["id"]),
                    type=cmd.get("command_type") or "",
                    parameters=cmd.get("parameters") or {},
                    camera=name or None,
                )
                for cmd in result or []
                if cmd.get("id")
            )
        return commands

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    async def stitch_profiles(self) -> dict[str, dict[str, Any]]:
        """GET /device-link/camera-config per registered camera ->
        ``stitch_profile`` (set in TTT's calibration tool)."""
        if not await self._signed_in():
            return {}
        profiles: dict[str, dict[str, Any]] = {}
        for name, camera_id in self._cameras.all_ids().items():
            try:
                config = await asyncio.to_thread(
                    self._client.get_camera_config, camera_id
                )
            except Exception as e:
                logger.warning("TTT: camera config for %s failed: %s", camera_id, e)
                continue
            profile = (config or {}).get("stitch_profile")
            if isinstance(profile, dict):
                profiles[name] = profile
        return profiles

    async def acknowledge_command(self, command_id: str) -> None:
        try:
            await asyncio.to_thread(self._client.acknowledge_command, command_id)
            logger.debug("TTT: Acknowledged command %s", command_id)
        except Exception as e:
            logger.warning("TTT: Failed to acknowledge command: %s", e)

    async def complete_command(self, command_id: str, result: dict[str, Any]) -> None:
        try:
            await asyncio.to_thread(self._client.complete_command, command_id, result)
            logger.debug("TTT: Completed command %s", command_id)
        except Exception as e:
            logger.warning("TTT: Failed to complete command: %s", e)
