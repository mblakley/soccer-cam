import sys
import os
import time
import atexit
import asyncio
import threading
from pathlib import Path
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PyQt6.QtCore import (
    QRunnable,
    QThreadPool,
    QObject,
    pyqtSignal as Signal,
    pyqtSlot as Slot,
)
from PyQt6.QtGui import QIcon, QAction
import win32serviceutil

from video_grouper.tray.autocam_automation import run_autocam_on_file
from video_grouper.update.update_manager import check_and_update
from video_grouper.version import get_version, get_full_version
from video_grouper.utils.youtube_upload import authenticate_youtube
from video_grouper.utils.paths import get_shared_data_path
from video_grouper.utils.config import load_config, Config
from video_grouper.task_processors import BallTrackingProcessor
from video_grouper.task_processors.register_tasks import register_all_tasks

# NOTE: ``register_providers`` is imported lazily inside the autocam_gui
# branch of __init__. Importing it eagerly here would pull in the
# homegrown ONNX stack (cv2 + onnxruntime + CUDA DLLs), which the tray
# never needs — it only runs autocam_gui ball-tracking. Doing it lazy
# keeps the tray bootable on machines without GPU drivers.
import webbrowser
from typing import Optional

from video_grouper.utils.logger import setup_logging, get_logger

# Configure logging - get_shared_data_path() handles PyInstaller vs dev
setup_logging(level="DEBUG", app_name="video_grouper_tray")
logger = get_logger(__name__)


class UpdateChecker(threading.Thread):
    def __init__(self, version, github_repo, signal):
        super().__init__()
        self.version = version
        self.github_repo = github_repo
        self.signal = signal
        self.daemon = True

    def run(self):
        while True:
            try:
                # Create event loop for this thread
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                # Check for updates
                has_update = loop.run_until_complete(
                    check_and_update(self.version, self.github_repo)
                )

                if has_update:
                    self.signal.emit("Update available! Click to install.")

                loop.close()

            except Exception as e:
                logger.error(f"Error checking for updates: {e}")

            # Sleep for an hour
            threading.Event().wait(3600)


class RunnerSignals(QObject):
    finished = Signal(Path, bool)


class AutocamRunner(QRunnable):
    def __init__(
        self, input_path: str, output_path: str, group_dir: Path, autocam_config
    ):
        super().__init__()
        self.input_path = input_path
        self.output_path = output_path
        self.group_dir = group_dir
        self.autocam_config = autocam_config
        self.signals = RunnerSignals()

    @Slot()
    def run(self):
        try:
            # Assuming run_autocam_on_file returns True on success, False on failure
            success = run_autocam_on_file(
                self.autocam_config, self.input_path, self.output_path
            )
            self.signals.finished.emit(self.group_dir, success)
        except Exception as e:
            logger.error(f"An error occurred during Once Autocam automation: {e}")
            self.signals.finished.emit(self.group_dir, False)


class YouTubeAuthRunner(QRunnable):
    class Signals(QObject):
        finished = Signal(bool, str)

    def __init__(self, credentials_file: str, token_file: str):
        super().__init__()
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.signals = self.Signals()

    @Slot()
    def run(self):
        try:
            success, message = authenticate_youtube(
                self.credentials_file, self.token_file
            )
            self.signals.finished.emit(success, message)
        except Exception as e:
            logger.error(f"Error during YouTube authentication: {e}")
            self.signals.finished.emit(False, f"Authentication error: {str(e)}")


