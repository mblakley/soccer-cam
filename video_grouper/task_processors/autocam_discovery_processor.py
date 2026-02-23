"""
Autocam Discovery Processor for finding trimmed directories and queuing autocam tasks.
"""

import logging
import json
from pathlib import Path

from .base_polling_processor import PollingProcessor
from .tasks.autocam import AutocamTask
from .autocam_utils import get_autocam_input_output_paths
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

        # Get all currently queued group names (read-only peek via internal deque)
        queued_group_names = set()
        if self.autocam_processor._queue is not None:
            for item in list(self.autocam_processor._queue._queue):
                if hasattr(item, "group_dir"):
                    queued_group_names.add(str(item.group_dir))

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
        """Find the raw video file and determine the autocam output path."""
        # Autocam 3.x outputs .mkv format; the task will convert to .mp4 after
        return get_autocam_input_output_paths(group_dir, output_ext="mkv")
