from __future__ import annotations

import configparser
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from video_grouper.pipeline.config import PipelineConfig
from video_grouper.utils.config_migrations import current_schema_version

logger = logging.getLogger(__name__)

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
        if not isinstance(value, str | int | float | bool):
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
    # Reolink download protocol selection. "auto" probes HTTP first and
    # falls back to Baichuan; "http" requires patched-firmware HTTP and
    # fails the download rather than falling back; "baichuan" skips the
    # HTTP probe entirely. Mixed-protocol downloads in a single session
    # have produced reproducible AutoCam wedges at the protocol-switch
    # GOP boundary (observed 2026-05-30 Fairport), so locking to a
    # single protocol per game is the safer default for tournament use.
    download_protocol: Literal["auto", "http", "baichuan"] = "auto"


class SchemaConfig(BaseModel):
    """Which config schema this file is written in. Machine-owned.

    Must be a real field on ``Config``: ``save_config`` rebuilds the file from
    ``Config.model_fields`` onto an empty parser, so a section the model does
    not know about is erased on the user's next ``/config`` save. A version
    that vanished would make an already-migrated file look unversioned and get
    migrated a second time.

    Defaults to the current version, so anything soccer-cam writes itself — a
    fresh install, the wizard — is stamped current and never migrated. Only a
    file predating versioning reads as 0.

    Because of that default, this field does NOT tell you what version a file
    on disk is at: an unmigrated file has no ``[SCHEMA]`` section and still
    reports current here. Version detection reads the raw parsed sections
    instead — see :func:`config_migrations.read_version`. Nothing outside the
    migration framework should need to ask.
    """

    version: int = Field(default_factory=current_schema_version)


class TeamConfig(BaseModel):
    """Everything about one team, in one place.

    Per-team settings used to be spread across ``[TEAMSNAP.*]``,
    ``[PLAYMETRICS.TEAM.*]``, ``[YOUTUBE.PLAYLIST_MAP]``,
    ``[PIPELINE.PER_TEAM]`` and ``[BALL_TRACKING.PER_TEAM]``, and the same team
    was spelled differently in each — on one real install, ``BU14 - Guzzetta``
    in TeamSnap and ``guzzetta`` in the playlist map. There was no canonical
    team identity anywhere, and each subsystem invented its own matching rule.

    Spelled ``[TEAM.<key>]``, where ``<key>`` is a short handle you choose. The
    key is never matched against anything; ``name`` and ``aliases`` are.
    """

    # As it appears in a game's match_info.ini [MATCH] my_team_name. That is
    # what every lookup is given at runtime, so it is what we match on.
    name: str = ""
    # Extra spellings that also identify this team. A short alias works
    # because matching falls back to substring — which is the only reason the
    # existing playlist map resolves "guzzetta" against "BU14 - Guzzetta".
    aliases: list[str] = Field(default_factory=list)
    enabled: bool = True

    teamsnap_team_id: str = ""
    playmetrics_team_id: str = ""
    youtube_playlist: str = ""
    # Pipeline preset or step-list override for this team's games.
    pipeline: str = ""

    @field_validator("aliases", mode="before")
    @classmethod
    def _split_aliases(cls, value: object) -> object:
        """Accept a comma-separated string — an INI cannot hold a list."""
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    def identifiers(self) -> list[str]:
        """Every string that identifies this team, longest first.

        Longest first so a specific alias wins over a shorter one that is also
        a substring of the same team name.
        """
        names = [self.name, *self.aliases]
        return sorted(
            (n.strip() for n in names if n and n.strip()), key=len, reverse=True
        )

    def matches(self, my_team_name: str) -> bool:
        """Whether *my_team_name* (from match_info.ini) is this team.

        Exact match first, then substring, mirroring the behaviour the
        playlist map already relied on. Case-insensitive throughout: section
        names preserve case but option keys do not, and match_info is
        hand-edited.
        """
        wanted = (my_team_name or "").strip().casefold()
        if not wanted:
            return False
        candidates = [n.casefold() for n in self.identifiers()]
        return any(c == wanted for c in candidates) or any(
            c in wanted for c in candidates
        )


