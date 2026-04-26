"""Pydantic config models for the ``[BALL_TRACKING]`` section."""

from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, Field, field_validator


class AutocamGuiProviderConfig(BaseModel):
    """Config for the ``autocam_gui`` provider (drives Once AutoCam GUI)."""

    executable: Optional[str] = None


class HomegrownProviderConfig(BaseModel):
    """Config for the ``homegrown`` provider — our in-house ball-tracking pipeline.

    The ``stages`` list defines the order of processing phases. Each name
    must match a registered :class:`ProcessingStage`. The default list
    matches the plan: stitch correction, detect, track, render.

    All stages share this single config object — they pull whichever
    fields they need. Future PRs may split per-stage sub-models if
    options grow.
    """

    enabled_stages: List[str] = Field(
        default_factory=lambda: [
            "stitch_correct",
            "field_mask",
            "detect",
            "track",
            "render",
        ],
        alias="stages",
    )

    # stitch_correct
    stitch_profile_path: Optional[str] = None

    # field_mask — same model-source contract as detect (mutually-exclusive
    # model_key vs model_path); skips silently if neither is set.
    field_mask_model_key: Optional[str] = None
    field_mask_model_path: Optional[str] = None
    field_mask_channel: Optional[str] = None
    field_mask_pipeline_version: Optional[str] = None
    field_mask_confidence: float = 0.7

    # detect — pick exactly one source:
    #   model_key: ask TTT for a license + encrypted artifact (production)
    #   model_path: load a plaintext .onnx from disk (dev / local testing)
    model_key: Optional[str] = None
    model_path: Optional[str] = None
    detect_channel: Optional[str] = (
        None  # canary / beta / stable; defaults to stable on TTT
    )
    detect_pipeline_version: Optional[str] = None
    device: str = "cuda:0"
    detect_confidence: float = 0.45
    detect_frame_interval: int = 4

    # track
    track_kalman_gate: float = 200.0
    track_max_missing: int = 15

    # render
    render_ema: float = 0.975
    render_lead_room: float = 0.15
    render_output_width: int = 1920
    render_output_height: int = 1080
    render_fov_deg: float = 50.0
    # Cylindrical projection — source has ~180° HFOV after stitch; view is the
    # rendered perspective FOV (render_fov_deg above). vfovs of -1 mean
    # "derive automatically for square pixels".
    render_src_hfov_deg: float = 180.0
    render_src_vfov_deg: float = -1.0
    render_view_vfov_deg: float = -1.0
    render_pitch_deg: float = 0.0

    model_config = {"validate_by_name": True}

    @field_validator("enabled_stages", mode="before")
    @classmethod
    def _split_csv_stages(cls, v: Any) -> Any:
        """Accept a CSV string from INI as well as a list."""
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v


class BallTrackingConfig(BaseModel):
    """Top-level ``[BALL_TRACKING]`` config.

    ``enabled`` is the master switch — when False, video stops at ``trimmed``
    and skips straight to upload.

    ``provider`` selects the default provider for all games. ``per_team``
    allows overriding by team name (key matches ``MatchInfo.team_name``).

    Provider-specific configs live under their own attribute, with the
    INI alias matching the provider's registered name in upper case
    (e.g. ``[BALL_TRACKING.AUTOCAM_GUI]``).
    """

    enabled: bool = True
    provider: str = "autocam_gui"
    autocam_gui: AutocamGuiProviderConfig = Field(
        default_factory=AutocamGuiProviderConfig, alias="AUTOCAM_GUI"
    )
    homegrown: HomegrownProviderConfig = Field(
        default_factory=HomegrownProviderConfig, alias="HOMEGROWN"
    )
    per_team: dict[str, str] = Field(default_factory=dict, alias="PER_TEAM")

    model_config = {"validate_by_name": True}

    def resolve_provider_for(self, team_name: str | None) -> tuple[str, BaseModel]:
        """Pick the provider name + its config for *team_name*.

        Falls back to :attr:`provider` if there's no per-team override.
        """
        name = self.per_team.get(team_name or "", self.provider)
        cfg = getattr(self, name)
        return name, cfg
