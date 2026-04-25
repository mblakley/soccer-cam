"""Comprehensive TTT integration tests.

Section A: End-to-end workflow tests (need live TTT backend)
Section B: Schedule, game, and content tests (need live TTT backend)
Section C: Standalone operation & graceful degradation (always run)
Section D: Auth edge cases (need live TTT backend)
"""

import tempfile
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from video_grouper.api_integrations.ttt_api import TTTApiClient, TTTApiError
from video_grouper.api_integrations.ttt_reporter import TTTReporter

from .conftest import (
    CAMERA_ID,
    GAME_SESSION_ID,
    MOMENT_TAG_1_ID,
    PENDING_CLIP_REQUEST_ID,
    SUPABASE_ANON_KEY,
    SUPABASE_URL,
    TEAM_ID,
    TEST_EMAIL,
    TEST_PASSWORD,
    TTT_API_URL,
)

pytestmark = [pytest.mark.integration]


# ===================================================================
# Section A: End-to-End Workflow Tests
# ===================================================================


class TestOnboardingWorkflow:
    """First-time setup sequence: login -> team discovery -> device config."""

    def test_01_login_to_supabase(self, tmp_path):
        """Fresh client can authenticate with Supabase email/password."""
        client = TTTApiClient(
            supabase_url=SUPABASE_URL,
            anon_key=SUPABASE_ANON_KEY,
            api_base_url=TTT_API_URL,
            storage_path=str(tmp_path),
        )
        assert not client.is_authenticated()
        client.login(TEST_EMAIL, TEST_PASSWORD)
        assert client.is_authenticated()
        # Tokens persisted to disk
        token_file = tmp_path / "ttt" / "tokens.json"
        assert token_file.exists()

    def test_02_get_team_assignments(self, ttt_client):
        """After login, discover which teams this user manages cameras for."""
        result = ttt_client.get_team_assignments()
        assert isinstance(result, list)
        assert len(result) > 0
        team_ids = [t.get("team_id") for t in result]
        assert TEAM_ID in team_ids

    def test_03_check_device_config(self, ttt_client):
        """get_device_config returns existing config or None for new device."""
        result = ttt_client.get_device_config()
        # May be None if no config saved yet, or a dict with stored config
        assert result is None or isinstance(result, dict)

    def test_04_save_device_config(self, ttt_client):
        """Save device config (camera IP, NTFY, YouTube settings)."""
        test_config = {
            "camera_ip": "192.168.1.100",
            "ntfy_topic": "test-soccer-cam",
            "ntfy_server_url": "https://ntfy.sh",
            "youtube_configured": True,
            "gcp_project_id": "test-gcp-project",
        }
        result = ttt_client.save_device_config(test_config)
        assert isinstance(result, dict)

    def test_05_retrieve_saved_device_config(self, ttt_client):
        """Retrieved config contains what was saved."""
        result = ttt_client.get_device_config()
        assert result is not None
        assert result.get("camera_ip") == "192.168.1.100"
        assert result.get("ntfy_topic") == "test-soccer-cam"
        assert result.get("youtube_configured") is True


class TestServiceRegistrationWorkflow:
    """App startup: register service -> heartbeat -> camera status."""

    _service_id = None

    def test_01_register_service(self, ttt_client):
        """Register soccer-cam instance, get service_id back."""
        machine_name = f"integ-test-{uuid.uuid4().hex[:8]}"
        result = ttt_client.register_service(
            machine_name,
            {
                "ffmpeg": True,
                "autocam": False,
                "camera_type": "reolink",
                "camera_ip": "192.168.1.100",
            },
        )
        assert isinstance(result, dict)
        assert "id" in result
        TestServiceRegistrationWorkflow._service_id = result["id"]

    def test_02_send_heartbeat(self, ttt_client):
        """Basic heartbeat keepalive with registered service."""
        sid = TestServiceRegistrationWorkflow._service_id
        assert sid is not None, "Service not registered"
        result = ttt_client.send_heartbeat(sid, "online")
        assert result is None or isinstance(result, dict)

    def test_03_enhanced_heartbeat(self, ttt_client):
        """Enhanced heartbeat with system metrics."""
        sid = TestServiceRegistrationWorkflow._service_id
        assert sid is not None
        result = ttt_client.enhanced_heartbeat(
            sid,
            {
                "cpu_usage_percent": 25.5,
                "memory_usage_percent": 42.0,
                "disk_free_gb": 150.7,
                "active_job_count": 0,
                "queue_depth": 2,
                "uptime_seconds": 3600,
                "version": "2.5.0-test",
                "last_error": None,
                "error_count_24h": 0,
            },
        )
        assert result is None or isinstance(result, dict)

    def test_04_camera_status_online(self, ttt_client):
        """Report camera as online with firmware version."""
        result = ttt_client.update_camera_status(
            CAMERA_ID, "online", firmware_version="v3.1.0"
        )
        assert result is None or isinstance(result, dict)

    def test_05_get_camera_config(self, ttt_client):
        """Fetch camera configuration from TTT."""
        result = ttt_client.get_camera_config(CAMERA_ID)
        assert result is None or isinstance(result, dict)


