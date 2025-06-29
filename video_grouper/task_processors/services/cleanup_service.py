"""
Cleanup service for managing file cleanup operations.
"""

import os
import logging
from typing import List, Optional
from video_grouper.utils.directory_state import DirectoryState

logger = logging.getLogger(__name__)


class CleanupService:
    """
    Service for managing file cleanup operations.
    Handles DAV file cleanup and other cleanup tasks.
    """
    
    def __init__(self, storage_path: str):
        """
        Initialize cleanup service.
        
        Args:
            storage_path: Path to storage directory
        """
        self.storage_path = storage_path
    
    def cleanup_dav_files(self, group_dir: str) -> bool:
        """
        Clean up DAV files in a group directory.
        
        Args:
            group_dir: Directory path
            
        Returns:
            True if cleanup was successful, False otherwise
        """
        try:
            logger.info(f"Starting DAV file cleanup for {group_dir}")
            
            # Check if directory has been processed (has combined.mp4)
            combined_path = os.path.join(group_dir, "combined.mp4")
            if not os.path.exists(combined_path):
                logger.debug(f"No combined.mp4 found in {group_dir}, skipping DAV cleanup")
                return False
            
            # Get directory state
            dir_state = DirectoryState(group_dir)
            
            # Find DAV files to clean up
            dav_files = []
            for filename in os.listdir(group_dir):
                if filename.lower().endswith('.dav'):
                    file_path = os.path.join(group_dir, filename)
                    dav_files.append(file_path)
            
            if not dav_files:
                logger.debug(f"No DAV files found in {group_dir}")
                return True
            
            # Remove DAV files
            removed_count = 0
            for dav_file in dav_files:
                try:
                    if os.path.exists(dav_file):
                        os.remove(dav_file)
                        removed_count += 1
                        logger.debug(f"Removed DAV file: {dav_file}")
                except Exception as e:
                    logger.error(f"Error removing DAV file {dav_file}: {e}")
            
            if removed_count > 0:
                logger.info(f"Cleaned up {removed_count} DAV files from {group_dir}")
                # Update directory state to indicate DAV files have been cleaned up
                dir_state.update_dir_state({'status': 'autocam_complete_dav_files_deleted'})
            
            return True
            
        except Exception as e:
            logger.error(f"Error during DAV cleanup for {group_dir}: {e}")
            return False
    
    def cleanup_temporary_files(self, group_dir: str, extensions: Optional[List[str]] = None) -> bool:
        """
        Clean up temporary files in a group directory.
        
        Args:
            group_dir: Directory path
            extensions: List of file extensions to clean up (default: ['.tmp', '.temp'])
            
        Returns:
            True if cleanup was successful, False otherwise
        """
        if extensions is None:
            extensions = ['.tmp', '.temp']
        
        try:
            logger.debug(f"Starting temporary file cleanup for {group_dir}")
            
            # Find temporary files
            temp_files = []
            for filename in os.listdir(group_dir):
                file_path = os.path.join(group_dir, filename)
                if os.path.isfile(file_path):
                    for ext in extensions:
                        if filename.lower().endswith(ext.lower()):
                            temp_files.append(file_path)
                            break
            
            if not temp_files:
                logger.debug(f"No temporary files found in {group_dir}")
                return True
            
            # Remove temporary files
            removed_count = 0
            for temp_file in temp_files:
                try:
                    if os.path.exists(temp_file):
                        os.remove(temp_file)
                        removed_count += 1
                        logger.debug(f"Removed temporary file: {temp_file}")
                except Exception as e:
                    logger.error(f"Error removing temporary file {temp_file}: {e}")
            
            if removed_count > 0:
                logger.info(f"Cleaned up {removed_count} temporary files from {group_dir}")
            
            return True
            
        except Exception as e:
            logger.error(f"Error during temporary file cleanup for {group_dir}: {e}")
            return False
    
    def should_cleanup_dav_files(self, group_dir: str) -> bool:
        """
        Check if DAV files should be cleaned up for a directory.
        
        Args:
            group_dir: Directory path
            
        Returns:
            True if DAV files should be cleaned up, False otherwise
        """
        try:
            # Check if combined.mp4 exists
            combined_path = os.path.join(group_dir, "combined.mp4")
            if not os.path.exists(combined_path):
                return False
            
            # Check directory status
            dir_state = DirectoryState(group_dir)
            
            # Only cleanup if processing is complete or nearly complete
            # (combined, trimmed, or autocam_complete status)
            return dir_state.status in ["combined", "trimmed", "autocam_complete"]
            
        except Exception as e:
            logger.error(f"Error checking DAV cleanup readiness for {group_dir}: {e}")
            return False 