#!/usr/bin/env python3
"""
Test script to verify unified NTFY state management.
"""

import asyncio
import sys
import os
import json

# Add the project root to the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from video_grouper.task_processors.services.ntfy_service import NtfyService
from video_grouper.utils.config import NtfyConfig

from video_grouper.utils.logger import setup_logging, get_logger

# Set up logging
setup_logging(level="DEBUG", app_name="test_ntfy")
logger = get_logger(__name__)


async def test_unified_state():
    """Test the unified state management."""

    # Create a minimal config
    ntfy_config = NtfyConfig(
        enabled=True, server_url="https://ntfy.sh", topic="test-topic"
    )

    # Create NTFY service with test storage
    test_storage = "/tmp/test_ntfy_unified"
    service = NtfyService(ntfy_config, test_storage)

    # Test saving and loading pending tasks
    print("Testing unified NTFY state management...")

    # Add a pending task
    service.mark_waiting_for_input(
        "/test/dir1",
        "game_start_time",
        {"task_metadata": {"time_offset": "00:00"}, "status": "waiting_for_input"},
    )
    pending_tasks = service.get_pending_tasks()
    print(f"✓ Added pending task, total: {len(pending_tasks)}")
    assert "/test/dir1" in pending_tasks

    # Add another pending task
    service.mark_waiting_for_input(
        "/test/dir2",
        "team_info",
        {"task_metadata": {"existing_info": {}}, "status": "waiting_for_input"},
    )
    pending_tasks = service.get_pending_tasks()
    print(f"✓ Added second pending task, total: {len(pending_tasks)}")
    assert "/test/dir2" in pending_tasks

    # Clear a pending task
    service.clear_pending_task("/test/dir1")
    pending_tasks = service.get_pending_tasks()
    print(f"✓ Cleared one pending task, total: {len(pending_tasks)}")
    assert "/test/dir1" not in pending_tasks

    # Test processed directories
    service.mark_as_processed("/test/dir3")
    processed_dirs = service.get_processed_directories()
    print(f"✓ Added processed directory, total: {len(processed_dirs)}")

    # Check the unified state file
    state_file_path = service.get_state_file_path()
    print(f"✓ Unified state file: {state_file_path}")

    if os.path.exists(state_file_path):
        with open(state_file_path, "r") as f:
            state = json.load(f)
        print(f"✓ State file contains: {list(state.keys())}")
        assert "pending_tasks" in state
    else:
        print("✗ State file not found")

    print("Test completed successfully!")


if __name__ == "__main__":
    asyncio.run(test_unified_state())
