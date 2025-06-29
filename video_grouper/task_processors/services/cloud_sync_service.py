"""
Cloud sync service for managing cloud synchronization operations.
"""

import logging
import os
from typing import Optional, Dict, Any
import configparser

from video_grouper.api_integrations.cloud_sync import CloudSync
from video_grouper.utils.directory_state import DirectoryState

logger = logging.getLogger(__name__)


class CloudSyncService:
    """
    Service for managing cloud synchronization operations.
    Handles uploading videos to cloud storage services.
    """
    
    def __init__(self, config: configparser.ConfigParser, storage_path: str):
        """
        Initialize cloud sync service.
        
        Args:
            config: Configuration object
            storage_path: Path to storage directory
        """
        self.config = config
        self.storage_path = storage_path
        self.cloud_sync = None
        self.enabled = False
        
        self._initialize_cloud_sync()
    
    def _initialize_cloud_sync(self) -> None:
        """Initialize cloud sync if enabled."""
        try:
            if (self.config.has_section('CLOUD_SYNC') and 
                self.config.getboolean('CLOUD_SYNC', 'enabled', fallback=False)):
                
                # Create cloud sync instance
                self.cloud_sync = CloudSync(self.config, self.storage_path)
                self.enabled = self.cloud_sync.enabled
                
                if self.enabled:
                    logger.info("Cloud sync service enabled")
                else:
                    logger.info("Cloud sync service disabled - configuration invalid")
            else:
                logger.info("Cloud sync service disabled in configuration")
                
        except Exception as e:
            logger.error(f"Error initializing cloud sync service: {e}")
            self.enabled = False
    
    def should_sync_directory(self, group_dir: str) -> bool:
        """
        Check if a directory should be synced to cloud storage.
        
        Args:
            group_dir: Directory path
            
        Returns:
            True if directory should be synced, False otherwise
        """
        if not self.enabled:
            return False
            
        try:
            # Check directory status
            dir_state = DirectoryState(group_dir)
            
            # Only sync completed directories
            if dir_state.status not in ["trimmed", "autocam_complete"]:
                return False
            
            # Check if final video exists
            final_video_path = self._get_final_video_path(group_dir)
            if not final_video_path or not os.path.exists(final_video_path):
                return False
            
            # Check if already synced
            if self._is_already_synced(group_dir):
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Error checking sync readiness for {group_dir}: {e}")
            return False
    
    def _get_final_video_path(self, group_dir: str) -> Optional[str]:
        """
        Get the path to the final video file for a directory.
        
        Args:
            group_dir: Directory path
            
        Returns:
            Path to final video file or None if not found
        """
        # Check for trimmed video first
        trimmed_path = os.path.join(group_dir, "trimmed.mp4")
        if os.path.exists(trimmed_path):
            return trimmed_path
        
        # Fall back to combined video
        combined_path = os.path.join(group_dir, "combined.mp4")
        if os.path.exists(combined_path):
            return combined_path
        
        return None
    
    def _is_already_synced(self, group_dir: str) -> bool:
        """
        Check if a directory has already been synced.
        
        Args:
            group_dir: Directory path
            
        Returns:
            True if already synced, False otherwise
        """
        try:
            # Check for sync marker file
            sync_marker = os.path.join(group_dir, ".cloud_synced")
            return os.path.exists(sync_marker)
            
        except Exception:
            return False
    
    def _mark_as_synced(self, group_dir: str) -> None:
        """
        Mark a directory as synced.
        
        Args:
            group_dir: Directory path
        """
        try:
            sync_marker = os.path.join(group_dir, ".cloud_synced")
            with open(sync_marker, 'w') as f:
                f.write("synced")
        except Exception as e:
            logger.error(f"Error marking {group_dir} as synced: {e}")
    
    def sync_directory(self, group_dir: str) -> bool:
        """
        Sync a directory to cloud storage.
        
        Args:
            group_dir: Directory path
            
        Returns:
            True if sync was successful, False otherwise
        """
        if not self.enabled:
            logger.debug("Cloud sync service not enabled")
            return False
        
        if not self.should_sync_directory(group_dir):
            logger.debug(f"Directory {group_dir} should not be synced")
            return False
        
        try:
            logger.info(f"Starting cloud sync for {group_dir}")
            
            # Get final video path
            final_video_path = self._get_final_video_path(group_dir)
            if not final_video_path:
                logger.error(f"No final video found for {group_dir}")
                return False
            
            # Sync the directory
            success = self.cloud_sync.sync_files_from_directory(group_dir)
            
            if success:
                logger.info(f"Successfully synced {group_dir} to cloud storage")
                self._mark_as_synced(group_dir)
                return True
            else:
                logger.error(f"Failed to sync {group_dir} to cloud storage")
                return False
                
        except Exception as e:
            logger.error(f"Error during cloud sync for {group_dir}: {e}")
            return False
    
    def get_sync_status(self, group_dir: str) -> Dict[str, Any]:
        """
        Get sync status for a directory.
        
        Args:
            group_dir: Directory path
            
        Returns:
            Dictionary with sync status information
        """
        return {
            'enabled': self.enabled,
            'should_sync': self.should_sync_directory(group_dir),
            'already_synced': self._is_already_synced(group_dir),
            'final_video_exists': self._get_final_video_path(group_dir) is not None
        } 