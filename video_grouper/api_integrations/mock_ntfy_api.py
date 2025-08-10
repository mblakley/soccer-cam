"""
Mock NTFY API for E2E testing.

This module provides a mock implementation of the NTFY API that simulates
successful notification sends without actually contacting the external server.
"""

import os
import logging
import asyncio
from datetime import datetime
from typing import Dict, List, Any
from video_grouper.utils.config import NtfyConfig
from video_grouper.api_integrations.mock_ntfy_communication import mock_ntfy_comm

logger = logging.getLogger(__name__)


class MockNtfyAPI:
    """
    Mock NTFY API that simulates successful notification sends.

    This class provides the same interface as the real NTFY API but
    simulates successful sends without actually contacting the external server.
    """

    def __init__(self, config: NtfyConfig, service_callback=None):
        """
        Initialize the mock NTFY API.

        Args:
            config: NTFY configuration
            service_callback: Optional callback service to notify when responses are received
        """
        self.config = config
        self.topic = config.topic
        self.server_url = config.server_url
        self.enabled = config.enabled
        self.service_callback = service_callback
        self._initialized = False
        self._sent_notifications = []
        self._response_listeners = {}
        self._message_id_counter = 0

    async def initialize(self):
        """Initialize the mock API (no-op for mock)."""
        if not self.enabled:
            logger.warning("Mock NTFY API disabled")
            return False

        logger.info(f"Mock NTFY API initialized for topic: {self.topic}")
        self._initialized = True
        return True

    async def close(self):
        """Close the mock API (no-op for mock)."""
        logger.info("Mock NTFY API closed")
        self._initialized = False

    async def send_notification(
        self,
        message: str,
        title: str = None,
        tags: List[str] = None,
        priority: int = None,
        image_path: str = None,
        actions: List[Dict[str, Any]] = None,
    ) -> bool:
        """
        Simulate sending a notification successfully.

        Args:
            message: Notification message
            title: Notification title
            tags: List of tags
            priority: Priority level
            image_path: Path to image file
            actions: List of action buttons

        Returns:
            True (simulates successful send)
        """
        if not self._initialized:
            logger.info("Mock NTFY API not initialized, initializing now...")
            await self.initialize()

        # Send message through the communication system
        message_id = mock_ntfy_comm.send_message(
            topic=self.topic,
            message=message,
            title=title,
            tags=tags,
            priority=priority,
            image_path=image_path,
            actions=actions,
        )

        # Store the notification for potential response simulation
        notification = {
            "message_id": message_id,
            "topic": self.topic,
            "message": message,
            "title": title,
            "tags": tags or [],
            "priority": priority or 3,
            "image_path": image_path,
            "actions": actions or [],
            "sent_at": datetime.now().isoformat(),
        }
        self._sent_notifications.append(notification)

        # Set up response listener if service_callback is provided
        if self.service_callback:
            # Create a task to wait for response and call the callback
            asyncio.create_task(self._wait_for_response_and_callback(message_id))

        # Simulate a small delay to mimic network latency
        await asyncio.sleep(0.1)

        logger.info(f"Mock NTFY API: Notification sent successfully (ID: {message_id})")
        return True

    async def _wait_for_response_and_callback(self, message_id: str):
        """
        Wait for a response to a message and call the service callback.

        Args:
            message_id: Message ID to wait for
        """
        try:
            # Wait for response through the communication system
            response = mock_ntfy_comm.wait_for_response(message_id, timeout=60.0)

            if response and self.service_callback:
                logger.info(
                    f"Mock NTFY API: Received response '{response}' for {message_id}, calling service callback"
                )
                # Call the service callback asynchronously
                await self.service_callback.process_response(response)
            elif response:
                logger.info(
                    f"Mock NTFY API: Received response '{response}' for {message_id}, but no service callback"
                )
            else:
                logger.warning(
                    f"Mock NTFY API: No response received for {message_id} within timeout"
                )

        except Exception as e:
            logger.error(
                f"Mock NTFY API: Error waiting for response to {message_id}: {e}"
            )

    async def wait_for_response(
        self, message_id: str, timeout: float = 60.0
    ) -> Dict[str, Any]:
        """
        Simulate waiting for a response.

        Args:
            message_id: Message ID to wait for
            timeout: Timeout in seconds

        Returns:
            Mock response data
        """
        logger.info(f"Mock NTFY API: Waiting for response to message {message_id}")

        # Wait for response through the communication system
        response = mock_ntfy_comm.wait_for_response(message_id, timeout)

        if response:
            mock_response = {
                "message_id": message_id,
                "response": response,
                "received_at": datetime.now().isoformat(),
            }
            logger.info(f"Mock NTFY API: Received response: {response}")
            return mock_response
        else:
            logger.warning(f"Mock NTFY API: No response received for {message_id}")
            return {
                "message_id": message_id,
                "response": None,
                "received_at": datetime.now().isoformat(),
            }

    async def ask_team_info(
        self,
        group_dir: str,
        combined_video_path: str,
        existing_info: Dict[str, str] = None,
    ) -> Dict[str, str]:
        """
        Send notifications about missing team information fields.

        Args:
            group_dir: Directory containing the match_info.ini file
            combined_video_path: Path to the combined video file
            existing_info: Dictionary with existing team info fields

        Returns:
            Dict containing team_name, opponent_name, and location
        """
        if not self._initialized:
            logger.warning("Mock NTFY API not initialized")
            return existing_info or {}

        # Initialize with existing info or empty dict
        team_info = existing_info or {}

        # Check for missing fields
        missing_fields = []
        if "team_name" not in team_info and "my_team_name" not in team_info:
            missing_fields.append("team name")
        if "opponent_name" not in team_info and "opponent_team_name" not in team_info:
            missing_fields.append("opponent team name")
        if "location" not in team_info:
            missing_fields.append("game location")

        # If there are missing fields, send a notification
        if missing_fields:
            missing_fields_str = ", ".join(missing_fields)

            # Get the directory information from the group directory
            directory_info = ""
            if group_dir:
                directory_name = os.path.basename(group_dir)
                directory_info = f" in directory: {directory_name}"

            # Send the notification
            success = await self.send_notification(
                message=f"Missing match information{directory_info}: {missing_fields_str}. Please update match_info.ini manually.",
                title="Missing Match Information",
                tags=["warning", "info"],
                priority=4,
            )

            if success:
                logger.info(
                    f"Mock NTFY API: Sent team info request for missing fields: {missing_fields_str}"
                )
            else:
                logger.error("Mock NTFY API: Failed to send team info request")
        else:
            logger.info(
                "Mock NTFY API: All team information fields are populated, no notification needed"
            )

        return team_info

    async def shutdown(self):
        """Shutdown the mock API."""
        logger.info("Mock NTFY API shutdown")
        self._initialized = False

    def get_sent_notifications(self) -> List[Dict[str, Any]]:
        """Get list of sent notifications for testing/debugging."""
        return self._sent_notifications.copy()


def create_mock_ntfy_api(config: NtfyConfig, service_callback=None) -> MockNtfyAPI:
    """
    Factory function to create a mock NTFY API.

    Args:
        config: NTFY configuration
        service_callback: Optional callback service to notify when responses are received

    Returns:
        MockNtfyAPI instance
    """
    return MockNtfyAPI(config, service_callback)