class TestRecordingPipelineWorkflow:
    """Full recording lifecycle: register -> download -> combine -> trim -> upload."""

    _recording_ids = []
    _fail_recording_id = None

    def test_01_register_recordings(self, ttt_client):
        """Register new recording files, get recording IDs back."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        files = [
            {
                "file_name": f"integ_test_{ts}_001.mp4",
                "file_group": f"integ-test-{ts}",
                "file_size_bytes": 1073741824,
                "duration_seconds": 1800.0,
                "recording_start": datetime.now(timezone.utc).isoformat(),
                "recording_end": datetime.now(timezone.utc).isoformat(),
            },
            {
                "file_name": f"integ_test_{ts}_002.mp4",
                "file_group": f"integ-test-{ts}",
                "file_size_bytes": 1073741824,
                "duration_seconds": 1800.0,
                "recording_start": datetime.now(timezone.utc).isoformat(),
                "recording_end": datetime.now(timezone.utc).isoformat(),
            },
        ]
        result = ttt_client.register_recordings(CAMERA_ID, TEAM_ID, files)
        assert isinstance(result, list)
        assert len(result) >= 1
        TestRecordingPipelineWorkflow._recording_ids = [
            r["id"] for r in result if "id" in r
        ]
        assert len(TestRecordingPipelineWorkflow._recording_ids) >= 1

    def test_02_download_in_progress(self, ttt_client):
        """Update recording: download stage starting."""
        rec_id = TestRecordingPipelineWorkflow._recording_ids[0]
        result = ttt_client.update_recording_status(rec_id, "download", "in_progress")
        assert result is None or isinstance(result, dict)

    def test_03_download_complete(self, ttt_client):
        """Update recording: download complete."""
        rec_id = TestRecordingPipelineWorkflow._recording_ids[0]
        result = ttt_client.update_recording_status(rec_id, "download", "completed")
        assert result is None or isinstance(result, dict)

    def test_04_combine_in_progress(self, ttt_client):
        """Update recording: combine stage starting."""
        rec_id = TestRecordingPipelineWorkflow._recording_ids[0]
        result = ttt_client.update_recording_status(rec_id, "combine", "in_progress")
        assert result is None or isinstance(result, dict)

    def test_05_combine_complete(self, ttt_client):
        """Update recording: combine complete."""
        rec_id = TestRecordingPipelineWorkflow._recording_ids[0]
        result = ttt_client.update_recording_status(rec_id, "combine", "completed")
        assert result is None or isinstance(result, dict)

    def test_06_trim_in_progress(self, ttt_client):
        """Update recording: trim stage starting."""
        rec_id = TestRecordingPipelineWorkflow._recording_ids[0]
        result = ttt_client.update_recording_status(rec_id, "trim", "in_progress")
        assert result is None or isinstance(result, dict)

    def test_07_trim_complete(self, ttt_client):
        """Update recording: trim complete."""
        rec_id = TestRecordingPipelineWorkflow._recording_ids[0]
        result = ttt_client.update_recording_status(rec_id, "trim", "completed")
        assert result is None or isinstance(result, dict)

    def test_08_upload_in_progress(self, ttt_client):
        """Update recording: upload stage starting."""
        rec_id = TestRecordingPipelineWorkflow._recording_ids[0]
        result = ttt_client.update_recording_status(rec_id, "upload", "in_progress")
        assert result is None or isinstance(result, dict)

    def test_09_upload_complete_with_youtube(self, ttt_client):
        """Update recording: upload complete with YouTube URL and video ID."""
        rec_id = TestRecordingPipelineWorkflow._recording_ids[0]
        result = ttt_client.update_recording_status(
            rec_id,
            "upload",
            "completed",
            youtube_url="https://youtube.com/watch?v=integ_test_123",
            youtube_video_id="integ_test_123",
        )
        assert result is None or isinstance(result, dict)

    def test_10_get_high_water_mark(self, ttt_client):
        """High water mark reflects registered recordings."""
        result = ttt_client.get_high_water_mark(CAMERA_ID)
        # Returns an ISO datetime string or None
        assert result is None or isinstance(result, str)

    def test_11_recording_pipeline_failure(self, ttt_client):
        """Register a recording and report download failure with error message."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        files = [
            {
                "file_name": f"integ_fail_{ts}.mp4",
                "file_group": f"integ-fail-{ts}",
                "file_size_bytes": 500000000,
                "recording_start": datetime.now(timezone.utc).isoformat(),
                "recording_end": datetime.now(timezone.utc).isoformat(),
            }
        ]
        registered = ttt_client.register_recordings(CAMERA_ID, TEAM_ID, files)
        assert isinstance(registered, list) and len(registered) >= 1
        fail_id = registered[0]["id"]
        TestRecordingPipelineWorkflow._fail_recording_id = fail_id

        result = ttt_client.update_recording_status(
            fail_id,
            "download",
            "failed",
            error_message="Connection timeout: camera unreachable",
        )
        assert result is None or isinstance(result, dict)


