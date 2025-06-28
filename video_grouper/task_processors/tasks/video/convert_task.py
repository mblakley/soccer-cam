"""
Convert task for converting video files from DAV to MP4 format.
"""

import os
import logging
from typing import List, Dict, Any, Optional, Callable, Awaitable
from dataclasses import dataclass

from .base_ffmpeg_task import BaseFfmpegTask
from video_grouper.models import MatchInfo
from video_grouper.directory_state import DirectoryState
from video_grouper.ffmpeg_utils import create_screenshot, get_video_duration

logger = logging.getLogger(__name__)


@dataclass(unsafe_hash=True)
class ConvertTask(BaseFfmpegTask):
    """
    Task for converting a video file from DAV to MP4 format.
    
    Uses FFmpeg to convert with:
    - Video: copy stream without re-encoding
    - Audio: re-encode to AAC with 192k bitrate
    """
    
    file_path: str
    
    @property
    def task_type(self) -> str:
        """Return the specific task type identifier."""
        return "convert"
    
    def get_command(self) -> List[str]:
        """
        Return the FFmpeg command to convert the video file.
        
        Returns:
            FFmpeg command as list of strings
        """
        output_path = self.file_path.replace('.dav', '.mp4')
        
        return [
            'ffmpeg',
            '-y',  # Overwrite output file
            '-i', self.file_path,  # Input file
            '-c:v', 'copy',  # Copy video stream
            '-c:a', 'aac',  # Re-encode audio to AAC
            '-b:a', '192k',  # Audio bitrate
            output_path
        ]
    
    async def execute(self, queue_task: Optional[Callable[[Any], Awaitable[None]]] = None) -> bool:
        """
        Execute the convert task and handle post-actions.
        
        Args:
            queue_task: Function to queue additional tasks
            
        Returns:
            True if command succeeded, False otherwise
        """
        # Execute the FFmpeg command
        success = await super().execute(queue_task)
        
        if success:
            await self._handle_post_conversion_actions(queue_task)
        else:
            await self._handle_task_failure()
        
        return success
    
    async def _handle_post_conversion_actions(self, queue_task: Optional[Callable[[Any], Awaitable[None]]] = None) -> None:
        """Handle post-conversion actions like creating screenshots and checking for combine readiness."""
        try:
            filename = os.path.basename(self.file_path)
            group_dir = os.path.dirname(self.file_path)
            dir_state = DirectoryState(group_dir)
            
            # Create screenshot from the converted MP4
            mp4_path = self.get_output_path()
            screenshot_path = mp4_path.replace('.mp4', '_screenshot.jpg')
            screenshot_success = await create_screenshot(mp4_path, screenshot_path)
            final_screenshot_path = screenshot_path if screenshot_success else None

            await dir_state.update_file_state(self.file_path, status="converted", screenshot_path=final_screenshot_path)

            # Create match_info.ini if it doesn't exist
            await self._ensure_match_info_exists(group_dir)

            await self._cleanup_dav_files(group_dir)

            if dir_state.is_ready_for_combining():
                logger.info(f"CONVERT: Group {os.path.basename(group_dir)} is ready for combining.")
                
                if queue_task:
                    from .combine_task import CombineTask
                    combine_task = CombineTask(group_dir=group_dir)
                    await queue_task(combine_task)
                    logger.info(f"CONVERT: Queued combine task: {combine_task}")
                else:
                    logger.warning(f"CONVERT: No task queue function available to queue combine task for {group_dir}")
                
        except Exception as e:
            logger.error(f"CONVERT: Error in post-conversion actions for {self}: {e}")
    
    async def _handle_task_failure(self) -> None:
        """Handle task failure by updating directory state."""
        try:
            dir_state = DirectoryState(os.path.dirname(self.file_path))
            await dir_state.update_file_state(self.file_path, status="conversion_failed")
        except Exception as e:
            logger.error(f"CONVERT: Error handling task failure for {self}: {e}")
    
    async def _ensure_match_info_exists(self, group_dir: str):
        """Creates match_info.ini in the group directory if it doesn't exist."""
        match_info, config = MatchInfo.get_or_create(group_dir)
    
    async def _cleanup_dav_files(self, directory: str):
        """
        Removes .dav files if a corresponding valid .mp4 file exists in the same directory.
        """
        deleted_count = 0
        
        try:
            dav_files = {f: f.replace('.dav', '.mp4') for f in os.listdir(directory) if f.endswith('.dav')}
            
            for dav_file, mp4_file in dav_files.items():
                dav_path = os.path.join(directory, dav_file)
                mp4_path = os.path.join(directory, mp4_file)
                
                if os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0:
                    try:
                        # Verify MP4 duration is reasonable (e.g., > 0)
                        mp4_duration = await get_video_duration(mp4_path)
                        if mp4_duration and mp4_duration > 0:
                            os.remove(dav_path)
                            deleted_count += 1
                            logger.info(f"CONVERT: Removed orphaned DAV file: {dav_path}")
                    except Exception as e:
                        logger.error(f"CONVERT: Error processing file {dav_path} for cleanup: {e}")
        except Exception as e:
            logger.error(f"CONVERT: Error processing directory {directory} for cleanup: {e}")
        
        return deleted_count
    
    def get_item_path(self) -> str:
        """Return the file path being converted."""
        return self.file_path
    
    def serialize(self) -> Dict[str, Any]:
        """
        Serialize the task for state persistence.
        
        Returns:
            Dictionary containing task data
        """
        return {
            "task_type": self.task_type,
            "file_path": self.file_path
        }
    
    def get_output_path(self) -> str:
        """
        Get the expected output path for the converted file.
        
        Returns:
            Path where the MP4 file will be created
        """
        return self.file_path.replace('.dav', '.mp4')
    
    def __str__(self) -> str:
        """String representation of the task."""
        return f"ConvertTask({os.path.basename(self.file_path)})"
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ConvertTask':
        """
        Create a ConvertTask from serialized data.
        
        Args:
            data: Dictionary containing task data
            
        Returns:
            ConvertTask instance
        """
        return cls(file_path=data['file_path']) 