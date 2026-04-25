from __future__ import annotations

import configparser
from pathlib import Path
from typing import Dict, Optional, List

from pydantic import BaseModel, Field, RootModel

from video_grouper.ball_tracking.config import BallTrackingConfig


# Monkey-patch ConfigParser to allow attribute-style access to sections used by tests
if not hasattr(configparser.ConfigParser, "__getattr__"):

    def _section_getattr(self, name):
        section = name.upper()
        if not self.has_section(section):
            # Lazy-create the section so attributes can be set later
            if section != "DEFAULT":
                self.add_section(section)
        return _SectionAccessor(self, section)

    def _section_setattr(self, name, value):
        # Allow setting attributes on sections directly
        section = name.upper()
        if not isinstance(value, (str, int, float, bool)):
            # Fallback to normal behaviour for internal attributes
            return object.__setattr__(self, name, value)
        if not self.has_section(section) and section != "DEFAULT":
            self.add_section(section)
        self.set(section, name, str(value))

    class _SectionAccessor:
        def __init__(self, parser, section):
            self._parser = parser
            self._section = section

        def __getattr__(self, key):
            return self._parser.get(self._section, key, fallback=None)

        def __setattr__(self, key, value):
            if key in ("_parser", "_section"):
                return object.__setattr__(self, key, value)
            self._parser.set(self._section, key, str(value))

    configparser.ConfigParser.__getattr__ = _section_getattr


class CameraConfig(BaseModel):
    name: str
    type: str
    device_ip: str
    username: str
    password: str
    channel: int = 0
    baichuan_port: int = 9000
    http_port: int = 80
    enabled: bool = True
    serial: str = ""


class StorageConfig(BaseModel):
    path: str
    min_free_gb: float = 2.0


class RecordingConfig(BaseModel):
    min_duration: int = 60
    max_duration: int = 3600


class ProcessingConfig(BaseModel):
    max_concurrent_downloads: int = 2
    max_concurrent_conversions: int = 1
    retry_attempts: int = 3
    retry_delay: int = 60
    trim_end_enabled: bool = False
    ffmpeg_timeout_seconds: int = 1800
    seam_realign_enabled: bool = False
    seam_realign_profile_path: Optional[str] = None


class LoggingConfig(BaseModel):
    level: str = "INFO"
    log_dir: str = "logs"
    app_name: str = "video_grouper"
    backup_count: int = 30  # Keep 30 days of logs


class AppConfig(BaseModel):
    check_interval_seconds: int = 60
    timezone: str = "America/New_York"
    github_repo: str = "mblakley/soccer-cam"
    storage_path: Optional[str] = None
    max_lookback_hours: int = 48
    max_files_per_poll: int = 50
    recording_end_date: Optional[str] = None


class TeamSnapTeamConfig(BaseModel):
    enabled: bool = False
    team_id: Optional[str] = None
    team_name: str


class TeamSnapConfig(BaseModel):
    enabled: bool = False
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    teams: list[TeamSnapTeamConfig] = Field(default_factory=list)

    # Legacy single-team fields (kept optional for backward compatibility)
    team_id: Optional[str] = None
    team_name: Optional[str] = None
    my_team_name: Optional[str] = None

    def __init__(self, **data):
        super().__init__(**data)

        # If legacy fields are supplied and `teams` is empty, populate it so
        # that newer code paths which expect `teams` continue to work.
        if not self.teams and (self.team_id or self.team_name or self.my_team_name):
            team_name = self.team_name or self.my_team_name or "Default"
            self.teams.append(
                TeamSnapTeamConfig(
                    team_id=self.team_id, team_name=team_name, enabled=True
                )
            )


class PlayMetricsTeamConfig(BaseModel):
    team_id: Optional[str] = None
    team_name: Optional[str] = None
    enabled: bool = True


