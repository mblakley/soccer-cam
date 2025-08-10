"""Base class for queue processors that process work items."""

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Dict

from .queue_type import QueueType
from video_grouper.utils.config import Config
from video_grouper.task_processors.tasks.base_task import BaseTask
from .task_registry import task_registry

logger = logging.getLogger(__name__)


class QueueProcessor(ABC):
    """
    Base class for processors that handle work queues.
    Provides common functionality for queue management and state persistence.
    """

    def __init__(self, storage_path: str, config: Config):
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
        self._max_retries = 3  # Maximum number of retry attempts
        self._retry_counts = {}  # Track retry counts for each item

        logger.info(f"Initialized {self.__class__.__name__}")

    @property
    @abstractmethod
    def queue_type(self) -> QueueType:
        """Return the queue type for this processor."""
        pass

    def get_state_file_name(self) -> str:
        """Get the name of the state file for this processor."""
        return f"{self.queue_type.value}_queue_state.json"

    @abstractmethod
    async def process_item(self, item: BaseTask) -> None:
        """Process a single work item."""
        pass

    def get_item_key(self, item: BaseTask) -> str:
        """Get a unique key for an item to prevent duplicates."""
        return str(item)

    async def add_work(self, item: BaseTask) -> None:
        """Add work to the processor's queue."""
        # Create queue if it doesn't exist
        if self._queue is None:
            self._queue = asyncio.Queue()

        item_key = self.get_item_key(item)

        if item_key not in self._queued_items:
            # Ensure the task knows the storage path for later execution.
            # Many task classes rely on `self.storage_path` being set at runtime
            # rather than during construction.
            if not hasattr(item, "storage_path"):
                setattr(item, "storage_path", self.storage_path)

            await self._queue.put(item)
            self._queued_items.add(item_key)
            queue_size = self._queue.qsize()
            logger.info(
                f"{self.__class__.__name__}: Added item to queue: {item} (queue size: {queue_size})"
            )

            # Always persist state immediately
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
        logger.info(f"{self.__class__.__name__}: Starting processing loop")

        while not self._shutdown_event.is_set():
            try:
                # Get the next item from the queue
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    # Timeout - check if we should continue or exit
                    if self._shutdown_event.is_set():
                        logger.info(
                            f"{self.__class__.__name__}: Shutdown signaled, exiting processing loop"
                        )
                        break
                    continue

                # Generate a unique trace ID for this processing attempt
                import uuid

                trace_id = str(uuid.uuid4())[:8]
                logger.info(
                    f"{self.__class__.__name__}: Processing item: {item} [trace_id: {trace_id}]"
                )

                try:
                    # Process the item
                    await self.process_item(item)
                    logger.info(
                        f"{self.__class__.__name__}: Successfully completed processing item: {item} [trace_id: {trace_id}]"
                    )
                    # Task succeeded - remove from queue
                    self._queue.task_done()
                    item_key = self.get_item_key(item)
                    self._queued_items.discard(item_key)
                    self._retry_counts.pop(
                        item_key, None
                    )  # Clear retry count on success
                    queue_size = self._queue.qsize()
                    await self.save_state()
                    logger.info(
                        f"{self.__class__.__name__}: Removed completed item from queue: {item} (queue size: {queue_size})"
                    )
                except Exception as e:
                    logger.error(
                        f"{self.__class__.__name__}: Failed to process item {item}: {e} [trace_id: {trace_id}]"
                    )

                    # Check retry count
                    item_key = self.get_item_key(item)
                    retry_count = self._retry_counts.get(item_key, 0)

                    if retry_count < self._max_retries:
                        # Task failed but can be retried - requeue at the end
                        self._queue.task_done()  # Mark current item as done
                        await self._queue.put(item)  # Requeue at the end
                        self._retry_counts[item_key] = retry_count + 1
                        queue_size = self._queue.qsize()
                        logger.info(
                            f"{self.__class__.__name__}: Requeued failed item at end of queue (attempt {retry_count + 1}/{self._max_retries}): {item} (queue size: {queue_size})"
                        )
                        await self.save_state()
                    else:
                        # Task failed and exceeded max retries - remove from queue
                        self._queue.task_done()
                        self._queued_items.discard(item_key)
                        self._retry_counts.pop(item_key, None)
                        queue_size = self._queue.qsize()
                        logger.error(
                            f"{self.__class__.__name__}: Item exceeded max retries ({self._max_retries}), removing from queue: {item} (queue size: {queue_size})"
                        )
                        await self.save_state()

            except Exception as e:
                logger.error(
                    f"{self.__class__.__name__}: Error in processing loop: {e}"
                )
                await asyncio.sleep(5)

        logger.info(f"{self.__class__.__name__}: Processing loop ended")

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

            logger.info(
                f"{self.__class__.__name__}: Saved state with {len(serialized_items)} items to {state_file}"
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

        try:
            with open(state_file, "r") as f:
                serialized_items = json.load(f)

            logger.info(
                f"{self.__class__.__name__}: Loading {len(serialized_items)} items from state"
            )

            # Restore items to the queue
            restored_count = 0
            skipped_count = 0
            for item_data in serialized_items:
                try:
                    # Skip legacy format items
                    if "item" in item_data and item_data["item"] == "None":
                        skipped_count += 1
                        continue

                    # Deserialize the task
                    task = self._deserialize_task(item_data)
                    if task:
                        # Add to queue and track
                        await self._queue.put(task)
                        item_key = self.get_item_key(task)
                        self._queued_items.add(item_key)
                        restored_count += 1
                        logger.debug(
                            f"{self.__class__.__name__}: Restored task to queue: {task}"
                        )
                    else:
                        # Task deserialization returned None (e.g., missing task_type)
                        skipped_count += 1
                        logger.debug(
                            f"{self.__class__.__name__}: Skipped invalid task data: {item_data}"
                        )
                except Exception as e:
                    logger.error(
                        f"{self.__class__.__name__}: Failed to deserialize task {item_data}: {e}"
                    )
                    skipped_count += 1

            logger.info(
                f"{self.__class__.__name__}: Successfully restored {restored_count} items to queue, skipped {skipped_count} invalid items"
            )

        except Exception as e:
            logger.error(f"{self.__class__.__name__}: Error loading state: {e}")
            # Don't delete the state file on error - let the user decide what to do

    def get_queue_size(self) -> int:
        """Get the current queue size."""
        if self._queue is None:
            return 0
        return self._queue.qsize()

    def get_status(self) -> Dict[str, object]:
        """Get processor status information."""
        return {
            "queue_size": self.get_queue_size(),
            "queued_items_count": len(self._queued_items),
            "running": self._processor_task is not None
            and not self._processor_task.done(),
        }

    def update_in_place(self, item_key: str, new_task: BaseTask) -> None:
        """Update a queued item in place, preserving its position in the queue."""
        if self._queue is None:
            return
        # Access the underlying deque of the asyncio.Queue
        queue_list = list(self._queue._queue)
        for idx, item in enumerate(queue_list):
            if self.get_item_key(item) == item_key:
                queue_list[idx] = new_task
                break
        # Rebuild the queue
        self._queue._queue.clear()
        for item in queue_list:
            self._queue._queue.append(item)

    def _deserialize_task(self, item_data: Dict[str, object]) -> BaseTask:
        """
        Deserialize a task from its serialized data.

        Args:
            item_data: Dictionary containing serialized task data

        Returns:
            Deserialized task instance, or None if deserialization failed
        """
        # Use the task registry to deserialize the task
        return task_registry.deserialize_task(item_data, self.queue_type)