class TrayAgentLock:
    """OS-managed file lock to ensure only one tray agent runs at a time.

    Uses msvcrt.locking (Windows) / fcntl.flock (POSIX) on an open file
    handle. The handle is held for the lifetime of the process, so the
    OS releases the lock automatically on exit — even if the process is
    killed forcefully. This avoids the stale-lock-file problems where a
    killed tray leaves behind a lock file that the next tray can't
    delete because Windows hasn't fully closed the dead process's
    handle yet.
    """

    def __init__(self, lock_file_path: str):
        self.lock_file_path = Path(lock_file_path)
        self.lock_acquired = False
        self._fh = None  # Held for life of process; closed in release()

    def acquire(self, timeout_seconds: int = 30) -> bool:
        """Acquire the lock, waiting up to timeout_seconds."""
        import sys

        start_time = time.time()

        while time.time() - start_time < timeout_seconds:
            try:
                # Open (or create) the lock file and try to take an
                # OS-level exclusive lock. If another tray holds it,
                # msvcrt raises OSError; we retry until timeout.
                fh = open(self.lock_file_path, "a+")
                try:
                    if sys.platform == "win32":
                        import msvcrt

                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    fh.close()
                    logger.info("Tray agent already running, waiting...")
                    time.sleep(1)
                    continue

                # Got the lock — record PID for diagnostics and keep the
                # handle open so the OS keeps the lock.
                fh.seek(0)
                fh.truncate()
                fh.write(f"{os.getpid()}\n")
                fh.flush()
                self._fh = fh
                self.lock_acquired = True
                logger.info(f"Tray agent lock acquired: {self.lock_file_path}")
                return True
            except Exception as e:
                logger.error(f"Error acquiring lock: {e}")
                time.sleep(1)
                continue

        logger.error(
            f"Could not acquire tray agent lock after {timeout_seconds} seconds"
        )
        return False

    def release(self):
        """Release the lock."""
        if self.lock_acquired and self._fh is not None:
            try:
                self._fh.close()  # OS releases the lock
            except Exception as e:
                logger.warning(f"Error closing lock handle: {e}")
            # Best-effort unlink; harmless if it fails (next acquire
            # will just reuse the file)
            try:
                self.lock_file_path.unlink()
            except Exception:
                pass
            logger.info(f"Tray agent lock released: {self.lock_file_path}")
            self._fh = None
            self.lock_acquired = False


