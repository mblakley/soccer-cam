"""Base class for queue processors that process work items."""

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict

logger = logging.getLogger(__name__)


class QueueProcessor(ABC):
    """
    Base class for processors that handle work queues.
    Provides common functionality for queue management and state persistence.
    """

    def __init__(self, storage_path: str, config: Any):
        """
        Initialize the queue processor.

        Args:
            storage_path: Path to the storage directory
            config: Configuration object
        """
        self.storage_path = storage_path
        self.config = config

        self._queue = None  # Defer creation until start()
        self._queued_items = set()
        self._processor_task = None
        self._shutdown_event = asyncio.Event()

        logger.info(f"Initialized {self.__class__.__name__}")

    @abstractmethod
    def get_state_file_name(self) -> str:
        """Get the name of the state file for this processor."""
        pass

    @abstractmethod
    async def process_item(self, item: Any) -> None:
        """Process a single work item."""
        pass

    def get_item_key(self, item: Any) -> str:
        """Get a unique key for an item to prevent duplicates."""
        return str(item)

    async def add_work(self, item: Any) -> None:
        """Add work to the processor's queue."""
        # Create queue if it doesn't exist
        if self._queue is None:
            self._queue = asyncio.Queue()

        item_key = self.get_item_key(item)

        if item_key not in self._queued_items:
            await self._queue.put(item)
            self._queued_items.add(item_key)
            logger.info(f"{self.__class__.__name__}: Added item to queue: {item}")
            await self.save_state()
        else:
            logger.debug(f"{self.__class__.__name__}: Item already queued: {item}")

    async def start(self) -> None:
        """Start the queue processor."""
        logger.info(f"Starting {self.__class__.__name__}")

        # Create queue if it doesn't exist
        if self._queue is None:
            self._queue = asyncio.Queue()

        # Load state first
        await self.load_state()

        # Start the processor task
        self._processor_task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the queue processor."""
        logger.info(f"Stopping {self.__class__.__name__}")
        self._shutdown_event.set()

        # Signal queue consumer to exit by putting a sentinel value
        if self._queue is not None:
            try:
                await self._queue.put(None)  # Sentinel value
            except Exception:
                pass  # Ignore errors during shutdown

        if self._processor_task:
            # Cancel the processor task
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass

        # Clear any remaining items from the queue to prevent hanging tasks
        if self._queue is not None:
            try:
                while not self._queue.empty():
                    try:
                        self._queue.get_nowait()
                        self._queue.task_done()
                    except asyncio.QueueEmpty:
                        break
            except Exception:
                pass  # Ignore errors during cleanup

        # Clear the queued items set
        self._queued_items.clear()

        # Set queue to None to ensure clean state
        self._queue = None

    async def _run(self) -> None:
        """Main processing loop."""
        while not self._shutdown_event.is_set():
            try:
                # Create tasks for both waiting for queue items and shutdown
                queue_task = asyncio.create_task(self._queue.get())
                shutdown_task = asyncio.create_task(self._shutdown_event.wait())

                # Wait for either a queue item or shutdown signal
                done, pending = await asyncio.wait(
                    {queue_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED
                )

                # Cancel any pending tasks
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                # Check if shutdown was signaled
                if shutdown_task in done:
                    # Put the item back if we got one but are shutting down
                    if queue_task in done and not queue_task.cancelled():
                        try:
                            item = queue_task.result()
                            await self._queue.put(item)
                        except Exception:
                            pass
                    break

                # Process the queue item
                if queue_task in done and not queue_task.cancelled():
                    try:
                        item = queue_task.result()

                        # Check for sentinel value (None) to exit cleanly
                        if item is None:
                            logger.debug(
                                f"{self.__class__.__name__}: Received sentinel value, exiting"
                            )
                            break

                        await self.process_item(item)

                        # Mark as done and remove from queued items
                        self._queue.task_done()
                        item_key = self.get_item_key(item)
                        self._queued_items.discard(item_key)
                        await self.save_state()
                    except Exception as e:
                        logger.error(
                            f"{self.__class__.__name__}: Error processing item: {e}"
                        )
                        await asyncio.sleep(5)

            except Exception as e:
                logger.error(
                    f"{self.__class__.__name__}: Error in processing loop: {e}"
                )
                await asyncio.sleep(5)

    async def save_state(self) -> None:
        """Save the current queue state to disk."""
        state_file = os.path.join(self.storage_path, self.get_state_file_name())

        try:
            # Get all items currently in the queue
            items = []
            temp_queue = asyncio.Queue()

            while not self._queue.empty():
                item = await self._queue.get()
                items.append(item)
                await temp_queue.put(item)

            # Restore the queue
            self._queue = temp_queue

            # Serialize items - call serialize() directly on each item
            serialized_items = []
            for item in items:
                if hasattr(item, "serialize"):
                    serialized_items.append(item.serialize())
                elif hasattr(item, "to_dict"):
                    serialized_items.append(item.to_dict())
                else:
                    # Fallback for simple items
                    serialized_items.append({"item": str(item)})

            # Write to file
            with open(state_file, "w") as f:
                json.dump(serialized_items, f, indent=2)

            logger.debug(
                f"{self.__class__.__name__}: Saved state with {len(serialized_items)} items"
            )

        except Exception as e:
            logger.error(f"{self.__class__.__name__}: Error saving state: {e}")

    async def load_state(self) -> None:
        """Load queue state from disk."""
        state_file = os.path.join(self.storage_path, self.get_state_file_name())

        if not os.path.exists(state_file):
            logger.debug(
                f"{self.__class__.__name__}: No state file found, starting with empty queue"
            )
            return

        # For now, we skip loading state since we don't have proper deserialization
        # State files will be cleaned up on next save
        logger.debug(
            f"{self.__class__.__name__}: Skipping state loading - starting with empty queue"
        )

        # Clean up old state file
        try:
            os.remove(state_file)
        except Exception:
            pass  # Ignore errors during cleanup

    def get_queue_size(self) -> int:
        """Get the current queue size."""
        if self._queue is None:
            return 0
        return self._queue.qsize()

    def get_status(self) -> Dict[str, Any]:
        """Get processor status information."""
        return {
            "queue_size": self.get_queue_size(),
            "queued_items_count": len(self._queued_items),
            "running": self._processor_task is not None
            and not self._processor_task.done(),
        }
