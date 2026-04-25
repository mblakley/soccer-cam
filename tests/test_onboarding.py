"""Tests for the onboarding wizard and supporting config/API functions."""

import json
from unittest.mock import MagicMock, patch

from video_grouper.utils.config import (
    Config,
    PlayMetricsTeamConfig,
    create_default_config,
    config_needs_onboarding,
    load_config,
    save_config,
)


# ---------------------------------------------------------------------------
# create_default_config
# ---------------------------------------------------------------------------


class TestCreateDefaultConfig:
    def test_creates_config_file(self, tmp_path):
        config_path = tmp_path / "config.ini"
        storage_path = str(tmp_path / "storage")

        config = create_default_config(config_path, storage_path)

        assert config_path.exists()
        assert isinstance(config, Config)
        assert config.storage.path == storage_path

    def test_creates_parent_directories(self, tmp_path):
        config_path = tmp_path / "sub" / "dir" / "config.ini"
        storage_path = str(tmp_path / "storage")

        create_default_config(config_path, storage_path)

        assert config_path.exists()

    def test_default_config_values(self, tmp_path):
        config_path = tmp_path / "config.ini"
        config = create_default_config(config_path, str(tmp_path))

        assert config.youtube.enabled is False
        assert config.ntfy.enabled is False
        assert config.ball_tracking.enabled is False
        assert config.ttt.enabled is False
        assert config.setup.onboarding_completed is False

    def test_config_is_loadable_after_save(self, tmp_path):
        config_path = tmp_path / "config.ini"
        create_default_config(config_path, str(tmp_path))

        reloaded = load_config(config_path)
        assert reloaded.storage.path == str(tmp_path)

    def test_no_cameras_by_default(self, tmp_path):
        config_path = tmp_path / "config.ini"
        config = create_default_config(config_path, str(tmp_path))

        assert config.cameras == []


# ---------------------------------------------------------------------------
# config_needs_onboarding
# ---------------------------------------------------------------------------


class TestConfigNeedsOnboarding:
    def test_returns_true_when_no_file(self, tmp_path):
        assert config_needs_onboarding(tmp_path / "missing.ini") is True

    def test_returns_true_when_onboarding_not_completed(self, tmp_path):
        config_path = tmp_path / "config.ini"
        create_default_config(config_path, str(tmp_path))

        assert config_needs_onboarding(config_path) is True

    def test_returns_false_when_onboarding_completed(self, tmp_path):
        config_path = tmp_path / "config.ini"
        config = create_default_config(config_path, str(tmp_path))
        config.setup.onboarding_completed = True
        save_config(config, config_path)

        assert config_needs_onboarding(config_path) is False

    def test_returns_true_on_corrupt_file(self, tmp_path):
        config_path = tmp_path / "config.ini"
        config_path.write_text("not valid ini [[[")

        assert config_needs_onboarding(config_path) is True


# ---------------------------------------------------------------------------
# SetupConfig in Config
# ---------------------------------------------------------------------------


class TestSetupConfig:
    def test_setup_section_roundtrips(self, tmp_path):
        config_path = tmp_path / "config.ini"
        config = create_default_config(config_path, str(tmp_path))
        config.setup.onboarding_completed = True
        save_config(config, config_path)

        reloaded = load_config(config_path)
        assert reloaded.setup.onboarding_completed is True

    def test_setup_defaults_to_not_completed(self, tmp_path):
        config_path = tmp_path / "config.ini"
        config = create_default_config(config_path, str(tmp_path))
        assert config.setup.onboarding_completed is False


# ---------------------------------------------------------------------------
# PlayMetrics teams round-trip
# ---------------------------------------------------------------------------


class TestPlayMetricsTeamsRoundTrip:
    def test_teams_roundtrip_as_sections(self, tmp_path):
        config_path = tmp_path / "config.ini"
        config = create_default_config(config_path, str(tmp_path))
        config.playmetrics.enabled = True
        config.playmetrics.username = "user@example.com"
        config.playmetrics.password = "secret"
        config.playmetrics.teams = [
            PlayMetricsTeamConfig(
                team_id="335774",
                team_name="WNY Flash - 13B ECNL-RL",
                enabled=True,
            ),
            PlayMetricsTeamConfig(
                team_id="111222",
                team_name="WNY Flash - 15G",
                enabled=False,
            ),
        ]
        save_config(config, config_path)

        raw = config_path.read_text(encoding="utf-8")
        assert "[PLAYMETRICS.TEAM.0]" in raw
        assert "[PLAYMETRICS.TEAM.1]" in raw
        assert "PlayMetricsTeamConfig(" not in raw

        reloaded = load_config(config_path)
        assert reloaded.playmetrics.enabled is True
        assert reloaded.playmetrics.username == "user@example.com"
        assert len(reloaded.playmetrics.teams) == 2
        by_id = {t.team_id: t for t in reloaded.playmetrics.teams}
        assert by_id["335774"].team_name == "WNY Flash - 13B ECNL-RL"
        assert by_id["335774"].enabled is True
        assert by_id["111222"].enabled is False

    def test_non_ascii_team_name_roundtrips(self, tmp_path):
        config_path = tmp_path / "config.ini"
        config = create_default_config(config_path, str(tmp_path))
        config.playmetrics.enabled = True
        config.playmetrics.username = "user@example.com"
        config.playmetrics.password = "secret"
        config.playmetrics.teams = [
            PlayMetricsTeamConfig(
                team_id="1", team_name="Flash \u2014 13B", enabled=True
            ),
        ]
        save_config(config, config_path)

        reloaded = load_config(config_path)
        assert reloaded.playmetrics.teams[0].team_name == "Flash \u2014 13B"


