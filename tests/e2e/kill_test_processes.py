#!/usr/bin/env python3
"""
Script to kill any remaining test processes from E2E tests.

This script can be run manually to clean up any processes that might have been
left running from previous test runs.
"""

import json
import logging
import sys
import time
from pathlib import Path

# Add project root to path for imports
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

logger = logging.getLogger(__name__)


def setup_logging():
    """Set up basic logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def kill_existing_python_processes():
    """Kill any existing Python processes that might be from test runs."""
    try:
        import psutil

        killed_count = 0
        logger.info("Checking for existing Python processes from test runs...")

        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if proc.info["name"] == "python.exe":
                    cmdline = proc.info["cmdline"]
                    if cmdline:
                        # Check if this is a process we might have started
                        cmd_str = " ".join(cmdline)
                        if any(
                            keyword in cmd_str
                            for keyword in [
                                "video_grouper",
                                "run.py",
                                "tray.main",
                                "test_runner",
                            ]
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
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass

        if killed_count > 0:
            logger.info(f"Killed {killed_count} test processes")
            time.sleep(2)
        else:
            logger.info("No test processes found")

    except ImportError:
        logger.warning("psutil not available, using fallback method")
        kill_existing_python_processes_fallback()
    except Exception as e:
        logger.warning(f"Error killing existing processes with psutil: {e}")
        kill_existing_python_processes_fallback()


def kill_existing_python_processes_fallback():
    """Fallback method to kill Python processes using Windows commands."""
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
        killed_count = 0

        for line in lines:
            if line.strip():
                parts = line.strip('"').split('","')
                if len(parts) >= 2:
                    pid = parts[1]
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
                        if any(
                            keyword in cmd_str
                            for keyword in [
                                "video_grouper",
                                "run.py",
                                "tray.main",
                                "test_runner",
                            ]
                        ):
                            logger.info(f"Found test process (PID: {pid}), killing...")
                            subprocess.run(["taskkill", "/PID", pid, "/F"], check=True)
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


def kill_pids_from_file():
    """Kill any processes whose PIDs are stored in the PID file."""
    pid_file = project_root / "tests/e2e/test_pids.json"

    try:
        if pid_file.exists():
            with open(pid_file, "r") as f:
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
            import psutil

            if psutil.pid_exists(pid):
                proc = psutil.Process(pid)
                logger.info(
                    f"Found existing {process_name} process (PID: {pid}), terminating..."
                )
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                    logger.info(f"Successfully terminated {process_name} (PID: {pid})")
                    killed_count += 1
                except psutil.TimeoutExpired:
                    logger.warning(f"Force killing {process_name} (PID: {pid})")
                    proc.kill()
                    killed_count += 1
            else:
                logger.debug(
                    f"Process {process_name} (PID: {pid}) is no longer running"
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            logger.debug(f"Process {process_name} (PID: {pid}) is no longer accessible")
        except Exception as e:
            logger.warning(f"Error checking process {process_name} (PID: {pid}): {e}")

    if killed_count > 0:
        logger.info(f"Killed {killed_count} processes from PID file")
        time.sleep(2)

    # Clear the PID file
    try:
        if pid_file.exists():
            pid_file.unlink()
            logger.info("Cleared PID file")
    except Exception as e:
        logger.warning(f"Could not clear PID file: {e}")


def cleanup_tray_lock_file():
    """Clean up any stale tray agent lock files."""
    try:
        config_dir = project_root / "tests/e2e"
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
                    logger.info(f"Force removed tray agent lock file: {lock_file_path}")
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


def main():
    """Main function to kill all test processes."""
    setup_logging()

    logger.info("=== Killing Test Processes ===")

    # Kill processes from PID file
    kill_pids_from_file()

    # Kill any remaining Python processes
    kill_existing_python_processes()

    # Clean up lock files
    cleanup_tray_lock_file()

    logger.info("=== Process Cleanup Complete ===")


if __name__ == "__main__":
    main()
