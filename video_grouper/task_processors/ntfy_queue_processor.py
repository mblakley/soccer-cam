"""
NTFY Queue Processor for handling NTFY questions and responses.

This processor acts as the central coordinator for NTFY interactions:
1. Maintains a queue of tasks to ask users
2. Sends questions through NTFY and waits for responses
3. Processes responses using task-specific logic
4. Handles startup processing of existing pending requests
5. Listens for responses via NTFY subscription API
"""

import os
import logging
import asyncio
import json
import httpx
from typing import Dict, Any, Optional, List
from datetime import datetime
from dataclasses import dataclass, field

from .services.ntfy_service import NtfyService
from .ntfy import BaseNtfyTask, NtfyTaskFactory
from video_grouper.models import MatchInfo, DirectoryState
from .polling_processor_base import PollingProcessor
from video_grouper.utils.config import Config
from video_grouper.utils.youtube_upload import get_playlist_name_from_mapping

logger = logging.getLogger(__name__)


@dataclass
class NtfyTaskWrapper:
    """Wrapper for NTFY tasks with additional tracking information."""

    task_id: str
    task: BaseNtfyTask
    created_at: datetime = field(default_factory=datetime.now)
    sent_at: Optional[datetime] = None
    response: Optional[str] = None
    response_at: Optional[datetime] = None


