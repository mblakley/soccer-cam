import sys
import os
import json
import configparser
import logging
import asyncio
import threading
from pathlib import Path
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QIcon
import win32serviceutil
import win32service
from video_grouper.update.update_manager import check_and_update
from video_grouper.version import get_version, get_full_version
from .config_ui import ConfigWindow
from video_grouper.paths import get_shared_data_path

# Configure logging
log_dir = Path('C:/ProgramData/VideoGrouper')
log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=log_dir / 'tray_agent.log',
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

class SystemTrayIcon(QSystemTrayIcon):
    update_available = pyqtSignal(str)
    
    def __init__(self):
        super().__init__()
        self.version = get_version()
        self.full_version = get_full_version()
        
        # Load configuration
        self.config_path = get_shared_data_path() / 'config.ini'
        self.config = configparser.ConfigParser()
        if self.config_path.exists():
            self.config.read(self.config_path)
            
        # Get update URL from config
        self.update_url = self.config.get('Updates', 'update_url', fallback='https://updates.videogrouper.com')
        
        self.init_ui()
        self.start_update_checker()
        
    def init_ui(self):
        # Create tray icon
        script_dir = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(script_dir, '..', 'icon.ico')
        self.setIcon(QIcon(icon_path))
        self.setToolTip(f'VideoGrouper v{self.full_version}')
        
        # Create context menu
        menu = QMenu()
        
        # Service control actions
        start_action = menu.addAction('Start Service')
        start_action.triggered.connect(self.start_service)
        
        stop_action = menu.addAction('Stop Service')
        stop_action.triggered.connect(self.stop_service)
        
        restart_action = menu.addAction('Restart Service')
        restart_action.triggered.connect(self.restart_service)
        
        menu.addSeparator()
        
        # Configuration action
        config_action = menu.addAction('Configuration')
        config_action.triggered.connect(self.show_config)
        
        # Update action
        self.update_action = menu.addAction('Check for Updates')
        self.update_action.triggered.connect(self.check_updates)
        
        menu.addSeparator()
        
        # Exit action
        exit_action = menu.addAction('Exit')
        exit_action.triggered.connect(self.exit_app)
        
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
        self.config_window = ConfigWindow()
        self.config_window.show()
        
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
        QApplication.quit()

def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    
    tray = SystemTrayIcon()
    tray.show()
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main() 