"""Base class for polling processors that discover work."""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class PollingProcessor(ABC):
    """
    Base class for processors that poll for work but don't maintain their own queues.

    These processors discover work and delegate it to other processors.
    They don't need to track individual state or maintain queues.
    """

    def __init__(self, storage_path: str, config: Any, poll_interval: int = 60):
        """
        Initialize the polling processor.

        Args:
            storage_path: Path to the storage directory
            config: Configuration object
            poll_interval: How often to poll for work in seconds
        """
        self.storage_path = storage_path
        self.config = config
        self.poll_interval = poll_interval

        self._processor_task = None
        self._shutdown_event = asyncio.Event()

        logger.info(
            f"Initialized {self.__class__.__name__} with poll interval: {poll_interval}s"
        )

    @abstractmethod
    async def discover_work(self) -> None:
        """
        Discover new work that needs to be done.
        This is called periodically by the polling loop.
        """
        pass

    async def start(self) -> None:
        """Start the polling processor."""
        logger.info(f"Starting {self.__class__.__name__}")

        # Start the processor task
        self._processor_task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the polling processor."""
        logger.info(f"Stopping {self.__class__.__name__}")
        self._shutdown_event.set()

        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        """Main polling loop."""
        while not self._shutdown_event.is_set():
            try:
                await self.discover_work()
                await asyncio.sleep(self.poll_interval)
            except Exception as e:
                logger.error(f"{self.__class__.__name__}: Error in polling loop: {e}")
                await asyncio.sleep(self.poll_interval)
