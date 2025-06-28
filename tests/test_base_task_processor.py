"""Tests for the base TaskProcessor class."""

import os
import json
import asyncio
import tempfile
import configparser
from unittest.mock import Mock, AsyncMock
import pytest

from video_grouper.task_processors.base import TaskProcessor


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def mock_config():
    """Create a mock configuration object."""
    config = configparser.ConfigParser()
    config.add_section('APP')
    config.set('APP', 'check_interval_seconds', '10')
    return config


class MockTaskProcessor(TaskProcessor):
    """Mock implementation of TaskProcessor for testing."""
    
    def get_state_file_name(self) -> str:
        return "mock_queue_state.json"
    
    async def process_item(self, item) -> bool:
        # Simulate some work
        await asyncio.sleep(0.01)
        return item != "fail_item"
    
    def deserialize_item(self, item_data):
        if isinstance(item_data, dict):
            if 'value' in item_data:
                return item_data['value']
            elif 'item' in item_data:
                return item_data['item']
        return None
    
    async def discover_work(self) -> None:
        # Simulate discovering work
        if hasattr(self, '_test_work_items'):
            for item in self._test_work_items:
                await self.add_to_queue(item)
            self._test_work_items = []


class TestTaskProcessor:
    """Test the base TaskProcessor class."""
    
    @pytest.mark.asyncio
    async def test_processor_initialization(self, temp_storage, mock_config):
        """Test processor initialization."""
        processor = MockTaskProcessor(temp_storage, mock_config, poll_interval=5)
        
        assert processor.storage_path == temp_storage
        assert processor.config == mock_config
        assert processor.poll_interval == 5
        assert processor.queue.qsize() == 0
        assert processor._processor_task is None
        assert not processor._shutdown_event.is_set()
    
    @pytest.mark.asyncio
    async def test_processor_lifecycle(self, temp_storage, mock_config):
        """Test processor start/stop lifecycle."""
        processor = MockTaskProcessor(temp_storage, mock_config, poll_interval=1)
        
        # Should start successfully
        await processor.start()
        assert processor._processor_task is not None
        assert not processor._processor_task.done()
        
        # Give it a moment to start processing
        await asyncio.sleep(0.1)
        
        # Should stop successfully
        await processor.stop()
        assert processor._processor_task.done()
        assert processor._shutdown_event.is_set()
    
    @pytest.mark.asyncio
    async def test_queue_operations(self, temp_storage, mock_config):
        """Test adding items to queue."""
        processor = MockTaskProcessor(temp_storage, mock_config)
        
        # Add items to queue
        await processor.add_to_queue("item1")
        await processor.add_to_queue("item2")
        
        assert processor.queue.qsize() == 2
        
        # Items should be retrievable
        item1 = await processor.queue.get()
        item2 = await processor.queue.get()
        
        assert item1 == "item1"
        assert item2 == "item2"
        assert processor.queue.qsize() == 0
    
    @pytest.mark.asyncio
    async def test_state_persistence(self, temp_storage, mock_config):
        """Test saving and loading state."""
        processor = MockTaskProcessor(temp_storage, mock_config)
        
        # Add items to queue
        await processor.add_to_queue("item1")
        await processor.add_to_queue("item2")
        
        # Save state
        await processor.save_state()
        
        # Verify state file was created
        state_file = os.path.join(temp_storage, "mock_queue_state.json")
        assert os.path.exists(state_file)
        
        # Verify state content
        with open(state_file, 'r') as f:
            state_data = json.load(f)
        assert len(state_data) == 2
        assert state_data[0]["item"] == "item1"
        assert state_data[1]["item"] == "item2"
    
    @pytest.mark.asyncio
    async def test_state_loading(self, temp_storage, mock_config):
        """Test loading state from file."""
        processor = MockTaskProcessor(temp_storage, mock_config)
        
        # Create a state file with test data
        state_file = os.path.join(temp_storage, "mock_queue_state.json")
        test_data = [{"value": "loaded_item1"}, {"value": "loaded_item2"}]
        
        with open(state_file, 'w') as f:
            json.dump(test_data, f)
        
        # Load state
        await processor.load_state()
        
        assert processor.queue.qsize() == 2
        
        # Verify items were properly deserialized
        item1 = await processor.queue.get()
        item2 = await processor.queue.get()
        assert item1 == "loaded_item1"
        assert item2 == "loaded_item2"
    
    @pytest.mark.asyncio
    async def test_state_loading_invalid_data(self, temp_storage, mock_config):
        """Test loading state with invalid data."""
        processor = MockTaskProcessor(temp_storage, mock_config)
        
        # Create a state file with invalid data
        state_file = os.path.join(temp_storage, "mock_queue_state.json")
        test_data = [{"invalid": "data"}, {"value": "valid_item"}]
        
        with open(state_file, 'w') as f:
            json.dump(test_data, f)
        
        # Load state - should handle invalid data gracefully
        await processor.load_state()
        
        # Only valid item should be loaded
        assert processor.queue.qsize() == 1
        item = await processor.queue.get()
        assert item == "valid_item"
    
    @pytest.mark.asyncio
    async def test_processing_loop(self, temp_storage, mock_config):
        """Test the main processing loop."""
        processor = MockTaskProcessor(temp_storage, mock_config, poll_interval=0.1)
        
        # Add some work items
        processor._test_work_items = ["work1", "work2"]
        
        # Start processor
        await processor.start()
        
        # Give it time to discover and process work
        await asyncio.sleep(0.2)
        
        # Stop processor
        await processor.stop()
        
        # Queue should be empty (work was processed)
        assert processor.queue.qsize() == 0
    
    @pytest.mark.asyncio
    async def test_processing_failure_handling(self, temp_storage, mock_config):
        """Test handling of processing failures."""
        processor = MockTaskProcessor(temp_storage, mock_config, poll_interval=0.1)
        
        # Add items that will succeed and fail
        await processor.add_to_queue("success_item")
        await processor.add_to_queue("fail_item")
        await processor.add_to_queue("another_success")
        
        # Start processor
        await processor.start()
        
        # Give it time to process
        await asyncio.sleep(0.2)
        
        # Stop processor
        await processor.stop()
        
        # All items should have been processed (even failed ones)
        assert processor.queue.qsize() == 0
    
    @pytest.mark.asyncio
    async def test_duplicate_item_prevention(self, temp_storage, mock_config):
        """Test that duplicate items are not added to queue."""
        processor = MockTaskProcessor(temp_storage, mock_config)
        
        # Add the same item multiple times
        await processor.add_to_queue("duplicate_item")
        await processor.add_to_queue("duplicate_item")
        await processor.add_to_queue("unique_item")
        await processor.add_to_queue("duplicate_item")
        
        # Should only have 2 items (one duplicate, one unique)
        assert processor.queue.qsize() == 2
        
        items = []
        while not processor.queue.empty():
            items.append(await processor.queue.get())
        
        assert "duplicate_item" in items
        assert "unique_item" in items
        assert len(items) == 2
    
    @pytest.mark.asyncio
    async def test_shutdown_during_processing(self, temp_storage, mock_config):
        """Test shutdown while items are being processed."""
        processor = MockTaskProcessor(temp_storage, mock_config, poll_interval=0.1)
        
        # Add many items to process
        for i in range(10):
            await processor.add_to_queue(f"item_{i}")
        
        # Start processor
        await processor.start()
        
        # Let it start processing
        await asyncio.sleep(0.05)
        
        # Stop processor while it's working
        await processor.stop()
        
        # Should stop gracefully
        assert processor._processor_task.done()
        assert processor._shutdown_event.is_set() 