def resolve_team(
    teams: dict[str, TeamConfig], my_team_name: str | None
) -> TeamConfig | None:
    """The configured team a game belongs to, or None.

    The single place team identity is resolved. It replaces five different
    matching rules — TeamSnap matched on a section name, PlayMetrics on an
    option value, and the YouTube playlist map on a lowercased substring —
    which is how the same team came to be spelled differently in every section.

    Exact matches win over substring matches across ALL teams, so a team whose
    name matches exactly always beats another team that merely contains the
    string. Within substring matching, longer identifiers are tried first.

    A free function rather than only a ``Config`` method because callers deep
    in the pipeline (the upload task) are handed one section, not the whole
    config.
    """
    if not my_team_name:
        return None
    candidates = [t for t in teams.values() if t.enabled]
    wanted = my_team_name.strip().casefold()

    for team in candidates:
        if any(n.casefold() == wanted for n in team.identifiers()):
            return team

    # Substring fallback: pick the team with the LONGEST matching identifier,
    # not merely the first one configured. With two teams whose names share a
    # word ("Flash" and "WNY Flash Rochester"), first-match would hand the game
    # to whichever happened to be listed first — a silent misfile that sends
    # the video to the wrong playlist.
    best: TeamConfig | None = None
    best_len = 0
    for team in candidates:
        for identifier in team.identifiers():
            folded = identifier.casefold()
            if folded in wanted and len(folded) > best_len:
                best, best_len = team, len(folded)
    return best


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
    seam_realign_profile_path: str | None = None
    # How the game-start time used for trimming is found (decision 1).
    #   "phase_detection" (default): run the offline whistle/ball/player
    #     game-phase detector on the combined video and set the start
    #     automatically from the detected kickoff. Whistle-capable Reolink
    #     cameras only — Dahua cameras (no usable whistle audio) and any
    #     rejected / low-confidence fit fall back to the NTFY walk (decision 7),
    #     so behavior is never worse than today.
    #   "ntfy": always ask via the NTFY "did the game start?" notification walk.
    game_start_method: Literal["phase_detection", "ntfy"] = "phase_detection"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    log_dir: str = "logs"
    app_name: str = "video_grouper"
    backup_count: int = 30  # Keep 30 days of logs


class AppConfig(BaseModel):
    check_interval_seconds: int = 60
    timezone: str = "America/New_York"
    github_repo: str = "mblakley/soccer-cam"
    storage_path: str | None = None
    max_lookback_hours: int = 48
    max_files_per_poll: int = 50
    recording_end_date: str | None = None
    # Reconciliation pass lookback. The incremental sync only queries a
    # forward-moving window (max_lookback_hours back from now), so a game
    # that ages past that window before it was fully captured is never
    # re-queried and is lost. When the download queue is idle, the poller
    # runs a full reconcile over this much larger window and re-queues any
    # camera file that is missing/short/incomplete on disk — regardless of
    # the high-water mark. 14 days covers a typical multi-week gap between
    # plugging the camera in.
    reconcile_lookback_days: int = 14
    # Auto-upgrade settings. auto_update=true (Chrome-style) silently installs
    # detected updates once the pipeline is quiescent; =false stops after
    # download+verify and waits for the tray's POST /api/update/apply.
    # update_api_url overrides the GitHub Releases endpoint for E2E testing;
    # the SOCCER_CAM_UPDATE_API_URL env var wins over both.
    auto_update: bool = True
    update_api_url: str | None = None


class TeamSnapTeamConfig(BaseModel):
    enabled: bool = False
    team_id: str | None = None
    team_name: str


class TeamSnapConfig(BaseModel):
    enabled: bool = False
    client_id: str | None = None
    client_secret: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    teams: list[TeamSnapTeamConfig] = Field(default_factory=list)

    # Legacy single-team fields (kept optional for backward compatibility)
    team_id: str | None = None
    team_name: str | None = None
    my_team_name: str | None = None

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
    team_id: str | None = None
    team_name: str | None = None
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
    username: str | None = None
    password: str | None = None

    # Legacy single-team fields (kept optional for backward compatibility)
    team_id: str | None = None
    team_name: str | None = None

    teams: list[PlayMetricsTeamConfig] = Field(default_factory=list)

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
    topic: str | None = None
    response_service: bool = False
    auto_respond: bool = False
    unplug_notification: bool = True


