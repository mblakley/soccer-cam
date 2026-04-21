"""
Combine task for combining multiple DAV files into a single MP4 video.
"""

import os
import logging
from typing import List, Dict, Any
from dataclasses import dataclass

from .base_ffmpeg_task import BaseFfmpegTask
from video_grouper.models import DirectoryState
from video_grouper.utils.ffmpeg_utils import combine_videos
from video_grouper.utils.paths import (
    get_combined_video_path,
    resolve_path,
)
from video_grouper.utils.stitch_remap import (
    load_profile,
    sidecar_path_for,
    write_profile,
)

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

    def serialize(self) -> Dict[str, Any]:
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

    def get_dav_files(self) -> List[str]:
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
        dav_files = self.get_dav_files()
        if not dav_files:
            await self._handle_task_failure()
            return False

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
            # Pass file paths directly to combine_videos (PyAV-based)
            success = await combine_videos(
                dav_files, output_path, camera_name=camera_name, camera_type=camera_type
            )

            if success:
                self._write_stitch_sidecar(output_path)
                await self._handle_post_combine_actions()
            else:
                await self._handle_task_failure()

            return success

        except Exception as e:
            logger.error(f"COMBINE: Error during combine task execution: {e}")
            await self._handle_task_failure()
            return False

    def _write_stitch_sidecar(self, output_path: str) -> None:
        """Copy the configured stitch profile to `<combined.mp4>.stitch.json`.

        Downstream readers (ball detection, tracking, broadcast-perspective render)
        look for the sidecar next to the MP4 to apply the per-row dx shift on read.
        No-op when no profile is configured or the source file is missing — the
        combined MP4 itself is not modified.
        """
        try:
            cfg = getattr(self, "config", None)
            processing = getattr(cfg, "processing", None) if cfg else None
            if not processing or not getattr(processing, "seam_realign_enabled", False):
                return
            profile_path = getattr(processing, "seam_realign_profile_path", None)
            if not profile_path:
                return
            profile = load_profile(profile_path)
            if profile is None:
                logger.warning(
                    f"COMBINE: seam_realign_enabled but no readable profile at "
                    f"{profile_path}; skipping sidecar"
                )
                return
            sidecar = sidecar_path_for(output_path)
            write_profile(profile, sidecar)
            logger.info(f"COMBINE: wrote stitch profile sidecar to {sidecar}")
        except Exception as e:
            # Sidecar write is non-critical: downstream will just skip the shift.
            logger.warning(f"COMBINE: failed to write stitch sidecar: {e}")

    async def _handle_post_combine_actions(self) -> None:
        """Handle post-combine actions like updating status and checking for trim readiness."""
        try:
            dir_state = DirectoryState(self.group_dir)

            logger.info(f"COMBINE: Successfully combined videos in {self.group_dir}")
            await dir_state.update_group_status("combined")

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
    def from_dict(cls, data: Dict[str, Any]) -> "CombineTask":
        """
        Create a CombineTask from serialized data.

        Args:
            data: Dictionary containing task data

        Returns:
            CombineTask instance
        """
        return cls(group_dir=data["group_dir"])

    @classmethod
    def deserialize(cls, data: Dict[str, object]) -> "CombineTask":
        """
        Deserialize a CombineTask from its serialized data.

        Args:
            data: Dictionary containing serialized task data

        Returns:
            Deserialized CombineTask instance
        """
        return cls.from_dict(data)
