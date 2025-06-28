import os
import logging
from typing import Any, Dict, Optional
from .polling_processor_base import PollingProcessor
from video_grouper.directory_state import DirectoryState
from video_grouper.models import VideoUploadTask, RecordingFile, MatchInfo
from .tasks.video import ConvertTask, CombineTask, TrimTask

logger = logging.getLogger(__name__)

class StateAuditor(PollingProcessor):
    """
    Task processor for auditing external state changes.
    Scans the shared_data directory for state.json files and queues appropriate tasks.
    """
    
    def __init__(self, storage_path: str, config: Any, poll_interval: int = 60):
        super().__init__(storage_path, config, poll_interval)
        # References to other processors to queue work
        self.download_processor = None
        self.video_processor = None
        self.upload_processor = None
    
    def set_processors(self, download_processor, video_processor, upload_processor):
        """Set references to other processors to queue work."""
        self.download_processor = download_processor
        self.video_processor = video_processor
        self.upload_processor = upload_processor
    
    async def discover_work(self) -> None:
        """
        Audit all directories in storage_path and queue appropriate tasks.
        This is the main work of the state auditor.
        """
        logger.info("STATE_AUDITOR: Starting audit of storage directory")
        
        try:
            # Get all directories in storage path
            items = os.listdir(self.storage_path)
            for item in items:
                group_dir = os.path.join(self.storage_path, item)
                if os.path.isdir(group_dir) and not item.startswith('.'):
                    await self._audit_directory(group_dir)
                    
        except Exception as e:
            logger.error(f"STATE_AUDITOR: Error during directory audit: {e}")
    
    async def _audit_directory(self, group_dir: str) -> None:
        """Audit a single directory and queue appropriate tasks."""
        state_file_path = os.path.join(group_dir, "state.json")
        if not os.path.exists(state_file_path):
            return
            
        try:
            dir_state = DirectoryState(group_dir)
            
            # Audit individual files
            for file_obj in dir_state.files.values():
                if file_obj.skip:
                    continue
                    
                # Queue download tasks for pending/failed downloads
                if file_obj.status in ["pending", "download_failed"]:
                    if self.download_processor:
                        recording_file = RecordingFile(
                            start_time=file_obj.start_time,
                            end_time=file_obj.end_time,
                            file_path=file_obj.file_path,
                            metadata=file_obj.metadata,
                            status=file_obj.status,
                            skip=file_obj.skip
                        )
                        await self.download_processor.add_work(recording_file)
                
                # Queue convert tasks for downloaded files
                elif file_obj.status == "downloaded":
                    if self.video_processor:
                        await self.video_processor.add_work(ConvertTask(file_obj.file_path))
                
                # Queue convert tasks for failed conversions
                elif file_obj.status == "conversion_failed":
                    if self.video_processor:
                        await self.video_processor.add_work(ConvertTask(file_obj.file_path))
            
            # Check if ready for combining
            if dir_state.is_ready_for_combining():
                combined_path = os.path.join(group_dir, "combined.mp4")
                if not os.path.exists(combined_path):
                    if self.video_processor:
                        await self.video_processor.add_work(CombineTask(group_dir))
            
            # Check for trimming (combined status with populated match info)
            if dir_state.status == "combined":
                combined_path = os.path.join(group_dir, "combined.mp4")
                if os.path.exists(combined_path):
                    # Check if match info is populated
                    match_info_path = os.path.join(group_dir, "match_info.ini")
                    if os.path.exists(match_info_path):
                        match_info = MatchInfo.from_file(match_info_path)
                        if match_info and match_info.is_populated():
                            if self.video_processor:
                                await self.video_processor.add_work(TrimTask.from_match_info(group_dir, match_info))
            
            # Check for videos to upload (autocam_complete status)
            if dir_state.status == "autocam_complete":
                # Check if video upload is enabled
                if (self.config.has_section('YOUTUBE') and 
                    self.config.getboolean('YOUTUBE', 'enabled', fallback=False)):
                    if self.upload_processor:
                        await self.upload_processor.add_work(VideoUploadTask(group_dir))
                        
        except Exception as e:
            logger.error(f"STATE_AUDITOR: Error auditing directory {group_dir}: {e}") 