class AutocamConfig(BaseModel):
    enabled: bool = True
    executable: str | None = None
    # AutoCam licence key, as sold by the vendor (their own spelling is
    # "licence"; we use the US spelling for our config surface). Entirely
    # user-supplied — we never ship a key.
    #
    # AutoCam stores its activation per Windows profile, under
    # %LOCALAPPDATA%\Once\licence.txt. That makes the licence invisible to
    # any other account: a render driven from a different user (notably the
    # LocalSystem service) is unlicensed and comes out watermarked even
    # though the desktop user is fully licensed. Setting this lets the
    # pipeline activate the profile it actually renders under, instead of
    # asking the operator to log in as that account and activate by hand.
    license_key: str = ""


class CloudSyncConfig(BaseModel):
    enabled: bool = False
    provider: str | None = None


class NodeConfig(BaseModel):
    """Per-node role for distributed video processing.

    - ``standalone`` (default): single-host setup. Orchestrator runs the full
      pipeline; no worker API, no remote workers.
    - ``master``: orchestrator + ``/api/work/*`` worker-coordination API.
      Remote workers register here and pull tasks.
    - ``worker``: this node does NOT run the orchestrator. Polls a master
      via ``master_url`` for work and processes it. ``python -m
      video_grouper.worker`` is the entry point.
    """

    role: str = "standalone"  # 'standalone' | 'master' | 'worker'
    master_url: str = ""  # worker only
    # Tokens master issues to workers at /api/work/register. Stored
    # comma-separated; worker keeps its own in worker_state.json after
    # registration.
    worker_tokens: list[str] = Field(default_factory=list)
    # Worker-side: which capabilities this node advertises to the master.
    capabilities: list[str] = Field(
        default_factory=lambda: ["combine", "trim", "pipeline"]
    )

    @field_validator("worker_tokens", "capabilities", mode="before")
    @classmethod
    def _parse_str_list(cls, v):
        if isinstance(v, str):
            stripped = v.strip()
            if stripped in ("", "[]"):
                return []
            if stripped.startswith("[") and stripped.endswith("]"):
                import ast

                try:
                    parsed = ast.literal_eval(stripped)
                    if isinstance(parsed, list):
                        return parsed
                except (ValueError, SyntaxError):
                    pass
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return v


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

    auth_server_enabled: bool = True
    auth_server_bind: str = "127.0.0.1"
    auth_server_port: int = 8765
    # Optional: container-side Supabase URL used by HTTP clients inside the
    # container, while supabase_url stays as the browser-facing URL emitted
    # in OAuth redirects. Leave blank to use supabase_url for both legs.
    supabase_internal_url: str = ""

    @field_validator("plugin_signing_public_keys", mode="before")
    @classmethod
    def _parse_str_list(cls, v):
        """Round-trip the list through INI: save_config writes
        ``str(list)`` (e.g. ``"['key1', 'key2']"``); on reload
        configparser returns that as a string. Parse it back to a list
        so reload doesn't fail validation.
        """
        if isinstance(v, str):
            stripped = v.strip()
            if stripped in ("", "[]"):
                return []
            if stripped.startswith("[") and stripped.endswith("]"):
                import ast

                try:
                    parsed = ast.literal_eval(stripped)
                    if isinstance(parsed, list):
                        return parsed
                except (ValueError, SyntaxError):
                    pass
            # Comma-separated fallback for hand-edited values.
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return v


class MomentTaggingConfig(BaseModel):
    enabled: bool = False
    api_base_url: str = "http://localhost:8000"
    service_role_key: str = ""


class YouTubePlaylistConfig(BaseModel):
    name_format: str
    description: str
    privacy_status: str


class YouTubeConfig(BaseModel):
    enabled: bool = False
    privacy_status: str = "private"
    use_mock: bool = False
    # Smoke-test escape hatch: short-circuit upload_video to return a fake
    # video id without calling the Google API. Must NOT be enabled in
    # production — reels will reference non-existent YouTube videos.
    skip_upload: bool = False
    # Minutes to hold uploads after the API reports the daily upload quota
    # is exhausted, before probing whether it has freed. The project-level
    # "Video Uploads per day" limit does NOT reset at midnight Pacific, so
    # the reset is discovered by polling rather than predicted.
    quota_retry_minutes: int = 30
    processed_playlist: YouTubePlaylistConfig | None = None
    raw_playlist: YouTubePlaylistConfig | None = None

    model_config = {"validate_by_name": True}


