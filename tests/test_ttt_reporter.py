"""Tests for the TTT reporter module."""

import asyncio
import pytest
from unittest.mock import MagicMock, patch

from video_grouper.api_integrations.ttt_reporter import TTTReporter
from video_grouper.utils.error_tracker import ErrorTracker


class TestTTTReporterDisabled:
    """When TTT is not configured, all methods should be no-ops."""

    def setup_method(self):
        self.config = MagicMock()
        self.config.ttt.camera_id = ""
        self.config.ttt.ttt_sync_enabled = False
        self.config.ttt.heartbeat_interval = 30
        self.reporter = TTTReporter(ttt_client=None, config=self.config)

    def test_not_enabled_when_no_client(self):
        assert self.reporter.enabled is False

    @pytest.mark.asyncio
    async def test_start_is_noop(self):
        await self.reporter.start()  # Should not raise

    @pytest.mark.asyncio
    async def test_report_camera_status_is_noop(self):
        await self.reporter.report_camera_status("online")  # Should not raise

    @pytest.mark.asyncio
    async def test_sync_config_returns_none(self):
        result = await self.reporter.sync_config()
        assert result is None

    @pytest.mark.asyncio
    async def test_push_config_is_noop(self):
        await self.reporter.push_config()  # Should not raise

    @pytest.mark.asyncio
    async def test_stop_is_noop(self):
        await self.reporter.stop()  # Should not raise, no task to cancel


class TestTTTReporterEnabled:
    """When TTT is configured, methods should call the client."""

    def setup_method(self):
        # Use regular MagicMock because the reporter calls client methods
        # synchronously via run_in_executor.
        self.client = MagicMock()
        self.config = MagicMock()
        self.config.ttt.camera_id = "test-camera-id"
        self.config.ttt.ttt_sync_enabled = True
        self.config.ttt.heartbeat_interval = 30
        self.config.recording.min_duration = 60
        self.config.recording.max_duration = 7200
        self.config.storage.path = "/tmp/test"
        self.config.storage.min_free_gb = 10
        self.reporter = TTTReporter(ttt_client=self.client, config=self.config)

    def test_enabled_when_client_provided(self):
        assert self.reporter.enabled is True

    def test_camera_id_set_from_config(self):
        assert self.reporter.camera_id == "test-camera-id"

    @pytest.mark.asyncio
    async def test_report_camera_status_calls_client(self):
        await self.reporter.report_camera_status("online")
        self.client.update_camera_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_report_camera_status_passes_correct_args(self):
        await self.reporter.report_camera_status("online")
        call_kwargs = self.client.update_camera_status.call_args
        assert call_kwargs is not None
        # Called with keyword arguments
        kwargs = call_kwargs.kwargs
        assert kwargs["camera_id"] == "test-camera-id"
        assert kwargs["status"] == "online"

    @pytest.mark.asyncio
    async def test_report_camera_status_skips_duplicate(self):
        await self.reporter.report_camera_status("online")
        await self.reporter.report_camera_status("online")
        assert self.client.update_camera_status.call_count == 1

    @pytest.mark.asyncio
    async def test_report_camera_status_reports_changes(self):
        await self.reporter.report_camera_status("online")
        await self.reporter.report_camera_status("recording")
        assert self.client.update_camera_status.call_count == 2

    @pytest.mark.asyncio
    async def test_report_camera_status_error_still_reports_next_different_status(self):
        """After an error, status is not cached — next call with same status goes through."""
        self.client.update_camera_status.side_effect = Exception("network error")
        await self.reporter.report_camera_status(
            "online"
        )  # fails, _last_status stays None
        # _last_status was not set because the call raised; next "online" call goes through
        self.client.update_camera_status.side_effect = None
        await self.reporter.report_camera_status("online")
        assert self.client.update_camera_status.call_count == 2

    @pytest.mark.asyncio
    async def test_sync_config_calls_client(self):
        self.client.get_camera_config.return_value = {"recording_config": {}}
        result = await self.reporter.sync_config()
        self.client.get_camera_config.assert_called_once_with("test-camera-id")
        assert result == {"recording_config": {}}

    @pytest.mark.asyncio
    async def test_sync_config_skipped_when_sync_disabled(self):
        self.config.ttt.ttt_sync_enabled = False
        result = await self.reporter.sync_config()
        self.client.get_camera_config.assert_not_called()
        assert result is None

    @pytest.mark.asyncio
    async def test_push_config_calls_client(self):
        await self.reporter.push_config()
        self.client.push_camera_config.assert_called_once()
        call_args = self.client.push_camera_config.call_args
        assert call_args.args[0] == "test-camera-id"

    @pytest.mark.asyncio
    async def test_push_config_skipped_when_sync_disabled(self):
        self.config.ttt.ttt_sync_enabled = False
        await self.reporter.push_config()
        self.client.push_camera_config.assert_not_called()

    @pytest.mark.asyncio
    async def test_report_status_handles_client_error(self):
        self.client.update_camera_status.side_effect = Exception("network error")
        await self.reporter.report_camera_status("online")  # Should not raise

    @pytest.mark.asyncio
    async def test_sync_config_handles_client_error(self):
        self.client.get_camera_config.side_effect = Exception("network error")
        result = await self.reporter.sync_config()
        assert result is None

    @pytest.mark.asyncio
    async def test_push_config_handles_client_error(self):
        self.client.push_camera_config.side_effect = Exception("network error")
        await self.reporter.push_config()  # Should not raise

    @pytest.mark.asyncio
    async def test_report_heartbeat_calls_client_when_camera_id_set(self):
        """When service_id is set (MagicMock returns truthy), enhanced_heartbeat is used."""
        self.reporter._last_status = "online"
        await self.reporter.report_heartbeat()
        # MagicMock config.ttt.service_id is truthy, so enhanced_heartbeat is called
        self.client.enhanced_heartbeat.assert_called_once()

    @pytest.mark.asyncio
    async def test_report_heartbeat_uses_camera_status_when_no_service_id(self):
        """When service_id is None, falls back to update_camera_status with default 'online'."""
        self.config.ttt.service_id = None
        await self.reporter.report_heartbeat()
        call_kwargs = self.client.update_camera_status.call_args.kwargs
        assert call_kwargs["status"] == "online"

    @pytest.mark.asyncio
    async def test_stop_cancels_heartbeat_task(self):
        # Start a fake heartbeat task that never completes
        async def _forever():
            while True:
                await asyncio.sleep(1000)

        self.reporter._heartbeat_task = asyncio.create_task(_forever())
        await self.reporter.stop()
        assert self.reporter._heartbeat_task.done()


