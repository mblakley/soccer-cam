"""Pydantic config models for the ``[BALL_TRACKING]`` section."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class AutocamGuiProviderConfig(BaseModel):
    """Config for the ``autocam_gui`` provider (drives Once AutoCam GUI)."""

    executable: Optional[str] = None


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
    per_team: dict[str, str] = Field(default_factory=dict, alias="PER_TEAM")

    model_config = {"validate_by_name": True}

    def resolve_provider_for(self, team_name: str | None) -> tuple[str, BaseModel]:
        """Pick the provider name + its config for *team_name*.

        Falls back to :attr:`provider` if there's no per-team override.
        """
        name = self.per_team.get(team_name or "", self.provider)
        cfg = getattr(self, name)
        return name, cfg
