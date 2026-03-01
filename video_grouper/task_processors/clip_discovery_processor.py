"""
Polling processor that discovers pending moment tags and highlight reels,
computes video offsets, creates clip records, and queues extraction tasks.
"""

import logging
import os
from datetime import datetime


from .base_polling_processor import PollingProcessor
from .services.timestamp_matcher import (
    compute_clip_boundaries,
    compute_combined_offset,
    compute_trimmed_offset,
)
from .tasks.clips.clip_extraction_task import ClipExtractionTask
from .tasks.clips.highlight_compilation_task import HighlightCompilationTask
from video_grouper.api_integrations.moment_api_client import MomentApiClient
from video_grouper.models import DirectoryState, MatchInfo
from video_grouper.utils.config import Config
from video_grouper.utils.paths import get_trimmed_video_path

logger = logging.getLogger(__name__)


class ClipDiscoveryProcessor(PollingProcessor):
    """Discovers pending moment tags and queues clip extraction tasks."""

    def __init__(
        self,
        storage_path: str,
        config: Config,
        api_client: MomentApiClient,
        clip_processor,  # forward ref to ClipProcessor
        poll_interval: int = 60,
    ):
        super().__init__(storage_path, config, poll_interval)
        self.api_client = api_client
        self.clip_processor = clip_processor

    async def discover_work(self) -> None:
        """Scan local group dirs for trimmed videos and discover pending tags."""
        await self._discover_pending_tags()
        await self._discover_pending_highlights()

    async def _discover_pending_tags(self) -> None:
        """Find trimmed videos with pending moment tags and queue clips."""
        if not os.path.isdir(self.storage_path):
            return

        for entry in os.scandir(self.storage_path):
            if not entry.is_dir():
                continue

            group_dir = entry.path
            dir_state = DirectoryState(group_dir)
            state_data = dir_state._load_state()

            # Only process directories that have been trimmed
            if state_data.get("status") not in ("trimmed", "uploaded"):
                continue

            # Look up the game session via API
            group_name = os.path.basename(group_dir)
            game_session = await self.api_client.get_game_session_by_dir(group_name)
            if not game_session:
                continue

            gs_id = game_session["id"]

            # Get pending tags (no video_offset_seconds yet)
            pending_tags = await self.api_client.get_pending_tags(gs_id)
            if not pending_tags:
                continue

            logger.info(
                "CLIP_DISCOVERY: Found %d pending tags for %s",
                len(pending_tags),
                group_name,
            )

            # Load recording files from directory state
            recording_files = self._get_recording_files(state_data)
            if not recording_files:
                logger.warning("CLIP_DISCOVERY: No recording files in %s", group_name)
                continue

            # Load match info for trim offset
            match_info, _ = MatchInfo.get_or_create(group_dir)
            start_time_offset = match_info.get_start_offset() if match_info else ""
            camera_tz = self.config.app.timezone

            # Get trimmed video path
            trimmed_path = get_trimmed_video_path(
                group_dir, match_info, self.storage_path
            )
            if not os.path.isfile(trimmed_path):
                logger.warning(
                    "CLIP_DISCOVERY: Trimmed video missing at %s", trimmed_path
                )
                continue

            for tag in pending_tags:
                await self._process_tag(
                    tag,
                    gs_id,
                    group_dir,
                    recording_files,
                    camera_tz,
                    start_time_offset,
                    trimmed_path,
                )

    async def _process_tag(
        self,
        tag: dict,
        gs_id: str,
        group_dir: str,
        recording_files,
        camera_tz: str,
        start_time_offset: str,
        trimmed_path: str,
    ) -> None:
        """Compute offsets for a single tag, create a clip record, and queue extraction."""
        tag_id = tag["id"]
        tagged_at_str = tag["tagged_at"]

        # Parse the tag's UTC timestamp
        tagged_at = datetime.fromisoformat(tagged_at_str)

        # Compute combined offset
        combined_offset = compute_combined_offset(tagged_at, recording_files, camera_tz)
        if combined_offset is None:
            logger.warning(
                "CLIP_DISCOVERY: Could not compute offset for tag %s", tag_id
            )
            return

        # Compute trimmed offset
        trimmed_offset = compute_trimmed_offset(combined_offset, start_time_offset)
        if trimmed_offset is None:
            logger.warning(
                "CLIP_DISCOVERY: Tag %s is before trim start, skipping", tag_id
            )
            return

        # Update tag with computed offsets
        await self.api_client.update_tag_offset(tag_id, combined_offset, trimmed_offset)

        # Compute clip boundaries
        clip_start, clip_end = compute_clip_boundaries(
            trimmed_offset, buffer_seconds=15.0
        )

        # Create clip record via API
        clip = await self.api_client.create_clip(
            moment_tag_id=tag_id,
            game_session_id=gs_id,
            clip_start=clip_start,
            clip_end=clip_end,
            clip_duration=clip_end - clip_start,
        )
        if not clip:
            return

        # Build output path
        clips_dir = os.path.join(group_dir, "clips")
        clip_filename = f"clip_{tag_id[:8]}_{clip_start:.0f}s.mp4"
        clip_output = os.path.join(clips_dir, clip_filename)

        # Queue extraction task
        task = ClipExtractionTask(
            tag_id=tag_id,
            clip_id=clip["id"],
            game_session_id=gs_id,
            group_dir=group_dir,
            trimmed_video_path=trimmed_path,
            clip_start=clip_start,
            clip_end=clip_end,
            clip_output_path=clip_output,
        )
        await self.clip_processor.add_work(task)
        logger.info("CLIP_DISCOVERY: Queued clip extraction for tag %s", tag_id[:8])

    async def _discover_pending_highlights(self) -> None:
        """Find pending highlight reels whose clips are all ready locally."""
        highlights = await self.api_client.get_pending_highlights()

        for hl in highlights:
            hl_id = hl["id"]
            clips = await self.api_client.get_highlight_clips(hl_id)
            if not clips:
                continue

            # Check all clips have local file_path and exist on disk
            clip_paths = []
            all_ready = True
            for c in clips:
                path = c.get("file_path")
                if not path or not os.path.isfile(path):
                    all_ready = False
                    break
                clip_paths.append(path)

            if not all_ready:
                continue

            # Determine output directory
            if clips and clips[0].get("game_session_id"):
                # Try to find the group dir from the first clip
                first_clip_dir = os.path.dirname(clips[0]["file_path"])
                output_dir = os.path.join(os.path.dirname(first_clip_dir), "highlights")
            else:
                output_dir = os.path.join(self.storage_path, "highlights")

            task = HighlightCompilationTask(
                highlight_id=hl_id,
                title=hl.get("title", "Highlight"),
                player_name=hl.get("player_name", ""),
                clip_local_paths=tuple(clip_paths),
                output_dir=output_dir,
            )
            await self.clip_processor.add_work(task)
            await self.api_client.update_highlight(hl_id, status="generating")
            logger.info(
                "CLIP_DISCOVERY: Queued highlight compilation %s (%d clips)",
                hl_id[:8],
                len(clip_paths),
            )

    def _get_recording_files(self, state_data: dict):
        """Extract RecordingFile objects from directory state data."""
        from video_grouper.models import RecordingFile

        files_data = state_data.get("files", {})
        result = []
        for _path, file_data in files_data.items():
            try:
                rec = RecordingFile.from_dict(file_data)
                result.append(rec)
            except Exception as e:
                logger.debug("Could not parse recording file: %s", e)
        return result