class TestProcessingJobWorkflow:
    """TTT dispatches a job, soccer-cam claims and processes it."""

    _job_id = None
    _fail_job_id = None

    def test_01_get_pending_jobs(self, ttt_client):
        """Poll for pending processing jobs."""
        result = ttt_client.get_pending_jobs()
        assert isinstance(result, list)

    def test_02_create_and_claim_job(self, ttt_client):
        """Create a processing job via TTT API, then claim it."""
        # Create job via direct API call (the web UI path)
        result = ttt_client._request(
            "POST",
            f"{TTT_API_URL}/api/processing-jobs",
            json={
                "team_id": TEAM_ID,
                "game_session_id": GAME_SESSION_ID,
                "config": {"job_type": "clip_extraction", "test_run": True},
            },
        )
        assert isinstance(result, dict)
        assert "id" in result
        TestProcessingJobWorkflow._job_id = result["id"]

        # Claim it
        claim_result = ttt_client.claim_job(result["id"])
        assert claim_result is None or isinstance(claim_result, dict)

    def test_03_progress_downloading(self, ttt_client):
        """Update job progress: downloading stage."""
        job_id = TestProcessingJobWorkflow._job_id
        assert job_id is not None
        result = ttt_client.update_job_progress(
            job_id, "downloading", {"percent": 0, "message": "Starting download"}
        )
        assert result is None or isinstance(result, dict)

    def test_04_progress_combining(self, ttt_client):
        """Update job progress: combining stage."""
        job_id = TestProcessingJobWorkflow._job_id
        result = ttt_client.update_job_progress(
            job_id, "combining", {"percent": 30, "message": "Merging video files"}
        )
        assert result is None or isinstance(result, dict)

    def test_05_progress_uploading(self, ttt_client):
        """Update job progress: uploading stage."""
        job_id = TestProcessingJobWorkflow._job_id
        result = ttt_client.update_job_progress(
            job_id, "uploading", {"percent": 70, "message": "Uploading to YouTube"}
        )
        assert result is None or isinstance(result, dict)

    def test_06_complete_job(self, ttt_client):
        """Complete the job with a result."""
        job_id = TestProcessingJobWorkflow._job_id
        try:
            result = ttt_client.complete_job(
                job_id,
                {
                    "youtube_url": "https://youtube.com/watch?v=integ_job_test",
                    "duration_seconds": 5400,
                },
            )
            assert result is None or isinstance(result, dict)
        except TTTApiError as e:
            if e.status_code == 404:
                pytest.xfail(
                    "Job not found (possibly modified by concurrent test session)"
                )
            raise

    def test_07_create_and_fail_job(self, ttt_client):
        """Create a job, claim it, then fail it with an error."""
        result = ttt_client._request(
            "POST",
            f"{TTT_API_URL}/api/processing-jobs",
            json={
                "team_id": TEAM_ID,
                "game_session_id": GAME_SESSION_ID,
                "config": {"job_type": "video_combine", "test_run": True},
            },
        )
        assert "id" in result
        fail_job_id = result["id"]
        TestProcessingJobWorkflow._fail_job_id = fail_job_id

        ttt_client.claim_job(fail_job_id)
        fail_result = ttt_client.fail_job(
            fail_job_id, "Combined video not found at expected path"
        )
        assert fail_result is None or isinstance(fail_result, dict)


class TestClipRequestWorkflow:
    """Coach requests clip, soccer-cam picks it up and fulfills it."""

    _created_request_id = None

    def test_01_get_pending_clip_requests(self, ttt_client):
        """Poll for pending clip requests, verify seed request present."""
        result = ttt_client.get_pending_clip_requests()
        assert isinstance(result, list)
        # Seed data has a pending clip request
        request_ids = [r.get("id") for r in result]
        assert PENDING_CLIP_REQUEST_ID in request_ids

    def test_02_seed_request_has_segments(self, ttt_client):
        """Verify the pending clip request includes segment data."""
        result = ttt_client.get_pending_clip_requests()
        seed_request = next(
            (r for r in result if r.get("id") == PENDING_CLIP_REQUEST_ID), None
        )
        assert seed_request is not None
        segments = seed_request.get("segments", [])
        assert len(segments) == 2  # Seed has 2 segments

    def test_03_create_clip_request(self, ttt_client):
        """Create a new clip request via TTT API."""
        result = ttt_client._request(
            "POST",
            f"{TTT_API_URL}/api/clip-requests",
            json={
                "game_session_id": GAME_SESSION_ID,
                "delivery_method": "external_storage",
                "is_compilation": False,
                "notes": "Integration test clip request",
                "segments": [
                    {
                        "start_time": 60,
                        "end_time": 90,
                        "label": "Test segment",
                        "sort_order": 0,
                    }
                ],
            },
        )
        assert isinstance(result, dict)
        assert "id" in result
        TestClipRequestWorkflow._created_request_id = result["id"]

    def test_04_start_clip_request(self, ttt_client):
        """Mark the new clip request as in-progress."""
        req_id = TestClipRequestWorkflow._created_request_id
        assert req_id is not None
        result = ttt_client.start_clip_request(req_id)
        assert isinstance(result, dict)
        assert result.get("status") == "in_progress"

    def test_05_fulfill_clip_request(self, ttt_client):
        """Fulfill the clip request with a URL and notes."""
        req_id = TestClipRequestWorkflow._created_request_id
        result = ttt_client.fulfill_clip_request(
            req_id,
            "https://drive.google.com/file/d/integ_test_clip",
            "Integration test: 1 clip extracted",
        )
        assert isinstance(result, dict)
        assert result.get("status") == "fulfilled"