class PlayMetricsConfig(BaseModel):
    """Configuration for PlayMetrics integration.

    Historically the configuration accepted top-level ``team_id`` and
    ``team_name`` attributes.  The newer schema supports multiple teams via
    the ``teams`` list.  To remain backward-compatible with existing tests and
    user configurations we expose the legacy attributes as optional fields
    and, when provided, automatically inject them into the ``teams`` list to
    ensure uniform downstream handling.
    """

    enabled: bool = False
    username: Optional[str] = None
    password: Optional[str] = None

    # Legacy single-team fields (kept optional for backward compatibility)
    team_id: Optional[str] = None
    team_name: Optional[str] = None

    teams: List[PlayMetricsTeamConfig] = Field(default_factory=list)

    def __init__(self, **data):
        super().__init__(**data)

        # If legacy fields are supplied and `teams` is empty, populate it so
        # that newer code paths which expect `teams` continue to work.
        if not self.teams and (self.team_id or self.team_name):
            self.teams.append(
                PlayMetricsTeamConfig(
                    team_id=self.team_id, team_name=self.team_name, enabled=True
                )
            )


class NtfyConfig(BaseModel):
    enabled: bool = False
    server_url: str = "https://ntfy.sh"
    topic: Optional[str] = None
    response_service: bool = False
    auto_respond: bool = False
    unplug_notification: bool = True


class AutocamConfig(BaseModel):
    enabled: bool = True
    executable: Optional[str] = None


class CloudSyncConfig(BaseModel):
    enabled: bool = False
    provider: Optional[str] = None


class TTTConfig(BaseModel):
    """Configuration for Team Tech Tools integration (clip request automation)."""

    enabled: bool = False
    supabase_url: str = ""
    anon_key: str = ""
    api_base_url: str = ""
    email: str = ""
    password: str = ""
    clip_request_poll_interval: int = 60
    google_drive_folder_id: str = ""
    # Hex-encoded Ed25519 public keys accepted for plugin signature verification.
    # List form lets operators rotate keys by adding a new one, shipping a release,
    # then later removing the old one.
    plugin_signing_public_keys: list[str] = [
        "a0c4e0e103b82c60e567b8521cbff1778e4947116db17f081228b1eeafa899f2",
    ]
    # Re-download a plugin's signed manifest when within this many days of expiry.
    plugin_refresh_headroom_days: int = 7
    plugin_sync_interval: int = 3600
    camera_id: str = ""
    ttt_sync_enabled: bool = False
    heartbeat_interval: int = 30
    job_polling_enabled: bool = False
    job_poll_interval: int = 30
    machine_name: str = ""


class MomentTaggingConfig(BaseModel):
    enabled: bool = False
    api_base_url: str = "http://localhost:8000"
    service_role_key: str = ""


class YouTubePlaylistConfig(BaseModel):
    name_format: str
    description: str
    privacy_status: str


class YouTubePlaylistMapConfig(RootModel[Dict[str, str]]):
    def get(self, team_name: str) -> Optional[str]:
        # First try exact match
        if team_name in self.root:
            return self.root[team_name]

        # Then try case-insensitive match
        team_name_lower = team_name.lower()
        for key, value in self.root.items():
            if key.lower() == team_name_lower:
                return value

        return None


class YouTubeConfig(BaseModel):
    enabled: bool = False
    privacy_status: str = "private"
    use_mock: bool = False
    processed_playlist: Optional[YouTubePlaylistConfig] = None
    raw_playlist: Optional[YouTubePlaylistConfig] = None
    playlist_map: Optional[YouTubePlaylistMapConfig] = None

    model_config = {"validate_by_name": True}

    @property
    def playlist_map_dict(self) -> Dict[str, str]:
        return self.playlist_map.root if self.playlist_map else {}


class SetupConfig(BaseModel):
    """Tracks onboarding wizard completion state."""

    onboarding_completed: bool = False


