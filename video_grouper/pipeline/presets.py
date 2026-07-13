"""Built-in pipeline presets — data-only step templates.

A *preset* is a named, ordered list of steps with sensible default config for
each, used to seed a fresh ``[PIPELINE]`` section. Selecting a preset follows a
**template / copy-in** model: :func:`apply_preset` *copies* the preset's steps
and their default config into a brand-new :class:`PipelineConfig` that the
caller persists. Nothing here mutates a live config or runs any step — this
module is pure data plus three small accessors, and deliberately imports
nothing from the step plugin modules (so it stays cheap to import in every
bundle, including the tray bundle that lacks the inference stack).

Each preset's step ``type`` is a registered built-in step name
(``stitch_correct`` / ``field_detect`` / ``ball_detect`` / ``ball_select`` /
``plan_camera`` / ``render`` / ``autocam``); the
``step_id`` is set equal to the type for the single-instance presets here.

The model source for the ``detect`` step is intentionally left **unset** in the
``homegrown`` preset: the user supplies it after selecting the preset. Either

  * sign into TTT, which resolves a ``model_key`` (free or premium tier,
    decided server-side), or
  * point ``model_path`` at a local ``.onnx`` (community / bring-your-own).

Seeding either field here would be wrong — there's no universal default, and
the OSS rule forbids shipping concrete model identifiers.
"""

from __future__ import annotations

from typing import Any

from video_grouper.pipeline.config import PipelineConfig, PipelineStepSpec

# A preset entry is an ordered list of (step_id, type, default_config) rows.
# Defaults mirror each step's own ``config_model`` defaults so a freshly seeded
# pipeline behaves identically whether a field is written explicitly or left to
# the step model — but they're spelled out here so the seeded config.ini is
# self-documenting and hand-editable.
_PresetStep = tuple[str, str, dict[str, Any]]

PRESETS: dict[str, list[_PresetStep]] = {
    # Homegrown ball-tracking pipeline: stitch the dual-lens seam, detect the
    # ball per frame, link detections into a smoothed trajectory, then render a
    # broadcast-style virtual-camera crop following the ball.
    #
    # NOTE: the detect and field_detect steps deliberately carry NO model
    # source (model_key / model_path). The user supplies it after picking this
    # preset — TTT login resolves a model_key, or a local model_path points at
    # a .onnx. See the module docstring.
    "homegrown": [
        (
            "stitch_correct",
            "stitch_correct",
            # Opt-in seam calibration; user points this at their profile.
            {"stitch_profile_path": ""},
        ),
        (
            "field_detect",
            "field_detect",
            # Model source intentionally omitted (same policy as ball_detect).
            # The 10-point field outline is REQUIRED downstream: ball_detect
            # crops the field band from it and ball_select's physics run in
            # world meters through its homography.
            {
                "device": "cuda:0",
                "field_score_threshold": 0.5,
                "field_min_keypoints": 6,
                "field_sample_frames": 7,
            },
        ),
        (
            "ball_detect",
            "ball_detect",
            # The heatmap candidate detector. Model source intentionally
            # omitted — user supplies model_key (via TTT login) or model_path
            # (local .onnx). Only inference tunables get defaults here.
            {
                "device": "cuda:0",
                "detect_confidence": 0.1,
                "detect_frame_interval": 4,
            },
        ),
        (
            "ball_select",
            "ball_select",
            # The learned game-ball selector + physics Viterbi + RTS smoother.
            # select_model_path intentionally omitted — supplied like the
            # detector model (TTT login or a local selector .npz).
            {
                "select_static_w": 2.0,
                "select_phys_sigma_px": 5.0,
                "select_bridge_w": 2.0,
                # off-field pin OFF (EXP-DIST-48): oob_w=2 pinned the track to field
                # boundaries during normal play, hurting selected recall. oob_w=0 beat it
                # on BOTH held-out games (Spencerport far 0.384->0.412 near 0.385->0.431;
                # Iron far 0.301->0.318 near 0.433->0.523) + AutoCam-agreement.
                "select_oob_w": 0.0,
            },
        ),
        (
            "plan_camera",
            "plan_camera",
            # AutoCam-calibrated cinematography (zoom curve, lead room,
            # dead-ball widening) — ALL camera intelligence lives here.
            {
                "plan_zoom_scale": 0.90,
                "plan_lead_frames": 8.0,
                "plan_deadball_hfov_deg": 52.0,
                "plan_missing_hfov_deg": 58.0,
            },
        ),
        (
            "render",
            "render",
            # The dumb renderer: executes plan_camera's command stream with
            # projection-feasibility clamps only.
            {
                "render_output_width": 1920,
                "render_output_height": 1080,
            },
        ),
    ],
    # AutoCam pipeline: a single step that drives the Once AutoCam desktop app.
    # The executable path is left unset — the user fills it in with their
    # AutoCam install location.
    "autocam": [
        (
            "autocam",
            "autocam",
            # executable intentionally unset — user supplies their AutoCam path.
            {"executable": ""},
        ),
    ],
}


def list_presets() -> list[str]:
    """Return the names of all built-in presets."""
    return list(PRESETS)


def get_preset(name: str) -> list[_PresetStep]:
    """Return a deep copy of the preset's step rows.

    Returns a copy (fresh dicts) so callers can mutate the result — e.g. fill
    in a ``model_path`` — without corrupting the shared :data:`PRESETS`
    template.

    Raises:
        KeyError: If *name* is not a known preset.
    """
    if name not in PRESETS:
        available = ", ".join(sorted(PRESETS)) or "(none)"
        raise KeyError(f"Unknown pipeline preset: {name!r}. Available: {available}")
    return [
        (step_id, step_type, dict(cfg)) for step_id, step_type, cfg in PRESETS[name]
    ]


def apply_preset(name: str, *, enabled: bool = False) -> PipelineConfig:
    """Copy a preset into a fresh :class:`PipelineConfig` the caller can persist.

    Template / copy-in model: the returned config is brand new — it carries
    only the preset's steps + their default config, ready to be assigned to
    ``Config.pipeline`` and saved. ``enabled`` defaults to ``False`` so a
    freshly seeded pipeline doesn't run until the user has filled in the bits
    the preset deliberately leaves blank (a detect model source, an AutoCam
    executable, etc.).

    Raises:
        KeyError: If *name* is not a known preset.
    """
    rows = get_preset(name)
    steps = [step_id for step_id, _type, _cfg in rows]
    step_specs = {
        step_id: PipelineStepSpec(step_id=step_id, type=step_type, config=cfg)
        for step_id, step_type, cfg in rows
    }
    return PipelineConfig(enabled=enabled, steps=steps, step_specs=step_specs)
