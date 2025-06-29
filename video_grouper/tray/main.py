import sys
import os
import json
import configparser
import logging
import asyncio
import threading
from pathlib import Path
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QMessageBox
from PyQt6.QtCore import (QRunnable, QThreadPool, QTimer, QObject,
                          pyqtSignal as Signal, pyqtSlot as Slot)
from PyQt6.QtGui import QIcon, QAction
import win32serviceutil
from video_grouper.tray.autocam_automation import run_autocam_on_file
from video_grouper.update.update_manager import check_and_update
from video_grouper.version import get_version, get_full_version
from video_grouper.utils.youtube_upload import authenticate_youtube
from .config_ui import ConfigWindow
from video_grouper.utils.paths import get_shared_data_path
from video_grouper.utils.config import load_config, Config
from typing import Optional

# Configure logging
# log_dir = Path('C:/ProgramData/VideoGrouper')
# log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    # filename=log_dir / 'tray_agent.log',
    stream=sys.stdout,
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
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
    def __init__(self, input_path: str, output_path: str, group_dir: Path):
        super().__init__()
        self.input_path = input_path
        self.output_path = output_path
        self.group_dir = group_dir
        self.signals = RunnerSignals()

    @Slot()
    def run(self):
        try:
            # Assuming run_autocam_on_file returns True on success, False on failure
            success = run_autocam_on_file(self.input_path, self.output_path)
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
            success, message = authenticate_youtube(self.credentials_file, self.token_file)
            self.signals.finished.emit(success, message)
        except Exception as e:
            logger.error(f"Error during YouTube authentication: {e}")
            self.signals.finished.emit(False, f"Authentication error: {str(e)}")

def get_autocam_input_output_paths(group_dir: Path):
    for root, _, files in os.walk(group_dir):
        for file in files:
            if file.endswith("-raw.mp4"):
                input_path = Path(root) / file
                output_path = input_path.with_name(input_path.name.replace("-raw.mp4", ".mp4"))
                return str(input_path), str(output_path)
    raise FileNotFoundError(f"No '-raw.mp4' file found in {group_dir}")

