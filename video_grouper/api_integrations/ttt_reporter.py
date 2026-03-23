"""Optional TTT status reporter. All methods are no-ops when TTT is not configured or unreachable."""

import asyncio
import logging

logger = logging.getLogger(__name__)


class TTTReporter:
    """Optionally reports camera and service status to TTT backend.

    All methods are no-ops when TTT is not configured or unreachable.
    Soccer-cam never blocks or crashes due to TTT being unavailable.
    """

    def __init__(self, ttt_client, config):
        """
        Args:
            ttt_client: TTTApiClient instance, or None if TTT not configured.
            config: App config object.
        """
        self.client = ttt_client
        self.config = config
        self.camera_id: str | None = config.ttt.camera_id or None
        self.enabled = ttt_client is not None
        self._heartbeat_task: asyncio.Task | None = None
        self._last_status: str | None = None

    async def start(self):
        """Start periodic heartbeat reporting. No-op if TTT not configured."""
        if not self.enabled:
            logger.info("TTT integration disabled — no credentials configured")
            return

        # Try initial camera registration
        await self.try_register_camera()

        # Start heartbeat loop
        interval = self.config.ttt.heartbeat_interval
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(interval))
        logger.info("TTT reporter started (heartbeat every %ds)", interval)

    async def stop(self):
        """Stop the heartbeat loop."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        logger.info("TTT reporter stopped")

    async def try_register_camera(self) -> None:
        """Register this camera with TTT or fetch existing. No-op if TTT unavailable."""
        if not self.enabled or self.camera_id:
            return
        try:
            loop = asyncio.get_event_loop()
            assignments = await loop.run_in_executor(
                None, self.client.get_team_assignments
            )
            if assignments:
                logger.info("TTT: Found %d team assignment(s)", len(assignments))
                # Camera registration happens via TTT web UI — we just need the camera_id.
                # For now, log that we're connected but don't auto-register
                # (camera must be registered via TTT web UI first).
        except Exception as e:
            logger.warning("TTT: Failed to check team assignments: %s", e)

    async def report_camera_status(self, status: str, error: str | None = None) -> None:
        """Report camera status to TTT. Fails silently if TTT unreachable."""
        if not self.enabled or not self.camera_id:
            return
        if status == self._last_status and error is None:
            return  # Don't spam identical status updates
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: self.client.update_camera_status(
                    camera_id=self.camera_id,
                    status=status,
                    error_message=error,
                ),
            )
            self._last_status = status
            logger.debug("TTT: Reported camera status: %s", status)
        except Exception as e:
            logger.warning("TTT: Failed to report camera status: %s", e)

    async def sync_config(self) -> dict | None:
        """Pull config from TTT web UI. Returns None if TTT unreachable.

        Local config.ini is always the fallback/source of truth.
        """
        if not self.enabled or not self.camera_id:
            return None
        if not self.config.ttt.ttt_sync_enabled:
            return None
        try:
            loop = asyncio.get_event_loop()
            config = await loop.run_in_executor(
                None,
                lambda: self.client.get_camera_config(self.camera_id),
            )
            if config:
                logger.info("TTT: Pulled camera config from TTT")
            return config
        except Exception as e:
            logger.warning("TTT: Failed to pull config: %s", e)
            return None

    async def push_config(self) -> None:
        """Upload current local config to TTT for backup/transfer purposes."""
        if not self.enabled or not self.camera_id:
            return
        if not self.config.ttt.ttt_sync_enabled:
            return
        try:
            config_data = {
                "recording_config": {
                    "min_duration": self.config.recording.min_duration,
                    "max_duration": self.config.recording.max_duration,
                },
                "storage_config": {
                    "download_path": str(self.config.storage.path),
                    "min_free_gb": self.config.storage.min_free_gb,
                },
            }
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: self.client.push_camera_config(self.camera_id, config_data),
            )
            logger.info("TTT: Pushed local config to TTT for backup")
        except Exception as e:
            logger.warning("TTT: Failed to push config: %s", e)

    async def report_heartbeat(self, metrics: dict | None = None) -> None:
        """Send heartbeat with optional system metrics. Fails silently if TTT unreachable."""
        if not self.enabled:
            return
        try:
            if self.camera_id:
                status = self._last_status or "online"
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    lambda: self.client.update_camera_status(
                        camera_id=self.camera_id,
                        status=status,
                    ),
                )
                logger.debug("TTT: Heartbeat sent")
        except Exception as e:
            logger.warning("TTT: Heartbeat failed: %s", e)

    async def _heartbeat_loop(self, interval: int):
        """Periodic heartbeat — runs until cancelled."""
        while True:
            try:
                await asyncio.sleep(interval)
                await self.report_heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("TTT: Heartbeat loop error: %s", e)
                # Continue the loop — don't crash on transient errors
