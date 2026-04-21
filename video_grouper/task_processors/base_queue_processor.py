"""Base class for queue processors that process work items."""

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Dict, Optional

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
        self._in_progress_item: Optional[BaseTask] = None  # Currently processing item
        self._sequence = 0
        self._items_by_key: dict[str, tuple[int, int, BaseTask]] = {}

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

    def _inject_storage_path(self, item: BaseTask) -> None:
        """Ensure the task knows the storage path and config for later execution."""
        if not hasattr(item, "storage_path"):
            setattr(item, "storage_path", self.storage_path)
        if not hasattr(item, "config"):
            setattr(item, "config", self.config)

    def _get_priority(self, item: BaseTask) -> int:
        """Return priority for this item. Lower = higher priority. Default: 2 (normal)."""
        return 2

    async def add_work(self, item: BaseTask, priority: int | None = None) -> None:
        """Add work to the processor's queue."""
        # Create queue if it doesn't exist
        if self._queue is None:
            self._queue = asyncio.PriorityQueue()

        item_key = self.get_item_key(item)

        if item_key not in self._queued_items:
            self._inject_storage_path(item)

            if priority is None:
                priority = self._get_priority(item)
            seq = self._sequence
            self._sequence += 1
            await self._queue.put((priority, seq, item))
            self._items_by_key[item_key] = (priority, seq, item)
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
            self._queue = asyncio.PriorityQueue()

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

        # Clear the queued items set and items_by_key
        self._queued_items.clear()
        self._items_by_key.clear()

        # Set queue to None to ensure clean state
        self._queue = None

    async def _run(self) -> None:
        """Main processing loop."""
        logger.info(f"{self.__class__.__name__}: Starting processing loop")

        while not self._shutdown_event.is_set():
            try:
                # Get the next item from the queue
                try:
                    priority, seq, item = await asyncio.wait_for(
                        self._queue.get(), timeout=5.0
                    )
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

                # Track in-progress item so it's persisted if we crash.
                # Remove from _items_by_key since it's no longer "queued".
                item_key = self.get_item_key(item)
                self._items_by_key.pop(item_key, None)
                self._in_progress_item = item
                await self.save_state()

                try:
                    # Process the item
                    await self.process_item(item)
                    logger.info(
                        f"{self.__class__.__name__}: Successfully completed processing item: {item} [trace_id: {trace_id}]"
                    )
                    # Task succeeded - remove from queue
                    self._queue.task_done()
                    self._queued_items.discard(item_key)
                    self._items_by_key.pop(item_key, None)
                    self._retry_counts.pop(
                        item_key, None
                    )  # Clear retry count on success
                    self._in_progress_item = None
                    queue_size = self._queue.qsize()
                    await self.save_state()
                    logger.info(
                        f"{self.__class__.__name__}: Removed completed item from queue: {item} (queue size: {queue_size})"
                    )
                except Exception as e:
                    from video_grouper.utils.youtube_upload import YouTubeQuotaError

                    if isinstance(e, YouTubeQuotaError):
                        # Quota errors should not be retried immediately.
                        # Wait until the quota resets (YouTube daily quotas
                        # reset at midnight Pacific Time) then retry.
                        self._queue.task_done()
                        self._in_progress_item = None

                        # Calculate wait: next midnight PT + 5 min buffer
                        from datetime import datetime, timedelta
                        import pytz

                        now_pt = datetime.now(pytz.timezone("US/Pacific"))
                        tomorrow_pt = (now_pt + timedelta(days=1)).replace(
                            hour=0, minute=5, second=0, microsecond=0
                        )
                        wait_seconds = (tomorrow_pt - now_pt).total_seconds()
                        wait_hours = wait_seconds / 3600

                        logger.warning(
                            f"{self.__class__.__name__}: YouTube quota exceeded for {item}. "
                            f"Waiting {wait_hours:.1f}h until quota resets "
                            f"(~{tomorrow_pt.strftime('%I:%M %p PT')}). [trace_id: {trace_id}]"
                        )

                        # Sleep until quota resets, then requeue
                        await asyncio.sleep(wait_seconds)
                        new_seq = self._sequence
                        self._sequence += 1
                        await self._queue.put((priority, new_seq, item))
                        self._items_by_key[item_key] = (priority, new_seq, item)
                        self._retry_counts.pop(item_key, None)
                        await self.save_state()
                        logger.info(
                            f"{self.__class__.__name__}: Quota reset, requeued {item} for upload."
                        )
                        continue

                    logger.error(
                        f"{self.__class__.__name__}: Failed to process item {item}: {e} [trace_id: {trace_id}]"
                    )

                    # Check retry count
                    retry_count = self._retry_counts.get(item_key, 0)

                    if retry_count < self._max_retries:
                        # Task failed but can be retried - requeue with low priority
                        self._queue.task_done()
                        self._in_progress_item = None
                        new_seq = self._sequence
                        self._sequence += 1
                        await self._queue.put((3, new_seq, item))
                        self._items_by_key[item_key] = (3, new_seq, item)
                        self._retry_counts[item_key] = retry_count + 1
                        queue_size = self._queue.qsize()
                        logger.info(
                            f"{self.__class__.__name__}: Requeued failed item at end of queue (attempt {retry_count + 1}/{self._max_retries}): {item} (queue size: {queue_size})"
                        )
                        await self.save_state()
                    else:
                        # Task failed and exceeded max retries - remove from queue
                        self._queue.task_done()
                        self._in_progress_item = None
                        self._queued_items.discard(item_key)
                        self._items_by_key.pop(item_key, None)
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

    def _serialize_item(self, item: BaseTask) -> dict:
        """Serialize a single task item to a dictionary."""
        if hasattr(item, "serialize"):
            return item.serialize()
        elif hasattr(item, "to_dict"):
            return item.to_dict()
        else:
            return {"item": str(item)}

    async def save_state(self) -> None:
        """Save the current queue state to disk, including any in-progress item."""
        state_file = os.path.join(self.storage_path, self.get_state_file_name())

        try:
            # Build sorted snapshot from _items_by_key (the canonical state).
            items = sorted(self._items_by_key.values(), key=lambda x: (x[0], x[1]))

            # Serialize items with priority metadata
            serialized_items = [
                {"priority": pri, "seq": seq, **self._serialize_item(task)}
                for pri, seq, task in items
            ]

            # Build state with in-progress item tracking
            state = {
                "queue": serialized_items,
            }
            if self._in_progress_item is not None:
                state["in_progress"] = self._serialize_item(self._in_progress_item)

            # Atomic write: write to temp file then rename
            temp_file = state_file + ".tmp"
            with open(temp_file, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(temp_file, state_file)

            total_items = len(serialized_items) + (1 if self._in_progress_item else 0)
            logger.info(
                f"{self.__class__.__name__}: Saved state with {total_items} items ({len(serialized_items)} queued, {'1 in-progress' if self._in_progress_item else '0 in-progress'}) to {state_file}"
            )

        except Exception as e:
            logger.error(f"{self.__class__.__name__}: Error saving state: {e}")

    async def load_state(self) -> None:
        """Load queue state from disk, including any in-progress item."""
        state_file = os.path.join(self.storage_path, self.get_state_file_name())

        if not os.path.exists(state_file):
            logger.debug(
                f"{self.__class__.__name__}: No state file found, starting with empty queue"
            )
            return

        try:
            with open(state_file, "r") as f:
                raw_state = json.load(f)

            # Support both new format (dict with "queue" key) and legacy format (plain list)
            if isinstance(raw_state, dict):
                serialized_items = raw_state.get("queue", [])
                in_progress_data = raw_state.get("in_progress")
            else:
                serialized_items = raw_state
                in_progress_data = None

            logger.info(
                f"{self.__class__.__name__}: Loading {len(serialized_items)} queued items from state"
                + (", plus 1 in-progress item" if in_progress_data else "")
            )

            # Restore in-progress item first (at front of queue for re-processing)
            restored_count = 0
            skipped_count = 0
            if in_progress_data:
                try:
                    task = self._deserialize_task(in_progress_data)
                    if task:
                        self._inject_storage_path(task)
                        seq = self._sequence
                        self._sequence += 1
                        await self._queue.put((0, seq, task))
                        item_key = self.get_item_key(task)
                        self._items_by_key[item_key] = (0, seq, task)
                        self._queued_items.add(item_key)
                        restored_count += 1
                        logger.info(
                            f"{self.__class__.__name__}: Restored in-progress task to front of queue (priority 0): {task}"
                        )
                except Exception as e:
                    logger.error(
                        f"{self.__class__.__name__}: Failed to restore in-progress task: {e}"
                    )

            # Restore queued items
            for item_data in serialized_items:
                try:
                    # Skip legacy format items
                    if "item" in item_data and item_data["item"] == "None":
                        skipped_count += 1
                        continue

                    # Extract priority and seq before deserializing
                    priority = item_data.pop("priority", 2)
                    seq = item_data.pop("seq", self._sequence)
                    self._sequence = max(self._sequence, seq + 1)

                    # Deserialize the task
                    task = self._deserialize_task(item_data)
                    if task:
                        self._inject_storage_path(task)
                        # Add to queue and track
                        await self._queue.put((priority, seq, task))
                        item_key = self.get_item_key(task)
                        self._items_by_key[item_key] = (priority, seq, task)
                        self._queued_items.add(item_key)
                        restored_count += 1
                        logger.debug(
                            f"{self.__class__.__name__}: Restored task to queue: {task}"
                        )
                    else:
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
        """Update a queued item in place, preserving its priority and sequence.

        Replaces the task in _items_by_key and rebuilds the PriorityQueue.
        This is safe because asyncio is single-threaded and this method is
        only called from coroutines that are not currently yielded inside
        Queue.get/put.
        """
        if item_key not in self._items_by_key:
            return
        pri, seq, _old_task = self._items_by_key[item_key]
        self._items_by_key[item_key] = (pri, seq, new_task)
        self._rebuild_queue()

    def _rebuild_queue(self):
        """Reconstruct the PriorityQueue from _items_by_key."""
        self._queue = asyncio.PriorityQueue()
        for pri, seq, task in sorted(self._items_by_key.values()):
            self._queue.put_nowait((pri, seq, task))

    def get_queued_items(self) -> list[BaseTask]:
        """Return a snapshot of all queued items (for inspection, not modification)."""
        return [task for _, _, task in sorted(self._items_by_key.values())]

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
