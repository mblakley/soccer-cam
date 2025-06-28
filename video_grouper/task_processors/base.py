import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
import aiofiles

logger = logging.getLogger(__name__)

class TaskProcessor(ABC):
    """Base class for all task processors."""
    
    def __init__(self, storage_path: str, config: Any, poll_interval: int = 60):
        """
        Initialize the task processor.
        
        Args:
            storage_path: Path to the shared data directory
            config: Configuration object
            poll_interval: How often to poll for work (in seconds)
        """
        self.storage_path = storage_path
        self.config = config
        self.poll_interval = poll_interval
        self.queue = asyncio.Queue()
        self.queued_items = set()
        self._shutdown_event = asyncio.Event()
        self._processor_task = None
        
        # Each processor has its own state file
        self.state_file_name = self.get_state_file_name()
        self.state_file_path = os.path.join(storage_path, self.state_file_name)
        
        logger.info(f"Initialized {self.__class__.__name__} with state file: {self.state_file_name}")
    
    @abstractmethod
    def get_state_file_name(self) -> str:
        """Return the name of the state file for this processor."""
        pass
    
    @abstractmethod
    async def process_item(self, item: Any) -> bool:
        """
        Process a single item from the queue.
        
        Args:
            item: The item to process
            
        Returns:
            True if processing was successful, False otherwise
        """
        pass
    
    @abstractmethod
    async def discover_work(self) -> None:
        """
        Discover new work that needs to be done and add it to the queue.
        This is called periodically by the polling loop.
        """
        pass
    
    async def add_to_queue(self, item: Any) -> None:
        """Add an item to the processing queue if it's not already queued."""
        item_key = self.get_item_key(item)
        if item_key not in self.queued_items:
            await self.queue.put(item)
            self.queued_items.add(item_key)
            logger.info(f"{self.__class__.__name__}: Added to queue: {item_key}")
            await self.save_state()
        else:
            logger.debug(f"{self.__class__.__name__}: Item already queued: {item_key}")
    
    def get_item_key(self, item: Any) -> str:
        """
        Get a unique key for the item to track if it's already queued.
        Override this in subclasses if needed.
        """
        if hasattr(item, 'file_path'):
            return item.file_path
        elif hasattr(item, 'item_path'):
            return item.item_path
        else:
            return str(item)
    
    async def save_state(self) -> None:
        """Save the current queue state to file."""
        try:
            # Drain the queue to get all items
            items = []
            while not self.queue.empty():
                items.append(await self.queue.get())
            
            # Serialize items
            serialized_items = []
            for item in items:
                if hasattr(item, 'to_dict'):
                    serialized_items.append(item.to_dict())
                else:
                    serialized_items.append(self.serialize_item(item))
            
            # Save to file
            async with aiofiles.open(self.state_file_path, 'w') as f:
                await f.write(json.dumps(serialized_items, indent=2))
            
            # Restore items to queue
            for item in items:
                await self.queue.put(item)
                
            logger.debug(f"{self.__class__.__name__}: Saved state with {len(items)} items")
            
        except Exception as e:
            logger.error(f"{self.__class__.__name__}: Failed to save state: {e}")
    
    def serialize_item(self, item: Any) -> Dict[str, Any]:
        """
        Serialize an item for state persistence.
        Override this in subclasses for custom serialization.
        """
        if hasattr(item, '__dict__'):
            return item.__dict__
        else:
            return {"item": str(item)}
    
    async def load_state(self) -> None:
        """Load the queue state from file."""
        if not os.path.exists(self.state_file_path):
            logger.info(f"{self.__class__.__name__}: No state file found, starting fresh")
            return
            
        try:
            async with aiofiles.open(self.state_file_path, 'r') as f:
                content = await f.read()
                if not content.strip():
                    logger.info(f"{self.__class__.__name__}: State file is empty")
                    return
                
                items_data = json.loads(content)
                
            for item_data in items_data:
                item = self.deserialize_item(item_data)
                if item:
                    await self.add_to_queue(item)
                    
            logger.info(f"{self.__class__.__name__}: Loaded {len(items_data)} items from state")
            
        except Exception as e:
            logger.error(f"{self.__class__.__name__}: Failed to load state: {e}")
    
    @abstractmethod
    def deserialize_item(self, item_data: Dict[str, Any]) -> Optional[Any]:
        """
        Deserialize an item from state data.
        Override this in subclasses.
        """
        pass
    
    async def start(self) -> None:
        """Start the task processor."""
        logger.info(f"Starting {self.__class__.__name__}")
        await self.load_state()
        
        # Start the processor task
        self._processor_task = asyncio.create_task(self._run())
    
    async def stop(self) -> None:
        """Stop the task processor."""
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
        # Start both the queue processor and work discovery
        tasks = [
            asyncio.create_task(self._process_queue()),
            asyncio.create_task(self._discover_work_loop())
        ]
        
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info(f"{self.__class__.__name__}: Processing loop cancelled")
            for task in tasks:
                if not task.done():
                    task.cancel()
    
    async def _process_queue(self) -> None:
        """Process items from the queue."""
        while not self._shutdown_event.is_set():
            try:
                # Wait for an item with a timeout so we can check shutdown
                try:
                    item = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                
                # Process the item
                success = await self.process_item(item)
                
                # Remove from queued set and save state
                item_key = self.get_item_key(item)
                self.queued_items.discard(item_key)
                await self.save_state()
                
                # Mark task as done
                self.queue.task_done()
                
                if success:
                    logger.info(f"{self.__class__.__name__}: Successfully processed {item_key}")
                else:
                    logger.error(f"{self.__class__.__name__}: Failed to process {item_key}")
                    
            except Exception as e:
                logger.error(f"{self.__class__.__name__}: Error in queue processing: {e}")
                await asyncio.sleep(1)
    
    async def _discover_work_loop(self) -> None:
        """Periodically discover new work."""
        while not self._shutdown_event.is_set():
            try:
                await self.discover_work()
                await asyncio.sleep(self.poll_interval)
            except Exception as e:
                logger.error(f"{self.__class__.__name__}: Error in work discovery: {e}")
                await asyncio.sleep(self.poll_interval) 