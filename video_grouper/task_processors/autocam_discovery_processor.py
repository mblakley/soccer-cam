"""
Autocam Discovery Processor for finding trimmed directories and queuing autocam tasks.
"""

import logging
import json
from pathlib import Path
import os

from .base_polling_processor import PollingProcessor
from .tasks.autocam import AutocamTask
from video_grouper.utils.config import Config

logger = logging.getLogger(__name__)


class AutocamDiscoveryProcessor(PollingProcessor):
    """
    Task processor for discovering trimmed directories and queuing autocam tasks.
    Polls for new trimmed directories and adds them to the autocam processor queue.
    """

    def __init__(
        self,
        storage_path: str,
        config: Config,
        autocam_processor=None,
        poll_interval: int = 30,
    ):
        """
        Initialize the autocam discovery processor.

        Args:
            storage_path: Path to the storage directory
            config: Configuration object
            autocam_processor: Reference to the AutocamProcessor for queuing tasks
            poll_interval: How often to poll for new trimmed directories (in seconds)
        """
        super().__init__(storage_path, config, poll_interval)
        self.autocam_processor = autocam_processor

    async def discover_work(self) -> None:
        """
        Discover new trimmed directories and queue autocam tasks.
        This is called periodically by the polling loop.
        """
        logger.info(
            "AUTOCAM_DISCOVERY: Discovering new trimmed groups for autocam processing..."
        )
        groups_dir = Path(self.storage_path)
        logger.info(f"AUTOCAM_DISCOVERY: Scanning directory: {groups_dir}")

        if not self.autocam_processor:
            logger.warning(
                "AUTOCAM_DISCOVERY: No autocam processor available for queuing tasks"
            )
            return

        # Get all currently queued group names
        queued_group_names = set()
        if self.autocam_processor._queue is not None:
            temp_queue = []
            while not self.autocam_processor._queue.empty():
                item = await self.autocam_processor._queue.get()
                if hasattr(item, "group_dir"):
                    queued_group_names.add(str(item.group_dir))
                temp_queue.append(item)
            for item in temp_queue:
                await self.autocam_processor._queue.put(item)

        logger.info(f"AUTOCAM_DISCOVERY: Currently queued groups: {queued_group_names}")

        # Scan for new trimmed groups
        for group_dir in groups_dir.iterdir():
            if group_dir.is_dir():
                logger.debug(f"AUTOCAM_DISCOVERY: Checking directory: {group_dir}")
                state_file = group_dir / "state.json"
                if state_file.exists():
                    try:
                        with open(state_file, "r") as f:
                            state_data = json.load(f)
                        status = state_data.get("status")
                        logger.debug(
                            f"AUTOCAM_DISCOVERY: Directory {group_dir.name} has status: {status}"
                        )

                        if (
                            status == "trimmed"
                            and str(group_dir) not in queued_group_names
                        ):
                            logger.info(
                                f"AUTOCAM_DISCOVERY: Found trimmed directory: {group_dir.name}"
                            )
                            try:
                                input_path, output_path = (
                                    self._get_autocam_input_output_paths(group_dir)
                                )
                                logger.info(
                                    f"AUTOCAM_DISCOVERY: Input path: {input_path}"
                                )
                                logger.info(
                                    f"AUTOCAM_DISCOVERY: Output path: {output_path}"
                                )

                                task = AutocamTask(
                                    group_dir=group_dir,
                                    input_path=input_path,
                                    output_path=output_path,
                                    autocam_config=self.config.autocam,
                                )
                                await self.autocam_processor.add_work(task)
                                logger.info(
                                    f"AUTOCAM_DISCOVERY: Enqueued autocam task for group '{group_dir.name}'"
                                )
                            except Exception as e:
                                logger.error(
                                    f"AUTOCAM_DISCOVERY: Could not enqueue autocam task for group '{group_dir.name}': {e}"
                                )
                        else:
                            logger.debug(
                                f"AUTOCAM_DISCOVERY: Directory {group_dir.name} skipped - status: {status}, queued: {str(group_dir) in queued_group_names}"
                            )
                    except Exception as e:
                        logger.error(
                            f"AUTOCAM_DISCOVERY: Error reading state.json for group '{group_dir.name}': {e}"
                        )
                else:
                    logger.debug(
                        f"AUTOCAM_DISCOVERY: No state.json found in {group_dir.name}"
                    )
            else:
                logger.debug(f"AUTOCAM_DISCOVERY: Skipping non-directory: {group_dir}")

        logger.info("AUTOCAM_DISCOVERY: Discovery complete")

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
                    # Autocam 3.x outputs .mkv format; the task will convert to .mp4 after
                    output_path = input_path.with_name(
                        input_path.name.replace("-raw.mp4", ".mkv")
                    )
                    return str(input_path), str(output_path)

        raise FileNotFoundError(f"No '-raw.mp4' file found in {group_dir}")