class SystemTrayIcon(QSystemTrayIcon):
    update_available = Signal(str)

    def __init__(self, config_path=None):
        super().__init__()
        # Ensure task types are registered for deserialization (safety net)
        register_all_tasks()
        self.version = get_version()
        self.full_version = get_full_version()

        # Load configuration
        if config_path:
            self.config_path = Path(config_path)
        else:
            self.config_path = get_shared_data_path() / "config.ini"

        self.config: Optional[Config] = None
        if self.config_path.exists():
            self.config = load_config(self.config_path)

        # Get GitHub repo from config for update checks
        self.github_repo = (
            self.config.app.github_repo if self.config else "mblakley/soccer-cam"
        )

        # Ensure storage path is configured in the config model
        # Use the typed StorageConfig from the Pydantic model
        if self.config and getattr(self.config.storage, "path", None) is None:
            self.config.storage.path = str(get_shared_data_path())

        self.init_ui()
        self.start_update_checker()

        self.threadpool = QThreadPool()
        logger.info(
            f"Using a thread pool with {self.threadpool.maxThreadCount()} threads."
        )

        # The tray's only processor responsibility is autocam_gui ball-tracking
        # (Once Sport's commercial GUI app needs Session 1+, which the service
        # can't provide). homegrown ball-tracking and the rest of the pipeline
        # (upload, video, ntfy, etc.) live in the service. When ball-tracking
        # finishes here, we set state.json -> ball_tracking_complete and the
        # service's StateAuditor picks it up to queue the YouTube upload.
        # See `~/.claude/plans/web-ui-consolidation.md` Phase 0a.
        self.ball_tracking_processor = None
        self.ball_tracking_discovery_processor = None

        if self.config and self.config.ball_tracking.enabled:
            if self.config.ball_tracking.provider == "autocam_gui":
                # Register provider implementations lazily so the import
                # chain (onnxruntime, cv2, CUDA DLLs) only runs when we
                # actually need ball-tracking.
                import video_grouper.ball_tracking.register_providers  # noqa: F401

                self.ball_tracking_processor = BallTrackingProcessor(
                    storage_path=self.config.storage.path,
                    config=self.config,
                    upload_processor=None,
                )

                from video_grouper.task_processors.ball_tracking_discovery_processor import (
                    BallTrackingDiscoveryProcessor,
                )

                self.ball_tracking_discovery_processor = BallTrackingDiscoveryProcessor(
                    storage_path=self.config.storage.path,
                    config=self.config,
                    ball_tracking_processor=self.ball_tracking_processor,
                    poll_interval=30,
                )
            else:
                logger.info(
                    "Tray: ball-tracking provider is %r; nothing to run here "
                    "(homegrown ball-tracking and other processors live in the "
                    "service). The tray will only run AutoCam-related work.",
                    self.config.ball_tracking.provider,
                )
        elif not self.config:
            logger.warning("No config loaded, skipping processor initialization")

    async def initialize(self):
        """Initialize the tray app asynchronously."""
        logger.info("Initializing SystemTrayIcon...")
        if self.ball_tracking_processor:
            await self.ball_tracking_processor.start()
        if self.ball_tracking_discovery_processor:
            await self.ball_tracking_discovery_processor.start()
        logger.info("SystemTrayIcon initialization complete")

    async def shutdown(self):
        """Shutdown the tray app asynchronously."""
        logger.info("Shutting down SystemTrayIcon...")
        if (
            hasattr(self, "ball_tracking_discovery_processor")
            and self.ball_tracking_discovery_processor
        ):
            await self.ball_tracking_discovery_processor.stop()
        if hasattr(self, "ball_tracking_processor") and self.ball_tracking_processor:
            await self.ball_tracking_processor.stop()
        logger.info("SystemTrayIcon shutdown complete")

    def init_ui(self):
        # Create tray icon - search multiple locations for PyInstaller compatibility
        icon_path = None
        candidates = []

        # 1. PyInstaller bundled data (via --add-data)
        if getattr(sys, "_MEIPASS", None):
            candidates.append(os.path.join(sys._MEIPASS, "video_grouper", "icon.ico"))

        # 2. Next to the executable (installed location)
        candidates.append(os.path.join(os.path.dirname(sys.executable), "icon.ico"))

        # 3. Relative to source file (development)
        script_dir = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(script_dir, "..", "icon.ico"))

        for candidate in candidates:
            if os.path.exists(candidate):
                icon_path = candidate
                break

        if icon_path:
            self.setIcon(QIcon(icon_path))
            logger.info(f"Loaded tray icon from: {icon_path}")
        else:
            logger.warning(f"Icon not found in any location: {candidates}")
            # Set a default system icon so the tray is at least visible
            self.setIcon(
                self.style().standardIcon(self.style().StandardPixmap.SP_ComputerIcon)
            )
        self.setToolTip(f"VideoGrouper v{self.full_version}")

        # Create context menu. Plan target: ≤ 4 items. The dashboard is
        # the entry point for everything else (config editor, setup
        # wizard, queue/camera/game state) — both nav links are on it.
        menu = QMenu()

        dashboard_action = QAction("Open Dashboard", self)
        dashboard_action.triggered.connect(self.open_dashboard)
        menu.addAction(dashboard_action)

        restart_action = QAction("Restart Service", self)
        restart_action.triggered.connect(self.restart_service)
        menu.addAction(restart_action)

        # Pause/resume recording. Only meaningful for cameras that
        # support it (Reolink); skip the menu item otherwise to keep
        # the menu under the 4-item budget.
        has_reolink_camera = bool(
            self.config and any(c.type == "reolink" for c in self.config.cameras)
        )
        if has_reolink_camera:
            self.recording_action = QAction("Pause Recording", self)
            self.recording_action.triggered.connect(self.toggle_recording)
            menu.addAction(self.recording_action)
            self._recording_enabled = True

        menu.addSeparator()

        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.exit_app)
        menu.addAction(exit_action)

        self.setContextMenu(menu)

        # Connect signals
        self.activated.connect(self.icon_activated)
        self.update_available.connect(self.show_update_notification)

    def start_update_checker(self):
        """Start the background update checker thread."""
        self.update_checker = UpdateChecker(
            self.version, self.github_repo, self.update_available
        )
        self.update_checker.start()

    def icon_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.open_dashboard()

    def _web_url(self, path: str = "/") -> str:
        """Build the local web app URL using configured port (default 8765)."""
        port = 8765
        if self.config is not None:
            port = getattr(self.config.ttt, "auth_server_port", 8765) or 8765
        return f"http://localhost:{port}{path}"

    def open_dashboard(self):
        """Open the dashboard / status page in the default browser."""
        try:
            webbrowser.open(self._web_url("/"))
        except Exception as e:
            logger.error(f"Could not launch browser: {e}", exc_info=True)
            self.showMessage(
                "Dashboard",
                f"Could not open browser: {e}",
                QSystemTrayIcon.MessageIcon.Critical,
            )

    def on_config_saved(self):
        """Hook for any post-save reloads. The web config editor saves
        directly so this is currently a no-op kept for legacy callers."""
        self.config = load_config(self.config_path)
        logger.info("Configuration saved.")

    def toggle_recording(self):
        """Toggle camera recording on/off via the Reolink API."""
        import asyncio
        from video_grouper.utils.config import load_config
        from video_grouper.cameras.reolink import ReolinkCamera

        try:
            config = load_config(self.config_path)
            cam_config = config.cameras[0] if config.cameras else None
            if not cam_config or cam_config.type != "reolink":
                self.showMessage("Recording", "No Reolink camera configured")
                return

            cam = ReolinkCamera(cam_config, config.storage.path)
            loop = asyncio.new_event_loop()

            if self._recording_enabled:
                # Pause: set TIMING to all zeros
                success = loop.run_until_complete(cam.stop_recording())
                if success:
                    self._recording_enabled = False
                    self.recording_action.setText("Resume Recording")
                    self.showMessage("Recording", "Recording paused")
                else:
                    self.showMessage("Recording", "Failed to pause recording")
            else:
                # Resume: set TIMING to all ones
                success = loop.run_until_complete(cam.start_recording())
                if success:
                    self._recording_enabled = True
                    self.recording_action.setText("Pause Recording")
                    self.showMessage("Recording", "Recording resumed")
                else:
                    self.showMessage("Recording", "Failed to resume recording")

            loop.close()
        except Exception as e:
            logger.error(f"Error toggling recording: {e}")
            self.showMessage(
                "Recording",
                f"Error: {str(e)}",
                QSystemTrayIcon.MessageIcon.Critical.value,
            )

    def restart_service(self):
        try:
            win32serviceutil.RestartService("VideoGrouperService")
            self.showMessage("Service", "Service restarted successfully")
        except Exception as e:
            logger.error(f"Error restarting service: {e}")
            self.showMessage(
                "Service",
                f"Failed to restart service: {str(e)}",
                QSystemTrayIcon.MessageIcon.Critical.value,
            )

    def show_update_notification(self, message):
        """Show update notification when available."""
        self.showMessage("Updates", message)

    def exit_app(self):
        """Exit the application."""
        if hasattr(self, "config_window") and self.config_window:
            self.config_window.close()

        # Schedule shutdown in the event loop
        asyncio.create_task(self.shutdown())
        QApplication.quit()


