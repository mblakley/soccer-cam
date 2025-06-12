import sys
import os
import json
import configparser
import logging
import asyncio
import threading
from pathlib import Path
from PyQt6.QtWidgets import (QApplication, QSystemTrayIcon, QMenu, QWidget,
                            QVBoxLayout, QTabWidget, QLabel, QLineEdit,
                            QPushButton, QFormLayout, QFileDialog, QMessageBox)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon
import win32serviceutil
import win32service
from video_grouper.update_manager import check_and_update
from video_grouper.version import get_version, get_full_version

# Configure logging
logging.basicConfig(
    filename='C:\\ProgramData\\VideoGrouper\\tray_agent.log',
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class ConfigWindow(QWidget):
    def __init__(self, config_path):
        super().__init__()
        self.config_path = config_path
        self.config = configparser.ConfigParser()
        if os.path.exists(config_path):
            self.config.read(config_path)
        self.init_ui()
        
    def init_ui(self):
        self.setWindowTitle('VideoGrouper Configuration')
        self.setGeometry(100, 100, 600, 400)
        
        layout = QVBoxLayout()
        
        # Create tab widget
        tabs = QTabWidget()
        
        # Camera Configuration Tab
        camera_tab = QWidget()
        camera_layout = QFormLayout()
        
        self.ip_address = QLineEdit()
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        
        camera_layout.addRow('IP Address:', self.ip_address)
        camera_layout.addRow('Username:', self.username)
        camera_layout.addRow('Password:', self.password)
        
        # Load existing values
        if 'Camera' in self.config:
            self.ip_address.setText(self.config.get('Camera', 'ip_address', fallback=''))
            self.username.setText(self.config.get('Camera', 'username', fallback=''))
            self.password.setText(self.config.get('Camera', 'password', fallback=''))
            
        camera_tab.setLayout(camera_layout)
        tabs.addTab(camera_tab, 'Camera Settings')
        
        # Storage Configuration Tab
        storage_tab = QWidget()
        storage_layout = QFormLayout()
        
        self.storage_path = QLineEdit()
        browse_button = QPushButton('Browse...')
        browse_button.clicked.connect(self.browse_storage_path)
        
        path_layout = QHBoxLayout()
        path_layout.addWidget(self.storage_path)
        path_layout.addWidget(browse_button)
        
        storage_layout.addRow('Storage Base Path:', path_layout)
        
        # Load existing value
        if 'Storage' in self.config:
            self.storage_path.setText(self.config.get('Storage', 'base_path', fallback=''))
            
        storage_tab.setLayout(storage_layout)
        tabs.addTab(storage_tab, 'Storage Settings')
        
        # Queue Status Tab
        queue_tab = QWidget()
        queue_layout = QVBoxLayout()
        
        self.queue_status = QLabel('No files in queue')
        queue_layout.addWidget(self.queue_status)
        
        queue_tab.setLayout(queue_layout)
        tabs.addTab(queue_tab, 'Queue Status')
        
        layout.addWidget(tabs)
        
        # Save button
        save_button = QPushButton('Save Configuration')
        save_button.clicked.connect(self.save_config)
        layout.addWidget(save_button)
        
        self.setLayout(layout)
        
    def browse_storage_path(self):
        path = QFileDialog.getExistingDirectory(self, 'Select Storage Directory')
        if path:
            self.storage_path.setText(path)
            
    def save_config(self):
        try:
            # Ensure sections exist
            if 'Camera' not in self.config:
                self.config.add_section('Camera')
            if 'Storage' not in self.config:
                self.config.add_section('Storage')
                
            # Save values
            self.config['Camera']['ip_address'] = self.ip_address.text()
            self.config['Camera']['username'] = self.username.text()
            self.config['Camera']['password'] = self.password.text()
            self.config['Storage']['base_path'] = self.storage_path.text()
            
            # Create config directory if it doesn't exist
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            
            # Save to file
            with open(self.config_path, 'w') as f:
                self.config.write(f)
                
            QMessageBox.information(self, 'Success', 'Configuration saved successfully')
            
        except Exception as e:
            logger.error(f"Error saving configuration: {e}")
            QMessageBox.critical(self, 'Error', f'Failed to save configuration: {str(e)}')
            
    def update_queue_status(self, status):
        self.queue_status.setText(status)

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
        self.config_path = Path('C:\\ProgramData\\VideoGrouper\\config.ini')
        self.config = configparser.ConfigParser()
        if self.config_path.exists():
            self.config.read(self.config_path)
            
        # Get update URL from config
        self.update_url = self.config.get('Updates', 'update_url', fallback='https://updates.videogrouper.com')
        
        self.init_ui()
        self.start_update_checker()
        
    def init_ui(self):
        # Create tray icon
        self.setIcon(QIcon('icon.ico'))
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
        self.config_window = ConfigWindow(self.config_path)
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