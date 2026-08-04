"""
NTFY response service for end-to-end testing.

This service subscribes to the NTFY topic and automatically responds with
controlled inputs to simulate user interaction during E2E testing.
"""

import asyncio
import logging

from video_grouper.api_integrations.mock_ntfy_communication import mock_ntfy_comm

logger = logging.getLogger(__name__)


class NtfyResponseService:
    """
    NTFY response service that automatically responds to NTFY notifications.

    This service subscribes to the configured NTFY topic and responds with
    controlled inputs to simulate user interaction during E2E testing.
    """

    # Reconnect backoff for the long-lived ntfy.sh stream. Starts short
    # because a server-side close is routine and the topic should be
    # listened to again almost immediately; caps so a genuine outage
    # doesn't turn into a hot retry loop.
    _RECONNECT_MIN_SECONDS = 5
    _RECONNECT_MAX_SECONDS = 300

    def __init__(
        self,
        topic: str,
        server_url: str = "https://ntfy.sh",
        auto_respond: bool = False,
    ):
        """
        Initialize the NTFY response service.

        Args:
            topic: NTFY topic to subscribe to
            server_url: NTFY server URL
            auto_respond: Whether to automatically respond to messages.
                          Only enable for E2E testing, never in production.
        """
        self.topic = topic
        self.server_url = server_url
        self.auto_respond = auto_respond
        self.subscription_url = f"{server_url}/{topic}/json"
        self.response_url = f"{server_url}/{topic}"

        # Per-type message counters for controlled responses
        self.message_counts: dict[str, int] = {
            "game_start": 0,
            "game_end": 0,
            "team_info": 0,
            "playlist": 0,
        }

        # Track processed NTFY message IDs to avoid duplicates
        self._processed_message_ids: set = set()

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

        # Register callback to receive messages from the mock communication system
        mock_ntfy_comm.register_response_callback(self._handle_new_message)

        # Also subscribe to real NTFY topic to respond to real requests
        logger.info(f"Subscribing to real NTFY topic: {self.topic}")
        self._real_ntfy_task = asyncio.create_task(self._real_ntfy_forever())

        logger.info(f"Successfully subscribed to NTFY topic: {self.topic}")

        # Keep the task running to handle messages
        while True:
            await asyncio.sleep(1)

    def _handle_new_message(self, message):
        """Handle new messages from the mock communication system."""
        logger.info(
            f"NTFY response service: Received message: {message.message[:50]}..."
        )

        # Process the message asynchronously
        asyncio.create_task(self._process_message_from_comm(message))

    async def _process_message_from_comm(self, message):
        """Process a message from the mock communication system."""
        try:
            # Check if we should respond to this message
            if self._should_respond_to_message(message.title, message.message):
                # Add a delay to ensure the task is properly registered
                await asyncio.sleep(3)

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

    async def _real_ntfy_forever(self):
        """Keep the NTFY subscription alive across server-side disconnects.

        ntfy.sh serves the topic as a long-lived chunked stream and closes
        it periodically -- observed every 1.5-3 hours in production. The
        subscribe coroutine used to be spawned exactly once, so the first
        close ended it ("peer closed connection without sending complete
        message body") and nothing ever re-created the task. From that
        moment the service silently stopped receiving responses: game
        start/end confirmations and team-info replies were dropped until
        somebody restarted the service. On 2026-08-04 the listener was
        dead for 25 minutes before an unrelated restart revived it, then
        died again 3 hours later.

        A disconnect is normal operation, not an error, so reconnect with
        a short backoff and treat a clean stream end the same as a raised
        exception -- both mean "no longer subscribed".
        """
        backoff = self._RECONNECT_MIN_SECONDS
        while True:
            try:
                await self._subscribe_to_real_ntfy()
                # Returned without raising: the stream ended cleanly.
                # Still means we are no longer subscribed.
                backoff = self._RECONNECT_MIN_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "NTFY response service: subscription dropped (%s); "
                    "reconnecting in %ss",
                    exc,
                    backoff,
                )
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(backoff * 2, self._RECONNECT_MAX_SECONDS)

    async def _subscribe_to_real_ntfy(self):
        """Subscribe to real NTFY topic and respond to messages.

        Returns when the stream ends; raises on transport errors. The
        caller (:meth:`_real_ntfy_forever`) owns reconnection.
        """
        import json

        import httpx

        subscription_url = f"{self.server_url}/{self.topic}/json"
        response_url = f"{self.server_url}/{self.topic}"

        logger.info(
            f"NTFY response service: Subscribing to real NTFY topic: {self.topic}"
        )
        logger.info(f"NTFY response service: Subscription URL: {subscription_url}")

        try:
            async with httpx.AsyncClient(timeout=None) as client:
                logger.info(
                    f"NTFY response service: Making GET request to {subscription_url}"
                )
                async with client.stream("GET", subscription_url) as response:
                    logger.info(
                        f"NTFY response service: Response status: {response.status_code}"
                    )
                    if response.status_code != 200:
                        logger.error(
                            f"NTFY response service: Failed to subscribe: {response.status_code}"
                        )
                        return

                    logger.info(
                        f"NTFY response service: Successfully subscribed to real NTFY topic: {self.topic}"
                    )

                    async for line in response.aiter_lines():
                        if line.strip():
                            try:
                                data = json.loads(line)
                                logger.debug(
                                    f"NTFY response service: Received data: {data}"
                                )

                                # Skip non-message events
                                if data.get("event") != "message":
                                    continue

                                # Deduplicate by NTFY message ID
                                msg_id = data.get("id", "")
                                if msg_id in self._processed_message_ids:
                                    logger.debug(
                                        f"NTFY response service: Skipping already-processed message: {msg_id}"
                                    )
                                    continue
                                self._processed_message_ids.add(msg_id)

                                message_content = data.get("message", "")
                                title = data.get("title", "")

                                logger.info(
                                    f"NTFY response service: Received real NTFY message: "
                                    f"title='{title}', message='{message_content[:80]}...'"
                                )

                                # Check if we should respond to this message
                                if self._should_respond_to_message(
                                    title, message_content
                                ):
                                    # Wait for the app to register the task as waiting_for_input
                                    await asyncio.sleep(3)

                                    response_text = self._get_response_for_message(
                                        title, message_content
                                    )
                                    if response_text:
                                        logger.info(
                                            f"NTFY response service: Sending response: {response_text}"
                                        )
                                        try:
                                            resp = await client.post(
                                                response_url,
                                                data=response_text.encode("utf-8"),
                                            )
                                            if resp.status_code == 200:
                                                logger.info(
                                                    f"NTFY response service: Sent real NTFY response: {response_text}"
                                                )
                                            else:
                                                logger.error(
                                                    f"NTFY response service: Failed to send response: {resp.status_code}"
                                                )
                                        except Exception as e:
                                            logger.error(
                                                f"NTFY response service: Error sending response: {e}"
                                            )

                            except json.JSONDecodeError as e:
                                logger.warning(
                                    f"NTFY response service: JSON decode error: {e}"
                                )
                                continue
                            except Exception as e:
                                logger.error(
                                    f"NTFY response service: Error processing real NTFY message: {e}"
                                )

        except Exception as e:
            # Do NOT swallow this: _real_ntfy_forever needs it to trigger a
            # reconnect. Swallowing it here is what left the listener dead.
            logger.error(
                f"NTFY response service: Error subscribing to real NTFY topic: {e}"
            )
            raise

    def _should_respond_to_message(self, title: str, message: str) -> bool:
        """Determine if we should respond to this message."""
        if not self.auto_respond:
            return False

        title_lower = title.lower()
        message_lower = message.lower()

        # Respond to game start/end detection messages
        if "game start" in title_lower or "game end" in title_lower:
            return True

        # Respond to team info requests
        if "missing match information" in message_lower:
            return True

        # Respond to "set game start time" messages
        if "set game start time" in title_lower or "set game end time" in title_lower:
            return True

        # Respond to playlist name requests
        if "youtube playlist request" in title_lower:
            return True

        return False

    def _get_response_for_message(self, title: str, message: str) -> str | None:
        """Get the appropriate response for the message based on per-type counters."""
        title_lower = title.lower()
        message_lower = message.lower()

        logger.info(
            f"NTFY response service: Getting response for title='{title}', "
            f"message='{message[:100]}...'"
        )

        # Handle team info requests
        if "missing match information" in message_lower:
            count = self.message_counts["team_info"]
            self.message_counts["team_info"] += 1

            if count == 0:
                response = "Hawks"
            elif count == 1:
                response = "Eagles"
            elif count == 2:
                response = "Central Park Soccer Fields"
            else:
                response = "Unknown"

            logger.info(
                f"NTFY response service: Responding with team info [{count}]: {response}"
            )
            return response

        # Handle playlist name requests
        if "youtube playlist request" in title_lower:
            self.message_counts["playlist"] += 1
            response = "Hawks Soccer 2024"
            logger.info(
                f"NTFY response service: Responding with playlist name: {response}"
            )
            return response

        # Handle game start time detection
        if "game start" in title_lower or "set game start time" in title_lower:
            count = self.message_counts["game_start"]
            self.message_counts["game_start"] += 1

            # Respond "Yes" immediately (at 00:00) since test clips are 1 minute each
            # (3 minutes total), so the game is already in progress from the first frame.
            response = "Yes, game started at 00:00"

            logger.info(
                f"NTFY response service: Responding to game start [{count}]: {response}"
            )
            return response

        # Handle game end time detection
        if "game end" in title_lower or "set game end time" in title_lower:
            count = self.message_counts["game_end"]
            self.message_counts["game_end"] += 1
            response = "Yes, the game ended"
            logger.info(
                f"NTFY response service: Responding to game end [{count}]: {response}"
            )
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
    auto_respond = getattr(config, "auto_respond", False)

    return NtfyResponseService(
        topic=topic, server_url=server_url, auto_respond=auto_respond
    )