class Config(BaseModel):
    cameras: list[CameraConfig] = Field(default_factory=list)
    storage: StorageConfig = Field(alias="STORAGE")
    recording: RecordingConfig = Field(alias="RECORDING")
    processing: ProcessingConfig = Field(alias="PROCESSING")
    logging: LoggingConfig = Field(alias="LOGGING")
    app: AppConfig = Field(alias="APP")
    teamsnap: TeamSnapConfig = Field(alias="TEAMSNAP")
    playmetrics: PlayMetricsConfig = Field(alias="PLAYMETRICS")
    ntfy: NtfyConfig = Field(alias="NTFY")
    youtube: YouTubeConfig = Field(alias="YOUTUBE")
    autocam: AutocamConfig = Field(alias="AUTOCAM")
    cloud_sync: CloudSyncConfig = Field(alias="CLOUD_SYNC")
    ttt: TTTConfig = Field(alias="TTT", default_factory=TTTConfig)
    moment_tagging: MomentTaggingConfig = Field(
        default_factory=MomentTaggingConfig, alias="MOMENT_TAGGING"
    )
    setup: SetupConfig = Field(alias="SETUP", default_factory=SetupConfig)
    ball_tracking: BallTrackingConfig = Field(
        alias="BALL_TRACKING", default_factory=BallTrackingConfig
    )

    model_config = {"validate_by_name": True}

    @property
    def camera(self) -> CameraConfig:
        """Convenience accessor for the first camera config."""
        return self.cameras[0]


def load_config(config_path: Path) -> Config:
    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")

    config_dict = {s: dict(parser.items(s)) for s in parser.sections()}

    # pydantic doesn't natively handle dot-separated sections from configparser
    # so we need to manually nest them.
    if "YOUTUBE.PLAYLIST.PROCESSED" in config_dict:
        config_dict.setdefault("YOUTUBE", {})["processed_playlist"] = config_dict.pop(
            "YOUTUBE.PLAYLIST.PROCESSED"
        )
    if "YOUTUBE.PLAYLIST.RAW" in config_dict:
        config_dict.setdefault("YOUTUBE", {})["raw_playlist"] = config_dict.pop(
            "YOUTUBE.PLAYLIST.RAW"
        )

    # Add support for YOUTUBE.PLAYLIST_MAP
    if "YOUTUBE.PLAYLIST_MAP" in config_dict:
        config_dict.setdefault("YOUTUBE", {})["playlist_map"] = (
            YouTubePlaylistMapConfig(config_dict.pop("YOUTUBE.PLAYLIST_MAP"))
        )

    # Handle BALL_TRACKING sub-sections (provider configs + per-team overrides).
    # `[BALL_TRACKING.AUTOCAM_GUI]` -> nested under BALL_TRACKING.AUTOCAM_GUI
    # `[BALL_TRACKING.PER_TEAM]` -> dict of team_name -> provider_name
    for section in list(config_dict.keys()):
        if section.startswith("BALL_TRACKING."):
            sub_alias = section.split(".", 1)[1]  # e.g. "AUTOCAM_GUI" or "PER_TEAM"
            sub_value = config_dict.pop(section)
            config_dict.setdefault("BALL_TRACKING", {})[sub_alias] = sub_value

    # Handle PlayMetrics teams
    playmetrics_teams = []
    for section in list(config_dict.keys()):
        if section.startswith("PLAYMETRICS.TEAM."):
            team_config = config_dict.pop(section)
            playmetrics_teams.append(team_config)

    # Attach teams to main PLAYMETRICS config
    if "PLAYMETRICS" in config_dict:
        config_dict["PLAYMETRICS"]["teams"] = playmetrics_teams

    # Handle camera sections: [CAMERA.name] -> cameras list
    cameras = []
    for section in list(config_dict.keys()):
        if section.startswith("CAMERA."):
            camera_name = section.split(".", 1)[1]
            camera_config = config_dict.pop(section)
            camera_config["name"] = camera_name
            cameras.append(camera_config)
    config_dict["cameras"] = cameras

    # Handle TeamSnap teams
    teamsnap_teams = []
    for section in list(config_dict.keys()):
        if section.startswith("TEAMSNAP."):
            team_config = config_dict.pop(section)
            # Use the team_name from the config, not the section name
            team_name = team_config.get("team_name", section.split(".", 1)[1])
            team_config["team_name"] = team_name
            teamsnap_teams.append(team_config)

    # Add teams to the main teamsnap config
    if "TEAMSNAP" in config_dict:
        config_dict["TEAMSNAP"]["teams"] = teamsnap_teams

    return Config.model_validate(config_dict)


