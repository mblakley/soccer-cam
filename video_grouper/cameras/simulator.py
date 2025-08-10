"""
Camera simulator for end-to-end testing.

This module provides a simulated camera implementation that mimics the behavior
of a real Dahua camera but returns controlled test data for comprehensive
end-to-end testing scenarios.
"""

import asyncio
import logging
import os
import tempfile
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .base import Camera, DeviceInfo

logger = logging.getLogger(__name__)


class SimulatorCamera(Camera):
    """
    Camera simulator that provides controlled test data for end-to-end testing.

    This simulator:
    - Returns a predefined set of 5 video files recorded 2 hours ago
    - Provides realistic file sizes and durations
    - Simulates download behavior with actual small test video files
    - Allows control over connection status for testing connected/disconnected filtering
    """

    def __init__(self, config, storage_path: str):
        """
        Initialize the camera simulator.

        Args:
            config: Camera configuration object
            storage_path: Path where files should be stored
        """
        self.config = config
        self.storage_path = storage_path
        self.device_ip = getattr(config, "device_ip", "127.0.0.1")
        self.username = getattr(config, "username", "admin")
        self.password = getattr(config, "password", "admin")

        # Simulation state
        self._is_connected = (
            True  # Camera is "connected" (in office) while simulator is running
        )
        self._connection_events = []
        self._recording_status = True

        # Record when the simulator started (this is when camera becomes "connected")
        self._simulator_start_time = datetime.now()

        # Add initial connection event
        self._connection_events.append((self._simulator_start_time, "connected"))

        # Generate test files metadata - 5 files recorded 2 hours ago
        self._test_files = self._generate_test_files()

        # Create temporary test video files
        self._create_test_video_files()

        logger.info(
            f"Camera simulator initialized with {len(self._test_files)} test files (camera is connected)"
        )

    def _generate_test_files(self) -> List[Dict[str, Any]]:
        """Generate metadata for test video files."""
        files = []

        # Start time: 12 hours ago in UTC to ensure files are clearly before simulator start
        # The simulator starts at current time, so files should be 12+ hours old to avoid overlap
        # after timezone conversion (EST to UTC adds 4 hours, so 12 hours ago becomes 8 hours ago in UTC)
        import pytz

        utc_now = datetime.now(pytz.utc)
        base_time = utc_now - timedelta(hours=12)

        # Generate 5 video files, each exactly 5 minutes long, consecutive (no gaps)
        file_duration_minutes = 5
        current_time = base_time

        for i in range(5):
            start_time = current_time
            end_time = current_time + timedelta(minutes=file_duration_minutes)

            # Format filename to match Dahua camera format
            start_str = start_time.strftime("%H.%M.%S")
            end_str = end_time.strftime("%H.%M.%S")
            filename = f"{start_str}-{end_str}[F][0@0][{134510 + i}].dav"

            # Create file metadata matching real camera format
            file_data = {
                "path": filename,
                "startTime": start_time.strftime("%Y-%m-%d %H:%M:%S"),
                "endTime": end_time.strftime("%Y-%m-%d %H:%M:%S"),
                "Length": 1024 * 1024 * 50,  # 50MB file size
                "FilePath": filename,
                "StartTime": start_time.strftime("%Y-%m-%d %H:%M:%S"),
                "EndTime": end_time.strftime("%Y-%m-%d %H:%M:%S"),
                "UTCOffset": -14400,  # EST timezone offset
                "size": 1024 * 1024 * 50,  # 50MB file size
            }

            files.append(file_data)
            current_time = end_time

        return files

    def _create_test_video_files(self) -> None:
        """Create small test video files that can be downloaded."""
        self._temp_dir = tempfile.mkdtemp(prefix="camera_sim_")
        self._test_file_paths = {}

        # Find the screenshot.jpg file
        screenshot_path = None
        possible_paths = [
            "screenshot.jpg",
            "tests/e2e/screenshot.jpg",
            "shared_data/screenshot.jpg",
            "../screenshot.jpg",
            "../../screenshot.jpg",
        ]

        for path in possible_paths:
            if os.path.exists(path):
                screenshot_path = os.path.abspath(path)
                break

        if not screenshot_path:
            logger.warning("screenshot.jpg not found, falling back to test pattern")

        for i, file_data in enumerate(self._test_files):
            # Create a Windows-safe filename
            start_time = datetime.strptime(file_data["startTime"], "%Y-%m-%d %H:%M:%S")
            end_time = datetime.strptime(file_data["endTime"], "%Y-%m-%d %H:%M:%S")

            # Use a safe filename format for Windows
            safe_filename = f"test_video_{i + 1:02d}_{start_time.strftime('%Y%m%d_%H%M%S')}_{end_time.strftime('%H%M%S')}.dav"
            local_path = os.path.join(self._temp_dir, safe_filename)

            # Create a small video file using ffmpeg if available, otherwise a placeholder
            try:
                # Try to create a real small video file
                import subprocess

                temp_mp4_path = local_path.replace(".dav", ".mp4")

                if screenshot_path:
                    # Use screenshot.jpg to create a video
                    cmd = [
                        "ffmpeg",
                        "-loop",
                        "1",
                        "-i",
                        screenshot_path,
                        "-c:v",
                        "libx264",
                        "-preset",
                        "ultrafast",
                        "-t",
                        "5",
                        "-pix_fmt",
                        "yuv420p",
                        "-y",
                        temp_mp4_path,
                    ]
                    logger.info(f"Creating video from screenshot: {screenshot_path}")
                else:
                    # Fallback to test pattern
                    cmd = [
                        "ffmpeg",
                        "-f",
                        "lavfi",
                        "-i",
                        "testsrc2=duration=5:size=320x240:rate=1",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "ultrafast",
                        "-y",
                        temp_mp4_path,
                    ]

                subprocess.run(cmd, capture_output=True, check=True, timeout=30)
                # Rename to .dav to match expected format
                os.rename(temp_mp4_path, local_path)
                logger.info(f"Created test video file: {local_path}")
            except (
                subprocess.CalledProcessError,
                FileNotFoundError,
                subprocess.TimeoutExpired,
            ) as e:
                logger.warning(f"Failed to create video with ffmpeg: {e}")
                # Fallback: create a dummy binary file
                with open(local_path, "wb") as f:
                    # Write some dummy data that looks like a video file
                    f.write(b"\x00\x00\x00\x20ftypmp42" + b"\x00" * 1000)
                logger.info(f"Created dummy test file: {local_path}")

            self._test_file_paths[file_data["path"]] = local_path

    async def check_availability(self) -> bool:
        """Check if the camera is available."""
        # Simulate occasional network issues (5% chance of failure)
        import random

        if random.random() < 0.05:
            logger.debug("Simulating camera availability check failure")
            return False

        await asyncio.sleep(0.1)  # Simulate network delay
        return True

    async def get_file_list(
        self, start_time: datetime = None, end_time: datetime = None
    ) -> List[Dict[str, Any]]:
        """Get list of recording files from the camera."""
        await asyncio.sleep(0.2)  # Simulate network delay

        # For E2E testing, always return the same 5 test files every time
        # This simulates finding the same historical files that need to be processed
        logger.info(
            f"Camera simulator returning {len(self._test_files)} files for time range {start_time} to {end_time} (E2E mode)"
        )

        # Filter files based on start_time if provided
        filtered_files = self._test_files.copy()
        if start_time:
            # Only return files that start after the start_time (i.e., files that are newer)
            filtered_files = []
            for file_data in self._test_files:
                file_start_time = datetime.strptime(
                    file_data["StartTime"], "%Y-%m-%d %H:%M:%S"
                )
                if file_start_time > start_time:
                    filtered_files.append(file_data)

            logger.info(
                f"Camera simulator filtered to {len(filtered_files)} files after start_time {start_time}"
            )

        # Log each file's details for debugging
        for i, file_data in enumerate(filtered_files):
            logger.info(
                f"Camera simulator file {i + 1}: {file_data['path']} - StartTime: {file_data['StartTime']}, EndTime: {file_data['EndTime']}, Length: {file_data['Length']}"
            )

        return filtered_files

    async def get_file_size(self, file_path: str) -> int:
        """Get size of a file on the camera."""
        await asyncio.sleep(0.1)  # Simulate network delay

        for file_data in self._test_files:
            if file_data["path"] == file_path:
                # Use Length field to match real camera format
                return file_data.get("Length", file_data.get("size", 0))

        raise FileNotFoundError(f"File not found: {file_path}")

    async def download_file(self, file_path: str, local_path: str) -> bool:
        """Download a file from the camera."""
        try:
            # Find the test file
            if file_path not in self._test_file_paths:
                logger.error(f"Test file not found: {file_path}")
                return False

            source_path = self._test_file_paths[file_path]

            # Simulate download progress and time
            file_size = os.path.getsize(source_path)
            logger.info(
                f"Simulating download of {file_path} ({file_size} bytes) to {local_path}"
            )

            # Create directory if it doesn't exist
            os.makedirs(os.path.dirname(local_path), exist_ok=True)

            # Simulate download time (slower for larger files)
            download_time = min(2.0, file_size / (1024 * 1024))  # Max 2 seconds
            await asyncio.sleep(download_time)

            # Copy the test file
            import shutil

            shutil.copy2(source_path, local_path)

            logger.info(f"Successfully downloaded {file_path} to {local_path}")
            return True

        except Exception as e:
            logger.error(f"Error downloading file {file_path}: {e}")
            return False

    async def stop_recording(self) -> bool:
        """Stop recording on the camera."""
        await asyncio.sleep(0.1)  # Simulate network delay
        self._recording_status = False
        logger.info("Camera recording stopped")
        return True

    async def get_recording_status(self) -> bool:
        """Get recording status from the camera."""
        await asyncio.sleep(0.1)  # Simulate network delay
        return self._recording_status

    async def get_device_info(self) -> DeviceInfo:
        """Get device information from the camera."""
        await asyncio.sleep(0.2)  # Simulate network delay

        return DeviceInfo(
            device_type="Camera Simulator",
            serial_number="SIM001",
            hardware_version="1.0",
            software_version="2024.1.0",
            build_date="2024-01-01",
            encoder_version="1.0",
        )

    def get_connected_timeframes(self) -> List[Tuple[datetime, Optional[datetime]]]:
        """Returns a list of timeframes when the camera was connected."""
        # For testing, simulate that camera was NOT connected during the test recording time
        # This ensures our test files will be processed (not filtered out)
        return []

    @property
    def connection_events(self) -> List[Tuple[datetime, str]]:
        """Get list of connection events."""
        return self._connection_events.copy()

    @property
    def is_connected(self) -> bool:
        """Get connection status."""
        return self._is_connected

    def set_connected(self, connected: bool) -> None:
        """Set connection status for testing."""
        if self._is_connected != connected:
            self._is_connected = connected
            event_type = "connected" if connected else "disconnected"
            self._connection_events.append((datetime.now(), event_type))
            logger.info(f"Camera simulator connection status changed to: {event_type}")

    def cleanup(self) -> None:
        """Clean up temporary test files."""
        if hasattr(self, "_temp_dir") and os.path.exists(self._temp_dir):
            import shutil

            shutil.rmtree(self._temp_dir)
            logger.info(f"Cleaned up temporary test files in {self._temp_dir}")

    async def close(self) -> None:
        """Close the camera connection."""
        logger.info("Closing camera simulator connection")
        self.cleanup()

    def __del__(self):
        """Ensure cleanup on destruction."""
        try:
            self.cleanup()
        except Exception:
            pass