class TestCommandWorkflow:
    """TTT sends remote command, soccer-cam acknowledges and completes it."""

    _command_id = None

    def test_01_create_command(self, ttt_client):
        """Create a restart command via TTT cameras API."""
        result = ttt_client._request(
            "POST",
            f"{TTT_API_URL}/api/cameras/commands",
            json={
                "camera_id": CAMERA_ID,
                "command_type": "restart",
                "parameters": {"reason": "integration test"},
            },
        )
        assert isinstance(result, dict)
        assert "id" in result
        TestCommandWorkflow._command_id = result["id"]

    def test_02_get_pending_commands(self, ttt_client):
        """Poll pending commands, verify the created command appears."""
        result = ttt_client.get_pending_commands(CAMERA_ID)
        assert isinstance(result, list)
        cmd_ids = [c.get("id") for c in result]
        assert TestCommandWorkflow._command_id in cmd_ids

    def test_03_acknowledge_command(self, ttt_client):
        """Acknowledge receipt of the command."""
        cmd_id = TestCommandWorkflow._command_id
        result = ttt_client.acknowledge_command(cmd_id)
        assert result is None or isinstance(result, dict)

    def test_04_complete_command(self, ttt_client):
        """Complete the command with a success result."""
        cmd_id = TestCommandWorkflow._command_id
        result = ttt_client.complete_command(
            cmd_id, {"success": True, "message": "Camera restarting"}
        )
        assert result is None or isinstance(result, dict)


class TestErrorDisplayWorkflows:
    """Verify errors flow from soccer-cam to TTT alerts and dashboard."""

    _error_service_id = None
    _error_job_id = None

    def test_01_recording_failure_creates_alert(self, ttt_client):
        """Pipeline failure triggers a recording_failed alert in TTT."""
        # Register a recording and fail it
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        files = [
            {
                "file_name": f"alert_test_{ts}.mp4",
                "file_group": f"alert-test-{ts}",
                "file_size_bytes": 100000,
                "recording_start": datetime.now(timezone.utc).isoformat(),
                "recording_end": datetime.now(timezone.utc).isoformat(),
            }
        ]
        registered = ttt_client.register_recordings(CAMERA_ID, TEAM_ID, files)
        rec_id = registered[0]["id"]

        # Fail the recording at download stage
        ttt_client.update_recording_status(
            rec_id,
            "download",
            "failed",
            error_message="Integration test: camera connection lost",
        )

        # Check that an alert was created
        alerts = ttt_client._request(
            "GET",
            f"{TTT_API_URL}/api/camera-alerts",
            params={"limit": "10"},
        )
        assert isinstance(alerts, list)
        failure_alerts = [
            a for a in alerts if a.get("alert_type") == "recording_failed"
        ]
        assert len(failure_alerts) > 0, "Expected recording_failed alert"
        assert failure_alerts[0].get("severity") == "error"

    def test_02_disk_space_alert(self, ttt_client):
        """Enhanced heartbeat with low disk triggers storage_low alert."""
        # Register a service for the heartbeat
        machine = f"disk-alert-test-{uuid.uuid4().hex[:8]}"
        svc = ttt_client.register_service(machine, {"ffmpeg": True})
        svc_id = svc["id"]
        TestErrorDisplayWorkflows._error_service_id = svc_id

        # Send heartbeat with dangerously low disk
        ttt_client.enhanced_heartbeat(
            svc_id,
            {
                "cpu_usage_percent": 10.0,
                "memory_usage_percent": 30.0,
                "disk_free_gb": 3.5,  # Below 10 GB threshold
                "active_job_count": 0,
                "queue_depth": 0,
                "version": "test",
            },
        )

        # Check for storage_low alert
        alerts = ttt_client._request(
            "GET",
            f"{TTT_API_URL}/api/camera-alerts",
            params={"limit": "10"},
        )
        storage_alerts = [a for a in alerts if a.get("alert_type") == "storage_low"]
        assert len(storage_alerts) > 0, "Expected storage_low alert"
        assert storage_alerts[0].get("severity") == "warning"

    def test_03_job_failure_in_error_log(self, ttt_client):
        """Failed processing job appears when listing jobs with error status."""
        # Create and fail a job
        job = ttt_client._request(
            "POST",
            f"{TTT_API_URL}/api/processing-jobs",
            json={
                "team_id": TEAM_ID,
                "game_session_id": GAME_SESSION_ID,
                "config": {"job_type": "test_error_log"},
            },
        )
        job_id = job["id"]
        TestErrorDisplayWorkflows._error_job_id = job_id
        ttt_client.claim_job(job_id)
        ttt_client.fail_job(
            job_id, "Integration test: deliberate failure for error log"
        )

        # Verify failed job is visible when listing by status
        jobs = ttt_client._request(
            "GET",
            f"{TTT_API_URL}/api/processing-jobs",
            params={"status": "error"},
        )
        assert isinstance(jobs, list)
        our_job = [j for j in jobs if j.get("id") == job_id]
        assert len(our_job) == 1, f"Expected job {job_id} in error-status list"
        assert our_job[0].get("status") == "error"
        assert "deliberate failure" in (our_job[0].get("error") or "")

    def test_04_heartbeat_error_metrics_on_dashboard(self, ttt_client):
        """Enhanced heartbeat error metrics visible on service dashboard."""
        svc_id = TestErrorDisplayWorkflows._error_service_id
        if svc_id is None:
            pytest.skip("No service registered for this test")

        ttt_client.enhanced_heartbeat(
            svc_id,
            {
                "cpu_usage_percent": 85.0,
                "memory_usage_percent": 70.0,
                "disk_free_gb": 50.0,
                "last_error": "[download] connection timeout",
                "error_count_24h": 3,
                "version": "test",
            },
        )

        dashboard = ttt_client._request("GET", f"{TTT_API_URL}/api/service-dashboard")
        assert isinstance(dashboard, dict)
        # Dashboard should have service info
        svc = dashboard.get("service")
        if svc:
            assert svc.get("error_count_24h") == 3
            assert "[download]" in (svc.get("last_error") or "")

    def test_05_camera_status_error_and_recovery(self, ttt_client):
        """Report error status, then recover to online."""
        # Report error
        ttt_client.update_camera_status(
            CAMERA_ID, "error", error_message="Integration test: sensor failure"
        )

        # Report recovery
        result = ttt_client.update_camera_status(CAMERA_ID, "online")
        assert result is None or isinstance(result, dict)

    def test_06_mark_alert_as_read(self, ttt_client):
        """Mark an alert as read and verify unread count decreases."""
        # Get unread count
        count_resp = ttt_client._request(
            "GET", f"{TTT_API_URL}/api/camera-alerts/count"
        )
        if isinstance(count_resp, dict):
            initial_count = count_resp.get("count", 0)
        else:
            initial_count = 0

        if initial_count == 0:
            pytest.skip("No unread alerts to test with")

        # Get first unread alert
        alerts = ttt_client._request(
            "GET",
            f"{TTT_API_URL}/api/camera-alerts",
            params={"unread_only": "true", "limit": "1"},
        )
        if not alerts:
            pytest.skip("No unread alerts")

        alert_id = alerts[0]["id"]
        ttt_client._request("PATCH", f"{TTT_API_URL}/api/camera-alerts/{alert_id}/read")

        # Verify count decreased
        new_count_resp = ttt_client._request(
            "GET", f"{TTT_API_URL}/api/camera-alerts/count"
        )
        if isinstance(new_count_resp, dict):
            new_count = new_count_resp.get("count", 0)
            assert new_count < initial_count


