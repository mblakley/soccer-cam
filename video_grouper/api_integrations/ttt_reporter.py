"""Optional TTT status reporter. All methods are no-ops when TTT is not configured or unreachable."""

import asyncio
import logging

from video_grouper.utils.error_tracker import ErrorTracker
from video_grouper.utils.system_metrics import get_system_metrics

logger = logging.getLogger(__name__)


class TTTReporter:
    """Optionally reports camera and service status to TTT backend.

    All methods are no-ops when TTT is not configured or unreachable.
    Soccer-cam never blocks or crashes due to TTT being unavailable.
    """

    def __init__(
        self,
        ttt_client,
        config,
        error_tracker: ErrorTracker | None = None,
        command_executor=None,
    ):
        """
        Args:
            ttt_client: TTTApiClient instance, or None if TTT not configured.
            config: App config object.
            error_tracker: Optional shared ErrorTracker for recording pipeline errors.
            command_executor: Optional CommandExecutor for executing remote commands.
        """
        self.client = ttt_client
        self.config = config
        self.camera_id: str | None = config.ttt.camera_id or None
        self.enabled = ttt_client is not None
        self._heartbeat_task: asyncio.Task | None = None
        self._last_status: str | None = None
        self._cached_team_id: str | None = None
        self.error_tracker = error_tracker or ErrorTracker()
        self._command_executor = command_executor

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
        """Send heartbeat with system metrics. Fails silently if TTT unreachable."""
        if not self.enabled:
            return
        try:
            system_metrics = get_system_metrics()

            # Add queue/error info
            heartbeat_data = {
                **system_metrics,
                "last_error": self.error_tracker.get_last_error(),
                "error_count_24h": self.error_tracker.get_error_count_24h(),
            }

            # Merge any externally provided metrics (active_job_count, queue_depth, version)
            if metrics:
                heartbeat_data.update(metrics)

            # Use enhanced heartbeat if we have a service_id, fall back to camera status
            service_id = getattr(self.config.ttt, "service_id", None)
            if service_id:
                await asyncio.get_event_loop().run_in_executor(
                    None, self.client.enhanced_heartbeat, service_id, heartbeat_data
                )
            elif self.camera_id:
                status = self._last_status or "online"
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self.client.update_camera_status(
                        camera_id=self.camera_id,
                        status=status,
                    ),
                )

            logger.debug("TTT: Heartbeat sent with metrics")
        except Exception as e:
            logger.warning("TTT: Heartbeat failed: %s", e)

    async def _heartbeat_loop(self, interval: int):
        """Periodic heartbeat + command polling — runs until cancelled."""
        while True:
            try:
                await asyncio.sleep(interval)
                await self.report_heartbeat()

                # Poll for commands every heartbeat cycle
                commands = await self.poll_pending_commands()
                for cmd in commands:
                    await self._execute_command(cmd)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("TTT: Heartbeat loop error: %s", e)
                # Continue the loop — don't crash on transient errors

    async def _execute_command(self, command: dict) -> None:
        """Acknowledge, execute, and report result for a command."""
        cmd_id = command.get("id")
        if not cmd_id:
            return

        # Acknowledge receipt
        await self.acknowledge_command(cmd_id)

        # Execute
        if self._command_executor:
            result = await self._command_executor.execute(command)
        else:
            result = {"success": False, "message": "Command executor not available"}

        # Report result
        await self.complete_command(cmd_id, result)

    # ------------------------------------------------------------------
    # Command polling & auto-record rules (Phase 5B)
    # ------------------------------------------------------------------

    async def pull_auto_record_rules(self) -> dict | None:
        """Pull auto-record rules from TTT. Returns None if unavailable."""
        if not self.enabled or not self.camera_id:
            return None
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, self.client.get_auto_record_rules, self.camera_id
            )
            if result:
                logger.info("TTT: Pulled auto-record rules")
            return result
        except Exception as e:
            logger.warning("TTT: Failed to pull auto-record rules: %s", e)
            return None

    async def poll_pending_commands(self) -> list[dict]:
        """Poll for pending commands from TTT. Returns empty list on failure."""
        if not self.enabled or not self.camera_id:
            return []
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, self.client.get_pending_commands, self.camera_id
            )
            if result:
                logger.info("TTT: Found %d pending command(s)", len(result))
            return result or []
        except Exception as e:
            logger.warning("TTT: Failed to poll commands: %s", e)
            return []

    async def acknowledge_command(self, command_id: str) -> None:
        """Acknowledge receipt of a command."""
        if not self.enabled:
            return
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, self.client.acknowledge_command, command_id
            )
            logger.debug("TTT: Acknowledged command %s", command_id)
        except Exception as e:
            logger.warning("TTT: Failed to acknowledge command: %s", e)

    async def complete_command(self, command_id: str, result: dict) -> None:
        """Report command completion."""
        if not self.enabled:
            return
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, self.client.complete_command, command_id, result
            )
            logger.debug("TTT: Completed command %s", command_id)
        except Exception as e:
            logger.warning("TTT: Failed to complete command: %s", e)

    # ------------------------------------------------------------------
    # Recording pipeline reporting (Phase 2B)
    # ------------------------------------------------------------------

    def _get_team_id_sync(self) -> str | None:
        """Synchronously get team ID from TTT (for use in run_in_executor calls).

        Fetches team assignments on first call and caches the result.
        Returns the first team_id from assignments, or None if unavailable.
        """
        if self._cached_team_id:
            return self._cached_team_id
        try:
            assignments = self.client.get_team_assignments()
            if assignments:
                team_id = assignments[0].get("team_id")
                if team_id:
                    self._cached_team_id = team_id
                    return team_id
        except Exception as e:
            logger.warning("TTT: Failed to fetch team assignments for team_id: %s", e)
        return None

    async def register_recordings(self, files: list) -> list[dict] | None:
        """Register new recording files with TTT.

        Returns list of registered recordings with TTT IDs,
        or None if TTT unreachable. Best-effort — never blocks processing.
        """
        if not self.enabled or not self.camera_id:
            return None
        try:
            file_dicts = []
            for f in files:
                file_path = f.file_path
                file_name = (
                    file_path.name
                    if hasattr(file_path, "name")
                    else str(file_path).split("/")[-1].split("\\")[-1]
                )
                # Extract group directory name from group_dir or file_path parent
                group_dir = getattr(f, "group_dir", None)
                file_group = ""
                if group_dir:
                    import os

                    file_group = os.path.basename(str(group_dir))
                file_dicts.append(
                    {
                        "file_name": file_name,
                        "file_group": file_group,
                        "file_size_bytes": f.metadata.get("file_size")
                        if f.metadata
                        else None,
                        "duration_seconds": None,
                        "recording_start": f.start_time.isoformat()
                        if f.start_time
                        else None,
                        "recording_end": f.end_time.isoformat() if f.end_time else None,
                    }
                )

            team_id = await asyncio.get_event_loop().run_in_executor(
                None, self._get_team_id_sync
            )
            if not team_id:
                logger.debug(
                    "TTT: No team_id available, skipping recording registration"
                )
                return None

            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.client.register_recordings(
                    self.camera_id, team_id, file_dicts
                ),
            )
            if result:
                logger.info("TTT: Registered %d recording(s)", len(result))
            return result
        except Exception as e:
            logger.warning("TTT: Failed to register recordings: %s", e)
            return None

    async def update_recording_status(
        self,
        recording_id: str | None,
        stage: str,
        status: str,
        error: str | None = None,
        youtube_url: str | None = None,
        youtube_video_id: str | None = None,
    ) -> None:
        """Update pipeline stage status for a recording.

        No-op if recording_id is None (TTT wasn't available during registration).
        """
        if not self.enabled or not recording_id:
            return
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.client.update_recording_status(
                    recording_id,
                    stage,
                    status,
                    error,
                    youtube_url,
                    youtube_video_id,
                ),
            )
            logger.debug("TTT: Updated recording %s %s=%s", recording_id, stage, status)
        except Exception as e:
            logger.warning("TTT: Failed to update recording status: %s", e)

    async def get_high_water_mark(self) -> str | None:
        """Get the latest recording timestamp TTT knows about for this camera.

        Supplementary — used for remote visibility, not for local decisions.
        """
        if not self.enabled or not self.camera_id:
            return None
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.client.get_high_water_mark(self.camera_id),
            )
            return result
        except Exception as e:
            logger.warning("TTT: Failed to get high water mark: %s", e)
            return None
