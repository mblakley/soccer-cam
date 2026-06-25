"""
Combine task for combining multiple DAV files into a single MP4 video.
"""

import logging
import os
from dataclasses import dataclass
from typing import Any

from video_grouper.models import DirectoryState
from video_grouper.utils.ffmpeg_utils import (
    combine_videos,
    detect_audio_video_gaps,
    detect_video_decode_corruption,
)
from video_grouper.utils.paths import (
    get_combined_video_path,
    resolve_path,
)

from .base_ffmpeg_task import BaseFfmpegTask

logger = logging.getLogger(__name__)


@dataclass(unsafe_hash=True)
class CombineTask(BaseFfmpegTask):
    """
    Task for combining multiple DAV files in a directory into a single combined MP4 video.

    Uses PyAV to concatenate files and convert to MP4 in one step.
    """

    group_dir: str

    @property
    def task_type(self) -> str:
        """Return the specific task type identifier."""
        return "combine"

    def get_item_path(self) -> str:
        """Return the group directory path."""
        return self.group_dir

    def serialize(self) -> dict[str, Any]:
        """
        Serialize the task for state persistence.

        Returns:
            Dictionary containing task data
        """
        return {"task_type": self.task_type, "group_dir": self.group_dir}

    def get_output_path(self) -> str:
        """
        Get the expected output path for the combined file.

        Returns:
            Path where the combined.mp4 file will be created
        """
        return get_combined_video_path(self.group_dir, self.storage_path)

    # File extensions that contain raw camera recordings to combine
    VIDEO_EXTENSIONS = (".dav", ".mp4")
    # Files produced by the pipeline itself that should not be combined
    EXCLUDE_PREFIXES = ("combined",)

    def get_dav_files(self) -> list[str]:
        """
        Get the list of video files to combine from the group directory.

        Supports .dav (Dahua) and .mp4 (Reolink) input files.
        Excludes pipeline-produced files like combined.mp4.

        Returns:
            Sorted list of video file paths
        """
        video_files = []
        group_dir_abs = resolve_path(self.group_dir, self.storage_path)
        try:
            for filename in sorted(os.listdir(group_dir_abs)):
                if not filename.lower().endswith(self.VIDEO_EXTENSIONS):
                    continue
                if filename.lower().startswith(self.EXCLUDE_PREFIXES):
                    continue
                video_files.append(os.path.join(group_dir_abs, filename))
        except FileNotFoundError:
            pass
        return video_files

    async def execute(self) -> bool:
        """
        Execute the combine task with proper file list creation and handle post-actions.

        Returns:
            True if command succeeded, False otherwise
        """
        # Segments whose audio is materially shorter than their video, detected
        # before combining. The combine itself pads each gap with silence (see
        # _combine_copy); VideoProcessor reads this list to warn the user.
        self.audio_gaps: list[dict] = []
        # Segments with an undecodable video region (byte-complete but corrupt
        # on the camera's SD card — unrecoverable, see detect_video_decode_
        # corruption). The combine cuts the dead span; VideoProcessor reads this
        # list to warn the user and flag the game as shipped-with-loss.
        self.video_corruption: list[dict] = []

        dav_files = self.get_dav_files()
        if not dav_files:
            await self._handle_task_failure()
            return False

        self.audio_gaps = detect_audio_video_gaps(dav_files)
        if self.audio_gaps:
            logger.warning(
                "COMBINE: %d segment(s) in %s have audio/video length mismatch: %s",
                len(self.audio_gaps),
                os.path.basename(self.group_dir),
                ", ".join(
                    f"{os.path.basename(g['path'])} "
                    f"({g['gap_seconds']:.0f}s {g.get('kind', 'short')})"
                    for g in self.audio_gaps
                ),
            )

        # Decode-probe each segment for in-file corruption a size check can't
        # see. PR #88 already guarantees byte-complete downloads, so anything
        # this finds is camera-side and unrecoverable — degrade (cut the dead
        # region in combine), don't re-download.
        self.video_corruption = detect_video_decode_corruption(dav_files)
        corrupt_starts = {
            c["path"]: c["corrupt_start_seconds"] for c in self.video_corruption
        }
        if self.video_corruption:
            logger.error(
                "COMBINE: %d segment(s) in %s have an undecodable video region; "
                "cutting the dead span (camera recording error, unrecoverable): %s",
                len(self.video_corruption),
                os.path.basename(self.group_dir),
                ", ".join(
                    f"{os.path.basename(c['path'])} "
                    f"(~{c['lost_seconds']:.0f}s lost from {c['corrupt_start_seconds']:.0f}s)"
                    for c in self.video_corruption
                ),
            )

        output_path = self.get_output_path()

        # Extract camera metadata from the group's state.json
        camera_name = None
        camera_type = None
        try:
            dir_state = DirectoryState(self.group_dir)
            first_file = dir_state.get_first_file()
            if first_file and first_file.metadata:
                camera_name = first_file.metadata.get("camera_name")
                camera_type = first_file.metadata.get("camera_type")
        except Exception:
            pass  # Non-critical: metadata is optional

        try:
            # Pass file paths directly to combine_videos (PyAV-based).
            # corrupt_starts tells combine where to cut each segment's
            # undecodable video region so the dead span is dropped, not muxed.
            success = await combine_videos(
                dav_files,
                output_path,
                camera_name=camera_name,
                camera_type=camera_type,
                corrupt_starts=corrupt_starts,
            )

            if success:
                await self._handle_post_combine_actions()
            else:
                await self._handle_task_failure()

            return success

        except Exception as e:
            logger.error(f"COMBINE: Error during combine task execution: {e}")
            await self._handle_task_failure()
            return False

    async def _handle_post_combine_actions(self) -> None:
        """Handle post-combine actions like updating status and checking for trim readiness."""
        try:
            dir_state = DirectoryState(self.group_dir)

            logger.info(f"COMBINE: Successfully combined videos in {self.group_dir}")
            await dir_state.update_group_status("combined")

            # If combine had to cut a corrupt (unrecoverable) video region,
            # durably flag the game so it isn't shipped as if perfect. This is a
            # marker, not a status change — the trim/upload flow continues.
            if self.video_corruption:
                total_lost = sum(c["lost_seconds"] for c in self.video_corruption)
                detail = "; ".join(
                    f"{os.path.basename(c['path'])} "
                    f"~{c['lost_seconds']:.0f}s@{c['corrupt_start_seconds']:.0f}s"
                    for c in self.video_corruption
                )
                dir_state.set_video_loss(total_lost, detail)

            # Match info gathering is triggered by VideoProcessor._on_combine_complete()
            # which fires async API lookups and NTFY questions after this task completes.

        except Exception as e:
            logger.error(f"COMBINE: Error in post-combine actions for {self}: {e}")

    async def _handle_task_failure(self) -> None:
        """Handle task failure by updating directory state."""
        try:
            dir_state = DirectoryState(self.group_dir)
            await dir_state.update_group_status(
                "combine_failed", error_message="Task execution failed"
            )
        except Exception as e:
            logger.error(f"COMBINE: Error handling task failure for {self}: {e}")

    def __str__(self) -> str:
        """String representation of the task."""
        return f"CombineTask({os.path.basename(self.group_dir)})"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CombineTask":
        """
        Create a CombineTask from serialized data.

        Args:
            data: Dictionary containing task data

        Returns:
            CombineTask instance
        """
        return cls(group_dir=data["group_dir"])

    @classmethod
    def deserialize(cls, data: dict[str, object]) -> "CombineTask":
        """
        Deserialize a CombineTask from its serialized data.

        Args:
            data: Dictionary containing serialized task data

        Returns:
            Deserialized CombineTask instance
        """
        return cls.from_dict(data)