class SetupConfig(BaseModel):
    """Tracks onboarding wizard completion state."""

    onboarding_completed: bool = False


class Config(BaseModel):
    # Every section defaults. Only `storage.path` is genuinely required — it
    # needs a real directory and there is no sane default for it.
    #
    # These eight used to be declared required with no default_factory even
    # though each constructs fine with no arguments. Nothing could build a
    # Config without naming all of them, so every place that needed a default
    # config hand-enumerated the sections: create_default_config, the setup
    # wizard's _build_config, and config.ini.dist. All three drifted, each
    # omitting a different set (ARCHIVE, AUTOCAM, PIPELINE, NODE,
    # MOMENT_TAGGING), and every entry they did list was literally `{}`.
    # With defaults here, a new section needs no generator edit at all.
    schema_meta: SchemaConfig = Field(alias="SCHEMA", default_factory=SchemaConfig)
    # Keyed by the [TEAM.<key>] handle. Insertion-ordered, so a config's own
    # ordering decides ties between two teams that both match.
    teams: dict[str, TeamConfig] = Field(default_factory=dict)
    cameras: list[CameraConfig] = Field(default_factory=list)
    storage: StorageConfig = Field(alias="STORAGE")
    recording: RecordingConfig = Field(
        alias="RECORDING", default_factory=RecordingConfig
    )
    processing: ProcessingConfig = Field(
        alias="PROCESSING", default_factory=ProcessingConfig
    )
    logging: LoggingConfig = Field(alias="LOGGING", default_factory=LoggingConfig)
    app: AppConfig = Field(alias="APP", default_factory=AppConfig)
    teamsnap: TeamSnapConfig = Field(alias="TEAMSNAP", default_factory=TeamSnapConfig)
    playmetrics: PlayMetricsConfig = Field(
        alias="PLAYMETRICS", default_factory=PlayMetricsConfig
    )
    ntfy: NtfyConfig = Field(alias="NTFY", default_factory=NtfyConfig)
    youtube: YouTubeConfig = Field(alias="YOUTUBE", default_factory=YouTubeConfig)
    cloud_sync: CloudSyncConfig = Field(
        alias="CLOUD_SYNC", default_factory=CloudSyncConfig
    )
    ttt: TTTConfig = Field(alias="TTT", default_factory=TTTConfig)
    moment_tagging: MomentTaggingConfig = Field(
        default_factory=MomentTaggingConfig, alias="MOMENT_TAGGING"
    )
    setup: SetupConfig = Field(alias="SETUP", default_factory=SetupConfig)
    node: NodeConfig = Field(alias="NODE", default_factory=NodeConfig)
    # Installation-level AutoCam settings (where the vendor's app lives and
    # the operator's licence key), as opposed to per-run rendering options,
    # which belong to the pipeline step. Kept a top-level section so it is
    # editable from /config: the editor only renders scalar fields of
    # top-level sections, and pipeline step specs are nested.
    autocam: AutocamConfig = Field(alias="AUTOCAM", default_factory=AutocamConfig)
    pipeline: PipelineConfig = Field(alias="PIPELINE", default_factory=PipelineConfig)

    model_config = {"validate_by_name": True}

    @property
    def camera(self) -> CameraConfig:
        """Convenience accessor for the first camera config."""
        return self.cameras[0]

    def team_for(self, my_team_name: str | None) -> TeamConfig | None:
        """The configured team a game belongs to, or None.

        See :func:`resolve_team` — this is the same lookup, for callers that
        hold the whole ``Config``.
        """
        return resolve_team(self.teams, my_team_name)

    def post_trim_processing_active(self) -> bool:
        """True when a post-trim processing stage owns ``trimmed`` groups.

        The config-driven pipeline (``[PIPELINE]``) is the sole post-trim
        processing path. When it is inactive, a trimmed group skips straight to
        upload.
        """
        pipeline = getattr(self, "pipeline", None)
        return bool(pipeline is not None and pipeline.is_active())


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

    # [YOUTUBE.PLAYLIST_MAP] was team -> playlist with its own substring
    # matching rule. Schema migration v2 folds it into [TEAM.<key>]
    # youtube_playlist, resolved by resolve_team like every other per-team
    # setting. Drop the section if an unmigrated file still carries it, so it
    # cannot look like it is still being honoured.
    config_dict.pop("YOUTUBE.PLAYLIST_MAP", None)

    # Handle BALL_TRACKING sub-sections (provider configs + per-team overrides).
    # `[BALL_TRACKING.AUTOCAM_GUI]` -> nested under BALL_TRACKING.AUTOCAM_GUI
    # `[BALL_TRACKING.PER_TEAM]` -> dict of team_name -> provider_name
    for section in list(config_dict.keys()):
        if section.startswith("BALL_TRACKING."):
            sub_alias = section.split(".", 1)[1]  # e.g. "AUTOCAM_GUI" or "PER_TEAM"
            sub_value = config_dict.pop(section)
            config_dict.setdefault("BALL_TRACKING", {})[sub_alias] = sub_value

    # Handle PIPELINE sub-sections: [PIPELINE.PER_TEAM] -> per-team overrides;
    # every other [PIPELINE.<step_id>] -> one step spec (its `type` + raw config).
    # PIPELINE.PER_TEAM is a RESERVED sub-section (per-team overrides) — a step
    # may not be named PER_TEAM. Every other [PIPELINE.<step_id>] is a step spec.
    # A step section without a `type` is malformed; skip it with a warning rather
    # than failing the entire config load (these sections are hand-editable).
    pipeline_step_specs: dict[str, dict] = {}
    for section in list(config_dict.keys()):
        if section.startswith("PIPELINE."):
            sub = section.split(".", 1)[1]
            sub_value = config_dict.pop(section)
            if sub == "PER_TEAM":
                config_dict.setdefault("PIPELINE", {})["PER_TEAM"] = sub_value
                continue
            step_type = sub_value.pop("type", None)
            if step_type is None:
                logger.warning(
                    "[PIPELINE.%s] has no `type`; ignoring this step section", sub
                )
                continue
            pipeline_step_specs[sub] = {
                "step_id": sub,
                "type": step_type,
                "config": sub_value,
            }
    if pipeline_step_specs:
        config_dict.setdefault("PIPELINE", {})["step_specs"] = pipeline_step_specs

    # Handle PlayMetrics teams
    playmetrics_teams = []
    for section in list(config_dict.keys()):
        if section.startswith("PLAYMETRICS.TEAM."):
            team_config = config_dict.pop(section)
            playmetrics_teams.append(team_config)

    # Attach teams to main PLAYMETRICS config
    if "PLAYMETRICS" in config_dict:
        config_dict["PLAYMETRICS"]["teams"] = playmetrics_teams

    # Handle team sections: [TEAM.<key>] -> teams dict, keyed by the handle.
    # Unlike [CAMERA.<name>] the section suffix is NOT the identity — `name`
    # inside the section is what gets matched against match_info. The handle is
    # just a stable label for the operator.
    teams: dict[str, dict] = {}
    for section in list(config_dict.keys()):
        if section.startswith("TEAM."):
            teams[section.split(".", 1)[1]] = config_dict.pop(section)
    if teams:
        config_dict["teams"] = teams

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

    # Project [TEAM.*] into the integrations' own team lists.
    #
    # [TEAM.<key>] is the single operator-facing place a team is configured,
    # but TeamSnapService/PlayMetricsService/TeamInfoTask each iterate their
    # section's `teams` list. Migration v2 DELETES the [TEAMSNAP.*] and
    # [PLAYMETRICS.TEAM.*] sections those lists were built from, so without
    # this projection an upgraded install would silently find no teams at all
    # and both integrations would quietly stop fetching schedules.
    #
    # Derived, not duplicated: entries appear here only because a [TEAM.*]
    # section names them, and a legacy entry of the same name wins so an
    # unmigrated file behaves exactly as before.
    for team_key, team_body in (config_dict.get("teams") or {}).items():
        team_name = (team_body.get("name") or team_key).strip()
        if not team_name or str(team_body.get("enabled", "true")).lower() in (
            "false",
            "0",
            "no",
        ):
            continue
        for section, id_field, bucket in (
            ("TEAMSNAP", "teamsnap_team_id", teamsnap_teams),
            ("PLAYMETRICS", "playmetrics_team_id", playmetrics_teams),
        ):
            team_id = (team_body.get(id_field) or "").strip()
            if not team_id or section not in config_dict:
                continue
            if any(
                (t.get("team_name") or "").strip().casefold() == team_name.casefold()
                for t in bucket
            ):
                continue
            bucket.append({"team_name": team_name, "team_id": team_id, "enabled": True})
    if "TEAMSNAP" in config_dict:
        config_dict["TEAMSNAP"]["teams"] = teamsnap_teams
    if "PLAYMETRICS" in config_dict:
        config_dict["PLAYMETRICS"]["teams"] = playmetrics_teams

    # [BALL_TRACKING] -> [PIPELINE] used to be folded in right here, on every
    # single load, forever. It is now schema migration v1
    # (video_grouper/utils/config_migrations.py), applied once at startup and
    # written back to disk. Drop the section if a caller loads a file that has
    # not been migrated yet — it is not a Config field, so model_validate would
    # ignore it anyway; being explicit keeps the intent readable.
    for section in [s for s in config_dict if s.startswith("BALL_TRACKING")]:
        config_dict.pop(section, None)

    # Installation-level AutoCam settings live in [AUTOCAM] so they're
    # editable from /config. Fold them into the AutoCam step spec, which is
    # what actually runs, unless that spec sets its own value. Same shape as
    # the legacy migration above: one operator-facing place, resolved here
    # rather than by giving steps access to the whole Config.
    _merge_autocam_section(config_dict)

    return Config.model_validate(config_dict)


