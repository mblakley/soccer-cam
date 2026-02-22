"""
Autocam Queue Processor for handling Once Autocam video processing tasks.
"""

import asyncio
import logging
import json
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
        # Ensure only one autocam task runs at a time
        self._autocam_semaphore = asyncio.Semaphore(1)

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

    def _get_autocam_input_output_paths(self, group_dir: Path) -> tuple[str, str]:
        """
        Find the raw video file and determine the autocam output path.

        Args:
            group_dir: Directory containing the video group

        Returns:
            Tuple of (input_path, output_path)

        Raises:
            FileNotFoundError: If no raw video file is found
        """
        video_dir = group_dir / "videos"
        if video_dir.exists():
            for f in video_dir.iterdir():
                if f.name.endswith("-raw.mp4"):
                    input_path = str(f)
                    output_path = str(f.with_name(f.name.replace("-raw.mp4", ".mp4")))
                    return input_path, output_path

        raise FileNotFoundError(
            f"No raw video file ending with '-raw.mp4' found in {group_dir}"
        )

    async def discover_work(self) -> None:
        """
        Scan storage path for group directories with 'trimmed' status
        and create autocam tasks for them.
        """
        storage = Path(self.storage_path)
        if not storage.exists():
            return

        for entry in storage.iterdir():
            if not entry.is_dir():
                continue

            state_file = entry / "state.json"
            if not state_file.exists():
                continue

            try:
                with open(state_file, "r") as f:
                    state_data = json.load(f)
            except (json.JSONDecodeError, IOError):
                continue

            if state_data.get("status") != "trimmed":
                continue

            try:
                input_path, output_path = self._get_autocam_input_output_paths(entry)
            except FileNotFoundError:
                continue

            task = AutocamTask(
                group_dir=entry,
                input_path=input_path,
                output_path=output_path,
                autocam_config=self.config.autocam,
            )
            await self.add_work(task)

    async def _add_to_youtube_queue(self, group_dir: Path) -> None:
        """
        Add a group to the YouTube upload queue.

        Args:
            group_dir: Directory containing the video group
        """
        try:
            # Import here to avoid circular imports
            from video_grouper.task_processors.tasks.upload import YoutubeUploadTask

            # Create the upload task with the minimal required argument.
            # Convert the absolute path to a relative path from storage_path
            relative_group_dir = group_dir.relative_to(Path(self.storage_path))
            youtube_task = YoutubeUploadTask(group_dir=str(relative_group_dir))

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
