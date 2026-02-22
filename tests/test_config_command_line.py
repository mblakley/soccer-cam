import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from video_grouper.__main__ import parse_arguments, load_application_config
from video_grouper.utils.config import Config


class TestCommandLineArguments:
    """Test command line argument parsing."""

    def test_parse_arguments_no_config(self):
        """Test parsing arguments with no config specified."""
        with patch("sys.argv", ["video_grouper"]):
            args = parse_arguments()
            assert args.config is None

    def test_parse_arguments_with_config(self):
        """Test parsing arguments with config specified."""
        with patch("sys.argv", ["video_grouper", "--config", "/path/to/config.ini"]):
            args = parse_arguments()
            assert args.config == "/path/to/config.ini"

    def test_parse_arguments_help(self):
        """Test that help is displayed correctly."""
        with patch("sys.argv", ["video_grouper", "--help"]):
            with pytest.raises(SystemExit):
                parse_arguments()


class TestConfigLoading:
    """Test configuration loading with custom paths."""

    def test_load_application_config_nonexistent_file(self):
        """Test loading config from a nonexistent file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            nonexistent_path = Path(tmpdir) / "does_not_exist.ini"
            with patch("video_grouper.__main__.logger") as mock_logger:
                config = load_application_config(nonexistent_path)
                assert config is None
                mock_logger.error.assert_called()

    def test_load_application_config_default_path(self):
        """Test loading config from default path."""
        with (
            patch("video_grouper.__main__.get_shared_data_path") as mock_get_path,
            patch("video_grouper.__main__.load_config") as mock_load_config,
            patch("video_grouper.__main__.FileLock") as mock_filelock,
            patch("pathlib.Path.exists", return_value=True),
        ):
            # Mock the FileLock context manager
            mock_lock_instance = MagicMock()
            mock_filelock.return_value = mock_lock_instance

            mock_get_path.return_value = Path("/default/shared/data")
            mock_config = MagicMock(spec=Config)
            mock_load_config.return_value = mock_config

            config = load_application_config()

            assert config == mock_config
            mock_load_config.assert_called_once_with(
                Path("/default/shared/data/config.ini")
            )


class TestMainFunction:
    """Test the main function with command line arguments."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock config object."""
        config = MagicMock(spec=Config)
        # Add required attributes
        config.storage = MagicMock()
        config.storage.path = "/custom/storage/path"
        config.camera = MagicMock()
        config.camera.device_ip = "192.168.1.100"
        config.camera.username = "admin"
        config.app = MagicMock()
        config.app.update_url = "https://updates.videogrouper.com"
        return config

    @pytest.mark.asyncio
    @patch("video_grouper.__main__.VideoGrouperApp")
    @patch("video_grouper.__main__.load_application_config")
    @patch("video_grouper.__main__.parse_arguments")
    async def test_main_with_custom_config(
        self, mock_parse_args, mock_load_config, mock_app_class, mock_config
    ):
        """Test main function with custom config path."""
        # Setup mocks
        mock_args = MagicMock()
        mock_args.config = "C:/custom/config.ini"  # Use Windows-style path
        mock_parse_args.return_value = mock_args

        mock_load_config.return_value = mock_config

        # Create a mock app with async methods
        mock_app = MagicMock()
        mock_app.run = AsyncMock()
        mock_app.shutdown = AsyncMock()
        mock_app_class.return_value = mock_app

        # Import and run main
        from video_grouper.__main__ import main

        await main()

        # Verify calls
        mock_parse_args.assert_called_once()
        mock_load_config.assert_called_once_with(Path("C:/custom/config.ini"))
        mock_app_class.assert_called_once_with(mock_config)
        mock_app.run.assert_called_once()
        # shutdown is called by app.run()'s finally block, not by main()
        mock_app.shutdown.assert_not_called()

    @pytest.mark.asyncio
    @patch("video_grouper.__main__.VideoGrouperApp")
    @patch("video_grouper.__main__.load_application_config")
    @patch("video_grouper.__main__.parse_arguments")
    async def test_main_with_default_config(
        self, mock_parse_args, mock_load_config, mock_app_class, mock_config
    ):
        """Test main function with default config path."""
        # Setup mocks
        mock_args = MagicMock()
        mock_args.config = None
        mock_parse_args.return_value = mock_args

        mock_load_config.return_value = mock_config

        # Create a mock app with async methods
        mock_app = MagicMock()
        mock_app.run = AsyncMock()
        mock_app.shutdown = AsyncMock()
        mock_app_class.return_value = mock_app

        # Import and run main
        from video_grouper.__main__ import main

        await main()

        # Verify calls
        mock_parse_args.assert_called_once()
        mock_load_config.assert_called_once_with(None)
        mock_app_class.assert_called_once_with(mock_config)
        mock_app.run.assert_called_once()
        # shutdown is called by app.run()'s finally block, not by main()
        mock_app.shutdown.assert_not_called()

    @pytest.mark.asyncio
    @patch("video_grouper.__main__.load_application_config")
    @patch("video_grouper.__main__.parse_arguments")
    async def test_main_with_failed_config_load(
        self, mock_parse_args, mock_load_config
    ):
        """Test main function when config loading fails."""
        # Setup mocks
        mock_args = MagicMock()
        mock_args.config = None
        mock_parse_args.return_value = mock_args

        mock_load_config.return_value = None

        # Import and run main
        from video_grouper.__main__ import main

        result = await main()

        # Verify result
        assert result is None


