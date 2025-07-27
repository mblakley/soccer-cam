"""
NTFY Queue Processor for handling NTFY questions and responses.

This processor acts as the central coordinator for NTFY interactions:
1. Maintains a queue of tasks to ask users
2. Sends questions through NTFY and waits for responses
3. Processes responses using task-specific logic
4. Handles startup processing of existing pending requests
5. Listens for responses via NTFY subscription API
"""

import os
import logging
from typing import Dict, Any, Optional

from .services.ntfy_service import NtfyService
from .tasks.ntfy import BaseNtfyTask, NtfyTaskFactory
from video_grouper.models import MatchInfo
from .base_queue_processor import QueueProcessor
from video_grouper.utils.config import Config
from .queue_type import QueueType
from video_grouper.task_processors.services.match_info_service import MatchInfoService
from video_grouper.utils.paths import get_combined_video_path

logger = logging.getLogger(__name__)


class NtfyProcessor(QueueProcessor):
    """
    Central coordinator for NTFY questions and responses.

    This processor:
    1. Maintains a queue of questions to ask users
    2. Sends questions through NTFY and waits for responses
    3. Processes responses and queues follow-up tasks
    4. Handles startup processing of existing pending requests
    """

    def __init__(
        self,
        storage_path: str,
        config: Config,
        ntfy_service: NtfyService,
        match_info_service: MatchInfoService,
        poll_interval: int = 30,
        video_processor: Optional[Any] = None,
    ):
        """
        Initialize the NTFY queue processor.

        Args:
            storage_path: Path to storage directory
            config: Configuration object
            ntfy_service: NTFY service instance
            match_info_service: MatchInfo service instance
            poll_interval: How often to check for responses (in seconds)
            video_processor: Reference to video processor (default None)
        """
        super().__init__(storage_path, config)
        self.ntfy_service = ntfy_service
        self.match_info_service = match_info_service
        self.poll_interval = poll_interval
        self.video_processor = video_processor
        self._stopping = False

    @property
    def queue_type(self) -> QueueType:
        """Return the queue type for this processor."""
        return QueueType.NTFY

    def get_state_file_name(self) -> str:
        """Get the name of the unified NTFY state file."""
        return "ntfy_service_state.json"

    async def start(self) -> None:
        """Start the NTFY queue processor."""
        logger.info("Starting NTFY Queue Processor")
        await self._process_pending_requests_on_startup()
        await super().start()

    async def process_item(self, item: BaseNtfyTask) -> None:
        """
        Process a single NTFY task.

        Args:
            item: BaseNtfyTask to process
        """
        try:
            logger.info(f"NTFY: Processing task: {item}")

            # Store the config in the task metadata for response processing
            if not item.metadata.get("config"):
                item.metadata["config"] = self.config

            # Execute the task using its own execute method
            success = await item.execute()

            if success:
                logger.info(f"NTFY: Successfully sent notification for task: {item}")
            else:
                logger.error(f"NTFY: Failed to send notification for task: {item}")

        except Exception as e:
            logger.error(f"NTFY: Error processing task {item}: {e}")

    def get_item_key(self, item: BaseNtfyTask) -> str:
        """Get unique key for a BaseNtfyTask."""
        return f"{item.get_task_type()}:{item.group_dir}:{hash(item)}"

    async def _process_pending_requests_on_startup(self) -> None:
        """Process any pending NTFY requests on startup."""
        from .ntfy_enums import NtfyStatus

        pending_tasks = self.ntfy_service.get_pending_tasks()
        if not pending_tasks:
            logger.info("No pending NTFY requests found on startup")
            return
        logger.info(f"Found {len(pending_tasks)} pending NTFY requests on startup")
        for group_dir, task_data in pending_tasks.items():
            task_type = task_data.get("task_type")
            status = task_data.get("status", "unknown")
            task_metadata = task_data.get("task_metadata", {})
            logger.info(
                f"Processing pending {task_type} request for {group_dir} (status: {status})"
            )
            if status == NtfyStatus.QUEUED.value:
                await self._recreate_queued_task(group_dir, task_type, task_metadata)
            elif (
                status == NtfyStatus.IN_PROGRESS.value or status == "waiting_for_input"
            ):
                logger.info(
                    f"Task {task_type} is in progress for {group_dir}, waiting for response"
                )
            else:
                logger.error(
                    f"Invalid pending task format for {group_dir}: status={status}, task_type={task_type}"
                )
                logger.error(
                    f"Expected status to be '{NtfyStatus.QUEUED.value}' or '{NtfyStatus.IN_PROGRESS.value}' or 'waiting_for_input', got '{status}'"
                )
                self.ntfy_service.clear_pending_task(group_dir)

    async def _recreate_queued_task(
        self, group_dir: str, task_type: str, metadata: Dict[str, Any]
    ) -> None:
        """Recreate a task that was queued but not sent."""
        logger.info(f"Recreating queued task for {group_dir}: {task_type}")

        # Recreate the task using the task factory
        task = NtfyTaskFactory.create_task(
            task_type, group_dir, self.config, self.ntfy_service, metadata
        )
        if task:
            await self.add_work(task)
        else:
            logger.warning(f"Could not recreate task of type: {task_type}")

    async def _check_match_info_completion(self, group_dir: str) -> None:
        """Check if match info has been populated for a directory."""
        logger.info(f"NTFY_QUEUE: Checking match info completion for {group_dir}")
        if await self.match_info_service.is_match_info_complete(group_dir):
            logger.info(f"NTFY_QUEUE: Match info is populated for {group_dir}")
            self.ntfy_service.mark_as_processed(group_dir)
            # Queue trim task if we have a combined video
            combined_path = get_combined_video_path(group_dir, self.storage_path)
            logger.info(f"NTFY_QUEUE: Checking for combined video at {combined_path}")
            logger.info(
                f"NTFY_QUEUE: Combined video exists: {os.path.exists(combined_path)}"
            )
            logger.info(
                f"NTFY_QUEUE: Video processor available: {self.video_processor is not None}"
            )

            if os.path.exists(combined_path) and self.video_processor:
                from .tasks.video import TrimTask

                match_info, _ = MatchInfo.get_or_create(group_dir, self.storage_path)
                trim_task = TrimTask.from_match_info(group_dir, match_info)
                logger.info(f"NTFY_QUEUE: Created trim task: {trim_task}")

                await self.video_processor.add_work(trim_task)
                logger.info(f"Queued trim task for {group_dir}")
            else:
                logger.warning(
                    f"NTFY_QUEUE: Cannot queue trim task - combined video exists: {os.path.exists(combined_path)}, video processor available: {self.video_processor is not None}"
                )
        else:
            logger.info(f"NTFY_QUEUE: Match info is not populated for {group_dir}")
            # Get match info for logging purposes
            match_info, _ = MatchInfo.get_or_create(group_dir, self.storage_path)
            if match_info:
                logger.info(
                    f"NTFY_QUEUE: Match info fields - my_team_name: '{match_info.my_team_name}', opponent_team_name: '{match_info.opponent_team_name}', location: '{match_info.location}', start_time_offset: '{match_info.start_time_offset}'"
                )

    async def save_state(self) -> None:
        # No-op: state is managed by NtfyService
        pass

    async def load_state(self) -> None:
        # No-op: state is managed by NtfyService
        pass

    async def stop(self) -> None:
        """Stop the NTFY queue processor."""
        logger.info("Stopping NTFY Queue Processor")
        self._stopping = True

        # Stop the main queue processing loop
        await super().stop()

    async def request_match_info_for_directory(
        self, group_dir: str, combined_video_path: str, force: bool = False
    ) -> bool:
        """
        Request match info for a combined directory.

        Args:
            group_dir: Directory path
            combined_video_path: Path to combined video
            force: Force request even if already processed

        Returns:
            True if tasks were added to queue, False otherwise
        """
        logger.info(f"NTFY_QUEUE: Requesting match info for {group_dir}")

        # Check if already processed or waiting
        if not force:
            if self.ntfy_service.has_been_processed(group_dir):
                logger.info(
                    f"NTFY_QUEUE: Directory {group_dir} already processed, skipping"
                )
                return False

            if self.ntfy_service.is_waiting_for_input(group_dir):
                logger.info(
                    f"NTFY_QUEUE: Already waiting for input for {group_dir}, skipping"
                )
                return False

        # Check if match info is already populated
        match_info, _ = MatchInfo.get_or_create(group_dir)

        if not force and match_info and match_info.is_populated():
            logger.info(f"NTFY_QUEUE: Match info already populated for {group_dir}")
            self.ntfy_service.mark_as_processed(group_dir)
            return False

        # Get existing team info
        existing_info = {}
        if match_info:
            existing_info = match_info.get_team_info()

        tasks_added = False

        # Determine what team information is missing
        missing_fields = []
        if "team_name" not in existing_info and "my_team_name" not in existing_info:
            missing_fields.append("team name")
        if (
            "opponent_name" not in existing_info
            and "opponent_team_name" not in existing_info
        ):
            missing_fields.append("opponent team name")
        if "location" not in existing_info:
            missing_fields.append("game location")

        # Add team info task if needed
        if missing_fields:
            from .tasks.ntfy import TeamInfoTask

            task = TeamInfoTask(
                group_dir,
                self.config,
                self.ntfy_service,
                combined_video_path,
                existing_info,
            )
            await self.add_work(task)
            tasks_added = True
            logger.info(f"Added team info task for {group_dir}")

        # Add game start time task
        from .tasks.ntfy import GameStartTask

        start_task = GameStartTask(
            group_dir, self.config, self.ntfy_service, combined_video_path, "00:00", 0
        )
        await self.add_work(start_task)
        tasks_added = True

        # Check if we should also ask for end time
        if match_info and match_info.start_time_offset:
            from .tasks.ntfy import GameEndTask

            end_task = GameEndTask(
                group_dir,
                self.config,
                self.ntfy_service,
                combined_video_path,
                match_info.start_time_offset,
            )
            await self.add_work(end_task)
            tasks_added = True

        return tasks_added
