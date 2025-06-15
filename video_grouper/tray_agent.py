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
        if self.config_path.exists():
            self.config.read(self.config_path)
        self.init_ui()
        
    def init_ui(self):
        self.setWindowTitle('VideoGrouper Configuration')
        layout = QVBoxLayout()
        
        # Create tab widget
        tabs = QTabWidget()
        
        # Camera Settings Tab
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
        
        # Storage Settings Tab
        storage_tab = QWidget()
        storage_layout = QFormLayout()
        
        self.storage_path = QLineEdit()
        browse_button = QPushButton('Browse...')
        browse_button.clicked.connect(self.browse_storage_path)
        
        storage_layout.addRow('Storage Path:', self.storage_path)
        storage_layout.addRow('', browse_button)
        
        # Load existing value
        if 'Storage' in self.config:
            self.storage_path.setText(self.config.get('Storage', 'base_path', fallback=''))
            
        storage_tab.setLayout(storage_layout)
        tabs.addTab(storage_tab, 'Storage Settings')
        
        # Match Info Tab
        match_tab = QWidget()
        match_layout = QVBoxLayout()
        
        self.match_info_status = QLabel('No pending match info')
        match_layout.addWidget(self.match_info_status)
        
        self.match_info_form = QFormLayout()
        self.start_time_offset = QLineEdit()
        self.my_team_name = QLineEdit()
        self.opponent_team_name = QLineEdit()
        self.location = QLineEdit()
        
        self.match_info_form.addRow('Start Time Offset (MM:SS):', self.start_time_offset)
        self.match_info_form.addRow('My Team Name:', self.my_team_name)
        self.match_info_form.addRow('Opponent Team Name:', self.opponent_team_name)
        self.match_info_form.addRow('Location:', self.location)
        
        match_layout.addLayout(self.match_info_form)
        
        self.save_match_info_button = QPushButton('Save Match Info')
        self.save_match_info_button.clicked.connect(self.save_match_info)
        match_layout.addWidget(self.save_match_info_button)
        
        match_tab.setLayout(match_layout)
        tabs.addTab(match_tab, 'Match Info')
        
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
        
        # Start timer to check for pending match info
        self.match_info_timer = QTimer()
        self.match_info_timer.timeout.connect(self.check_pending_match_info)
        self.match_info_timer.start(60000)  # Check every minute
        self.check_pending_match_info()  # Initial check
        
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

    def check_pending_match_info(self):
        """Check for directories that need match info input."""
        try:
            if 'Storage' not in self.config:
                return
                
            storage_path = self.config.get('Storage', 'base_path')
            if not os.path.exists(storage_path):
                return
                
            for directory in os.listdir(storage_path):
                full_path = os.path.join(storage_path, directory)
                if not os.path.isdir(full_path):
                    continue
                    
                status_file = os.path.join(full_path, "processing_status.txt")
                if not os.path.exists(status_file):
                    continue
                    
                with open(status_file, 'r') as f:
                    status = f.read().strip()
                    
                if status == "user_input":
                    match_info_path = os.path.join(full_path, "match_info.ini")
                    if not os.path.exists(match_info_path):
                        continue
                        
                    match_info = configparser.ConfigParser()
                    match_info.read(match_info_path)
                    
                    if not all_fields_filled(match_info["MATCH"]):
                        combined_file = os.path.join(full_path, "combined.mp4")
                        if os.path.exists(combined_file):
                            self.match_info_status.setText(f'Match info needed for {directory}\nVideo file: {combined_file}')
                            self.current_match_dir = full_path
                            self.match_info_form.setEnabled(True)
                            self.save_match_info_button.setEnabled(True)
                            return
                            
            self.match_info_status.setText('No pending match info')
            self.current_match_dir = None
            self.match_info_form.setEnabled(False)
            self.save_match_info_button.setEnabled(False)
            
        except Exception as e:
            logger.error(f"Error checking pending match info: {e}")
            
    def save_match_info(self):
        """Save match info to the current directory."""
        try:
            if not hasattr(self, 'current_match_dir') or not self.current_match_dir:
                return
                
            match_info = configparser.ConfigParser()
            match_info_path = os.path.join(self.current_match_dir, "match_info.ini")
            match_info.read(match_info_path)
            
            if "MATCH" not in match_info:
                match_info.add_section("MATCH")
                
            match_info["MATCH"]["start_time_offset"] = self.start_time_offset.text()
            match_info["MATCH"]["my_team_name"] = self.my_team_name.text()
            match_info["MATCH"]["opponent_team_name"] = self.opponent_team_name.text()
            match_info["MATCH"]["location"] = self.location.text()
            
            with open(match_info_path, 'w') as f:
                match_info.write(f)
                
            QMessageBox.information(self, 'Success', 'Match info saved successfully')
            self.check_pending_match_info()  # Refresh status
            
        except Exception as e:
            logger.error(f"Error saving match info: {e}")
            QMessageBox.critical(self, 'Error', f'Failed to save match info: {str(e)}')

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