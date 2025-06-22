from __future__ import annotations
import asyncio
from typing import TYPE_CHECKING, Optional, Union
import logging
import json
import os
from datetime import datetime
from .locking import FileLock
from .models import RecordingFile

if TYPE_CHECKING:
    from .models import RecordingFile

logger = logging.getLogger(__name__)

class DirectoryState:
    """Represents the state of files in a directory with state tracking."""
    def __init__(self, directory_path: str):
        self.directory_path = directory_path
        self.state_file_path = os.path.join(directory_path, "state.json")
        self.files: dict[str, RecordingFile] = {}
        self._lock = asyncio.Lock()
        self.status: str = "pending"
        self.error_message: Optional[str] = None

        # Validate directory name format before proceeding
        dir_name = os.path.basename(directory_path)
        try:
            datetime.strptime(dir_name, "%Y.%m.%d-%H.%M.%S")
        except ValueError:
            # Not a video group directory, return early
            return

        self._load_state()
        
    def _load_state(self):
        """Load the state from the JSON file."""
        try:
            with FileLock(self.state_file_path):
                if os.path.exists(self.state_file_path):
                    logger.debug(f"Loading directory state from {self.state_file_path}")
                    with open(self.state_file_path, 'r') as f:
                        state_data = json.load(f)
                        self.status = state_data.get("status", "pending")
                        self.error_message = state_data.get("error_message")
                        
                        loaded_files = state_data.get("files", {})
                        for file_path, file_data in loaded_files.items():
                            # Ensure backward compatibility with older state files
                            file_data.setdefault('total_size', 0)
                            
                            self.files[file_path] = RecordingFile.from_dict(file_data)
                            self.files[file_path].file_path = file_path # Ensure file_path is set

                    logger.debug(f"Loaded {len(self.files)} files from directory state")
                else:
                    logger.debug(f"No existing state file found at {self.state_file_path}")
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Error loading directory state: {e}")
            self.files = {}
            self.status = "pending"
        except TimeoutError as e:
            logger.error(f"Timeout loading state for {self.directory_path}: {e}")
        except Exception as e:
            logger.error(f"Could not load state for {self.directory_path}: {e}")
            
    def _save_state_nolock(self):
        """Saves the current state to the JSON file without acquiring the lock."""
        files_dict = {
            fp: fs.to_dict() for fp, fs in self.files.items()
        }
        state_data = {
            "status": self.status,
            "error_message": self.error_message,
            "files": files_dict
        }
        try:
            with FileLock(self.state_file_path):
                with open(self.state_file_path, 'w') as f:
                    json.dump(state_data, f, indent=4)
        except TimeoutError as e:
            logger.error(f"Timeout saving state for {self.directory_path}: {e}")
        except Exception as e:
            logger.error(f"Could not save state for {self.directory_path}: {e}")
        logger.debug(f"Saved directory state with {len(self.files)} files to {self.state_file_path}")
            
    async def save_state(self):
        """Asynchronously saves the current state to the JSON file."""
        async with self._lock:
            self._save_state_nolock()
            
    async def add_file(self, file_path, file_obj: RecordingFile):
        """Adds or updates a file in the directory state."""
        async with self._lock:
            if file_path not in self.files:
                if isinstance(file_obj, RecordingFile):
                    file_obj.group_dir = self.directory_path
                
                self.files[file_path] = file_obj
                self._save_state_nolock()

    async def update_file_state(self, file_path: str, **kwargs) -> None:
        """Update the state of a file in the plan."""
        async with self._lock:
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
        
    def get_file_by_path(self, file_path: str) -> Optional[RecordingFile]:
        """Get a file by its full path."""
        return self.files.get(file_path)

    def get_file_by_status(self, status: str) -> list[RecordingFile]:
        """Get all files with the specified status."""
        return [f for f in self.files.values() if hasattr(f, 'status') and f.status == status]
        
    def get_last_file(self) -> Optional[RecordingFile]:
        """Returns the last file in the group based on end time."""
        if not self.files:
            return None
        return max(self.files.values(), key=lambda f: f.end_time)
    
    def get_first_file(self) -> Optional[RecordingFile]:
        """Returns the first file in the group based on start time."""
        if not self.files:
            return None
        return min(self.files.values(), key=lambda f: f.start_time)

    def get_files_by_status(self, status: str) -> list[RecordingFile]:
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
        """Marks a file to be skipped in future processing, without changing its status."""
        async with self._lock:
            if file_path in self.files:
                self.files[file_path].skip = True
                self._save_state_nolock()

    async def update_file_status(self, file_path: str, status: str):
        async with self._lock:
            if file_path in self.files:
                self.files[file_path].status = status
                self._save_state_nolock()

    async def update_group_status(self, status: str, error_message: Optional[str] = None):
        """Update the status of all files in the group."""
        async with self._lock:
            self.status = status
            self.error_message = error_message
            self._save_state_nolock()