def save_config(config: Config, config_path: Path):
    parser = configparser.ConfigParser()

    # Write camera sections as [CAMERA.name]
    for cam in config.cameras:
        section_name = f"CAMERA.{cam.name}"
        cam_dict = cam.model_dump()
        cam_dict.pop("name")
        parser[section_name] = {k: str(v) for k, v in cam_dict.items() if v is not None}

    for field_name, field in Config.model_fields.items():
        alias = field.alias if field.alias else field_name
        value = getattr(config, field_name)

        # cameras are handled above
        if field_name == "cameras":
            continue

        if field_name == "playmetrics" and hasattr(value, "teams"):
            # Write each PlayMetrics team as its own [PLAYMETRICS.TEAM.N] section
            # so load_config's section.startswith("PLAYMETRICS.TEAM.") check
            # picks them up.
            for index, item in enumerate(value.teams):
                section_name = f"PLAYMETRICS.TEAM.{index}"
                parser[section_name] = {
                    k: str(v) for k, v in item.model_dump().items() if v is not None
                }
            # Main [PLAYMETRICS] section: credentials + enabled, but not teams
            playmetrics_dict = value.model_dump()
            playmetrics_dict.pop("teams", None)
            parser[alias] = {
                k: str(v) for k, v in playmetrics_dict.items() if v is not None
            }
            continue

        if field_name == "teamsnap" and hasattr(value, "teams"):
            # Handle teamsnap teams
            for item in value.teams:
                section_name = f"TEAMSNAP.{item.team_name}"
                item_dict = item.model_dump()
                item_dict.pop("team_name")
                parser[section_name] = {
                    k: str(v) for k, v in item_dict.items() if v is not None
                }
            # Add main teamsnap config without teams
            teamsnap_dict = value.model_dump()
            teamsnap_dict.pop("teams")
            parser["TEAMSNAP"] = {
                k: str(v) for k, v in teamsnap_dict.items() if v is not None
            }
            continue

        if isinstance(value, BaseModel):
            section_items = {}
            for sub_field_name, sub_field in type(value).model_fields.items():
                sub_alias = sub_field.alias if sub_field.alias else sub_field_name
                sub_value = getattr(value, sub_field_name)

                if isinstance(sub_value, BaseModel):
                    nested_section_name = f"{alias}.{sub_alias.upper()}"
                    parser[nested_section_name] = {
                        k: str(v)
                        for k, v in sub_value.model_dump().items()
                        if v is not None
                    }

                elif isinstance(sub_value, dict):
                    # Non-empty dict[str, str] -> [PARENT.SUBALIAS] sub-section.
                    # Empty dicts are skipped so reload doesn't see "{}" as a string.
                    if sub_value:
                        nested_section_name = f"{alias}.{sub_alias.upper()}"
                        parser[nested_section_name] = {
                            k: str(v) for k, v in sub_value.items()
                        }

                elif sub_value is not None:
                    section_items[sub_alias] = str(sub_value)

            if section_items:
                parser[alias] = section_items

    with config_path.open("w", encoding="utf-8") as f:
        parser.write(f)


def create_default_config(config_path: Path, storage_path: str) -> Config:
    """Create a minimal config with sensible defaults and save to disk.

    Used by the onboarding wizard to bootstrap a new config.ini.
    """
    config = Config.model_validate(
        {
            "cameras": [],
            "STORAGE": {"path": storage_path},
            "RECORDING": {},
            "PROCESSING": {},
            "LOGGING": {},
            "APP": {},
            "TEAMSNAP": {},
            "PLAYMETRICS": {},
            "NTFY": {},
            "YOUTUBE": {},
            "AUTOCAM": {"enabled": False},
            "CLOUD_SYNC": {},
            "TTT": {},
            "SETUP": {"onboarding_completed": False},
        }
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    save_config(config, config_path)
    return config


def config_needs_onboarding(config_path: Path) -> bool:
    """Check if the config file exists but onboarding hasn't been completed."""
    if not config_path.exists():
        return True
    try:
        config = load_config(config_path)
        return not config.setup.onboarding_completed
    except Exception:
        return True
