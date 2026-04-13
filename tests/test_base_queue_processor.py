"""Tests for QueueProcessor in-progress item persistence."""

import asyncio
import json
import os

import pytest

from video_grouper.task_processors.base_queue_processor import QueueProcessor
from video_grouper.task_processors.queue_type import QueueType
from video_grouper.task_processors.tasks.base_task import BaseTask


# ---------------------------------------------------------------------------
# Minimal concrete implementations for testing
# ---------------------------------------------------------------------------


class StubTask(BaseTask):
    """Minimal concrete task for testing."""

    def __init__(self, name: str):
        self.name = name

    @classmethod
    def queue_type(cls) -> QueueType:
        return QueueType.VIDEO

    @property
    def task_type(self) -> str:
        return "stub"

    def get_item_path(self) -> str:
        return self.name

    def serialize(self):
        return {"task_type": "stub", "name": self.name}

    @classmethod
    def deserialize(cls, data):
        return cls(name=data["name"])

    async def execute(self) -> bool:
        return True

    def __str__(self):
        return f"StubTask({self.name})"


class StubQueueProcessor(QueueProcessor):
    """Concrete QueueProcessor for testing."""

    def __init__(self, storage_path, config):
        super().__init__(storage_path, config)
        self.processed_items = []
        self.fail_next = False  # Set to True to make process_item raise

    @property
    def queue_type(self) -> QueueType:
        return QueueType.VIDEO

    async def process_item(self, item):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("Simulated failure")
        self.processed_items.append(item)

    def _deserialize_task(self, item_data):
        if item_data.get("task_type") == "stub":
            return StubTask.deserialize(item_data)
        return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestQueueProcessorInProgressPersistence:
    """Tests for the in-progress item persistence fix."""

    @pytest.mark.asyncio
    async def test_in_progress_item_persisted_during_processing(
        self, tmp_path, mock_config
    ):
        """While an item is being processed, it should appear in the saved state."""
        processor = StubQueueProcessor(str(tmp_path), mock_config)
        processor._queue = asyncio.PriorityQueue()

        # Use an event to pause processing mid-flight
        processing_started = asyncio.Event()
        processing_continue = asyncio.Event()

        original_process = processor.process_item

        async def pausing_process(item):
            processing_started.set()
            await processing_continue.wait()
            await original_process(item)

        processor.process_item = pausing_process

        task = StubTask("test-item")
        await processor.add_work(task)

        # Start the processing loop
        run_task = asyncio.create_task(processor._run())

        # Wait for item to be pulled from queue and processing to start
        await asyncio.wait_for(processing_started.wait(), timeout=5.0)

        # Read the state file – the in-progress item should be persisted
        state_file = os.path.join(str(tmp_path), processor.get_state_file_name())
        with open(state_file) as f:
            state = json.load(f)

        assert "in_progress" in state
        assert state["in_progress"]["name"] == "test-item"
        # The queue should be empty (item was removed by get())
        assert state["queue"] == []

        # Let processing finish
        processing_continue.set()
        processor._shutdown_event.set()
        await asyncio.wait_for(run_task, timeout=5.0)

    @pytest.mark.asyncio
    async def test_in_progress_item_recovered_on_restart(self, tmp_path, mock_config):
        """An in-progress item from a saved state should be restored at the front of the queue."""
        state_file = os.path.join(
            str(tmp_path), f"{QueueType.VIDEO.value}_queue_state.json"
        )

        # Write a state file with an in-progress item and one queued item
        state = {
            "in_progress": {"task_type": "stub", "name": "crashed-item"},
            "queue": [{"task_type": "stub", "name": "queued-item"}],
        }
        with open(state_file, "w") as f:
            json.dump(state, f)

        processor = StubQueueProcessor(str(tmp_path), mock_config)
        processor._queue = asyncio.PriorityQueue()
        await processor.load_state()

        # The in-progress item should be first (priority 0), then the queued item (priority 2)
        assert processor._queue.qsize() == 2
        pri1, _, first = processor._queue.get_nowait()
        assert first.name == "crashed-item"
        assert pri1 == 0  # In-progress recovery gets highest priority
        pri2, _, second = processor._queue.get_nowait()
        assert second.name == "queued-item"
        assert pri2 == 2  # Normal priority

    @pytest.mark.asyncio
    async def test_in_progress_item_cleared_after_success(self, tmp_path, mock_config):
        """After successful processing, in_progress should be None and not in state file."""
        processor = StubQueueProcessor(str(tmp_path), mock_config)
        processor._queue = asyncio.PriorityQueue()

        task = StubTask("success-item")
        await processor.add_work(task)

        # Run one iteration of the processing loop then stop
        processor._shutdown_event = asyncio.Event()
        run_task = asyncio.create_task(processor._run())

        # Wait for the item to be processed
        await asyncio.sleep(0.5)
        processor._shutdown_event.set()
        await asyncio.wait_for(run_task, timeout=5.0)

        # Verify in-progress is cleared
        assert processor._in_progress_item is None

        # Verify state file has no in-progress
        state_file = os.path.join(str(tmp_path), processor.get_state_file_name())
        with open(state_file) as f:
            state = json.load(f)
        assert "in_progress" not in state
        assert state["queue"] == []

    @pytest.mark.asyncio
    async def test_in_progress_item_cleared_after_max_retries(
        self, tmp_path, mock_config
    ):
        """After exceeding max retries, the item should be removed completely."""
        processor = StubQueueProcessor(str(tmp_path), mock_config)
        processor._queue = asyncio.PriorityQueue()
        processor._max_retries = 0  # Fail immediately, no retries

        # Make process_item always fail
        async def always_fail(item):
            raise RuntimeError("always fails")

        processor.process_item = always_fail

        task = StubTask("fail-item")
        await processor.add_work(task)

        run_task = asyncio.create_task(processor._run())
        await asyncio.sleep(0.5)
        processor._shutdown_event.set()
        await asyncio.wait_for(run_task, timeout=5.0)

        # Verify the item is completely gone
        assert processor._in_progress_item is None
        assert processor._queue.qsize() == 0

        state_file = os.path.join(str(tmp_path), processor.get_state_file_name())
        with open(state_file) as f:
            state = json.load(f)
        assert "in_progress" not in state
        assert state["queue"] == []

    @pytest.mark.asyncio
    async def test_save_state_new_format_with_queue_key(self, tmp_path, mock_config):
        """State file should use new dict format with 'queue' key."""
        processor = StubQueueProcessor(str(tmp_path), mock_config)
        processor._queue = asyncio.PriorityQueue()

        task = StubTask("item-1")
        await processor.add_work(task)

        state_file = os.path.join(str(tmp_path), processor.get_state_file_name())
        with open(state_file) as f:
            state = json.load(f)

        assert isinstance(state, dict)
        assert "queue" in state
        assert len(state["queue"]) == 1
        assert state["queue"][0]["name"] == "item-1"

    @pytest.mark.asyncio
    async def test_load_state_legacy_format(self, tmp_path, mock_config):
        """Legacy state files (plain list) should still load correctly."""
        state_file = os.path.join(
            str(tmp_path), f"{QueueType.VIDEO.value}_queue_state.json"
        )

        # Write legacy format (plain list, no dict wrapper)
        legacy_state = [{"task_type": "stub", "name": "legacy-item"}]
        with open(state_file, "w") as f:
            json.dump(legacy_state, f)

        processor = StubQueueProcessor(str(tmp_path), mock_config)
        processor._queue = asyncio.PriorityQueue()
        await processor.load_state()

        assert processor._queue.qsize() == 1
        _, _, item = processor._queue.get_nowait()
        assert item.name == "legacy-item"