class TestTTTReporterNoCameraId:
    """When TTT is enabled but no camera_id is set, status methods are no-ops."""

    def setup_method(self):
        self.client = MagicMock()
        self.config = MagicMock()
        self.config.ttt.camera_id = ""
        self.config.ttt.ttt_sync_enabled = True
        self.config.ttt.heartbeat_interval = 30
        self.reporter = TTTReporter(ttt_client=self.client, config=self.config)

    def test_camera_id_is_none_when_empty_string(self):
        assert self.reporter.camera_id is None

    @pytest.mark.asyncio
    async def test_report_camera_status_is_noop_without_camera_id(self):
        await self.reporter.report_camera_status("online")
        self.client.update_camera_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_config_is_noop_without_camera_id(self):
        result = await self.reporter.sync_config()
        self.client.get_camera_config.assert_not_called()
        assert result is None

    @pytest.mark.asyncio
    async def test_push_config_is_noop_without_camera_id(self):
        await self.reporter.push_config()
        self.client.push_camera_config.assert_not_called()


class TestRecordingReporter:
    """Tests for recording registration and status reporting (Phase 2B)."""

    def setup_method(self):
        self.client = MagicMock()
        self.config = MagicMock()
        self.config.ttt.camera_id = "test-camera-id"
        self.config.ttt.ttt_sync_enabled = True
        self.config.ttt.heartbeat_interval = 30
        self.reporter = TTTReporter(ttt_client=self.client, config=self.config)

        # Provide a default team_id via get_team_assignments
        self.client.get_team_assignments.return_value = [
            {"team_id": "team-uuid-1", "team_name": "Hawks"}
        ]

    def _make_recording_file(self, filename="game01.dav", start=None, end=None):
        """Create a minimal mock RecordingFile."""
        from datetime import datetime
        from unittest.mock import MagicMock

        f = MagicMock()
        f.file_path = filename
        f.start_time = start or datetime(2026, 3, 1, 10, 0, 0)
        f.end_time = end or datetime(2026, 3, 1, 11, 0, 0)
        f.metadata = {}
        f.group_dir = "/storage/2026.03.01-10.00.00"
        return f

    @pytest.mark.asyncio
    async def test_register_recordings_calls_client(self):
        self.client.register_recordings.return_value = [{"id": "rec-1"}]
        mock_file = self._make_recording_file()
        result = await self.reporter.register_recordings([mock_file])
        self.client.register_recordings.assert_called_once()
        assert result == [{"id": "rec-1"}]

    @pytest.mark.asyncio
    async def test_register_recordings_returns_none_on_client_error(self):
        self.client.register_recordings.side_effect = Exception("network")
        mock_file = self._make_recording_file()
        result = await self.reporter.register_recordings([mock_file])
        assert result is None

    @pytest.mark.asyncio
    async def test_register_recordings_returns_none_when_disabled(self):
        reporter = TTTReporter(ttt_client=None, config=self.config)
        result = await reporter.register_recordings([self._make_recording_file()])
        assert result is None

    @pytest.mark.asyncio
    async def test_register_recordings_returns_none_without_camera_id(self):
        self.config.ttt.camera_id = ""
        reporter = TTTReporter(ttt_client=self.client, config=self.config)
        result = await reporter.register_recordings([self._make_recording_file()])
        assert result is None
        self.client.register_recordings.assert_not_called()

    @pytest.mark.asyncio
    async def test_register_recordings_returns_none_when_no_team_id(self):
        self.client.get_team_assignments.return_value = []
        result = await self.reporter.register_recordings([self._make_recording_file()])
        assert result is None
        self.client.register_recordings.assert_not_called()

    @pytest.mark.asyncio
    async def test_register_recordings_passes_camera_and_team_id(self):
        self.client.register_recordings.return_value = [{"id": "rec-1"}]
        mock_file = self._make_recording_file()
        await self.reporter.register_recordings([mock_file])
        call_args = self.client.register_recordings.call_args
        assert call_args.args[0] == "test-camera-id"
        assert call_args.args[1] == "team-uuid-1"

    @pytest.mark.asyncio
    async def test_update_recording_status_calls_client(self):
        await self.reporter.update_recording_status("rec-1", "download", "downloaded")
        self.client.update_recording_status.assert_called_once_with(
            "rec-1", "download", "downloaded", None, None, None
        )

    @pytest.mark.asyncio
    async def test_update_recording_status_noop_when_no_id(self):
        await self.reporter.update_recording_status(None, "download", "downloaded")
        self.client.update_recording_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_recording_status_noop_when_disabled(self):
        reporter = TTTReporter(ttt_client=None, config=self.config)
        await reporter.update_recording_status("rec-1", "download", "downloaded")
        self.client.update_recording_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_recording_status_handles_error(self):
        self.client.update_recording_status.side_effect = Exception("network")
        # Should not raise
        await self.reporter.update_recording_status("rec-1", "download", "failed")

    @pytest.mark.asyncio
    async def test_update_recording_status_passes_optional_fields(self):
        await self.reporter.update_recording_status(
            "rec-1",
            "upload",
            "complete",
            youtube_url="https://youtu.be/abc",
            youtube_video_id="abc",
        )
        self.client.update_recording_status.assert_called_once_with(
            "rec-1", "upload", "complete", None, "https://youtu.be/abc", "abc"
        )

    @pytest.mark.asyncio
    async def test_get_high_water_mark(self):
        self.client.get_high_water_mark.return_value = "2026-03-01T15:30:00Z"
        result = await self.reporter.get_high_water_mark()
        assert result == "2026-03-01T15:30:00Z"
        self.client.get_high_water_mark.assert_called_once_with("test-camera-id")

    @pytest.mark.asyncio
    async def test_get_high_water_mark_returns_none_when_disabled(self):
        reporter = TTTReporter(ttt_client=None, config=self.config)
        result = await reporter.get_high_water_mark()
        assert result is None

    @pytest.mark.asyncio
    async def test_get_high_water_mark_returns_none_without_camera_id(self):
        self.config.ttt.camera_id = ""
        reporter = TTTReporter(ttt_client=self.client, config=self.config)
        result = await reporter.get_high_water_mark()
        assert result is None
        self.client.get_high_water_mark.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_high_water_mark_handles_error(self):
        self.client.get_high_water_mark.side_effect = Exception("network")
        result = await self.reporter.get_high_water_mark()
        assert result is None

    def test_get_team_id_sync_caches_result(self):
        """_get_team_id_sync caches the team ID after the first call."""
        self.client.get_team_assignments.return_value = [
            {"team_id": "team-uuid-1", "team_name": "Hawks"}
        ]
        result1 = self.reporter._get_team_id_sync()
        result2 = self.reporter._get_team_id_sync()
        assert result1 == "team-uuid-1"
        assert result2 == "team-uuid-1"
        # Should only call the API once due to caching
        assert self.client.get_team_assignments.call_count == 1

    def test_get_team_id_sync_returns_none_on_error(self):
        """_get_team_id_sync returns None when API fails."""
        self.client.get_team_assignments.side_effect = Exception("network")
        result = self.reporter._get_team_id_sync()
        assert result is None

    def test_get_team_id_sync_returns_none_when_empty(self):
        """_get_team_id_sync returns None when no assignments."""
        self.client.get_team_assignments.return_value = []
        result = self.reporter._get_team_id_sync()
        assert result is None


