"""S3 — phase-verify NTFY task.

The camera manager confirms each detected boundary: soccer-cam renders a
screenshot at the transition (trimmed-video time) and sends it to the NTFY topic
with Correct / Not Correct buttons; the verdict is written to the TTT game
session's ``phase_*_verified`` column. Verifies ONE boundary per task and chains
to the next (like ``GameStartTask``), so one verify request walks all four.

SECURITY: task metadata is persisted to ``state.json``, so the TTT *connection*
carried here is sanitized (no email/password) — the write authenticates via the
client's stored tokens (the running app logged in at startup).
"""

import asyncio
import logging
import os
from typing import Any

from video_grouper.task_processors.services.ntfy_service import NtfyService
from video_grouper.utils.config import Config

from .base_ntfy_task import BaseNtfyTask, NtfyTaskResult

logger = logging.getLogger(__name__)

_PHASE_LABELS = {
    "kickoff": "Kickoff",
    "halftime": "Halftime",
    "second_half": "Second-half kickoff",
    "end": "End of game",
}
_SECRET_KEYS = ("email", "password")


def sanitize_ttt_conn(ttt_config: dict | None) -> dict:
    """Drop credentials from a TTT config dict — safe to persist in task metadata."""
    return {k: v for k, v in (ttt_config or {}).items() if k not in _SECRET_KEYS}


def _mmss(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


class PhaseVerifyTask(BaseNtfyTask):
    """Ask the camera manager to confirm one detected phase boundary, then chain."""

    def __init__(
        self,
        group_dir: str,
        config: Config,
        ntfy_service: NtfyService,
        *,
        video_path: str | None,
        remaining: list,
        recording_group_dir: str,
        storage_path: str,
        ttt_conn: dict,
        metadata: dict[str, Any] | None = None,
    ):
        # remaining: ordered list of [phase_key, seconds] still to verify; head = current.
        ntfy_cfg = getattr(config, "ntfy", None)
        md: dict[str, Any] = dict(metadata or {})
        md.update(
            {
                "video_path": video_path,
                "remaining": remaining,
                "recording_group_dir": recording_group_dir,
                "storage_path": str(storage_path),
                "ttt_conn": sanitize_ttt_conn(ttt_conn),
                "config": {
                    "ntfy": {
                        "topic": getattr(ntfy_cfg, "topic", ""),
                        "server_url": getattr(ntfy_cfg, "server_url", ""),
                        "enabled": getattr(ntfy_cfg, "enabled", False),
                    }
                },
            }
        )
        super().__init__(group_dir, config, ntfy_service, md)
        self.video_path = video_path
        self.remaining = remaining
        self.recording_group_dir = recording_group_dir
        self.storage_path = str(storage_path)
        self.ttt_conn = sanitize_ttt_conn(ttt_conn)

    def get_task_type(self) -> str:
        from .enums import NtfyInputType

        return NtfyInputType.PHASE_VERIFY.value

    @property
    def _current(self):
        return self.remaining[0]  # [phase_key, seconds]

    async def create_question(self) -> dict[str, Any]:
        key, seconds = self._current
        label = _PHASE_LABELS.get(key, key)
        offset = _mmss(seconds)
        image_path = None
        if self.video_path and os.path.exists(self.video_path):
            image_path = await self.generate_screenshot(self.video_path, int(seconds))
        topic = self.metadata.get("config", {}).get("ntfy", {}).get("topic", "")
        actions = [
            {
                "action": "http",
                "label": "Correct",
                "url": f"https://ntfy.sh/{topic}",
                "method": "POST",
                "headers": {"Content-Type": "text/plain"},
                "body": f"Correct: {key} at {offset}",
                "clear": True,
            },
            {
                "action": "http",
                "label": "Not Correct",
                "url": f"https://ntfy.sh/{topic}",
                "method": "POST",
                "headers": {"Content-Type": "text/plain"},
                "body": f"Not correct: {key} at {offset}",
                "clear": True,
            },
        ]
        return {
            "message": f"Is this the {label.lower()}? (detected at {offset})",
            "title": f"Verify {label} — {offset}",
            "tags": ["phase_verify", key, offset],
            "priority": 4,
            "image_path": image_path,
            "actions": actions,
            "metadata": self.metadata,
        }

    async def process_response(self, response: str) -> NtfyTaskResult:
        key, _seconds = self._current
        rl = response.lower()
        if "not correct" in rl or "not_correct" in rl:
            verdict = "not_correct"
        elif "correct" in rl:
            verdict = "correct"
        else:
            return NtfyTaskResult(
                success=False, should_continue=True, message=f"Unhandled: {response}"
            )

        from video_grouper.task_processors.phase_ttt_push import push_phase_verified

        await asyncio.to_thread(
            push_phase_verified,
            self.ttt_conn,
            self.recording_group_dir,
            key,
            verdict,
            self.storage_path,
        )
        logger.info("phase_verify: %s = %s for %s", key, verdict, self.group_dir)

        rest = self.remaining[1:]
        if rest:
            return NtfyTaskResult(
                success=True,
                should_continue=True,
                message=f"{key}={verdict}; {len(rest)} left",
                metadata={"next_remaining": rest},
            )
        return NtfyTaskResult(
            success=True,
            should_continue=False,
            message=f"{key}={verdict}; phase verification complete",
        )

    @classmethod
    def create_next_task(cls, current_task, next_remaining):
        return cls(
            group_dir=current_task.group_dir,
            config=current_task.config,
            ntfy_service=current_task.ntfy_service,
            video_path=current_task.video_path,
            remaining=next_remaining,
            recording_group_dir=current_task.recording_group_dir,
            storage_path=current_task.storage_path,
            ttt_conn=current_task.ttt_conn,
        )

    @classmethod
    def deserialize(cls, data: dict[str, object]) -> "PhaseVerifyTask":
        from video_grouper.task_processors.services.ntfy_service import NtfyService
        from video_grouper.utils.config import Config

        raw_md = data.get("metadata") or {}
        md: dict[str, Any] = raw_md if isinstance(raw_md, dict) else {}
        config = Config()
        group_dir = str(data.get("group_dir", ""))
        ntfy_service = NtfyService(config.ntfy, group_dir)
        return cls(
            group_dir=group_dir,
            config=config,
            ntfy_service=ntfy_service,
            video_path=md.get("video_path"),
            remaining=md.get("remaining", []),
            recording_group_dir=md.get("recording_group_dir", ""),
            storage_path=md.get("storage_path", ""),
            ttt_conn=md.get("ttt_conn", {}),
            metadata=md,
        )
