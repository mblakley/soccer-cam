import sys
import os
import configparser
import logging
import json
import asyncio
import pytz
import threading
from pathlib import Path
from datetime import datetime
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QTabWidget, QLabel, 
                             QLineEdit, QPushButton, QFormLayout, QFileDialog, QMessageBox, QCheckBox, QListWidget, QListWidgetItem, QGroupBox, QComboBox)
from PyQt6.QtCore import QTimer, QSize, pyqtSignal as Signal
from PyQt6.QtGui import QIcon
from video_grouper.locking import FileLock
from video_grouper.paths import get_shared_data_path
from video_grouper.directory_state import DirectoryState
from video_grouper.time_utils import get_all_timezones, convert_utc_to_local
from video_grouper.models import MatchInfo
from video_grouper.youtube_upload import authenticate_youtube, get_youtube_paths
from .queue_item_widget import QueueItemWidget
from .match_info_item_widget import MatchInfoItemWidget

logger = logging.getLogger(__name__)

def all_fields_filled(match_info):
    """Checks if all required match info fields are filled."""
    if match_info is None:
        return False
    
    required_fields = [match_info.my_team_name, match_info.opponent_team_name, match_info.location, match_info.start_time_offset]
    return all(field.strip() for field in required_fields)

class ConfigWindow(QWidget):
    # Signal emitted when configuration is saved
    config_saved = Signal()

    def __init__(self, config=None):
        super().__init__()
        self.config_path = get_shared_data_path() / 'config.ini'
        self.config = config if config is not None else configparser.ConfigParser()
        if config is None:
            self.load_config()
        
        # Set the window icon
        script_dir = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(script_dir, '..', 'icon.ico')
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        
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

        # -- YouTube Settings Group --
        youtube_group = QGroupBox("YouTube Upload Settings")
        youtube_layout = QFormLayout()
        
        # YouTube enabled checkbox
        self.youtube_enabled = QCheckBox("Enable YouTube Uploads")
        
        # Authentication button
        self.youtube_auth_button = QPushButton('Authenticate with YouTube')
        self.youtube_auth_button.clicked.connect(self.authenticate_youtube)
        
        # Status label and refresh button
        status_layout = QVBoxLayout()
        self.youtube_status_label = QLabel("Not authenticated")
        refresh_status_button = QPushButton("Refresh Status")
        refresh_status_button.clicked.connect(self.refresh_youtube_status)
        status_layout.addWidget(self.youtube_status_label)
        status_layout.addWidget(refresh_status_button)
        
        # Playlist configuration
        playlist_group = QGroupBox("Playlist Configuration")
        playlist_layout = QFormLayout()
        
        # Processed videos playlist
        self.processed_playlist_name = QLineEdit()
        self.processed_playlist_name.setPlaceholderText("{my_team_name} 2013s")
        
        # Raw videos playlist
        self.raw_playlist_name = QLineEdit()
        self.raw_playlist_name.setPlaceholderText("{my_team_name} 2013s - Full Field")
        
        playlist_layout.addRow("Processed Videos Playlist:", self.processed_playlist_name)
        playlist_layout.addRow("Raw Videos Playlist:", self.raw_playlist_name)
        playlist_group.setLayout(playlist_layout)
        
        youtube_layout.addRow('', self.youtube_enabled)
        youtube_layout.addRow('', self.youtube_auth_button)
        youtube_layout.addRow('Status:', status_layout)
        youtube_layout.addRow('', playlist_group)
        
        youtube_group.setLayout(youtube_layout)
        settings_layout.addWidget(youtube_group)

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
        if 'YOUTUBE' in self.config:
            self.youtube_enabled.setChecked(self.config.getboolean('YOUTUBE', 'enabled', fallback=False))
            
            # Check token status
            storage_path = self.config.get('STORAGE', 'path', fallback=None)
            if storage_path:
                self.check_youtube_token_status()
            
        # Load playlist configuration
        if 'youtube.playlist.processed' in self.config:
            self.processed_playlist_name.setText(self.config.get('youtube.playlist.processed', 'name_format', fallback="{my_team_name} 2013s"))
        if 'youtube.playlist.raw' in self.config:
            self.raw_playlist_name.setText(self.config.get('youtube.playlist.raw', 'name_format', fallback="{my_team_name} 2013s - Full Field"))

    def check_youtube_token_status(self, token_file_path=None):
        """Check if the YouTube token file exists and update the status label accordingly."""
        storage_path = self.storage_path.text()
        if not storage_path:
            self.youtube_status_label.setText("Storage path not set")
            return
        
        # Get token file path
        _, token_file = get_youtube_paths(storage_path)
        
        if os.path.exists(token_file):
            try:
                with open(token_file, 'r') as f:
                    token_data = json.load(f)
                    
                # Check if token has basic required fields
                if 'token' in token_data and 'refresh_token' in token_data:
                    self.youtube_status_label.setText("Token exists (click Authenticate to verify)")
                else:
                    self.youtube_status_label.setText("Token exists but may be invalid")
            except Exception:
                self.youtube_status_label.setText("Token file exists but is not valid JSON")
        else:
            self.youtube_status_label.setText("Not authenticated")

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
            
    def authenticate_youtube(self):
        """Authenticate with YouTube API."""
        # Get storage path for resolving paths
        storage_path = self.storage_path.text()
        if not storage_path:
            QMessageBox.warning(self, 'Warning', 'Please specify the storage path first.')
            return
        
        # Get credentials and token file paths
        credentials_file, token_file = get_youtube_paths(storage_path)
        
        # Check if credentials file exists
        if not os.path.exists(credentials_file):
            QMessageBox.warning(self, 'Warning', 
                               f"YouTube credentials file not found at: {credentials_file}\n\n"
                               f"Please place your client_secret.json file in the {os.path.dirname(credentials_file)} directory.")
            return
        
        # Update status
        self.youtube_status_label.setText("Authenticating...")
        self.youtube_auth_button.setEnabled(False)
        
        # Run authentication in a separate thread to avoid freezing UI
        def auth_thread():
            try:
                success, message = authenticate_youtube(credentials_file, token_file)
                
                # Update UI in the main thread
                def update_ui():
                    self.youtube_auth_button.setEnabled(True)
                    if success:
                        self.youtube_status_label.setText("Authenticated")
                        QMessageBox.information(self, 'Success', message)
                    else:
                        self.youtube_status_label.setText("Authentication failed")
                        QMessageBox.warning(self, 'Authentication Failed', message)
                
                # Execute in main thread
                QTimer.singleShot(0, update_ui)
                
            except Exception as e:
                logger.error(f"Error during YouTube authentication: {e}")
                
                def show_error():
                    self.youtube_auth_button.setEnabled(True)
                    self.youtube_status_label.setText("Authentication error")
                    QMessageBox.critical(self, 'Error', f"Authentication error: {str(e)}")
                
                QTimer.singleShot(0, show_error)
        
        # Start authentication thread
        auth_thread = threading.Thread(target=auth_thread, daemon=True)
        auth_thread.start()
        
        # Add a timeout to check if the thread is still running
        def check_auth_thread():
            if auth_thread.is_alive():
                # Thread is still running after timeout, assume it's stuck
                self.youtube_auth_button.setEnabled(True)
                self.youtube_status_label.setText("Authentication timed out")
                QMessageBox.warning(self, 'Authentication Timeout', 
                                   "Authentication process is taking too long. The browser may have completed, but the callback failed.\n\n"
                                   "Check if a token.json file was created in your YouTube directory. If it exists, authentication may have succeeded despite this error.")
            
        # Check after 30 seconds
        QTimer.singleShot(30000, check_auth_thread)

    def save_settings(self):
        """Saves all settings from the Settings tab."""
        try:
            if 'CAMERA' not in self.config: self.config.add_section('CAMERA')
            if 'STORAGE' not in self.config: self.config.add_section('STORAGE')
            if 'APP' not in self.config: self.config.add_section('APP')
            if 'YOUTUBE' not in self.config: self.config.add_section('YOUTUBE')
            if 'youtube.playlist.processed' not in self.config: self.config.add_section('youtube.playlist.processed')
            if 'youtube.playlist.raw' not in self.config: self.config.add_section('youtube.playlist.raw')
                
            self.config['CAMERA']['device_ip'] = self.ip_address.text()
            self.config['CAMERA']['username'] = self.username.text()
            self.config['CAMERA']['password'] = self.password.text()
            self.config['STORAGE']['path'] = self.storage_path.text()
            self.config['APP']['timezone'] = self.timezone_combo.currentText()
            self.config['YOUTUBE']['enabled'] = str(self.youtube_enabled.isChecked())
            
            # Save playlist configuration
            self.config['youtube.playlist.processed']['name_format'] = self.processed_playlist_name.text() or "{my_team_name} 2013s"
            self.config['youtube.playlist.processed']['description'] = f"Processed videos for {'{my_team_name}'} 2013s team"
            self.config['youtube.playlist.processed']['privacy_status'] = "unlisted"
            
            self.config['youtube.playlist.raw']['name_format'] = self.raw_playlist_name.text() or "{my_team_name} 2013s - Full Field"
            self.config['youtube.playlist.raw']['description'] = f"Raw full field videos for {'{my_team_name}'} 2013s team"
            self.config['youtube.playlist.raw']['privacy_status'] = "unlisted"
            
            with FileLock(self.config_path):
                with open(self.config_path, 'w') as f:
                    self.config.write(f)
            QMessageBox.information(self, 'Success', 'Settings saved successfully.')
            self.config_saved.emit()

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

            for item in queue_data:
                group_name = item.get('group_name', 'Unknown')
                status = item.get('status', 'unknown')
                display_text = f"{group_name} - Status: {status}"
                self.autocam_queue_list.addItem(display_text)
        except Exception as e:
            logger.error(f"Error refreshing autocam queue display: {e}")
            
    def refresh_autocam_queue_tab(self):
        """Refreshes the autocam queue tab."""
        self.refresh_autocam_queue_display()

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
                    match_info = MatchInfo.from_file(match_info_path)
                    
                    if not all_fields_filled(match_info):
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
            
            # Create a MatchInfo object with the provided data
            match_info = MatchInfo(
                my_team_name=info_dict["my_team_name"],
                opponent_team_name=info_dict["opponent_team_name"],
                location=info_dict["location"],
                start_time_offset=info_dict["start_time_offset"]
            )
            
            # Convert to ConfigParser to save to file
            with FileLock(match_info_path):
                config = configparser.ConfigParser()
                if os.path.exists(match_info_path):
                    config.read(match_info_path)
                
                if "MATCH" not in config:
                    config.add_section("MATCH")
                    
                config["MATCH"]["start_time_offset"] = match_info.start_time_offset
                config["MATCH"]["my_team_name"] = match_info.my_team_name
                config["MATCH"]["opponent_team_name"] = match_info.opponent_team_name
                config["MATCH"]["location"] = match_info.location
                # Preserve total_duration if it exists
                if "total_duration" in config["MATCH"]:
                    match_info.total_duration = config["MATCH"]["total_duration"]
                else:
                    config["MATCH"]["total_duration"] = match_info.total_duration
                
                with open(match_info_path, 'w') as f:
                    config.write(f)
            
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

    def refresh_youtube_status(self):
        """Manually refresh the YouTube token status."""
        storage_path = self.storage_path.text()
        if storage_path:
            self.check_youtube_token_status()
        else:
            self.youtube_status_label.setText("Storage path not set")

def main():
    """Main entry point for the standalone configuration UI."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        stream=sys.stdout
    )
    logger.info("Running Configuration UI in standalone mode")

    app = QApplication(sys.argv)
    
    # Set the application icon
    script_dir = os.path.dirname(os.path.abspath(__file__))
    icon_path = os.path.join(script_dir, '..', 'icon.ico')
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    
    # Config path is now handled by the window itself
    window = ConfigWindow()
    window.show()
    app.setQuitOnLastWindowClosed(True)
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main() 