class TestCommandPolling:
    """Tests for command polling and execution (Phase 5B)."""

    def setup_method(self):
        self.client = MagicMock()
        self.config = MagicMock()
        self.config.ttt.camera_id = "test-camera-id"
        self.config.ttt.ttt_sync_enabled = True
        self.config.ttt.heartbeat_interval = 30
        self.reporter = TTTReporter(ttt_client=self.client, config=self.config)

    @pytest.mark.asyncio
    async def test_poll_pending_commands(self):
        self.client.get_pending_commands.return_value = [
            {"id": "cmd-1", "command_type": "restart"}
        ]
        result = await self.reporter.poll_pending_commands()
        assert len(result) == 1
        assert result[0]["id"] == "cmd-1"

    @pytest.mark.asyncio
    async def test_poll_commands_returns_empty_on_error(self):
        self.client.get_pending_commands.side_effect = Exception("network")
        result = await self.reporter.poll_pending_commands()
        assert result == []

    @pytest.mark.asyncio
    async def test_poll_commands_returns_empty_when_disabled(self):
        reporter = TTTReporter(ttt_client=None, config=self.config)
        result = await reporter.poll_pending_commands()
        assert result == []

    @pytest.mark.asyncio
    async def test_poll_commands_returns_empty_without_camera_id(self):
        self.config.ttt.camera_id = ""
        reporter = TTTReporter(ttt_client=self.client, config=self.config)
        result = await reporter.poll_pending_commands()
        assert result == []
        self.client.get_pending_commands.assert_not_called()

    @pytest.mark.asyncio
    async def test_acknowledge_command(self):
        await self.reporter.acknowledge_command("cmd-1")
        self.client.acknowledge_command.assert_called_once_with("cmd-1")

    @pytest.mark.asyncio
    async def test_acknowledge_command_noop_when_disabled(self):
        reporter = TTTReporter(ttt_client=None, config=self.config)
        await reporter.acknowledge_command("cmd-1")
        self.client.acknowledge_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_acknowledge_command_handles_error(self):
        self.client.acknowledge_command.side_effect = Exception("network")
        await self.reporter.acknowledge_command("cmd-1")  # Should not raise

    @pytest.mark.asyncio
    async def test_complete_command(self):
        await self.reporter.complete_command("cmd-1", {"success": True})
        self.client.complete_command.assert_called_once_with("cmd-1", {"success": True})

    @pytest.mark.asyncio
    async def test_complete_command_noop_when_disabled(self):
        reporter = TTTReporter(ttt_client=None, config=self.config)
        await reporter.complete_command("cmd-1", {"success": True})
        self.client.complete_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_complete_command_handles_error(self):
        self.client.complete_command.side_effect = Exception("network")
        await self.reporter.complete_command(
            "cmd-1", {"success": True}
        )  # Should not raise

    @pytest.mark.asyncio
    async def test_pull_auto_record_rules(self):
        self.client.get_auto_record_rules.return_value = {"pre_start_minutes": 15}
        result = await self.reporter.pull_auto_record_rules()
        assert result == {"pre_start_minutes": 15}
        self.client.get_auto_record_rules.assert_called_once_with("test-camera-id")

    @pytest.mark.asyncio
    async def test_pull_auto_record_rules_returns_none_on_error(self):
        self.client.get_auto_record_rules.side_effect = Exception("network")
        result = await self.reporter.pull_auto_record_rules()
        assert result is None

    @pytest.mark.asyncio
    async def test_pull_auto_record_rules_returns_none_when_disabled(self):
        reporter = TTTReporter(ttt_client=None, config=self.config)
        result = await reporter.pull_auto_record_rules()
        assert result is None

    @pytest.mark.asyncio
    async def test_execute_command_calls_executor(self):
        """_execute_command delegates to the command executor."""
        from unittest.mock import AsyncMock

        executor = MagicMock()
        executor.execute = AsyncMock(return_value={"success": True, "message": "ok"})
        reporter = TTTReporter(
            ttt_client=self.client, config=self.config, command_executor=executor
        )
        await reporter._execute_command({"id": "cmd-1", "command_type": "restart"})
        executor.execute.assert_called_once()
        self.client.acknowledge_command.assert_called_once_with("cmd-1")
        self.client.complete_command.assert_called_once_with(
            "cmd-1", {"success": True, "message": "ok"}
        )

    @pytest.mark.asyncio
    async def test_execute_command_skips_when_no_id(self):
        """_execute_command is a no-op when command has no id."""
        from unittest.mock import AsyncMock

        executor = MagicMock()
        executor.execute = AsyncMock()
        reporter = TTTReporter(
            ttt_client=self.client, config=self.config, command_executor=executor
        )
        await reporter._execute_command({"command_type": "restart"})  # no id
        executor.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_command_without_executor_reports_failure(self):
        """When no executor is set, complete_command is called with failure result."""
        reporter = TTTReporter(ttt_client=self.client, config=self.config)
        await reporter._execute_command({"id": "cmd-1", "command_type": "restart"})
        self.client.acknowledge_command.assert_called_once_with("cmd-1")
        call_args = self.client.complete_command.call_args
        assert call_args.args[0] == "cmd-1"
        assert call_args.args[1]["success"] is False


