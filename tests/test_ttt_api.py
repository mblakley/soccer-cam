"""Tests for the TTT API client."""

import json
import tempfile
import time
import unittest
from unittest.mock import Mock

from video_grouper.api_integrations.ttt_api import TTTApiClient, TTTApiError


def _mock_response(status_code=200, json_data=None, text=""):
    """Create a mock httpx response."""
    resp = Mock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    resp.text = text or json.dumps(json_data if json_data is not None else {})
    return resp


class TestTTTApiClient(unittest.TestCase):
    """Test the TTT API client."""

    def setUp(self):
        self.client = TTTApiClient(
            supabase_url="https://test.supabase.co",
            anon_key="test-anon-key",
            api_base_url="https://api.test.com",
            storage_path=tempfile.mkdtemp(),
        )
        # Pre-set tokens to skip login
        self.client._access_token = "test-jwt-token"
        self.client._expires_at = time.time() + 3600
        self.client._refresh_token_value = "test-refresh"

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def test_not_authenticated_raises(self):
        self.client._access_token = None
        with self.assertRaises(TTTApiError, msg="Not authenticated"):
            self.client.get_team_assignments()

    def test_request_http_error(self):
        self.client._http.request = Mock(
            return_value=_mock_response(500, text="Internal Server Error")
        )
        with self.assertRaises(TTTApiError) as ctx:
            self.client.get_team_assignments()
        self.assertEqual(ctx.exception.status_code, 500)

    def test_request_204_returns_none(self):
        self.client._http.request = Mock(return_value=_mock_response(204))
        result = self.client._request("DELETE", "https://api.test.com/api/something")
        self.assertIsNone(result)

    # ------------------------------------------------------------------
    # Existing clip request methods
    # ------------------------------------------------------------------

    def test_get_team_assignments(self):
        data = [{"team_id": "t1", "team_name": "Eagles"}]
        self.client._http.request = Mock(return_value=_mock_response(200, data))
        result = self.client.get_team_assignments()
        self.assertEqual(result, data)
        call_args = self.client._http.request.call_args
        self.assertEqual(
            call_args[0], ("GET", "https://api.test.com/api/device-link/me")
        )

    def test_get_pending_clip_requests(self):
        data = [{"id": "cr1", "status": "pending"}]
        self.client._http.request = Mock(return_value=_mock_response(200, data))
        result = self.client.get_pending_clip_requests()
        self.assertEqual(result, data)

    def test_start_clip_request(self):
        data = {"id": "cr1", "status": "in_progress"}
        self.client._http.request = Mock(return_value=_mock_response(200, data))
        result = self.client.start_clip_request("cr1")
        self.assertEqual(result["status"], "in_progress")
        call_args = self.client._http.request.call_args
        self.assertIn("/api/clip-requests/cr1/start", call_args[0][1])

    def test_fulfill_clip_request(self):
        data = {"id": "cr1", "status": "fulfilled"}
        self.client._http.request = Mock(return_value=_mock_response(200, data))
        result = self.client.fulfill_clip_request(
            "cr1", "https://drive.google.com/file", "Done"
        )
        self.assertEqual(result["status"], "fulfilled")
        call_args = self.client._http.request.call_args
        body = call_args[1]["json"]
        self.assertEqual(body["fulfilled_url"], "https://drive.google.com/file")
        self.assertEqual(body["fulfilled_notes"], "Done")

    # ------------------------------------------------------------------
    # Schedule & game management
    # ------------------------------------------------------------------

    def test_get_schedule(self):
        data = [
            {
                "game_id": "g1",
                "opponent_name": "Falcons",
                "start_time": "2026-03-08T15:00:00Z",
            }
        ]
        self.client._http.request = Mock(return_value=_mock_response(200, data))
        result = self.client.get_schedule("team-1", "2026-03-01", "2026-03-31")
        self.assertEqual(result, data)
        call_args = self.client._http.request.call_args
        params = call_args[1]["params"]
        self.assertEqual(params["team_id"], "team-1")
        self.assertEqual(params["start_date"], "2026-03-01")
        self.assertEqual(params["end_date"], "2026-03-31")

    def test_get_schedule_no_dates(self):
        self.client._http.request = Mock(return_value=_mock_response(200, []))
        self.client.get_schedule("team-1")
        call_args = self.client._http.request.call_args
        params = call_args[1]["params"]
        self.assertEqual(params, {"team_id": "team-1"})

    def test_get_roster(self):
        data = [{"user_id": "u1", "display_name": "Player One", "role": "player"}]
        self.client._http.request = Mock(return_value=_mock_response(200, data))
        result = self.client.get_roster("team-1")
        self.assertEqual(result, data)
        call_args = self.client._http.request.call_args
        self.assertEqual(call_args[1]["params"]["team_id"], "team-1")

    def test_auto_match_video_matched(self):
        data = {
            "matched": True,
            "game_id": "g1",
            "opponent_name": "Falcons",
            "message": "ok",
        }
        self.client._http.request = Mock(return_value=_mock_response(200, data))
        result = self.client.auto_match_video(
            "team-1", "https://youtube.com/watch?v=abc", "2026-03-08T15:00:00Z"
        )
        self.assertTrue(result["matched"])
        call_args = self.client._http.request.call_args
        body = call_args[1]["json"]
        self.assertEqual(body["team_id"], "team-1")
        self.assertEqual(body["video_url"], "https://youtube.com/watch?v=abc")

    def test_auto_match_video_no_match(self):
        data = {"matched": False, "game_id": None, "message": "No matching game found"}
        self.client._http.request = Mock(return_value=_mock_response(200, data))
        result = self.client.auto_match_video(
            "team-1", "https://youtube.com/watch?v=abc", "2026-03-08T15:00:00Z"
        )
        self.assertFalse(result["matched"])

    # ------------------------------------------------------------------
    # Game sessions
    # ------------------------------------------------------------------

    def test_get_game_sessions_with_dir(self):
        data = [{"id": "gs1", "recording_group_dir": "/data/2026-03-08"}]
        self.client._http.request = Mock(return_value=_mock_response(200, data))
        result = self.client.get_game_sessions(
            "team-1", recording_group_dir="/data/2026-03-08"
        )
        self.assertEqual(result, data)
        params = self.client._http.request.call_args[1]["params"]
        self.assertEqual(params["recording_group_dir"], "/data/2026-03-08")

    def test_get_game_sessions_without_dir(self):
        self.client._http.request = Mock(return_value=_mock_response(200, []))
        self.client.get_game_sessions("team-1")
        params = self.client._http.request.call_args[1]["params"]
        self.assertNotIn("recording_group_dir", params)

    def test_create_game_session_minimal(self):
        data = {"id": "gs1", "team_id": "team-1"}
        self.client._http.request = Mock(return_value=_mock_response(200, data))
        result = self.client.create_game_session(
            "team-1", "/data/2026-03-08", "2026-03-08", "Falcons"
        )
        self.assertEqual(result["id"], "gs1")
        body = self.client._http.request.call_args[1]["json"]
        self.assertEqual(body["opponent_name"], "Falcons")
        self.assertEqual(body["status"], "processing")
        self.assertNotIn("video_youtube_id", body)

    def test_create_game_session_with_youtube_id(self):
        self.client._http.request = Mock(
            return_value=_mock_response(200, {"id": "gs1"})
        )
        self.client.create_game_session(
            "team-1",
            "/data/2026-03-08",
            "2026-03-08",
            "Falcons",
            video_youtube_id="abc123",
        )
        body = self.client._http.request.call_args[1]["json"]
        self.assertEqual(body["video_youtube_id"], "abc123")

    def test_update_game_session(self):
        data = {"id": "gs1", "status": "uploaded"}
        self.client._http.request = Mock(return_value=_mock_response(200, data))
        result = self.client.update_game_session(
            "gs1", status="uploaded", video_youtube_id="xyz"
        )
        self.assertEqual(result["status"], "uploaded")
        body = self.client._http.request.call_args[1]["json"]
        self.assertEqual(body["status"], "uploaded")
        self.assertEqual(body["video_youtube_id"], "xyz")

    # ------------------------------------------------------------------
    # Time sync
    # ------------------------------------------------------------------

    def test_get_sync_anchors(self):
        data = [{"id": "sa1", "anchor_type": "chirp", "status": "pending"}]
        self.client._http.request = Mock(return_value=_mock_response(200, data))
        result = self.client.get_sync_anchors("gs1")
        self.assertEqual(result, data)
        params = self.client._http.request.call_args[1]["params"]
        self.assertEqual(params["game_session_id"], "gs1")

    def test_update_sync_anchor(self):
        data = {"id": "sa1", "status": "detected"}
        self.client._http.request = Mock(return_value=_mock_response(200, data))
        result = self.client.update_sync_anchor(
            "sa1",
            detected_at_video_time=123.45,
            detection_confidence=0.95,
            computed_offset_ms=-3200.0,
            status="detected",
        )
        self.assertEqual(result["status"], "detected")
        body = self.client._http.request.call_args[1]["json"]
        self.assertAlmostEqual(body["detected_at_video_time"], 123.45)
        self.assertAlmostEqual(body["detection_confidence"], 0.95)

    def test_reconcile_game_session(self):
        data = {
            "sync_status": "synced",
            "true_recording_start": "2026-03-08T14:58:00Z",
            "tags_updated": 5,
        }
        self.client._http.request = Mock(return_value=_mock_response(200, data))
        result = self.client.reconcile_game_session("gs1")
        self.assertEqual(result["tags_updated"], 5)
        call_url = self.client._http.request.call_args[0][1]
        self.assertIn("/api/game-sessions/gs1/reconcile", call_url)

    # ------------------------------------------------------------------
    # Moment tags & clips
    # ------------------------------------------------------------------

    def test_get_pending_moment_tags(self):
        data = [
            {
                "id": "mt1",
                "tagged_at": "2026-03-08T15:10:00Z",
                "video_offset_seconds": None,
            }
        ]
        self.client._http.request = Mock(return_value=_mock_response(200, data))
        result = self.client.get_pending_moment_tags("gs1")
        self.assertEqual(result, data)
        params = self.client._http.request.call_args[1]["params"]
        self.assertEqual(params["pending_offset"], "true")
        self.assertEqual(params["game_session_id"], "gs1")

    def test_update_moment_tag(self):
        data = {"id": "mt1", "video_offset_seconds": 720.5}
        self.client._http.request = Mock(return_value=_mock_response(200, data))
        result = self.client.update_moment_tag(
            "mt1", video_offset_seconds=720.5, trimmed_offset_seconds=120.5
        )
        self.assertAlmostEqual(result["video_offset_seconds"], 720.5)
        body = self.client._http.request.call_args[1]["json"]
        self.assertAlmostEqual(body["trimmed_offset_seconds"], 120.5)

    def test_create_moment_clip(self):
        data = {"id": "mc1", "status": "pending"}
        self.client._http.request = Mock(return_value=_mock_response(200, data))
        result = self.client.create_moment_clip("mt1", "gs1", 700.0, 730.0, 30.0)
        self.assertEqual(result["id"], "mc1")
        body = self.client._http.request.call_args[1]["json"]
        self.assertEqual(body["moment_tag_id"], "mt1")
        self.assertEqual(body["game_session_id"], "gs1")
        self.assertAlmostEqual(body["clip_start_offset"], 700.0)
        self.assertAlmostEqual(body["clip_end_offset"], 730.0)

    def test_create_moment_clip_default_duration(self):
        self.client._http.request = Mock(
            return_value=_mock_response(200, {"id": "mc1"})
        )
        self.client.create_moment_clip("mt1", "gs1", 700.0, 730.0)
        body = self.client._http.request.call_args[1]["json"]
        self.assertAlmostEqual(body["clip_duration"], 30.0)

    def test_update_moment_clip(self):
        data = {"id": "mc1", "status": "ready", "file_path": "/clips/mc1.mp4"}
        self.client._http.request = Mock(return_value=_mock_response(200, data))
        result = self.client.update_moment_clip(
            "mc1", status="ready", file_path="/clips/mc1.mp4"
        )
        self.assertEqual(result["status"], "ready")
        body = self.client._http.request.call_args[1]["json"]
        self.assertEqual(body["file_path"], "/clips/mc1.mp4")

    # ------------------------------------------------------------------
    # Schedule providers — PlayMetrics onboarding additions
    # ------------------------------------------------------------------

    def test_create_schedule_provider_default(self):
        data = {"id": "p1", "provider_type": "teamsnap"}
        self.client._http.request = Mock(return_value=_mock_response(201, data))
        result = self.client.create_schedule_provider(
            {"provider_type": "teamsnap", "credentials": {"client_id": "cid"}}
        )
        self.assertEqual(result["id"], "p1")
        # No dry_run param when default
        self.assertIsNone(self.client._http.request.call_args[1].get("params"))

    def test_create_schedule_provider_dry_run(self):
        dry_run_response = {"ok": True, "teams": [{"id": "t1", "name": "Eagles"}]}
        self.client._http.request = Mock(
            return_value=_mock_response(201, dry_run_response)
        )
        result = self.client.create_schedule_provider(
            {"provider_type": "teamsnap", "credentials": {"client_id": "cid"}},
            dry_run=True,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["teams"][0]["id"], "t1")
        # dry_run=true should be passed as a query param
        self.assertEqual(
            self.client._http.request.call_args[1]["params"], {"dry_run": "true"}
        )

    def test_connect_playmetrics(self):
        data = {
            "refresh_token": "rt-1",
            "roles": [{"id": "role-1", "name": "Coach"}],
            "teams": [{"id": "tm-1", "name": "Eagles", "role_id": "role-1"}],
        }
        self.client._http.request = Mock(return_value=_mock_response(200, data))
        result = self.client.connect_playmetrics("user@example.com", "pw")
        self.assertEqual(result["refresh_token"], "rt-1")
        self.assertEqual(result["roles"][0]["id"], "role-1")
        # Body should carry email + password; URL should be the connect endpoint
        call_args = self.client._http.request.call_args
        self.assertEqual(
            call_args[0],
            (
                "POST",
                "https://api.test.com/api/device-link/schedule-providers/playmetrics/connect",
            ),
        )
        self.assertEqual(
            call_args[1]["json"], {"email": "user@example.com", "password": "pw"}
        )


if __name__ == "__main__":
    unittest.main()