# Step types that consume installation-level AutoCam settings.
_AUTOCAM_STEP_TYPES = ("autocam", "autocam_cli")


def _merge_autocam_section(config_dict: dict) -> None:
    """Push ``[AUTOCAM]`` values down into AutoCam pipeline step specs."""
    autocam_section = config_dict.get("AUTOCAM") or {}
    shared = {
        key: value
        for key in ("executable", "license_key")
        if (value := autocam_section.get(key))
    }
    if not shared:
        return
    specs = (config_dict.get("PIPELINE") or {}).get("step_specs") or {}
    for spec in specs.values():
        if not isinstance(spec, dict) or spec.get("type") not in _AUTOCAM_STEP_TYPES:
            continue
        step_cfg = spec.setdefault("config", {})
        for key, value in shared.items():
            step_cfg.setdefault(key, value)


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

        # teams -> one [TEAM.<key>] section each. `aliases` is a list, which an
        # INI cannot hold, so join it back to the comma-separated form the
        # field validator parses on the way in.
        if field_name == "teams":
            for key, team in value.items():
                team_dict = team.model_dump()
                team_dict["aliases"] = ", ".join(team_dict.get("aliases") or [])
                parser[f"TEAM.{key}"] = {
                    k: str(v) for k, v in team_dict.items() if v is not None
                }
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

        if field_name == "pipeline":
            # [PIPELINE] scalars + ordered step ids; one [PIPELINE.<id>] per step
            # (type + that step's raw config); [PIPELINE.PER_TEAM] if present.
            main = {
                "enabled": str(value.enabled),
                "community_plugins_enabled": str(value.community_plugins_enabled),
                "gpu_concurrency": str(value.gpu_concurrency),
                "ram_heavy_concurrency": str(value.ram_heavy_concurrency),
            }
            if value.steps:
                main["steps"] = ", ".join(value.steps)
            parser["PIPELINE"] = main
            for step_id, spec in value.step_specs.items():
                section_items = {"type": spec.type}
                for k, v in spec.config.items():
                    if v is not None:
                        section_items[k] = str(v)
                parser[f"PIPELINE.{step_id}"] = section_items
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

    Used on first boot when no config exists, and by the onboarding wizard.

    Every section defaults, so only the storage path is named here. This used
    to enumerate twelve sections as ``{}`` because they were declared required
    on the model; it drifted from the wizard's copy of the same list (each
    omitted a different set). Adding a section to ``Config`` now needs no edit
    here.
    """
    config = Config(storage=StorageConfig(path=storage_path))
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