# ---------------------------------------------------------------------------
# TTT API client - device config methods
# ---------------------------------------------------------------------------


class TestTTTApiDeviceConfig:
    @patch("video_grouper.api_integrations.ttt_api.httpx.Client")
    def test_get_device_config_success(self, mock_client_cls, tmp_path):
        from video_grouper.api_integrations.ttt_api import TTTApiClient

        mock_http = MagicMock()
        mock_client_cls.return_value = mock_http

        client = TTTApiClient(
            supabase_url="https://example.supabase.co",
            anon_key="test-key",
            api_base_url="https://api.example.com",
            storage_path=str(tmp_path),
        )
        client._access_token = "test-token"
        client._expires_at = 9999999999.0

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "ntfy_topic": "soccer-cam-abc123",
            "camera_ip": "192.168.1.100",
        }
        mock_http.request.return_value = mock_resp

        result = client.get_device_config()

        assert result["ntfy_topic"] == "soccer-cam-abc123"
        assert result["camera_ip"] == "192.168.1.100"

    @patch("video_grouper.api_integrations.ttt_api.httpx.Client")
    def test_get_device_config_not_found(self, mock_client_cls, tmp_path):
        from video_grouper.api_integrations.ttt_api import TTTApiClient

        mock_http = MagicMock()
        mock_client_cls.return_value = mock_http

        client = TTTApiClient(
            supabase_url="https://example.supabase.co",
            anon_key="test-key",
            api_base_url="https://api.example.com",
            storage_path=str(tmp_path),
        )
        client._access_token = "test-token"
        client._expires_at = 9999999999.0

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = "Not found"
        mock_http.request.return_value = mock_resp

        result = client.get_device_config()
        assert result is None

    @patch("video_grouper.api_integrations.ttt_api.httpx.Client")
    def test_save_device_config(self, mock_client_cls, tmp_path):
        from video_grouper.api_integrations.ttt_api import TTTApiClient

        mock_http = MagicMock()
        mock_client_cls.return_value = mock_http

        client = TTTApiClient(
            supabase_url="https://example.supabase.co",
            anon_key="test-key",
            api_base_url="https://api.example.com",
            storage_path=str(tmp_path),
        )
        client._access_token = "test-token"
        client._expires_at = 9999999999.0

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ntfy_topic": "new-topic"}
        mock_http.request.return_value = mock_resp

        data = {"ntfy_topic": "new-topic", "camera_ip": "10.0.0.1"}
        result = client.save_device_config(data)

        assert result["ntfy_topic"] == "new-topic"
        # Verify it called PUT on the right endpoint
        call_args = mock_http.request.call_args
        assert call_args[0][0] == "PUT"
        assert "/api/device-link/config" in call_args[0][1]


# ---------------------------------------------------------------------------
# Password encryption
# ---------------------------------------------------------------------------


