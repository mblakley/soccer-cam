import asyncio
import atexit
import os
import sys
import threading
import time

# NOTE: ``register_providers`` is imported lazily inside the autocam_gui
# branch of __init__. Importing it eagerly here would pull in the
# homegrown ONNX stack (cv2 + onnxruntime + CUDA DLLs), which the tray
# never needs — it only runs autocam_gui ball-tracking. Doing it lazy
# keeps the tray bootable on machines without GPU drivers.
import webbrowser
from pathlib import Path

import win32serviceutil
from PyQt6.QtCore import (
    QObject,
    QRunnable,
    QThreadPool,
)
from PyQt6.QtCore import (
    pyqtSignal as Signal,
)
from PyQt6.QtCore import (
    pyqtSlot as Slot,
)
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from video_grouper.task_processors import BallTrackingProcessor
from video_grouper.task_processors.register_tasks import register_tray_tasks
from video_grouper.tray.autocam_automation import run_autocam_on_file
from video_grouper.utils.config import Config, load_config
from video_grouper.utils.logger import get_logger, setup_logging
from video_grouper.utils.paths import get_shared_data_path
from video_grouper.utils.youtube_upload import authenticate_youtube
from video_grouper.version import get_full_version, get_version


def _bootstrap_log_dir() -> Path:
    """Per-user-writable log dir for tray BOOTSTRAP logging only.

    The first ~30 log lines (before ``load_config`` returns the user's
    storage path) need somewhere writable; this function returns a
    user-owned path that always exists. After config loads,
    :class:`SystemTrayIcon` calls :func:`setup_logging_from_config`
    which moves the file handler to
    ``<storage>/logs/video_grouper_tray.log`` so the tray and service
    co-locate their logs at a path the dashboard can find without
    per-user-profile guesswork.
    """
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "VideoGrouper" / "logs"
    # Non-Windows fallback (tray is Windows-only in practice, but keep
    # the path logic working for dev runs on macOS/Linux).
    return Path.home() / ".videogrouper" / "logs"


setup_logging(
    level="DEBUG", log_dir=_bootstrap_log_dir(), app_name="video_grouper_tray"
)
logger = get_logger(__name__)


