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
from typing import Optional

from .base_polling_processor import PollingProcessor
from .recording_locator import find_combined_video, resolve_recording_dir
from .tasks.clips.highlight_compilation_task import HighlightCompilationTask
from ..utils.config import Config
from ..utils.ffmpeg_utils import trim_video

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

        for reel in reels:
            reel_id = reel.get("id")
            if not reel_id or reel_id in self._processing:
                continue

            self._processing.add(reel_id)
            asyncio.create_task(self._process_reel(reel))

    async def _process_reel(self, reel: dict) -> None:
        """Process a single highlight reel end-to-end."""
        reel_id = reel["id"]
        tmpdir: Optional[str] = None
        final_output_path: Optional[str] = None
        claimed = False

        try:
            # Fetch the ordered game-clips for this reel.
            try:
                game_clips = await asyncio.to_thread(
                    self.ttt_client.get_highlight_game_clips, reel_id
                )
            except Exception as e:
                logger.error(
                    "HIGHLIGHT_REEL: failed to fetch game clips for %s: %s", reel_id, e
                )
                return

            if not game_clips:
                logger.warning(
                    "HIGHLIGHT_REEL: reel %s has no game clips, skipping", reel_id
                )
                return

            # Resolve every clip's source BEFORE claiming. If any are missing on
            # this install, another camera-manager may have them — bare return,
            # no claim, no fail.
            resolved_sources = []
            for idx, clip in enumerate(game_clips):
                recording_group_dir = clip.get("recording_group_dir")
                resolved_dir = resolve_recording_dir(
                    self.storage_path, recording_group_dir
                )
                if not resolved_dir:
                    logger.info(
                        "HIGHLIGHT_REEL: reel %s skipped — clip %d source not local "
                        "(recording_group_dir=%r)",
                        reel_id,
                        idx,
                        recording_group_dir,
                    )
                    return
                combined = find_combined_video(resolved_dir)
                if not combined:
                    logger.info(
                        "HIGHLIGHT_REEL: reel %s skipped — clip %d combined.mp4 missing in %s",
                        reel_id,
                        idx,
                        resolved_dir,
                    )
                    return
                resolved_sources.append((clip, combined))

            # Claim the reel.
            await asyncio.to_thread(self.ttt_client.claim_highlight, reel_id)
            claimed = True
            logger.info(
                "HIGHLIGHT_REEL: claimed reel %s (%d clips)",
                reel_id,
                len(resolved_sources),
            )

            # Trim each clip into a tmpdir.
            tmpdir = tempfile.mkdtemp(prefix=f"reel-{reel_id}-")
            trimmed_paths: list[str] = []
            for idx, (clip, source) in enumerate(resolved_sources):
                start = int(clip["start_time"])
                end = int(clip["end_time"])
                duration = max(end - start, 0)
                if duration <= 0:
                    raise ValueError(
                        f"Clip {idx} has non-positive duration: start={start} end={end}"
                    )
                out_path = os.path.join(tmpdir, f"clip-{idx:03d}.mp4")
                ok = await trim_video(
                    source, out_path, f"{start:.2f}", f"{duration:.2f}"
                )
                if not ok:
                    raise RuntimeError(
                        f"trim_video failed for clip {idx} ({source} {start}-{end})"
                    )
                trimmed_paths.append(out_path)

            # Concatenate via the existing HighlightCompilationTask.
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

            final_output_path = task.output_path

            # Upload to YouTube.
            description = (
                f"Highlight reel for {reel['player_name']}"
                if reel.get("player_name")
                else ""
            )
            youtube_video_id = await asyncio.to_thread(
                self.youtube_uploader.upload_video,
                final_output_path,
                task.title,
                description,
                None,  # tags
                "unlisted",  # privacy_status
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
            self._processing.discard(reel_id)
