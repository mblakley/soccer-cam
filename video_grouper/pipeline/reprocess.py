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
from pathlib import Path

from pydantic import BaseModel

from video_grouper.pipeline.base import StepSpec

logger = logging.getLogger(__name__)


REPROCESS_REQUEST_FILENAME = "reprocess_request.json"


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
    the detect step replaced by a ``transform_detections`` step using
    the same step_id slot — so the runner's fingerprint-based skip logic
    naturally recognises a type change as "step changed, re-run".

    ``preseed_step_ids`` is the step ids whose recorded produced
    artifacts must be replayed into the working manifest BEFORE the
    runner loop, so the replacement step sees them as inputs. For
    ``skip_detect`` this is the old detect step's id (``transform_detections``
    needs the previous detection JSON as its input).
    """
    new_specs: list[StepSpec] = []
    preseed: list[str] = []
    for spec in specs:
        if spec.type == "stabilize" and request.stabilization_strength is not None:
            patched = dict(spec.config)
            patched["stabilization_strength"] = request.stabilization_strength
            new_specs.append(
                StepSpec(step_id=spec.step_id, type=spec.type, config=patched)
            )
        elif spec.type == "detect" and request.skip_detect:
            # Same step_id, different type — the runner's fingerprint
            # change drives a re-run with the new step's config.
            new_specs.append(
                StepSpec(
                    step_id=spec.step_id,
                    type="transform_detections",
                    config={},
                )
            )
            # The replacement step CONSUMES detections_path — needs the
            # old detect record's produced detections_path replayed.
            preseed.append(spec.step_id)
        else:
            new_specs.append(spec)
    return new_specs, preseed