class UpdateStatusPoller(threading.Thread):
    """Polls the service's ``GET /api/update/status`` endpoint and
    bridges state changes onto the tray's Qt signals.

    Replaces the legacy ``UpdateChecker`` which drove the buggy
    in-tray check + exe-swap install path. The actual GitHub poll
    and (in Phase 2) the installer spawn both live in the service
    now; the tray is just the UI surface that surfaces what the
    service has staged. See
    ``~/.claude/plans/investigate-the-auto-upgrade-process-jiggly-gem.md``.

    Two transitions matter:

    - ``pending_version`` appears (or changes value): emit
      ``update_pending`` so the tray surfaces a notification. The
      service will install automatically when ``auto_update=true``;
      otherwise the user clicks Install Update.
    - ``pending_version`` clears (install completed): emit
      ``update_cleared`` so the tray can hide the menu item / clear
      the persistent banner.
    """

    POLL_INTERVAL_SECONDS = 60
    REQUEST_TIMEOUT_SECONDS = 10

    def __init__(
        self,
        status_url: str,
        on_pending,
        on_cleared,
    ):
        super().__init__()
        self.status_url = status_url
        self.on_pending = on_pending
        self.on_cleared = on_cleared
        self.daemon = True
        self._stop = threading.Event()
        self._last_pending: str | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        # httpx is heavier than we need for a single localhost poll;
        # stdlib urllib is fine and keeps the tray's import budget
        # lean (every dep pulled in here ships in _internal/).
        import json
        import urllib.error
        import urllib.request

        while not self._stop.is_set():
            try:
                with urllib.request.urlopen(
                    self.status_url, timeout=self.REQUEST_TIMEOUT_SECONDS
                ) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                self._handle_status(body)
            except urllib.error.URLError as exc:
                # Service may be restarting; quiet logging at debug
                logger.debug("update status poll failed: %s", exc)
            except Exception as exc:
                logger.warning("update status poll unexpected error: %s", exc)
            self._stop.wait(self.POLL_INTERVAL_SECONDS)

    def _handle_status(self, body: dict) -> None:
        pending = body.get("pending_version")
        if pending and pending != self._last_pending:
            self._last_pending = pending
            try:
                self.on_pending(pending, bool(body.get("auto_update", True)))
            except Exception as exc:
                logger.warning("on_pending callback raised: %s", exc)
        elif not pending and self._last_pending:
            self._last_pending = None
            try:
                self.on_cleared()
            except Exception as exc:
                logger.warning("on_cleared callback raised: %s", exc)


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
    # Two parameter signal: (version, auto_update_enabled). Qt strict
    # typing means we pass the auto_update flag through rather than
    # re-reading config from the slot (the slot runs on the GUI
    # thread; config reads do disk I/O).
    update_pending_signal = Signal(str, bool)
    update_cleared_signal = Signal()

    def __init__(self, config_path=None):
        super().__init__()
        # Ensure task types are registered for deserialization (safety net)
        register_tray_tasks()
        self.version = get_version()
        self.full_version = get_full_version()

        # Load configuration
        if config_path:
            self.config_path = Path(config_path)
        else:
            self.config_path = get_shared_data_path() / "config.ini"

        self.config: Config | None = None
        if self.config_path.exists():
            self.config = load_config(self.config_path)
            # Switch logging from the bootstrap LOCALAPPDATA path to
            # the configured storage path (<storage>/logs/...) so the
            # tray's log lands alongside the service's, where the
            # dashboard expects it. We call ``setup_logging`` directly
            # (not ``setup_logging_from_config``) so we can pin
            # ``app_name="video_grouper_tray"`` — otherwise we'd
            # collide with the service's open handle on
            # ``video_grouper.log`` and Windows would fail us with
            # PermissionError. ``setup_logging`` removes existing
            # handlers FIRST, so on failure we re-establish the
            # bootstrap path so the tray never silently goes log-less.
            storage_path = self.config and getattr(self.config.storage, "path", None)
            if storage_path:
                try:
                    setup_logging(
                        level="DEBUG",
                        log_dir=Path(storage_path) / "logs",
                        app_name="video_grouper_tray",
                    )
                    logger.info(
                        "Tray logging re-routed to %s",
                        Path(storage_path) / "logs" / "video_grouper_tray.log",
                    )
                except (PermissionError, OSError) as exc:
                    setup_logging(
                        level="DEBUG",
                        log_dir=_bootstrap_log_dir(),
                        app_name="video_grouper_tray",
                    )
                    logger.warning(
                        "Could not switch tray log to storage path "
                        "(%s); falling back to %s",
                        exc,
                        _bootstrap_log_dir(),
                    )

        # No more in-tray GitHub poll -- the service owns that loop.
        # See ``UpdateStatusPoller`` below for the new client surface.

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

        # Install Update -- hidden by default. Surfaced when the
        # service reports a pending_version AND auto_update=false.
        # In auto_update=true mode the service installs on its own
        # once the pipeline quiesces; the tray shows a notification
        # but no clickable menu item.
        self.install_update_action = QAction("Install Update", self)
        self.install_update_action.triggered.connect(self.apply_pending_update)
        self.install_update_action.setVisible(False)
        menu.addAction(self.install_update_action)

        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.exit_app)
        menu.addAction(exit_action)

        self.setContextMenu(menu)

        # Connect signals
        self.activated.connect(self.icon_activated)
        self.update_pending_signal.connect(self.on_update_pending)
        self.update_cleared_signal.connect(self.on_update_cleared)

    def start_update_checker(self):
        """Start the background poller that mirrors the service's
        update state into the tray UI."""
        self.update_poller = UpdateStatusPoller(
            status_url=self._web_url("/api/update/status"),
            on_pending=lambda v, auto: self.update_pending_signal.emit(v, auto),
            on_cleared=lambda: self.update_cleared_signal.emit(),
        )
        self.update_poller.start()

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

        from video_grouper.cameras.reolink import ReolinkCamera
        from video_grouper.utils.config import load_config

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

    def on_update_pending(self, version: str, auto_update: bool) -> None:
        """Surface a notification when the service stages a new version.

        Two flavours per the plan's Chrome-style UX:

        - ``auto_update=true``: brief informational toast. The service
          will install on its own once the pipeline goes idle. No menu
          item to click.
        - ``auto_update=false``: persistent notification + reveal the
          ``Install Update`` menu item so the user can drive the apply.
        """
        if auto_update:
            self.showMessage(
                "VideoGrouper",
                f"Update v{version} ready. Will install automatically when idle.",
                QSystemTrayIcon.MessageIcon.Information,
            )
            self.install_update_action.setVisible(False)
        else:
            self.showMessage(
                "VideoGrouper",
                f"Update v{version} ready -- choose Install Update from the tray menu.",
                QSystemTrayIcon.MessageIcon.Information,
            )
            self.install_update_action.setText(f"Install Update v{version}")
            self.install_update_action.setVisible(True)

    def on_update_cleared(self) -> None:
        """Hide the Install Update menu item once the service confirms
        the staged update is no longer pending (installed or aborted)."""
        self.install_update_action.setVisible(False)
        self.install_update_action.setText("Install Update")

    def apply_pending_update(self) -> None:
        """POST /api/update/apply when the user clicks Install Update.

        Phase 1 boundary: the service returns 503 with a "Phase 2"
        explanation. We surface that string verbatim so the user
        understands why nothing happens; once Phase 2 lands the same
        click drives the real installer spawn with no client-side
        change needed.
        """
        import json
        import urllib.error
        import urllib.request

        url = self._web_url("/api/update/apply")
        try:
            req = urllib.request.Request(url, method="POST", data=b"")
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            self.showMessage(
                "Update",
                f"Update scheduled: {body.get('status', '?')}",
                QSystemTrayIcon.MessageIcon.Information,
            )
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8"))
                reason = body.get("reason") or body.get("status") or str(exc)
            except Exception:
                reason = str(exc)
            self.showMessage(
                "Update",
                reason,
                QSystemTrayIcon.MessageIcon.Warning,
            )
        except Exception as exc:
            logger.error("Could not POST /api/update/apply: %s", exc, exc_info=True)
            self.showMessage(
                "Update",
                f"Could not request install: {exc}",
                QSystemTrayIcon.MessageIcon.Critical,
            )

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

        # Initialize the tray application. SystemTrayIcon's __init__
        # re-routes the log file from %LOCALAPPDATA%\VideoGrouper\logs
        # (the bootstrap path) to <storage>/logs/video_grouper_tray.log
        # once load_config has returned the user's configured storage
        # path, so the dashboard can find the tray log at a stable
        # well-known location without needing per-user-session
        # discovery.
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
