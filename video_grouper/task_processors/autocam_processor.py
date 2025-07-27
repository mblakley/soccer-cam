"""
Autocam Queue Processor for handling Once Autocam video processing tasks.
"""

import logging
import json
import os
from pathlib import Path

from .base_queue_processor import QueueProcessor
from .tasks.autocam import AutocamTask
from .queue_type import QueueType
from video_grouper.utils.config import Config

logger = logging.getLogger(__name__)


class AutocamProcessor(QueueProcessor):
    """
    Task processor for Once Autocam operations.
    Processes autocam tasks sequentially.
    """

    def __init__(self, storage_path: str, config: Config, upload_processor=None):
        """
        Initialize the autocam processor.

        Args:
            storage_path: Path to the storage directory
            config: Configuration object
            upload_processor: Optional upload processor for YouTube uploads
        """
        super().__init__(storage_path, config)
        self.upload_processor = upload_processor
        self._is_first_check = True

    @property
    def queue_type(self) -> QueueType:
        """Return the queue type for this processor."""
        return QueueType.AUTOCAM

    async def process_item(self, item: AutocamTask) -> None:
        """
        Process an autocam task.

        Args:
            item: AutocamTask to process
        """
        try:
            logger.info(f"AUTOCAM: Processing task: {item}")

            # Execute the task using its own execute method
            success = await item.execute()

            if success:
                logger.info(f"AUTOCAM: Successfully completed task: {item}")
                await self._handle_successful_completion(item)
            else:
                logger.error(f"AUTOCAM: Task execution failed: {item}")

        except Exception as e:
            logger.error(f"AUTOCAM: Error processing task {item}: {e}")

    def get_item_key(self, item: AutocamTask) -> str:
        """Get unique key for an AutocamTask."""
        return f"{item.task_type}:{item.get_item_path()}:{hash(item)}"

    async def discover_work(self) -> None:
        """
        Discover new work (trimmed groups) and enqueue as tasks if not already queued or processed.
        This is called by the polling loop in the base QueueProcessor.
        """
        logger.info("AUTOCAM: Discovering new trimmed groups for autocam processing...")
        groups_dir = Path(self.storage_path)
        # Get all currently queued group names
        queued_group_names = set()
        if self._queue is not None:
            temp_queue = []
            while not self._queue.empty():
                item = await self._queue.get()
                if hasattr(item, "group_dir"):
                    queued_group_names.add(str(item.group_dir))
                temp_queue.append(item)
            for item in temp_queue:
                await self._queue.put(item)
        # Scan for new trimmed groups
        for group_dir in groups_dir.iterdir():
            if group_dir.is_dir():
                state_file = group_dir / "state.json"
                if state_file.exists():
                    try:
                        with open(state_file, "r") as f:
                            state_data = json.load(f)
                        status = state_data.get("status")
                        if (
                            status == "trimmed"
                            and str(group_dir) not in queued_group_names
                        ):
                            try:
                                input_path, output_path = (
                                    self._get_autocam_input_output_paths(group_dir)
                                )
                                task = AutocamTask(
                                    group_dir=group_dir,
                                    input_path=input_path,
                                    output_path=output_path,
                                    autocam_config=self.config.autocam,
                                )
                                await self.add_work(task)
                                logger.info(
                                    f"AUTOCAM: Enqueued autocam task for group '{group_dir.name}'"
                                )
                            except Exception as e:
                                logger.error(
                                    f"AUTOCAM: Could not enqueue autocam task for group '{group_dir.name}': {e}"
                                )
                    except Exception as e:
                        logger.error(
                            f"AUTOCAM: Error reading state.json for group '{group_dir.name}': {e}"
                        )

    async def _handle_successful_completion(self, item: AutocamTask) -> None:
        """
        Handle successful completion of an autocam task.

        Args:
            item: The completed AutocamTask
        """
        group_dir = item.group_dir
        group_name = group_dir.name

        # Update the group's main state file
        logger.info(
            f"AUTOCAM: Updating group '{group_name}' status to autocam_complete."
        )
        state_file = group_dir / "state.json"

        if state_file.exists():
            try:
                with open(state_file, "r") as f:
                    state_data = json.load(f)
                state_data["status"] = "autocam_complete"
                with open(state_file, "w") as f:
                    json.dump(state_data, f, indent=4)

                # Check if YouTube uploads are enabled
                if self.config.youtube.enabled:
                    # Add to YouTube upload queue
                    logger.info(
                        f"AUTOCAM: YouTube uploads are enabled. Adding group '{group_name}' to YouTube upload queue."
                    )
                    await self._add_to_youtube_queue(group_dir)
                else:
                    logger.info(
                        f"AUTOCAM: YouTube uploads are not enabled. Skipping upload for group '{group_name}'."
                    )

            except (json.JSONDecodeError, IOError) as e:
                logger.error(
                    f"AUTOCAM: Could not update status to autocam_complete for group {group_name}: {e}"
                )
        else:
            logger.warning(
                f"AUTOCAM: state.json not found for group {group_name} on successful completion."
            )

    async def _add_to_youtube_queue(self, group_dir: Path) -> None:
        """
        Add a group to the YouTube upload queue.

        Args:
            group_dir: Directory containing the video group
        """
        try:
            # Import here to avoid circular imports
            from video_grouper.task_processors.tasks.upload import YoutubeUploadTask
            from video_grouper.task_processors.services.ntfy_service import NtfyService

            # Create NTFY service for the upload task
            ntfy_service = NtfyService(self.storage_path, self.config)

            # Create the upload task with the minimal required argument.
            youtube_task = YoutubeUploadTask(group_dir=str(group_dir))

            # Add to the upload processor queue if available
            if self.upload_processor:
                await self.upload_processor.add_work(youtube_task)
                logger.info(
                    f"AUTOCAM: Added YouTube upload task for group {group_dir.name} to upload queue"
                )
            else:
                logger.warning(
                    f"AUTOCAM: No upload processor available for group {group_dir.name}"
                )

        except Exception as e:
            logger.error(
                f"AUTOCAM: Error creating YouTube upload task for group {group_dir.name}: {e}"
            )

    def _get_autocam_input_output_paths(self, group_dir: Path) -> tuple[str, str]:
        """
        Get the input and output paths for autocam processing.

        Args:
            group_dir: Directory containing the video group

        Returns:
            Tuple of (input_path, output_path)

        Raises:
            FileNotFoundError: If no '-raw.mp4' file is found
        """
        for root, _, files in os.walk(group_dir):
            for file in files:
                if file.endswith("-raw.mp4"):
                    input_path = Path(root) / file
                    output_path = input_path.with_name(
                        input_path.name.replace("-raw.mp4", ".mp4")
                    )
                    return str(input_path), str(output_path)

        raise FileNotFoundError(f"No '-raw.mp4' file found in {group_dir}")