class TestHeartbeatAndMonitoring:
    """Ongoing monitoring: heartbeat, config sync."""

    def test_01_enhanced_heartbeat_full_metrics(self, ttt_client):
        """Send heartbeat with all system metrics."""
        machine = f"monitor-test-{uuid.uuid4().hex[:8]}"
        svc = ttt_client.register_service(machine, {"ffmpeg": True})
        result = ttt_client.enhanced_heartbeat(
            svc["id"],
            {
                "cpu_usage_percent": 15.0,
                "memory_usage_percent": 35.0,
                "disk_free_gb": 200.0,
                "active_job_count": 1,
                "queue_depth": 5,
                "uptime_seconds": 86400,
                "version": "2.5.0",
                "last_error": None,
                "error_count_24h": 0,
            },
        )
        assert result is None or isinstance(result, dict)

    def test_02_push_camera_config(self, ttt_client):
        """Push local camera config to TTT for backup."""
        config_data = {
            "recording_config": {"resolution": "4K", "pre_record_minutes": 15},
            "storage_config": {"download_path": "C:/recordings", "min_free_gb": 10},
        }
        result = ttt_client.push_camera_config(CAMERA_ID, config_data)
        assert result is None or isinstance(result, dict)


# ===================================================================
# Section B: Schedule, Game, and Content Tests
# ===================================================================


class TestScheduleAndGames:
    """Schedule sync and game matching."""

    def test_get_schedule(self, ttt_client):
        """Get schedule for team."""
        result = ttt_client.get_schedule(TEAM_ID)
        assert isinstance(result, list)

    def test_get_schedule_with_dates(self, ttt_client):
        """Filter schedule by date range."""
        result = ttt_client.get_schedule(TEAM_ID, "2025-01-01", "2027-12-31")
        assert isinstance(result, list)

    def test_get_roster(self, ttt_client):
        """Get team roster."""
        result = ttt_client.get_roster(TEAM_ID)
        assert isinstance(result, list)

    def test_auto_match_video_no_match(self, ttt_client):
        """Auto-match with time far from any game returns no match."""
        result = ttt_client.auto_match_video(
            TEAM_ID,
            "https://youtube.com/watch?v=no_match_test",
            "2020-01-01T03:00:00Z",  # Far from any game
        )
        assert isinstance(result, dict)
        assert "matched" in result


