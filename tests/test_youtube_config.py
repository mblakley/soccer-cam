"""
Tests for YouTube configuration functionality, specifically playlist mapping.
"""

import pytest
import tempfile
from pathlib import Path

from video_grouper.utils.config import (
    load_config,
    save_config,
    YouTubeConfig,
    YouTubePlaylistMapConfig,
    Config,
    CameraConfig,
    StorageConfig,
    RecordingConfig,
    ProcessingConfig,
    LoggingConfig,
    AppConfig,
    TeamSnapConfig,
    PlayMetricsConfig,
    NtfyConfig,
    AutocamConfig,
    CloudSyncConfig,
)


class TestYouTubePlaylistMapConfig:
    """Test the YouTubePlaylistMapConfig class."""

    def test_youtube_playlist_map_config_creation(self):
        """Test creating YouTubePlaylistMapConfig with team mappings."""
        mappings = {
            "Team A": "Team A 2024 Season",
            "Team B": "Team B Soccer Videos",
            "U12 Eagles": "Eagles Youth Soccer",
        }

        playlist_map = YouTubePlaylistMapConfig(mappings)

        assert playlist_map.root == mappings
        assert playlist_map.get("Team A") == "Team A 2024 Season"
        assert playlist_map.get("Team B") == "Team B Soccer Videos"
        assert playlist_map.get("U12 Eagles") == "Eagles Youth Soccer"
        assert playlist_map.get("Non-existent Team") is None

    def test_youtube_playlist_map_config_empty(self):
        """Test creating empty YouTubePlaylistMapConfig."""
        playlist_map = YouTubePlaylistMapConfig({})

        assert playlist_map.root == {}
        assert playlist_map.get("Any Team") is None


class TestYouTubeConfig:
    """Test the YouTubeConfig class."""

    def test_youtube_config_with_playlist_map(self):
        """Test YouTubeConfig with playlist mapping."""
        mappings = {"Team A": "Team A Playlist"}
        playlist_map = YouTubePlaylistMapConfig(mappings)

        config = YouTubeConfig(
            enabled=True, privacy_status="unlisted", playlist_map=playlist_map
        )

        assert config.enabled is True
        assert config.privacy_status == "unlisted"
        assert config.playlist_map is not None
        assert config.playlist_map.get("Team A") == "Team A Playlist"
        assert config.playlist_map_dict == mappings

    def test_youtube_config_without_playlist_map(self):
        """Test YouTubeConfig without playlist mapping."""
        config = YouTubeConfig(enabled=False)

        assert config.enabled is False
        assert config.playlist_map is None
        assert config.playlist_map_dict == {}

    def test_youtube_config_defaults(self):
        """Test YouTubeConfig default values."""
        config = YouTubeConfig()

        assert config.enabled is False
        assert config.privacy_status == "private"
        assert config.playlist_mapping == {}
        assert config.processed_playlist is None
        assert config.raw_playlist is None
        assert config.playlist_map is None


class TestConfigLoadingAndSaving:
    """Test loading and saving configuration with YouTube playlist mapping."""

    @pytest.fixture
    def temp_config_file(self):
        """Create a temporary config file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ini", delete=False) as f:
            yield Path(f.name)
        # Clean up after test
        Path(f.name).unlink(missing_ok=True)

    @pytest.fixture
    def sample_config(self, temp_config_file):
        """Create a sample configuration with YouTube playlist mapping."""
        config_content = """
[CAMERA]
type = dahua
device_ip = 192.168.1.100
username = admin
password = password

[STORAGE]
path = /tmp/test

[RECORDING]
min_duration = 60

[PROCESSING]
max_concurrent_downloads = 2

[LOGGING]
level = INFO

[APP]
check_interval_seconds = 60

[TEAMSNAP]
enabled = false

[PLAYMETRICS]
enabled = false

[NTFY]
enabled = false

[YOUTUBE]
enabled = true
privacy_status = unlisted

[YOUTUBE.PLAYLIST_MAP]
Team A = Team A 2024 Season
Team B = Team B Soccer Videos
U12 Eagles = Eagles Youth Soccer
Special Characters = Test & Special < > Characters

[AUTOCAM]
enabled = false

