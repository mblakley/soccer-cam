"""Base class for queue processors that process work items."""

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class QueueProcessor(ABC):
    """
    Base class for processors that maintain queues and process work items.
    
    These processors are given work to do and need to track their own state.
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
        
        self._queue = asyncio.Queue()
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
    
    @abstractmethod
    def serialize_item(self, item: Any) -> Dict[str, Any]:
        """Serialize an item for state persistence."""
        pass
    
    @abstractmethod
    def deserialize_item(self, data: Dict[str, Any]) -> Any:
        """Deserialize an item from state data."""
        pass
    
    def get_item_key(self, item: Any) -> str:
        """Get a unique key for an item to prevent duplicates."""
        return str(item)
    
    async def add_work(self, item: Any) -> None:
        """Add work to the processor's queue."""
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
        
        # Load state first
        await self.load_state()
        
        # Start the processor task
        self._processor_task = asyncio.create_task(self._run())
    
    async def stop(self) -> None:
        """Stop the queue processor."""
        logger.info(f"Stopping {self.__class__.__name__}")
        self._shutdown_event.set()
        
        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass
    
    async def _run(self) -> None:
        """Main processing loop."""
        while not self._shutdown_event.is_set():
            try:
                # Create tasks for both waiting for queue items and shutdown
                queue_task = asyncio.create_task(self._queue.get())
                shutdown_task = asyncio.create_task(self._shutdown_event.wait())
                
                # Wait for either a queue item or shutdown signal
                done, pending = await asyncio.wait(
                    {queue_task, shutdown_task},
                    return_when=asyncio.FIRST_COMPLETED
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
                        await self.process_item(item)
                        
                        # Mark as done and remove from queued items
                        self._queue.task_done()
                        item_key = self.get_item_key(item)
                        self._queued_items.discard(item_key)
                        await self.save_state()
                    except Exception as e:
                        logger.error(f"{self.__class__.__name__}: Error processing item: {e}")
                        await asyncio.sleep(5)
                
            except Exception as e:
                logger.error(f"{self.__class__.__name__}: Error in processing loop: {e}")
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
            
            # Serialize items
            serialized_items = [self.serialize_item(item) for item in items]
            
            # Write to file
            with open(state_file, 'w') as f:
                json.dump(serialized_items, f, indent=2)
                
            logger.debug(f"{self.__class__.__name__}: Saved state with {len(serialized_items)} items")
            
        except Exception as e:
            logger.error(f"{self.__class__.__name__}: Error saving state: {e}")
    
    async def load_state(self) -> None:
        """Load queue state from disk."""
        state_file = os.path.join(self.storage_path, self.get_state_file_name())
        
        if not os.path.exists(state_file):
            logger.debug(f"{self.__class__.__name__}: No state file found, starting with empty queue")
            return
        
        try:
            with open(state_file, 'r') as f:
                content = f.read().strip()
                if not content:
                    logger.debug(f"{self.__class__.__name__}: State file is empty")
                    return
                
                serialized_items = json.loads(content)
            
            if not isinstance(serialized_items, list):
                logger.error(f"{self.__class__.__name__}: State file is not a list, ignoring")
                return
            
            # Deserialize and add items to queue
            for item_data in serialized_items:
                try:
                    item = self.deserialize_item(item_data)
                    if item:
                        await self.add_work(item)
                except Exception as e:
                    logger.warning(f"{self.__class__.__name__}: Error deserializing item {item_data}: {e}")
            
            logger.info(f"{self.__class__.__name__}: Loaded {len(serialized_items)} items from state")
            
        except Exception as e:
            logger.error(f"{self.__class__.__name__}: Error loading state: {e}")
    
    def get_queue_size(self) -> int:
        """Get the current queue size."""
        return self._queue.qsize()
    
    def get_status(self) -> Dict[str, Any]:
        """Get processor status information."""
        return {
            "queue_size": self.get_queue_size(),
            "queued_items_count": len(self._queued_items),
            "running": self._processor_task is not None and not self._processor_task.done()
        } 