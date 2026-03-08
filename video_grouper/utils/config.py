from __future__ import annotations

import configparser
from pathlib import Path
from typing import Dict, Optional, List

from pydantic import BaseModel, Field, RootModel


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
    type: str
    device_ip: str
    username: str
    password: str
    auto_stop_recording: bool = True
    channel: int = 0


class StorageConfig(BaseModel):
    path: str


class RecordingConfig(BaseModel):
    min_duration: int = 60
    max_duration: int = 3600


class ProcessingConfig(BaseModel):
    max_concurrent_downloads: int = 2
    max_concurrent_conversions: int = 1
    retry_attempts: int = 3
    retry_delay: int = 60
    trim_end_enabled: bool = False


class LoggingConfig(BaseModel):
    level: str = "INFO"
    log_dir: str = "logs"
    app_name: str = "video_grouper"
    backup_count: int = 30  # Keep 30 days of logs


class AppConfig(BaseModel):
    check_interval_seconds: int = 60
    timezone: str = "America/New_York"
    update_url: str = "https://updates.videogrouper.com"
    storage_path: Optional[str] = None


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
    plugin_signing_key: str = ""
    plugin_sync_interval: int = 3600


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


class Config(BaseModel):
    camera: CameraConfig = Field(alias="CAMERA")
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

    model_config = {"validate_by_name": True}


def load_config(config_path: Path) -> Config:
    parser = configparser.ConfigParser()
    parser.read(config_path)

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

    # Handle PlayMetrics teams
    playmetrics_teams = []
    for section in list(config_dict.keys()):
        if section.startswith("PLAYMETRICS.TEAM."):
            team_config = config_dict.pop(section)
            playmetrics_teams.append(team_config)

    # Attach teams to main PLAYMETRICS config
    if "PLAYMETRICS" in config_dict:
        config_dict["PLAYMETRICS"]["teams"] = playmetrics_teams

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

    for field_name, field in Config.model_fields.items():
        alias = field.alias if field.alias else field_name
        value = getattr(config, field_name)

        if field_name == "playmetrics_teams":
            for item in value:
                section_name = f"PLAYMETRICS.{item.team_name}"
                item_dict = item.model_dump()
                item_dict.pop("team_name")
                parser[section_name] = {
                    k: str(v) for k, v in item_dict.items() if v is not None
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

                elif sub_value is not None:
                    section_items[sub_alias] = str(sub_value)

            if section_items:
                parser[alias] = section_items

    with config_path.open("w") as f:
        parser.write(f)
