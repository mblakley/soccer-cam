"""Clip request processor — polls TTT for pending requests, extracts clips, uploads to Drive."""

import asyncio
import logging
import os
import tempfile
from typing import Optional

from .base_polling_processor import PollingProcessor
from ..utils.config import Config

logger = logging.getLogger(__name__)


class ClipRequestProcessor(PollingProcessor):
    """Polls TTT API for pending clip requests and processes them.

    Flow per request:
    1. Mark as in_progress via TTT API
    2. Locate combined.mp4 in recording_group_dir
    3. Extract clips via FFmpeg
    4. If compilation, merge clips
    5. Upload to Google Drive
    6. Report fulfilled URL back to TTT
    """

    def __init__(
        self,
        storage_path: str,
        config: Config,
        ttt_client,
        drive_uploader,
        ntfy_service=None,
        poll_interval: int = 60,
    ):
        super().__init__(storage_path, config, poll_interval)
        self.ttt_client = ttt_client
        self.drive_uploader = drive_uploader
        self.ntfy_service = ntfy_service
        self._processing = set()  # Track in-flight request IDs

    async def discover_work(self) -> None:
        """Poll TTT for pending clip requests and process them."""
        if not self.ttt_client.is_authenticated():
            logger.debug("TTT client not authenticated, skipping poll")
            return

        try:
            requests = await asyncio.to_thread(
                self.ttt_client.get_pending_clip_requests
            )
        except Exception as e:
            logger.error(f"Failed to poll TTT for clip requests: {e}")
            return

        for req in requests:
            req_id = req.get("id")
            if not req_id or req_id in self._processing:
                continue

            self._processing.add(req_id)
            asyncio.create_task(self._process_request(req))

    async def _process_request(self, req: dict) -> None:
        """Process a single clip request end-to-end."""
        req_id = req["id"]
        try:
            recording_dir = self._resolve_recording_dir(req)
            if not recording_dir:
                return

            # Find the source video
            combined_path = self._find_source_video(recording_dir)
            if not combined_path:
                self._notify_missing_footage(req, recording_dir)
                return

            # Mark as in_progress
            try:
                await asyncio.to_thread(self.ttt_client.start_clip_request, req_id)
            except Exception as e:
                if "Cannot start" not in str(e):
                    logger.error(f"Failed to start clip request {req_id}: {e}")
                    return
                # Already in_progress, continue processing

            # Extract clips
            segments = req.get("segments", [])
            if not segments:
                logger.warning(f"Clip request {req_id} has no segments, skipping")
                return

            clip_paths = await self._extract_segments(combined_path, segments, req_id)
            if not clip_paths:
                logger.error(f"No clips extracted for request {req_id}")
                return

            # Compile if needed
            is_compilation = req.get("is_compilation", False)
            if is_compilation and len(clip_paths) > 1:
                output_path = os.path.join(
                    tempfile.gettempdir(), f"ttt_compilation_{req_id}.mp4"
                )
                from ..utils.ffmpeg_utils import compile_clips

                await compile_clips(clip_paths, output_path)
                upload_paths = [output_path]
            else:
                upload_paths = clip_paths

            # Upload to Google Drive
            folder_id = self.config.ttt.google_drive_folder_id
            if not folder_id:
                logger.error("Google Drive folder ID not configured for TTT")
                return

            share_urls = []
            for path in upload_paths:
                filename = os.path.basename(path)
                try:
                    url = await asyncio.to_thread(
                        self.drive_uploader.upload_and_share, path, folder_id, filename
                    )
                    share_urls.append(url)
                except Exception as e:
                    logger.error(f"Failed to upload {filename} to Drive: {e}")
                    return

            # Fulfill the request
            fulfilled_url = (
                share_urls[0] if len(share_urls) == 1 else ", ".join(share_urls)
            )
            notes = f"{len(segments)} clip(s) extracted and uploaded"
            try:
                await asyncio.to_thread(
                    self.ttt_client.fulfill_clip_request, req_id, fulfilled_url, notes
                )
                logger.info(f"Fulfilled clip request {req_id}: {fulfilled_url}")
            except Exception as e:
                logger.error(f"Failed to fulfill clip request {req_id}: {e}")
                return

            # Notify success
            if self.ntfy_service:
                try:
                    await self.ntfy_service.send_notification(
                        "Clip request fulfilled",
                        f"Request {req_id[:8]}... completed with {len(segments)} clip(s)",
                    )
                except Exception:
                    pass  # Non-critical

            # Clean up temp files
            for path in clip_paths:
                if path.startswith(tempfile.gettempdir()):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            for path in upload_paths:
                if path.startswith(tempfile.gettempdir()) and path not in clip_paths:
                    try:
                        os.remove(path)
                    except OSError:
                        pass

        except Exception as e:
            logger.error(f"Error processing clip request {req_id}: {e}", exc_info=True)
        finally:
            self._processing.discard(req_id)

    def _resolve_recording_dir(self, req: dict) -> Optional[str]:
        """Resolve the recording group directory to an absolute path."""
        game_session = req.get("game_session") or {}
        recording_dir = game_session.get("recording_group_dir")
        if not recording_dir:
            logger.warning(f"Clip request {req['id']} has no recording_group_dir")
            return None

        # Try as absolute path first, then relative to storage
        if os.path.isabs(recording_dir) and os.path.isdir(recording_dir):
            return recording_dir

        abs_path = os.path.join(self.storage_path, recording_dir)
        if os.path.isdir(abs_path):
            return abs_path

        logger.warning(f"Recording dir not found: {recording_dir}")
        return None

    def _find_source_video(self, recording_dir: str) -> Optional[str]:
        """Find combined.mp4 in the recording directory tree."""
        # Direct check
        combined = os.path.join(recording_dir, "combined.mp4")
        if os.path.isfile(combined):
            return combined

        # Check subdirectories
        for entry in os.listdir(recording_dir):
            subdir = os.path.join(recording_dir, entry)
            if os.path.isdir(subdir):
                combined = os.path.join(subdir, "combined.mp4")
                if os.path.isfile(combined):
                    return combined

        return None

    def _notify_missing_footage(self, req: dict, recording_dir: str) -> None:
        """Send notification about missing footage."""
        req_id = req["id"]
        logger.warning(
            f"Source video not found for clip request {req_id} at {recording_dir}"
        )
        if self.ntfy_service:
            try:
                game = req.get("game_session", {})
                opponent = game.get("opponent_name", "unknown")
                asyncio.create_task(
                    self.ntfy_service.send_notification(
                        "Clip request needs footage",
                        f"Request for game vs {opponent} — footage not found at {recording_dir}. Drive may be disconnected.",
                    )
                )
            except Exception:
                pass

    async def _extract_segments(
        self, source_path: str, segments: list, req_id: str
    ) -> list[str]:
        """Extract clips for each segment."""
        from ..utils.ffmpeg_utils import extract_clip

        clip_paths = []
        sorted_segments = sorted(segments, key=lambda s: s.get("sort_order", 0))

        for i, seg in enumerate(sorted_segments):
            start = seg.get("start_time", 0)
            end = seg.get("end_time", 0)
            if end <= start:
                logger.warning(
                    f"Invalid segment times for request {req_id}: {start}-{end}"
                )
                continue

            label = seg.get("label", f"clip_{i + 1}")
            safe_label = "".join(
                c if c.isalnum() or c in "-_" else "_" for c in str(label)
            )
            output_path = os.path.join(
                tempfile.gettempdir(),
                f"ttt_{req_id[:8]}_{safe_label}.mp4",
            )

            try:
                await extract_clip(source_path, start, end, output_path)
                clip_paths.append(output_path)
            except Exception as e:
                logger.error(f"Failed to extract segment {i} for request {req_id}: {e}")

        return clip_paths
