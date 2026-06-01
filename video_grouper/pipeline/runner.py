"""PipelineRunner — run an ordered list of steps against one game's manifest.

Replaces ``HomegrownProvider.run``. Responsibilities:

- **Order + handoff:** run each step in order. A step whose ``runtime`` doesn't
  match this runner's runtime is a cross-session handoff point — mark it
  ``awaiting_<runtime>`` and stop; the other process (service<->tray) resumes
  from the persisted manifest.
- **Resume:** rebuild the working artifact map from the immutable source each
  run, replaying only steps that are skipped. A step is skipped iff
  ``not dirty`` and it completed with a matching config fingerprint and its
  recorded outputs still exist. The first re-run sets ``dirty``, forcing every
  later step to re-run (an upstream artifact changed).
- **Contract:** validate a step's declared ``consumes`` are present before
  running, and its declared ``produces`` resolve to non-empty files after.

Concurrency: one runner per game at a time (single async task). Each ``run``
re-loads the manifest fresh, so the service<->tray handoff is the only sharing.

Resource gating (serializing GPU/UI-bound steps) is layered on in the
orchestration phase; the runner here is pure sequencing + resume + handoff.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from video_grouper.pipeline import create_step, get_step_meta
from video_grouper.pipeline.base import StepContext, StepSpec
from video_grouper.pipeline.manifest import PipelineManifest

if TYPE_CHECKING:
    from video_grouper.pipeline.resources import ResourceManager

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    """Outcome of a (partial) pipeline run.

    - ``complete``: every step finished and the final output exists.
    - ``awaiting``: stopped at a step that must run in ``awaiting_runtime``
      (cross-session handoff); not an error.
    - ``failed``: a step failed; ``failed_step`` / ``error`` describe it.
    """

    status: str
    awaiting_runtime: str | None = None
    failed_step: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "complete"


def _nonempty_file(path: str | None) -> bool:
    return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0


def fingerprint(spec: StepSpec) -> str:
    """Stable fingerprint of a step's type + config (drives resume invalidation)."""
    payload = json.dumps(
        {"type": spec.type, "config": spec.config},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PipelineRunner:
    def __init__(
        self,
        specs: list[StepSpec],
        runtime: str = "service",
        resource_manager: "ResourceManager | None" = None,
    ):
        self.specs = list(specs)
        self.runtime = runtime
        self.resource_manager = resource_manager

    async def run(
        self, input_path: str, output_path: str, ctx: StepContext
    ) -> PipelineResult:
        manifest = PipelineManifest.load_or_init(ctx.group_dir, input_path, output_path)
        # Rebuild the working artifact map from the immutable source; skipped
        # steps are replayed on top so a re-run never sees its own stale output.
        manifest.reset_working_artifacts()
        dirty = False

        for spec in self.specs:
            fp = fingerprint(spec)

            # Resume: skip an unchanged, already-complete step whose outputs survive.
            if (
                not dirty
                and manifest.is_complete(spec.step_id, fp)
                and self._recorded_outputs_valid(manifest, spec.step_id)
            ):
                manifest.replay_step(spec.step_id)
                logger.info("pipeline: skipping completed step %s", spec.step_id)
                continue

            try:
                meta = get_step_meta(spec.type)
            except ValueError as e:
                return self._fail(manifest, spec.step_id, str(e), spec.type)

            # Cross-session handoff: this runtime can't run the step.
            if meta.runtime not in ("any", self.runtime):
                manifest.mark_awaiting(spec.step_id, spec.type, meta.runtime)
                logger.info(
                    "pipeline: step %s needs runtime %r; handing off (this=%r)",
                    spec.step_id,
                    meta.runtime,
                    self.runtime,
                )
                return PipelineResult("awaiting", awaiting_runtime=meta.runtime)

            # Runtime matches but the step's deps aren't in this bundle.
            if not meta.available:
                missing = ", ".join(meta.requires)
                return self._fail(
                    manifest,
                    spec.step_id,
                    f"step {spec.type!r} unavailable in this bundle (needs {missing})",
                    spec.type,
                )

            # From here on every step re-runs (an upstream artifact changed).
            dirty = True

            try:
                step = create_step(spec.type, spec.config)
            except Exception as e:  # noqa: BLE001 — config/validation surfaced as failure
                return self._fail(
                    manifest, spec.step_id, f"could not construct step: {e}", spec.type
                )

            missing_inputs = [k for k in step.consumes if not manifest.get(k)]
            if missing_inputs:
                return self._fail(
                    manifest,
                    spec.step_id,
                    f"missing required inputs: {', '.join(missing_inputs)}",
                    spec.type,
                )

            before = dict(manifest.artifacts)
            manifest.mark_running(spec.step_id, spec.type, fp, self.runtime)
            try:
                ok = await self._run_step(step, manifest, ctx)
            except Exception as e:  # noqa: BLE001 — surface as a failed step
                logger.exception("pipeline: step %s raised", spec.step_id)
                return self._fail(manifest, spec.step_id, f"exception: {e}", spec.type)

            if not ok:
                return self._fail(
                    manifest, spec.step_id, "step returned False", spec.type
                )

            # Strictly validate declared outputs exist + are non-empty.
            for key in step.produces:
                if not _nonempty_file(manifest.get(key)):
                    return self._fail(
                        manifest,
                        spec.step_id,
                        f"declared output {key!r} missing or empty",
                        spec.type,
                    )

            # Record declared outputs AND any rebound artifacts (keys whose value
            # changed), so resume can re-validate them — covers optional steps
            # like stitch that rebind input_path without a declared output.
            after = manifest.artifacts
            produced_keys = set(step.produces) | {
                k for k, v in after.items() if before.get(k) != v
            }
            produced = {k: after[k] for k in produced_keys if after.get(k)}

            manifest.mark_complete(spec.step_id, produced, step_type=spec.type)
            logger.info("pipeline: step %s complete", spec.step_id)

        out = manifest.output_path
        if not _nonempty_file(out):
            culprit = self.specs[-1] if self.specs else None
            msg = f"output {out} missing or empty after pipeline"
            if culprit is not None:
                # Attribute to the final step so resume re-runs it.
                manifest.mark_failed(culprit.step_id, msg, step_type=culprit.type)
            logger.error("pipeline: %s", msg)
            return PipelineResult(
                "failed",
                failed_step=culprit.step_id if culprit else None,
                error=msg,
            )
        return PipelineResult("complete")

    # ------------------------------------------------------------------

    async def _run_step(
        self, step, manifest: PipelineManifest, ctx: StepContext
    ) -> bool:
        """Run *step*, serialized on its declared resources when a manager is set.

        With no resource manager — or a step that declares no resources — this is
        a transparent passthrough, so the pure-sequencing tests stay unaffected.
        """
        if self.resource_manager is not None and step.resources:
            async with self.resource_manager.acquire(step.resources):
                return await step.run(manifest, ctx)
        return await step.run(manifest, ctx)

    def _recorded_outputs_valid(self, manifest: PipelineManifest, step_id: str) -> bool:
        return all(_nonempty_file(p) for p in manifest.produced_paths(step_id).values())

    def _fail(
        self,
        manifest: PipelineManifest,
        step_id: str,
        error: str,
        step_type: str | None = None,
    ) -> PipelineResult:
        logger.error("pipeline: step %s failed: %s", step_id, error)
        manifest.mark_failed(step_id, error, step_type=step_type)
        return PipelineResult("failed", failed_step=step_id, error=error)
