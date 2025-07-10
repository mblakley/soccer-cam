import sys
import os
import logging
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
from .config_ui import ConfigWindow
from video_grouper.utils.paths import get_shared_data_path
from video_grouper.utils.config import load_config, Config
from video_grouper.task_processors import AutocamProcessor
from typing import Optional

# Configure logging
# log_dir = Path('C:/ProgramData/VideoGrouper')
# log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    # filename=log_dir / 'tray_agent.log',
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


class UpdateChecker(threading.Thread):
    def __init__(self, version, update_url, signal):
        super().__init__()
        self.version = version
        self.update_url = update_url
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
                    check_and_update(self.version, self.update_url)
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


class SystemTrayIcon(QSystemTrayIcon):
    update_available = Signal(str)

    def __init__(self):
        super().__init__()
        self.version = get_version()
        self.full_version = get_full_version()

        # Load configuration
        self.config_path = get_shared_data_path() / "config.ini"
        self.config: Optional[Config] = None
        if self.config_path.exists():
            self.config = load_config(self.config_path)

        # Get update URL from config
        self.update_url = (
            self.config.app.update_url
            if self.config
            else "https://updates.videogrouper.com"
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

        # Initialize AutocamProcessor
        self.autocam_processor = AutocamProcessor(
            storage_path=self.config.storage.path, config=self.config
        )

    async def initialize(self):
        """Initialize the tray app asynchronously."""
        logger.info("Initializing SystemTrayIcon...")
        await self.autocam_processor.start()
        logger.info("SystemTrayIcon initialization complete")

    async def shutdown(self):
        """Shutdown the tray app asynchronously."""
        logger.info("Shutting down SystemTrayIcon...")
        if hasattr(self, "autocam_processor") and self.autocam_processor:
            await self.autocam_processor.stop()
        logger.info("SystemTrayIcon shutdown complete")

    def init_ui(self):
        # Create tray icon
        script_dir = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(script_dir, "..", "icon.ico")
        self.setIcon(QIcon(icon_path))
        self.setToolTip(f"VideoGrouper v{self.full_version}")

        # Create context menu
        menu = QMenu()

        # Service control actions
        start_action = QAction("Start Service", self)
        start_action.triggered.connect(self.start_service)
        menu.addAction(start_action)

        stop_action = QAction("Stop Service", self)
        stop_action.triggered.connect(self.stop_service)
        menu.addAction(stop_action)

        restart_action = QAction("Restart Service", self)
        restart_action.triggered.connect(self.restart_service)
        menu.addAction(restart_action)

        menu.addSeparator()

        # Configuration action
        config_action = QAction("Configuration", self)
        config_action.triggered.connect(self.show_config)
        menu.addAction(config_action)

        # Update action
        self.update_action = QAction("Check for Updates", self)
        self.update_action.triggered.connect(self.check_updates)
        menu.addAction(self.update_action)

        menu.addSeparator()

        # Exit action
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
            self.version, self.update_url, self.update_available
        )
        self.update_checker.start()

    def icon_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_config()

    def show_config(self):
        if not hasattr(self, "config_window") or self.config_window is None:
            self.config_window = ConfigWindow()
            self.config_window.config_saved.connect(self.on_config_saved)

        self.config_window.show()
        self.config_window.raise_()
        self.config_window.activateWindow()
        self.refresh_autocam_queue_ui()

    def refresh_autocam_queue_ui(self):
        if hasattr(self, "config_window") and self.config_window:
            self.config_window.refresh_autocam_queue_tab()

    def on_config_saved(self):
        self.config = load_config(self.config_path)
        logger.info("Configuration saved.")

    def start_service(self):
        try:
            win32serviceutil.StartService("VideoGrouperService")
            self.showMessage("Service", "Service started successfully")
        except Exception as e:
            logger.error(f"Error starting service: {e}")
            self.showMessage(
                "Service",
                f"Failed to start service: {str(e)}",
                QSystemTrayIcon.MessageIcon.Critical.value,
            )

    def stop_service(self):
        try:
            win32serviceutil.StopService("VideoGrouperService")
            self.showMessage("Service", "Service stopped successfully")
        except Exception as e:
            logger.error(f"Error stopping service: {e}")
            self.showMessage(
                "Service",
                f"Failed to stop service: {str(e)}",
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

    async def check_updates(self):
        """Manually check for updates."""
        try:
            has_update = await check_and_update(self.version, self.update_url)
            if has_update:
                self.showMessage(
                    "Updates",
                    "Update installed successfully. Please restart the application.",
                )
            else:
                self.showMessage("Updates", "No updates available.")
        except Exception as e:
            logger.error(f"Error checking for updates: {e}")
            self.showMessage(
                "Updates",
                f"Error checking for updates: {str(e)}",
                QSystemTrayIcon.MessageIcon.Critical,
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
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    tray = SystemTrayIcon()
    await tray.initialize()
    tray.show()

    # Keep the event loop running
    while True:
        await asyncio.sleep(0.1)
        app.processEvents()


if __name__ == "__main__":
    asyncio.run(main())
