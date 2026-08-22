"""
Tests for YouTube configuration functionality, specifically playlist mapping.
"""

import tempfile
from pathlib import Path

import pytest

from video_grouper.utils.config import (
    YouTubeConfig,
    load_config,
    save_config,
)


class TestYouTubeConfig:
    """Test the YouTubeConfig class."""

    def test_youtube_config_defaults(self):
        """Test YouTubeConfig default values."""
        config = YouTubeConfig()

        assert config.enabled is False
        assert config.privacy_status == "private"
        assert config.processed_playlist is None
        assert config.raw_playlist is None


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

    def test_playlist_map_section_is_migrated_into_teams(self, sample_config):
        """[YOUTUBE.PLAYLIST_MAP] is schema v2 territory now.

        It had its own substring matching rule, which is exactly the
        fragmentation [TEAM.<key>] removes. After migration the playlists live
        on the teams and resolve through the shared resolver.
        """
        from video_grouper.utils.config_migrations import migrate_config_file

        migrate_config_file(sample_config)
        config = load_config(sample_config)

        assert config.team_for("Team A").youtube_playlist == "Team A 2024 Season"
        assert config.team_for("U12 Eagles").youtube_playlist == "Eagles Youth Soccer"

    def test_the_section_stops_being_honoured_once_removed(self, sample_config):
        """Loading WITHOUT migrating must not quietly half-work.

        The field is gone from the model, so an unmigrated file simply has no
        playlists — better than a config key that looks live and is ignored.
        """
        config = load_config(sample_config)
        assert config.teams == {}
        assert not hasattr(config.youtube, "playlist_map")

    def test_youtube_scalars_still_round_trip(self, temp_config_file):
        temp_config_file.write_text(
            "[STORAGE]\npath = /tmp/test\n\n"
            "[YOUTUBE]\nenabled = true\nprivacy_status = unlisted\n",
            encoding="utf-8",
        )
        config = load_config(temp_config_file)
        assert config.youtube.enabled is True
        assert config.youtube.privacy_status == "unlisted"

        save_config(config, temp_config_file)
        reloaded = load_config(temp_config_file)
        assert reloaded.youtube.privacy_status == "unlisted"
