"""
NTFY integration for video_grouper.

This module provides functionality to send notifications with screenshots to users
and receive responses to identify when a game starts and ends.
"""
import os
import logging
import json
import asyncio
import httpx
import uuid
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Tuple, Any, Set
import configparser
from pathlib import Path
from video_grouper.ffmpeg_utils import create_screenshot, get_video_duration
import subprocess

logger = logging.getLogger(__name__)

async def compress_image(input_path: str, output_path: Optional[str] = None, quality: int = 60, max_width: int = 800) -> str:
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
        path_obj = Path(input_path)
        output_path = str(path_obj.with_stem(f"{path_obj.stem}_compressed"))
        
    try:
        # Use ffmpeg to compress the image
        cmd = [
            "ffmpeg",
            "-i", input_path,
            "-vf", f"scale='min({max_width},iw)':-1",  # Scale down if larger than max_width
            "-q:v", str(quality // 10),  # Convert quality to ffmpeg scale (0-10)
            "-y",  # Overwrite output file if it exists
            output_path
        ]
        
        # Run the command
        process = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE
        )
        stdout, stderr = process.communicate()
        
        if process.returncode != 0:
            logger.error(f"Error compressing image: {stderr.decode()}")
            return input_path
            
        # Check if compression actually reduced the file size
        original_size = os.path.getsize(input_path)
        compressed_size = os.path.getsize(output_path)
        
        if compressed_size >= original_size:
            logger.info(f"Compression did not reduce file size: {original_size} -> {compressed_size} bytes")
            os.remove(output_path)
            return input_path
            
        logger.info(f"Successfully compressed image: {original_size} -> {compressed_size} bytes ({int(100 - compressed_size/original_size*100)}% reduction)")
        return output_path
        
    except Exception as e:
        logger.error(f"Error during image compression: {e}")
        return input_path

