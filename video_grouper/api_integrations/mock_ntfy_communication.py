"""
Shared communication system for mock NTFY components.

This module provides a communication mechanism between the mock NTFY API
and NTFY response service to simulate the complete user interaction flow.
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class MockNtfyMessage:
    """Represents a mock NTFY message."""

    message_id: str
    topic: str
    message: str
    title: str
    tags: List[str]
    priority: int
    image_path: Optional[str]
    actions: List[Dict[str, Any]]
    sent_at: str
    response: Optional[str] = None
    responded_at: Optional[str] = None


class MockNtfyCommunication:
    """
    Shared communication system for mock NTFY components.

    This class provides a singleton instance that allows the mock NTFY API
    to "send" notifications and the NTFY response service to "receive"
    and respond to them.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._messages: Dict[str, MockNtfyMessage] = {}
        self._response_callbacks: List[Callable[[MockNtfyMessage], None]] = []
        self._message_counter = 0
        self._initialized = True
        logger.info("Mock NTFY communication system initialized")

    def send_message(
        self,
        topic: str,
        message: str,
        title: str,
        tags: List[str],
        priority: int,
        image_path: Optional[str],
        actions: List[Dict[str, Any]],
    ) -> str:
        """
        Send a mock NTFY message.

        Args:
            topic: NTFY topic
            message: Message content
            title: Message title
            tags: Message tags
            priority: Message priority
            image_path: Path to image file
            actions: List of action buttons

        Returns:
            Message ID
        """
        message_id = f"mock_msg_{self._message_counter}"
        self._message_counter += 1

        mock_message = MockNtfyMessage(
            message_id=message_id,
            topic=topic,
            message=message,
            title=title,
            tags=tags or [],
            priority=priority or 3,
            image_path=image_path,
            actions=actions or [],
            sent_at=datetime.now().isoformat(),
        )

        self._messages[message_id] = mock_message

        logger.info(
            f"Mock NTFY: Message sent - ID: {message_id}, Topic: {topic}, Message: {message[:50]}..."
        )

        # Notify response callbacks
        for callback in self._response_callbacks:
            try:
                callback(mock_message)
            except Exception as e:
                logger.error(f"Error in response callback: {e}")

        return message_id

    def get_pending_messages(self, topic: str) -> List[MockNtfyMessage]:
        """
        Get pending messages for a topic.

        Args:
            topic: NTFY topic

        Returns:
            List of pending messages
        """
        return [
            msg
            for msg in self._messages.values()
            if msg.topic == topic and msg.response is None
        ]

    def respond_to_message(self, message_id: str, response: str) -> bool:
        """
        Respond to a mock NTFY message.

        Args:
            message_id: Message ID to respond to
            response: Response content

        Returns:
            True if response was successful
        """
        if message_id not in self._messages:
            logger.warning(f"Mock NTFY: Message {message_id} not found")
            return False

        mock_message = self._messages[message_id]
        mock_message.response = response
        mock_message.responded_at = datetime.now().isoformat()

        logger.info(f"Mock NTFY: Response received for {message_id}: {response}")
        return True

    def wait_for_response(
        self, message_id: str, timeout: float = 60.0
    ) -> Optional[str]:
        """
        Wait for a response to a message.

        Args:
            message_id: Message ID to wait for
            timeout: Timeout in seconds

        Returns:
            Response content or None if timeout
        """
        start_time = datetime.now()

        while (datetime.now() - start_time).total_seconds() < timeout:
            if message_id in self._messages:
                mock_message = self._messages[message_id]
                if mock_message.response is not None:
                    return mock_message.response

            # Sleep briefly before checking again
            import time

            time.sleep(0.1)

        logger.warning(f"Mock NTFY: Timeout waiting for response to {message_id}")
        return None

    def register_response_callback(
        self, callback: Callable[[MockNtfyMessage], None]
    ) -> None:
        """
        Register a callback to be called when new messages are sent.

        Args:
            callback: Function to call with new messages
        """
        self._response_callbacks.append(callback)
        logger.info("Mock NTFY: Response callback registered")

    def get_message(self, message_id: str) -> Optional[MockNtfyMessage]:
        """
        Get a specific message by ID.

        Args:
            message_id: Message ID

        Returns:
            MockNtfyMessage or None if not found
        """
        return self._messages.get(message_id)

    def clear_messages(self) -> None:
        """Clear all messages (useful for testing)."""
        self._messages.clear()
        logger.info("Mock NTFY: All messages cleared")


# Global instance
mock_ntfy_comm = MockNtfyCommunication()
