"""
Cleanup service for managing file cleanup operations.
"""

import os
import logging
from typing import List, Optional

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

    def cleanup_temporary_files(
        self, group_dir: str, extensions: Optional[List[str]] = None
    ) -> bool:
        """
        Clean up temporary files in a group directory.

        Args:
            group_dir: Directory path
            extensions: List of file extensions to clean up (default: ['.tmp', '.temp'])

        Returns:
            True if cleanup was successful, False otherwise
        """
        if extensions is None:
            extensions = [".tmp", ".temp"]

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
                logger.info(
                    f"Cleaned up {removed_count} temporary files from {group_dir}"
                )

            return True

        except Exception as e:
            logger.error(f"Error during temporary file cleanup for {group_dir}: {e}")
            return False

    def should_cleanup_dav_files(self, group_dir: str) -> bool:
        # This method is now obsolete and can be removed if not used elsewhere
        return False

    async def process_directory(self, group_dir: str) -> None:
        """
        Process cleanup tasks for a directory. (DAV cleanup removed)

        Args:
            group_dir: Directory path
            dir_state: Directory state object
        """
        try:
            # Only clean up temporary files (DAV cleanup removed)
            self.cleanup_temporary_files(group_dir)
        except Exception as e:
            logger.error(f"Error during directory cleanup for {group_dir}: {e}")