class NtfyAPI:
    """
    NTFY API integration for sending notifications with screenshots and receiving responses.
    """
    def __init__(self, config: Optional[configparser.ConfigParser] = None):
        """
        Initialize the NTFY API client.
        
        Args:
            config: ConfigParser object containing NTFY settings
        """
        self.enabled = False
        self.base_url = "https://ntfy.sh"
        self.topic = None
        self.client = None
        self.response_queue = asyncio.Queue()
        self.listener_task = None
        self.session_id = str(uuid.uuid4())[:8]  # Create a unique session ID
        
        # Track sent messages and their responses
        self.pending_messages = {}  # message_id -> future
        self.message_timestamps = {}  # message_id -> timestamp
        self.processed_screenshots = set()  # Set of screenshot paths that have been processed
        self.message_timeout = 300  # seconds (5 minutes)
        
        if config:
            self.configure(config)
    
    def configure(self, config: configparser.ConfigParser) -> bool:
        """
        Configure the NTFY API client from a ConfigParser object.
        
        Args:
            config: ConfigParser object containing NTFY settings
            
        Returns:
            bool: True if configuration was successful, False otherwise
        """
        if not config.has_section('NTFY'):
            logger.warning("No NTFY section in config, NTFY integration disabled")
            self.enabled = False
            return False
        
        self.enabled = config.getboolean('NTFY', 'enabled', fallback=False)
        if not self.enabled:
            logger.info("NTFY integration is disabled in config")
            return False
        
        self.base_url = config.get('NTFY', 'server_url', fallback="https://ntfy.sh")
        
        # Get topic name or generate a random one if not provided
        configured_topic = config.get('NTFY', 'topic', fallback=None)
        if configured_topic:
            self.topic = configured_topic
        else:
            # Generate a random topic name if not provided
            self.topic = f"soccer-cam-{self.session_id}"
            logger.info(f"Using auto-generated NTFY topic: {self.topic}")
        
        logger.info(f"NTFY integration configured with topic: {self.topic}")
        return True
    
    async def initialize(self):
        """Initialize the NTFY API integration."""
        if not self.enabled:
            logger.info("NTFY API integration not enabled")
            return
            
        logger.info(f"Initializing NTFY API with topic: {self.topic}")
        
        # Create a new HTTP client
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=None)
            
        # Start the response listener task
        self._response_listener_task = asyncio.create_task(self._listen_for_responses())
        
    async def close(self):
        """Close the NTFY API integration."""
        logger.info("Closing NTFY API integration")
        
        # Cancel the response listener task
        if self._response_listener_task:
            self._response_listener_task.cancel()
            try:
                await self._response_listener_task
            except asyncio.CancelledError:
                pass
            self._response_listener_task = None
            
        # Close the HTTP client
        if self.client:
            await self.client.aclose()
            self.client = None
            
    async def _listen_for_responses(self):
        """
        Listen for responses from the NTFY topic.
        This runs as a background task and puts responses into the queue.
        """
        if not self.enabled or not self.topic:
            logger.warning("Cannot listen for NTFY responses - integration not enabled")
            return
        
        url = f"{self.base_url}/{self.topic}/json"
        retry_count = 0
        max_retries = 10  # Increased from 5
        retry_delay = 3   # Reduced from 5
        
        while True:
            try:
                # Create a new client for each connection attempt
                logger.info(f"Creating new HTTP client for NTFY response listener")
                async with httpx.AsyncClient(timeout=None) as client:
                    logger.info(f"Starting NTFY response listener for topic: {self.topic}")
                    
                    # Log the request
                    logger.info(f"Sending GET request to {url}")
                    async with client.stream("GET", url) as response:
                        logger.info(f"Connected to NTFY stream: {url} with status {response.status_code}")
                        
                        # Reset retry count on successful connection
                        retry_count = 0
                        
                        # Process the stream line by line
                        async for line in response.aiter_lines():
                            # Log every line received, even empty ones
                            logger.info(f"NTFY stream raw line: {line}")
                            
                            if not line.strip():
                                logger.debug("Empty line received from NTFY stream")
                                continue
                                
                            try:
                                data = json.loads(line)
                                logger.info(f"Processing NTFY message: {json.dumps(data)[:500]}")
                                await self._process_response(data)
                            except json.JSONDecodeError:
                                logger.error(f"Failed to parse NTFY response: {line[:100]}...")
                            
            except httpx.ReadTimeout as e:
                logger.warning(f"Read timeout in NTFY response listener: {e}")
                # For read timeouts, just reconnect without increasing retry count
                await asyncio.sleep(1)
                continue
                
            except httpx.ConnectTimeout as e:
                retry_count += 1
                logger.error(f"Connection timeout in NTFY response listener (attempt {retry_count}/{max_retries}): {e}")
                
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
                logger.error(f"HTTP error in NTFY response listener (attempt {retry_count}/{max_retries}): {e}")
                
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
                logger.error(f"Unexpected error in NTFY response listener: {e}", exc_info=True)
                await asyncio.sleep(10)  # Wait before retrying
                
    async def _process_response(self, response_data: Dict[str, Any]):
        """Process a response from the NTFY topic."""
        try:
            # Log all responses for debugging
            logger.info(f"PROCESSING NTFY RESPONSE: {json.dumps(response_data)}")
            
            # Extract event type and message
            event_type = response_data.get("event")
            message = response_data.get("message", "")
            title = response_data.get("title", "")
            
            logger.info(f"NTFY RESPONSE DETAILS - Event: {event_type}, Title: {title}, Message: {message}")
            
            # Log all fields in the response for debugging
            for key, value in response_data.items():
                logger.info(f"NTFY FIELD: {key} = {value}")
            
            # For message events (regular messages)
            if event_type == "message":
                # Check for responses to our questions (case insensitive)
                lower_message = message.lower()
                
                # Log all incoming messages
                logger.info(f"Processing message content: '{message}'")
                
                # Check for game started/ended responses
                if any(keyword in lower_message for keyword in ["yes", "game started", "game ended", "started", "ended"]):
                    logger.info(f"Detected YES response: '{message}'")
                    self._handle_response(message)
                elif any(keyword in lower_message for keyword in ["no", "not yet", "continue"]):
                    logger.info(f"Detected NO response: '{message}'")
                    self._handle_response(message)
                    
            # For action events (button clicks)
            elif event_type in ["action", "click", "response"]:
                # Extract action details
                action = response_data.get("action", "")
                button = response_data.get("button", "")
                
                logger.info(f"Detected action event: Action={action}, Button={button}")
                
                # Check for game started/ended responses in any field
                if any(keyword in str(value).lower() for value in response_data.values() 
                       for keyword in ["yes", "game started", "game ended", "started", "ended"]):
                    logger.info(f"Detected YES in action event: {response_data}")
                    self._handle_response("Yes")
                elif any(keyword in str(value).lower() for value in response_data.values()
                         for keyword in ["no", "not yet", "continue"]):
                    logger.info(f"Detected NO in action event: {response_data}")
                    self._handle_response("No")
            
            # For any other event type
            else:
                logger.info(f"Received unhandled event type: {event_type}")
                # Check if any field contains a response we're looking for
                if any(keyword in str(response_data).lower() for keyword in 
                       ["yes", "game started", "game ended", "started", "ended", "no", "not yet"]):
                    logger.info(f"Found potential response in unhandled event: {response_data}")
                    self._handle_response(str(response_data))
                    
        except Exception as e:
            logger.error(f"Error processing NTFY response: {e}", exc_info=True)
            
    def _handle_response(self, response: str):
        """Handle a response to a message."""
        if not self.pending_messages:
            logger.warning(f"Received response but no pending messages: {response}")
            return
            
        # Find the most recent message
        most_recent_id = max(
            self.pending_messages.keys(),
            key=lambda msg_id: self.message_timestamps.get(msg_id, 0)
        )
        
        # Complete the future with the response
        future = self.pending_messages.get(most_recent_id)
        if future and not future.done():
            future.set_result(response)
            
            # Clean up
            del self.pending_messages[most_recent_id]
            del self.message_timestamps[most_recent_id]
            
            logger.info(f"Processed response for message {most_recent_id}: {response}")
        else:
            logger.warning(f"Future for message {most_recent_id} already completed or missing")
    
    async def ask_game_start_time(self, 
                                 combined_video_path: str, 
                                 group_dir: str, 
                                 time_offset_minutes: int = 5) -> Optional[str]:
        """
        Ask the user to identify when the game starts by sending screenshots at intervals.
        
        Args:
            combined_video_path: Path to the combined video
            group_dir: Path to the group directory
            time_offset_minutes: Minutes between screenshots
            
        Returns:
            str: Start time offset in HH:MM:SS format or None if not determined
        """
        if not self.enabled or not os.path.exists(combined_video_path):
            return None
            
        # Get video duration
        duration = await get_video_duration(combined_video_path)
        if duration is None:
            logger.error(f"Failed to get duration for {combined_video_path}")
            return None
            
        # Convert duration to seconds if it's a float
        if isinstance(duration, float):
            duration_seconds = int(duration)
        else:
            # Try to parse duration string (HH:MM:SS)
            try:
                h, m, s = map(int, duration.split(':'))
                duration_seconds = h * 3600 + m * 60 + s
            except (ValueError, AttributeError):
                logger.error(f"Invalid duration format: {duration}")
                return None
        
        # Calculate interval in seconds
        interval_seconds = time_offset_minutes * 60
        
        # Start from the beginning and check at intervals
        current_offset = 0
        max_offset = min(duration_seconds, 45 * 60)  # Max 45 minutes
        
        while current_offset <= max_offset:
            # Format time as HH:MM:SS
            formatted_time = str(timedelta(seconds=current_offset)).split('.')[0]
            
            # Create a screenshot at the current offset
            time_str = str(timedelta(seconds=current_offset)).split('.')[0]
            formatted_datetime = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = os.path.join(group_dir, f"{formatted_datetime}_{time_str.replace(':', '')}.jpg")
            
            # Skip if we've already processed this time offset for this video
            screenshot_key = f"{combined_video_path}_{current_offset}"
            if screenshot_key in self.processed_screenshots:
                logger.info(f"Already processed screenshot at {time_str}, skipping")
                current_offset += interval_seconds
                continue
                
            # Mark as processed
            self.processed_screenshots.add(screenshot_key)
            
            screenshot_created = await create_screenshot(
                combined_video_path, 
                screenshot_path, 
                time_offset=time_str
            )
            
            if not screenshot_created:
                logger.error(f"Failed to create screenshot at {time_str}")
                current_offset += interval_seconds
                continue
            
            # Compress the screenshot to reduce file size
            compressed_path = await compress_image(
                screenshot_path,
                quality=60,  # Medium quality (0-100)
                max_width=800  # Reasonable width for mobile devices
            )
            
            # Generate a unique message ID for tracking
            message_id = f"start_{formatted_datetime}_{current_offset}"
            
            # Send notification with the screenshot
            actions = [
                {
                    "action": "http", 
                    "label": "Yes, game started", 
                    "url": f"https://ntfy.sh/{self.topic}",
                    "method": "POST",
                    "headers": {"Content-Type": "text/plain"},
                    "body": f"Yes, game started at {formatted_time} (ID: {message_id})",
                    "clear": True
                },
                {
                    "action": "http", 
                    "label": "No, not yet", 
                    "url": f"https://ntfy.sh/{self.topic}",
                    "method": "POST",
                    "headers": {"Content-Type": "text/plain"},
                    "body": f"No, not yet at {formatted_time} (ID: {message_id})",
                    "clear": True
                }
            ]
            
            # Create a future to wait for the response
            response_future = asyncio.Future()
            
            # Store the future to be completed when a response is received
            self.pending_messages[message_id] = response_future
            self.message_timestamps[message_id] = time.time()
            
            # Send notification
            sent = await self.send_notification(
                message=f"Has the game started at this point ({formatted_time} into the video)?\nTimestamp: {formatted_datetime}\nID: {message_id}",
                title="Game Start Time Detection",
                tags=["soccer", "question"],
                priority=4,
                image_path=compressed_path,
                actions=actions
            )
            
            # Clean up the compressed image if it's different from the original
            if compressed_path != screenshot_path and os.path.exists(compressed_path):
                try:
                    os.remove(compressed_path)
                except Exception as e:
                    logger.warning(f"Failed to remove compressed image {compressed_path}: {e}")
            
            if not sent:
                logger.error("Failed to send notification")
                del self.pending_messages[message_id]
                del self.message_timestamps[message_id]
                current_offset += interval_seconds
                continue
                
            # Wait for a response with a timeout
            try:
                logger.info(f"Waiting for response to message {message_id}")
                response = await asyncio.wait_for(response_future, timeout=self.message_timeout)
                
                # Check if the response indicates the game has started
                if response and "Yes, game started" in response:
                    logger.info(f"User confirmed game started at {formatted_time}")
                    return formatted_time
                    
                # If user says not yet, continue to next interval
                logger.info(f"User indicated game has not started at {formatted_time}")
                
            except asyncio.TimeoutError:
                logger.warning(f"No response received for game start time at {formatted_time}")
                # Clean up the pending message
                if message_id in self.pending_messages:
                    del self.pending_messages[message_id]
                    del self.message_timestamps[message_id]
            
            # Move to the next interval
            current_offset += interval_seconds
            
        logger.warning("Failed to determine game start time - no positive response received")
        return None
        
    async def ask_game_end_time(self, 
                             combined_video_path: str, 
                             group_dir: str,
                             start_time_offset: str,
                             time_offset_minutes: int = 5) -> Optional[str]:
        """
        Ask the user to identify when the game ends by sending screenshots at intervals.
        
        Args:
            combined_video_path: Path to the combined video
            group_dir: Path to the group directory
            start_time_offset: Start time offset in HH:MM:SS format
            time_offset_minutes: Minutes between screenshots
            
        Returns:
            str: End time offset in HH:MM:SS format or None if not determined
        """
        if not self.enabled or not os.path.exists(combined_video_path):
            return None
            
        # Get video duration
        duration = await get_video_duration(combined_video_path)
        if duration is None:
            logger.error(f"Failed to get duration for {combined_video_path}")
            return None
            
        # Convert duration to seconds if it's a float
        if isinstance(duration, float):
            duration_seconds = int(duration)
        else:
            # Try to parse duration string (HH:MM:SS)
            try:
                h, m, s = map(int, duration.split(':'))
                duration_seconds = h * 3600 + m * 60 + s
            except (ValueError, AttributeError):
                logger.error(f"Invalid duration format: {duration}")
                return None
        
        # Convert start time offset to seconds
        try:
            h, m, s = map(int, start_time_offset.split(':'))
            start_seconds = h * 3600 + m * 60 + s
        except (ValueError, AttributeError):
            logger.error(f"Invalid start time offset format: {start_time_offset}")
            return None
        
        # Calculate interval in seconds
        interval_seconds = time_offset_minutes * 60
        
        # Start from the start time + 45 minutes (typical game length)
        current_offset = start_seconds + 45 * 60
        max_offset = min(duration_seconds, start_seconds + 120 * 60)  # Max 2 hours after start
        
        while current_offset <= max_offset:
            # Format time as HH:MM:SS
            formatted_time = str(timedelta(seconds=current_offset)).split('.')[0]
            
            # Create a screenshot at the current offset
            time_str = str(timedelta(seconds=current_offset)).split('.')[0]
            formatted_datetime = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = os.path.join(group_dir, f"{formatted_datetime}_{time_str.replace(':', '')}.jpg")
            
            # Skip if we've already processed this time offset for this video
            screenshot_key = f"{combined_video_path}_{current_offset}"
            if screenshot_key in self.processed_screenshots:
                logger.info(f"Already processed screenshot at {time_str}, skipping")
                current_offset += interval_seconds
                continue
                
            # Mark as processed
            self.processed_screenshots.add(screenshot_key)
            
            screenshot_created = await create_screenshot(
                combined_video_path, 
                screenshot_path, 
                time_offset=time_str
            )
            
            if not screenshot_created:
                logger.error(f"Failed to create screenshot at {time_str}")
                current_offset += interval_seconds
                continue
            
            # Compress the screenshot to reduce file size
            compressed_path = await compress_image(
                screenshot_path,
                quality=60,  # Medium quality (0-100)
                max_width=800  # Reasonable width for mobile devices
            )
            
            # Generate a unique message ID for tracking
            message_id = f"end_{formatted_datetime}_{current_offset}"
            
            # Send notification with the screenshot
            actions = [
                {
                    "action": "http", 
                    "label": "Yes, game ended", 
                    "url": f"https://ntfy.sh/{self.topic}",
                    "method": "POST",
                    "headers": {"Content-Type": "text/plain"},
                    "body": f"Yes, game ended at {formatted_time} (ID: {message_id})",
                    "clear": True
                },
                {
                    "action": "http", 
                    "label": "No, not yet", 
                    "url": f"https://ntfy.sh/{self.topic}",
                    "method": "POST",
                    "headers": {"Content-Type": "text/plain"},
                    "body": f"No, not yet at {formatted_time} (ID: {message_id})",
                    "clear": True
                }
            ]
            
            # Create a future to wait for the response
            response_future = asyncio.Future()
            
            # Store the future to be completed when a response is received
            self.pending_messages[message_id] = response_future
            self.message_timestamps[message_id] = time.time()
            
            # Send notification
            sent = await self.send_notification(
                message=f"Has the game ended at this point ({formatted_time} into the video)?\nTimestamp: {formatted_datetime}\nID: {message_id}",
                title="Game End Time Detection",
                tags=["soccer", "question"],
                priority=4,
                image_path=compressed_path,
                actions=actions
            )
            
            # Clean up the compressed image if it's different from the original
            if compressed_path != screenshot_path and os.path.exists(compressed_path):
                try:
                    os.remove(compressed_path)
                except Exception as e:
                    logger.warning(f"Failed to remove compressed image {compressed_path}: {e}")
            
            if not sent:
                logger.error("Failed to send notification")
                del self.pending_messages[message_id]
                del self.message_timestamps[message_id]
                current_offset += interval_seconds
                continue
                
            # Wait for a response with a timeout
            try:
                logger.info(f"Waiting for response to message {message_id}")
                response = await asyncio.wait_for(response_future, timeout=self.message_timeout)
                
                # Check if the response indicates the game has ended
                if response and "Yes, game ended" in response:
                    logger.info(f"User confirmed game ended at {formatted_time}")
                    return formatted_time
                    
                # If user says not yet, continue to next interval
                logger.info(f"User indicated game has not ended at {formatted_time}")
                
            except asyncio.TimeoutError:
                logger.warning(f"No response received for game end time at {formatted_time}")
                # Clean up the pending message
                if message_id in self.pending_messages:
                    del self.pending_messages[message_id]
                    del self.message_timestamps[message_id]
            
            # Move to the next interval
            current_offset += interval_seconds
            
        logger.warning("Failed to determine game end time - no positive response received")
        return None

    async def send_notification(self,
                             message: str,
                             title: str = None,
                             tags: List[str] = None,
                             priority: int = None,
                             image_path: str = None,
                             actions: List[Dict[str, Any]] = None) -> bool:
        """
        Send a notification to the NTFY topic.
        
        Args:
            message: The message to send
            title: Optional title for the notification
            tags: Optional list of tags for the notification
            priority: Optional priority (1-5)
            image_path: Optional path to an image to attach
            actions: Optional list of action buttons
            
        Returns:
            bool: True if sent successfully, False otherwise
        """
        if not self.enabled or not self.topic:
            logger.warning("Cannot send NTFY notification - integration not enabled")
            return False
        
        headers = {}
        if title:
            headers["Title"] = title
        if tags:
            headers["Tags"] = ",".join(tags)
        if priority:
            headers["Priority"] = str(priority)
        if actions:
            headers["Actions"] = json.dumps(actions)
        
        try:
            # Create a new client for each request to avoid connection issues
            async with httpx.AsyncClient(timeout=30.0) as client:
                if image_path and os.path.exists(image_path):
                    # Use PUT method with the file data as the request body
                    with open(image_path, 'rb') as file:
                        # Add the filename header
                        headers["Filename"] = os.path.basename(image_path)
                        
                        # Send the request with the file data
                        response = await client.put(
                            f"{self.base_url}/{self.topic}",
                            content=file.read(),
                            headers=headers
                        )
                else:
                    # Regular text notification
                    response = await client.post(
                        f"{self.base_url}/{self.topic}",
                        data=message,
                        headers=headers
                    )
            
            # Check response status code safely (works with both real responses and mocks)
            status_code = getattr(response, 'status_code', 200)
            if status_code >= 400:
                logger.error(f"Failed to send NTFY notification: {status_code}")
                return False
                
            logger.info(f"Successfully sent NTFY notification to {self.topic}")
            return True
                
        except Exception as e:
            logger.error(f"Failed to send NTFY notification: {e}")
            return False

    async def shutdown(self):
        """Properly close the NTFY connection and cleanup resources."""
        logger.info("Shutting down NTFY API connection")
        if hasattr(self, '_client') and self._client:
            await self._client.aclose()
        
        if hasattr(self, '_response_listener_task') and self._response_listener_task:
            if not self._response_listener_task.done():
                self._response_listener_task.cancel()
                try:
                    await self._response_listener_task
                except asyncio.CancelledError:
                    logger.info("NTFY response listener task cancelled")
        
        logger.info("NTFY API shutdown complete")

    async def wait_for_response(self, message_id: str, timeout: float = 60.0) -> Dict[str, Any]:
        """
        Wait for a response to a specific message.
        
        Args:
            message_id: The ID of the message to wait for
            timeout: How long to wait for a response (in seconds)
            
        Returns:
            Dict containing the response data
        """
        if message_id in self.pending_messages:
            try:
                response = await asyncio.wait_for(self.pending_messages[message_id], timeout=timeout)
                return response
            except asyncio.TimeoutError:
                logger.warning(f"Timed out waiting for response to message {message_id}")
                # Clean up the pending message
                if message_id in self.pending_messages:
                    del self.pending_messages[message_id]
                    if message_id in self.message_timestamps:
                        del self.message_timestamps[message_id]
                return {'is_affirmative': False, 'message': 'Timeout'}
        else:
            logger.warning(f"No pending message with ID {message_id}")
            return {'is_affirmative': False, 'message': 'No pending message'}