"""Highlight reel processor — polls TTT for pending reels, renders them
locally, and uploads the combined video back to YouTube.

Phase 2 of the highlight-reel pipeline. Phase 1 (in-app preview that chains
existing YouTube clips by timestamp) is shipped on the TTT side; this
processor handles the external-share path that produces a single shareable
YouTube URL.

Per-reel flow:
1. Poll ``GET /api/highlights?status=pending&camera_id=<id>``.
2. For each reel, fetch its ordered game-clips. Each clip carries the
   ``recording_group_dir`` of its source recording (joined server-side via
   game_videos → camera_recordings → game_sessions).
3. Resolve every clip's source ``combined.mp4`` locally. If any source is
   missing on this install, log + skip the reel WITHOUT claiming — another
   camera-manager may have it.
4. Claim via ``PATCH status='generating'``.
5. Trim each clip from its local ``combined.mp4`` using the clip's
   ``start_time``/``end_time``.
6. Concatenate via the existing ``HighlightCompilationTask`` (FFmpeg concat
   demuxer).
7. Upload to YouTube under the user's OAuth (privacy=unlisted).
8. ``PATCH status='ready'`` with file_path + youtube_video_id, or
   ``PATCH status='failed'`` with error_message on any exception.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path

from ..utils.config import Config
from ..utils.ffmpeg_utils import trim_video
from .base_polling_processor import PollingProcessor
from .recording_locator import find_combined_video, resolve_recording_dir
from .tasks.clips.highlight_compilation_task import HighlightCompilationTask

logger = logging.getLogger(__name__)


class HighlightReelProcessor(PollingProcessor):
    """Polls TTT for pending highlight reels and renders/uploads them."""

    def __init__(
        self,
        storage_path: str,
        config: Config,
        ttt_client,
        youtube_uploader,
        poll_interval: int = 60,
    ):
        super().__init__(storage_path, config, poll_interval)
        self.ttt_client = ttt_client
        self.youtube_uploader = youtube_uploader
        self._processing: set[str] = set()
        # Track per-reel render tasks so stop() can cancel them cleanly.
        self._render_tasks: set[asyncio.Task] = set()
        # Suppress repeat "skipped — source not local" logs across polling
        # cycles. Cleared when a reel is no longer pending or moves on.
        self._already_skipped: set[str] = set()

    async def stop(self) -> None:
        """Cancel in-flight render tasks then defer to the polling-loop stop."""
        for t in list(self._render_tasks):
            t.cancel()
        if self._render_tasks:
            await asyncio.gather(*self._render_tasks, return_exceptions=True)
        await super().stop()

    async def _report_progress(self, reel_id: str, stage: str, percent: int) -> None:
        """Fire-and-forget progress report to TTT. Failures are logged, not raised."""
        try:
            await asyncio.to_thread(
                self.ttt_client.update_highlight_progress,
                reel_id,
                stage=stage,
                percent=percent,
            )
        except Exception as exc:
            logger.warning(
                "HIGHLIGHT_REEL: failed to report %s %s=%d%% : %s",
                reel_id,
                stage,
                percent,
                exc,
            )

    async def discover_work(self) -> None:
        """Poll TTT for pending highlight reels and process them."""
        if not self.ttt_client.is_authenticated():
            logger.debug("HIGHLIGHT_REEL: TTT client not authenticated, skipping poll")
            return

        if not self.youtube_uploader:
            logger.debug(
                "HIGHLIGHT_REEL: no YouTube uploader configured, skipping poll"
            )
            return

        camera_id = getattr(self.config.ttt, "camera_id", None) or None

        try:
            reels = await asyncio.to_thread(
                self.ttt_client.get_pending_highlights, camera_id
            )
        except Exception as e:
            logger.error("HIGHLIGHT_REEL: failed to poll TTT for highlights: %s", e)
            return

        # Prune the skip-suppression set: keep only ids still pending. Reels
        # cancelled or completed server-side disappear from the response, and
        # we want their entries garbage-collected so the set doesn't grow
        # unboundedly across the lifetime of the process.
        pending_ids = {r.get("id") for r in reels if r.get("id")}
        self._already_skipped &= pending_ids

        for reel in reels:
            reel_id = reel.get("id")
            if not reel_id or reel_id in self._processing:
                continue

            self._processing.add(reel_id)
            task = asyncio.create_task(self._process_reel(reel))
            self._render_tasks.add(task)
            task.add_done_callback(self._render_tasks.discard)

    async def _process_reel(self, reel: dict) -> None:
        """Dispatch + run the correct render path based on the reel's source.

        - ``source='manual'`` (default) — user-curated game-clip reel, fetched
          from ``GET /api/highlights/{id}/game-clips``. Each clip carries
          int ``start_time``/``end_time`` seconds + ``recording_group_dir``.
        - ``source='moment_tagger'`` — auto-created from tagged moments,
          fetched from ``GET /api/highlights/{id}/moment-clips``. Each clip
          carries float ``clip_start_offset``/``clip_end_offset`` seconds +
          ``recording_group_dir`` (joined from the parent game_session).

        Everything from claim → trim → concat → upload → PATCH ready is
        identical between the two paths and lives in
        ``_process_reel_from_clips``; only the fetch + per-clip offset
        extraction differ.

        ``_processing.discard(reel_id)`` runs in our ``finally`` here so the
        processor never leaks the reel_id even if the helpers raise.
        """
        reel_id = reel["id"]
        try:
            source = reel.get("source", "manual")
            if source == "moment_tagger":
                clips_fn = self.ttt_client.get_highlight_moment_clips
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
                clips_fn = self.ttt_client.get_highlight_game_clips
                fetch_label = "HIGHLIGHT_REEL"

                def extract_offsets(clip: dict) -> tuple[float, float]:
                    return float(clip["start_time"]), float(clip["end_time"])

            try:
                clips = await asyncio.to_thread(clips_fn, reel_id)
            except Exception as e:
                logger.error(
                    "%s: failed to fetch clips for %s: %s", fetch_label, reel_id, e
                )
                return

            if not clips:
                logger.warning(
                    "%s: reel %s has no clips, skipping", fetch_label, reel_id
                )
                return

            await self._process_reel_from_clips(reel, clips, extract_offsets)
        finally:
            self._processing.discard(reel_id)

    async def _process_reel_from_clips(
        self,
        reel: dict,
        clips: list[dict],
        extract_offsets,
    ) -> None:
        """Shared render pipeline: resolve sources → claim → trim → concat →
        upload → PATCH ready (or report blocker / fail).

        ``extract_offsets(clip) -> (start_seconds, end_seconds)`` adapts the
        per-clip shape (game_clips use int start_time/end_time; moment_clips
        use float clip_start_offset/clip_end_offset). Everything else is
        identical between the two reel sources.

        Caller (``_process_reel``) owns the ``_processing.discard(reel_id)``
        cleanup so we don't double-discard.
        """
        reel_id = reel["id"]
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
                        await asyncio.to_thread(
                            self.ttt_client.report_blocker,
                            reel_id,
                            self.config.ttt.camera_id,
                            reason,
                        )
                    return
                combined = find_combined_video(resolved_dir)
                if not combined:
                    if reel_id not in self._already_skipped:
                        reason = f"Clip {idx} combined.mp4 not found in {resolved_dir}"
                        logger.info(
                            "HIGHLIGHT_REEL: reel %s skipped — clip %d combined.mp4 missing in %s",
                            reel_id,
                            idx,
                            resolved_dir,
                        )
                        self._already_skipped.add(reel_id)
                        await asyncio.to_thread(
                            self.ttt_client.report_blocker,
                            reel_id,
                            self.config.ttt.camera_id,
                            reason,
                        )
                    return
                resolved_sources.append((clip, combined))

            # We resolved this reel — clear any prior skip suppression so a
            # future state change re-logs.
            self._already_skipped.discard(reel_id)

            # Claim the reel atomically. Returns None when another camera-manager
            # already claimed it (409) — skip without rendering in that case.
            camera_id = getattr(self.config.ttt, "camera_id", None) or ""
            claim_result = await asyncio.to_thread(
                self.ttt_client.claim_highlight, reel_id, camera_id
            )
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
            await self._report_progress(reel_id, "trimming", 0)

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
                    reel_id, "trimming", int((idx + 1) * 100 / total_clips)
                )

            # Concatenate via the existing HighlightCompilationTask.
            await self._report_progress(reel_id, "concatenating", 0)
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
            await self._report_progress(reel_id, "concatenating", 100)

            final_output_path = task.output_path

            # Idempotent-upload guard: if a prior run uploaded to YouTube but
            # failed to PATCH complete (transient TTT outage), the reel already
            # carries a youtube_video_id. Reuse it instead of creating an orphan.
            try:
                latest_reel = await asyncio.to_thread(
                    self.ttt_client.get_highlight, reel_id
                )
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
                await asyncio.to_thread(
                    self.ttt_client.complete_highlight,
                    reel_id,
                    file_path=final_output_path,
                    youtube_video_id=existing_yt_id,
                )
                logger.info(
                    "HIGHLIGHT_REEL: reel %s ready (youtube_video_id=%s, idempotent)",
                    reel_id,
                    existing_yt_id,
                )
                return

            # Upload to YouTube. on_progress is called on the uploader's thread
            # so it cannot await — schedule the PATCH back on this loop instead.
            await self._report_progress(reel_id, "uploading", 0)
            loop = asyncio.get_running_loop()
            last_reported = [-1]

            def _on_upload_progress(pct: int) -> None:
                # Throttle so we don't PATCH on every chunk — only on 5% steps
                # AND a final 100% report from the uploader. Guard against the
                # loop being closed (e.g. shutdown mid-upload) so this never
                # raises RuntimeError into the uploader thread.
                if loop.is_closed():
                    return
                if pct == 100 or pct - last_reported[0] >= 5:
                    last_reported[0] = pct
                    asyncio.run_coroutine_threadsafe(
                        self._report_progress(reel_id, "uploading", pct), loop
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
            # failure so the reel routes through fail_highlight rather than
            # being marked ready with no video.
            if youtube_video_id is None:
                raise RuntimeError(
                    "YouTube upload returned no video id (see soccer-cam logs)"
                )

            # Report back.
            await asyncio.to_thread(
                self.ttt_client.complete_highlight,
                reel_id,
                file_path=final_output_path,
                youtube_video_id=youtube_video_id,
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
                    await asyncio.to_thread(
                        self.ttt_client.fail_highlight, reel_id, str(e)[:500]
                    )
                except Exception as report_exc:
                    logger.error(
                        "HIGHLIGHT_REEL: also failed to report reel %s failure: %s",
                        reel_id,
                        report_exc,
                    )
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)
