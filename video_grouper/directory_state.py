import os
import json
import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional, Union

from video_grouper.file_state import FileState
from video_grouper.models import RecordingFile

logger = logging.getLogger(__name__)

class DirectoryState:
    """Represents the state of files in a directory with state tracking."""
    def __init__(self, path: str):
        self.path = path
        self.files: Dict[str, Union[FileState, RecordingFile]] = {}
        self.state_file = os.path.join(path, "state.json")
        self._lock = asyncio.Lock()
        self.status: str = "pending"
        self._load_state()
        
    def _load_state(self):
        """Load the state from the JSON file."""
        try:
            if os.path.exists(self.state_file):
                logger.info(f"Loading directory state from {self.state_file}")
                with open(self.state_file, 'r') as f:
                    state_data = json.load(f)
                    self.status = state_data.get("status", "pending")
                    files_data = state_data.get("files", {})
                    self.files = {}
                    
                    # Process each file in the state
                    for path, data in files_data.items():
                        # Handle old state file format
                        if isinstance(data, str):
                            self.files[path] = RecordingFile(
                                file_path=path,
                                status=data
                            )
                        else:
                            self.files[path] = RecordingFile.from_dict(data)
                            
                logger.info(f"Loaded {len(self.files)} files from directory state")
            else:
                logger.info(f"No existing state file found at {self.state_file}")
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Error loading directory state: {e}")
            # If state is corrupted, start fresh
            self.files = {}
            self.status = "pending"
            
    def _save_state_nolock(self):
        """Save state to the state.json file without acquiring the lock."""
        state_data = {
            "status": self.status,
            "files": {fp: file_obj.to_dict() for fp, file_obj in self.files.items()}
        }
        with open(self.state_file, 'w') as f:
            json.dump(state_data, f, indent=4)
        logger.debug(f"Saved directory state with {len(self.files)} files to {self.state_file}")
            
    async def save_state(self):
        """Save state to the state.json file in the group directory."""
        async with self._lock:
            # This method now only saves the current in-memory state.
            # The caller is responsible for loading state first if modifications are based on prior state.
            self._save_state_nolock()
            
    async def add_file(self, file_obj: Union[FileState, RecordingFile]) -> bool:
        """Add a file to the state if it doesn't exist already.
        This is an atomic read-modify-write operation.
        """
        async with self._lock:
            self._load_state()
            
            file_path = file_obj.file_path
            
            if file_path in self.files:
                logger.info(f"File {os.path.basename(file_path)} already in directory state")
                return False
                
            if isinstance(file_obj, RecordingFile):
                file_obj.group_dir = self.path
                
            self.files[file_path] = file_obj
            self._save_state_nolock()
            logger.info(f"Added file {os.path.basename(file_path)} to directory state")
            return True
        
    async def update_file_state(self, file_path: str, **kwargs) -> None:
        """Update the state of a file in the plan."""
        async with self._lock:
            self._load_state()
            if file_path not in self.files:
                logger.warning(f"File {os.path.basename(file_path)} not found in directory state")
                return
            
            for key, value in kwargs.items():
                setattr(self.files[file_path], key, value)
            
            # Update last_updated if the file object has this attribute
            file_obj = self.files[file_path]
            if hasattr(file_obj, 'last_updated'):
                file_obj.last_updated = datetime.now()
            
            self._save_state_nolock()
            logger.debug(f"Updated state for {os.path.basename(file_path)}")
        
    def get_file_by_path(self, file_path: str) -> Optional[Union[FileState, RecordingFile]]:
        """Get a file by its full path."""
        return self.files.get(file_path)

    def get_file_by_status(self, status: str) -> List[Union[FileState, RecordingFile]]:
        """Get all files with the specified status."""
        return [f for f in self.files.values() if hasattr(f, 'status') and f.status == status]
        
    def get_last_file(self) -> Optional['RecordingFile']:
        """Returns the last file in the group based on end time."""
        if not self.files:
            return None
        return max(self.files.values(), key=lambda f: f.end_time)

    def get_files_by_status(self, status: str) -> List['RecordingFile']:
        """Returns a list of files matching the given status."""
        return [f for f in self.files.values() if f.status == status]

    def is_last_file(self, file_path: str) -> bool:
        """Check if this is the last file in the group to be processed."""
        # If this is the only file, it's the last file
        if len(self.files) == 1:
            return True
            
        # If there are any files not yet converted, this is not the last file
        for path, file_obj in self.files.items():
            if path != file_path and hasattr(file_obj, 'status') and file_obj.status not in ["converted", "skipped"]:
                return False
                
        return True
        
    def is_file_in_state(self, file_path: str) -> bool:
        """Check if a file is already in the directory state."""
        return file_path in self.files

    def is_ready_for_combining(self) -> bool:
        """Check if all non-skipped files are converted and ready for combining."""
        if not self.files:
            return False
            
        # Filter out any files that are marked to be skipped.
        files_to_consider = [f for f in self.files.values() if not f.skip]

        # If there are no files left to consider (e.g., all were skipped), we can't combine.
        if not files_to_consider:
            return False
            
        # All of the remaining files must be in the 'converted' state.
        return all(f.status == 'converted' for f in files_to_consider)

    def is_file_in_queue(self, file_path: str) -> bool:
        """Check if a file is already in the directory state."""
        return file_path in self.files
        
    async def mark_file_as_skipped(self, file_path: str) -> None:
        """Mark a file as skipped."""
        await self.update_file_state(file_path, status="skipped", skip=True)

    async def update_file_status(self, file_path: str, status: str, screenshot_path: Optional[str] = None):
        """Update the status of a file."""
        async with self._lock:
            self._load_state()
            if file_path in self.files:
                self.files[file_path].status = status
                if screenshot_path:
                    self.files[file_path].screenshot_path = screenshot_path
                self._save_state_nolock()

    async def update_group_status(self, status: str):
        """Update the status of all files in the group."""
        async with self._lock:
            self._load_state()
            self.status = status
            self._save_state_nolock()