class SystemTrayIcon(QSystemTrayIcon):
    update_available = Signal(str)
    
    def __init__(self):
        super().__init__()
        self.version = get_version()
        self.full_version = get_full_version()
        
        # Load configuration
        self.config_path = get_shared_data_path() / 'config.ini'
        self.config: Optional[Config] = None
        if self.config_path.exists():
            self.config = load_config(self.config_path)
            
        # Get update URL from config
        self.update_url = self.config.app.update_url if self.config else 'https://updates.videogrouper.com'
        
        # Ensure essential paths are configured
        if not self.config.has_section('paths'):
            self.config.add_section('paths')
        if not self.config.has_option('paths', 'shared_data_path'):
            self.config.set('paths', 'shared_data_path', str(get_shared_data_path()))

        self._is_first_check = True
        self.init_ui()
        self.start_update_checker()
        
        self.threadpool = QThreadPool()
        logger.info(f"Using a thread pool with {self.threadpool.maxThreadCount()} threads.")

        self._autocam_queue_timer = QTimer()
        self._autocam_queue_processing = False
        self._autocam_queue_timer.timeout.connect(self._check_autocam_queue)
        self._autocam_queue_timer.start(10000)
        self._check_autocam_queue()
        
    def init_ui(self):
        # Create tray icon
        script_dir = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(script_dir, '..', 'icon.ico')
        self.setIcon(QIcon(icon_path))
        self.setToolTip(f'VideoGrouper v{self.full_version}')
        
        # Create context menu
        menu = QMenu()
        
        # Service control actions
        start_action = QAction('Start Service', self)
        start_action.triggered.connect(self.start_service)
        menu.addAction(start_action)
        
        stop_action = QAction('Stop Service', self)
        stop_action.triggered.connect(self.stop_service)
        menu.addAction(stop_action)
        
        restart_action = QAction('Restart Service', self)
        restart_action.triggered.connect(self.restart_service)
        menu.addAction(restart_action)
        
        menu.addSeparator()
        
        # Configuration action
        config_action = QAction('Configuration', self)
        config_action.triggered.connect(self.show_config)
        menu.addAction(config_action)
        
        # Update action
        self.update_action = QAction('Check for Updates', self)
        self.update_action.triggered.connect(self.check_updates)
        menu.addAction(self.update_action)
        
        menu.addSeparator()
        
        # Exit action
        exit_action = QAction('Exit', self)
        exit_action.triggered.connect(self.exit_app)
        menu.addAction(exit_action)
        
        self.setContextMenu(menu)
        
        # Connect signals
        self.activated.connect(self.icon_activated)
        self.update_available.connect(self.show_update_notification)
        
    def start_update_checker(self):
        """Start the background update checker thread."""
        self.update_checker = UpdateChecker(self.version, self.update_url, self.update_available)
        self.update_checker.start()
        
    def icon_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_config()
            
    def show_config(self):
        if not hasattr(self, 'config_window') or self.config_window is None:
            self.config_window = ConfigWindow()
            self.config_window.config_saved.connect(self.on_config_saved)
        
        self.config_window.show()
        self.config_window.raise_()
        self.config_window.activateWindow()
        self.refresh_autocam_queue_ui()
    
    def refresh_autocam_queue_ui(self):
        if hasattr(self, 'config_window') and self.config_window:
            self.config_window.refresh_autocam_queue_tab()

    def on_config_saved(self):
        self.config = load_config(self.config_path)
        logger.info("Configuration saved.")

    def start_service(self):
        try:
            win32serviceutil.StartService('VideoGrouperService')
            self.showMessage('Service', 'Service started successfully')
        except Exception as e:
            logger.error(f"Error starting service: {e}")
            self.showMessage('Service', f'Failed to start service: {str(e)}', QSystemTrayIcon.MessageIcon.Critical)
            
    def stop_service(self):
        try:
            win32serviceutil.StopService('VideoGrouperService')
            self.showMessage('Service', 'Service stopped successfully')
        except Exception as e:
            logger.error(f"Error stopping service: {e}")
            self.showMessage('Service', f'Failed to stop service: {str(e)}', QSystemTrayIcon.MessageIcon.Critical)
            
    def restart_service(self):
        try:
            win32serviceutil.RestartService('VideoGrouperService')
            self.showMessage('Service', 'Service restarted successfully')
        except Exception as e:
            logger.error(f"Error restarting service: {e}")
            self.showMessage('Service', f'Failed to restart service: {str(e)}', QSystemTrayIcon.MessageIcon.Critical)
            
    async def check_updates(self):
        """Manually check for updates."""
        try:
            has_update = await check_and_update(self.version, self.update_url)
            if has_update:
                self.showMessage('Updates', 'Update installed successfully. Please restart the application.')
            else:
                self.showMessage('Updates', 'No updates available.')
        except Exception as e:
            logger.error(f"Error checking for updates: {e}")
            self.showMessage('Updates', f'Error checking for updates: {str(e)}', QSystemTrayIcon.MessageIcon.Critical)
            
    def show_update_notification(self, message):
        """Show update notification when available."""
        self.showMessage('Updates', message)
        
    def exit_app(self):
        if hasattr(self, 'config_window') and self.config_window:
            self.config_window.close()
        QApplication.quit()

    def _check_autocam_queue(self):
        logger.info("Checking Autocam queue...")
        if self._autocam_queue_processing:
            return

        groups_dir = Path(self.config.paths.shared_data_path)
        logger.info(f"Scanning for groups in '{groups_dir}'")
        autocam_queue_path = groups_dir / "autocam_queue_state.json"

        queue = []
        if autocam_queue_path.exists():
            try:
                with open(autocam_queue_path, "r") as f:
                    queue = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                logger.warning("Could not read autocam queue file. Starting fresh.")
                queue = []

        queue_changed = False

        # On first run, reset 'processing' items to 'queued'
        if self._is_first_check:
            for item in queue:
                if item['status'] == 'processing':
                    item['status'] = 'queued'
                    logger.warning(f"Found job in 'processing' state from previous run. Re-queueing group '{item['group_name']}'.")
                    queue_changed = True
            self._is_first_check = False

        existing_group_names = {item['group_name'] for item in queue}

        for group_dir in groups_dir.iterdir():
            if group_dir.is_dir() and group_dir.name not in existing_group_names:
                state_file = group_dir / "state.json"
                if state_file.exists():
                    try:
                        with open(state_file, "r") as f:
                            state_data = json.load(f)
                        status = state_data.get("status")
                        if status == "trimmed":
                            group_name = group_dir.name
                            queue.append({"group_name": group_name, "status": "queued"})
                            logger.info(f"Found new trimmed group '{group_name}'. Adding to Autocam queue.")
                            queue_changed = True
                    except (json.JSONDecodeError, IOError) as e:
                        logger.error(f"Error processing state.json for group {group_dir.name}: {e}")

        for item in queue:
            if item['status'] == 'autocam_failed':
                item['status'] = 'queued'
                logger.info(f"Re-queueing failed Autocam job for group '{item['group_name']}'.")
                queue_changed = True

        if queue_changed:
            with open(autocam_queue_path, "w") as f:
                json.dump(queue, f, indent=4)
            self.refresh_autocam_queue_ui()

        if any(item['status'] == 'queued' for item in queue):
            self._autocam_queue_processing = True
            logger.info("Found items in autocam queue. Starting runner.")
            self._run_autocam_from_queue()
        
        logger.info("Autocam queue check finished.")

    def _run_autocam_from_queue(self):
        groups_dir = Path(self.config.paths.shared_data_path)
        autocam_queue_path = groups_dir / "autocam_queue_state.json"

        if not autocam_queue_path.exists():
            self._autocam_queue_processing = False
            return

        try:
            with open(autocam_queue_path, "r") as f:
                queue = json.load(f)
        except json.JSONDecodeError:
            logger.error("Could not decode autocam queue. Resetting.")
            os.remove(autocam_queue_path)
            self._autocam_queue_processing = False
            return

        item_to_process = None
        for item in queue:
            if item['status'] == 'queued':
                item_to_process = item
                break

        if not item_to_process:
            logger.info("No queued items found in autocam queue.")
            self._autocam_queue_processing = False
            return

        group_name = item_to_process['group_name']
        group_dir = groups_dir / group_name

        try:
            input_path, output_path = get_autocam_input_output_paths(group_dir)
        except (FileNotFoundError, ValueError) as e:
            logger.error(f"Could not find video file for group {group_name}: {e}")
            item_to_process['status'] = 'autocam_failed'
            with open(autocam_queue_path, "w") as f:
                json.dump(queue, f, indent=4)
            self._autocam_queue_processing = False
            QTimer.singleShot(0, self._check_autocam_queue)
            return

        item_to_process['status'] = 'processing'
        with open(autocam_queue_path, "w") as f:
            json.dump(queue, f, indent=4)
        
        self.refresh_autocam_queue_ui()

        logger.info(f"Starting Autocam process for group {group_name}...")
        self._autocam_queue_timer.stop()
        logger.info("Autocam queue timer stopped while processing.")
        runner = AutocamRunner(input_path, output_path, group_dir)
        runner.signals.finished.connect(self.on_autocam_runner_finished)
        self.threadpool.start(runner)

    def on_autocam_runner_finished(self, group_dir, success):
        group_name = group_dir.name

        groups_dir = Path(self.config.paths.shared_data_path)
        autocam_queue_path = groups_dir / "autocam_queue_state.json"

        with open(autocam_queue_path, "r") as f:
            queue = json.load(f)

        item_found = False
        if success:
            # On success, remove the item from the queue
            original_length = len(queue)
            queue = [item for item in queue if item['group_name'] != group_name]
            if len(queue) < original_length:
                item_found = True
                logger.info(f"Successfully processed and removed group '{group_name}' from Autocam queue.")
        else:
            # On failure, mark as failed to be retried later
            for item in queue:
                if item['group_name'] == group_name:
                    item['status'] = 'autocam_failed'
                    item_found = True
                    break

        if not item_found:
            logger.error(f"Finished job for group {group_name}, but it was not found in the queue.")
            self._autocam_queue_processing = False
            return

        with open(autocam_queue_path, "w") as f:
            json.dump(queue, f, indent=4)

        # Update the group's main state file on success
        if success:
            logger.info(f"Updating group '{group_name}' status to autocam_complete.")
            state_file = group_dir / "state.json"
            if state_file.exists():
                try:
                    with open(state_file, "r") as f:
                        state_data = json.load(f)
                    state_data['status'] = 'autocam_complete'
                    with open(state_file, "w") as f:
                        json.dump(state_data, f, indent=4)
                    
                    # Check if YouTube uploads are enabled
                    if self.config.youtube.enabled if self.config else False:
                        # Add to YouTube upload queue
                        logger.info(f"YouTube uploads are enabled. Adding group '{group_name}' to YouTube upload queue.")
                        ffmpeg_queue_path = groups_dir / "ffmpeg_queue_state.json"
                        
                        youtube_task = {
                            "task_type": "youtube_upload",
                            "item_path": str(group_dir)
                        }
                        
                        try:
                            # Load existing queue
                            queue = []
                            if ffmpeg_queue_path.exists():
                                with open(ffmpeg_queue_path, "r") as f:
                                    queue = json.load(f)
                            
                            # Check if this task is already in the queue
                            if not any(task.get("task_type") == "youtube_upload" and 
                                      task.get("item_path") == str(group_dir) for task in queue):
                                queue.append(youtube_task)
                                with open(ffmpeg_queue_path, "w") as f:
                                    json.dump(queue, f, indent=4)
                                logger.info(f"Added group '{group_name}' to YouTube upload queue.")
                        except Exception as e:
                            logger.error(f"Error adding group '{group_name}' to YouTube upload queue: {e}")
                    else:
                        logger.info(f"YouTube uploads are not enabled. Skipping upload for group '{group_name}'.")
                except (json.JSONDecodeError, IOError) as e:
                    logger.error(f"Could not update status to autocam_complete for group {group_name}: {e}")
            else:
                logger.warning(f"state.json not found for group {group_name} on successful completion.")
        else:
            logger.error(f"AUTOCAM_RUNNER: Failed to process video for group {group_name}.")

        self.refresh_autocam_queue_ui()

        self._autocam_queue_processing = False
        self._autocam_queue_timer.start()
        logger.info("Autocam queue timer restarted.")

def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    
    tray = SystemTrayIcon()
    tray.show()
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main() 