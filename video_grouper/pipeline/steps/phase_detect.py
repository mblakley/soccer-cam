"""Phase-detect step — fuse the game-phase boundaries (kickoff / halftime /
second-half / end) from the multi-signal detector.

Runs the offline game-phase detector
(:mod:`video_grouper.inference.phase_detector`) on the step's input video using
the field polygon ``field_detect`` produced (and the AutoCam ball sidecar when
one is present next to the video). Writes ``phases.json`` next to the input and
records ``phases_path`` in the manifest; the four boundary offsets are also
persisted to the group's ``state.json`` under ``game_phases`` (source
``phase_fused``) for the later TTT push (S2).

The detector's sanity gate may reject an implausible fit (``ok == False``); the
step still records the (un-trusted) result so a consumer can see what was found,
and a ``None`` result (no play detected at all) is recorded as an empty
``ok=false`` artifact — the declared ``phases_path`` output is therefore always
written, so the runner's non-empty-output contract holds.

Ordered AFTER ``field_detect`` (it consumes the polygon that step produces).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import cast

from pydantic import BaseModel

# Top-level import: pulls in onnxruntime/cv2/av (the detector core imports them
# at module top). In a bundle without the inference stack importing this module
# fails and register_steps' try/except omits the step — same pattern as
# field_detect / ball_detect.
from video_grouper.inference.phase_detector import PersonModelUnavailable, detect_phases
from video_grouper.pipeline import register_step
from video_grouper.pipeline.base import PipelineStep, StepContext
from video_grouper.pipeline.manifest import PipelineManifest

logger = logging.getLogger(__name__)

# The four fused boundaries, in the order the detector reports them.
PHASE_KEYS = ("kickoff", "halftime", "second_half", "end")


class PhaseDetectStepConfig(BaseModel):
    # Decode cadence (seconds between sampled frames) for the player-on-field
    # curve — the detector's backbone signal. Lower = finer halftime
    # localization but slower; mirrors the detector core's default.
    phase_step_seconds: float = 12.0

    # Optional override for the YOLO person-detection model. Unlike ball_detect
    # (freemium: model_key licenses a TTT model) the person model is a base
    # public detector shipped with the install, so the default is the bundled
    # model. Set model_path only to point at a different local .onnx; when it
    # can't be resolved the step degrades to an ok=false artifact (below).
    model_path: str | None = None


def _load_polygon(path: str) -> list:
    """Return the ``polygon`` list from a field_detect ``field_polygon.json``."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("polygon") or []


def _build_payload(result: dict | None) -> dict:
    """Shape the detector result into the persisted phases artifact.

    Always returns a dict (never None) so the declared output is written even
    when the detector found no play — keeping the runner's non-empty-output
    contract satisfied.
    """
    if result is None:
        return {
            "ok": False,
            "source": "phase_fused",
            "times": {},
            "reasons": ["no_play"],
        }
    raw = result.get("times") or {}
    times = {k: float(raw[k]) for k in PHASE_KEYS if k in raw}
    # kickoff is un-clamped in the core; clamp at output (matches the CLI).
    if "kickoff" in times:
        times["kickoff"] = max(0.0, times["kickoff"])
    return {
        "ok": bool(result.get("ok")),
        "source": "phase_fused",
        "times": times,
        "reasons": list(result.get("reasons") or []),
        "used": result.get("used"),
    }


class PhaseDetectStep(PipelineStep[PhaseDetectStepConfig]):
    name = "phase_detect"
    config_model = PhaseDetectStepConfig
    consumes = ("input_path", "field_polygon_path")
    produces = ("phases_path",)
    runtime = "service"
    requires = ("onnxruntime", "cv2", "av")
    resources = ("gpu",)

    async def run(self, manifest: PipelineManifest, ctx: StepContext) -> bool:
        in_path = Path(cast(str, manifest.get("input_path")))
        polygon_path = cast(str, manifest.get("field_polygon_path"))
        polygon = await asyncio.to_thread(_load_polygon, polygon_path)

        try:
            result = await asyncio.to_thread(
                detect_phases,
                str(in_path),
                polygon,
                step=self.config.phase_step_seconds,
                person_model=self.config.model_path,
            )
            payload = _build_payload(result)
        except PersonModelUnavailable:
            # No person model resolvable (not bundled, not configured). The
            # player-on-field curve is the detector's backbone, so there is no
            # meaningful result — record an ok=false artifact rather than fail
            # the pipeline, so downstream steps and the non-empty-output
            # contract still hold. (field_detect degrades the same way.)
            logger.warning(
                "phase_detect: no YOLO person model available; writing ok=false phases artifact. "
                "Set [PIPELINE] phase_detect model_path or install the bundled person model."
            )
            payload = {
                "ok": False,
                "source": "phase_fused",
                "times": {},
                "reasons": ["no_person_model"],
            }

        phases_path = in_path.with_name("phases.json")
        with open(phases_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        manifest.put("phases_path", str(phases_path))

        # Persist to the group state for the later TTT push (S2). Best-effort:
        # the canonical artifact is phases.json + the manifest entry above.
        await asyncio.to_thread(self._persist_to_state, ctx, payload)

        # S2: push the trimmed-time boundaries to the recording's TTT game session
        # (looked up by the group dir). Best-effort + non-fatal; skips cleanly for
        # community installs (TTT disabled) or recordings with no TTT session.
        await asyncio.to_thread(self._push_to_ttt, ctx, payload)

        logger.info(
            "phase_detect: ok=%s times=%s -> %s",
            payload["ok"],
            payload.get("times"),
            phases_path,
        )
        return True

    @staticmethod
    def _persist_to_state(ctx: StepContext, payload: dict) -> None:
        try:
            from video_grouper.models import DirectoryState

            DirectoryState(str(ctx.group_dir)).set_game_phases(payload)
        except Exception as e:  # noqa: BLE001 — state persistence is best-effort
            logger.warning(
                "phase_detect: could not persist phases to state.json: %s", e
            )

    @staticmethod
    def _push_to_ttt(ctx: StepContext, payload: dict) -> None:
        from video_grouper.task_processors.phase_ttt_push import push_phases_to_ttt

        push_phases_to_ttt(
            ctx.ttt_config,
            ctx.group_dir.name,
            payload,
            str(ctx.storage_path),
        )


register_step(PhaseDetectStep.name, PhaseDetectStep, PhaseDetectStepConfig)
