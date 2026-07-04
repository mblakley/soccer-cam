"""Per-recording reprocess override — runtime patch applied to a pipeline
spec list before the runner loop.

A coach or camera-manager occasionally wants to re-run a recording with
different stabilization (e.g. "redo this windy game at extreme
strength"). Re-running the whole pipeline is wasteful: the
detection pass is the expensive part, and stabilization is just
per-frame 2×3 math — the detections from the previous run can be
forwarded through the new ``motion.json`` instead.

This module provides:

* :class:`ReprocessRequest` — the schema of
  ``<group_dir>/reprocess_request.json``, the persistent per-recording
  override file written by the tray UI and the (eventual) TTT API
  poller.
* :func:`read_reprocess_request` — load it if present.
* :func:`apply_overrides` — patch a list of :class:`StepSpec` against a
  request, and return (modified_specs, step_ids_to_preseed). The
  preseed list names step records whose ``produced`` artifacts must be
  replayed into the working artifact map BEFORE the runner loop, so
  the replacement steps see the previous run's outputs as inputs.

The override file persists across runs by design — so a coach's
"reprocess at extreme" stays active for that recording until they
explicitly change it or remove the file. A fresh recording with no
override falls through to the global pipeline config unchanged.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC
from pathlib import Path

from pydantic import BaseModel

from video_grouper.pipeline.base import StepSpec

logger = logging.getLogger(__name__)


REPROCESS_REQUEST_FILENAME = "reprocess_request.json"
CANCEL_REQUEST_FILENAME = "cancel_request.json"


def pipeline_state_path(group_dir: Path) -> Path:
    """The runner persists its per-step status here (mirrors
    ``manifest.MANIFEST_FILENAME``; duplicated to avoid a cross-module
    import for callers that only need to peek at running-ness)."""
    return Path(group_dir) / "pipeline_state.json"


def is_pipeline_running(group_dir: Path) -> bool:
    """True iff the runner has a step in ``status="running"`` for this
    group — i.e. the pipeline is actively executing. The web layer uses
    this to gate "only one run at a time" + to decide whether to surface
    a Cancel button instead of the Reprocess form."""
    path = pipeline_state_path(group_dir)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    for step in data.get("steps", []):
        if step.get("status") == "running":
            return True
    return False


def write_cancel_request(group_dir: Path) -> Path:
    """Drop the marker the runner checks between steps. Idempotent —
    re-writing during an existing cancel is a no-op."""
    path = Path(group_dir) / CANCEL_REQUEST_FILENAME
    path.write_text(json.dumps({"requested_at": _now_iso()}), encoding="utf-8")
    return path


def cancel_requested(group_dir: Path) -> bool:
    """True iff ``cancel_request.json`` exists in *group_dir*. The runner
    polls this between steps; the file is removed on observation so a
    stale cancel from a previous run can't poison the next."""
    return (Path(group_dir) / CANCEL_REQUEST_FILENAME).exists()


def consume_cancel_request(group_dir: Path) -> None:
    """Remove the cancel marker after the runner has honored it.
    Tolerant of the file being missing (the user could have removed it
    by hand)."""
    path = Path(group_dir) / CANCEL_REQUEST_FILENAME
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("could not remove %s: %s", path, exc)


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat()


class ReprocessRequest(BaseModel):
    """Per-recording override knobs.

    The file is hand-editable (it's a small JSON), but the tray / TTT
    integrations are the typical writers. Only fields that are set
    (non-None / non-default) take effect — every other step keeps the
    pipeline's global config.
    """

    # Sets ``stabilization_strength`` on the stabilize step's config.
    # When None, no change. ``StabilizeStepConfig._apply_strength_preset``
    # validates the value, so a typo here surfaces as a step-construction
    # error rather than a silent miss.
    stabilization_strength: str | None = None

    # Cheap-reprocess: swap the detect step for transform_detections so
    # the previous run's detections get forwarded through the new
    # motion.json instead of re-running ONNX. Saves ~minutes per game.
    skip_detect: bool = False

    # Config swap (e.g. autocam -> homegrown): when set, this run's processing
    # pipeline is REBUILT from the named preset instead of the pipeline's global
    # config. Lets a camera manager reprocess a video under a different provider
    # from the status page. None = keep the global config.
    config_preset: str | None = None

    # Provenance: ISO 8601 timestamp + who asked. Surfaced in logs only.
    requested_at: str | None = None
    requested_by: str | None = None  # e.g. "tray" or "ttt:user-uuid"


def read_reprocess_request(group_dir: Path) -> ReprocessRequest | None:
    """Return the parsed request, or None if no override file is present."""
    path = Path(group_dir) / REPROCESS_REQUEST_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(
            "reprocess: %s exists but could not be parsed (%s: %s); ignoring",
            path,
            type(e).__name__,
            e,
        )
        return None
    try:
        return ReprocessRequest.model_validate(data)
    except Exception as e:
        logger.warning(
            "reprocess: %s failed schema validation (%s); ignoring",
            path,
            e,
        )
        return None


def apply_overrides(
    specs: list[StepSpec],
    request: ReprocessRequest,
) -> tuple[list[StepSpec], list[str]]:
    """Return ``(new_specs, preseed_step_ids)``.

    ``new_specs`` is the pipeline with the request's overrides applied:
    the stabilize step's config patched, and (if ``skip_detect`` is set)
    the detect step dropped from the list. The pipeline's existing
    ``transform_detections`` step (in the ``broadcast_stabilized`` preset)
    already handles lifting raw-coord detections into the new stabilized
    space using the patched motion sidecar — so reprocess + skip_detect
    just needs the previous run's raw detections preseeded as input to
    ``transform_detections`` for the ONNX pass to be elided correctly.

    ``preseed_step_ids`` is the step ids whose recorded produced
    artifacts must be replayed into the working manifest BEFORE the
    runner loop, so the replacement step sees them as inputs. For
    ``skip_detect`` this is the dropped detect step's id (so
    ``transform_detections`` sees the previous detection JSON).
    """
    # A config-preset swap replaces the whole processing pipeline for this run
    # (autocam <-> homegrown): the preset defines the new ordered step list and
    # nothing is preseeded (a fresh run under the new provider). The manifest was
    # already invalidated by the restart handler, so the new steps run clean.
    if request.config_preset:
        from video_grouper.pipeline.presets import get_preset

        try:
            rows = get_preset(request.config_preset)
        except Exception as e:  # noqa: BLE001 — unknown preset -> keep global config
            logger.warning(
                "reprocess: unknown config_preset %r (%s); keeping global config",
                request.config_preset,
                e,
            )
        else:
            swapped = [
                StepSpec(step_id=sid, type=stype, config=dict(cfg))
                for sid, stype, cfg in rows
            ]
            return swapped, []

    new_specs: list[StepSpec] = []
    preseed: list[str] = []
    for spec in specs:
        if spec.type == "stabilize" and request.stabilization_strength is not None:
            patched = dict(spec.config)
            patched["stabilization_strength"] = request.stabilization_strength
            new_specs.append(
                StepSpec(step_id=spec.step_id, type=spec.type, config=patched)
            )
        elif spec.type == "ball_detect" and request.skip_detect:
            # Drop the detect step; the dropped step's produced
            # ``detections_path`` gets preseeded into the manifest so
            # the downstream ``transform_detections`` step sees the
            # previous run's raw detections as input.
            preseed.append(spec.step_id)
            continue
        else:
            new_specs.append(spec)
    return new_specs, preseed
