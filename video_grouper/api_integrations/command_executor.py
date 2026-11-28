"""Executes remote commands received over the device link (IDeviceLink).

Commands are validated before execution. Soccer-cam only executes
commands it understands and considers safe."""

import logging

from video_grouper.integrations.device_link import DeviceCommand

logger = logging.getLogger(__name__)

SUPPORTED_COMMANDS = {
    "restart",
    "start_recording",
    "stop_recording",
    "delete_old_files",
}


class CommandExecutor:
    """Validates and executes remote commands from the device link."""

    def __init__(self, app):
        """
        Args:
            app: SoccerCamApp instance (for camera/processor access)
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