class TestEnhancedHeartbeat:
    """Tests for system metrics in heartbeat."""

    def setup_method(self):
        self.client = MagicMock()
        self.config = MagicMock()
        self.config.ttt.camera_id = "test-camera-id"
        self.config.ttt.service_id = "test-service-id"
        self.config.ttt.ttt_sync_enabled = True
        self.config.ttt.heartbeat_interval = 30
        self.error_tracker = ErrorTracker()
        self.reporter = TTTReporter(
            ttt_client=self.client, config=self.config, error_tracker=self.error_tracker
        )

    @pytest.mark.asyncio
    async def test_heartbeat_sends_metrics(self):
        with patch(
            "video_grouper.api_integrations.ttt_reporter.get_system_metrics",
            return_value={"cpu_usage_percent": 25.0, "disk_free_gb": 100.0},
        ):
            await self.reporter.report_heartbeat()
            self.client.enhanced_heartbeat.assert_called_once()
            call_args = self.client.enhanced_heartbeat.call_args
            assert "cpu_usage_percent" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_heartbeat_includes_error_info(self):
        self.error_tracker.record("download", "timeout")
        with patch(
            "video_grouper.api_integrations.ttt_reporter.get_system_metrics",
            return_value={},
        ):
            await self.reporter.report_heartbeat()
            call_args = self.client.enhanced_heartbeat.call_args
            metrics = call_args[0][1]
            assert metrics["error_count_24h"] == 1
            assert "download" in metrics["last_error"]

    @pytest.mark.asyncio
    async def test_heartbeat_includes_no_error_info_when_none(self):
        with patch(
            "video_grouper.api_integrations.ttt_reporter.get_system_metrics",
            return_value={},
        ):
            await self.reporter.report_heartbeat()
            call_args = self.client.enhanced_heartbeat.call_args
            metrics = call_args[0][1]
            assert metrics["error_count_24h"] == 0
            assert metrics["last_error"] is None

    @pytest.mark.asyncio
    async def test_heartbeat_merges_external_metrics(self):
        with patch(
            "video_grouper.api_integrations.ttt_reporter.get_system_metrics",
            return_value={"cpu_usage_percent": 10.0},
        ):
            await self.reporter.report_heartbeat(
                metrics={"queue_depth": 5, "version": "1.2.3"}
            )
            call_args = self.client.enhanced_heartbeat.call_args
            metrics = call_args[0][1]
            assert metrics["cpu_usage_percent"] == 10.0
            assert metrics["queue_depth"] == 5
            assert metrics["version"] == "1.2.3"

    @pytest.mark.asyncio
    async def test_heartbeat_falls_back_to_camera_status_when_no_service_id(self):
        """When service_id is not set, falls back to camera status update."""
        # service_id not set — getattr returns None via MagicMock spec
        self.config.ttt.service_id = None
        with patch(
            "video_grouper.api_integrations.ttt_reporter.get_system_metrics",
            return_value={},
        ):
            await self.reporter.report_heartbeat()
            self.client.enhanced_heartbeat.assert_not_called()
            self.client.update_camera_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_error_tracker_default_created_when_none_passed(self):
        """Reporter creates its own ErrorTracker when none is provided."""
        reporter = TTTReporter(ttt_client=self.client, config=self.config)
        assert reporter.error_tracker is not None
        assert reporter.error_tracker.get_error_count_24h() == 0

    @pytest.mark.asyncio
    async def test_heartbeat_fails_silently_on_client_error(self):
        self.client.enhanced_heartbeat.side_effect = Exception("network error")
        with patch(
            "video_grouper.api_integrations.ttt_reporter.get_system_metrics",
            return_value={},
        ):
            await self.reporter.report_heartbeat()  # Should not raise
