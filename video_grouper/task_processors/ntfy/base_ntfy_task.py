"""
Base class for NTFY tasks.

This provides the common interface and functionality for all NTFY tasks.
"""

import os
import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from datetime import datetime
from dataclasses import dataclass, field

from video_grouper.models import MatchInfo
from video_grouper.utils.config import Config

logger = logging.getLogger(__name__)


@dataclass
class NtfyTaskResult:
    """Result of processing an NTFY task."""

    success: bool
    should_continue: bool = False
    message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


class BaseNtfyTask(ABC):
    """
    Base class for all NTFY tasks.

    This provides the common interface and functionality that all NTFY tasks
    should implement.
    """

    def __init__(
        self, group_dir: str, config: Config, metadata: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize the base NTFY task.

        Args:
            group_dir: Directory associated with the task
            config: Configuration object
            metadata: Additional metadata for the task
        """
        self.group_dir = group_dir
        self.config = config
        self.metadata = metadata or {}
        self.created_at = datetime.now()

    @abstractmethod
    async def create_question(self) -> Dict[str, Any]:
        """
        Create the question data for this task.

        Returns:
            Dictionary containing question data (message, title, tags, actions, etc.)
        """
        pass

    @abstractmethod
    async def process_response(self, response: str) -> NtfyTaskResult:
        """
        Process a response to this task.

        Args:
            response: The user's response

        Returns:
            NtfyTaskResult indicating success and whether to continue
        """
        pass

    @abstractmethod
    def get_task_type(self) -> str:
        """
        Get the type identifier for this task.

        Returns:
            String identifier for the task type
        """
        pass

    async def generate_screenshot(
        self, video_path: str, time_seconds: int
    ) -> Optional[str]:
        """
        Generate a screenshot at the specified time.

        Args:
            video_path: Path to the video file
            time_seconds: Time offset in seconds

        Returns:
            Path to the generated screenshot, or None if failed
        """
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

    async def get_video_duration(self, video_path: str) -> Optional[int]:
        """
        Get video duration in seconds.

        Args:
            video_path: Path to the video file

        Returns:
            Duration in seconds, or None if failed
        """
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

    def get_match_info(self) -> MatchInfo:
        """
        Get or create the MatchInfo object for this task's directory.

        Returns:
            MatchInfo object
        """
        return MatchInfo.get_or_create(self.group_dir)[0]
