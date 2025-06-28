import os
import logging
import asyncio
from typing import Any, Dict, Optional
import aiofiles
from .queue_processor_base import QueueProcessor
from video_grouper.directory_state import DirectoryState
from video_grouper.models import FFmpegTask, ConvertTask, CombineTask, TrimTask, MatchInfo, task_from_dict
from video_grouper.ffmpeg_utils import async_convert_file, create_screenshot, trim_video

logger = logging.getLogger(__name__)

class VideoProcessor(QueueProcessor):
    """
    Task processor for video operations (convert, combine, trim).
    Processes FFmpeg tasks sequentially.
    """
    
    def __init__(self, storage_path: str, config: Any):
        super().__init__(storage_path, config)
        self.upload_processor = None
        
    def set_upload_processor(self, upload_processor):
        """Set reference to upload processor to queue work."""
        self.upload_processor = upload_processor
    
    def get_state_file_name(self) -> str:
        return "ffmpeg_queue_state.json"
    
    async def process_item(self, item: FFmpegTask) -> None:
        """
        Process a video task (convert, combine, or trim).
        
        Args:
            item: FFmpegTask to process
        """
        try:
            logger.info(f"VIDEO: Processing task: {item}")
            
            if item.task_type == "convert":
                await self._handle_conversion_task(item.item_path)
            elif item.task_type == "combine":
                await self._handle_combine_task(item.item_path)
            elif item.task_type == "trim":
                if isinstance(item, TrimTask):
                    await self._handle_trim_task(item.item_path, item.match_info)
                else:
                    await self._handle_trim_task(item.item_path)
            else:
                logger.warning(f"VIDEO: Unknown task type: {item.task_type}")
                
        except Exception as e:
            logger.error(f"VIDEO: Error processing task {item}: {e}")
    
    def serialize_item(self, item: FFmpegTask) -> Dict[str, Any]:
        """Serialize an FFmpegTask for state persistence."""
        return item.to_dict()
    
    def deserialize_item(self, item_data: Dict[str, Any]) -> Optional[FFmpegTask]:
        """Deserialize an FFmpegTask from state data."""
        try:
            return task_from_dict(item_data)
        except Exception as e:
            logger.error(f"VIDEO: Failed to deserialize FFmpegTask: {e}")
            return None
    
    def get_item_key(self, item: FFmpegTask) -> str:
        """Get unique key for an FFmpegTask."""
        if hasattr(item, 'match_info'):
            return f"{item.task_type}:{item.item_path}:{hash(item.match_info)}"
        return f"{item.task_type}:{item.item_path}"
    
    async def _handle_conversion_task(self, file_path: str) -> None:
        """Handle a video conversion task."""
        filename = os.path.basename(file_path)
        group_dir = os.path.dirname(file_path)
        dir_state = DirectoryState(group_dir)
        file_obj = dir_state.files.get(file_path)
                        
        if not file_obj:
            logger.error(f"VIDEO: File {filename} not found in state for conversion. Skipping.")
            return False

        # Re-check skip status right before processing
        if file_obj.skip:
            logger.info(f"VIDEO: Skipping conversion for {filename} because 'skip' is true.")
            return
                        
        logger.info(f"VIDEO: Converting {filename} to MP4...")
        try:
            mp4_path = await async_convert_file(file_path)
            if mp4_path and os.path.exists(mp4_path):
                logger.info(f"VIDEO: Successfully converted {filename}.")
                
                # Create screenshot from the converted MP4
                screenshot_path = mp4_path.replace('.mp4', '_screenshot.jpg')
                screenshot_success = await create_screenshot(mp4_path, screenshot_path)
                
                final_screenshot_path = screenshot_path if screenshot_success else None

                await dir_state.update_file_state(file_path, status="converted", screenshot_path=final_screenshot_path)

                # Create match_info.ini if it doesn't exist
                await self._ensure_match_info_exists(group_dir)

                await self._cleanup_dav_files(group_dir)

                if dir_state.is_ready_for_combining():
                    logger.info(f"VIDEO: Group {os.path.basename(group_dir)} is ready for combining.")
                    await self.add_work(CombineTask(group_dir))
            else:
                await dir_state.update_file_state(file_path, status="conversion_failed")
                logger.error(f"VIDEO: Conversion failed for {file_path}")
        except Exception as e:
            await dir_state.update_file_state(file_path, status="conversion_failed")
            logger.error(f"VIDEO: An unexpected error occurred during conversion of {file_path}: {e}", exc_info=True)

    async def _handle_combine_task(self, group_dir: str) -> None:
        """Combines all converted MP4 files in a group directory."""
        logger.info(f"VIDEO: Starting combine task for {group_dir}")
        dir_state = DirectoryState(group_dir)
        
        mp4_files = sorted([f.file_path.replace('.dav', '.mp4') for f in dir_state.get_files_by_status("converted")])
        
        if not mp4_files:
            logger.warning(f"VIDEO: No MP4 files to combine in {group_dir}")
            return

        combined_path = os.path.join(group_dir, "combined.mp4")
        file_list_path = os.path.join(group_dir, "filelist.txt")

        try:
            async with aiofiles.open(file_list_path, 'w') as f:
                for mp4_file in mp4_files:
                    # Format for ffmpeg concat demuxer
                    await f.write(f"file '{os.path.basename(mp4_file)}'\n")
            
            cmd = [
                'ffmpeg',
                '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', file_list_path,
                '-c', 'copy',
                combined_path
            ]
            
            logger.info(f"VIDEO: Running ffmpeg combine command: {' '.join(cmd)}")
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            await process.wait()

            if process.returncode == 0:
                logger.info(f"VIDEO: Successfully combined videos in {group_dir}")
                await dir_state.update_group_status("combined")
                
                # Check for match_info.ini and queue trim task if it's populated
                match_info_path = os.path.join(group_dir, "match_info.ini")
                if os.path.exists(match_info_path):
                    match_info = MatchInfo.from_file(match_info_path)
                    if match_info and match_info.is_populated():
                        await self.add_work(TrimTask(group_dir, match_info))
            else:
                logger.error(f"VIDEO: Failed to combine videos in {group_dir}")
                await dir_state.update_group_status("combine_failed", error_message="ffmpeg combine command failed.")
        except Exception as e:
            logger.error(f"VIDEO: Error during combine for {group_dir}: {e}")
            await dir_state.update_group_status("combine_failed", error_message=str(e))
        finally:
            if os.path.exists(file_list_path):
                os.remove(file_list_path)

    async def _handle_trim_task(self, group_dir: str, match_info: Optional[MatchInfo] = None) -> None:
        """
        Handles trimming of a combined video file based on match_info.ini.
        """
        logger.info(f"VIDEO: Handling trim task for {group_dir}")

        dir_state = DirectoryState(group_dir)
        
        # Load match info if not provided
        if match_info is None:
            match_info_path = os.path.join(group_dir, "match_info.ini")
            match_info = MatchInfo.from_file(match_info_path)
            if match_info is None:
                logger.error(f"VIDEO: Failed to read match_info.ini at {match_info_path}")
                await dir_state.update_group_status("trim_failed", error_message="Failed to read match_info.ini")
                return

        combined_path = os.path.join(group_dir, "combined.mp4")
        if not os.path.exists(combined_path):
            logger.error(f"VIDEO: Combined video not found at {combined_path}. Cannot trim.")
            await dir_state.update_group_status("trim_failed", error_message="Combined video not found for trimming.")
            return

        try:
            my_team_name = match_info.my_team_name
            opponent_team_name = match_info.opponent_team_name
            location = match_info.location
            start_offset = match_info.start_time_offset
            total_duration_seconds = match_info.get_total_duration_seconds()
            
            # Get sanitized names for filename
            my_team_sanitized, opponent_sanitized, location_sanitized = match_info.get_sanitized_names()

            # Get date from group directory name to add to filename
            group_name = os.path.basename(group_dir)
            date_str_ymd = "nodate"
            date_str_mdy = "nodate"
            try:
                from datetime import datetime
                date_part = group_name.split('-')[0]
                dt_obj = datetime.strptime(date_part, '%Y.%m.%d')
                date_str_ymd = dt_obj.strftime('%Y.%m.%d')
                date_str_mdy = dt_obj.strftime('%m-%d-%Y')
            except (ValueError, IndexError) as e:
                logger.warning(f"VIDEO: Could not parse date from group name '{group_name}'. Using generic date string. Error: {e}")

            # Create the subdirectory for the trimmed file
            sub_dir_name = f"{date_str_ymd} - {my_team_name} vs {opponent_team_name} ({location})"
            sub_dir_path = os.path.join(group_dir, sub_dir_name)
            os.makedirs(sub_dir_path, exist_ok=True)

            # Define the output filename and path
            output_filename = f"{my_team_sanitized}-{opponent_sanitized}-{location_sanitized}-{date_str_mdy}-raw.mp4"
            output_path = os.path.join(sub_dir_path, output_filename)

            logger.info(f"VIDEO: Preparing to trim {combined_path} to {output_path} with offset {start_offset} and duration {total_duration_seconds}s")

            trim_successful = await trim_video(
                input_path=combined_path,
                output_path=output_path,
                start_offset=start_offset,
                duration=str(int(total_duration_seconds))
            )

            if trim_successful:
                logger.info(f"VIDEO: Successfully trimmed video to {output_path}")
                await dir_state.update_group_status("trimmed")
            else:
                logger.error(f"VIDEO: Failed to trim video for {group_dir}")
                await dir_state.update_group_status("trim_failed", error_message="FFmpeg trim command failed")

        except Exception as e:
            logger.error(f"VIDEO: Error during trim task for {group_dir}: {e}")
            await dir_state.update_group_status("trim_failed", error_message=str(e))

    async def _ensure_match_info_exists(self, group_dir: str):
        """Creates match_info.ini in the group directory if it doesn't exist."""
        # Use the new MatchInfo.get_or_create method
        match_info, config = MatchInfo.get_or_create(group_dir)

    async def _cleanup_dav_files(self, directory: str):
        """
        Removes .dav files if a corresponding valid .mp4 file exists in the same directory.
        """
        from video_grouper.ffmpeg_utils import get_video_duration
        
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
                            logger.info(f"VIDEO: Removed orphaned DAV file: {dav_path}")
                    except Exception as e:
                        logger.error(f"VIDEO: Error processing file {dav_path} for cleanup: {e}")
        except Exception as e:
            logger.error(f"VIDEO: Error processing directory {directory} for cleanup: {e}")
        
        return deleted_count 