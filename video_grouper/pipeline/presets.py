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
(``stitch_correct`` / ``detect`` / ``track`` / ``render`` / ``autocam``); the
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
    # NOTE: the detect step deliberately carries NO model source (model_key /
    # model_path). The user supplies it after picking this preset — TTT login
    # resolves a model_key, or a local model_path points at a .onnx. See the
    # module docstring.
    "homegrown": [
        (
            "stitch_correct",
            "stitch_correct",
            # Opt-in seam calibration; user points this at their profile.
            {"stitch_profile_path": ""},
        ),
        (
            "detect",
            "detect",
            # Model source intentionally omitted — user supplies model_key
            # (via TTT login) or model_path (local .onnx). Only inference
            # tunables get defaults here.
            {
                "device": "cuda:0",
                "detect_confidence": 0.45,
                "detect_frame_interval": 4,
            },
        ),
        (
            "track",
            "track",
            {
                "track_kalman_gate": 200.0,
                "track_max_missing": 15,
            },
        ),
        (
            "render",
            "render",
            {
                "render_mode": "broadcast",
                "render_output_width": 1920,
                "render_output_height": 1080,
                "render_vertical_tracking": True,
            },
        ),
    ],
    # Homegrown pipeline + camera stabilization. Same as `homegrown` plus a
    # `stabilize` step inserted ahead of detect, with detect_stabilize and
    # render_stabilize turned on so the trajectory + broadcast crop both sit on
    # top of the stabilized frames. Adds one analysis pass to the run (no
    # re-encode); opt-in for users whose camera physically moves in wind.
    "broadcast_stabilized": [
        (
            "stitch_correct",
            "stitch_correct",
            {"stitch_profile_path": ""},
        ),
        (
            "stabilize",
            "stabilize",
            {
                # "heavy" picks the polygon-zone blend with full per-axis
                # budgets — the right baseline for a typical breezy day on
                # a 16' tripod. Drop to "light" / "standard" for calmer
                # conditions or bump to "extreme" for a really gusty day;
                # the reprocess flow exposes this as a dropdown.
                "stabilization_strength": "heavy",
            },
        ),
        (
            "detect",
            "detect",
            # Model source intentionally omitted — user supplies model_key
            # (via TTT login) or model_path (local .onnx). ``detect_stabilize``
            # runs ONNX on stabilized frames (better SNR) but writes the
            # detections back in RAW source coords — the canonical schema
            # for ``detections.json``, regardless of stabilization. The
            # next step (``transform_detections``) lifts them into
            # stabilized-output coords for the downstream consumers.
            {
                "device": "cuda:0",
                "detect_confidence": 0.45,
                "detect_frame_interval": 4,
                "detect_stabilize": True,
            },
        ),
        (
            # Lift raw-coord detections into stabilized-output coords so
            # track + render can operate against a single coord space.
            # This step is also the reprocess flow's pivot — a
            # ``skip_detect`` reprocess just re-runs this with the new
            # ``motion.json`` instead of re-running ONNX, which is the
            # whole point of writing detect's output in raw coords.
            "transform_detections",
            "transform_detections",
            {},
        ),
        (
            "track",
            "track",
            {
                "track_kalman_gate": 200.0,
                "track_max_missing": 15,
            },
        ),
        (
            "render",
            "render",
            {
                "render_mode": "broadcast",
                "render_output_width": 1920,
                "render_output_height": 1080,
                "render_vertical_tracking": True,
                "render_stabilize": True,
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
