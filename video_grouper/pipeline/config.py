"""Config models for the ``[PIPELINE]`` section + legacy migration.

The pipeline is an ordered list of step ids (``steps``) plus one
``[PIPELINE.<step_id>]`` section per step carrying its ``type`` and that step's
own raw config. Config values stay raw (strings from INI) here — each step's
own ``config_model`` validates/coerces them at ``create_step`` time, so this
layer never imports plugin modules.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, field_validator

from video_grouper.pipeline.base import StepSpec

logger = logging.getLogger(__name__)


class PipelineStepSpec(BaseModel):
    """One configured step: ``[PIPELINE.<step_id>]`` -> step_id + type + config."""

    step_id: str
    type: str
    config: dict[str, Any] = Field(default_factory=dict)


class PipelineConfig(BaseModel):
    """The ``[PIPELINE]`` section.

    ``steps`` is the ordered list of step ids; ``step_specs`` maps each id to its
    spec. ``enabled`` is the master switch. Resource-pool capacities tune the
    scheduler. ``per_team`` allows per-team overrides (applied upstream).
    """

    enabled: bool = False
    community_plugins_enabled: bool = False
    gpu_concurrency: int = 1
    ram_heavy_concurrency: int = 1
    steps: list[str] = Field(default_factory=list)
    step_specs: dict[str, PipelineStepSpec] = Field(default_factory=dict)
    per_team: dict[str, str] = Field(default_factory=dict, alias="PER_TEAM")

    model_config = {"populate_by_name": True}

    @field_validator("steps", mode="before")
    @classmethod
    def _split_csv(cls, v: Any) -> Any:
        """Accept a CSV string from INI as well as a list."""
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    def is_active(self, team_name: str | None = None) -> bool:
        """True when the pipeline is enabled and resolves to at least one step.

        This is the single predicate the orchestrator / video processor / state
        auditor key on to decide whether the config-driven pipeline owns a
        ``trimmed`` group (vs. the legacy ball-tracking path or a straight
        skip-to-upload). Keeping it here avoids every call site re-deriving
        ``enabled and ordered_steps()``.
        """
        return bool(self.enabled and self.ordered_steps(team_name))

    def ordered_steps(self, team_name: str | None = None) -> list[StepSpec]:
        """Return the configured steps as runner-ready :class:`StepSpec`s, in order.

        ``team_name`` is accepted for forward-compatibility with per-team
        pipelines; today a single pipeline applies to all teams.
        """
        specs: list[StepSpec] = []
        for step_id in self.steps:
            spec = self.step_specs.get(step_id)
            if spec is None:
                logger.warning(
                    "pipeline: step id %r is listed in `steps` but has no "
                    "[PIPELINE.%s] section; skipping",
                    step_id,
                    step_id,
                )
                continue
            specs.append(
                StepSpec(step_id=spec.step_id, type=spec.type, config=dict(spec.config))
            )
        return specs


# Which legacy [BALL_TRACKING.HOMEGROWN] fields belong to which stage, so a
# migrated pipeline carries only each step's own options.
_HOMEGROWN_STAGE_FIELDS: dict[str, list[str]] = {
    "stitch_correct": ["stitch_profile_path"],
    "detect": [
        "model_key",
        "model_path",
        "detect_channel",
        "detect_pipeline_version",
        "device",
        "detect_confidence",
        "detect_frame_interval",
    ],
    "track": ["track_kalman_gate", "track_max_missing"],
    # Only the output resolution survives the migration; the legacy render
    # tunables (ema / lead_room / fov_deg) have no analogue in the cylindrical
    # broadcast render, which uses its own (defaulted) control params.
    "render": [
        "render_output_width",
        "render_output_height",
    ],
}


def migrate_ball_tracking_to_pipeline(bt: dict | None) -> dict | None:
    """Build a ``[PIPELINE]`` config dict from a legacy ``[BALL_TRACKING]`` dict.

    *bt* is the BALL_TRACKING dict as nested by ``load_config`` (with
    ``AUTOCAM_GUI`` / ``HOMEGROWN`` / ``PER_TEAM`` sub-dicts). Returns ``None``
    when there's nothing recognizable to migrate (caller leaves PIPELINE as-is).
    """
    if not bt:
        return None
    provider = str(bt.get("provider") or "").strip()
    out: dict[str, Any] = {
        "enabled": bt.get("enabled", "true"),
        "steps": [],
        "step_specs": {},
    }
    # NOTE: legacy [BALL_TRACKING.PER_TEAM] mapped team -> provider name, which has
    # no meaning under the pipeline's per-team model. Intentionally NOT carried
    # over; per-team pipeline overrides get defined when that feature lands.

    if provider == "autocam_gui":
        executable = (bt.get("AUTOCAM_GUI") or {}).get("executable")
        cfg = {"executable": executable} if executable else {}
        out["steps"] = ["autocam"]
        out["step_specs"]["autocam"] = {
            "step_id": "autocam",
            "type": "autocam",
            "config": cfg,
        }
    elif provider == "homegrown":
        hg = bt.get("HOMEGROWN") or {}
        stages_raw = hg.get("stages") or "stitch_correct, detect, track, render"
        stages = (
            [s.strip() for s in stages_raw.split(",") if s.strip()]
            if isinstance(stages_raw, str)
            else list(stages_raw)
        )
        out["steps"] = stages
        for stage in stages:
            fields = _HOMEGROWN_STAGE_FIELDS.get(stage, [])
            cfg = {f: hg[f] for f in fields if hg.get(f) not in (None, "")}
            out["step_specs"][stage] = {
                "step_id": stage,
                "type": stage,
                "config": cfg,
            }
    else:
        return None

    return out
