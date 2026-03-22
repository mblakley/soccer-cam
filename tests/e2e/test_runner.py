#!/usr/bin/env python3
"""
End-to-End Test Runner for Video Grouper System

This module orchestrates a complete end-to-end test of the video processing pipeline
using mock services and simulated components. It starts the video_grouper application
as a subprocess and monitors its progress through log files.
"""

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
import json

import pytest

logger = logging.getLogger(__name__)

# Attempt to import configuration utilities; if that fails, adjust sys.path then retry
project_root = Path(__file__).parent.parent.parent
try:
    from video_grouper.utils.config import load_config
    from video_grouper.utils.logger import setup_logging_from_config
except ImportError:
    sys.path.insert(0, str(project_root))
    from video_grouper.utils.config import load_config
    from video_grouper.utils.logger import setup_logging_from_config

# Import mock NTFY service at module level (optional for tests)
try:
    from video_grouper.api_integrations.ntfy_response import (
        create_ntfy_response_service,
    )
except ImportError as e:
    logger.warning(f"Could not import mock NTFY service: {e}")
    create_ntfy_response_service = None


def setup_e2e_environment(project_root: Path) -> None:
    """Set up environment variables for E2E testing."""
    # Set environment variables - simple and direct
    os.environ.update(
        {
            "USE_MOCK_NTFY": "false",  # Use real NTFY API for sending requests
            "USE_MOCK_TEAMSNAP": "true",
            "USE_MOCK_PLAYMETRICS": "true",
            "USE_MOCK_SERVICES": "true",
            "PYTHONPATH": f"{os.environ.get('PYTHONPATH', '')}:{project_root}",
        }
    )

    logger.info("E2E environment configured:")
    logger.info("  - USE_MOCK_NTFY: false (using real NTFY API)")
    logger.info("  - USE_MOCK_TEAMSNAP: true")
    logger.info("  - USE_MOCK_PLAYMETRICS: true")
    logger.info("  - USE_MOCK_SERVICES: true")


