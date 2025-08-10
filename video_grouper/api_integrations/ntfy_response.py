"""
NTFY response service for end-to-end testing.

This service subscribes to the NTFY topic and automatically responds with
controlled inputs to simulate user interaction during E2E testing.
"""

import asyncio
import logging
from typing import Optional
from video_grouper.api_integrations.mock_ntfy_communication import mock_ntfy_comm

logger = logging.getLogger(__name__)


class NtfyResponseService:
    """
    NTFY response service that automatically responds to NTFY notifications.

    This service subscribes to the configured NTFY topic and responds with
    controlled inputs to simulate user interaction during E2E testing.
    """

    def __init__(self, topic: str, server_url: str = "https://ntfy.sh"):
        """
        Initialize the NTFY response service.

        Args:
            topic: NTFY topic to subscribe to
            server_url: NTFY server URL
        """
        self.topic = topic
        self.server_url = server_url
        self.subscription_url = f"{server_url}/{topic}/json"
        self.response_url = f"{server_url}/{topic}"

        # Track message count for controlled responses
        self.message_count = 0

        # Define response sequence for E2E testing
        self.response_sequence = [
            # First message: "No, not yet at 00:00"
            "No, not yet at 00:00",
            # Second message: "No, not yet at 05:00"
            "No, not yet at 05:00",
            # Third message: "Yes, game started at 10:00"
            "Yes, game started at 10:00",
        ]

        # Track processed messages to avoid duplicates
        self.processed_messages = set()

        logger.info(f"NTFY response service initialized for topic: {topic}")

    async def start(self):
        """Start the NTFY response service."""
        logger.info("Starting NTFY response service")

        # Start the subscription task
        self.subscription_task = asyncio.create_task(self._subscribe_and_respond())

        logger.info("NTFY response service started")

    async def stop(self):
        """Stop the NTFY response service."""
        logger.info("Stopping NTFY response service")

        if hasattr(self, "subscription_task"):
            self.subscription_task.cancel()
            try:
                await self.subscription_task
            except asyncio.CancelledError:
                pass

        logger.info("NTFY response service stopped")

    async def _subscribe_and_respond(self):
        """Subscribe to NTFY topic and respond to messages."""
        logger.info(f"Subscribing to NTFY topic: {self.topic}")

        # Register callback to receive messages from the communication system
        mock_ntfy_comm.register_response_callback(self._handle_new_message)

        # Also subscribe to real NTFY topic to respond to real requests
        logger.info(f"Subscribing to real NTFY topic: {self.topic}")
        self._real_ntfy_task = asyncio.create_task(self._subscribe_to_real_ntfy())

        logger.info(f"Successfully subscribed to NTFY topic: {self.topic}")

        # Keep the task running to handle messages
        while True:
            await asyncio.sleep(1)

    def _handle_new_message(self, message):
        """Handle new messages from the communication system."""
        logger.info(
            f"NTFY response service: Received message: {message.message[:50]}..."
        )

        # Process the message asynchronously
        asyncio.create_task(self._process_message_from_comm(message))

    async def _process_message_from_comm(self, message):
        """Process a message from the communication system."""
        try:
            # Check if we should respond to this message
            if self._should_respond_to_message(message.title, message.message):
                # Add a small delay to ensure the task is properly registered
                await asyncio.sleep(5)

                response = self._get_response_for_message(
                    message.title, message.message
                )
                if response:
                    # Send response through the communication system
                    success = mock_ntfy_comm.respond_to_message(
                        message.message_id, response
                    )
                    if success:
                        logger.info(f"NTFY response service: Sent response: {response}")
                    else:
                        logger.error("NTFY response service: Failed to send response")
        except Exception as e:
            logger.error(f"NTFY response service: Error processing message: {e}")

    async def _subscribe_to_real_ntfy(self):
        """Subscribe to real NTFY topic and respond to messages."""
        import httpx
        import json

        subscription_url = f"{self.server_url}/{self.topic}/json"
        response_url = f"{self.server_url}/{self.topic}"

        print(f"NTFY response service: Subscribing to real NTFY topic: {self.topic}")
        print(f"Subscription URL: {subscription_url}")

        try:
            async with httpx.AsyncClient(timeout=None) as client:
                print(
                    f"NTFY response service: Making GET request to {subscription_url}"
                )
                async with client.stream("GET", subscription_url) as response:
                    print(
                        f"NTFY response service: Response status: {response.status_code}"
                    )
                    if response.status_code != 200:
                        print(
                            f"NTFY response service: Failed to subscribe to real NTFY topic: {response.status_code}"
                        )
                        return

                    print(
                        f"NTFY response service: Successfully subscribed to real NTFY topic: {self.topic}"
                    )

                    async for line in response.aiter_lines():
                        if line.strip():
                            try:
                                data = json.loads(line)
                                print(f"NTFY response service: Received data: {data}")

                                # Skip non-message events
                                if data.get("event") != "message":
                                    print(
                                        f"NTFY response service: Skipping non-message event: {data.get('event')}"
                                    )
                                    continue

                                message_content = data.get("message", "")
                                title = data.get("title", "")

                                print(
                                    f"NTFY response service: Received real NTFY message: {title} - {message_content[:50]}..."
                                )

                                # Check if we should respond to this message
                                if self._should_respond_to_message(
                                    title, message_content
                                ):
                                    print(
                                        "NTFY response service: Should respond to message"
                                    )
                                    # Add a small delay to ensure the task is properly registered
                                    await asyncio.sleep(0.5)

                                    response_text = self._get_response_for_message(
                                        title, message_content
                                    )
                                    if response_text:
                                        print(
                                            f"NTFY response service: Sending response: {response_text}"
                                        )
                                        # Send response to real NTFY topic
                                        try:
                                            resp = await client.post(
                                                response_url,
                                                data=response_text.encode("utf-8"),
                                            )
                                            if resp.status_code == 200:
                                                print(
                                                    f"NTFY response service: Sent real NTFY response: {response_text}"
                                                )
                                            else:
                                                print(
                                                    f"NTFY response service: Failed to send real NTFY response: {resp.status_code}"
                                                )
                                        except Exception as e:
                                            print(
                                                f"NTFY response service: Error sending response: {e}"
                                            )

                            except json.JSONDecodeError as e:
                                print(f"NTFY response service: JSON decode error: {e}")
                                continue
                            except Exception as e:
                                print(
                                    f"NTFY response service: Error processing real NTFY message: {e}"
                                )

        except Exception as e:
            print(f"NTFY response service: Error subscribing to real NTFY topic: {e}")

    def _should_respond_to_message(self, title: str, message: str) -> bool:
        """Determine if we should respond to this message."""
        # Respond to game start/end detection messages
        if "game start" in title.lower() or "game end" in title.lower():
            return True

        # Respond to team info requests
        if "missing match information" in message.lower():
            return True

        # Respond to playlist name requests
        if "youtube playlist request" in title.lower():
            return True

        return False

    def _get_response_for_message(self, title: str, message: str) -> Optional[str]:
        """Get the appropriate response for the message."""
        logger.info(
            f"NTFY response service: Getting response for title='{title}', message='{message[:100]}...'"
        )

        # For team info requests, provide structured data
        if "missing match information" in message.lower():
            if self.message_count == 0:
                response = "Hawks"  # Team name
                logger.info(
                    f"NTFY response service: Responding with team name: {response}"
                )
            elif self.message_count == 1:
                response = "Eagles"  # Opponent name
                logger.info(
                    f"NTFY response service: Responding with opponent name: {response}"
                )
            elif self.message_count == 2:
                response = "Central Park Soccer Fields"  # Location
                logger.info(
                    f"NTFY response service: Responding with location: {response}"
                )
            else:
                response = "No"  # Default response for game start/end questions
                logger.info(
                    f"NTFY response service: Responding with default: {response}"
                )

            self.message_count += 1
            return response

        # For playlist name requests
        if "youtube playlist request" in title.lower():
            response = "Hawks Soccer 2024"
            logger.info(
                f"NTFY response service: Responding with playlist name: {response}"
            )
            self.message_count += 1
            return response

        # For game start/end detection messages
        if "game start" in title.lower() or "game end" in title.lower():
            # Always send a response from the sequence, cycling through them
            response_index = self.message_count % len(self.response_sequence)
            response = self.response_sequence[response_index]
            logger.info(
                f"NTFY response service: Responding to game detection: {response} (index {response_index})"
            )
            self.message_count += 1
            return response

        # Default case
        logger.warning(
            f"NTFY response service: No response pattern matched for title='{title}'"
        )
        return None


# Factory function to create the NTFY response service
def create_ntfy_response_service(config) -> NtfyResponseService:
    """
    Create an NTFY response service based on configuration.

    Args:
        config: NTFY configuration object

    Returns:
        NtfyResponseService instance
    """
    topic = getattr(config, "topic", "video_grouper_mblakley43431")
    server_url = getattr(config, "server_url", "https://ntfy.sh")

    return NtfyResponseService(topic=topic, server_url=server_url)