class NtfyQueueProcessor(PollingProcessor):
    """
    Central coordinator for NTFY questions and responses.

    This processor:
    1. Maintains a queue of questions to ask users
    2. Sends questions through NTFY and waits for responses
    3. Processes responses and queues follow-up tasks
    4. Handles startup processing of existing pending requests
    """

    def __init__(
        self,
        storage_path: str,
        config: Config,
        ntfy_service: NtfyService,
        poll_interval: int = 30,
    ):
        """
        Initialize the NTFY queue processor.

        Args:
            storage_path: Path to storage directory
            config: Configuration object
            ntfy_service: NTFY service instance
            poll_interval: How often to check for responses (in seconds)
        """
        super().__init__(storage_path, config, poll_interval)
        self.ntfy_service = ntfy_service

        # References to other processors to queue work
        self.video_processor = None

        # Task queue and tracking
        self._task_queue: List[NtfyTaskWrapper] = []
        self._sent_tasks: Dict[str, NtfyTaskWrapper] = {}
        self._pending_responses: Dict[str, asyncio.Event] = {}
        self._response_data: Dict[str, Optional[str]] = {}
        self._task_counter = 0

        # Response listener
        self._listener_task: Optional[asyncio.Task] = None
        self._listener_running = False
        self._listener_client: Optional[httpx.AsyncClient] = None

        # State tracking
        self._stopping = False

    def set_video_processor(self, video_processor):
        """Set reference to video processor to queue work."""
        self.video_processor = video_processor

    async def start(self) -> None:
        """Start the NTFY queue processor."""
        logger.info("Starting NTFY Queue Processor")

        # Start the response listener first
        await self._start_response_listener()

        # Process any pending requests from startup
        await self._process_pending_requests_on_startup()

        # Start the main polling loop
        await super().start()

    async def discover_work(self) -> None:
        """
        Check for new tasks to send and process any completed tasks.
        This is the main work of the NTFY queue processor.
        """
        logger.debug("NTFY_QUEUE: Checking for new tasks to send")

        try:
            # Send any pending tasks
            await self._send_pending_tasks()

            # Check for completed tasks and process them
            await self._process_completed_tasks()

        except Exception as e:
            logger.error(f"NTFY_QUEUE: Error during work discovery: {e}")

    async def add_task(self, task: BaseNtfyTask) -> str:
        """
        Add a task to the queue.

        Args:
            task: The NTFY task to add

        Returns:
            Task ID
        """
        self._task_counter += 1
        task_id = f"{task.get_task_type()}_{self._task_counter}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

        task_wrapper = NtfyTaskWrapper(task_id=task_id, task=task)

        self._task_queue.append(task_wrapper)

        # Mark as waiting for input in the NTFY service (this saves state to file)
        self.ntfy_service.mark_waiting_for_input(
            task.group_dir,
            f"{task.get_task_type()}_queued",
            {
                "task_id": task_id,
                "task_type": task.get_task_type(),
                "metadata": task.metadata,
                "status": "queued",
            },
        )

        logger.info(
            f"Added task to queue: {task_id} ({task.get_task_type()}) for {task.group_dir}"
        )

        return task_id

    async def _send_pending_tasks(self) -> None:
        """Send any pending tasks in the queue."""
        if not self._task_queue:
            return

        # Only send a task if we don't have any sent tasks waiting for responses
        if self._sent_tasks:
            logger.debug(
                f"Waiting for responses to {len(self._sent_tasks)} sent tasks before sending more"
            )
            return

        # Send one task at a time to avoid overwhelming the user
        task_wrapper = self._task_queue.pop(0)

        try:
            logger.info(f"Sending task: {task_wrapper.task_id}")

            # Create the question data from the task
            question_data = await task_wrapper.task.create_question()

            if not question_data:
                logger.warning(
                    f"Task {task_wrapper.task_id} returned empty question data, skipping"
                )
                return

            # Send the notification via NTFY
            success = await self.ntfy_service.ntfy_api.send_notification(
                message=question_data["message"],
                title=question_data["title"],
                tags=question_data["tags"],
                priority=question_data["priority"],
                image_path=question_data.get("image_path"),
                actions=question_data.get("actions", []),
            )

            if success:
                task_wrapper.sent_at = datetime.now()
                self._sent_tasks[task_wrapper.task_id] = task_wrapper

                # Update the state to "sent" (this saves state to file)
                self.ntfy_service.mark_waiting_for_input(
                    task_wrapper.task.group_dir,
                    task_wrapper.task.get_task_type(),
                    {
                        "task_id": task_wrapper.task_id,
                        "task_type": task_wrapper.task.get_task_type(),
                        "metadata": task_wrapper.task.metadata,
                        "status": "sent",
                        "sent_at": task_wrapper.sent_at.isoformat(),
                        "image_path": question_data.get("image_path"),
                    },
                )

                logger.info(f"Successfully sent task: {task_wrapper.task_id}")
                logger.info(
                    f"Waiting for response before sending next task. Queue has {len(self._task_queue)} tasks remaining"
                )
            else:
                # Put the task back in the queue to retry later
                self._task_queue.insert(0, task_wrapper)
                logger.warning(
                    f"Failed to send task: {task_wrapper.task_id}, will retry"
                )

        except Exception as e:
            logger.error(f"Error sending task {task_wrapper.task_id}: {e}")
            # Put the task back in the queue to retry later
            self._task_queue.insert(0, task_wrapper)

    async def _process_responses(self) -> None:
        """Process responses from the NTFY service."""
        # This method is no longer needed since we're using the subscription API
        pass

    async def _process_completed_tasks(self) -> None:
        """Process tasks that have received responses."""
        completed_tasks = [
            t for t in self._sent_tasks.values() if t.response is not None
        ]

        for task_wrapper in completed_tasks:
            try:
                logger.info(f"Processing completed task: {task_wrapper.task_id}")

                # Process the response using the task's logic
                result = await task_wrapper.task.process_response(
                    task_wrapper.response or ""
                )

                if result.success:
                    logger.info(
                        f"Task {task_wrapper.task_id} completed successfully: {result.message}"
                    )

                    # If the task should continue, create a new task for the next iteration
                    if result.should_continue and result.metadata:
                        if task_wrapper.task.get_task_type() == "game_start_time":
                            # For game start tasks, create the next task in the sequence
                            next_time_offset = result.metadata.get("next_time_offset")
                            next_time_seconds = result.metadata.get("next_time_seconds")

                            if next_time_offset and next_time_seconds is not None:
                                from .ntfy import GameStartTask

                                next_task = GameStartTask.create_next_task(
                                    task_wrapper.task,
                                    next_time_offset,
                                    next_time_seconds,
                                )
                                await self.add_task(next_task)
                                logger.info(
                                    f"Created next game start task for {next_time_offset}"
                                )

                # Remove from sent tasks
                del self._sent_tasks[task_wrapper.task_id]

            except Exception as e:
                logger.error(
                    f"Error processing completed task {task_wrapper.task_id}: {e}"
                )

    async def _process_pending_requests_on_startup(self) -> None:
        """Process any pending NTFY requests on startup."""
        pending_inputs = self.ntfy_service.get_pending_inputs()

        if not pending_inputs:
            logger.info("No pending NTFY requests found on startup")
            return

        logger.info(f"Found {len(pending_inputs)} pending NTFY requests on startup")

        for group_dir, input_data in pending_inputs.items():
            input_type = input_data.get("input_type")
            metadata = input_data.get("metadata", {})
            status = metadata.get("status", "unknown")

            logger.info(
                f"Processing pending {input_type} request for {group_dir} (status: {status})"
            )

            if status == "queued":
                # Task was queued but not sent yet, recreate it
                task_type_str = input_type.replace("_queued", "")
                await self._recreate_queued_task(group_dir, task_type_str, metadata)

            elif status == "sent":
                # Task was sent but no response received, recreate it
                task_type_str = input_type.replace("_sent", "")
                await self._recreate_sent_task(group_dir, task_type_str, metadata)

            else:
                # New format required - fail if not in expected format
                logger.error(
                    f"Invalid pending input format for {group_dir}: status={status}, input_type={input_type}"
                )
                logger.error(
                    f"Expected status to be 'queued' or 'sent', got '{status}'"
                )
                # Clear the invalid pending input
                self.ntfy_service.clear_pending_input(group_dir)

    async def _recreate_queued_task(
        self, group_dir: str, task_type: str, metadata: Dict[str, Any]
    ) -> None:
        """Recreate a task that was queued but not sent."""
        logger.info(f"Recreating queued task for {group_dir}: {task_type}")

        # Recreate the task using the task factory
        task = NtfyTaskFactory.create_task(
            task_type, group_dir, metadata.get("metadata", {})
        )
        if task:
            await self.add_task(task)
        else:
            logger.warning(f"Could not recreate task of type: {task_type}")

    async def _recreate_sent_task(
        self, group_dir: str, task_type: str, metadata: Dict[str, Any]
    ) -> None:
        """Recreate a task that was sent but no response received."""
        logger.info(f"Recreating sent task for {group_dir}: {task_type}")

        # Recreate the task and mark it as sent
        task = NtfyTaskFactory.create_task(
            task_type, group_dir, metadata.get("metadata", {})
        )
        if task:
            task_id = metadata.get("task_id")
            if task_id:
                task_wrapper = NtfyTaskWrapper(task_id=task_id, task=task)

                # Mark as sent
                sent_at_str = metadata.get("sent_at")
                if sent_at_str:
                    try:
                        task_wrapper.sent_at = datetime.fromisoformat(sent_at_str)
                    except ValueError:
                        task_wrapper.sent_at = datetime.now()
                else:
                    task_wrapper.sent_at = datetime.now()

                self._sent_tasks[task_id] = task_wrapper
                logger.info(f"Recreated sent task: {task_id}")
            else:
                # No task ID, just add to queue
                await self.add_task(task)
        else:
            logger.warning(f"Could not recreate task of type: {task_type}")

    async def _check_match_info_completion(self, group_dir: str) -> None:
        """Check if match info has been populated for a directory."""
        match_info_path = os.path.join(group_dir, "match_info.ini")
        if not os.path.exists(match_info_path):
            return

        match_info = MatchInfo.from_file(match_info_path)
        if match_info and match_info.is_populated():
            # User has populated the match info, mark as processed
            logger.info(f"Match info populated for {group_dir}, marking as processed")
            self.ntfy_service.mark_as_processed(group_dir)

            # Queue trim task if we have a combined video
            combined_path = os.path.join(group_dir, "combined.mp4")
            if os.path.exists(combined_path) and self.video_processor:
                from .tasks.video import TrimTask

                await self.video_processor.add_work(
                    TrimTask.from_match_info(group_dir, match_info)
                )
                logger.info(f"Queued trim task for {group_dir}")

    async def _check_playlist_name_completion(
        self, group_dir: str, info: Dict[str, Any]
    ) -> None:
        """Check if playlist name has been provided for a directory."""
        team_name = info.get("team_name")
        if not team_name:
            return

        # Check if the config has been updated with the playlist name
        playlist_name = get_playlist_name_from_mapping(team_name, self.config)
        if playlist_name:
            logger.info(
                f"Playlist name found for {team_name}, clearing pending request"
            )
            self.ntfy_service.clear_pending_input(group_dir)

            # Update the directory state with the playlist name
            dir_state = DirectoryState(group_dir)
            dir_state.set_youtube_playlist_name(playlist_name)
            logger.info(f"Updated directory state with playlist name: {playlist_name}")

    async def stop(self) -> None:
        """Stop the NTFY queue processor."""
        logger.info("Stopping NTFY Queue Processor")
        self._stopping = True

        # Stop the response listener
        await self._stop_response_listener()

        # Stop the main polling loop
        await super().stop()

    async def request_match_info_for_directory(
        self, group_dir: str, combined_video_path: str, force: bool = False
    ) -> bool:
        """
        Request match info for a combined directory.

        Args:
            group_dir: Directory path
            combined_video_path: Path to combined video
            force: Force request even if already processed

        Returns:
            True if tasks were added to queue, False otherwise
        """
        logger.info(f"NTFY_QUEUE: Requesting match info for {group_dir}")

        # Check if already processed or waiting
        if not force:
            if self.ntfy_service.has_been_processed(group_dir):
                logger.info(
                    f"NTFY_QUEUE: Directory {group_dir} already processed, skipping"
                )
                return False

            if self.ntfy_service.is_waiting_for_input(group_dir):
                logger.info(
                    f"NTFY_QUEUE: Already waiting for input for {group_dir}, skipping"
                )
                return False

        # Check if match info is already populated
        match_info, _ = MatchInfo.get_or_create(group_dir)

        if not force and match_info and match_info.is_populated():
            logger.info(f"NTFY_QUEUE: Match info already populated for {group_dir}")
            self.ntfy_service.mark_as_processed(group_dir)
            return False

        # Get existing team info
        existing_info = {}
        if match_info:
            existing_info = match_info.get_team_info()

        tasks_added = False

        # Determine what team information is missing
        missing_fields = []
        if "team_name" not in existing_info and "my_team_name" not in existing_info:
            missing_fields.append("team name")
        if (
            "opponent_name" not in existing_info
            and "opponent_team_name" not in existing_info
        ):
            missing_fields.append("opponent team name")
        if "location" not in existing_info:
            missing_fields.append("game location")

        # Add team info task if needed
        if missing_fields:
            from .ntfy import TeamInfoTask

            task = TeamInfoTask(group_dir, combined_video_path, existing_info)
            await self.add_task(task)
            tasks_added = True
            logger.info(f"Added team info task for {group_dir}")

        # Add game start time task
        from .ntfy import GameStartTask

        start_task = GameStartTask(group_dir, combined_video_path, 0, "00:00")
        await self.add_task(start_task)
        tasks_added = True

        # Check if we should also ask for end time
        if match_info and match_info.start_time_offset:
            from .ntfy import GameEndTask

            end_task = GameEndTask(
                group_dir, combined_video_path, match_info.start_time_offset
            )
            await self.add_task(end_task)
            tasks_added = True

        return tasks_added

    async def _get_video_duration(self, video_path: str) -> Optional[int]:
        """Get video duration in seconds."""
        try:
            from video_grouper.utils.ffmpeg_utils import get_video_duration

            duration = await get_video_duration(video_path)

            # Convert duration to seconds if it's a string
            if isinstance(duration, str):
                try:
                    parts = list(map(int, duration.split(":")))
                    if len(parts) == 3:
                        return parts[0] * 3600 + parts[1] * 60 + parts[2]
                    elif len(parts) == 2:
                        return parts[0] * 60 + parts[1]
                    else:
                        return int(duration)
                except Exception:
                    logger.error(f"Could not parse duration string: {duration}")
                    return None

            return duration
        except Exception as e:
            logger.error(f"Error getting video duration for {video_path}: {e}")
            return None

    async def _generate_screenshot(
        self, video_path: str, time_seconds: int
    ) -> Optional[str]:
        """Generate a screenshot at the specified time and return the path."""
        try:
            from video_grouper.utils.ffmpeg_utils import create_screenshot
            from datetime import timedelta

            # Convert seconds to time string
            time_str = str(timedelta(seconds=time_seconds)).split(".")[0]

            # Create temporary screenshot path
            formatted_datetime = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = os.path.join(
                os.path.dirname(video_path),
                f"temp_screenshot_{time_seconds}_{formatted_datetime}.jpg",
            )

            # Create screenshot
            screenshot_created = await create_screenshot(
                video_path, screenshot_path, time_offset=time_str
            )

            if screenshot_created:
                # Compress the screenshot to reduce file size
                compressed_path = await self._compress_image(
                    screenshot_path,
                    quality=60,  # Medium quality (0-100)
                    max_width=800,  # Reasonable width for mobile devices
                )

                # Clean up the original screenshot if compression created a new file
                if compressed_path != screenshot_path and os.path.exists(
                    screenshot_path
                ):
                    try:
                        os.remove(screenshot_path)
                    except Exception as e:
                        logger.warning(
                            f"Failed to remove original screenshot {screenshot_path}: {e}"
                        )

                return compressed_path
            else:
                logger.warning(
                    f"Failed to create screenshot at {time_str} for {video_path}"
                )
                return None

        except Exception as e:
            logger.error(
                f"Error generating screenshot for {video_path} at {time_seconds}s: {e}"
            )
            return None

    async def _compress_image(
        self,
        input_path: str,
        output_path: Optional[str] = None,
        quality: int = 60,
        max_width: int = 800,
    ) -> str:
        """
        Compress an image to reduce file size.

        Args:
            input_path: Path to the input image
            output_path: Path to save the compressed image (if None, will overwrite the input)
            quality: JPEG quality (0-100, lower means more compression)
            max_width: Maximum width of the output image

        Returns:
            Path to the compressed image
        """
        if not os.path.exists(input_path):
            logger.error(f"Input image not found: {input_path}")
            return input_path

        if output_path is None:
            # Create a temporary path with _compressed suffix
            from pathlib import Path

            path_obj = Path(input_path)
            output_path = str(path_obj.with_stem(f"{path_obj.stem}_compressed"))

        try:
            # Use ffmpeg to compress the image
            import subprocess

            cmd = [
                "ffmpeg",
                "-i",
                input_path,
                "-vf",
                f"scale='min({max_width},iw)':-1",  # Scale down if larger than max_width
                "-q:v",
                str(quality // 10),  # Convert quality to ffmpeg scale (0-10)
                "-y",  # Overwrite output file if it exists
                output_path,
            ]

            # Run the command
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            stdout, stderr = process.communicate()

            if process.returncode != 0:
                logger.error(f"Error compressing image: {stderr.decode()}")
                return input_path

            # Check if compression actually reduced the file size
            original_size = os.path.getsize(input_path)
            compressed_size = os.path.getsize(output_path)

            if compressed_size >= original_size:
                logger.info(
                    f"Compression did not reduce file size: {original_size} -> {compressed_size} bytes"
                )
                os.remove(output_path)
                return input_path

            logger.info(
                f"Successfully compressed image: {original_size} -> {compressed_size} bytes ({int(100 - compressed_size / original_size * 100)}% reduction)"
            )
            return output_path

        except Exception as e:
            logger.error(f"Error during image compression: {e}")
            return input_path

    async def _start_response_listener(self) -> None:
        """Start listening for NTFY responses."""
        if self._listener_running:
            logger.info("Response listener already running")
            return

        logger.info("Starting NTFY response listener")
        self._listener_running = True
        self._listener_task = asyncio.create_task(self._listen_for_responses())

    async def _stop_response_listener(self) -> None:
        """Stop listening for NTFY responses."""
        if not self._listener_running:
            return

        logger.info("Stopping NTFY response listener")
        self._listener_running = False

        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass

        if self._listener_client:
            await self._listener_client.aclose()

    async def _listen_for_responses(self) -> None:
        """Listen for responses from the NTFY topic."""
        topic = self.ntfy_service.ntfy_api.topic
        base_url = self.ntfy_service.ntfy_api.base_url

        logger.info(f"NTFY API topic: {topic}")
        logger.info(f"NTFY API base_url: {base_url}")
        logger.info(f"NTFY API enabled: {self.ntfy_service.ntfy_api.enabled}")

        if not topic or not base_url:
            logger.error("Cannot start response listener - missing topic or base URL")
            return

        if not self.ntfy_service.ntfy_api.enabled:
            logger.error("Cannot start response listener - NTFY API not enabled")
            return

        url = f"{base_url}/{topic}/json"
        retry_count = 0
        max_retries = 10
        retry_delay = 3

        while not self._stopping:
            try:
                # Create a new client for each connection attempt
                logger.info("Creating new HTTP client for NTFY response listener")
                async with httpx.AsyncClient(timeout=None) as client:
                    logger.info(f"Starting NTFY response listener for topic: {topic}")

                    # Log the request
                    logger.info(f"Sending GET request to {url}")
                    async with client.stream("GET", url) as response:
                        logger.info(
                            f"Connected to NTFY stream: {url} with status {response.status_code}"
                        )

                        if response.status_code != 200:
                            logger.error(
                                f"Failed to subscribe to NTFY topic: {response.status_code}"
                            )
                            response_text = await response.text()
                            logger.error(f"Response text: {response_text}")
                            break

                        # Reset retry count on successful connection
                        retry_count = 0

                        # Process the stream line by line
                        async for line in response.aiter_lines():
                            if self._stopping:
                                break

                            # Log every line received, even empty ones
                            logger.info(f"NTFY stream raw line: {line}")

                            if not line.strip():
                                logger.debug("Empty line received from NTFY stream")
                                continue

                            try:
                                data = json.loads(line)
                                logger.info(
                                    f"Processing NTFY message: {json.dumps(data)[:500]}"
                                )
                                await self._handle_message_response(data)
                            except json.JSONDecodeError:
                                logger.error(
                                    f"Failed to parse NTFY response: {line[:100]}..."
                                )

            except httpx.ReadTimeout as e:
                logger.warning(f"Read timeout in NTFY response listener: {e}")
                # For read timeouts, just reconnect without increasing retry count
                await asyncio.sleep(1)
                continue

            except httpx.ConnectTimeout as e:
                retry_count += 1
                logger.error(
                    f"Connection timeout in NTFY response listener (attempt {retry_count}/{max_retries}): {e}"
                )

                if retry_count > max_retries:
                    logger.error("Max retries exceeded for NTFY response listener")
                    # Don't break, keep trying with longer delays
                    await asyncio.sleep(60)  # Wait a minute before trying again
                    retry_count = max_retries // 2  # Reset retry count partially
                else:
                    # Exponential backoff
                    wait_time = retry_delay * (2 ** (retry_count - 1))
                    logger.info(f"Retrying NTFY connection in {wait_time} seconds")
                    await asyncio.sleep(wait_time)

            except (httpx.RequestError, httpx.HTTPStatusError) as e:
                retry_count += 1
                logger.error(
                    f"HTTP error in NTFY response listener (attempt {retry_count}/{max_retries}): {e}"
                )

                if retry_count > max_retries:
                    logger.error("Max retries exceeded for NTFY response listener")
                    # Don't break, keep trying with longer delays
                    await asyncio.sleep(60)  # Wait a minute before trying again
                    retry_count = max_retries // 2  # Reset retry count partially
                else:
                    # Exponential backoff
                    wait_time = retry_delay * (2 ** (retry_count - 1))
                    logger.info(f"Retrying NTFY connection in {wait_time} seconds")
                    await asyncio.sleep(wait_time)

            except asyncio.CancelledError:
                logger.info("NTFY response listener task cancelled")
                break

            except Exception as e:
                logger.error(
                    f"Unexpected error in NTFY response listener: {e}", exc_info=True
                )
                await asyncio.sleep(10)  # Wait before retrying

        logger.info("NTFY response listener stopped")

    async def _handle_message_response(self, data: Dict[str, Any]) -> None:
        """Handle a message response from NTFY."""
        message = data.get("message", "")
        title = data.get("title", "")
        tags = data.get("tags", [])
        message_id = data.get("id", "")

        logger.info(f"Received NTFY message: {message}")
        logger.info(f"Title: {title}")
        logger.info(f"Tags: {tags}")
        logger.info(f"Message ID: {message_id}")

        # Check if this is a message we just sent (not a response)
        # We can identify this by checking if the message content matches any of our sent tasks
        for task_id, task_wrapper in self._sent_tasks.items():
            # Create question data to compare
            try:
                question_data = await task_wrapper.task.create_question()
                if (
                    question_data.get("message") == message
                    and question_data.get("title") == title
                    and task_wrapper.sent_at
                    and (datetime.now() - task_wrapper.sent_at).total_seconds() < 60
                ):  # Within last minute
                    logger.info(
                        f"Ignoring message we just sent: {message_id} (task: {task_id})"
                    )
                    return
            except Exception as e:
                logger.warning(f"Error checking if message matches task {task_id}: {e}")
                continue

        # Check for responses to our questions (case insensitive)
        lower_message = message.lower()

        # Log all incoming messages
        logger.info(f"Processing message content: '{message}'")

        # Check for game started/ended responses
        if any(
            keyword in lower_message
            for keyword in ["yes", "game started", "game ended", "started", "ended"]
        ):
            logger.info(f"Detected YES response: '{message}'")
            await self._handle_response(message)
        elif any(keyword in lower_message for keyword in ["no", "not yet", "continue"]):
            logger.info(f"Detected NO response: '{message}'")
            await self._handle_response(message)
        elif any(keyword in lower_message for keyword in ["not a game"]):
            logger.info(f"Detected NOT A GAME response: '{message}'")
            await self._handle_response(message)
        else:
            logger.info(f"Unhandled response message: {message}")

    async def _handle_response(self, response: str) -> None:
        """Handle a response to a message."""
        logger.info(f"Handling response: {response}")

        # Extract message ID from the response if present
        message_id = None
        if "(ID:" in response:
            try:
                message_id = response.split("(ID:")[1].split(")")[0].strip()
                logger.info(f"Extracted message ID: {message_id}")
            except Exception as e:
                logger.warning(f"Failed to extract message ID from response: {e}")

        # Find the task that matches this response
        if message_id:
            # Look for the specific task by message ID
            for task_id, task_wrapper in self._sent_tasks.items():
                if task_wrapper.task.metadata.get("message_id") == message_id:
                    logger.info(f"Found matching task by message ID: {task_id}")
                    await self._process_response(task_id, response)
                    return

        # If no message ID or no match found, try to find by response content
        for task_id, task_wrapper in list(self._sent_tasks.items()):
            task_type = task_wrapper.task.get_task_type()
            if task_type in ["game_start_time", "game_end_time"]:
                # Check if this response matches the task
                if self._response_matches_task(response, task_wrapper.task):
                    logger.info(f"Found matching task by content: {task_id}")
                    await self._process_response(task_id, response)
                    return

        logger.warning(f"No matching task found for response: {response}")

    def _response_matches_task(self, response: str, task: BaseNtfyTask) -> bool:
        """Check if a response matches a specific task."""
        response_lower = response.lower()

        # Check for time information in the response
        time_offset = task.metadata.get("time_offset")
        if time_offset is not None and time_offset in response:
            return True

        # Check for question type in response
        if task.get_task_type() == "game_start_time":
            return "start" in response_lower or "00:00" in response
        elif task.get_task_type() == "game_end_time":
            return "end" in response_lower

        return False

    async def _process_response(self, task_id: str, response: str) -> None:
        """Process a response to a specific task."""
        if task_id not in self._sent_tasks:
            logger.warning(f"Task {task_id} not found in sent tasks")
            return

        task_wrapper = self._sent_tasks[task_id]
        task_wrapper.response = response
        task_wrapper.response_at = datetime.now()

        logger.info(f"Processing response for task {task_id}: {response}")

        # The task will be processed in the next discovery cycle
        # by the _process_completed_tasks method