class E2ETestRunner:
    """End-to-End test runner that orchestrates the complete video processing pipeline."""

    def __init__(self, config_path: str = None):
        self.config_path = config_path or str(
            Path(__file__).parent / "e2e_test_config.ini"
        )
        self.project_root = Path(__file__).parent.parent.parent

        # Load configuration
        self.config = load_config(self.config_path)
        setup_logging_from_config(self.config)

        # Process management
        self.video_grouper_process: Optional[subprocess.Popen] = None
        self.tray_process: Optional[subprocess.Popen] = None
        self.processes_to_cleanup: List[subprocess.Popen] = []

        # PID tracking file for persistent process management
        self.pid_file = self.project_root / "tests/e2e/test_pids.json"

        # Test data paths - use absolute paths based on project root
        self.test_data_path = self.project_root / "tests/e2e/test_data"
        self.test_logs_path = self.project_root / "tests/e2e/test_logs"

        # Log file paths for monitoring
        self.video_grouper_log_path = (
            self.test_logs_path / f"{self.config.logging.app_name}.log"
        )
        self.tray_log_path = self.test_logs_path / "tray_app.log"

        # Pipeline progress tracking
        self.pipeline_stages = {
            "camera_polling": False,
            "files_discovered": False,
            "downloads_started": False,
            "downloads_completed": False,
            "combining_started": False,
            "combining_completed": False,
            "match_info_queried": False,
            "team_info_populated": False,  # Phase 1: Team info from TeamSnap
            "ntfy_queued": False,  # NTFY tasks queued as "waiting for input"
            "ntfy_prompted": False,
            "timing_info_populated": False,  # Phase 2: Timing info from NTFY
            "trimming_started": False,
            "trimming_completed": False,
            "autocam_started": False,
            "autocam_completed": False,
            "upload_started": False,
            "upload_completed": False,
        }

        # Stage timestamps for timeout tracking
        self.stage_timestamps: Dict[str, datetime] = {}

        # Multi-group completion tracking: counts how many times key events occur.
        # With 2 groups, each count must reach 2 for the pipeline to be complete.
        self.expected_group_count = 2
        self.multi_group_counts: Dict[str, int] = {
            "groups_created": 0,
            "combines_completed": 0,
            "trims_completed": 0,
            "autocams_completed": 0,
            "uploads_completed": 0,
        }
        self.multi_group_patterns: Dict[str, List[str]] = {
            "groups_created": ["Created new group directory"],
            "combines_completed": [
                "VIDEO: Successfully completed task: CombineTask",
            ],
            "trims_completed": [
                "VIDEO: Successfully completed task: TrimTask",
            ],
            "autocams_completed": [
                "AUTOCAM: Successfully completed task",
            ],
            "uploads_completed": [
                "UPLOAD: Successfully completed task",
                "YOUTUBE_UPLOAD: Task completed successfully",
            ],
        }

        # Expected log patterns for each stage
        self.stage_patterns = {
            "camera_polling": [
                "CameraPoller: Polling camera",
                "CAMERA_POLLER: Looking for new files",
                "CAMERA_POLLER: Camera connected",
                "Successfully obtained ReoLink API token",
            ],
            "files_discovered": [
                "Found new recording files",
                "new files to process",
                "CAMERA_POLLER: Found",
                "Found.*recording files from ReoLink camera",
            ],
            "downloads_started": [
                "Starting download task",
                "Downloading file",
                "via Baichuan",
                "DOWNLOAD: Processing task",
                "DOWNLOAD: Starting download of",
            ],
            "downloads_completed": [
                "Download completed",
                "Successfully downloaded",
                "All downloads completed",
            ],
            "combining_started": [
                "Starting combine task",
                "Combining video files",
                "Queuing combine task",
                "Running ffmpeg combine command",
                "VIDEO: Processing task: CombineTask",
            ],
            "combining_completed": [
                "Combine completed",
                "Successfully combined",
                "Combined video created",
                "VIDEO: Successfully completed task: CombineTask",
                "VIDEO: Triggering API-based match info",
            ],
            "match_info_queried": [
                "Mock TeamSnap: Looking for games",
                "Mock PlayMetrics: Looking for games",
                "Querying TeamSnap for games",
                "Querying PlayMetrics for games",
                "Triggering API-based match info",
                "populate_match_info_from_apis",
            ],
            "team_info_populated": [
                "Successfully updated match_info.ini with TeamSnap data",
                "Updated match_info.ini with my_team_name",
                "Updated match_info.ini with opponent_team_name",
                "Updated match_info.ini with location",
            ],
            "ntfy_queued": [
                "NTFY: Marked",
                "as waiting for input",
                "mark_waiting_for_input",
                "waiting_for_input",
                "NTFY: Successfully sent notification for task",
                "Mark as waiting for input in the NTFY service",
                "NTFY: Processing task",
            ],
            "ntfy_prompted": [
                "NTFY: Prompting user",
                "Sending NTFY notification",
                "Game start time detection",
                "Mock NTFY API: Notification sent successfully",
                "NTFY API: Attempting to send notification",
                "Successfully sent NTFY notification",
                "Game Start Time",
            ],
            "timing_info_populated": [
                "Updated match_info.ini with game_start_time",
                "Updated match_info.ini with game_end_time",
                "Match info complete with timing information",
                "Game start time set to",
                "start_time_offset",
            ],
            "trimming_started": [
                "Starting trim task",
                "Trimming video file",
                "Queuing trim task",
                "NTFY_QUEUE: Queued trim task",
                "Created trim task",
                "NTFY_QUEUE: Created trim task",
                "VIDEO: Processing task: TrimTask",
            ],
            "trimming_completed": [
                "Trim completed",
                "Successfully trimmed",
                "Trimmed video created",
                "VIDEO: Successfully completed task: TrimTask",
            ],
            "autocam_started": [
                "Starting Once Autocam automation",
                "Starting autocam task",
                "Queuing autocam task",
                "AUTOCAM: Processing task",
            ],
            "autocam_completed": [
                "Automation script finished, closing application",
                "Autocam completed",
                "Autocam processing finished",
                "AUTOCAM: Successfully completed task",
            ],
            "upload_started": [
                "Starting upload task",
                "Uploading to YouTube",
                "Queuing upload task",
                "UPLOAD: Processing task",
                "Added item to queue: YoutubeUploadTask",
            ],
            "upload_completed": [
                "Upload completed",
                "Successfully uploaded",
                "Video uploaded to YouTube",
                "UPLOAD: Successfully completed task",
                "YOUTUBE_UPLOAD: Task completed successfully",
            ],
        }

    def _add_process_to_pid_file(self, process_name: str, process: subprocess.Popen):
        """Add a process to the PID tracking file."""
        try:
            # Load existing PIDs
            pids = {}
            if self.pid_file.exists():
                with open(self.pid_file, "r") as f:
                    pids = json.load(f)

            # Add new process
            pids[process_name] = process.pid

            # Save back to file
            with open(self.pid_file, "w") as f:
                json.dump(pids, f, indent=2)

            logger.info(f"Added {process_name} (PID: {process.pid}) to PID file")
        except Exception as e:
            logger.warning(f"Could not add {process_name} to PID file: {e}")

    def _kill_pids_from_file(self):
        """Kill any processes whose PIDs are stored in the PID file."""
        try:
            if self.pid_file.exists():
                with open(self.pid_file, "r") as f:
                    pids = json.load(f)
            else:
                logger.info("No PID file found")
                return
        except Exception as e:
            logger.warning(f"Could not load PID file: {e}")
            return

        if not pids:
            logger.info("PID file is empty")
            return

        logger.info(f"Found PID file with {len(pids)} processes to check")
        killed_count = 0

        for process_name, pid in pids.items():
            try:
                # Check if process is still running
                try:
                    import psutil

                    if psutil.pid_exists(pid):
                        proc = psutil.Process(pid)
                        logger.info(
                            f"Found existing {process_name} process (PID: {pid}), terminating..."
                        )
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                            logger.info(
                                f"Successfully terminated {process_name} (PID: {pid})"
                            )
                            killed_count += 1
                        except psutil.TimeoutExpired:
                            logger.warning(f"Force killing {process_name} (PID: {pid})")
                            proc.kill()
                            killed_count += 1
                    else:
                        logger.debug(
                            f"Process {process_name} (PID: {pid}) is no longer running"
                        )
                except ImportError:
                    # Fallback: use os.kill to check and terminate
                    import os

                    try:
                        os.kill(pid, signal.SIGTERM)
                        logger.info(f"Sent SIGTERM to {process_name} (PID: {pid})")
                        killed_count += 1
                    except (ProcessLookupError, PermissionError):
                        logger.debug(
                            f"Process {process_name} (PID: {pid}) is no longer running"
                        )
            except Exception as e:
                logger.warning(
                    f"Error checking process {process_name} (PID: {pid}): {e}"
                )

        if killed_count > 0:
            logger.info(f"Killed {killed_count} processes from PID file")
            time.sleep(2)

        # Clear the PID file
        try:
            if self.pid_file.exists():
                self.pid_file.unlink()
                logger.info("Cleared PID file")
        except Exception as e:
            logger.warning(f"Could not clear PID file: {e}")

    def _kill_existing_python_processes(self):
        """Kill any existing Python processes that might be from test runs."""
        try:
            import psutil

            killed_count = 0
            current_pid = os.getpid()
            logger.info("Checking for existing Python processes from test runs...")

            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    if proc.info["name"] == "python.exe":
                        # Skip the current process
                        if proc.info["pid"] == current_pid:
                            logger.debug(
                                f"Skipping current process (PID: {current_pid})"
                            )
                            continue

                        cmdline = proc.info["cmdline"]
                        if cmdline:
                            # Check if this is a process we might have started
                            cmd_str = " ".join(cmdline)
                            # Skip the current test runner process
                            if (
                                "test_runner.py" in cmd_str
                                and proc.info["pid"] == current_pid
                            ):
                                logger.debug(
                                    f"Skipping current test runner process (PID: {current_pid})"
                                )
                                continue
                            # Only kill specific test processes, not the test runner itself
                            if any(
                                keyword in cmd_str
                                for keyword in ["video_grouper", "run.py", "tray.main"]
                            ):
                                logger.info(
                                    f"Found test process (PID: {proc.info['pid']}): {cmd_str}"
                                )
                                proc.terminate()
                                try:
                                    proc.wait(timeout=3)
                                    logger.info(
                                        f"Successfully terminated process (PID: {proc.info['pid']})"
                                    )
                                    killed_count += 1
                                except psutil.TimeoutExpired:
                                    logger.warning(
                                        f"Force killing process (PID: {proc.info['pid']})"
                                    )
                                    proc.kill()
                                    killed_count += 1
                except (
                    psutil.NoSuchProcess,
                    psutil.AccessDenied,
                    psutil.ZombieProcess,
                ):
                    pass

            if killed_count > 0:
                logger.info(f"Killed {killed_count} test processes")
                time.sleep(2)
            else:
                logger.info("No test processes found")

        except ImportError:
            logger.warning("psutil not available, using fallback method")
            self._kill_existing_python_processes_fallback()
        except Exception as e:
            logger.warning(f"Error killing existing processes with psutil: {e}")
            self._kill_existing_python_processes_fallback()

    def _kill_existing_python_processes_fallback(self):
        """Fallback method to kill Python processes using Windows commands."""
        try:
            current_pid = os.getpid()
            # Use tasklist to find Python processes
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV"],
                capture_output=True,
                text=True,
                check=True,
            )

            lines = result.stdout.strip().split("\n")[1:]  # Skip header
            killed_count = 0

            for line in lines:
                if line.strip():
                    parts = line.strip('"').split('","')
                    if len(parts) >= 2:
                        pid = parts[1]
                        # Skip the current process
                        if int(pid) == current_pid:
                            logger.debug(
                                f"Skipping current process (PID: {current_pid})"
                            )
                            continue

                        try:
                            # Use wmic to get command line
                            wmic_result = subprocess.run(
                                [
                                    "wmic",
                                    "process",
                                    "where",
                                    f"ProcessId={pid}",
                                    "get",
                                    "CommandLine",
                                    "/FORMAT:CSV",
                                ],
                                capture_output=True,
                                text=True,
                                check=True,
                            )
                            cmd_str = wmic_result.stdout
                            # Skip the current test runner process
                            if "test_runner.py" in cmd_str and int(pid) == current_pid:
                                logger.debug(
                                    f"Skipping current test runner process (PID: {current_pid})"
                                )
                                continue
                            # Only kill specific test processes, not the test runner itself
                            if any(
                                keyword in cmd_str
                                for keyword in ["video_grouper", "run.py", "tray.main"]
                            ):
                                logger.info(
                                    f"Found test process (PID: {pid}), killing..."
                                )
                                subprocess.run(
                                    ["taskkill", "/PID", pid, "/F"], check=True
                                )
                                killed_count += 1
                        except subprocess.CalledProcessError:
                            pass  # Process might have already terminated

            if killed_count > 0:
                logger.info(f"Killed {killed_count} test processes")
                time.sleep(2)
            else:
                logger.info("No test processes found")

        except Exception as e:
            logger.warning(f"Error in fallback process killing: {e}")

    def _kill_autocam_gui_process(self):
        """Kill any running Autocam GUI.exe process from previous test runs."""
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/IM", "GUI.exe"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                logger.info("Killed Autocam GUI.exe process")
                time.sleep(2)
            else:
                logger.debug("No Autocam GUI.exe process found to kill")
        except Exception as e:
            logger.warning(f"Error killing Autocam GUI.exe: {e}")

    def setup_test_environment(self) -> bool:
        """Set up the test environment."""
        try:
            logger.info("=== Setting up E2E Test Environment ===")

            # Kill any existing processes from previous test runs
            logger.info("Cleaning up any existing processes from previous test runs...")
            self._kill_pids_from_file()
            self._kill_existing_python_processes()
            self._kill_autocam_gui_process()

            # Create test logs directory first
            self.test_logs_path.mkdir(parents=True, exist_ok=True)

            # Clean up any existing test data completely, but preserve YouTube credentials
            if self.test_data_path.exists():
                try:
                    logger.info(
                        f"Removing existing test data directory: {self.test_data_path}"
                    )

                    # Preserve YouTube credentials if they exist
                    youtube_credentials_backup = None
                    youtube_dir = self.test_data_path / "youtube"
                    if youtube_dir.exists():
                        youtube_credentials_backup = {
                            "client_secret": youtube_dir / "client_secret.json",
                            "token": youtube_dir / "token.json",
                        }
                        # Read the files before deletion
                        for key, file_path in youtube_credentials_backup.items():
                            if file_path.exists():
                                with open(file_path, "r") as f:
                                    youtube_credentials_backup[key] = f.read()

                    shutil.rmtree(self.test_data_path)
                    logger.info("Successfully removed test data directory")

                    # Restore YouTube credentials if they existed
                    if youtube_credentials_backup:
                        youtube_dir.mkdir(parents=True, exist_ok=True)
                        for key, content in youtube_credentials_backup.items():
                            if content:  # Only restore if we have content
                                file_path = youtube_dir / f"{key}.json"
                                with open(file_path, "w") as f:
                                    f.write(content)
                                logger.info(f"Restored YouTube credentials: {key}")

                except PermissionError as e:
                    logger.warning(f"Could not remove test data directory: {e}")
                    # Try to remove individual files and directories
                    for item in self.test_data_path.iterdir():
                        try:
                            if item.is_file():
                                item.unlink()
                                logger.info(f"Removed file: {item}")
                            elif item.is_dir():
                                shutil.rmtree(item)
                                logger.info(f"Removed directory: {item}")
                        except Exception as e:
                            logger.warning(f"Could not remove {item}: {e}")
                except Exception as e:
                    logger.error(f"Error removing test data directory: {e}")
                    return False

            # Recreate test data directory
            self.test_data_path.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created clean test data directory: {self.test_data_path}")

            # Copy YouTube credentials from shared_data to test_data
            youtube_source_dir = self.project_root / "shared_data" / "youtube"
            youtube_dest_dir = self.test_data_path / "youtube"
            if youtube_source_dir.exists():
                youtube_dest_dir.mkdir(parents=True, exist_ok=True)
                for file_name in ["client_secret.json", "token.json"]:
                    source_file = youtube_source_dir / file_name
                    dest_file = youtube_dest_dir / file_name
                    if source_file.exists():
                        shutil.copy2(source_file, dest_file)
                        logger.info(f"Copied YouTube credentials: {file_name}")
                    else:
                        logger.warning(
                            f"YouTube credentials file not found: {source_file}"
                        )
            else:
                logger.warning(
                    f"YouTube credentials directory not found: {youtube_source_dir}"
                )

            # Create latest_video.txt with yesterday at midnight to avoid timezone issues
            yesterday = datetime.now() - timedelta(days=1)
            yesterday_midnight = yesterday.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            latest_video_path = self.test_data_path / "latest_video.txt"
            with open(latest_video_path, "w") as f:
                f.write(yesterday_midnight.strftime("%Y-%m-%d %H:%M:%S"))
            logger.info(
                f"Created latest_video.txt with timestamp: {yesterday_midnight}"
            )

            # Clear any remaining state files that might exist
            state_files = [
                "ntfy_service_state.json",
                "download_queue_state.json",
                "video_queue_state.json",
                "upload_queue_state.json",
                "autocam_queue_state.json",
                "youtube_queue_state.json",
            ]

            for state_file in state_files:
                state_path = self.test_data_path / state_file
                if state_path.exists():
                    try:
                        state_path.unlink()
                        logger.info(f"Removed state file: {state_file}")
                    except Exception as e:
                        logger.warning(f"Could not remove state file {state_file}: {e}")

            # Set environment variables for mock services
            setup_e2e_environment(self.project_root)

            # Start Docker camera simulator
            if not self._start_simulator_container():
                return False

            logger.info("[OK] Test environment set up")
            logger.info(f"  - Test data path: {self.test_data_path}")
            logger.info(f"  - Test logs path: {self.test_logs_path}")
            logger.info(f"  - Video grouper log: {self.video_grouper_log_path}")
            return True

        except Exception as e:
            logger.error(f"Failed to set up test environment: {e}")
            return False

    def _start_simulator_container(self) -> bool:
        """Start the Docker camera simulator container and wait for it to be ready."""
        try:
            logger.info("Starting Docker camera simulator...")

            # Stop any existing simulator container first
            subprocess.run(
                ["docker", "compose", "--profile", "reolink", "down"],
                cwd=str(self.project_root),
                capture_output=True,
                timeout=30,
            )

            # Build and start the simulator
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "--profile",
                    "reolink",
                    "up",
                    "-d",
                    "--build",
                    "reolink-simulator",
                ],
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                logger.error(f"Failed to start simulator: {result.stderr}")
                return False

            # Wait for simulator to be ready (health check on HTTP port)
            import urllib.request

            for attempt in range(30):
                try:
                    req = urllib.request.Request(
                        "http://127.0.0.1:8180/cgi-bin/api.cgi?cmd=Login&token=null",
                        method="GET",
                    )
                    urllib.request.urlopen(req, timeout=2)
                    logger.info(f"Camera simulator ready (attempt {attempt + 1})")
                    return True
                except Exception:
                    time.sleep(2)

            logger.error("Camera simulator did not become ready within 60 seconds")
            return False

        except Exception as e:
            logger.error(f"Failed to start simulator container: {e}")
            return False

    def _stop_simulator_container(self):
        """Stop the Docker camera simulator container."""
        try:
            logger.info("Stopping Docker camera simulator...")
            subprocess.run(
                ["docker", "compose", "--profile", "reolink", "down"],
                cwd=str(self.project_root),
                capture_output=True,
                timeout=30,
            )
            logger.info("Camera simulator stopped")
        except Exception as e:
            logger.warning(f"Error stopping simulator: {e}")

    def start_video_grouper_app(self) -> bool:
        """Start the video_grouper application as a subprocess with file logging."""
        try:
            logger.info("=== Starting Video Grouper Application ===")

            # Build the command to start video_grouper with absolute paths
            config_path_abs = os.path.abspath(self.config_path)
            run_script_abs = os.path.abspath(self.project_root / "run.py")

            # Create a dedicated log file for this subprocess
            subprocess_log_path = self.test_logs_path / "video_grouper_subprocess.log"

            cmd = ["uv", "run", "python", run_script_abs, "--config", config_path_abs]

            logger.info(f"Starting video_grouper with command: {' '.join(cmd)}")
            logger.info(f"Subprocess log file: {subprocess_log_path}")

            # Set up environment variables for mock services
            env = os.environ.copy()
            # Environment variables are already set by setup_e2e_environment, just copy them

            # Start the process with logging to file
            with open(subprocess_log_path, "w") as log_file:
                self.video_grouper_process = subprocess.Popen(
                    cmd,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                    env=env,
                    cwd=str(self.project_root),
                )

            self.processes_to_cleanup.append(self.video_grouper_process)
            self._add_process_to_pid_file("video_grouper", self.video_grouper_process)

            # Give it a moment to start
            time.sleep(3)

            # Check if process is still running
            if self.video_grouper_process.poll() is not None:
                logger.error("Video grouper process failed to start")
                return False

            logger.info(
                f"[OK] Video Grouper application started (PID: {self.video_grouper_process.pid})"
            )
            logger.info(f"[OK] Logging to: {subprocess_log_path}")
            return True

        except Exception as e:
            logger.error(f"Failed to start video grouper app: {e}")
            return False

    def start_tray_app(self) -> bool:
        """Start the tray application as a subprocess with file logging."""
        try:
            logger.info("=== Starting Tray Application ===")

            # Kill any existing tray agent processes and clean up lock files
            self._kill_existing_tray_agents()
            self._cleanup_tray_lock_file()

            # Build the command to start tray agent with absolute paths
            config_path_abs = os.path.abspath(self.config_path)

            # Create a dedicated log file for this subprocess
            subprocess_log_path = self.test_logs_path / "tray_subprocess.log"

            cmd = [
                "uv",
                "run",
                "python",
                "-m",
                "video_grouper.tray.main",
                config_path_abs,
            ]

            logger.info(f"Starting tray agent with command: {' '.join(cmd)}")
            logger.info(f"Tray subprocess log file: {subprocess_log_path}")

            # Set up environment variables for the tray agent
            env = os.environ.copy()
            # Environment variables are already set by setup_e2e_environment, just copy them

            # Start the process with logging to file
            with open(subprocess_log_path, "w") as log_file:
                self.tray_process = subprocess.Popen(
                    cmd,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                    env=env,
                    cwd=str(self.project_root),
                )

            self.processes_to_cleanup.append(self.tray_process)
            self._add_process_to_pid_file("tray", self.tray_process)

            # Wait for the tray agent to acquire its lock file (up to 60 seconds)
            config_dir = Path(self.config_path).parent
            lock_file_path = config_dir / "tray_agent.lock"
            tray_start_deadline = time.time() + 60
            while time.time() < tray_start_deadline:
                # Check if process crashed
                if self.tray_process.poll() is not None:
                    logger.error("Tray agent process failed to start")
                    self._cleanup_tray_lock_file()
                    return False
                if lock_file_path.exists():
                    break
                time.sleep(2)

            if not lock_file_path.exists():
                logger.error(
                    "Tray agent lock file not found after 60s - tray agent may not be running properly"
                )
                return False

            logger.info(f"[OK] Tray agent started (PID: {self.tray_process.pid})")
            logger.info(f"[OK] Logging to: {subprocess_log_path}")
            logger.info(f"[OK] Tray agent lock file created: {lock_file_path}")
            return True

        except Exception as e:
            logger.error(f"Failed to start tray app: {e}")
            return False

    def _kill_existing_tray_agents(self):
        """Kill any existing tray agent processes to ensure only one runs at a time."""
        killed_count = 0

        try:
            import psutil

            # Find all Python processes that might be tray agents
            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    if proc.info["name"] == "python.exe":
                        cmdline = proc.info["cmdline"]
                        # Look for specific tray agent command line patterns
                        if cmdline and any(
                            "video_grouper.tray.main" in arg for arg in cmdline
                        ):
                            logger.info(
                                f"Found tray agent process (PID: {proc.info['pid']}), terminating..."
                            )
                            proc.terminate()
                            try:
                                proc.wait(
                                    timeout=5
                                )  # Wait up to 5 seconds for graceful termination
                                logger.info(
                                    f"Successfully terminated tray agent process (PID: {proc.info['pid']})"
                                )
                                killed_count += 1
                            except psutil.TimeoutExpired:
                                logger.warning(
                                    f"Force killing tray agent process (PID: {proc.info['pid']})"
                                )
                                proc.kill()
                                killed_count += 1
                except (
                    psutil.NoSuchProcess,
                    psutil.AccessDenied,
                    psutil.ZombieProcess,
                ):
                    # Process might have already terminated or we don't have permission
                    pass

            # Give a moment for processes to terminate
            if killed_count > 0:
                time.sleep(2)
                logger.info(f"Killed {killed_count} existing tray agent process(es)")

        except ImportError:
            logger.warning("psutil not available, trying fallback method")
            self._kill_existing_tray_agents_fallback()
        except Exception as e:
            logger.warning(f"Error killing existing tray agents with psutil: {e}")
            self._kill_existing_tray_agents_fallback()

    def _kill_existing_tray_agents_fallback(self):
        """Fallback method to kill tray agents using Windows tasklist/taskkill commands."""
        try:
            import subprocess

            # Use tasklist to find Python processes
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV"],
                capture_output=True,
                text=True,
                check=True,
            )

            lines = result.stdout.strip().split("\n")[1:]  # Skip header
            for line in lines:
                if line.strip():
                    parts = line.strip('"').split('","')
                    if len(parts) >= 2:
                        pid = parts[1]
                        # Check if this process is running our tray agent
                        try:
                            # Use wmic to get command line
                            wmic_result = subprocess.run(
                                [
                                    "wmic",
                                    "process",
                                    "where",
                                    f"ProcessId={pid}",
                                    "get",
                                    "CommandLine",
                                    "/FORMAT:CSV",
                                ],
                                capture_output=True,
                                text=True,
                                check=True,
                            )
                            if "video_grouper.tray.main" in wmic_result.stdout:
                                logger.info(
                                    f"Found tray agent process (PID: {pid}), killing..."
                                )
                                subprocess.run(
                                    ["taskkill", "/PID", pid, "/F"], check=True
                                )
                                logger.info(
                                    f"Successfully killed tray agent process (PID: {pid})"
                                )
                        except subprocess.CalledProcessError:
                            pass  # Process might have already terminated

        except Exception as e:
            logger.warning(f"Error in fallback tray agent killing: {e}")

    def _check_processes_running(self) -> bool:
        """Check if all processes are still running."""
        if self.video_grouper_process and self.video_grouper_process.poll() is not None:
            logger.error("Video grouper process has stopped")
            return False
        if self.tray_process and self.tray_process.poll() is not None:
            logger.error("Tray process has stopped")
            # Clean up any stale lock files when tray process stops
            self._cleanup_tray_lock_file()
            return False
        return True

    def _cleanup_tray_lock_file(self):
        """Clean up any stale tray agent lock files."""
        try:
            config_dir = Path(self.config_path).parent
            lock_file_path = config_dir / "tray_agent.lock"
            if lock_file_path.exists():
                logger.info(f"Removing stale tray agent lock file: {lock_file_path}")
                try:
                    lock_file_path.unlink()
                    logger.info(
                        f"Successfully removed tray agent lock file: {lock_file_path}"
                    )
                except PermissionError:
                    logger.warning(
                        f"Permission denied removing lock file: {lock_file_path}"
                    )
                    # Try to force remove on Windows
                    try:
                        import os

                        os.remove(str(lock_file_path))
                        logger.info(
                            f"Force removed tray agent lock file: {lock_file_path}"
                        )
                    except Exception as e2:
                        logger.error(f"Could not force remove lock file: {e2}")
                except FileNotFoundError:
                    logger.info(f"Lock file already removed: {lock_file_path}")
                except Exception as e:
                    logger.error(f"Unexpected error removing lock file: {e}")
            else:
                logger.debug(f"No tray agent lock file found at: {lock_file_path}")
        except Exception as e:
            logger.warning(f"Error in tray agent lock file cleanup: {e}")

    def _read_log_file(self, log_path: Path) -> str:
        """Read the contents of a log file."""
        try:
            if log_path.exists():
                with open(log_path, "r", encoding="utf-8") as f:
                    return f.read()
            return ""
        except Exception as e:
            logger.warning(f"Could not read log file {log_path}: {e}")
            return ""

    def _check_ntfy_service_state(self) -> bool:
        """
        Check if NTFY tasks are queued as "waiting for input" in the NTFY service state.

        Returns:
            True if any NTFY tasks are in "waiting_for_input" status
        """
        try:
            # Look for NTFY service state file in the correct location
            ntfy_state_file = self.test_data_path / "ntfy_service_state.json"

            if not ntfy_state_file.exists():
                logger.debug("NTFY service state file not found")
                return False

            # Read the NTFY service state
            with open(ntfy_state_file, "r", encoding="utf-8") as f:
                state = json.load(f)

            # Check for pending tasks with "waiting_for_input" status
            pending_tasks = state.get("pending_tasks", {})

            for group_dir, task_data in pending_tasks.items():
                status = task_data.get("status")
                if status == "waiting_for_input":
                    logger.info(f"Found NTFY task waiting for input in {group_dir}")
                    return True

            logger.debug(
                f"No NTFY tasks found in waiting_for_input status. Pending tasks: {pending_tasks}"
            )
            return False

        except Exception as e:
            logger.error(f"Error checking NTFY service state: {e}")
            return False

    def _check_match_info_completion(self, group_dir: str) -> Dict[str, bool]:
        """
        Check if match_info.ini is properly populated with team info and timing info.

        Args:
            group_dir: Directory containing the match_info.ini file

        Returns:
            Dict with 'team_info_complete' and 'timing_info_complete' flags
        """
        match_info_path = Path(group_dir) / "match_info.ini"

        if not match_info_path.exists():
            return {"team_info_complete": False, "timing_info_complete": False}

        try:
            # Parse the match_info.ini file properly
            import configparser

            config = configparser.ConfigParser()
            config.read(match_info_path, encoding="utf-8")

            # Check for team info (Phase 1) - ensure fields exist and have values
            team_info_complete = False
            if "MATCH" in config:
                match_section = config["MATCH"]
                team_info_complete = bool(
                    match_section.get("my_team_name", "").strip()
                    and match_section.get("opponent_team_name", "").strip()
                    and match_section.get("location", "").strip()
                )

            # Check for timing info (Phase 2) - ensure start_time_offset has a value
            timing_info_complete = False
            if "MATCH" in config:
                match_section = config["MATCH"]
                timing_info_complete = bool(
                    match_section.get("start_time_offset", "").strip()
                )

            logger.info(
                f"Match info check for {group_dir}: team_info={team_info_complete}, timing_info={timing_info_complete}"
            )

            return {
                "team_info_complete": team_info_complete,
                "timing_info_complete": timing_info_complete,
            }

        except Exception as e:
            logger.error(f"Error checking match_info.ini in {group_dir}: {e}")
            return {"team_info_complete": False, "timing_info_complete": False}

    def _check_stage_completion(self, stage: str, log_content: str) -> bool:
        """Check if a specific stage has been completed based on log patterns."""
        # Use the stage patterns defined in the class instead of local patterns
        if stage in self.stage_patterns:
            return any(pattern in log_content for pattern in self.stage_patterns[stage])

        return False

    def _update_pipeline_progress(self, log_content: str) -> Dict[str, bool]:
        """Update pipeline progress based on log content."""
        # Also check tray agent logs for autocam processing
        tray_log_path = self.test_logs_path / "tray_subprocess.log"
        tray_log_content = self._read_log_file(tray_log_path)
        combined_log_content = log_content + "\n" + tray_log_content

        updated_stages = {}

        for stage in self.pipeline_stages:
            if not self.pipeline_stages[stage]:  # Only check uncompleted stages
                stage_completed = False

                # Special handling for match_info.ini file checks
                if stage == "team_info_populated" or stage == "timing_info_populated":
                    # Check actual match_info.ini files in test data directories
                    stage_completed = self._check_match_info_files(stage)
                elif stage == "ntfy_queued":
                    # Check NTFY service state for queued tasks, or fall back to log patterns
                    stage_completed = (
                        self._check_ntfy_service_state()
                        or self._check_stage_completion(stage, combined_log_content)
                    )
                else:
                    # Check log patterns for other stages
                    stage_completed = self._check_stage_completion(
                        stage, combined_log_content
                    )

                if stage_completed:
                    self.pipeline_stages[stage] = True
                    self.stage_timestamps[stage] = datetime.now()
                    updated_stages[stage] = True
                    logger.info(f"Pipeline stage completed: {stage}")

        return updated_stages

    def _check_match_info_files(self, stage: str) -> bool:
        """
        Check match_info.ini files for team_info_populated or timing_info_populated stages.

        Args:
            stage: Either 'team_info_populated' or 'timing_info_populated'

        Returns:
            True if any match_info.ini file meets the criteria for the stage
        """
        if not self.test_data_path.exists():
            return False

        try:
            # Look for directories that contain match_info.ini files
            for group_dir in self.test_data_path.iterdir():
                if group_dir.is_dir():
                    match_info_check = self._check_match_info_completion(str(group_dir))

                    if (
                        stage == "team_info_populated"
                        and match_info_check["team_info_complete"]
                    ):
                        logger.info(f"Found team info populated in {group_dir}")
                        return True
                    elif (
                        stage == "timing_info_populated"
                        and match_info_check["timing_info_complete"]
                    ):
                        logger.info(f"Found timing info populated in {group_dir}")
                        return True

            return False

        except Exception as e:
            logger.error(f"Error checking match_info files for stage {stage}: {e}")
            return False

    def _update_multi_group_progress(self, log_content: str) -> bool:
        """
        Count occurrences of multi-group patterns in the log.

        Returns True if any count increased (resets inactivity timeout).
        """
        changed = False
        for key, patterns in self.multi_group_patterns.items():
            # Count total occurrences of any pattern for this key
            count = 0
            for pattern in patterns:
                count += log_content.count(pattern)
            if count > self.multi_group_counts[key]:
                old = self.multi_group_counts[key]
                self.multi_group_counts[key] = count
                changed = True
                logger.info(
                    f"Multi-group progress: {key} = {count}/{self.expected_group_count} "
                    f"(was {old})"
                )
        return changed

    def _all_groups_complete(self) -> bool:
        """Check if all multi-group counts have reached the expected count."""
        return all(
            count >= self.expected_group_count
            for count in self.multi_group_counts.values()
        )

    def _validate_group_assignments(self) -> bool:
        """
        Validate that each group directory got the correct opponent assignment.

        Group 1 (earlier recording) should have opponent = Eagles.
        Group 2 (later recording) should have opponent = Falcons.
        """
        import configparser

        if not self.test_data_path.exists():
            logger.error("Test data path does not exist for validation")
            return False

        # Find group directories (skip non-group dirs like 'youtube')
        group_dirs = []
        for item in sorted(self.test_data_path.iterdir()):
            if item.is_dir() and (item / "match_info.ini").exists():
                group_dirs.append(item)

        if len(group_dirs) < self.expected_group_count:
            logger.error(
                f"Expected {self.expected_group_count} group directories with "
                f"match_info.ini, found {len(group_dirs)}: {group_dirs}"
            )
            return False

        # Validate opponents in order (sorted by directory name = chronological)
        expected_opponents = ["Eagles", "Falcons"]
        all_valid = True

        for i, (group_dir, expected_opponent) in enumerate(
            zip(group_dirs, expected_opponents)
        ):
            config = configparser.ConfigParser()
            config.read(group_dir / "match_info.ini", encoding="utf-8")

            actual_opponent = ""
            if "MATCH" in config:
                actual_opponent = config["MATCH"].get("opponent_team_name", "").strip()

            if actual_opponent == expected_opponent:
                logger.info(
                    f"Group {i + 1} ({group_dir.name}): "
                    f"opponent = {actual_opponent} -- CORRECT"
                )
            else:
                logger.error(
                    f"Group {i + 1} ({group_dir.name}): "
                    f"opponent = '{actual_opponent}', expected '{expected_opponent}'"
                )
                all_valid = False

        return all_valid

    def _check_timeout(
        self, last_activity_time: datetime, timeout_seconds: int = 65
    ) -> bool:
        """Check if we've exceeded the timeout since last activity."""
        if last_activity_time is None:
            return False

        elapsed = (datetime.now() - last_activity_time).total_seconds()
        return elapsed > timeout_seconds

    def monitor_pipeline_progress(self, max_wait_minutes: int = 10) -> bool:
        """Monitor the pipeline progress through log files."""
        logger.info("=== Monitoring Pipeline Progress ===")

        start_time = datetime.now()
        max_wait_seconds = max_wait_minutes * 60
        last_pipeline_change_time = start_time
        last_log_size = 0

        # Check both the subprocess log and the main application log
        subprocess_log_path = self.test_logs_path / "video_grouper_subprocess.log"
        main_log_path = self.video_grouper_log_path

        while True:
            # Check if we've exceeded the maximum wait time
            elapsed = (datetime.now() - start_time).total_seconds()
            if elapsed > max_wait_seconds:
                logger.error(f"Test timeout after {max_wait_minutes} minutes")
                return False

            # Check if processes are still running
            if not self._check_processes_running():
                logger.error("One or more processes have stopped unexpectedly")
                return False

            # Read log files
            subprocess_log_content = self._read_log_file(subprocess_log_path)
            main_log_content = self._read_log_file(main_log_path)
            combined_log_content = subprocess_log_content + "\n" + main_log_content

            # Check for new log content (for debugging, but don't update timeout)
            current_log_size = len(combined_log_content)
            if current_log_size > last_log_size:
                logger.info(
                    f"New log content detected ({current_log_size - last_log_size} bytes)"
                )
                last_log_size = current_log_size

            # Update pipeline progress and track when stages actually change
            # Include tray log for autocam patterns (tray agent handles autocam)
            tray_log_path = self.test_logs_path / "tray_subprocess.log"
            tray_log_content = self._read_log_file(tray_log_path)
            all_logs = combined_log_content + "\n" + tray_log_content

            updated_stages = self._update_pipeline_progress(combined_log_content)
            multi_group_changed = self._update_multi_group_progress(all_logs)

            if updated_stages or multi_group_changed:
                last_pipeline_change_time = datetime.now()
                if updated_stages:
                    logger.info(
                        f"Pipeline stages updated: {list(updated_stages.keys())}"
                    )

            # Use multi-group counts (not booleans) to detect if heavy stages
            # are still in progress for group 2 after group 1 completes.
            autocam_started = self.pipeline_stages.get("autocam_started", False)
            autocams_done = self.multi_group_counts["autocams_completed"]
            upload_started = self.pipeline_stages.get("upload_started", False)
            uploads_done = self.multi_group_counts["uploads_completed"]

            ntfy_queued = self.pipeline_stages.get("ntfy_queued", False)
            timing_populated = self.pipeline_stages.get("timing_info_populated", False)

            combining_started = self.pipeline_stages.get("combining_started", False)
            combines_done = self.multi_group_counts["combines_completed"]
            trimming_started = self.pipeline_stages.get("trimming_started", False)
            trims_done = self.multi_group_counts["trims_completed"]

            if autocam_started and autocams_done < self.expected_group_count:
                # Autocam is running - heavy AI processing takes up to 90 minutes
                timeout_seconds = 5400  # 90 minutes
                timeout_description = "90 minutes (autocam processing)"
            elif upload_started and uploads_done < self.expected_group_count:
                # Upload is running - give it 10 minutes for large files
                timeout_seconds = 600  # 10 minutes
                timeout_description = "10 minutes (YouTube upload processing)"
            elif ntfy_queued and not timing_populated:
                # NTFY conversation in progress - give 3 minutes for multiple rounds
                timeout_seconds = 180  # 3 minutes
                timeout_description = "3 minutes (NTFY conversation)"
            elif combining_started and combines_done < self.expected_group_count:
                # FFmpeg combining large files - give 10 minutes
                timeout_seconds = 600  # 10 minutes
                timeout_description = "10 minutes (FFmpeg combining)"
            elif trimming_started and trims_done < self.expected_group_count:
                # FFmpeg trimming large video - give 10 minutes
                timeout_seconds = 600  # 10 minutes
                timeout_description = "10 minutes (FFmpeg trimming)"
            else:
                # Other stages - use 90 seconds (includes real FFmpeg processing)
                timeout_seconds = 90
                timeout_description = "90 seconds"

            # Check for timeout since last pipeline change
            if self._check_timeout(
                last_pipeline_change_time, timeout_seconds=timeout_seconds
            ):
                logger.error(
                    f"No pipeline stage changes for {timeout_description}. "
                    f"Last change: {last_pipeline_change_time}"
                )
                logger.error("Current pipeline status:")
                for stage, completed in self.pipeline_stages.items():
                    status = "DONE" if completed else "PENDING"
                    logger.error(f"  {status} {stage}")
                logger.error("Multi-group completion counts:")
                for key, count in self.multi_group_counts.items():
                    status = "DONE" if count >= self.expected_group_count else "PENDING"
                    logger.error(
                        f"  {status} {key}: {count}/{self.expected_group_count}"
                    )
                return False

            # Check if fully complete: all boolean stages AND all groups done
            all_stages_complete = all(self.pipeline_stages.values())
            all_groups_complete = self._all_groups_complete()

            if all_stages_complete and all_groups_complete:
                logger.info("All pipeline stages and multi-group completions done!")
                # Validate correct game assignment per group
                if self._validate_group_assignments():
                    logger.info("Group assignment validation passed!")
                    return True
                else:
                    logger.error("Group assignment validation FAILED!")
                    return False

            # Log current progress
            completed_count = sum(self.pipeline_stages.values())
            total_count = len(self.pipeline_stages)
            group_summary = ", ".join(
                f"{k}={v}/{self.expected_group_count}"
                for k, v in self.multi_group_counts.items()
            )
            logger.info(
                f"Pipeline progress: {completed_count}/{total_count} stages, "
                f"groups: [{group_summary}]"
            )

            # Wait before next check
            time.sleep(5)

    async def cleanup_test_environment(self):
        """Clean up the test environment."""
        logger.info("=== Cleaning up Test Environment ===")

        # Stop all processes
        for process in self.processes_to_cleanup:
            if (
                process and hasattr(process, "poll") and process.poll() is None
            ):  # Process is still running
                try:
                    logger.info(f"Terminating process {process.pid}")
                    process.terminate()

                    # Wait up to 5 seconds for graceful shutdown
                    try:
                        process.wait(timeout=5)
                        logger.info(f"Process {process.pid} terminated gracefully")
                    except subprocess.TimeoutExpired:
                        logger.warning(
                            f"Process {process.pid} did not terminate gracefully, killing it"
                        )
                        process.kill()
                        try:
                            process.wait(timeout=2)
                            logger.info(f"Process {process.pid} killed")
                        except subprocess.TimeoutExpired:
                            logger.error(f"Process {process.pid} could not be killed")

                except Exception as e:
                    logger.warning(f"Error stopping process {process.pid}: {e}")

        # Ensure all tray agent processes are killed
        logger.info("Ensuring all tray agent processes are terminated...")
        self._kill_existing_tray_agents()
        self._kill_autocam_gui_process()

        # Kill any processes from PID file
        logger.info("Killing any remaining processes from PID file...")
        self._kill_pids_from_file()

        # Wait a moment for processes to fully terminate
        time.sleep(2)

        # Clean up any remaining lock files
        logger.info("Cleaning up tray agent lock files...")
        self._cleanup_tray_lock_file()

        # Stop Docker camera simulator
        self._stop_simulator_container()

        # NTFY response service is now stopped by the video_grouper application

        # Clean up test data (optional - keep for debugging)
        logger.info("Test data and logs preserved for debugging")
        logger.info(f"  - Test data: {self.test_data_path}")
        logger.info(f"  - Test logs: {self.test_logs_path}")

    async def run_e2e_test(self) -> bool:
        """Run the complete end-to-end test."""
        try:
            logger.info("Starting End-to-End Test")

            # Set up test environment
            if not self.setup_test_environment():
                return False

            # Start applications
            if not self.start_video_grouper_app():
                return False

            if not self.start_tray_app():
                return False

            # NTFY response service is now started by the video_grouper application
            # when USE_MOCK_NTFY environment variable is set

            # Monitor pipeline progress
            success = self.monitor_pipeline_progress(max_wait_minutes=120)

            return success

        except Exception as e:
            logger.error(f"E2E test failed with exception: {e}")
            return False
        finally:
            await self.cleanup_test_environment()
            # Final cleanup to ensure no processes are left running
            logger.info("Final cleanup: ensuring no test processes remain...")
            self._kill_pids_from_file()
            self._kill_existing_tray_agents()
            self._kill_autocam_gui_process()
            self._kill_existing_python_processes()
            time.sleep(1)  # Brief wait for processes to terminate
            self._cleanup_tray_lock_file()

            # Try one more time after a longer wait
            time.sleep(2)
            self._cleanup_tray_lock_file()


class TestE2EPipeline:
    """Pytest test class for the E2E pipeline."""

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_complete_pipeline(self):
        """Test the complete video processing pipeline end-to-end."""
        runner = E2ETestRunner()
        success = await runner.run_e2e_test()
        assert success, "E2E pipeline test failed"


async def main():
    """Main function to run the E2E test."""
    runner = E2ETestRunner()

    def signal_handler(signum, frame):
        logger.info("Received interrupt signal, cleaning up...")
        asyncio.create_task(runner.cleanup_test_environment())
        runner._kill_pids_from_file()
        runner._kill_existing_tray_agents()
        runner._kill_autocam_gui_process()
        runner._kill_existing_python_processes()
        runner._cleanup_tray_lock_file()
        sys.exit(1)

    # Set up signal handlers for cleanup
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        success = await runner.run_e2e_test()

        if success:
            logger.info("E2E Test PASSED!")
            return 0
        else:
            logger.error("E2E Test FAILED!")
            return 1
    except KeyboardInterrupt:
        logger.info("Test interrupted by user")
        return 1


if __name__ == "__main__":
    import asyncio

    exit_code = asyncio.run(main())
    sys.exit(exit_code)
