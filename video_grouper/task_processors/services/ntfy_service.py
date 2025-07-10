"""
NTFY service for interactive user notifications and input.
"""

import json
import logging
import os
from typing import Dict, Optional, Any, Set
from datetime import datetime
import asyncio

from video_grouper.api_integrations.ntfy import NtfyAPI
from video_grouper.models import MatchInfo
from video_grouper.utils.config import NtfyConfig
from ...utils.paths import get_ntfy_service_state_path

logger = logging.getLogger(__name__)


class NtfyService:
    """
    Service for NTFY API integration with state tracking.
    Handles interactive notifications and tracks pending tasks (user input and queue).
    """

    def __init__(self, config: NtfyConfig, storage_path: str):
        """
        Initialize NTFY service.

        Args:
            config: Configuration object
            storage_path: Path to storage directory
        """
        self.config = config
        self.storage_path = storage_path
        self.ntfy_api = None
        self.enabled = False

        # State tracking for pending tasks
        self._pending_tasks: Dict[str, Dict[str, Any]] = {}
        self._processed_dirs: Set[str] = set()
        self._state_file = get_ntfy_service_state_path(storage_path)

        # For handling direct responses to prompts
        self._response_events: Dict[str, asyncio.Event] = {}
        self._response_data: Dict[str, Optional[str]] = {}

        self._state: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._initialized = False

        self._initialize_api()
        self._load_state()

    def _initialize_api(self) -> None:
        """Initialize NTFY API."""
        try:
            self.ntfy_api = NtfyAPI(self.config, service_callback=self)
            self.enabled = self.ntfy_api.enabled

            if self.enabled:
                logger.info("NTFY service enabled")
            else:
                logger.info("NTFY service disabled")
        except Exception as e:
            logger.error(f"Error initializing NTFY API: {e}")
            self.enabled = False

    async def _ensure_initialized(self) -> bool:
        """Ensure NTFY API is fully initialized. Called before first use."""
        if not self.enabled or not self.ntfy_api:
            return False

        # Check if already initialized
        if hasattr(self.ntfy_api, "_initialized") and self.ntfy_api._initialized:
            return True

        try:
            await self.ntfy_api.initialize()
            # Mark as initialized to avoid repeated calls
            self.ntfy_api._initialized = True
            return True
        except Exception as e:
            logger.error(f"Error initializing NTFY API async components: {e}")
            self.enabled = False
            return False

    def _load_state(self) -> None:
        """Load unified NTFY state from disk."""
        if not os.path.exists(self._state_file):
            return
        try:
            with open(self._state_file, "r") as f:
                state = json.load(f)
            self._pending_tasks = state.get("pending_tasks", {})
            self._processed_dirs = set(state.get("processed_dirs", []))
            logger.debug(
                f"NTFY service loaded unified state: {len(self._pending_tasks)} pending tasks, "
                f"{len(self._processed_dirs)} processed dirs"
            )
        except Exception as e:
            logger.error(f"Error loading NTFY unified state: {e}")
            self._pending_tasks = {}
            self._processed_dirs = set()

    def _save_state(self) -> None:
        """Save unified NTFY state to disk."""
        try:
            state = {
                "pending_tasks": self._pending_tasks,
                "processed_dirs": list(self._processed_dirs),
                "last_updated": datetime.now().isoformat(),
            }
            with open(self._state_file, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving NTFY unified state: {e}")

    def is_waiting_for_input(self, group_dir: str) -> bool:
        """Check if we're waiting for user input for a specific directory."""
        return (
            group_dir in self._pending_tasks
            and self._pending_tasks[group_dir].get("status") == "waiting_for_input"
        )

    def has_been_processed(self, group_dir: str) -> bool:
        """Check if a directory has already been processed with NTFY."""
        return group_dir in self._processed_dirs

    def mark_as_processed(self, group_dir: str) -> None:
        """Mark a directory as processed."""
        self._processed_dirs.add(group_dir)
        self._pending_tasks.pop(group_dir, None)
        self._save_state()

    def mark_waiting_for_input(
        self, group_dir: str, task_type: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Mark a directory as waiting for user input."""
        # Convert old metadata structure to new unified structure
        if metadata and "task_metadata" in metadata:
            # Already in new format
            task_metadata = metadata["task_metadata"]
        else:
            # Convert from old format
            task_metadata = metadata or {}

        self._pending_tasks[group_dir] = {
            "task_type": task_type,
            "status": "waiting_for_input",
            "task_metadata": task_metadata,
            "sent_at": datetime.now().isoformat(),
            "response": None,
        }
        self._save_state()

    def clear_pending_task(self, group_dir: str) -> None:
        """Clear pending task status for a directory."""
        if group_dir in self._pending_tasks:
            del self._pending_tasks[group_dir]
            self._save_state()

    def get_pending_tasks(self) -> Dict[str, Dict[str, Any]]:
        """Get all pending tasks."""
        return self._pending_tasks.copy()

    # For backward compatibility
    def get_pending_inputs(self) -> Dict[str, Dict[str, Any]]:
        return self.get_pending_tasks()

    def get_processed_directories(self) -> Set[str]:
        """Get all processed directories."""
        return self._processed_dirs.copy()

    def get_state_file_path(self) -> str:
        """Get the path to the unified NTFY state file."""
        return self._state_file

    async def shutdown(self) -> None:
        """Shutdown the NTFY service."""
        logger.info("Shutting down NTFY service")
        if self.ntfy_api:
            await self.ntfy_api.shutdown()

    async def process_response(self, response: str) -> None:
        """
        Process a response from NTFY and route it to the appropriate pending task.

        Args:
            response: The user's response message
        """
        logger.info(f"Processing NTFY response: {response}")
        for group_dir, task_data in list(self._pending_tasks.items()):
            task_type = task_data.get("task_type")
            metadata = task_data.get("metadata", {})
            status = task_data.get("status")
            if status != "waiting_for_input":
                logger.debug(
                    f"Skipping task {task_type} for {group_dir} - status is {status}, not waiting_for_input"
                )
                continue
            if self._response_matches_task(response, task_type, metadata):
                logger.info(
                    f"Found matching task for response: {task_type} in {group_dir}"
                )
                await self._process_task_response(
                    group_dir, task_type, metadata, response
                )
                return
        logger.warning(f"No matching task found for response: {response}")
        logger.debug(f"Current pending tasks: {self._pending_tasks}")

    def _response_matches_task(
        self, response: str, input_type: str, metadata: Dict[str, Any]
    ) -> bool:
        """
        Check if a response matches a specific task.

        Args:
            response: The user's response
            input_type: Type of input the task is waiting for
            metadata: Task metadata

        Returns:
            True if the response matches the task
        """
        from ..tasks.ntfy.enums import NtfyInputType

        response_lower = response.lower()

        logger.debug(
            f"Checking if response '{response}' matches task type '{input_type}' with metadata: {metadata}"
        )

        # Check for time information in the response
        # The task's metadata is now directly in metadata (no nested structure)
        time_offset = metadata.get("time_offset")

        if time_offset is not None and time_offset in response:
            logger.debug(f"Response matches by time offset: {time_offset}")
            return True

        # Check for question type in response
        if input_type == NtfyInputType.GAME_START_TIME.value:
            matches = "start" in response_lower or "00:00" in response
            logger.debug(
                f"Game start time check: 'start' in response_lower={('start' in response_lower)}, '00:00' in response={('00:00' in response)}, matches={matches}"
            )
            return matches
        elif input_type == NtfyInputType.GAME_END_TIME.value:
            matches = "end" in response_lower
            logger.debug(
                f"Game end time check: 'end' in response_lower={('end' in response_lower)}, matches={matches}"
            )
            return matches
        elif input_type == NtfyInputType.TEAM_INFO.value:
            # Team info responses might contain team names, locations, etc.
            keywords = ["team", "location", "vs", "against"]
            matches = any(keyword in response_lower for keyword in keywords)
            logger.debug(f"Team info check: keywords={keywords}, matches={matches}")
            return matches

        logger.debug(f"No matching logic found for input type: {input_type}")
        return False

    async def _process_task_response(
        self, group_dir: str, input_type: str, metadata: Dict[str, Any], response: str
    ) -> None:
        """
        Process a response for a specific task.

        Args:
            group_dir: Directory associated with the task
            input_type: Type of input the task is waiting for
            metadata: Task metadata
            response: The user's response
        """
        try:
            # Create the task using the factory
            from ..tasks.ntfy import NtfyTaskFactory

            # Get the config from the metadata (now directly in metadata)
            config = metadata.get("config")
            if not config:
                logger.error(
                    f"No configuration available for task {input_type} in {group_dir}"
                )
                return

            # Use the task's metadata for creating the task
            task = NtfyTaskFactory.create_task(
                input_type, group_dir, config, self, metadata
            )

            if not task:
                logger.error(
                    f"Could not create task of type {input_type} for {group_dir}"
                )
                return

            # Process the response using the task's logic
            result = await task.process_response(response)

            if result.success:
                logger.info(
                    f"Task {input_type} completed successfully: {result.message}"
                )

                # If the task should continue, we might need to create follow-up tasks
                if result.should_continue and result.metadata:
                    logger.info(
                        f"Task {input_type} should continue with metadata: {result.metadata}"
                    )
                    # This would need to be handled by the processor that created the original task

                # Mark task as completed and remove from pending inputs
                if not result.should_continue:
                    self.clear_pending_task(group_dir)
                    logger.info(
                        f"Task {input_type} completed and removed from pending inputs"
                    )

                    # Check if match info is now complete
                    await self._check_match_info_completion(group_dir)

            else:
                logger.error(f"Task {input_type} failed: {result.message}")

        except Exception as e:
            logger.error(
                f"Error processing response for task {input_type} in {group_dir}: {e}"
            )

    async def _check_match_info_completion(self, group_dir: str) -> None:
        """Check if match info has been populated for a directory."""
        logger.info(f"Checking match info completion for {group_dir}")

        match_info_path = os.path.join(group_dir, "match_info.ini")
        if not os.path.exists(match_info_path):
            logger.info(f"No match_info.ini found at {match_info_path}")
            return

        match_info = MatchInfo.from_file(match_info_path)
        logger.info(f"Loaded match_info: {match_info}")

        if match_info and match_info.is_populated():
            logger.info(f"Match info is populated for {group_dir}")
            # User has populated the match info, mark as processed
            logger.info(f"Match info populated for {group_dir}, marking as processed")
            self.mark_as_processed(group_dir)
        else:
            logger.info(f"Match info is not populated for {group_dir}")
            if match_info:
                logger.info(
                    f"Match info fields - my_team_name: '{match_info.my_team_name}', opponent_team_name: '{match_info.opponent_team_name}', location: '{match_info.location}', start_time_offset: '{match_info.start_time_offset}'"
                )
