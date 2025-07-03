from __future__ import annotations

import configparser
from pathlib import Path
from typing import Dict, Optional

from pydantic import BaseModel, Field


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


class LoggingConfig(BaseModel):
    level: str = "INFO"
    log_file: str = "logs/video_grouper.log"
    max_log_size: int = 10485760
    backup_count: int = 5


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


class PlayMetricsConfig(BaseModel):
    enabled: bool = False
    username: Optional[str] = None
    password: Optional[str] = None
    team_id: Optional[str] = None
    team_name: Optional[str] = None


class PlayMetricsTeamConfig(PlayMetricsConfig):
    team_name: str


class NtfyConfig(BaseModel):
    enabled: bool = False
    server_url: str = "https://ntfy.sh"
    topic: Optional[str] = None


class AutocamConfig(BaseModel):
    enabled: bool = True
    executable: Optional[str] = None


class CloudSyncConfig(BaseModel):
    enabled: bool = False
    provider: Optional[str] = None


class YouTubePlaylistConfig(BaseModel):
    name_format: str
    description: str
    privacy_status: str


class YouTubeConfig(BaseModel):
    enabled: bool = False
    privacy_status: str = "private"
    playlist_mapping: Dict[str, str] = Field(default_factory=dict)
    processed_playlist: Optional[YouTubePlaylistConfig] = None
    raw_playlist: Optional[YouTubePlaylistConfig] = None

    model_config = {"validate_by_name": True}


class Config(BaseModel):
    camera: CameraConfig = Field(alias="CAMERA")
    storage: StorageConfig = Field(alias="STORAGE")
    recording: RecordingConfig = Field(alias="RECORDING")
    processing: ProcessingConfig = Field(alias="PROCESSING")
    logging: LoggingConfig = Field(alias="LOGGING")
    app: AppConfig = Field(alias="APP")
    teamsnap: TeamSnapConfig = Field(alias="TEAMSNAP")
    playmetrics: PlayMetricsConfig = Field(alias="PLAYMETRICS")
    playmetrics_teams: list[PlayMetricsTeamConfig] = Field(default_factory=list)
    ntfy: NtfyConfig = Field(alias="NTFY")
    youtube: YouTubeConfig = Field(alias="YOUTUBE")
    autocam: AutocamConfig = Field(alias="AUTOCAM")
    cloud_sync: CloudSyncConfig = Field(alias="CLOUD_SYNC")

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
    if "YOUTUBE_PLAYLIST_MAPPING" in config_dict:
        config_dict.setdefault("YOUTUBE", {})["playlist_mapping"] = config_dict.pop(
            "YOUTUBE_PLAYLIST_MAPPING"
        )

    # Handle PlayMetrics teams
    playmetrics_teams = []
    for section in list(config_dict.keys()):
        if section.startswith("PLAYMETRICS."):
            team_config = config_dict.pop(section)
            team_name = section.split(".", 1)[1]
            team_config["team_name"] = team_name
            playmetrics_teams.append(team_config)

    config_dict["playmetrics_teams"] = playmetrics_teams

    # Handle TeamSnap teams
    teamsnap_teams = []
    for section in list(config_dict.keys()):
        if section.startswith("TEAMSNAP."):
            team_config = config_dict.pop(section)
            team_name = section.split(".", 1)[1]
            team_config["team_name"] = team_name
            teamsnap_teams.append(team_config)

    # Add teams to the main teamsnap config
    if "TEAMSNAP" in config_dict:
        config_dict["TEAMSNAP"]["teams"] = teamsnap_teams

    return Config.model_validate(config_dict)


def save_config(config: Config, config_path: Path):
    parser = configparser.ConfigParser()

    for field_name, field in config.model_fields.items():
        alias = field.alias if field.alias else field_name
        value = getattr(config, field_name)

        if field_name == "playmetrics_teams":
            for item in value:
                section_name = f"PLAYMETRICS.{item.team_name}"
                item_dict = item.dict()
                item_dict.pop("team_name")
                parser[section_name] = {
                    k: str(v) for k, v in item_dict.items() if v is not None
                }
            continue

        if field_name == "teamsnap" and hasattr(value, "teams"):
            # Handle teamsnap teams
            for item in value.teams:
                section_name = f"TEAMSNAP.{item.team_name}"
                item_dict = item.dict()
                item_dict.pop("team_name")
                parser[section_name] = {
                    k: str(v) for k, v in item_dict.items() if v is not None
                }
            # Add main teamsnap config without teams
            teamsnap_dict = value.dict()
            teamsnap_dict.pop("teams")
            parser["TEAMSNAP"] = {
                k: str(v) for k, v in teamsnap_dict.items() if v is not None
            }
            continue

        if isinstance(value, BaseModel):
            section_items = {}
            for sub_field_name, sub_field in value.model_fields.items():
                sub_alias = sub_field.alias if sub_field.alias else sub_field_name
                sub_value = getattr(value, sub_field_name)

                if isinstance(sub_value, BaseModel):
                    nested_section_name = f"{alias}.{sub_alias.upper()}"
                    parser[nested_section_name] = {
                        k: str(v) for k, v in sub_value.dict().items() if v is not None
                    }
                elif sub_field_name == "playlist_mapping":
                    parser["YOUTUBE_PLAYLIST_MAPPING"] = {
                        k: str(v) for k, v in sub_value.items() if v is not None
                    }
                elif sub_value is not None:
                    section_items[sub_alias] = str(sub_value)

            if section_items:
                parser[alias] = section_items

    with config_path.open("w") as f:
        parser.write(f)
