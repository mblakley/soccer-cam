"""Reactive recovery for byte-complete-but-corrupt camera segments.

Combine and trim are fast stream-copies that never decode video, so a camera
segment with an undecodable HEVC region (byte-complete — PR #88's size check
passes — yet corrupt on the SD card) rides straight through into the trimmed
``-raw.mp4`` the config-driven pipeline consumes. The FIRST step that actually
DECODES the video (stitch_correct / field_detect / render / frame_fanout — see
the ``homegrown`` preset order) is where it surfaces, as an
``av.error.InvalidDataError`` propagating out of the step.

Rather than decode every game proactively to catch the rare corrupt one
(~realtime cost on every clean game — 90-150 min for a 90-min game), recovery is
REACTIVE: when a pipeline decode step fails, :class:`PipelineProcessor` calls
:func:`recover_pipeline_input` on that game — already known to be corrupt — which:

1. **Localizes** the corruption by null-decoding the SOURCE segments
   (:func:`detect_video_decode_corruption` — the one expensive decode, ONLY here).
2. **Repairs** by re-combining the segments with the corrupt one cut at its last
   clean keyframe (:func:`combine_videos` ``corrupt_starts=`` — the proven path)
   then re-trimming to the same ``-raw.mp4`` the pipeline reads.
3. **Surfaces** the loss: a durable ``video_loss`` flag on state.json + the NTFY
   warning, so the game isn't shipped as if perfect.
4. **Invalidates** the pipeline manifest so the re-run starts fresh on the
   repaired input.

The decode steps run BEFORE upload (upload is queued only on ``pipeline_complete``),
so recovery yields a clean output pre-upload — no re-upload of a corrupt version is
needed. :class:`PipelineProcessor` bounds this to a single attempt per game (a
persisted marker) so a corruption the cut can't resolve fails terminally instead of
looping (the original N=16 retry-storm symptom).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Raw camera segments to re-combine live at the top of the group dir. Mirrors
# CombineTask.VIDEO_EXTENSIONS / EXCLUDE_PREFIXES so recovery re-combines exactly
# the inputs the original combine used (the trimmed ``-raw.mp4`` lives in a
# subdirectory, so a top-level listing never picks it up).
_SOURCE_EXTENSIONS = (".dav", ".mp4")
_EXCLUDE_PREFIXES = ("combined",)


@dataclass
class RecoveryOutcome:
    """Result of a recovery attempt.

    ``repaired`` is True only when source corruption was localized AND the
    combined/trimmed input was successfully rebuilt; the caller then re-runs the
    pipeline on the repaired input. When False, ``reason`` says why (no localized
    corruption, or a repair step failed) and the caller fails the game terminally.
    """

    repaired: bool
    lost_seconds: float = 0.0
    corruptions: list[dict] = field(default_factory=list)
    reason: str | None = None


def _source_segments(group_dir: str) -> list[str]:
    """Top-level raw camera segments in *group_dir* (same selection as CombineTask)."""
    segments: list[str] = []
    try:
        for name in sorted(os.listdir(group_dir)):
            low = name.lower()
            if low.endswith(_SOURCE_EXTENSIONS) and not low.startswith(
                _EXCLUDE_PREFIXES
            ):
                segments.append(os.path.join(group_dir, name))
    except FileNotFoundError:
        pass
    return segments


def _camera_metadata(group_dir: str) -> tuple[str | None, str | None]:
    """Best-effort camera name/type from state.json (re-stamped on the rebuild)."""
    try:
        from video_grouper.models import DirectoryState

        first_file = DirectoryState(group_dir).get_first_file()
        if first_file and first_file.metadata:
            meta = first_file.metadata
            name = meta.get("camera_name")
            ctype = meta.get("camera_type")
            return (
                str(name) if name is not None else None,
                str(ctype) if ctype is not None else None,
            )
    except Exception:  # noqa: BLE001 — metadata is optional, never block recovery
        pass
    return None, None


async def notify_video_corruption(
    ntfy_processor: Any, group_dir: str, corruptions: list[dict]
) -> None:
    """Notify the camera manager (via NTFY) that footage was lost to camera
    recording corruption.

    Unlike an audio gap (auto-corrected, no footage lost), this is a genuine loss:
    a segment was byte-complete but had an undecodable region the camera wrote to
    its SD card, which is unrecoverable (re-downloading returns identical bytes).
    Recovery cut the dead span so the rest of the game stays in sync; this tells the
    user roughly how much video the camera lost. Best-effort: never let a
    notification failure affect recovery.
    """
    try:
        ntfy_api = None
        ntfy_service = getattr(ntfy_processor, "ntfy_service", None)
        if ntfy_service is not None:
            ntfy_api = getattr(ntfy_service, "ntfy_api", None)
        if ntfy_api is None:
            return

        total_lost = sum(c["lost_seconds"] for c in corruptions)
        game = os.path.basename(group_dir.rstrip("/\\"))
        message = (
            f"~{total_lost:.0f}s of {game} was lost to a camera recording "
            f"error ({len(corruptions)} segment(s) had an undecodable video "
            f"region). The dead span was cut so the rest stays in sync. This "
            f"is unrecoverable — the camera's own recording is corrupt, "
            f"re-downloading would return the same bad bytes."
        )
        await ntfy_api.send_notification(
            message=message,
            title=f"Video lost in {game}",
            tags=["warning"],
            priority=4,
        )
        logger.warning(
            "RECOVERY: %.0fs of video lost to camera corruption in %s across "
            "%d segment(s); cut the dead region and notified user",
            total_lost,
            group_dir,
            len(corruptions),
        )
    except Exception as e:  # noqa: BLE001 — notification is best-effort
        logger.error(
            "RECOVERY: failed to send video-corruption NTFY for %s: %s", group_dir, e
        )


async def recover_pipeline_input(
    group_dir: str,
    storage_path: str,
    input_path: str,
    *,
    config: Any,
    ntfy_processor: Any = None,
) -> RecoveryOutcome:
    """Localize source corruption and rebuild the pipeline's input from it.

    Called by :class:`PipelineProcessor` after a pipeline decode step failed with a
    corruption error. ``input_path`` is the trimmed ``-raw.mp4`` the pipeline reads;
    on success it is overwritten with a clean rebuild. See the module docstring.
    """
    from video_grouper.models import DirectoryState, MatchInfo
    from video_grouper.pipeline.manifest import PipelineManifest
    from video_grouper.task_processors.tasks.video import TrimTask
    from video_grouper.utils.ffmpeg_utils import (
        combine_videos,
        detect_video_decode_corruption,
        trim_video,
    )
    from video_grouper.utils.paths import get_combined_video_path

    group_dir = str(group_dir)
    sources = _source_segments(group_dir)
    if not sources:
        return RecoveryOutcome(False, reason="no source segments found to re-combine")

    # Localize: the one expensive null-decode of every source segment, run ONLY
    # here (game already known corrupt), never proactively.
    corruptions = await asyncio.to_thread(detect_video_decode_corruption, sources)
    if not corruptions:
        # The decode failure wasn't a source-segment corruption we can cut around
        # (e.g. a transient/codec issue). Don't loop — let the caller fail it.
        return RecoveryOutcome(
            False, reason="no decodable corruption localized in source segments"
        )

    corrupt_starts = {c["path"]: c["corrupt_start_seconds"] for c in corruptions}
    total_lost = sum(c["lost_seconds"] for c in corruptions)
    detail = "; ".join(
        f"{os.path.basename(c['path'])} "
        f"~{c['lost_seconds']:.0f}s@{c['corrupt_start_seconds']:.0f}s"
        for c in corruptions
    )
    logger.error(
        "RECOVERY: %d corrupt segment(s) in %s (~%.0fs lost); rebuilding "
        "combined/trimmed with keyframe-aware cut: %s",
        len(corruptions),
        os.path.basename(group_dir),
        total_lost,
        detail,
    )

    # Repair (combine): re-combine with the corrupt region cut at its last clean
    # keyframe — the proven _combine_copy degrade path. e6a339f's audio-align keeps
    # A/V synced across the cut.
    camera_name, camera_type = _camera_metadata(group_dir)
    combined_path = get_combined_video_path(group_dir, storage_path)
    ok = await combine_videos(
        sources,
        combined_path,
        camera_name=camera_name,
        camera_type=camera_type,
        corrupt_starts=corrupt_starts,
    )
    if not ok:
        return RecoveryOutcome(
            False,
            corruptions=corruptions,
            reason="re-combine of source segments failed",
        )

    # Repair (trim): re-trim the rebuilt combined back to the SAME -raw.mp4 the
    # pipeline consumes, using the offsets the original trim used.
    match_info, _ = MatchInfo.get_or_create(group_dir, storage_path)
    if match_info is None:
        return RecoveryOutcome(
            False, corruptions=corruptions, reason="no match_info to re-derive trim"
        )
    trim_end = getattr(getattr(config, "processing", None), "trim_end_enabled", False)
    trim = TrimTask.from_match_info(group_dir, match_info, trim_end_enabled=trim_end)
    ok = await trim_video(combined_path, input_path, trim.start_time, trim.end_time)
    if not ok:
        return RecoveryOutcome(
            False, corruptions=corruptions, reason="re-trim of rebuilt combined failed"
        )

    # Surface: durable video_loss flag + NTFY warning, so it isn't shipped as if
    # perfect.
    try:
        DirectoryState(group_dir).set_video_loss(total_lost, detail)
    except Exception as e:  # noqa: BLE001 — flag is best-effort, repair already done
        logger.error("RECOVERY: failed to flag video_loss for %s: %s", group_dir, e)
    await notify_video_corruption(ntfy_processor, group_dir, corruptions)

    # Invalidate the manifest so the pipeline re-runs every step on the repaired
    # input (same path, new bytes — resume would otherwise replay stale outputs).
    manifest_path = PipelineManifest.path_for(group_dir)
    try:
        if os.path.exists(manifest_path):
            os.remove(manifest_path)
    except OSError as e:
        logger.warning("RECOVERY: could not delete manifest %s: %s", manifest_path, e)

    return RecoveryOutcome(
        repaired=True, lost_seconds=total_lost, corruptions=corruptions
    )