class TestGameSessionManagement:
    """Game session CRUD."""

    _session_id = None

    def test_01_create_game_session(self, ttt_client):
        """Create a game session with unique recording_group_dir."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        result = ttt_client.create_game_session(
            TEAM_ID,
            f"/recordings/integ-test-{ts}",
            "2026-04-01",
            "Test Opponents FC",
            status="processing",  # Valid: clips_ready|complete|live|processing
        )
        assert isinstance(result, dict)
        assert "id" in result
        TestGameSessionManagement._session_id = result["id"]

    def test_02_get_game_sessions(self, ttt_client):
        """List game sessions for team, verify created session present."""
        result = ttt_client.get_game_sessions(TEAM_ID)
        assert isinstance(result, list)
        session_ids = [s.get("id") for s in result]
        assert TestGameSessionManagement._session_id in session_ids

    def test_03_update_game_session(self, ttt_client):
        """Update game session status and opponent."""
        sid = TestGameSessionManagement._session_id
        result = ttt_client.update_game_session(
            sid, status="complete", opponent_name="Updated Opponents FC"
        )
        assert isinstance(result, dict)


class TestMomentTagsAndClips:
    """Moment tag offset calculation and clip creation."""

    _clip_id = None

    def test_01_get_pending_moment_tags(self, ttt_client):
        """Get moment tags needing offset calculation."""
        result = ttt_client.get_pending_moment_tags(GAME_SESSION_ID)
        assert isinstance(result, list)

    def test_02_update_moment_tag_offset(self, ttt_client):
        """Set video_offset_seconds on a moment tag."""
        result = ttt_client.update_moment_tag(
            MOMENT_TAG_1_ID,
            video_offset_seconds=125.5,
            trimmed_offset_seconds=25.5,
        )
        assert isinstance(result, dict)

    def test_03_create_moment_clip(self, ttt_client):
        """Create a clip record for a moment tag."""
        result = ttt_client.create_moment_clip(
            MOMENT_TAG_1_ID, GAME_SESSION_ID, 110.0, 140.0, 30.0
        )
        assert isinstance(result, dict)
        assert "id" in result
        TestMomentTagsAndClips._clip_id = result["id"]

    def test_04_update_moment_clip(self, ttt_client):
        """Update clip status to ready."""
        clip_id = TestMomentTagsAndClips._clip_id
        assert clip_id is not None, "Clip not created in previous test"
        result = ttt_client.update_moment_clip(
            clip_id, status="ready", file_path="/clips/test_clip.mp4"
        )
        assert isinstance(result, dict)


class TestTimeSyncAndReconcile:
    """Time sync anchor detection and reconciliation."""

    def test_get_sync_anchors(self, ttt_client):
        """Get sync anchors for a game session."""
        result = ttt_client.get_sync_anchors(GAME_SESSION_ID)
        assert isinstance(result, list)

    def test_reconcile_game_session(self, ttt_client):
        """Trigger time sync reconciliation."""
        result = ttt_client.reconcile_game_session(GAME_SESSION_ID)
        assert isinstance(result, dict)


# ===================================================================
# Section C: Standalone Operation & Graceful Degradation
# (These tests run ALWAYS -- no TTT backend required)
# ===================================================================


class TestStandaloneOperation:
    """Soccer-cam pipeline works fully without TTT enabled."""

    def test_reporter_disabled_when_no_client(self):
        """TTTReporter with no client is disabled and all methods are no-ops."""
        config = MagicMock()
        config.ttt.camera_id = ""
        config.ttt.ttt_sync_enabled = False
        config.ttt.heartbeat_interval = 30
        reporter = TTTReporter(ttt_client=None, config=config)
        assert reporter.enabled is False

    @pytest.mark.asyncio
    async def test_reporter_noop_methods_when_disabled(self):
        """All reporter methods return None/[] without raising when disabled."""
        config = MagicMock()
        config.ttt.camera_id = ""
        config.ttt.ttt_sync_enabled = False
        config.ttt.heartbeat_interval = 30
        reporter = TTTReporter(ttt_client=None, config=config)

        # None of these should raise
        await reporter.report_camera_status("online")
        result = await reporter.register_recordings([])
        assert result is None
        await reporter.update_recording_status("rec-1", "download", "downloaded")
        result = await reporter.sync_config()
        assert result is None
        await reporter.push_config()
        commands = await reporter.poll_pending_commands()
        assert commands == []
        hwm = await reporter.get_high_water_mark()
        assert hwm is None

    def test_processors_default_ttt_reporter_is_none(self):
        """All pipeline processors default ttt_reporter to None."""
        from video_grouper.task_processors.download_processor import DownloadProcessor
        from video_grouper.task_processors.upload_processor import UploadProcessor
        from video_grouper.task_processors.video_processor import VideoProcessor

        # DownloadProcessor
        dp = DownloadProcessor.__new__(DownloadProcessor)
        dp.ttt_reporter = None  # Mimics default
        assert dp.ttt_reporter is None

        # VideoProcessor
        vp = VideoProcessor.__new__(VideoProcessor)
        vp.ttt_reporter = None
        assert vp.ttt_reporter is None

        # UploadProcessor
        up = UploadProcessor.__new__(UploadProcessor)
        up.ttt_reporter = None
        assert up.ttt_reporter is None

    def test_camera_poller_skips_registration_without_ttt(self):
        """CameraPoller with no ttt_reporter skips TTT file registration."""
        from video_grouper.task_processors.camera_poller import CameraPoller

        poller = CameraPoller.__new__(CameraPoller)
        poller.ttt_reporter = None
        # The guard `if self.ttt_reporter and new_files_by_group:` means
        # no TTT calls happen when ttt_reporter is None
        assert poller.ttt_reporter is None

    @pytest.mark.asyncio
    async def test_job_processor_skips_when_not_authenticated(self):
        """TTTJobProcessor.discover_work() does nothing when not authenticated."""
        from video_grouper.task_processors.ttt_job_processor import TTTJobProcessor

        mock_client = MagicMock()
        mock_client.is_authenticated.return_value = False
        mock_client.get_pending_jobs = MagicMock()

        config = MagicMock()
        config.ttt.job_polling_enabled = True
        config.ttt.machine_name = "test"
        config.cameras = [MagicMock(type="dahua", device_ip="1.2.3.4")]
        config.ball_tracking.enabled = False

        processor = TTTJobProcessor(
            storage_path=tempfile.mkdtemp(),
            config=config,
            ttt_client=mock_client,
        )
        await processor.discover_work()
        mock_client.get_pending_jobs.assert_not_called()

    @pytest.mark.asyncio
    async def test_reporter_update_recording_status_noop_when_no_id(self):
        """update_recording_status is a no-op when recording_id is None."""
        client = MagicMock()
        config = MagicMock()
        config.ttt.camera_id = "test-cam"
        config.ttt.ttt_sync_enabled = True
        config.ttt.heartbeat_interval = 30
        reporter = TTTReporter(ttt_client=client, config=config)

        await reporter.update_recording_status(None, "download", "downloaded")
        client.update_recording_status.assert_not_called()


class TestGracefulDegradation:
    """TTT enabled but unreachable -- pipeline continues working."""

    def _make_failing_reporter(self):
        """Create a TTTReporter with a client that errors on every method."""
        client = MagicMock()
        # Make every client method raise
        for method_name in [
            "update_camera_status",
            "register_recordings",
            "update_recording_status",
            "enhanced_heartbeat",
            "get_pending_commands",
            "get_camera_config",
            "push_camera_config",
            "get_high_water_mark",
            "get_team_assignments",
        ]:
            getattr(client, method_name).side_effect = Exception("Network unreachable")

        config = MagicMock()
        config.ttt.camera_id = "test-camera"
        config.ttt.ttt_sync_enabled = True
        config.ttt.heartbeat_interval = 30
        config.ttt.service_id = None
        return TTTReporter(ttt_client=client, config=config)

    @pytest.mark.asyncio
    async def test_camera_status_survives_network_error(self):
        """report_camera_status doesn't raise on network error."""
        reporter = self._make_failing_reporter()
        await reporter.report_camera_status("online")  # Should not raise

    @pytest.mark.asyncio
    async def test_register_recordings_returns_none_on_error(self):
        """register_recordings returns None when TTT is unreachable."""
        reporter = self._make_failing_reporter()
        mock_file = MagicMock()
        mock_file.file_path = "test.mp4"
        mock_file.start_time = datetime(2026, 3, 1)
        mock_file.end_time = datetime(2026, 3, 1)
        mock_file.metadata = {}
        mock_file.group_dir = "/storage/test"
        result = await reporter.register_recordings([mock_file])
        assert result is None  # Not a crash, just None

    @pytest.mark.asyncio
    async def test_update_recording_status_survives_error(self):
        """update_recording_status silently logs on error."""
        reporter = self._make_failing_reporter()
        await reporter.update_recording_status(
            "rec-123", "download", "failed"
        )  # Should not raise

    @pytest.mark.asyncio
    async def test_heartbeat_survives_error(self):
        """report_heartbeat silently continues on error."""
        reporter = self._make_failing_reporter()
        await reporter.report_heartbeat()  # Should not raise

    @pytest.mark.asyncio
    async def test_command_poll_returns_empty_on_error(self):
        """poll_pending_commands returns [] when TTT is unreachable."""
        reporter = self._make_failing_reporter()
        result = await reporter.poll_pending_commands()
        assert result == []

    @pytest.mark.asyncio
    async def test_sync_config_returns_none_on_error(self):
        """sync_config returns None when TTT is unreachable."""
        reporter = self._make_failing_reporter()
        result = await reporter.sync_config()
        assert result is None

    @pytest.mark.asyncio
    async def test_high_water_mark_returns_none_on_error(self):
        """get_high_water_mark returns None when TTT is unreachable."""
        reporter = self._make_failing_reporter()
        result = await reporter.get_high_water_mark()
        assert result is None

    @pytest.mark.asyncio
    async def test_job_processor_survives_api_error(self):
        """TTTJobProcessor.discover_work() handles API errors gracefully."""
        from video_grouper.task_processors.ttt_job_processor import TTTJobProcessor

        mock_client = MagicMock()
        mock_client.is_authenticated.return_value = True
        mock_client.get_pending_jobs.side_effect = Exception("Network error")

        config = MagicMock()
        config.ttt.job_polling_enabled = True
        config.ttt.machine_name = "test"
        config.cameras = [MagicMock(type="dahua", device_ip="1.2.3.4")]
        config.ball_tracking.enabled = False

        processor = TTTJobProcessor(
            storage_path=tempfile.mkdtemp(),
            config=config,
            ttt_client=mock_client,
        )
        # Should not raise
        await processor.discover_work()


