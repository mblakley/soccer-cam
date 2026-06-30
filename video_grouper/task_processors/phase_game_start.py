"""Phase-detection game-start resolver (decisions 1, 7, 8).

This is the default way game start is found. When
``[PROCESSING] game_start_method == "phase_detection"`` AND the recording camera
is a whistle-capable Reolink, run the offline game-phase detector on the combined
video and — if its fit passes the sanity gate — write
``match_info.start_time_offset`` from the detected kickoff, using the SAME coarse
``GAME_START_BACKUP_SECONDS`` backup the NTFY walk uses (decision 8; the backup
never cuts into the real start). The caller then skips the NTFY game-start walk.

Falls back (returns ``False`` — the caller runs the NTFY walk unchanged) for:

* ``game_start_method == "ntfy"`` (manual flow forced),
* a Dahua camera (decision 7 — no usable whistle audio),
* a ``None`` / sanity-gate-rejected (``ok == False``) detector fit,
* any unexpected error (detection can never make game start worse than today).

The detector dependency (onnxruntime / cv2 / av) is imported lazily inside
:func:`_run_detector` so importing this module stays cheap in bundles that lack
the inference stack — only the actual Reolink + phase-detection path needs it.
"""

from __future__ import annotations

import logging
import os

# The existing coarse trim backup the NTFY walk applies (decision 8 — reuse it,
# do NOT introduce a new/tighter buffer). This import is light (no ML deps).
from video_grouper.task_processors.tasks.ntfy.game_start_task import (
    GAME_START_BACKUP_SECONDS,
)

logger = logging.getLogger(__name__)


def _camera_is_reolink(config) -> bool:
    """True iff a configured camera is a Reolink.

    Reads the structured ``cameras`` list on a real pydantic Config, and falls
    back to the ``[CAMERA] type`` section accessor used by configparser-based
    test configs. Defensive: never raises on an unusual config shape.
    """
    cams = getattr(config, "cameras", None)
    types: list[str] = []
    if isinstance(cams, list) and cams:
        types = [getattr(c, "type", "") or "" for c in cams]
    else:
        cam = getattr(config, "camera", None)
        ctype = getattr(cam, "type", None)
        if isinstance(ctype, str) and ctype:
            types = [ctype]
    return any(t.strip().lower() == "reolink" for t in types)


def phase_game_start_enabled(config) -> bool:
    """True iff config selects phase-detection game start for a Reolink camera."""
    processing = getattr(config, "processing", None)
    method = getattr(processing, "game_start_method", None) or "phase_detection"
    if method != "phase_detection":
        return False
    return _camera_is_reolink(config)


def _format_offset(seconds: int) -> str:
    """Format whole seconds as the ``MM:SS`` offset the NTFY walk writes."""
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _load_field_polygon(group_dir: str, combined_video_path: str):
    """Best-effort field polygon for the combined video.

    Reuses a ``field_polygon.json`` artifact if one already exists in the group
    dir (e.g. from a prior field_detect run); otherwise returns the neutral
    full-frame rectangle from the video's own dimensions — the same "the field
    IS the frame" default field_detect emits when no model is configured. So the
    detector always has a usable polygon. Returns ``None`` only if the video
    dimensions can't be read.
    """
    import json

    for root, _dirs, files in os.walk(group_dir):
        if "field_polygon.json" in files:
            try:
                with open(
                    os.path.join(root, "field_polygon.json"), encoding="utf-8"
                ) as f:
                    poly = json.load(f).get("polygon")
                if poly:
                    return poly
            except (OSError, json.JSONDecodeError, ValueError):
                pass
            break

    try:
        import av

        with av.open(combined_video_path) as container:
            stream = container.streams.video[0]
            width = stream.codec_context.width
            height = stream.codec_context.height
        return [
            [0.0, 0.0],
            [float(width), 0.0],
            [float(width), float(height)],
            [0.0, float(height)],
        ]
    except Exception as e:  # noqa: BLE001 — degrade to no-polygon -> NTFY fallback
        logger.warning(
            "phase game-start: could not read dimensions of %s: %s",
            combined_video_path,
            e,
        )
        return None


async def _run_detector(group_dir: str, combined_video_path: str) -> dict | None:
    """Load the polygon and run the detector off the event loop."""
    import asyncio

    from video_grouper.inference.phase_detector import detect_phases

    polygon = _load_field_polygon(group_dir, combined_video_path)
    if not polygon:
        return None
    return await asyncio.to_thread(detect_phases, combined_video_path, polygon)


async def maybe_resolve_phase_game_start(
    group_dir: str,
    combined_video_path: str,
    config,
    storage_path: str | None = None,
) -> bool:
    """Try to set game start from phase detection. Returns True iff it did.

    On True: ``match_info.start_time_offset`` has been written from the detected
    kickoff minus :data:`GAME_START_BACKUP_SECONDS`, and the fused phases are
    persisted to the group state (source ``phase_fused``); the caller MUST skip
    the NTFY game-start walk. On False: nothing was written and the caller runs
    the NTFY walk exactly as before.
    """
    if not phase_game_start_enabled(config):
        return False

    from video_grouper.models import MatchInfo

    # Already resolved (an earlier pass set it) — treat as handled so we neither
    # re-run the expensive detector nor fall through to the NTFY walk.
    existing, _ = MatchInfo.get_or_create(group_dir, storage_path)
    if existing and existing.start_time_offset and existing.start_time_offset.strip():
        return True

    if not combined_video_path or not os.path.exists(combined_video_path):
        return False

    try:
        result = await _run_detector(group_dir, combined_video_path)
    except Exception as e:  # noqa: BLE001 — never let detection break the flow
        logger.warning("phase game-start: detector failed for %s: %s", group_dir, e)
        return False

    if not result or not result.get("ok"):
        logger.info(
            "phase game-start: no usable fit for %s (falling back to the NTFY walk)",
            group_dir,
        )
        return False

    times = result.get("times") or {}
    kickoff = times.get("kickoff")
    if kickoff is None:
        return False

    start_seconds = max(0, int(round(float(kickoff))) - GAME_START_BACKUP_SECONDS)
    offset = _format_offset(start_seconds)

    MatchInfo.update_game_times(
        group_dir, start_time_offset=offset, storage_path=storage_path
    )
    _persist_phases(group_dir, storage_path, result)

    logger.info(
        "phase game-start: set start_time_offset=%s for %s "
        "(kickoff=%.1fs - %ds backup)",
        offset,
        group_dir,
        float(kickoff),
        GAME_START_BACKUP_SECONDS,
    )
    return True


def _persist_phases(group_dir: str, storage_path: str | None, result: dict) -> None:
    """Persist the fused phases to the group state (source ``phase_fused``).

    Best-effort: a failure here must not undo the start-time write above.
    """
    try:
        from video_grouper.models import DirectoryState

        times = result.get("times") or {}
        payload = {
            "source": "phase_fused",
            "ok": bool(result.get("ok")),
            "times": {k: float(v) for k, v in times.items() if v is not None},
            "reasons": list(result.get("reasons") or []),
            "used": result.get("used"),
        }
        DirectoryState(group_dir, storage_path).set_game_phases(payload)
    except Exception as e:  # noqa: BLE001 — state persistence is best-effort
        logger.warning(
            "phase game-start: could not persist phases for %s: %s", group_dir, e
        )
