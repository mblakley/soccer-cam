import sys
import os
import configparser
import logging
import json
import asyncio
import pytz
from pathlib import Path
from datetime import datetime
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QTabWidget, QLabel, 
                             QLineEdit, QPushButton, QFormLayout, QFileDialog, QMessageBox, QCheckBox, QListWidget, QListWidgetItem, QGroupBox, QComboBox)
from PyQt6.QtCore import QTimer, QSize
from video_grouper.locking import FileLock
from video_grouper.paths import get_shared_data_path
from video_grouper.directory_state import DirectoryState
from video_grouper.time_utils import get_all_timezones, convert_utc_to_local
from .queue_item_widget import QueueItemWidget
from .match_info_item_widget import MatchInfoItemWidget

logger = logging.getLogger(__name__)

def all_fields_filled(match_section):
    """Checks if all required match info fields are filled."""
    required = ['my_team_name', 'opponent_team_name', 'location', 'start_time_offset']
    return all(match_section.get(field) for field in required)

class ConfigWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.config_path = get_shared_data_path() / 'config.ini'
        self.config = configparser.ConfigParser()
        self.load_config()
        self.init_ui()
        
    def load_config(self):
        try:
            with FileLock(self.config_path):
                if self.config_path.exists():
                    self.config.read(self.config_path)
        except TimeoutError as e:
            logger.error(f"Could not acquire lock to read config file: {e}")
            QMessageBox.critical(self, 'Error', 'Could not load configuration file. It may be in use by another process.')
        except Exception as e:
            logger.error(f"Error loading configuration: {e}")
            
    def init_ui(self):
        self.setWindowTitle('VideoGrouper Configuration')
        layout = QVBoxLayout()
        
        # Create tab widget
        tabs = QTabWidget()
        
        # Match Info Tab
        match_tab = QWidget()
        match_layout = QVBoxLayout()
        self.match_info_list = QListWidget()
        self.match_info_list.setSpacing(5)
        self.match_info_list.setWordWrap(True)
        self.match_info_list.setStyleSheet("QListWidget::item { border-bottom: 1px solid #ddd; }")
        match_layout.addWidget(self.match_info_list)
        match_tab.setLayout(match_layout)
        tabs.addTab(match_tab, 'Match Info')

        # Download Queue Tab
        download_queue_tab = QWidget()
        download_queue_layout = QVBoxLayout()
        self.download_queue_list = QListWidget()
        self.download_queue_list.setSpacing(5)
        self.download_queue_list.setIconSize(QSize(160, 90))
        download_queue_layout.addWidget(self.download_queue_list)
        download_queue_tab.setLayout(download_queue_layout)
        tabs.addTab(download_queue_tab, 'Download Queue')

        # Processing Queue Tab
        processing_queue_tab = QWidget()
        processing_queue_layout = QVBoxLayout()
        self.processing_queue_list = QListWidget()
        self.processing_queue_list.setSpacing(5)
        self.processing_queue_list.setIconSize(QSize(160, 90))
        processing_queue_layout.addWidget(self.processing_queue_list)
        processing_queue_tab.setLayout(processing_queue_layout)
        tabs.addTab(processing_queue_tab, 'Processing Queue')

        # Autocam Queue Tab
        autocam_queue_tab = QWidget()
        autocam_queue_layout = QVBoxLayout()
        self.autocam_queue_list = QListWidget()
        self.autocam_queue_list.setSpacing(5)
        autocam_queue_layout.addWidget(self.autocam_queue_list)
        autocam_queue_tab.setLayout(autocam_queue_layout)
        tabs.addTab(autocam_queue_tab, 'Autocam Queue')

        # Connection History Tab
        connection_tab = QWidget()
        connection_layout = QVBoxLayout()
        self.connection_events_list = QListWidget()
        connection_layout.addWidget(self.connection_events_list)
        connection_tab.setLayout(connection_layout)
        tabs.addTab(connection_tab, 'Connection History')

        # Skipped Files Tab
        skipped_tab = QWidget()
        skipped_layout = QVBoxLayout()
        self.skipped_list = QListWidget()
        skipped_layout.addWidget(self.skipped_list)
        skipped_tab.setLayout(skipped_layout)
        tabs.addTab(skipped_tab, 'Skipped Files')
        
        # Settings Tab
        settings_tab = QWidget()
        settings_layout = QVBoxLayout()

        # -- Camera Settings Group --
        camera_group = QGroupBox("Camera Settings")
        camera_layout = QFormLayout()
        self.ip_address = QLineEdit()
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_password_checkbox = QCheckBox("Show Password")
        self.show_password_checkbox.toggled.connect(self.toggle_password_visibility)
        camera_layout.addRow('IP Address:', self.ip_address)
        camera_layout.addRow('Username:', self.username)
        camera_layout.addRow('Password:', self.password)
        camera_layout.addRow('', self.show_password_checkbox)
        camera_group.setLayout(camera_layout)
        settings_layout.addWidget(camera_group)

        # -- Storage Settings Group --
        storage_group = QGroupBox("Storage Settings")
        storage_layout = QFormLayout()
        self.storage_path = QLineEdit()
        browse_button = QPushButton('Browse...')
        browse_button.clicked.connect(self.browse_storage_path)
        storage_layout.addRow('Storage Path:', self.storage_path)
        storage_layout.addRow('', browse_button)
        storage_group.setLayout(storage_layout)
        settings_layout.addWidget(storage_group)

        # -- User Preferences Group --
        prefs_group = QGroupBox("User Preferences")
        prefs_layout = QFormLayout()
        self.timezone_combo = QComboBox()
        self.timezone_combo.addItems(get_all_timezones())
        prefs_layout.addRow("Timezone:", self.timezone_combo)
        prefs_group.setLayout(prefs_layout)
        settings_layout.addWidget(prefs_group)

        settings_layout.addStretch(1) # Pushes content to the top

        # -- Save Button for all settings --
        save_settings_button = QPushButton('Save All Settings')
        save_settings_button.clicked.connect(self.save_settings)
        settings_layout.addWidget(save_settings_button)

        settings_tab.setLayout(settings_layout)
        tabs.addTab(settings_tab, 'Settings')

        # Load existing values into fields
        self.load_settings_into_ui()

        layout.addWidget(tabs)
        self.setLayout(layout)
        
        # Timer to refresh queue displays
        self.queue_timer = QTimer()
        self.queue_timer.timeout.connect(self.refresh_all_displays)
        self.queue_timer.start(5000)
        self.refresh_all_displays()
        
    def load_settings_into_ui(self):
        """Populates the UI fields from the loaded config."""
        if 'CAMERA' in self.config:
            self.ip_address.setText(self.config.get('CAMERA', 'device_ip', fallback=''))
            self.username.setText(self.config.get('CAMERA', 'username', fallback=''))
            self.password.setText(self.config.get('CAMERA', 'password', fallback=''))
        if 'STORAGE' in self.config:
            self.storage_path.setText(self.config.get('STORAGE', 'path', fallback=''))
        if 'APP' in self.config:
            tz_str = self.config.get('APP', 'timezone', fallback='UTC')
            if tz_str in get_all_timezones():
                self.timezone_combo.setCurrentText(tz_str)

    def toggle_password_visibility(self, checked):
        """Toggles the visibility of the password field."""
        if checked:
            self.password.setEchoMode(QLineEdit.EchoMode.Normal)
        else:
            self.password.setEchoMode(QLineEdit.EchoMode.Password)
            
    def browse_storage_path(self):
        path = QFileDialog.getExistingDirectory(self, 'Select Storage Directory')
        if path:
            self.storage_path.setText(path)
            
    def save_settings(self):
        """Saves all settings from the Settings tab."""
        try:
            if 'CAMERA' not in self.config: self.config.add_section('CAMERA')
            if 'STORAGE' not in self.config: self.config.add_section('STORAGE')
            if 'APP' not in self.config: self.config.add_section('APP')
                
            self.config['CAMERA']['device_ip'] = self.ip_address.text()
            self.config['CAMERA']['username'] = self.username.text()
            self.config['CAMERA']['password'] = self.password.text()
            self.config['STORAGE']['path'] = self.storage_path.text()
            self.config['APP']['timezone'] = self.timezone_combo.currentText()
            
            with FileLock(self.config_path):
                with open(self.config_path, 'w') as f:
                    self.config.write(f)
            QMessageBox.information(self, 'Success', 'Settings saved successfully.')

        except TimeoutError:
            QMessageBox.critical(self, 'Error', 'Could not save settings: file is locked.')
        except Exception as e:
            logger.error(f"Error saving settings: {e}")
            QMessageBox.critical(self, 'Error', f'Failed to save settings: {str(e)}')
            
    def refresh_all_displays(self):
        """Refreshes the text in all dynamic display tabs."""
        self.refresh_queue_displays()
        self.refresh_skipped_files_display()
        self.refresh_match_info_display()
        self.refresh_connection_events_display()

    def refresh_queue_displays(self):
        """Refreshes the text in the queue display tabs."""
        self.refresh_download_queue_display()
        self.refresh_processing_queue_display()
        self.refresh_autocam_queue_display()

    def refresh_download_queue_display(self):
        """Reads and displays the download queue state."""
        queue_file = get_shared_data_path() / "download_queue_state.json"
        self.download_queue_list.clear()
        tz_str = self.config.get('APP', 'timezone', fallback='UTC')
        try:
            with FileLock(queue_file):
                if not queue_file.exists(): return
                with open(queue_file, 'r') as f: queue_data = json.load(f)
            if not queue_data: return

            for item_data in queue_data:
                file_path = item_data.get('file_path', 'Unknown File')
                filename = os.path.basename(file_path)
                group_name = os.path.basename(os.path.dirname(file_path))
                
                widget = QueueItemWidget(
                    item_text=filename, 
                    file_path=file_path, 
                    skip_callback=self.handle_skip_request,
                    show_thumbnail=False,
                    group_name=group_name,
                    timezone_str=tz_str
                )
                list_item = QListWidgetItem(self.download_queue_list)
                list_item.setSizeHint(widget.sizeHint())
                self.download_queue_list.addItem(list_item)
                self.download_queue_list.setItemWidget(list_item, widget)

        except TimeoutError:
            self.download_queue_list.addItem("Could not read queue state: file is locked.")
        except Exception as e:
            logger.error(f"Error refreshing download queue display: {e}")

    def refresh_autocam_queue_display(self):
        """Reads and displays the autocam queue state."""
        queue_file = get_shared_data_path() / "autocam_queue_state.json"
        self.autocam_queue_list.clear()
        try:
            with FileLock(queue_file):
                if not queue_file.exists(): return
                with open(queue_file, 'r') as f: queue_data = json.load(f)
            if not queue_data: return

            for group_dir in queue_data:
                item = QListWidgetItem(os.path.basename(group_dir))
                self.autocam_queue_list.addItem(item)
        except Exception as e:
            logger.error(f"Error refreshing autocam queue display: {e}")

    def refresh_processing_queue_display(self):
        """Reads and displays the FFmpeg processing queue state."""
        queue_file = get_shared_data_path() / "ffmpeg_queue_state.json"
        self.processing_queue_list.clear()
        tz_str = self.config.get('APP', 'timezone', fallback='UTC')
        try:
            with FileLock(queue_file):
                if not queue_file.exists(): return
                with open(queue_file, 'r') as f: queue_data = json.load(f)
            if not queue_data: return

            for task_type, item_path in queue_data:
                item_name = os.path.basename(item_path)
                display_text = f"Task: {task_type.capitalize()}, Item: {item_name}"
                
                file_to_skip = item_path if task_type == 'convert' else None
                
                widget = QueueItemWidget(
                    item_text=display_text, 
                    file_path=file_to_skip, 
                    skip_callback=self.handle_skip_request if file_to_skip else None,
                    show_thumbnail=True,
                    timezone_str=tz_str
                )
                list_item = QListWidgetItem(self.processing_queue_list)
                list_item.setSizeHint(widget.sizeHint())
                self.processing_queue_list.addItem(list_item)
                self.processing_queue_list.setItemWidget(list_item, widget)
                
                if not file_to_skip:
                    widget.skip_button.setEnabled(False)
                    widget.skip_button.setToolTip("Cannot skip a directory-level task.")

        except TimeoutError:
            self.processing_queue_list.addItem("Could not read queue state: file is locked.")
        except Exception as e:
            logger.error(f"Error reading processing queue state: {e}")
            self.processing_queue_list.addItem("Error reading processing queue state.")

    def handle_skip_request(self, file_path: str):
        """
        Finds the correct state.json for a file and marks it as skipped.
        """
        try:
            logger.info(f"Received skip request for: {file_path}")
            group_dir = os.path.dirname(file_path)
            if not os.path.isdir(group_dir):
                raise FileNotFoundError(f"Could not find group directory for file: {file_path}")
            
            dir_state = DirectoryState(group_dir)
            asyncio.run(dir_state.mark_file_as_skipped(file_path))

            QMessageBox.information(self, 'Success', f'File marked to be skipped:\n{os.path.basename(file_path)}')
            self.refresh_queue_displays()
        except Exception as e:
            logger.error(f"Error processing skip request for {file_path}: {e}")
            QMessageBox.critical(self, 'Error', f'Failed to mark file as skipped:\n{str(e)}')

    def refresh_skipped_files_display(self):
        """Scans for and displays all files marked as skipped."""
        self.skipped_list.clear()
        storage_path_str = self.config.get('STORAGE', 'path', fallback=None)
        if not storage_path_str or not os.path.isdir(storage_path_str): return
        tz_str = self.config.get('APP', 'timezone', fallback='UTC')

        try:
            for dirname in os.listdir(storage_path_str):
                group_dir_path = os.path.join(storage_path_str, dirname)
                if not os.path.isdir(group_dir_path): continue

                dir_state = DirectoryState(group_dir_path)
                if not dir_state.files: continue # Skip if not a valid group dir or no files

                for file_obj in dir_state.files.values():
                    if file_obj.skip:
                        filename = os.path.basename(file_obj.file_path)
                        display_text = f"{filename} (Status: {file_obj.status})"
                        group_name = os.path.basename(os.path.dirname(file_obj.file_path))

                        widget = QueueItemWidget(
                            item_text=display_text,
                            file_path=file_obj.file_path,
                            skip_callback=None, # No "unskip" functionality for now
                            show_thumbnail=False,
                            group_name=group_name,
                            timezone_str=tz_str
                        )
                        list_item = QListWidgetItem(self.skipped_list)
                        list_item.setSizeHint(widget.sizeHint())
                        self.skipped_list.addItem(list_item)
                        self.skipped_list.setItemWidget(list_item, widget)
        except Exception as e:
            logger.error(f"Error refreshing skipped files display: {e}")

    def refresh_match_info_display(self):
        """Scans for and displays directories that need match info."""
        self.match_info_list.clear()
        storage_path_str = self.config.get('STORAGE', 'path', fallback=None)
        if not storage_path_str or not os.path.isdir(storage_path_str):
            return
        tz_str = self.config.get('APP', 'timezone', fallback='UTC')

        try:
            for dirname in os.listdir(storage_path_str):
                group_dir_path = os.path.join(storage_path_str, dirname)
                if not os.path.isdir(group_dir_path): continue

                dir_state = DirectoryState(group_dir_path)
                if dir_state.status == "combined":
                    # Check if match info is already populated
                    match_info_path = os.path.join(group_dir_path, "match_info.ini")
                    match_info = configparser.ConfigParser()
                    if os.path.exists(match_info_path):
                        match_info.read(match_info_path)
                    
                    if not all_fields_filled(match_info["MATCH"] if "MATCH" in match_info else {}):
                        widget = MatchInfoItemWidget(group_dir_path, self.save_match_info, timezone_str=tz_str)
                        list_item = QListWidgetItem(self.match_info_list)
                        list_item.setSizeHint(widget.sizeHint())
                        self.match_info_list.addItem(list_item)
                        self.match_info_list.setItemWidget(list_item, widget)
        except Exception as e:
            logger.error(f"Error refreshing match info display: {e}")

    def save_match_info(self, group_dir_path, info_dict):
        """Callback to save match info for a group and refresh."""
        try:
            match_info_path = os.path.join(group_dir_path, "match_info.ini")
            
            with FileLock(match_info_path):
                match_info = configparser.ConfigParser()
                if os.path.exists(match_info_path):
                    match_info.read(match_info_path)
                
                if "MATCH" not in match_info:
                    match_info.add_section("MATCH")
                    
                match_info["MATCH"]["start_time_offset"] = info_dict["start_time_offset"]
                match_info["MATCH"]["my_team_name"] = info_dict["my_team_name"]
                match_info["MATCH"]["opponent_team_name"] = info_dict["opponent_team_name"]
                match_info["MATCH"]["location"] = info_dict["location"]
                
                with open(match_info_path, 'w') as f:
                    match_info.write(f)
            
            QMessageBox.information(self, 'Success', f'Match info saved for {os.path.basename(group_dir_path)}')
            self.refresh_match_info_display()
            
        except TimeoutError:
            QMessageBox.critical(self, 'Error', 'Could not save match info: file is locked.')
        except Exception as e:
            logger.error(f"Error saving match info: {e}")
            QMessageBox.critical(self, 'Error', f'Failed to save match info: {str(e)}')

    def update_queue_status(self, status):
        # This method is now deprecated.
        pass
    
    def check_pending_match_info(self):
        # This method is now deprecated and replaced by refresh_match_info_display.
        pass
            
    def save_match_info(self):
        # This method is now deprecated and handled by the item widget's save logic.
        pass

    def refresh_connection_events_display(self):
        """Reads and displays camera connection timeframes."""
        self.connection_events_list.clear()
        storage_path_str = self.config.get('STORAGE', 'path', fallback=None)
        if not storage_path_str or not os.path.isdir(storage_path_str):
            self.connection_events_list.addItem("Storage path not configured or not found.")
            return

        state_file = Path(storage_path_str) / "camera_state.json"
        if not state_file.exists():
            self.connection_events_list.addItem("No connection history found.")
            return

        try:
            with FileLock(state_file):
                with open(state_file, 'r') as f:
                    state_data = json.load(f)
        except (TimeoutError, Exception) as e:
            logger.error(f"Error reading camera state file: {e}")
            self.connection_events_list.addItem(f"Error reading state file: {e}")
            return
            
        connection_events = state_data.get('connection_events', [])
        if not connection_events:
            self.connection_events_list.addItem("No connection events recorded.")
            return

        # Parse events
        parsed_events = []
        for event in connection_events:
            t_str = event['event_datetime']
            event_type = event['event_type']
            dt_obj = datetime.fromisoformat(t_str)
            if dt_obj.tzinfo is None:
                dt_obj = pytz.utc.localize(dt_obj)
            parsed_events.append((dt_obj, event_type))
        
        # Get timeframes
        timeframes = []
        start_time = None
        sorted_events = sorted(parsed_events, key=lambda x: x[0])
        
        for event_time, event_type in sorted_events:
            if event_type == "connected":
                if start_time is None:
                    start_time = event_time
            else:  # any other event is a disconnection
                if start_time is not None:
                    timeframes.append((start_time, event_time))
                    start_time = None
                    
        if start_time is not None:
            timeframes.append((start_time, None))

        # Display timeframes
        if not timeframes:
            self.connection_events_list.addItem("No complete connection periods found.")
            return

        tz_str = self.config.get('APP', 'timezone', fallback='UTC')
        
        for start, end in reversed(timeframes): # Show most recent first
            start_local = convert_utc_to_local(start, tz_str)
            start_str = start_local.strftime('%Y-%m-%d %H:%M:%S')

            if end:
                end_local = convert_utc_to_local(end, tz_str)
                end_str = end_local.strftime('%Y-%m-%d %H:%M:%S')
                
                # Find the corresponding disconnection event to get the message
                message = f"Disconnected: {end_str}"
                for event in connection_events:
                    if datetime.fromisoformat(event['event_datetime']).astimezone(pytz.utc) == end.astimezone(pytz.utc) and event['event_type'] == 'disconnected':
                        message = f"Disconnected: {end_str} ({event['message']})"
                        break
                
                display_text = f"Connected: {start_str}  |  {message}"
            else:
                display_text = f"Connected: {start_str}  |  (Still connected)"
            
            self.connection_events_list.addItem(display_text)

def main():
    """Main entry point for the standalone configuration UI."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        stream=sys.stdout
    )
    logger.info("Running Configuration UI in standalone mode")

    app = QApplication(sys.argv)
    
    # Config path is now handled by the window itself
    window = ConfigWindow()
    window.show()
    app.setQuitOnLastWindowClosed(True)
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main() 