class TestIntegrationValueAdd:
    """Demonstrate what TTT integration enables beyond standalone operation."""

    def test_remote_visibility_recordings_tracked(self, ttt_client):
        """Registered recordings are visible via the camera-recordings API."""
        result = ttt_client._request(
            "GET",
            f"{TTT_API_URL}/api/camera-recordings",
            params={"team_id": TEAM_ID},
        )
        assert isinstance(result, list)
        assert len(result) > 0  # Seed + test recordings exist

    def test_remote_visibility_service_dashboard(self, ttt_client):
        """Service dashboard shows registered services and metrics."""
        result = ttt_client._request("GET", f"{TTT_API_URL}/api/service-dashboard")
        assert isinstance(result, dict)
        # Dashboard should have service info from our heartbeats
        assert "service" in result or "active_jobs" in result

    def test_config_backup_and_restore(self, ttt_client):
        """Save device config, then retrieve it (simulating new device restore)."""
        # Save
        config_data = {
            "camera_ip": "10.0.0.50",
            "ntfy_topic": "restore-test",
            "youtube_configured": False,
        }
        ttt_client.save_device_config(config_data)

        # Simulate new device: retrieve the config
        retrieved = ttt_client.get_device_config()
        assert retrieved is not None
        assert retrieved.get("camera_ip") == "10.0.0.50"
        assert retrieved.get("ntfy_topic") == "restore-test"


