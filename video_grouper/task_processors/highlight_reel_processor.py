"""Highlight reel processor — renders pending reels locally and uploads the
combined video to YouTube.

Phase 2 of the highlight-reel pipeline. Phase 1 (in-app preview that chains
existing YouTube clips by timestamp) is shipped on the TTT side; this
processor handles the external-share path that produces a single shareable
YouTube URL.

Reels arrive from the work source (``IWorkSource``, kind ``highlight_reel``)
via the work poller. Per-reel flow, every report through the same port:

1. ``details`` -> the reel's ordered clips. Each clip carries the
   ``recording_group_dir`` of its source recording.
2. Resolve every clip's source video locally (see ``reel_source_for``): the
   broadcast (ball-following) ``<slug>.mp4`` once it has finished rendering
   from the current trim, else the trimmed full-field ``<slug>-raw.mp4`` (also
   for clips a coach cut from the full-field upload). If any source is missing
   on this install, report ``blocked`` (once) and skip the reel WITHOUT
   claiming — another camera-manager may have it.
3. ``claim`` (None = another camera-manager has it: skip).
4. Trim each clip from that video using the clip's offsets (``progress`` per
   stage).
5. Concatenate via the existing ``HighlightCompilationTask`` (FFmpeg concat
   demuxer).
6. Upload to YouTube under the user's OAuth (privacy=unlisted) — unless
   ``latest`` shows a previous run already uploaded it.
7. ``complete`` with file_path + youtube_video_id, or ``fail`` on any
   exception after the claim.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path

from ..integrations.work import HIGHLIGHT_REEL, IWorkSource, WorkItem
from ..integrations.work.sources.null import NullWorkSource
from ..utils.config import Config
from ..utils.ffmpeg_utils import trim_video
from .base_queue_processor import QueueProcessor
from .queue_type import QueueType
from .recording_locator import (
    ready_broadcast_for,
    resolve_recording_dir,
)
from .tasks.clips.highlight_compilation_task import HighlightCompilationTask
from .tasks.ttt.highlight_reel_task import HighlightReelTask

logger = logging.getLogger(__name__)


def reel_source_for(recording_dir: str, clip: dict) -> str | None:
    """The local video a reel clip is cut from.

    Decided 2026-11-26 (training/docs/DECISIONS.md): moment clips and reels come
    from the broadcast (ball-following) view. Both clip kinds carry offsets on
    the trimmed, game-clock timeline: moment clips are placed from each tag's
    trimmed offset (ClipDiscoveryProcessor), and game clips are cut by a coach
    on the uploaded game video. So:

    - a game clip cut from the full-field upload (``video_type`` ``full`` or
      ``raw``) comes from the trimmed full-field ``<slug>-raw.mp4``;
    - everything else comes from the broadcast ``<slug>.mp4`` once it has
      finished rendering from this trim, else ``<slug>-raw.mp4`` (same
      timeline). The raw path is the exact one the game's match info names.

    Never ``combined.mp4``: it is untrimmed, so these offsets would land early
    by the length of the trimmed-off pre-game footage. Reels were cut from it
    until 2026-11-27.
    """
    raw = _trimmed_raw_for(recording_dir)
    if raw is None:
        return None
    if clip.get("video_type") in _FULL_FIELD_TYPES:
        return raw
    return ready_broadcast_for(raw, recording_dir)


# TTT game_videos.video_type values that mean the full-field (panorama) upload.
_FULL_FIELD_TYPES = ("full", "raw")


def _trimmed_raw_for(recording_dir: str) -> str | None:
    """The game's trimmed ``<slug>-raw.mp4`` at the exact path its match info
    names -- the same path clip discovery uses -- or None. Never a directory
    scan: a re-trim after a team/opponent fix can leave an older game
    subdirectory beside the current one."""
    from video_grouper.models import MatchInfo
    from video_grouper.utils.paths import get_trimmed_video_path

    ini = os.path.join(recording_dir, "match_info.ini")
    info = MatchInfo.from_file(ini) if os.path.exists(ini) else None
    if info is None:
        return None
    raw = get_trimmed_video_path(
        recording_dir,
        info,
        os.path.dirname(os.path.abspath(recording_dir)),
        create_dir=False,
    )
    return raw if os.path.isfile(raw) else None


class HighlightReelProcessor(QueueProcessor):
    """Drains the highlight-reel queue. ``process_item`` runs the
    full trim → concat → upload flow under the shared ``ram_heavy``
    gate so FFmpeg work can't overlap the main pipeline's render
    step.

    The "source not local" skip-and-don't-claim path used to live in
    the polling loop; it now happens in ``process_item`` (the
    processor sees an item the poller already enqueued, then defers it
    by raising — the base class requeue logic handles retries).
    """

    def __init__(
        self,
        storage_path: str,
        config: Config,
        work: IWorkSource | None = None,
        youtube_uploader=None,
        resource_manager=None,
    ):
        super().__init__(storage_path, config)
        self.work: IWorkSource = work or NullWorkSource()
        self.youtube_uploader = youtube_uploader
        self.resource_manager = resource_manager
        # Suppress repeat "skipped — source not local" logs across
        # retries. Used by `_process_reel_from_clips`.
        self._already_skipped: set[str] = set()

    @property
    def queue_type(self) -> QueueType:
        return QueueType.TTT_HIGHLIGHT_REEL

    def get_item_key(self, item: HighlightReelTask) -> str:
        return item.ttt_id

    async def process_item(self, item: HighlightReelTask) -> None:
        if self.youtube_uploader is None:
            raise RuntimeError("HighlightReelProcessor needs a youtube_uploader")
        if self.resource_manager is not None:
            async with self.resource_manager.acquire(("ram_heavy",)):
                await self._process_reel(item.payload)
        else:
            await self._process_reel(item.payload)

    async def _report_progress(self, reel: WorkItem, stage: str, percent: int) -> None:
        """Fire-and-forget progress report. Failures are logged, not raised."""
        try:
            await self.work.progress(reel, stage, percent)
        except Exception as exc:
            logger.warning(
                "HIGHLIGHT_REEL: failed to report %s %s=%d%% : %s",
                reel.id,
                stage,
                percent,
                exc,
            )

    async def _process_reel(self, reel: dict) -> None:
        """Dispatch + run the correct render path based on the reel's source.

        The work source's ``details`` returns the clips for the reel's source:

        - ``source='manual'`` (default) — user-curated game clips. Each clip
          carries int ``start_time``/``end_time`` seconds + ``recording_group_dir``.
        - ``source='moment_tagger'`` — auto-created from tagged moments. Each
          clip carries float ``clip_start_offset``/``clip_end_offset`` seconds +
          ``recording_group_dir`` (joined from the parent game_session).

        Everything from claim → trim → concat → upload → complete is
        identical between the two paths and lives in
        ``_process_reel_from_clips``; only the fetch + per-clip offset
        extraction differ.

        Failures inside ``_process_reel_from_clips`` propagate so the
        QueueProcessor base requeues / nacks per its normal retry
        rules.
        """
        reel_id = reel["id"]
        source = reel.get("source", "manual")
        if source == "moment_tagger":
            fetch_label = "MOMENT_REEL"

            def extract_offsets(clip: dict) -> tuple[float, float]:
                start = clip.get("clip_start_offset")
                end = clip.get("clip_end_offset")
                if start is None or end is None:
                    raise ValueError(
                        f"moment_clip {clip.get('id')} missing clip_start_offset"
                        f" / clip_end_offset (got start={start!r}, end={end!r})"
                    )
                return float(start), float(end)
        else:
            fetch_label = "HIGHLIGHT_REEL"

            def extract_offsets(clip: dict) -> tuple[float, float]:
                return float(clip["start_time"]), float(clip["end_time"])

        work_item = WorkItem(HIGHLIGHT_REEL, reel_id, reel)
        try:
            clips = (await self.work.details(work_item)).get("clips") or []
        except Exception as e:
            logger.error(
                "%s: failed to fetch clips for %s: %s", fetch_label, reel_id, e
            )
            return

        if not clips:
            logger.warning("%s: reel %s has no clips, skipping", fetch_label, reel_id)
            return

        await self._process_reel_from_clips(work_item, clips, extract_offsets)

    async def _process_reel_from_clips(
        self,
        work_item: WorkItem,
        clips: list[dict],
        extract_offsets,
    ) -> None:
        """Shared render pipeline: resolve sources → claim → trim → concat →
        upload → complete (or report blocked / fail).

        ``extract_offsets(clip) -> (start_seconds, end_seconds)`` adapts the
        per-clip shape (game_clips use int start_time/end_time; moment_clips
        use float clip_start_offset/clip_end_offset). Everything else is
        identical between the two reel sources.

        Caller (``_process_reel``) owns the ``_processing.discard(reel_id)``
        cleanup so we don't double-discard.
        """
        reel = work_item.payload
        reel_id = work_item.id
        tmpdir: str | None = None
        final_output_path: str | None = None
        claimed = False

        try:
            # Defensive dedup: TTT should return each clip once, but if the
            # backend ever leaks duplicates we don't want to render them.
            seen: set = set()
            deduped: list[dict] = []
            for clip in clips:
                cid = clip.get("id")
                if cid and cid not in seen:
                    seen.add(cid)
                    deduped.append(clip)
            clips = deduped

            # Resolve every clip's source BEFORE claiming. If any are missing on
            # this install, another camera-manager may have them — bare return,
            # no claim, no fail.
            resolved_sources = []
            for idx, clip in enumerate(clips):
                recording_group_dir = clip.get("recording_group_dir")
                resolved_dir = resolve_recording_dir(
                    self.storage_path, recording_group_dir
                )
                if not resolved_dir:
                    if reel_id not in self._already_skipped:
                        reason = (
                            f"Clip {idx} source unavailable: "
                            f"recording_group_dir is None"
                            if recording_group_dir is None
                            else f"Clip {idx} source dir not found on this camera-manager: "
                            f"{recording_group_dir}"
                        )
                        logger.info(
                            "HIGHLIGHT_REEL: reel %s skipped — clip %d source not local "
                            "(recording_group_dir=%r)",
                            reel_id,
                            idx,
                            recording_group_dir,
                        )
                        self._already_skipped.add(reel_id)
                        await self.work.blocked(work_item, reason)
                    return
                source = reel_source_for(resolved_dir, clip)
                if not source:
                    if reel_id not in self._already_skipped:
                        reason = (
                            f"Clip {idx}: no trimmed game video in {resolved_dir}. "
                            "The game must be trimmed (match info + trim) before a "
                            "reel can be cut; recordings from before trimming "
                            "existed only have combined.mp4 and can't be used."
                        )
                        logger.info(
                            "HIGHLIGHT_REEL: reel %s skipped — clip %d has no trimmed game video in %s",
                            reel_id,
                            idx,
                            resolved_dir,
                        )
                        self._already_skipped.add(reel_id)
                        await self.work.blocked(work_item, reason)
                    return
                resolved_sources.append((clip, source))

            # We resolved this reel — clear any prior skip suppression so a
            # future state change re-logs.
            self._already_skipped.discard(reel_id)

            # Claim the reel atomically. Returns None when another camera-manager
            # already claimed it — skip without rendering in that case.
            claim_result = await self.work.claim(work_item)
            if claim_result is None:
                logger.info(
                    "HIGHLIGHT_REEL: reel %s already claimed by another camera-manager"
                    " — skipping",
                    reel_id,
                )
                return
            claimed = True
            logger.info(
                "HIGHLIGHT_REEL: claimed reel %s (%d clips)",
                reel_id,
                len(resolved_sources),
            )

            total_clips = len(resolved_sources)
            await self._report_progress(work_item, "trimming", 0)

            # Trim each clip into a tmpdir. Use float for start/end to preserve
            # sub-second precision — moment-tag offsets may legitimately carry
            # fractional seconds that int() would otherwise truncate.
            tmpdir = tempfile.mkdtemp(prefix=f"reel-{reel_id}-")
            trimmed_paths: list[str] = []
            for idx, (clip, source) in enumerate(resolved_sources):
                start, end = extract_offsets(clip)
                duration = end - start
                if duration <= 0:
                    raise ValueError(
                        f"Clip {idx} has non-positive duration: start={start} end={end}"
                    )
                out_path = os.path.join(tmpdir, f"clip-{idx:03d}.mp4")
                ok = await trim_video(
                    source, out_path, f"{start:.3f}", f"{duration:.3f}"
                )
                if not ok:
                    raise RuntimeError(
                        f"trim_video failed for clip {idx} ({source} {start}-{end})"
                    )
                trimmed_paths.append(out_path)
                await self._report_progress(
                    work_item,
                    "trimming",
                    int((idx + 1) * 100 / total_clips),
                )

            # Concatenate via the existing HighlightCompilationTask.
            await self._report_progress(work_item, "concatenating", 0)
            output_dir = str(Path(self.storage_path) / "highlights")
            task = HighlightCompilationTask(
                highlight_id=reel_id,
                title=reel.get("title") or f"Highlight reel {reel_id}",
                player_name=reel.get("player_name") or "",
                clip_local_paths=tuple(trimmed_paths),
                output_dir=output_dir,
            )
            ok = await task.execute()
            if not ok:
                raise RuntimeError("combine_videos failed during reel concatenation")
            await self._report_progress(work_item, "concatenating", 100)

            final_output_path = task.output_path

            # Idempotent-upload guard: if a prior run uploaded to YouTube but
            # failed to report complete (transient outage), the reel already
            # carries a youtube_video_id. Reuse it instead of creating an orphan.
            try:
                latest_reel = await self.work.latest(work_item)
                existing_yt_id = (latest_reel or {}).get("youtube_video_id")
            except Exception as exc:
                logger.warning(
                    "HIGHLIGHT_REEL: could not refetch reel %s for idempotent check,"
                    " proceeding with upload: %s",
                    reel_id,
                    exc,
                )
                existing_yt_id = None

            if existing_yt_id:
                logger.warning(
                    "HIGHLIGHT_REEL: reel %s already has youtube_video_id=%s"
                    " — skipping re-upload, marking ready",
                    reel_id,
                    existing_yt_id,
                )
                await self.work.complete(
                    work_item,
                    {
                        "file_path": final_output_path,
                        "youtube_video_id": existing_yt_id,
                    },
                )
                logger.info(
                    "HIGHLIGHT_REEL: reel %s ready (youtube_video_id=%s, idempotent)",
                    reel_id,
                    existing_yt_id,
                )
                return

            # Upload to YouTube. on_progress is called on the uploader's thread
            # so it cannot await — schedule the report back on this loop instead.
            await self._report_progress(work_item, "uploading", 0)
            loop = asyncio.get_running_loop()
            last_reported = [-1]

            def _on_upload_progress(pct: int) -> None:
                # Throttle so we don't report on every chunk — only on 5% steps
                # AND a final 100% report from the uploader. Guard against the
                # loop being closed (e.g. shutdown mid-upload) so this never
                # raises RuntimeError into the uploader thread.
                if loop.is_closed():
                    return
                if pct == 100 or pct - last_reported[0] >= 5:
                    last_reported[0] = pct
                    asyncio.run_coroutine_threadsafe(
                        self._report_progress(work_item, "uploading", pct),
                        loop,
                    )

            player_name = reel.get("player_name")
            description = (
                f"Highlight reel for {player_name}"
                if player_name
                else f"Highlight reel: {reel.get('title') or reel_id}"
            )
            youtube_video_id = await asyncio.to_thread(
                self.youtube_uploader.upload_video,
                final_output_path,
                task.title,
                description,
                None,  # tags
                "unlisted",  # privacy_status
                None,  # playlist_id
                _on_upload_progress,
            )

            # `upload_video` returns None on internal exceptions (HttpError /
            # auth refresh failures) instead of raising. Treat that as a
            # failure so the reel routes through fail rather than
            # being marked ready with no video.
            if youtube_video_id is None:
                raise RuntimeError(
                    "YouTube upload returned no video id (see soccer-cam logs)"
                )

            # Report back.
            await self.work.complete(
                work_item,
                {"file_path": final_output_path, "youtube_video_id": youtube_video_id},
            )
            logger.info(
                "HIGHLIGHT_REEL: reel %s ready (youtube_video_id=%s)",
                reel_id,
                youtube_video_id,
            )

        except Exception as e:
            logger.error(
                "HIGHLIGHT_REEL: reel %s failed: %s", reel_id, e, exc_info=True
            )
            if claimed:
                try:
                    await self.work.fail(work_item, str(e))
                except Exception as report_exc:
                    logger.error(
                        "HIGHLIGHT_REEL: also failed to report reel %s failure: %s",
                        reel_id,
                        report_exc,
                    )
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)
