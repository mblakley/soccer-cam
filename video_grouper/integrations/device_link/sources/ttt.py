"""Team Tech Tools device link.

=====================  ===================================================
port                   TTT client call(s)
=====================  ===================================================
register(capabilities) register_service(machine_name, capabilities), once
                       it returns an id; only while signed in
heartbeat()            send_heartbeat(service_id, "online")   if registered
                                                              + signed in
                       update_camera_status(camera_id, "online")
                                                              if camera_id
                       list_machine_cameras() -> camera_enabled
                                                              if machine_id
                       get_pending_commands(camera_id) -> commands
                                                              if camera_id
acknowledge_command    acknowledge_command(command_id)
complete_command       complete_command(command_id, result)
=====================  ===================================================

``camera_id`` is ``[TTT] camera_id`` (read on each call); ``machine_name`` is
``[TTT] machine_name`` or the host name; ``machine_id`` is this computer's
stable id (``utils.machine_id``), looked up once on the first heartbeat.
Every call is caught and logged on its own, so one failing endpoint never
stops the others or the caller.
"""

from __future__ import annotations

import asyncio
import logging
import platform
from typing import Any

from video_grouper.integrations.registry import provides

from ..base import DeviceCommand, HeartbeatReply, IDeviceLink

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
        storage_path: str = ".",
        machine_id: Any = _UNSET,
    ):
        self._client = client
        self._ttt_config = ttt_config
        self._storage_path = storage_path
        self._machine_id = machine_id
        self._service_id: str | None = None
        self.heartbeat_interval = int(getattr(ttt_config, "heartbeat_interval", 30))

    @classmethod
    def from_context(cls, context) -> TTTDeviceLink | None:
        if not context.ttt_enabled():
            return None
        return cls(context.ttt_client(), context.config.ttt, context.storage_path)

    @property
    def _camera_id(self) -> str | None:
        return getattr(self._ttt_config, "camera_id", None) or None

    def _machine(self) -> str | None:
        if self._machine_id is _UNSET:
            try:
                from video_grouper.utils.machine_id import get_or_create_machine_id

                self._machine_id = get_or_create_machine_id(self._storage_path)
            except Exception:
                self._machine_id = None
        return self._machine_id

    # ------------------------------------------------------------------
    # register
    # ------------------------------------------------------------------

    async def register(self, capabilities: dict[str, Any]) -> None:
        if self._service_id is not None:
            return
        if not self._client.is_authenticated():
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

    # ------------------------------------------------------------------
    # heartbeat
    # ------------------------------------------------------------------

    async def heartbeat(self) -> HeartbeatReply:
        await self._service_heartbeat()
        await self._camera_heartbeat()
        camera_enabled = await self._camera_enabled_state()
        commands = await self._pending_commands()
        return HeartbeatReply(camera_enabled=camera_enabled, commands=commands)

    async def _service_heartbeat(self) -> None:
        if not self._service_id or not self._client.is_authenticated():
            return
        try:
            await asyncio.to_thread(
                self._client.send_heartbeat, self._service_id, "online"
            )
        except Exception as exc:
            logger.debug("TTT_JOBS: heartbeat failed: %s", exc)

    async def _camera_heartbeat(self) -> None:
        camera_id = self._camera_id
        if not camera_id:
            return
        try:
            await asyncio.to_thread(
                lambda: self._client.update_camera_status(
                    camera_id=camera_id, status="online"
                )
            )
            logger.debug("TTT: Heartbeat sent")
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
        camera_id = self._camera_id
        if not camera_id:
            return []
        try:
            result = await asyncio.to_thread(
                self._client.get_pending_commands, camera_id
            )
        except Exception as e:
            logger.warning("TTT: Failed to poll commands: %s", e)
            return []
        if result:
            logger.info("TTT: Found %d pending command(s)", len(result))
        return [
            DeviceCommand(
                id=str(cmd["id"]),
                type=cmd.get("command_type") or "",
                parameters=cmd.get("parameters") or {},
            )
            for cmd in result or []
            if cmd.get("id")
        ]

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

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