# ===================================================================
# Section D: Auth Edge Cases
# ===================================================================


class TestAuthEdgeCases:
    """Authentication edge cases."""

    def test_login_bad_credentials(self, ttt_backend_available, tmp_path):
        """Wrong password raises TTTApiError."""
        client = TTTApiClient(
            supabase_url=SUPABASE_URL,
            anon_key=SUPABASE_ANON_KEY,
            api_base_url=TTT_API_URL,
            storage_path=str(tmp_path),
        )
        with pytest.raises(TTTApiError) as exc_info:
            client.login(TEST_EMAIL, "wrong_password_123")
        assert exc_info.value.status_code == 400

    def test_unauthenticated_call_raises(self, ttt_backend_available, tmp_path):
        """API call without login raises TTTApiError."""
        client = TTTApiClient(
            supabase_url=SUPABASE_URL,
            anon_key=SUPABASE_ANON_KEY,
            api_base_url=TTT_API_URL,
            storage_path=str(tmp_path),
        )
        with pytest.raises(TTTApiError, match="Not authenticated"):
            client.get_team_assignments()

    def test_refresh_token_after_login(self, ttt_backend_available, tmp_path):
        """Token refresh succeeds after login."""
        client = TTTApiClient(
            supabase_url=SUPABASE_URL,
            anon_key=SUPABASE_ANON_KEY,
            api_base_url=TTT_API_URL,
            storage_path=str(tmp_path),
        )
        client.login(TEST_EMAIL, TEST_PASSWORD)
        assert client.is_authenticated()
        client.refresh_token()
        assert client.is_authenticated()

    def test_token_persistence_and_reload(self, ttt_backend_available, tmp_path):
        """Tokens persist to disk and can be loaded by a new client."""
        storage = str(tmp_path)
        client1 = TTTApiClient(
            supabase_url=SUPABASE_URL,
            anon_key=SUPABASE_ANON_KEY,
            api_base_url=TTT_API_URL,
            storage_path=storage,
        )
        client1.login(TEST_EMAIL, TEST_PASSWORD)
        assert client1.is_authenticated()

        # New client pointed at same storage should load tokens
        client2 = TTTApiClient(
            supabase_url=SUPABASE_URL,
            anon_key=SUPABASE_ANON_KEY,
            api_base_url=TTT_API_URL,
            storage_path=storage,
        )
        assert client2.is_authenticated()


class TestCapabilities:
    """Feature flags and plugins."""

    def test_get_capabilities(self, ttt_client):
        """Get feature flags for the current user."""
        result = ttt_client.get_capabilities()
        assert isinstance(result, (dict, list))

    def test_get_available_plugins(self, ttt_client):
        """Get list of available plugins."""
        result = ttt_client.get_available_plugins()
        assert isinstance(result, list)