[CLOUD_SYNC]
enabled = false
"""
        temp_config_file.write_text(config_content.strip())
        return temp_config_file

    def test_load_config_with_playlist_map(self, sample_config):
        """Test loading configuration with YouTube playlist mapping."""
        config = load_config(sample_config)

        assert isinstance(config, Config)
        assert config.youtube.enabled is True
        assert config.youtube.privacy_status == "unlisted"
        assert config.youtube.playlist_map is not None

        # Test playlist mappings
        assert config.youtube.playlist_map.get("Team A") == "Team A 2024 Season"
        assert config.youtube.playlist_map.get("Team B") == "Team B Soccer Videos"
        assert config.youtube.playlist_map.get("U12 Eagles") == "Eagles Youth Soccer"
        assert (
            config.youtube.playlist_map.get("Special Characters")
            == "Test & Special < > Characters"
        )
        assert config.youtube.playlist_map.get("Non-existent Team") is None

        # Test playlist_map_dict property (keys will be lowercase due to configparser)
        expected_dict = {
            "team a": "Team A 2024 Season",
            "team b": "Team B Soccer Videos",
            "u12 eagles": "Eagles Youth Soccer",
            "special characters": "Test & Special < > Characters",
        }
        assert config.youtube.playlist_map_dict == expected_dict

    def test_load_config_without_playlist_map(self, temp_config_file):
        """Test loading configuration without YouTube playlist mapping section."""
        config_content = """
[CAMERA]
type = dahua
device_ip = 192.168.1.100
username = admin
password = password

[STORAGE]
path = /tmp/test

[RECORDING]
min_duration = 60

[PROCESSING]
max_concurrent_downloads = 2

[LOGGING]
level = INFO

[APP]
check_interval_seconds = 60

[TEAMSNAP]
enabled = false

[PLAYMETRICS]
enabled = false

[NTFY]
enabled = false

[YOUTUBE]
enabled = true
privacy_status = private

[AUTOCAM]
enabled = false

[CLOUD_SYNC]
enabled = false
"""
        temp_config_file.write_text(config_content.strip())

        config = load_config(temp_config_file)

        assert isinstance(config, Config)
        assert config.youtube.enabled is True
        assert config.youtube.privacy_status == "private"
        assert config.youtube.playlist_map is None
        assert config.youtube.playlist_map_dict == {}

    def test_save_and_reload_config_with_playlist_map(self, temp_config_file):
        """Test saving and reloading configuration with YouTube playlist mapping."""
        # Create a config with playlist mapping
        mappings = {"Team A": "Team A Videos", "Team B": "Team B Highlights"}
        playlist_map = YouTubePlaylistMapConfig(mappings)

        original_config = Config(
            camera=CameraConfig(
                type="dahua",
                device_ip="192.168.1.100",
                username="admin",
                password="password",
            ),
            storage=StorageConfig(path="/tmp/test"),
            recording=RecordingConfig(),
            processing=ProcessingConfig(),
            logging=LoggingConfig(),
            app=AppConfig(),
            teamsnap=TeamSnapConfig(),
            playmetrics=PlayMetricsConfig(),
            ntfy=NtfyConfig(),
            youtube=YouTubeConfig(
                enabled=True, privacy_status="unlisted", playlist_map=playlist_map
            ),
            autocam=AutocamConfig(),
            cloud_sync=CloudSyncConfig(),
        )

        # Save the config
        save_config(original_config, temp_config_file)

        # Reload the config
        reloaded_config = load_config(temp_config_file)

        # Verify the playlist mapping was preserved
        assert reloaded_config.youtube.playlist_map is not None
        assert reloaded_config.youtube.playlist_map.get("Team A") == "Team A Videos"
        assert reloaded_config.youtube.playlist_map.get("Team B") == "Team B Highlights"
        # Keys will be lowercase when reloaded from configparser
        expected_mappings = {"team a": "Team A Videos", "team b": "Team B Highlights"}
        assert reloaded_config.youtube.playlist_map_dict == expected_mappings

    def test_config_with_empty_playlist_map_section(self, temp_config_file):
        """Test configuration with empty YOUTUBE.PLAYLIST_MAP section."""
        config_content = """
[CAMERA]
type = dahua
device_ip = 192.168.1.100
username = admin
password = password

[STORAGE]
path = /tmp/test

[RECORDING]
min_duration = 60

[PROCESSING]
max_concurrent_downloads = 2

[LOGGING]
level = INFO

[APP]
check_interval_seconds = 60

[TEAMSNAP]
enabled = false

[PLAYMETRICS]
enabled = false

[NTFY]
enabled = false

[YOUTUBE]
enabled = true

[YOUTUBE.PLAYLIST_MAP]
# Empty section

[AUTOCAM]
enabled = false

[CLOUD_SYNC]
enabled = false
"""
        temp_config_file.write_text(config_content.strip())

        config = load_config(temp_config_file)

        assert isinstance(config, Config)
        assert config.youtube.playlist_map is not None
        assert config.youtube.playlist_map_dict == {}


if __name__ == "__main__":
    pytest.main([__file__])