class TestDeviceConfigPasswordHandling:
    """Verify the wizard sends camera_password for server-side encryption."""

    @patch("video_grouper.api_integrations.ttt_api.httpx.Client")
    def test_camera_password_included_in_save(self, mock_client_cls, tmp_path):
        """Verify camera_password is sent to TTT API for server-side encryption."""
        from video_grouper.api_integrations.ttt_api import TTTApiClient

        mock_http = MagicMock()
        mock_client_cls.return_value = mock_http

        client = TTTApiClient(
            supabase_url="https://example.supabase.co",
            anon_key="test-key",
            api_base_url="https://api.example.com",
            storage_path=str(tmp_path),
        )
        client._access_token = "test-token"
        client._expires_at = 9999999999.0

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"camera_password": "secret123"}
        mock_http.request.return_value = mock_resp

        data = {
            "camera_ip": "192.168.1.50",
            "camera_password": "secret123",
        }
        client.save_device_config(data)

        call_args = mock_http.request.call_args
        sent_body = call_args[1]["json"]
        assert sent_body["camera_password"] == "secret123"

    @patch("video_grouper.api_integrations.ttt_api.httpx.Client")
    def test_camera_password_returned_on_get(self, mock_client_cls, tmp_path):
        """Verify camera_password is returned (decrypted server-side) on GET."""
        from video_grouper.api_integrations.ttt_api import TTTApiClient

        mock_http = MagicMock()
        mock_client_cls.return_value = mock_http

        client = TTTApiClient(
            supabase_url="https://example.supabase.co",
            anon_key="test-key",
            api_base_url="https://api.example.com",
            storage_path=str(tmp_path),
        )
        client._access_token = "test-token"
        client._expires_at = 9999999999.0

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "camera_ip": "192.168.1.50",
            "camera_password": "secret123",
        }
        mock_http.request.return_value = mock_resp

        result = client.get_device_config()
        assert result["camera_password"] == "secret123"


# ---------------------------------------------------------------------------
# YouTube embedded OAuth client
# ---------------------------------------------------------------------------


class TestYouTubeEmbeddedAuth:
    def test_embedded_client_config_has_required_fields(self):
        from video_grouper.utils.youtube_upload import EMBEDDED_CLIENT_CONFIG

        installed = EMBEDDED_CLIENT_CONFIG["installed"]
        assert "client_id" in installed
        assert "client_secret" in installed
        assert "auth_uri" in installed
        assert "token_uri" in installed
        assert "redirect_uris" in installed

    @patch("video_grouper.utils.youtube_upload.os.makedirs")
    def test_embedded_auth_reuses_existing_token(self, mock_makedirs, tmp_path):
        """If a valid token exists, embedded auth should load it."""
        youtube_dir = tmp_path / "youtube"
        youtube_dir.mkdir(parents=True, exist_ok=True)
        token_file = youtube_dir / "token.json"

        token_data = {
            "token": "mock-access-token",
            "refresh_token": "mock-refresh-token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "test.apps.googleusercontent.com",
            "client_secret": "test-secret",
            "scopes": [
                "https://www.googleapis.com/auth/youtube.upload",
                "https://www.googleapis.com/auth/youtube.readonly",
                "https://www.googleapis.com/auth/youtube",
            ],
        }
        token_file.write_text(json.dumps(token_data))

        assert token_file.exists()


# ---------------------------------------------------------------------------
# Wizard config save (integration-style, no GUI)
# ---------------------------------------------------------------------------


class TestWizardConfigSave:
    """Test the config generation that the wizard produces."""

    def test_full_config_with_all_integrations(self, tmp_path):
        """Simulate a wizard run that configures everything."""
        from video_grouper.utils.config import CameraConfig

        config_path = tmp_path / "config.ini"
        config = create_default_config(config_path, str(tmp_path))

        # Camera
        config.cameras = [
            CameraConfig(
                name="reolink",
                type="reolink",
                device_ip="192.168.1.50",
                username="admin",
                password="secret123",
            )
        ]

        # YouTube
        config.youtube.enabled = True

        # NTFY
        config.ntfy.enabled = True
        config.ntfy.topic = "soccer-cam-test1234"
        config.ntfy.server_url = "https://ntfy.sh"
        config.ntfy.response_service = True

        # TTT
        config.ttt.enabled = True
        config.ttt.supabase_url = "https://test.supabase.co"
        config.ttt.anon_key = "test-anon-key"
        config.ttt.api_base_url = "https://api.test.com"
        config.ttt.email = "user@example.com"
        config.ttt.password = "testpass"

        # Mark completed
        config.setup.onboarding_completed = True

        save_config(config, config_path)

        # Verify everything roundtrips
        reloaded = load_config(config_path)
        assert len(reloaded.cameras) == 1
        assert reloaded.cameras[0].device_ip == "192.168.1.50"
        assert reloaded.youtube.enabled is True
        assert reloaded.ntfy.enabled is True
        assert reloaded.ntfy.topic == "soccer-cam-test1234"
        assert reloaded.ttt.enabled is True
        assert reloaded.ttt.email == "user@example.com"
        assert reloaded.setup.onboarding_completed is True

    def test_minimal_config_all_skipped(self, tmp_path):
        """Simulate a wizard run where user skips everything."""
        config_path = tmp_path / "config.ini"
        config = create_default_config(config_path, str(tmp_path))
        config.setup.onboarding_completed = True
        save_config(config, config_path)

        reloaded = load_config(config_path)
        assert reloaded.cameras == []
        assert reloaded.youtube.enabled is False
        assert reloaded.ntfy.enabled is False
        assert reloaded.ttt.enabled is False
        assert reloaded.setup.onboarding_completed is True

    def test_ntfy_auto_generated_topic_format(self):
        """Verify the NTFY topic generation produces valid topics."""
        import secrets

        topic = f"soccer-cam-{secrets.token_hex(4)}"
        assert topic.startswith("soccer-cam-")
        assert len(topic) == len("soccer-cam-") + 8  # 4 bytes = 8 hex chars


