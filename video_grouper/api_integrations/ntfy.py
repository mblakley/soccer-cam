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
from pathlib import Path
from video_grouper.utils.ffmpeg_utils import create_screenshot, get_video_duration
from video_grouper.utils.config import NtfyConfig
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
    def __init__(self, config: NtfyConfig):
        """
        Initialize the NTFY API client.
        
        Args:
            config: NTFY configuration object
        """
        self.config = config
        self.enabled = config.enabled
        self.base_url = config.server_url
        self.topic = config.topic
        self.client = None
        self.response_queue = asyncio.Queue()
        self.listener_task = None
        self.session_id = str(uuid.uuid4())[:8]  # Create a unique session ID
        
        # Track sent messages and their responses
        self.pending_messages = {}  # message_id -> future
        self.message_timestamps = {}  # message_id -> timestamp
        self.processed_screenshots = set()  # Set of screenshot paths that have been processed
        self.message_timeout = 300  # seconds (5 minutes)
        
        if not self.topic:
            self.topic = f"soccer-cam-{self.session_id}"
            logger.info(f"Using auto-generated NTFY topic: {self.topic}")
        
        logger.info(f"NTFY integration configured with topic: {self.topic}")
    
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
                
                # Check if this is a text input response (JSON format)
                try:
                    # Try to parse as JSON
                    if message.strip().startswith('{') and message.strip().endswith('}'):
                        json_data = json.loads(message)
                        if 'id' in json_data and ('response' in json_data or 'text' in json_data):
                            logger.info(f"Detected text input response: {json_data}")
                            message_id = json_data.get('id')
                            response_text = json_data.get('response', json_data.get('text', ''))
                            self._handle_specific_response(message_id, response_text)
                            return
                except json.JSONDecodeError:
                    pass  # Not JSON, continue with normal processing
                
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
                
                # Check if this is a text input response
                if "input" in response_data:
                    input_text = response_data.get("input", "")
                    logger.info(f"Detected text input: {input_text}")
                    
                    # Try to extract message ID from the body if available
                    body = response_data.get("body", "")
                    message_id = None
                    
                    # Try to find message ID in the body
                    if isinstance(body, str) and "id" in body.lower():
                        try:
                            body_data = json.loads(body)
                            if "id" in body_data:
                                message_id = body_data["id"]
                        except:
                            pass
                    
                    if message_id:
                        self._handle_specific_response(message_id, input_text)
                    else:
                        self._handle_response({"response": input_text})
                    return
                
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
            
    def _handle_specific_response(self, message_id: str, response: str):
        """Handle a response to a specific message ID."""
        if message_id in self.pending_messages:
            future = self.pending_messages[message_id]
            if not future.done():
                # For text input responses, create a structured response
                response_data = {
                    'id': message_id,
                    'response': response,
                    'timestamp': time.time()
                }
                
                future.set_result(response_data)
                
                # Clean up
                del self.pending_messages[message_id]
                if message_id in self.message_timestamps:
                    del self.message_timestamps[message_id]
                
                logger.info(f"Processed specific response for message {message_id}: {response}")
            else:
                logger.warning(f"Future for message {message_id} already completed")
        else:
            logger.warning(f"No pending message with ID {message_id}")
            # Try to handle as a regular response as fallback
            self._handle_response(response)
    
    async def ask_game_start_time(self, 
                             combined_video_path: str, 
                             group_dir: str, 
                             time_offset_minutes: int = 5) -> Optional[str]:
        """
        Send notification about the need to set game start time.
        
        Args:
            combined_video_path: Path to the combined video file
            group_dir: Path to the group directory
            time_offset_minutes: Minutes between screenshots
            
        Returns:
            None as we're not getting input, just sending notifications
        """
        if not self.enabled:
            logger.warning("NTFY integration not enabled - cannot send game start time notification")
            return None
            
        # Create screenshots at different times in the video
        try:
            # Get video duration
            duration = await get_video_duration(combined_video_path)
            if not duration:
                logger.error(f"Failed to get video duration for {combined_video_path}")
                return None
                
            # Parse the duration
            if isinstance(duration, str):
                h, m, s = map(int, duration.split(':'))
                total_seconds = h * 3600 + m * 60 + s
            else:
                total_seconds = int(duration)
                
            # Create a screenshot at the beginning of the video
            screenshot_path = os.path.join(os.path.dirname(combined_video_path), "temp_screenshot_start.jpg")
            
            screenshot_created = await create_screenshot(
                combined_video_path, 
                screenshot_path, 
                time_offset="00:00:00"
            )
            
            if not screenshot_created:
                logger.error(f"Failed to create screenshot at start of video {combined_video_path}")
                return None
                
            # Compress the screenshot
            screenshot_path = await compress_image(
                screenshot_path,
                quality=60,
                max_width=800
            )
            
            # Send notification with the screenshot
            await self.send_notification(
                message=f"Game start time needs to be set manually in match_info.ini for {os.path.basename(group_dir)}",
                title="Set Game Start Time",
                tags=["warning", "info"],
                priority=4,
                image_path=screenshot_path
            )
            
            # Clean up the screenshot
            if os.path.exists(screenshot_path):
                try:
                    os.remove(screenshot_path)
                except Exception as e:
                    logger.warning(f"Failed to remove screenshot {screenshot_path}: {e}")
                    
            logger.info(f"Sent notification about setting game start time for {group_dir}")
            
        except Exception as e:
            logger.error(f"Error creating screenshots for game start time notification: {e}")
            
        return None
        
    async def ask_game_end_time(self, 
                         combined_video_path: str, 
                         group_dir: str,
                         start_time_offset: str,
                         time_offset_minutes: int = 5) -> Optional[str]:
        """
        Send notification about the need to set game end time.
        
        Args:
            combined_video_path: Path to the combined video file
            group_dir: Path to the group directory
            start_time_offset: Start time offset in HH:MM:SS format
            time_offset_minutes: Minutes between screenshots
            
        Returns:
            None as we're not getting input, just sending notifications
        """
        if not self.enabled:
            logger.warning("NTFY integration not enabled - cannot send game end time notification")
            return None
            
        # Create screenshots at different times in the video
        try:
            # Get video duration
            duration = await get_video_duration(combined_video_path)
            if not duration:
                logger.error(f"Failed to get video duration for {combined_video_path}")
                return None
                
            # Parse the duration
            if isinstance(duration, str):
                h, m, s = map(int, duration.split(':'))
                total_seconds = h * 3600 + m * 60 + s
            else:
                total_seconds = int(duration)
                
            # Parse start time offset
            try:
                start_h, start_m, start_s = map(int, start_time_offset.split(':'))
                start_seconds = start_h * 3600 + start_m * 60 + start_s
            except (ValueError, AttributeError):
                logger.error(f"Invalid start time offset format: {start_time_offset}")
                # Use a default offset of 5 seconds
                start_seconds = 5
            
            # Calculate a point after the start time for the screenshot
            # Make sure it's within the video duration
            mid_point = min(start_seconds + 60, total_seconds - 5)  # 1 minute after start or near end
            if mid_point < 0:
                mid_point = 5  # Default to 5 seconds if calculation results in negative value
                
            time_str = str(timedelta(seconds=mid_point)).split('.')[0]
            
            # Create a screenshot at the calculated time
            screenshot_path = os.path.join(os.path.dirname(combined_video_path), "temp_screenshot_end.jpg")
            
            screenshot_created = await create_screenshot(
                combined_video_path, 
                screenshot_path, 
                time_offset=time_str
            )
            
            if not screenshot_created:
                logger.error(f"Failed to create screenshot at {time_str} of video {combined_video_path}")
                return None
                
            # Compress the screenshot
            screenshot_path = await compress_image(
                screenshot_path,
                quality=60,
                max_width=800
            )
            
            # Send notification with the screenshot
            await self.send_notification(
                message=f"Game end time needs to be set manually in match_info.ini for {os.path.basename(group_dir)}",
                title="Set Game End Time",
                tags=["warning", "info"],
                priority=4,
                image_path=screenshot_path
            )
            
            # Clean up the screenshot
            if os.path.exists(screenshot_path):
                try:
                    os.remove(screenshot_path)
                except Exception as e:
                    logger.warning(f"Failed to remove screenshot {screenshot_path}: {e}")
                    
            logger.info(f"Sent notification about setting game end time for {group_dir}")
            
        except Exception as e:
            logger.error(f"Error creating screenshots for game end time notification: {e}")
            
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

    async def ask_team_info(self, combined_video_path: str, existing_info: Dict[str, str] = None) -> Dict[str, str]:
        """
        Send notifications about missing team information fields.
        
        Args:
            combined_video_path: Path to the combined video file
            existing_info: Dictionary with existing team info fields
            
        Returns:
            Dict containing team_name, opponent_name, and location (same as existing_info)
        """
        if not self.enabled:
            logger.warning("NTFY integration not enabled - cannot send team information notifications")
            return existing_info or {}
            
        # Initialize with existing info or empty dict
        team_info = existing_info or {}
        
        # Check for missing fields
        missing_fields = []
        if 'team_name' not in team_info and 'my_team_name' not in team_info:
            missing_fields.append("team name")
        if 'opponent_name' not in team_info and 'opponent_team_name' not in team_info:
            missing_fields.append("opponent team name")
        if 'location' not in team_info:
            missing_fields.append("game location")
        
        # If there are missing fields, send a notification
        if missing_fields:
            missing_fields_str = ", ".join(missing_fields)
            
            # Create a screenshot if video path provided
            screenshot_path = None
            if combined_video_path and os.path.exists(combined_video_path):
                try:
                    # Create a screenshot at the middle of the video
                    duration = await get_video_duration(combined_video_path)
                    if duration:
                        # Parse the duration
                        if isinstance(duration, str):
                            h, m, s = map(int, duration.split(':'))
                            total_seconds = h * 3600 + m * 60 + s
                        else:
                            total_seconds = int(duration)
                        
                        # Take screenshot at the middle
                        mid_point = total_seconds // 2
                        time_str = str(timedelta(seconds=mid_point)).split('.')[0]
                        
                        # Create temporary screenshot
                        formatted_datetime = datetime.now().strftime("%Y%m%d_%H%M%S")
                        screenshot_path = os.path.join(os.path.dirname(combined_video_path), 
                                                    f"temp_screenshot_{formatted_datetime}.jpg")
                        
                        screenshot_created = await create_screenshot(
                            combined_video_path, 
                            screenshot_path, 
                            time_offset=time_str
                        )
                        
                        if not screenshot_created:
                            screenshot_path = None
                        else:
                            # Compress the screenshot
                            screenshot_path = await compress_image(
                                screenshot_path,
                                quality=60,
                                max_width=800
                            )
                except Exception as e:
                    logger.error(f"Error creating screenshot: {e}")
                    screenshot_path = None
            
            # Send the notification
            await self.send_notification(
                message=f"Missing match information: {missing_fields_str}. Please update match_info.ini manually.",
                title="Missing Match Information",
                tags=["warning", "info"],
                priority=4,
                image_path=screenshot_path
            )
            
            # Clean up the screenshot if it exists
            if screenshot_path and os.path.exists(screenshot_path):
                try:
                    os.remove(screenshot_path)
                except Exception as e:
                    logger.warning(f"Failed to remove screenshot {screenshot_path}: {e}")
            
            logger.info(f"Sent notification about missing match information: {missing_fields_str}")
        else:
            logger.info("All team information fields are populated, no notification needed")
        
        return team_info

    async def ask_resolve_game_conflict(self, 
                            combined_video_path: str, 
                            group_dir: str,
                            game_options: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """
        Send notification asking the user to resolve a conflict between games from different sources.
        
        Args:
            combined_video_path: Path to the combined video file
            group_dir: Path to the group directory
            game_options: List of game dictionaries from TeamSnap and PlayMetrics
            
        Returns:
            Dict containing the selected game information or None if no selection was made
        """
        if not self.enabled:
            logger.warning("NTFY integration not enabled - cannot send game conflict resolution notification")
            return None
            
        if not game_options or len(game_options) < 2:
            logger.warning(f"Not enough game options to resolve conflict: {len(game_options) if game_options else 0}")
            return game_options[0] if game_options else None
            
        logger.info(f"Asking user to resolve conflict between {len(game_options)} games")
        
        # Create a screenshot from a random point in the video for context
        screenshot_path = None
        if combined_video_path and os.path.exists(combined_video_path):
            try:
                # Get video duration
                duration = await get_video_duration(combined_video_path)
                if duration:
                    # Parse the duration
                    if isinstance(duration, str):
                        h, m, s = map(int, duration.split(':'))
                        total_seconds = h * 3600 + m * 60 + s
                    else:
                        total_seconds = int(duration)
                    
                    # Take screenshot at a random point in the middle third of the video
                    import random
                    start_point = total_seconds // 3
                    end_point = total_seconds * 2 // 3
                    random_point = random.randint(start_point, end_point)
                    time_str = str(timedelta(seconds=random_point)).split('.')[0]
                    
                    # Create temporary screenshot
                    formatted_datetime = datetime.now().strftime("%Y%m%d_%H%M%S")
                    screenshot_path = os.path.join(os.path.dirname(combined_video_path), 
                                                f"temp_screenshot_conflict_{formatted_datetime}.jpg")
                    
                    screenshot_created = await create_screenshot(
                        combined_video_path, 
                        screenshot_path, 
                        time_offset=time_str
                    )
                    
                    if not screenshot_created:
                        logger.error(f"Failed to create screenshot for game conflict resolution")
                        screenshot_path = None
                    else:
                        # Compress the screenshot
                        screenshot_path = await compress_image(
                            screenshot_path,
                            quality=60,
                            max_width=800
                        )
            except Exception as e:
                logger.error(f"Error creating screenshot for game conflict: {e}")
                screenshot_path = None
        
        # Create a unique message ID for this request
        message_id = f"game_conflict_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        
        # Create action buttons for each game option
        actions = []
        for i, game in enumerate(game_options):
            # Extract relevant information for the button
            source = game.get('source', 'Unknown')
            team_name = game.get('team_name', 'Unknown Team')
            opponent = game.get('opponent_name', game.get('opponent', 'Unknown Opponent'))
            location = game.get('location', 'Unknown Location')
            date = game.get('date', '')
            
            # Create a button with the opponent name
            action = {
                "action": f"http://localhost:8080/select_game/{i}",
                "label": f"{opponent} ({source})",
                "clear": True
            }
            actions.append(action)
        
        # Create a future for the response
        response_future = asyncio.Future()
        self.pending_messages[message_id] = response_future
        self.message_timestamps[message_id] = time.time()
        
        # Send the notification with the screenshot and action buttons
        message_text = (
            f"Multiple games found for the recording in {os.path.basename(group_dir)}.\n"
            f"Please select which game this recording is for:"
        )
        
        # Add game details to the message
        for i, game in enumerate(game_options):
            source = game.get('source', 'Unknown')
            team_name = game.get('team_name', 'Unknown Team')
            opponent = game.get('opponent_name', game.get('opponent', 'Unknown Opponent'))
            location = game.get('location', 'Unknown Location')
            date = game.get('date', '')
            
            message_text += f"\n\n{i+1}. {team_name} vs {opponent} at {location} ({source})"
            if date:
                message_text += f" on {date}"
        
        # Add message ID to the notification for tracking responses
        message_text += f"\n\nID: {message_id}"
        
        # Send the notification
        sent = await self.send_notification(
            message=message_text,
            title="Game Conflict - Select Correct Game",
            tags=["warning", "question"],
            priority=4,
            image_path=screenshot_path,
            actions=actions
        )
        
        if not sent:
            logger.error(f"Failed to send game conflict notification")
            if message_id in self.pending_messages:
                del self.pending_messages[message_id]
            if message_id in self.message_timestamps:
                del self.message_timestamps[message_id]
            return None
        
        # Clean up the screenshot if it exists
        if screenshot_path and os.path.exists(screenshot_path):
            try:
                os.remove(screenshot_path)
            except Exception as e:
                logger.warning(f"Failed to remove screenshot {screenshot_path}: {e}")
        
        # Wait for a response with a longer timeout (10 minutes)
        try:
            response = await asyncio.wait_for(response_future, timeout=600.0)
            
            # Parse the response to determine which game was selected
            logger.info(f"Received response for game conflict: {response}")
            
            # Try to extract the game index from the response
            selected_index = None
            
            # Check if it's a structured response
            if isinstance(response, dict) and 'response' in response:
                response_text = response['response']
            else:
                response_text = str(response)
            
            # Try to find the selected index in various response formats
            if "select_game/" in response_text:
                # Extract from URL pattern
                import re
                match = re.search(r'select_game/(\d+)', response_text)
                if match:
                    selected_index = int(match.group(1))
            elif response_text.isdigit():
                # Direct numeric response (1-based)
                selected_index = int(response_text) - 1
            else:
                # Try to match opponent name
                for i, game in enumerate(game_options):
                    opponent = game.get('opponent_name', game.get('opponent', '')).lower()
                    if opponent and opponent in response_text.lower():
                        selected_index = i
                        break
            
            # Return the selected game if a valid index was found
            if selected_index is not None and 0 <= selected_index < len(game_options):
                logger.info(f"User selected game option {selected_index + 1}: {game_options[selected_index]}")
                return game_options[selected_index]
            else:
                logger.warning(f"Could not determine selected game from response: {response_text}")
                return None
                
        except asyncio.TimeoutError:
            logger.warning(f"Timed out waiting for game conflict resolution")
            # Clean up the pending message
            if message_id in self.pending_messages:
                del self.pending_messages[message_id]
            if message_id in self.message_timestamps:
                del self.message_timestamps[message_id]
            return None