async def main():
    """Main entry point for the tray application."""
    # Config path: use CLI arg if provided, otherwise default
    if len(sys.argv) >= 2:
        config_path = Path(sys.argv[1])
    else:
        config_path = get_shared_data_path() / "config.ini"

    # Create lock file path in the same directory as the config
    config_dir = config_path.parent
    config_dir.mkdir(parents=True, exist_ok=True)
    lock_file_path = config_dir / "tray_agent.lock"

    # Create and acquire lock
    lock = TrayAgentLock(str(lock_file_path))
    if not lock.acquire():
        logger.error("Another tray agent is already running. Exiting.")
        sys.exit(1)

    # Register cleanup function
    atexit.register(lock.release)

    try:
        app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)

        # First-run detection: open the browser to the web wizard if needed.
        # The web wizard is hosted by the service (or Docker container) at
        # localhost:8765/setup. The tray no longer ships its own wizard.
        from video_grouper.utils.config import config_needs_onboarding

        if config_needs_onboarding(config_path):
            logger.info("Config needs onboarding; opening web wizard")
            try:
                webbrowser.open("http://localhost:8765/setup/welcome")
            except Exception as e:
                logger.error(f"Could not launch browser for setup: {e}")

        # Initialize the tray application
        tray_app = SystemTrayIcon(config_path)
        await tray_app.initialize()
        tray_app.show()

        # Keep the event loop running
        while True:
            await asyncio.sleep(0.1)
            app.processEvents()

    except Exception as e:
        logger.error(f"Error in tray application: {e}")
        sys.exit(1)
    finally:
        # Ensure lock is released
        lock.release()


if __name__ == "__main__":
    asyncio.run(main())
