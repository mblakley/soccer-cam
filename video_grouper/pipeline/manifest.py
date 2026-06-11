"""The pipeline manifest — filesystem state handoff + resumability.

Each game's video-group directory gets a ``pipeline_state.json`` (sibling to
``state.json``) that records:

- ``artifacts``: a map of artifact key -> absolute file path. This is how one
  step hands state to the next — a step reads the paths named in its
  ``consumes`` and records the paths it writes via :meth:`put`.
- ``steps``: a per-step record (status, fingerprint, produced artifacts,
  timestamps, which runtime ran it). This is what makes the pipeline
  *resumable*: on restart the runner skips a step whose fingerprint matches and
  whose status is ``complete``, instead of redoing hours of GPU work.

Kept separate from ``state.json`` (the coarse lifecycle status) so the two
have independent locks and a manifest write can never disturb the lifecycle
status the discovery/upload processors key on.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from video_grouper.utils.atomic_json import read_json, write_json

MANIFEST_FILENAME = "pipeline_state.json"
MANIFEST_VERSION = 1


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PipelineManifest:
    """Wraps ``<group_dir>/pipeline_state.json``.

    The in-memory ``data`` dict is the source of truth during a run; mutating
    helpers persist at step boundaries (:meth:`mark_running` / :meth:`mark_complete`
    / :meth:`mark_failed` / :meth:`mark_skipped`). :meth:`put` updates artifacts
    in memory and is flushed by the next ``mark_*`` (or an explicit :meth:`save`).
    """

    def __init__(self, path: str, data: dict):
        self.path = path
        self.data = data

    # ------------------------------------------------------------------
    # Construction / persistence
    # ------------------------------------------------------------------

    @classmethod
    def path_for(cls, group_dir: str | os.PathLike) -> str:
        return os.path.join(str(group_dir), MANIFEST_FILENAME)

    @classmethod
    def load_or_init(
        cls,
        group_dir: str | os.PathLike,
        input_path: str,
        output_path: str,
    ) -> PipelineManifest:
        """Load an existing manifest, or initialize a fresh one.

        A manifest from an unrecognized version is discarded and re-initialized
        (no in-place upgrades for v1).
        """
        path = cls.path_for(group_dir)
        data = read_json(path, default=None)
        if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION:
            data = {
                "version": MANIFEST_VERSION,
                "input_path": str(input_path),
                "output_path": str(output_path),
                "artifacts": {
                    "input_path": str(input_path),
                    "output_path": str(output_path),
                },
                "steps": [],
            }
        return cls(path, data)

    def save(self) -> None:
        write_json(self.path, self.data)

    # ------------------------------------------------------------------
    # Artifacts
    # ------------------------------------------------------------------

    def get(self, key: str) -> Any | None:
        """Return the artifact path stored under *key*, or ``None``."""
        return self.data["artifacts"].get(key)

    def put(self, key: str, value: str) -> None:
        """Record an artifact path in memory (persisted at the next ``mark_*``)."""
        self.data["artifacts"][key] = value

    @property
    def output_path(self) -> str:
        return self.data["output_path"]

    @property
    def artifacts(self) -> dict[str, str]:
        """The live working artifact map (key -> path)."""
        return self.data["artifacts"]

    def reset_working_artifacts(self) -> None:
        """Reset the working artifact map to the immutable source input + output,
        discarding any rebinds.

        The runner replays each *skipped* step's recorded produced entries on top
        of this, so a step that re-runs sees correct upstream state rather than
        its own stale prior outputs (e.g. stitch_correct rebinding input_path to
        a corrected file that no longer exists).
        """
        self.data["artifacts"] = {
            "input_path": self.data["input_path"],
            "output_path": self.data["output_path"],
        }

    def replay_step(self, step_id: str) -> None:
        """Re-apply a completed step's recorded produced/rebound artifacts."""
        self.data["artifacts"].update(self.produced_paths(step_id))

    # ------------------------------------------------------------------
    # Step records
    # ------------------------------------------------------------------

    def _find(self, step_id: str) -> dict | None:
        for rec in self.data["steps"]:
            if rec["step_id"] == step_id:
                return rec
        return None

    def _upsert(self, step_id: str, step_type: str) -> dict:
        rec = self._find(step_id)
        if rec is None:
            rec = {"step_id": step_id, "type": step_type}
            self.data["steps"].append(rec)
        return rec

    def status_of(self, step_id: str) -> str | None:
        rec = self._find(step_id)
        return rec.get("status") if rec else None

    def is_complete(self, step_id: str, fingerprint: str) -> bool:
        """True iff *step_id* completed with a matching config fingerprint.

        The runner additionally checks that the recorded ``produces`` paths
        still exist + are non-empty before honoring the skip.
        """
        rec = self._find(step_id)
        return bool(
            rec
            and rec.get("status") == "complete"
            and rec.get("config_fingerprint") == fingerprint
        )

    def produced_paths(self, step_id: str) -> dict[str, str]:
        rec = self._find(step_id)
        return dict(rec.get("produced", {})) if rec else {}

    def mark_running(
        self, step_id: str, step_type: str, fingerprint: str, ran_in: str
    ) -> None:
        rec = self._upsert(step_id, step_type)
        rec.update(
            {
                "status": "running",
                "config_fingerprint": fingerprint,
                "ran_in": ran_in,
                "started_at": _now(),
                "finished_at": None,
                "error": None,
            }
        )
        self.save()

    def mark_complete(
        self,
        step_id: str,
        produced: dict[str, str] | None = None,
        step_type: str | None = None,
    ) -> None:
        rec = self._upsert(step_id, step_type or step_id)
        produced = produced or {}
        rec.update({"status": "complete", "finished_at": _now(), "produced": produced})
        self.data["artifacts"].update(produced)
        self.save()

    def mark_skipped(self, step_id: str) -> None:
        rec = self._upsert(step_id, step_id)
        rec.update({"status": "skipped", "finished_at": _now()})
        self.save()

    def mark_awaiting(self, step_id: str, step_type: str, runtime: str) -> None:
        """Record that *step_id* is waiting for the *runtime* it must run in.

        Status is ``awaiting_<runtime>`` (e.g. ``awaiting_tray``). This is the
        cross-session handoff marker: the service stops at a tray step, the tray
        runs it, and the service resumes — the manifest is the shared medium.
        """
        rec = self._upsert(step_id, step_type)
        rec["status"] = f"awaiting_{runtime}"
        self.save()

    def mark_awaiting_tray(self, step_id: str, step_type: str) -> None:
        """Back-compat shorthand for ``mark_awaiting(..., "tray")``."""
        self.mark_awaiting(step_id, step_type, "tray")

    def mark_failed(
        self, step_id: str, error: str, step_type: str | None = None
    ) -> None:
        rec = self._upsert(step_id, step_type or step_id)
        rec.update({"status": "failed", "finished_at": _now(), "error": error})
        self.save()
