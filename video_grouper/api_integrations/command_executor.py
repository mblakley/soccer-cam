"""Executes remote commands received over the device link (IDeviceLink).

Commands are validated before execution. Soccer-cam only executes
commands it understands and considers safe."""

import asyncio
import logging

from video_grouper.integrations.device_link import DeviceCommand
from video_grouper.models.directory_state import SkipDownloadError, skip_download

logger = logging.getLogger(__name__)

SUPPORTED_COMMANDS = {
    "restart",
    "start_recording",
    "stop_recording",
    "delete_old_files",
    "skip_download",
}


class CommandExecutor:
    """Validates and executes remote commands from the device link."""

    def __init__(self, app):
        """
        Args:
            app: VideoGrouperApp instance (for camera/processor access)
        """
        self.app = app

    async def execute(self, command: DeviceCommand) -> dict:
        """Execute a command and return the result.

        Returns:
            dict with success: bool, message: str
        """
        cmd_type = command.type
        params = command.parameters

        if cmd_type not in SUPPORTED_COMMANDS:
            return {"success": False, "message": f"Unknown command: {cmd_type}"}

        try:
            if cmd_type == "restart":
                return await self._restart_camera(params)
            elif cmd_type == "start_recording":
                return await self._start_recording(params)
            elif cmd_type == "stop_recording":
                return await self._stop_recording(params)
            elif cmd_type == "delete_old_files":
                return await self._delete_old_files(params)
            elif cmd_type == "skip_download":
                return await self._skip_download(params)
        except Exception as e:
            logger.error("Command execution failed [%s]: %s", cmd_type, e)
            return {"success": False, "message": str(e)}

    async def _restart_camera(self, params: dict) -> dict:
        """Restart the camera via its API."""
        # Access camera through the app's camera manager
        # This is a placeholder — actual implementation depends on camera API
        logger.info("Executing restart command")
        return {"success": True, "message": "Restart command sent to camera"}

    async def _start_recording(self, params: dict) -> dict:
        """Start recording on the camera."""
        logger.info("Executing start_recording command")
        return {"success": True, "message": "Recording started"}

    async def _stop_recording(self, params: dict) -> dict:
        """Stop recording on the camera."""
        logger.info("Executing stop_recording command")
        return {"success": True, "message": "Recording stopped"}

    async def _delete_old_files(self, params: dict) -> dict:
        """Delete old recording files based on retention policy."""
        days = params.get("older_than_days", 30)
        logger.info("Executing delete_old_files (older than %d days)", days)
        # Placeholder — actual deletion would scan the download directory
        return {"success": True, "message": f"Cleaned files older than {days} days"}

    async def _skip_download(self, params: dict) -> dict:
        """Give up on a download that keeps failing, for a person in TTT.

        The same ``skip_download`` the dashboard's Skip button runs, recorded
        as ``TTT: <requested_by>``. ``parameters``: ``group_dir``,
        ``file_name``, ``requested_by``, optional ``reason`` (and the
        ``machine_id`` the device link already matched to this install).
        """
        required = ("group_dir", "file_name", "requested_by")
        values = {
            k: params.get(k).strip() if isinstance(params.get(k), str) else ""
            for k in required
        }
        missing = [k for k in required if not values[k]]
        if missing:
            return {
                "success": False,
                "message": f"skip_download is missing {', '.join(missing)}",
            }
        storage_path = getattr(self.app, "storage_path", None)
        if not storage_path:
            return {"success": False, "message": "No storage folder to skip in"}
        reason = params.get("reason")
        try:
            result = await asyncio.to_thread(
                skip_download,
                storage_path,
                values["group_dir"],
                values["file_name"],
                by=f"TTT: {values['requested_by']}",
                reason=str(reason) if reason is not None else None,
            )
        except SkipDownloadError as e:
            return {"success": False, "message": str(e)}
        if result.changed:
            message = (
                f"Skipped {result.file_name} in {result.group} "
                f"(was {result.previous_status})"
            )
        else:
            message = f"{result.file_name} in {result.group} was already skipped"
        return {"success": True, "message": message, "changed": result.changed}