class TestIntegration:
    """Test integration with real config files."""

    def test_load_application_config_with_real_config(self):
        """Test loading config from a real config file in a real temp directory."""
        config_content = """
[CAMERA]
type = dahua
device_ip = 192.168.1.100
username = admin
password = admin

[STORAGE]
path = /custom/storage/path

[RECORDING]
# Duration in seconds
min_duration = 60
max_duration = 3600

[PROCESSING]
max_concurrent_downloads = 2
max_concurrent_conversions = 1
retry_attempts = 3
retry_delay = 60

[LOGGING]
level = INFO
log_dir = logs
app_name = video_grouper
backup_count = 30

[APP]
check_interval_seconds = 60
timezone = America/New York

[TEAMSNAP]
# Set to true to enable TeamSnap integration
enabled = false
# OAuth credentials (get these from TeamSnap developer portal)
client_id = test_client_id
client_secret = test_client_secret
# Access token
access_token = test_access_token
# Your team ID from TeamSnap
team_id = test_team_id
# Your team name as it should appear in video titles
my_team_name = Test Team Name

[PLAYMETRICS]
# Set to true to enable PlayMetrics integration
enabled = false
# Your PlayMetrics login credentials
username = test@example.com
password = test_password
# Your team ID from PlayMetrics (optional, will try to find from dashboard if not provided)
team_id = test_team_id
# Your team name as it should appear in video titles (optional, will try to extract from PlayMetrics)
team_name = Test Team Name

[NTFY]
# Set to true to enable NTFY integration for game start/end time detection
enabled = false
# NTFY server URL (default is ntfy.sh)
server_url = https://ntfy.sh
# Topic name for notifications (if not provided, a random one will be generated)
# Use a unique, hard-to-guess topic name for security
topic = test-unique-soccer-cam-topic

[YOUTUBE]
# If you want to upload videos to YouTube, you need to set up a project in the
# Google Cloud Console and get a client_secrets.json file.
# See docs/youtube/README.md for more details.
# To enable, set to true
enabled = false
# The privacy status of the video.
# Valid values are: public, private, unlisted
privacy_status = private

[YOUTUBE_PLAYLIST_MAPPING]
# This section allows you to map a team name (from match_info.ini's my_team_name)
# to a specific YouTube playlist name.
#
# The value on the right is the base playlist name for processed videos.
# Raw videos will be uploaded to a playlist with " - Full Field" appended.
#
# Example:
# Hilton Heat=Hilton Heat 2012s
# WNY Flash=WNY Flash 2013s
#
# With this config:
# - Processed videos for "Hilton Heat" go to "Hilton Heat 2012s"
# - Raw videos for "Hilton Heat" go to "Hilton Heat 2012s - Full Field"
Default=Test Playlist

[AUTOCAM]
# Whether to enable autocam processing
enabled = true

# The path to the autocam executable
executable = /path/to/autocam

# Playlist configuration for processed videos
[YOUTUBE.PLAYLIST.PROCESSED]
# Format string for playlist name. Available variables: {my_team_name}, {opponent_team_name}, {location}
name_format = {my_team_name} 2013s
# Description for the playlist
description = Processed videos for {my_team_name} 2013s team
# Privacy status for the playlist (private, unlisted, public)
privacy_status = unlisted

# Playlist configuration for raw videos
[YOUTUBE.PLAYLIST.RAW]
# Format string for playlist name. Available variables: {my_team_name}, {opponent_team_name}, {location}
name_format = {my_team_name} 2013s - Full Field
# Description for the playlist
description = Raw full field videos for {my_team_name} 2013s team
# Privacy status for the playlist (private, unlisted, public)
privacy_status = unlisted

[CLOUD_SYNC]
enabled = false
provider = google
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.ini"
            with open(config_path, "w") as f:
                f.write(config_content.strip())
            config = load_application_config(config_path)
            assert config is not None
            assert isinstance(config, Config)
            assert config.storage.path == "/custom/storage/path"
            assert config.camera.device_ip == "192.168.1.100"
            assert config.camera.username == "admin"
