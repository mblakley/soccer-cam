"""
NTFY service for interactive user notifications and input.
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any

from video_grouper.api_integrations.ntfy import NtfyAPI
from video_grouper.utils.config import NtfyConfig

from ...utils.paths import get_ntfy_service_state_path

logger = logging.getLogger(__name__)

# A buffered response carries no group_dir of its own, so we use freshness
# relative to task registration as an identity proxy: a response only replays
# against a newly-registered task if it plausibly arrived AS an answer to that
# task. This stops a stale tap from an earlier game (replayed by the listener's
# ?since=24h reconnect) from auto-answering a different game's question.
#
# When we have the NTFY server timestamp (the normal phone-tap path), the tap
# must have happened no earlier than this many seconds before the task was
# registered. 60s comfortably covers the genuine sub-second race (the tap
# arrives microseconds before mark_waiting_for_input finishes) while rejecting
# taps that are minutes/hours old.
BUFFER_FRESHNESS_WINDOW_SECONDS = 60

# Fallback when a buffered response has no usable server timestamp
# (synthetic/internal responses): only an in-flight race qualifies, so the
# entry must have been received within this many seconds (monotonic) of the
# task registering. We never replay an older entry on this weak signal.
BUFFER_RACE_WINDOW_SECONDS = 10


class NtfyService:
    """
    Service for NTFY API integration with state tracking.
    Handles interactive notifications and tracks pending tasks (user input and queue).
    """

    def __init__(self, config: NtfyConfig, storage_path: str, completion_callback=None):
        """
        Initialize NTFY service.

        Args:
            config: Configuration object
            storage_path: Path to storage directory
            completion_callback: Optional callback to NTFY processor for match info completion and queue management
        """
        self.config = config
        self.storage_path = storage_path
        self.ntfy_api = None
        self.enabled = False
        self.completion_callback = completion_callback

        # State tracking for pending tasks
        self._pending_tasks: dict[str, dict[str, Any]] = {}
        self._processed_dirs: set[str] = set()
        self._state_file = get_ntfy_service_state_path(storage_path)

        # Buffer for responses that arrived before any task was registered
        # to receive them. The recurring race: state_auditor queues a new
        # NTFY question while the listener is replaying the user's earlier
        # tap (?since=24h on reconnect). The replay arrives microseconds
        # before mark_waiting_for_input completes, so process_response
        # finds no matching task and drops the response — and then the
        # task registers and the user's phone shows the SAME question
        # again, even though they already answered.
        # Buffer entries are (received_monotonic, server_time, response_text)
        # where server_time is the NTFY server receive time (epoch seconds,
        # or None if unavailable). When a new task registers we replay the
        # buffer against it, but ONLY for entries that are fresh relative to
        # the task's registration time (see _try_buffered_responses_for) — a
        # stale tap from a previous game must never auto-answer a new one.
        # Entries older than the TTL get pruned on every check so the buffer
        # doesn't grow.
        from collections import deque

        self._unmatched_responses: deque = deque(maxlen=64)
        self._unmatched_response_ttl_seconds = 300  # 5 min — far longer
        # than the listener replay window typically needs.

        # For handling direct responses to prompts
        self._response_events: dict[str, asyncio.Event] = {}
        self._response_data: dict[str, str | None] = {}

        # Generic response handlers for non-task components (e.g. camera poller)
        self._response_handlers: dict[str, Any] = {}

        # Track the most recently sent notification's group_dir
        # so responses get routed to the correct group
        self._last_notified_group_dir: str | None = None

        self._state: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._initialized = False

        self._initialize_api()
        self._load_state()

    def _initialize_api(self) -> None:
        """Initialize NTFY API."""
        try:
            # Check if we should use mock NTFY API for testing
            use_mock_ntfy = os.environ.get("USE_MOCK_NTFY", "false").lower() == "true"

            if use_mock_ntfy:
                from video_grouper.api_integrations.mock_ntfy_api import (
                    create_mock_ntfy_api,
                )

                self.ntfy_api = create_mock_ntfy_api(self.config, service_callback=self)
                logger.info("Using mock NTFY API for testing")
            else:
                self.ntfy_api = NtfyAPI(self.config, service_callback=self)

            self.enabled = self.ntfy_api.enabled

            if self.enabled:
                logger.info("NTFY service enabled")
            else:
                logger.info("NTFY service disabled")
        except Exception as e:
            logger.error(f"Error initializing NTFY API: {e}")
            self.enabled = False

    async def _ensure_initialized(self) -> bool:
        """Ensure NTFY API is fully initialized. Called before first use."""
        if not self.enabled or not self.ntfy_api:
            return False

        # Check if already initialized
        if hasattr(self.ntfy_api, "_initialized") and self.ntfy_api._initialized:
            return True

        try:
            await self.ntfy_api.initialize()
            # Mark as initialized to avoid repeated calls
            self.ntfy_api._initialized = True
            return True
        except Exception as e:
            logger.error(f"Error initializing NTFY API async components: {e}")
            self.enabled = False
            return False

    def _load_state(self) -> None:
        """Load unified NTFY state from disk."""
        if not os.path.exists(self._state_file):
            return
        try:
            with open(self._state_file) as f:
                state = json.load(f)
            self._pending_tasks = state.get("pending_tasks", {})
            self._processed_dirs = set(state.get("processed_dirs", []))
            logger.debug(
                f"NTFY service loaded unified state: {len(self._pending_tasks)} pending tasks, "
                f"{len(self._processed_dirs)} processed dirs"
            )
        except Exception as e:
            logger.error(f"Error loading NTFY unified state: {e}")
            self._pending_tasks = {}
            self._processed_dirs = set()

    def _save_state(self) -> None:
        """Save unified NTFY state to disk."""
        try:
            state = {
                "pending_tasks": self._pending_tasks,
                "processed_dirs": list(self._processed_dirs),
                "last_updated": datetime.now().isoformat(),
            }
            with open(self._state_file, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving NTFY unified state: {e}")

    def is_waiting_for_input(self, group_dir: str) -> bool:
        """Check if we're waiting for user input for a specific directory."""
        return (
            group_dir in self._pending_tasks
            and self._pending_tasks[group_dir].get("status") == "waiting_for_input"
        )

    def is_failed_to_send(self, group_dir: str) -> bool:
        """Check if a task failed to send for a specific directory."""
        return (
            group_dir in self._pending_tasks
            and self._pending_tasks[group_dir].get("status") == "failed_to_send"
        )

    def has_been_processed(self, group_dir: str) -> bool:
        """Check if a directory has already been processed with NTFY."""
        return group_dir in self._processed_dirs

    def mark_as_processed(self, group_dir: str) -> None:
        """Mark a directory as processed."""
        self._processed_dirs.add(group_dir)
        self._pending_tasks.pop(group_dir, None)
        self._save_state()

    def mark_waiting_for_input(
        self, group_dir: str, task_type: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Mark a directory as waiting for user input."""
        # Convert old metadata structure to new unified structure
        if metadata and "task_metadata" in metadata:
            # Already in new format
            task_metadata = metadata["task_metadata"]
        else:
            # Convert from old format
            task_metadata = metadata or {}

        self._pending_tasks[group_dir] = {
            "task_type": task_type,
            "status": "waiting_for_input",
            "task_metadata": task_metadata,
            "sent_at": datetime.now().isoformat(),
            "response": None,
        }
        self._last_notified_group_dir = group_dir
        self._save_state()
        logger.info(
            f"NTFY: Marked {group_dir} as waiting for input (task_type: {task_type})"
        )

        # Replay any unmatched responses that arrived before this task
        # registered — this is the bug Mark hit where he answers, the
        # response gets dropped because the task hadn't registered yet,
        # and his phone gets the same question again. _try_buffered_responses_for
        # is async (it awaits _process_task_response), so schedule it
        # rather than blocking. If the buffer is empty, this is a no-op.
        if self._unmatched_responses:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._try_buffered_responses_for(group_dir))
            except RuntimeError:
                # No running loop — synchronous test path. Skip; the buffer
                # will remain populated and there's no async work to do.
                pass

    def mark_failed_to_send(
        self, group_dir: str, task_type: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Mark a directory as failed to send notification."""
        # Convert old metadata structure to new unified structure
        if metadata and "task_metadata" in metadata:
            # Already in new format
            task_metadata = metadata["task_metadata"]
        else:
            # Convert from old format
            task_metadata = metadata or {}

        self._pending_tasks[group_dir] = {
            "task_type": task_type,
            "status": "failed_to_send",
            "task_metadata": task_metadata,
            "failed_at": datetime.now().isoformat(),
            "retry_count": self._pending_tasks.get(group_dir, {}).get("retry_count", 0)
            + 1,
        }
        self._save_state()

    def clear_pending_task(self, group_dir: str) -> None:
        """Clear pending task status for a directory."""
        if group_dir in self._pending_tasks:
            del self._pending_tasks[group_dir]
            self._save_state()

    def get_pending_tasks(self) -> dict[str, dict[str, Any]]:
        """Get all pending tasks."""
        return self._pending_tasks.copy()

    # For backward compatibility
    def get_pending_inputs(self) -> dict[str, dict[str, Any]]:
        return self.get_pending_tasks()

    def get_processed_directories(self) -> set[str]:
        """Get all processed directories."""
        return self._processed_dirs.copy()

    def get_state_file_path(self) -> str:
        """Get the path to the unified NTFY state file."""
        return self._state_file

    async def send_notification(
        self,
        message: str,
        title: str = None,
        tags: list[str] = None,
        priority: int = None,
        image_path: str = None,
        actions: list[dict[str, Any]] = None,
    ) -> bool:
        """
        Send a notification via NTFY, ensuring the API is initialized first.

        Args:
            message: Notification message
            title: Notification title
            tags: List of tags
            priority: Priority level
            image_path: Path to image file
            actions: List of action buttons

        Returns:
            True if notification was sent successfully
        """
        # Ensure the API is initialized
        if not await self._ensure_initialized():
            logger.error("Failed to initialize NTFY API for notification")
            return False

        # Send the notification
        return await self.ntfy_api.send_notification(
            message=message,
            title=title,
            tags=tags,
            priority=priority,
            image_path=image_path,
            actions=actions,
        )

    async def shutdown(self) -> None:
        """Shutdown the NTFY service."""
        if self.ntfy_api:
            await self.ntfy_api.shutdown()
        self._save_state()

    async def process_combined_directory(
        self, group_dir: str, combined_path: str, force: bool = False
    ) -> bool:
        """
        Process a combined directory for match information using NTFY.

        Args:
            group_dir: Directory path
            combined_path: Path to combined video
            force: Force processing even if already done

        Returns:
            True if processing was successful or initiated, False otherwise
        """
        logger.info(f"=== NTFY processing combined directory: {group_dir} ===")

        # Check if already processed
        if not force and self.has_been_processed(group_dir):
            logger.info(f"Directory {group_dir} already processed, skipping")
            return True

        # Check if already waiting for input
        if self.is_waiting_for_input(group_dir):
            logger.info(f"Already waiting for input for {group_dir}")
            return True

        try:
            # Initialize API if needed
            if not await self._ensure_initialized():
                logger.error("Failed to initialize NTFY API")
                return False

            # First ask if there was a match during the recording period
            success = await self.request_was_there_a_match(group_dir, combined_path)

            if success:
                logger.info(f"Successfully initiated NTFY processing for {group_dir}")
                return True
            else:
                logger.error(f"Failed to initiate NTFY processing for {group_dir}")
                return False

        except Exception as e:
            logger.error(
                f"Error in NTFY process_combined_directory for {group_dir}: {e}"
            )
            return False

    async def request_was_there_a_match(self, group_dir: str, video_file: str) -> bool:
        """
        Request confirmation if there was a match during the recording period.

        Args:
            group_dir: Directory containing the video group
            video_file: Name of the video file

        Returns:
            True if the request was successful, False otherwise
        """
        if not await self._ensure_initialized():
            logger.error("NTFY service not initialized")
            return False

        try:
            # Mark as waiting for input
            self.mark_waiting_for_input(
                group_dir, "was_there_a_match", {"combined_video_path": video_file}
            )

            # Create and execute the was there a match task
            from ..tasks.ntfy import NtfyTaskFactory

            task = NtfyTaskFactory.create_was_there_a_match_task(
                group_dir=group_dir,
                config=self.config,
                ntfy_service=self,
                combined_video_path=video_file,
            )

            if task:
                # Execute the task to send the notification
                question_data = await task.create_question()
                if question_data:
                    success = await self.send_notification(
                        message=question_data["message"],
                        title=question_data["title"],
                        tags=question_data["tags"],
                        priority=question_data["priority"],
                        actions=question_data["actions"],
                    )

                    if success:
                        logger.info(f"Was there a match request sent for {group_dir}")
                        return True
                    else:
                        logger.warning(
                            f"Failed to send was there a match request for {group_dir}"
                        )
                        return False
                else:
                    logger.warning(
                        f"No question data generated for was there a match task for {group_dir}"
                    )
                    return False
            else:
                logger.error(f"Failed to create was there a match task for {group_dir}")
                return False

        except Exception as e:
            logger.error(f"Error requesting was there a match for {group_dir}: {e}")
            return False

    async def request_team_info(self, group_dir: str, video_file: str) -> bool:
        """
        Request team information for a video file.

        Args:
            group_dir: Directory containing the video group
            video_file: Name of the video file

        Returns:
            True if the request was successful, False otherwise
        """
        if not await self._ensure_initialized():
            logger.error("NTFY service not initialized")
            return False

        try:
            result = await self.ntfy_api.ask_team_info(group_dir, video_file)
            if result:
                logger.info(f"Team info request successful for {group_dir}")
                return True
            else:
                logger.warning(f"Team info request failed for {group_dir}")
                return False
        except Exception as e:
            logger.error(f"Error requesting team info for {group_dir}: {e}")
            return False

    async def request_playlist_name(self, group_dir: str, team_name: str) -> bool:
        """
        Request playlist name for a team.

        Args:
            group_dir: Directory containing the video group
            team_name: Name of the team

        Returns:
            True if the request was successful, False otherwise
        """
        if not await self._ensure_initialized():
            logger.error("NTFY service not initialized")
            return False

        try:
            # Mark as waiting for input
            self.mark_waiting_for_input(
                group_dir, "playlist_name", {"team_name": team_name}
            )

            # Send notification requesting playlist name
            result = await self.send_notification(
                f"Please provide YouTube playlist name for {team_name}",
                title="YouTube Playlist Request",
            )

            if result:
                logger.info(f"Playlist name request sent for {group_dir}")
                return True
            else:
                logger.warning(f"Playlist name request failed for {group_dir}")
                return False
        except Exception as e:
            logger.error(f"Error requesting playlist name for {group_dir}: {e}")
            return False

    def register_response_handler(self, key: str, handler) -> None:
        """Register a handler for non-task responses.

        When a response contains the key (case-insensitive), the handler
        is called with the response text.  Handlers are checked before
        the task-based routing in process_response().
        """
        self._response_handlers[key] = handler

    def unregister_response_handler(self, key: str) -> None:
        """Remove a previously registered response handler."""
        self._response_handlers.pop(key, None)

    async def process_response(
        self, response: str, server_time: float | None = None
    ) -> None:
        """
        Process a response from NTFY and route it to the appropriate pending task.

        Prefers the most recently notified group_dir to avoid routing responses
        to stale pending tasks from previous runs.

        Args:
            response: The user's response message
            server_time: NTFY server receive time for this response (epoch
                seconds), when available. Used to reject stale replays when the
                response has to be buffered. None for synthetic/internal
                responses (e.g. the mock API or registered response handlers).
        """
        logger.info(f"Processing NTFY response: {response}")

        # Check generic response handlers first (e.g. home recording deletion)
        response_lower = response.lower()
        for key, handler in list(self._response_handlers.items()):
            if key.lower() in response_lower:
                logger.info(f"Matched response handler: {key}")
                try:
                    await handler(response)
                except Exception as e:
                    logger.error(f"Error in response handler '{key}': {e}")
                return

        # Try the most recently notified group first
        if self._last_notified_group_dir:
            task_data = self._pending_tasks.get(self._last_notified_group_dir)
            if task_data and task_data.get("status") == "waiting_for_input":
                task_type = task_data.get("task_type")
                metadata = task_data.get("task_metadata", {})
                if self._response_matches_task(
                    self._last_notified_group_dir, task_type, metadata, response
                ):
                    logger.info(
                        f"Found matching task for response: {task_type} in {self._last_notified_group_dir} (most recent notification)"
                    )
                    await self._process_task_response(
                        self._last_notified_group_dir, task_type, metadata, response
                    )
                    return

        # Fall back to iterating all pending tasks (sorted by sent_at descending)
        sorted_tasks = sorted(
            self._pending_tasks.items(),
            key=lambda x: x[1].get("sent_at", ""),
            reverse=True,
        )
        for group_dir, task_data in sorted_tasks:
            task_type = task_data.get("task_type")
            metadata = task_data.get("task_metadata", {})
            status = task_data.get("status")
            if status != "waiting_for_input":
                continue
            if self._response_matches_task(group_dir, task_type, metadata, response):
                logger.info(
                    f"Found matching task for response: {task_type} in {group_dir}"
                )
                await self._process_task_response(
                    group_dir, task_type, metadata, response
                )
                return
        # No task currently waiting for this response. Buffer it — a task
        # registration may be moments away (state_auditor and the response
        # listener both run on startup; the listener can replay an old
        # tap before mark_waiting_for_input completes). When the task
        # finally registers, _try_buffered_responses_for replays this
        # against it. See the buffer's docstring on __init__.
        import time

        self._prune_unmatched_buffer()
        self._unmatched_responses.append((time.monotonic(), server_time, response))
        logger.warning(
            f"No matching task found for response: {response} "
            f"(buffered {len(self._unmatched_responses)} unmatched responses, "
            f"will retry when next task registers)"
        )
        logger.debug(f"Current pending tasks: {self._pending_tasks}")

    def _prune_unmatched_buffer(self) -> None:
        """Drop unmatched responses older than the TTL."""
        import time

        cutoff = time.monotonic() - self._unmatched_response_ttl_seconds
        while self._unmatched_responses and self._unmatched_responses[0][0] < cutoff:
            self._unmatched_responses.popleft()

    @staticmethod
    def _is_buffered_response_fresh(
        server_time: float | None,
        received_monotonic: float,
        now_monotonic: float,
        task_sent_at_epoch: float | None,
    ) -> bool:
        """Decide whether a buffered response is fresh enough to answer a task.

        A response carries no group_dir, so we use freshness relative to the
        task's registration time as an identity proxy. A response replays only
        if it plausibly arrived AS an answer to the just-registered task:

        - With an NTFY server timestamp (the normal phone-tap path), the tap
          must have happened no earlier than BUFFER_FRESHNESS_WINDOW_SECONDS
          before the task was registered. This rejects a tap replayed from a
          previous game by ?since=24h on restart, while still accepting the
          genuine sub-second race (the tap arrives microseconds before
          mark_waiting_for_input finishes). If the task's sent_at can't be
          parsed, fall back to the monotonic recency check below.
        - Without a usable server timestamp (synthetic/internal responses),
          only the genuine just-happened race qualifies: the entry must have
          been received within BUFFER_RACE_WINDOW_SECONDS (monotonic). We never
          replay an older entry on this weak signal.
        """
        if server_time is not None and task_sent_at_epoch is not None:
            return server_time >= task_sent_at_epoch - BUFFER_FRESHNESS_WINDOW_SECONDS
        return (now_monotonic - received_monotonic) <= BUFFER_RACE_WINDOW_SECONDS

    async def _try_buffered_responses_for(self, group_dir: str) -> bool:
        """Replay buffered unmatched responses against a freshly-registered task.

        A buffered response is only replayed when it both matches the task type
        AND is fresh relative to the task's registration time (see
        _is_buffered_response_fresh). This prevents a stale answer from a
        previous game — replayed by the listener's ?since=24h reconnect — from
        auto-answering a newly-registered question for a different game.

        Returns True if a buffered response matched and was processed.
        """
        import time

        self._prune_unmatched_buffer()
        if not self._unmatched_responses:
            return False
        task_data = self._pending_tasks.get(group_dir)
        if not task_data or task_data.get("status") != "waiting_for_input":
            return False
        task_type = task_data.get("task_type")
        metadata = task_data.get("task_metadata", {})

        # Parse the task's registration time (epoch seconds) for the freshness
        # check. Always written as ISO format by mark_waiting_for_input.
        task_sent_at_epoch = None
        sent_at_raw = task_data.get("sent_at")
        if sent_at_raw:
            try:
                task_sent_at_epoch = datetime.fromisoformat(sent_at_raw).timestamp()
            except (ValueError, TypeError):
                task_sent_at_epoch = None

        now_monotonic = time.monotonic()

        # Iterate a snapshot so we can mutate the deque safely.
        snapshot = list(self._unmatched_responses)
        for received_at, server_time, response in snapshot:
            if not self._response_matches_task(
                group_dir, task_type, metadata, response
            ):
                continue
            if not self._is_buffered_response_fresh(
                server_time, received_at, now_monotonic, task_sent_at_epoch
            ):
                logger.info(
                    f"NTFY: NOT replaying stale buffered response '{response}' "
                    f"against task {task_type} for {group_dir} "
                    f"(server_time={server_time}, task sent_at={sent_at_raw}) — "
                    f"it predates this question, so it belongs to another game; "
                    f"leaving it buffered to age out."
                )
                continue
            logger.info(
                f"NTFY: Replaying buffered response '{response}' against "
                f"newly-registered task {task_type} for {group_dir}"
            )
            # Remove from buffer first so we don't loop on it.
            try:
                self._unmatched_responses.remove((received_at, server_time, response))
            except ValueError:
                pass
            await self._process_task_response(group_dir, task_type, metadata, response)
            return True
        return False

    def _response_matches_task(
        self, group_dir: str, input_type: str, metadata: dict[str, Any], response: str
    ) -> bool:
        """
        Check if a response matches a specific task.

        Args:
            group_dir: Directory associated with the task
            input_type: Type of input the task is waiting for
            metadata: Task metadata
            response: The user's response

        Returns:
            True if the response matches the task
        """
        from ..tasks.ntfy.enums import NtfyInputType

        logger.debug(
            f"Checking if response '{response}' matches task {input_type} in {group_dir}"
        )

        response_lower = response.lower()

        # Check for question type in response
        if input_type == NtfyInputType.GAME_START_TIME.value:
            # Accept response patterns from GameStartTask action buttons (with or without time)
            valid_patterns = ["yes, game started at", "no, not yet at", "not a game at"]
            # Convert both patterns and response to lowercase for case-insensitive matching
            response_lower = response.lower()
            # Check if any pattern matches the response (patterns may be followed by time)
            matches = any(pattern in response_lower for pattern in valid_patterns)
            logger.debug(
                f"Game start time check: valid_patterns={valid_patterns}, response_lower={response_lower}, matches={matches}"
            )
            return matches
        elif input_type == NtfyInputType.GAME_END_TIME.value:
            # Check for game end time patterns
            valid_patterns = ["yes, the game ended", "no, not yet", "not a game"]
            matches = any(pattern in response_lower for pattern in valid_patterns)
            logger.debug(
                f"Game end time check: valid_patterns={valid_patterns}, response_lower={response_lower}, matches={matches}"
            )
            return matches
        elif input_type == NtfyInputType.TEAM_INFO.value:
            # Accept any non-empty text as valid team info
            matches = len(response.strip()) > 0
            logger.debug(
                f"Team info check: response_lower={response_lower}, matches={matches}"
            )
            return matches
        elif input_type == NtfyInputType.PLAYLIST_NAME.value:
            # Accept any non-empty text as valid playlist name
            matches = len(response.strip()) > 0
            logger.debug(
                f"Playlist name check: response_lower={response_lower}, matches={matches}"
            )
            return matches
        elif input_type == NtfyInputType.WAS_THERE_A_MATCH.value:
            # Check for yes/no responses (including action button responses)
            valid_patterns = [
                "yes, there was a match",
                "yes",
                "y",
                "true",
                "1",
                "no, there was no match",
                "no",
                "n",
                "false",
                "0",
            ]
            matches = any(pattern in response_lower for pattern in valid_patterns)
            logger.debug(
                f"Was there a match check: valid_patterns={valid_patterns}, response_lower={response_lower}, matches={matches}"
            )
            return matches
        else:
            logger.debug(f"Unknown input type: {input_type}")
            return False

    async def _process_task_response(
        self, group_dir: str, input_type: str, metadata: dict[str, Any], response: str
    ) -> None:
        """
        Process a response for a specific task.

        Args:
            group_dir: Directory associated with the task
            input_type: Type of input the task is waiting for
            metadata: Task metadata
            response: The user's response
        """
        try:
            # Create the task using the factory
            from ..tasks.ntfy import NtfyTaskFactory

            # Get the config from the metadata
            config = metadata.get("config")

            if not config:
                logger.error(
                    f"No configuration available for task {input_type} in {group_dir}"
                )
                return

            logger.info(
                f"Creating task of type {input_type} for {group_dir} with config: {config}"
            )

            # Convert dict config to Config object if needed
            if isinstance(config, dict):
                from video_grouper.utils.config import NtfyConfig

                # Extract NTFY config from the dict
                ntfy_config_data = config.get("ntfy", {})
                ntfy_config = NtfyConfig(**ntfy_config_data)

                # Create a minimal config with just the NTFY section
                class MinimalConfig:
                    def __init__(self, ntfy_config):
                        self.ntfy = ntfy_config

                config = MinimalConfig(ntfy_config)

            # Use the task's metadata for creating the task
            task = NtfyTaskFactory.create_task(
                input_type, group_dir, config, self, metadata
            )

            if not task:
                logger.error(
                    f"Could not create task of type {input_type} for {group_dir}"
                )
                return

            logger.info(
                f"Successfully created task of type {input_type} for {group_dir}"
            )

            # Process the response using the task's logic
            logger.info(
                f"Processing response '{response}' for task {input_type} in {group_dir}"
            )
            result = await task.process_response(response)

            if result.success:
                logger.info(
                    f"Task {input_type} completed successfully: {result.message}"
                )

                # If the task should continue, we might need to create follow-up tasks
                if result.should_continue:
                    logger.info(f"Task {input_type} should continue: {result.message}")

                    # Remove the current task from the queue since it's being replaced
                    if self.completion_callback:
                        logger.info(
                            f"Removing continuing task from queue: {input_type} for {group_dir}"
                        )
                        asyncio.create_task(
                            self.completion_callback(
                                group_dir, input_type, task_completed=True
                            )
                        )

                    # Handle different task types that should continue
                    if input_type == "was_there_a_match":
                        # User confirmed there was a match, continue to team info
                        logger.info(
                            f"User confirmed there was a match for {group_dir}, proceeding to team info"
                        )

                        # Clear the current task and start team info
                        self.clear_pending_task(group_dir)

                        # Get the combined video path from metadata
                        combined_video_path = metadata.get("combined_video_path")
                        if combined_video_path:
                            # Request team info
                            success = await self.request_team_info(
                                group_dir, combined_video_path
                            )
                            if success:
                                logger.info(
                                    f"Successfully initiated team info request for {group_dir}"
                                )
                            else:
                                logger.error(
                                    f"Failed to initiate team info request for {group_dir}"
                                )
                        else:
                            logger.error(
                                f"No combined_video_path in metadata for {group_dir}"
                            )

                    elif (
                        input_type == "team_info"
                        and result.metadata
                        and "next_field" in result.metadata
                    ):
                        from video_grouper.task_processors.tasks.ntfy.team_info_task import (
                            TeamInfoTask,
                        )

                        next_task = TeamInfoTask.create_next_task(
                            current_task=task,
                            next_field=result.metadata["next_field"],
                        )

                        logger.info(
                            f"Creating and executing next team info task for field {result.metadata['next_field']}"
                        )
                        await next_task.execute()

                    elif (
                        input_type == "game_start_time"
                        and result.metadata
                        and "next_time_offset" in result.metadata
                    ):
                        from video_grouper.task_processors.tasks.ntfy.game_start_task import (
                            GameStartTask,
                        )

                        # Create the next task with the updated time
                        next_task = GameStartTask.create_next_task(
                            current_task=task,
                            next_time_offset=result.metadata["next_time_offset"],
                            next_time_seconds=result.metadata["next_time_seconds"],
                        )

                        # Execute the next task immediately
                        logger.info(
                            f"Creating and executing next task for time {result.metadata['next_time_offset']}"
                        )
                        await next_task.execute()
                    else:
                        logger.info(
                            f"Task continuation not implemented for type {input_type}"
                        )

                # Mark task as completed and remove from pending inputs
                if not result.should_continue:
                    self.clear_pending_task(group_dir)
                    logger.info(
                        f"Task {input_type} completed and removed from pending inputs"
                    )

                    # Notify queue processor to remove task from queue
                    if self.completion_callback:
                        logger.info(
                            f"Notifying queue processor to remove completed task: {input_type} for {group_dir}"
                        )
                        # Call the callback asynchronously to avoid blocking
                        asyncio.create_task(
                            self.completion_callback(
                                group_dir, input_type, task_completed=True
                            )
                        )

                    # Check if match info is now complete by calling the processor's callback
                    if self.completion_callback:
                        await self.completion_callback(
                            group_dir, None, task_completed=False
                        )

            else:
                logger.error(f"Task {input_type} failed: {result.message}")

        except Exception as e:
            logger.error(
                f"Error processing response for task {input_type} in {group_dir}: {e}"
            )