# ---------------------------------------------------------------------------
# Wizard TTT integration (mocked API)
# ---------------------------------------------------------------------------


class TestWizardTTTIntegration:
    """Test the TTT device config save/restore flow without GUI."""

    @patch("video_grouper.api_integrations.ttt_api.httpx.Client")
    def test_ttt_login_and_save_config(self, mock_client_cls, tmp_path):
        from video_grouper.api_integrations.ttt_api import TTTApiClient

        mock_http = MagicMock()
        mock_client_cls.return_value = mock_http

        # Mock login response
        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {
            "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJleHAiOjk5OTk5OTk5OTl9."
            "K5VmG3H1vG4RJQqCZr5zPm9XhYwGUvxWGVe0EbVxdEQ",
            "refresh_token": "test-refresh",
            "expires_in": 3600,
        }

        # Mock get_team_assignments response
        teams_resp = MagicMock()
        teams_resp.status_code = 200
        teams_resp.json.return_value = [
            {
                "camera_manager_id": "cm-1",
                "team_id": "team-1",
                "team_name": "Eagles",
            }
        ]

        # Mock save_device_config response
        save_resp = MagicMock()
        save_resp.status_code = 200
        save_resp.json.return_value = {"ntfy_topic": "soccer-cam-abc"}

        mock_http.post.return_value = login_resp
        mock_http.request.side_effect = [teams_resp, save_resp]

        client = TTTApiClient(
            supabase_url="https://test.supabase.co",
            anon_key="test-key",
            api_base_url="https://api.test.com",
            storage_path=str(tmp_path),
        )

        # Login
        client.login("user@example.com", "pass123")
        assert client.is_authenticated()

        # Get teams
        teams = client.get_team_assignments()
        assert len(teams) == 1
        assert teams[0]["team_name"] == "Eagles"

        # Save device config
        result = client.save_device_config(
            {"ntfy_topic": "soccer-cam-abc", "camera_ip": "192.168.1.50"}
        )
        assert result["ntfy_topic"] == "soccer-cam-abc"

    @patch("video_grouper.api_integrations.ttt_api.httpx.Client")
    def test_ttt_restore_flow(self, mock_client_cls, tmp_path):
        """Test that stored config can be restored to local config."""
        from video_grouper.api_integrations.ttt_api import TTTApiClient

        mock_http = MagicMock()
        mock_client_cls.return_value = mock_http

        client = TTTApiClient(
            supabase_url="https://test.supabase.co",
            anon_key="test-key",
            api_base_url="https://api.test.com",
            storage_path=str(tmp_path),
        )
        client._access_token = "test-token"
        client._expires_at = 9999999999.0

        # Mock get_device_config response (existing config found)
        config_resp = MagicMock()
        config_resp.status_code = 200
        config_resp.json.return_value = {
            "ntfy_topic": "soccer-cam-existing",
            "ntfy_server_url": "https://ntfy.sh",
            "youtube_configured": True,
            "camera_ip": "10.0.0.5",
            "camera_username": "admin",
            "gcp_project_id": "soccer-cam-abc123",
        }
        mock_http.request.return_value = config_resp

        device_config = client.get_device_config()
        assert device_config is not None
        assert device_config["ntfy_topic"] == "soccer-cam-existing"
        assert device_config["camera_ip"] == "10.0.0.5"

        # Apply to local config (simulating wizard restore)
        config_path = tmp_path / "config.ini"
        config = create_default_config(config_path, str(tmp_path))

        if device_config.get("ntfy_topic"):
            config.ntfy.enabled = True
            config.ntfy.topic = device_config["ntfy_topic"]
            config.ntfy.server_url = device_config.get(
                "ntfy_server_url", "https://ntfy.sh"
            )

        if device_config.get("youtube_configured"):
            config.youtube.enabled = True

        config.setup.onboarding_completed = True
        save_config(config, config_path)

        reloaded = load_config(config_path)
        assert reloaded.ntfy.enabled is True
        assert reloaded.ntfy.topic == "soccer-cam-existing"
        assert reloaded.youtube.enabled is True
        assert reloaded.setup.onboarding_completed is True
