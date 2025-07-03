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
from PyQt6.QtWidgets import (
    QApplication,
    QVBoxLayout,
    QTabWidget,
    QLabel,
    QLineEdit,
    QPushButton,
    QFormLayout,
    QFileDialog,
    QMessageBox,
    QCheckBox,
    QListWidget,
    QListWidgetItem,
    QGroupBox,
    QComboBox,
    QHBoxLayout,
    QDialog,
    QDialogButtonBox,
    QScrollArea,
    QWidget,
    QInputDialog,
)
from PyQt6.QtCore import QTimer, QSize, Qt, pyqtSignal as Signal
from PyQt6.QtGui import QIcon
from video_grouper.utils.locking import FileLock
from video_grouper.utils.paths import get_shared_data_path
from video_grouper.models import DirectoryState
from video_grouper.utils.time_utils import get_all_timezones, convert_utc_to_local
from video_grouper.models import MatchInfo
from video_grouper.utils.youtube_upload import authenticate_youtube, get_youtube_paths
from video_grouper.api_integrations.cloud_sync import CloudSync, GoogleAuthProvider
from .queue_item_widget import QueueItemWidget
from .match_info_item_widget import MatchInfoItemWidget
from video_grouper.utils.config import (
    load_config,
    save_config,
    Config,
    TeamSnapTeamConfig,
    PlayMetricsTeamConfig,
)
from typing import Optional

logger = logging.getLogger(__name__)


def all_fields_filled(match_info):
    """Checks if all required match info fields are filled."""
    if match_info is None:
        return False

    required_fields = [
        match_info.my_team_name,
        match_info.opponent_team_name,
        match_info.location,
        match_info.start_time_offset,
    ]
    return all(field.strip() for field in required_fields)


class ConfigWindow(QWidget):
    # Signal emitted when configuration is saved
    config_saved = Signal()

    def __init__(self):
        super().__init__()
        self.config_path = get_shared_data_path() / "config.ini"
        self.config: Optional[Config] = None
        self.load_config()

        # Set the window icon
        script_dir = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(script_dir, "..", "icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.init_ui()

    def load_config(self):
        try:
            with FileLock(self.config_path):
                if self.config_path.exists():
                    self.config = load_config(self.config_path)
        except TimeoutError as e:
            logger.error(f"Could not acquire lock to read config file: {e}")
            QMessageBox.critical(
                self,
                "Error",
                "Could not load configuration file. It may be in use by another process.",
            )
        except Exception as e:
            logger.error(f"Error loading configuration: {e}")

    def init_ui(self):
        self.setWindowTitle("VideoGrouper Configuration")
        layout = QVBoxLayout()

        # Create tab widget
        tabs = QTabWidget()

        # Match Info Tab
        match_tab = QWidget()
        match_layout = QVBoxLayout()
        self.match_info_list = QListWidget()
        self.match_info_list.setSpacing(5)
        self.match_info_list.setWordWrap(True)
        self.match_info_list.setStyleSheet(
            "QListWidget::item { border-bottom: 1px solid #ddd; }"
        )
        match_layout.addWidget(self.match_info_list)
        match_tab.setLayout(match_layout)
        tabs.addTab(match_tab, "Match Info")

        # Download Queue Tab
        download_queue_tab = QWidget()
        download_queue_layout = QVBoxLayout()
        self.download_queue_list = QListWidget()
        self.download_queue_list.setSpacing(5)
        self.download_queue_list.setIconSize(QSize(160, 90))
        download_queue_layout.addWidget(self.download_queue_list)
        download_queue_tab.setLayout(download_queue_layout)
        tabs.addTab(download_queue_tab, "Download Queue")

        # Processing Queue Tab
        processing_queue_tab = QWidget()
        processing_queue_layout = QVBoxLayout()
        self.processing_queue_list = QListWidget()
        self.processing_queue_list.setSpacing(5)
        self.processing_queue_list.setIconSize(QSize(160, 90))
        processing_queue_layout.addWidget(self.processing_queue_list)
        processing_queue_tab.setLayout(processing_queue_layout)
        tabs.addTab(processing_queue_tab, "Processing Queue")

        # Autocam Queue Tab
        autocam_queue_tab = QWidget()
        autocam_queue_layout = QVBoxLayout()
        self.autocam_queue_list = QListWidget()
        self.autocam_queue_list.setSpacing(5)
        autocam_queue_layout.addWidget(self.autocam_queue_list)
        autocam_queue_tab.setLayout(autocam_queue_layout)
        tabs.addTab(autocam_queue_tab, "Autocam Queue")

        # Connection History Tab
        connection_tab = QWidget()
        connection_layout = QVBoxLayout()
        self.connection_events_list = QListWidget()
        connection_layout.addWidget(self.connection_events_list)
        connection_tab.setLayout(connection_layout)
        tabs.addTab(connection_tab, "Connection History")

        # Skipped Files Tab
        skipped_tab = QWidget()
        skipped_layout = QVBoxLayout()
        self.skipped_list = QListWidget()
        skipped_layout.addWidget(self.skipped_list)
        skipped_tab.setLayout(skipped_layout)
        tabs.addTab(skipped_tab, "Skipped Files")

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
        camera_layout.addRow("IP Address:", self.ip_address)
        camera_layout.addRow("Username:", self.username)
        camera_layout.addRow("Password:", self.password)
        camera_layout.addRow("", self.show_password_checkbox)
        camera_group.setLayout(camera_layout)
        settings_layout.addWidget(camera_group)

        # -- Storage Settings Group --
        storage_group = QGroupBox("Storage Settings")
        storage_layout = QFormLayout()
        self.storage_path = QLineEdit()
        browse_button = QPushButton("Browse...")
        browse_button.clicked.connect(self.browse_storage_path)
        storage_layout.addRow("Storage Path:", self.storage_path)
        storage_layout.addRow("", browse_button)
        storage_group.setLayout(storage_layout)
        settings_layout.addWidget(storage_group)

        # -- YouTube Settings Group --
        youtube_group = QGroupBox("YouTube Upload Settings")
        youtube_layout = QFormLayout()

        # YouTube enabled checkbox
        self.youtube_enabled = QCheckBox("Enable YouTube Uploads")

        # Authentication button
        self.youtube_auth_button = QPushButton("Authenticate with YouTube")
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

        playlist_layout.addRow(
            "Processed Videos Playlist:", self.processed_playlist_name
        )
        playlist_layout.addRow("Raw Videos Playlist:", self.raw_playlist_name)
        playlist_group.setLayout(playlist_layout)

        youtube_layout.addRow("", self.youtube_enabled)
        youtube_layout.addRow("", self.youtube_auth_button)
        youtube_layout.addRow("Status:", status_layout)
        youtube_layout.addRow("", playlist_group)

        youtube_group.setLayout(youtube_layout)
        settings_layout.addWidget(youtube_group)

        # -- Team Management Tab --
        team_management_tab = QWidget()
        team_management_layout = QVBoxLayout()

        # Create tab widget for different integration types
        team_integrations_tabs = QTabWidget()

        # -- TeamSnap Tab --
        teamsnap_tab = QWidget()
        teamsnap_layout = QVBoxLayout()

        # TeamSnap configurations list
        teamsnap_list_group = QGroupBox("TeamSnap Configurations")
        teamsnap_list_layout = QVBoxLayout()

        self.teamsnap_configs_list = QListWidget()
        self.teamsnap_configs_list.setSelectionMode(
            QListWidget.SelectionMode.SingleSelection
        )
        self.teamsnap_configs_list.itemSelectionChanged.connect(
            self.load_selected_teamsnap_config
        )

        teamsnap_buttons_layout = QHBoxLayout()
        self.add_teamsnap_button = QPushButton("Add")
        self.add_teamsnap_button.clicked.connect(self.add_teamsnap_config)
        self.remove_teamsnap_button = QPushButton("Remove")
        self.remove_teamsnap_button.clicked.connect(self.remove_teamsnap_config)
        teamsnap_buttons_layout.addWidget(self.add_teamsnap_button)
        teamsnap_buttons_layout.addWidget(self.remove_teamsnap_button)

        teamsnap_list_layout.addWidget(self.teamsnap_configs_list)
        teamsnap_list_layout.addLayout(teamsnap_buttons_layout)
        teamsnap_list_group.setLayout(teamsnap_list_layout)
        teamsnap_layout.addWidget(teamsnap_list_group)

        # TeamSnap configuration form
        teamsnap_form_group = QGroupBox("TeamSnap Configuration")
        teamsnap_form_layout = QFormLayout()

        # Configuration name
        self.teamsnap_config_name = QLineEdit()
        teamsnap_form_layout.addRow("Configuration Name:", self.teamsnap_config_name)

        # TeamSnap enabled checkbox
        self.teamsnap_enabled = QCheckBox("Enable TeamSnap Integration")
        teamsnap_form_layout.addRow("", self.teamsnap_enabled)

        # TeamSnap credentials
        self.teamsnap_client_id = QLineEdit()
        self.teamsnap_client_secret = QLineEdit()
        self.teamsnap_client_secret.setEchoMode(QLineEdit.EchoMode.Password)

        # Show password checkbox
        self.show_teamsnap_secret_checkbox = QCheckBox("Show Secret")
        self.show_teamsnap_secret_checkbox.toggled.connect(
            lambda checked: self.teamsnap_client_secret.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )

        # Hidden fields (not shown in UI but used for storage)
        self.teamsnap_access_token = QLineEdit()
        self.teamsnap_team_id = QLineEdit()
        self.teamsnap_team_name = QLineEdit()

        # Fetch TeamSnap info button
        self.fetch_teamsnap_info_button = QPushButton("Fetch Team Info")
        self.fetch_teamsnap_info_button.clicked.connect(self.fetch_teamsnap_info)

        # Status label
        self.teamsnap_status_label = QLabel("Not connected")

        # Add fields to layout
        teamsnap_form_layout.addRow("Client ID:", self.teamsnap_client_id)
        teamsnap_form_layout.addRow("Client Secret:", self.teamsnap_client_secret)
        teamsnap_form_layout.addRow("", self.show_teamsnap_secret_checkbox)
        teamsnap_form_layout.addRow("", self.fetch_teamsnap_info_button)
        teamsnap_form_layout.addRow("Status:", self.teamsnap_status_label)

        # Save button
        self.save_teamsnap_button = QPushButton("Save Configuration")
        self.save_teamsnap_button.clicked.connect(self.save_teamsnap_config)
        teamsnap_form_layout.addRow("", self.save_teamsnap_button)

        teamsnap_form_group.setLayout(teamsnap_form_layout)
        teamsnap_layout.addWidget(teamsnap_form_group)

        teamsnap_tab.setLayout(teamsnap_layout)

        # -- PlayMetrics Tab --
        playmetrics_tab = QWidget()
        playmetrics_layout = QVBoxLayout()

        # PlayMetrics configurations list
        playmetrics_list_group = QGroupBox("PlayMetrics Configurations")
        playmetrics_list_layout = QVBoxLayout()

        self.playmetrics_configs_list = QListWidget()
        self.playmetrics_configs_list.setSelectionMode(
            QListWidget.SelectionMode.SingleSelection
        )
        self.playmetrics_configs_list.itemSelectionChanged.connect(
            self.load_selected_playmetrics_config
        )

        playmetrics_buttons_layout = QHBoxLayout()
        self.add_playmetrics_button = QPushButton("Add")
        self.add_playmetrics_button.clicked.connect(self.add_playmetrics_config)
        self.remove_playmetrics_button = QPushButton("Remove")
        self.remove_playmetrics_button.clicked.connect(self.remove_playmetrics_config)
        playmetrics_buttons_layout.addWidget(self.add_playmetrics_button)
        playmetrics_buttons_layout.addWidget(self.remove_playmetrics_button)

        playmetrics_list_layout.addWidget(self.playmetrics_configs_list)
        playmetrics_list_layout.addLayout(playmetrics_buttons_layout)
        playmetrics_list_group.setLayout(playmetrics_list_layout)
        playmetrics_layout.addWidget(playmetrics_list_group)

        # PlayMetrics configuration form
        playmetrics_form_group = QGroupBox("PlayMetrics Configuration")
        playmetrics_form_layout = QFormLayout()

        # Configuration name
        self.playmetrics_config_name = QLineEdit()
        playmetrics_form_layout.addRow(
            "Configuration Name:", self.playmetrics_config_name
        )

        # PlayMetrics enabled checkbox
        self.playmetrics_enabled = QCheckBox("Enable PlayMetrics Integration")
        playmetrics_form_layout.addRow("", self.playmetrics_enabled)

        # PlayMetrics credentials
        self.playmetrics_username = QLineEdit()
        self.playmetrics_password = QLineEdit()
        self.playmetrics_password.setEchoMode(QLineEdit.EchoMode.Password)

        # Hidden fields (not shown in UI but used for storage)
        self.playmetrics_team_id = QLineEdit()
        self.playmetrics_team_name = QLineEdit()

        # Show password checkbox
        self.show_playmetrics_password_checkbox = QCheckBox("Show Password")
        self.show_playmetrics_password_checkbox.toggled.connect(
            lambda checked: self.playmetrics_password.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )

        # Fetch PlayMetrics info button
        self.fetch_playmetrics_info_button = QPushButton("Fetch Team Info")
        self.fetch_playmetrics_info_button.clicked.connect(self.fetch_playmetrics_info)

        # Status label
        self.playmetrics_status_label = QLabel("Not connected")

        # Add fields to layout
        playmetrics_form_layout.addRow("Username:", self.playmetrics_username)
        playmetrics_form_layout.addRow("Password:", self.playmetrics_password)
        playmetrics_form_layout.addRow("", self.show_playmetrics_password_checkbox)
        playmetrics_form_layout.addRow("", self.fetch_playmetrics_info_button)
        playmetrics_form_layout.addRow("Status:", self.playmetrics_status_label)

        # Save button
        self.save_playmetrics_button = QPushButton("Save Configuration")
        self.save_playmetrics_button.clicked.connect(self.save_playmetrics_config)
        playmetrics_form_layout.addRow("", self.save_playmetrics_button)

        playmetrics_form_group.setLayout(playmetrics_form_layout)
        playmetrics_layout.addWidget(playmetrics_form_group)

        playmetrics_tab.setLayout(playmetrics_layout)

        # Add tabs to team integrations tab widget
        team_integrations_tabs.addTab(teamsnap_tab, "TeamSnap")
        team_integrations_tabs.addTab(playmetrics_tab, "PlayMetrics")

        team_management_layout.addWidget(team_integrations_tabs)
        team_management_tab.setLayout(team_management_layout)

        # -- User Preferences Group --
        prefs_group = QGroupBox("User Preferences")
        prefs_layout = QFormLayout()
        self.timezone_combo = QComboBox()
        self.timezone_combo.addItems(get_all_timezones())
        prefs_layout.addRow("Timezone:", self.timezone_combo)
        prefs_group.setLayout(prefs_layout)
        settings_layout.addWidget(prefs_group)

        # -- Cloud Sync Group --
        cloud_sync_group = QGroupBox("Cloud Sync")
        cloud_sync_layout = (
            QHBoxLayout()
        )  # Main layout is horizontal to split left/right sides

        # Left side - Username and password fields stacked vertically
        self.auth_stack_left = QVBoxLayout()
        self.auth_stack_left.setContentsMargins(0, 0, 10, 0)  # Add some right margin

        # Username field
        username_layout = QFormLayout()
        self.cloud_username = QLineEdit()
        self.cloud_username.textChanged.connect(self.update_cloud_sync_ui_state)
        username_layout.addRow("Username:", self.cloud_username)

        # Password field
        self.cloud_password = QLineEdit()
        self.cloud_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.cloud_password.textChanged.connect(self.update_cloud_sync_ui_state)
        username_layout.addRow("Password:", self.cloud_password)

        # Show password checkbox
        self.show_cloud_password_checkbox = QCheckBox("Show Password")
        self.show_cloud_password_checkbox.toggled.connect(
            lambda checked: self.cloud_password.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        username_layout.addRow("", self.show_cloud_password_checkbox)

        self.auth_stack_left.addLayout(username_layout)

        # Signed in state (initially hidden)
        self.signed_in_layout = QVBoxLayout()
        self.signed_in_label = QLabel("Signed in as: Not signed in")
        self.signed_in_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sign_out_button = QPushButton("Sign Out")
        self.sign_out_button.clicked.connect(self.sign_out_cloud)
        self.signed_in_layout.addWidget(self.signed_in_label)
        self.signed_in_layout.addWidget(self.sign_out_button)
        self.signed_in_layout.addStretch(1)

        # Create widgets to hold the layouts
        self.auth_widget = QWidget()
        self.auth_widget.setLayout(self.auth_stack_left)
        self.signed_in_widget = QWidget()
        self.signed_in_widget.setLayout(self.signed_in_layout)
        self.signed_in_widget.setVisible(False)  # Initially hidden

        # Right side - Google auth
        right_layout = QVBoxLayout()
        right_layout.setContentsMargins(10, 0, 0, 0)  # Add some left margin

        # OR label centered
        or_label = QLabel("OR")
        or_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_layout.addWidget(or_label)

        # Google auth button
        self.google_auth_button = QPushButton("Sign in with Google")
        self.google_auth_button.clicked.connect(self.authenticate_with_google)
        right_layout.addWidget(self.google_auth_button)

        # Google auth status
        self.google_auth_status = QLabel("Not signed in")
        self.google_auth_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_layout.addWidget(self.google_auth_status)

        # Add stretches to center the content vertically
        right_layout.addStretch(1)

        # Add left and right layouts to the main horizontal layout
        left_container = QVBoxLayout()
        left_container.addWidget(self.auth_widget)
        left_container.addWidget(self.signed_in_widget)
        cloud_sync_layout.addLayout(left_container, 1)  # 1 = stretch factor
        cloud_sync_layout.addLayout(right_layout, 1)

        # Create a wrapper layout for the sync button (to span both columns)
        wrapper_layout = QVBoxLayout()
        wrapper_layout.addLayout(cloud_sync_layout)

        # Sync to cloud button
        self.sync_to_cloud_button = QPushButton("Sync to Cloud")
        self.sync_to_cloud_button.clicked.connect(self.sync_to_cloud)
        self.sync_to_cloud_button.setEnabled(
            False
        )  # Disabled by default until authenticated
        wrapper_layout.addWidget(self.sync_to_cloud_button)

        cloud_sync_group.setLayout(wrapper_layout)
        settings_layout.addWidget(cloud_sync_group)

        settings_layout.addStretch(1)  # Pushes content to the top

        # -- Save Button for all settings --
        save_settings_button = QPushButton("Save All Settings")
        save_settings_button.clicked.connect(self.save_settings)
        settings_layout.addWidget(save_settings_button)

        settings_tab.setLayout(settings_layout)
        tabs.addTab(settings_tab, "Settings")
        tabs.addTab(team_management_tab, "Team Management")

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
        """Load settings from the config object into the UI."""
        if not self.config:
            return

        # Camera settings
        self.ip_address.setText(self.config.camera.device_ip)
        self.username.setText(self.config.camera.username)
        self.password.setText(self.config.camera.password)

        # Storage settings
        self.storage_path.setText(self.config.storage.path)

        # YouTube settings
        self.youtube_enabled.setChecked(self.config.youtube.enabled)
        self.processed_playlist_name.setText(
            self.config.youtube.processed_playlist.name_format
        )
        self.raw_playlist_name.setText(self.config.youtube.raw_playlist.name_format)

        # TeamSnap configurations
        self.teamsnap_configs_list.clear()
        if self.config.teamsnap.enabled and not self.config.teamsnap_teams:
            self.teamsnap_configs_list.addItem("Default")
        for team in self.config.teamsnap_teams:
            self.teamsnap_configs_list.addItem(team.team_name)

        # PlayMetrics configurations
        self.playmetrics_configs_list.clear()
        if self.config.playmetrics.enabled and not self.config.playmetrics_teams:
            self.playmetrics_configs_list.addItem("Default")
        for team in self.config.playmetrics_teams:
            self.playmetrics_configs_list.addItem(team.team_name)

        # Cloud Sync settings
        # This part will be more complex and will be handled separately

        # Refresh the UI state for dynamic elements
        self.update_cloud_sync_ui_state()

    def check_youtube_token_status(self, token_file_path=None):
        """Check the status of the YouTube token file."""
        if not token_file_path:
            return

        storage_path = self.storage_path.text()
        if not storage_path:
            self.youtube_status_label.setText("Storage path not set")
            return

        # Get token file path
        _, token_file = get_youtube_paths(storage_path)

        if os.path.exists(token_file):
            try:
                with open(token_file, "r") as f:
                    token_data = json.load(f)

                # Check if token has basic required fields
                if "token" in token_data and "refresh_token" in token_data:
                    self.youtube_status_label.setText(
                        "Token exists (click Authenticate to verify)"
                    )
                else:
                    self.youtube_status_label.setText("Token exists but may be invalid")
            except Exception:
                self.youtube_status_label.setText(
                    "Token file exists but is not valid JSON"
                )
        else:
            self.youtube_status_label.setText("Not authenticated")
            self.password.setEchoMode(QLineEdit.EchoMode.Password)

    def browse_storage_path(self):
        path = QFileDialog.getExistingDirectory(self, "Select Storage Directory")
        if path:
            self.storage_path.setText(path)

    def authenticate_youtube(self):
        """Authenticate with YouTube API."""
        # Get storage path for resolving paths
        storage_path = self.storage_path.text()
        if not storage_path:
            QMessageBox.warning(
                self, "Warning", "Please specify the storage path first."
            )
            return

        # Get credentials and token file paths
        credentials_file, token_file = get_youtube_paths(storage_path)

        # Check if credentials file exists
        if not os.path.exists(credentials_file):
            QMessageBox.warning(
                self,
                "Warning",
                f"YouTube credentials file not found at: {credentials_file}\n\n"
                f"Please place your client_secret.json file in the {os.path.dirname(credentials_file)} directory.",
            )
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
                        QMessageBox.information(self, "Success", message)
                    else:
                        self.youtube_status_label.setText("Authentication failed")
                        QMessageBox.warning(self, "Authentication Failed", message)

                # Execute in main thread
                QTimer.singleShot(0, update_ui)

            except Exception as e:
                logger.error(f"Error during YouTube authentication: {e}")

                def show_error(err=e):
                    self.youtube_auth_button.setEnabled(True)
                    self.youtube_status_label.setText("Authentication error")
                    QMessageBox.critical(
                        self, "Error", f"Authentication error: {str(err)}"
                    )

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
                QMessageBox.warning(
                    self,
                    "Authentication Timeout",
                    "Authentication process is taking too long. The browser may have completed, but the callback failed.\n\n"
                    "Check if a token.json file was created in your YouTube directory. If it exists, authentication may have succeeded despite this error.",
                )

        # Check after 30 seconds
        QTimer.singleShot(30000, check_auth_thread)

    def save_settings(self):
        """Save all settings from the UI to the config file."""
        try:
            with FileLock(self.config_path):
                # Update config object from UI
                self.config.camera.device_ip = self.ip_address.text()
                self.config.camera.username = self.username.text()
                self.config.camera.password = self.password.text()
                self.config.storage.path = self.storage_path.text()
                self.config.youtube.enabled = self.youtube_enabled.isChecked()
                self.config.youtube.processed_playlist.name_format = (
                    self.processed_playlist_name.text()
                )
                self.config.youtube.raw_playlist.name_format = (
                    self.raw_playlist_name.text()
                )
                self.config.app.timezone = self.timezone_combo.currentText()

                # Save the updated config object
                save_config(self.config, self.config_path)

            QMessageBox.information(self, "Success", "Settings saved successfully!")
            self.config_saved.emit()
        except TimeoutError as e:
            logger.error(f"Could not acquire lock to save config file: {e}")
            QMessageBox.critical(
                self,
                "Error",
                "Could not save configuration file. It may be in use by another process.",
            )
        except Exception as e:
            logger.error(f"Error saving settings: {e}")
            QMessageBox.critical(self, "Error", f"Failed to save settings: {e}")

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
        tz_str = self.config.get("APP", "timezone", fallback="UTC")
        try:
            with FileLock(queue_file):
                if not queue_file.exists():
                    return
                with open(queue_file, "r") as f:
                    queue_data = json.load(f)
            if not queue_data:
                return

            for item_data in queue_data:
                file_path = item_data.get("file_path", "Unknown File")
                filename = os.path.basename(file_path)
                group_name = os.path.basename(os.path.dirname(file_path))

                widget = QueueItemWidget(
                    item_text=filename,
                    file_path=file_path,
                    skip_callback=self.handle_skip_request,
                    show_thumbnail=False,
                    group_name=group_name,
                    timezone_str=tz_str,
                )
                list_item = QListWidgetItem(self.download_queue_list)
                list_item.setSizeHint(widget.sizeHint())
                self.download_queue_list.addItem(list_item)
                self.download_queue_list.setItemWidget(list_item, widget)

        except TimeoutError:
            self.download_queue_list.addItem(
                "Could not read queue state: file is locked."
            )
        except Exception as e:
            logger.error(f"Error refreshing download queue display: {e}")

    def refresh_autocam_queue_display(self):
        """Reads and displays the autocam queue state."""
        queue_file = get_shared_data_path() / "autocam_queue_state.json"
        self.autocam_queue_list.clear()
        try:
            with FileLock(queue_file):
                if not queue_file.exists():
                    return
                with open(queue_file, "r") as f:
                    queue_data = json.load(f)
            if not queue_data:
                return

            for item in queue_data:
                group_name = item.get("group_name", "Unknown")
                status = item.get("status", "unknown")
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
        tz_str = self.config.get("APP", "timezone", fallback="UTC")
        try:
            with FileLock(queue_file):
                if not queue_file.exists():
                    return
                with open(queue_file, "r") as f:
                    queue_data = json.load(f)
            if not queue_data:
                return

            for task_type, item_path in queue_data:
                item_name = os.path.basename(item_path)
                display_text = f"Task: {task_type.capitalize()}, Item: {item_name}"

                file_to_skip = item_path if task_type == "convert" else None

                widget = QueueItemWidget(
                    item_text=display_text,
                    file_path=file_to_skip,
                    skip_callback=self.handle_skip_request if file_to_skip else None,
                    show_thumbnail=True,
                    timezone_str=tz_str,
                )
                list_item = QListWidgetItem(self.processing_queue_list)
                list_item.setSizeHint(widget.sizeHint())
                self.processing_queue_list.addItem(list_item)
                self.processing_queue_list.setItemWidget(list_item, widget)

                if not file_to_skip:
                    widget.skip_button.setEnabled(False)
                    widget.skip_button.setToolTip("Cannot skip a directory-level task.")

        except TimeoutError:
            self.processing_queue_list.addItem(
                "Could not read queue state: file is locked."
            )
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
                raise FileNotFoundError(
                    f"Could not find group directory for file: {file_path}"
                )

            dir_state = DirectoryState(group_dir)
            asyncio.run(dir_state.mark_file_as_skipped(file_path))

            QMessageBox.information(
                self,
                "Success",
                f"File marked to be skipped:\n{os.path.basename(file_path)}",
            )
            self.refresh_queue_displays()
        except Exception as e:
            logger.error(f"Error processing skip request for {file_path}: {e}")
            QMessageBox.critical(
                self, "Error", f"Failed to mark file as skipped:\n{str(e)}"
            )

    def refresh_skipped_files_display(self):
        """Scans for and displays all files marked as skipped."""
        self.skipped_list.clear()
        storage_path_str = self.config.get("STORAGE", "path", fallback=None)
        if not storage_path_str or not os.path.isdir(storage_path_str):
            return
        tz_str = self.config.get("APP", "timezone", fallback="UTC")

        try:
            for dirname in os.listdir(storage_path_str):
                group_dir_path = os.path.join(storage_path_str, dirname)
                if not os.path.isdir(group_dir_path):
                    continue

                dir_state = DirectoryState(group_dir_path)
                if not dir_state.files:
                    continue  # Skip if not a valid group dir or no files

                for file_obj in dir_state.files.values():
                    if file_obj.skip:
                        filename = os.path.basename(file_obj.file_path)
                        display_text = f"{filename} (Status: {file_obj.status})"
                        group_name = os.path.basename(
                            os.path.dirname(file_obj.file_path)
                        )

                        widget = QueueItemWidget(
                            item_text=display_text,
                            file_path=file_obj.file_path,
                            skip_callback=None,  # No "unskip" functionality for now
                            show_thumbnail=False,
                            group_name=group_name,
                            timezone_str=tz_str,
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
        storage_path_str = self.config.get("STORAGE", "path", fallback=None)
        if not storage_path_str or not os.path.isdir(storage_path_str):
            return
        tz_str = self.config.get("APP", "timezone", fallback="UTC")

        try:
            for dirname in os.listdir(storage_path_str):
                group_dir_path = os.path.join(storage_path_str, dirname)
                if not os.path.isdir(group_dir_path):
                    continue

                dir_state = DirectoryState(group_dir_path)
                if dir_state.status == "combined":
                    # Check if match info is already populated
                    match_info_path = os.path.join(group_dir_path, "match_info.ini")
                    match_info = MatchInfo.from_file(match_info_path)

                    if not all_fields_filled(match_info):
                        widget = MatchInfoItemWidget(
                            group_dir_path, self.save_match_info, timezone_str=tz_str
                        )
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
                start_time_offset=info_dict["start_time_offset"],
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

                with open(match_info_path, "w") as f:
                    config.write(f)

            QMessageBox.information(
                self,
                "Success",
                f"Match info saved for {os.path.basename(group_dir_path)}",
            )
            self.refresh_match_info_display()

        except TimeoutError:
            QMessageBox.critical(
                self, "Error", "Could not save match info: file is locked."
            )
        except Exception as e:
            logger.error(f"Error saving match info: {e}")
            QMessageBox.critical(self, "Error", f"Failed to save match info: {str(e)}")

    def update_queue_status(self, status):
        # This method is now deprecated.
        pass

    def check_pending_match_info(self):
        # This method is now deprecated and replaced by refresh_match_info_display.
        pass

    def refresh_connection_events_display(self):
        """Reads and displays camera connection timeframes."""
        self.connection_events_list.clear()
        storage_path_str = self.config.get("STORAGE", "path", fallback=None)
        if not storage_path_str or not os.path.isdir(storage_path_str):
            self.connection_events_list.addItem(
                "Storage path not configured or not found."
            )
            return

        state_file = Path(storage_path_str) / "camera_state.json"
        if not state_file.exists():
            self.connection_events_list.addItem("No connection history found.")
            return

        try:
            with FileLock(state_file):
                with open(state_file, "r") as f:
                    state_data = json.load(f)
        except (TimeoutError, Exception) as e:
            logger.error(f"Error reading camera state file: {e}")
            self.connection_events_list.addItem(f"Error reading state file: {e}")
            return

        connection_events = state_data.get("connection_events", [])
        if not connection_events:
            self.connection_events_list.addItem("No connection events recorded.")
            return

        # Parse events
        parsed_events = []
        for event in connection_events:
            t_str = event["event_datetime"]
            event_type = event["event_type"]
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

        tz_str = self.config.get("APP", "timezone", fallback="UTC")

        for start, end in reversed(timeframes):  # Show most recent first
            start_local = convert_utc_to_local(start, tz_str)
            start_str = start_local.strftime("%Y-%m-%d %H:%M:%S")

            if end:
                end_local = convert_utc_to_local(end, tz_str)
                end_str = end_local.strftime("%Y-%m-%d %H:%M:%S")

                # Find the corresponding disconnection event to get the message
                message = f"Disconnected: {end_str}"
                for event in connection_events:
                    if (
                        datetime.fromisoformat(event["event_datetime"]).astimezone(
                            pytz.utc
                        )
                        == end.astimezone(pytz.utc)
                        and event["event_type"] == "disconnected"
                    ):
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

    def fetch_teamsnap_info(self):
        """Fetch team information from TeamSnap using the provided credentials."""
        # Check if client ID and secret are provided
        client_id = self.teamsnap_client_id.text().strip()
        client_secret = self.teamsnap_client_secret.text().strip()

        if not client_id or not client_secret:
            QMessageBox.warning(
                self,
                "Missing Credentials",
                "Please enter your TeamSnap Client ID and Client Secret.",
            )
            return

        # Update status
        self.teamsnap_status_label.setText("Connecting to TeamSnap...")
        self.fetch_teamsnap_info_button.setEnabled(False)

        # Run in a separate thread to avoid freezing UI
        def teamsnap_thread():
            try:
                # Save client ID and secret to a temporary config
                temp_config = configparser.ConfigParser()
                temp_config.add_section("TEAMSNAP")
                temp_config.set("TEAMSNAP", "enabled", "true")
                temp_config.set("TEAMSNAP", "client_id", client_id)
                temp_config.set("TEAMSNAP", "client_secret", client_secret)

                # Import here to avoid circular imports
                from video_grouper.api_integrations.teamsnap import TeamSnapAPI

                # Initialize TeamSnap API
                teamsnap_api = TeamSnapAPI(temp_config)

                # Get access token
                success = teamsnap_api.get_access_token()

                if not success:

                    def show_error():
                        self.teamsnap_status_label.setText("Authentication failed")
                        self.fetch_teamsnap_info_button.setEnabled(True)
                        QMessageBox.critical(
                            self,
                            "Error",
                            "Failed to authenticate with TeamSnap. Please check your credentials.",
                        )

                    QTimer.singleShot(0, show_error)
                    return

                # Get team information
                teams = teamsnap_api.get_teams()

                if not teams:

                    def show_no_teams():
                        self.teamsnap_status_label.setText("No teams found")
                        self.fetch_teamsnap_info_button.setEnabled(True)
                        QMessageBox.warning(
                            self, "No Teams", "No teams found in your TeamSnap account."
                        )

                    QTimer.singleShot(0, show_no_teams)
                    return

                # Show team selection dialog
                def show_team_selection():
                    dialog = QDialog(self)
                    dialog.setWindowTitle("Select Teams")
                    dialog.resize(400, 300)
                    main_layout = QVBoxLayout(dialog)

                    main_layout.addWidget(
                        QLabel("Select the teams you want to enable:")
                    )

                    # Create a scrollable area for teams
                    scroll_area = QScrollArea()
                    scroll_area.setWidgetResizable(True)
                    scroll_content = QWidget()
                    scroll_layout = QVBoxLayout(scroll_content)

                    # Create checkboxes for each team
                    team_checkboxes = []
                    for team in teams:
                        team_name = team.get("name", "Unknown Team")
                        checkbox = QCheckBox(team_name)

                        # Check if this team is already configured
                        team_id = team.get("id")
                        for section in self.config.sections():
                            if (
                                section.startswith("TEAMSNAP.TEAM.")
                                and self.config.get(section, "team_id", fallback="")
                                == team_id
                            ):
                                checkbox.setChecked(
                                    self.config.getboolean(
                                        section, "enabled", fallback=True
                                    )
                                )
                                break

                        scroll_layout.addWidget(checkbox)
                        team_checkboxes.append((checkbox, team))

                    scroll_layout.addStretch(1)
                    scroll_area.setWidget(scroll_content)
                    main_layout.addWidget(scroll_area)

                    # Add buttons
                    buttons = QDialogButtonBox(
                        QDialogButtonBox.StandardButton.Ok
                        | QDialogButtonBox.StandardButton.Cancel
                    )
                    buttons.accepted.connect(dialog.accept)
                    buttons.rejected.connect(dialog.reject)
                    main_layout.addWidget(buttons)

                    # Show dialog and process results
                    if dialog.exec() == QDialog.DialogCode.Accepted:
                        # Save selected teams
                        selected_teams = [
                            (checkbox.isChecked(), team)
                            for checkbox, team in team_checkboxes
                        ]
                        self.save_teamsnap_teams(
                            teamsnap_api.access_token, selected_teams
                        )
                    else:
                        self.teamsnap_status_label.setText("Team selection canceled")
                        self.fetch_teamsnap_info_button.setEnabled(True)

                QTimer.singleShot(0, show_team_selection)

            except Exception as e:
                logger.error(f"Error fetching TeamSnap info: {e}")

                def show_error(err=e):
                    self.teamsnap_status_label.setText("Error")
                    self.fetch_teamsnap_info_button.setEnabled(True)
                    QMessageBox.critical(
                        self, "Error", f"Error fetching TeamSnap info: {str(err)}"
                    )

                QTimer.singleShot(0, show_error)

        # Start thread
        threading.Thread(target=teamsnap_thread, daemon=True).start()

    def save_teamsnap_teams(self, access_token, selected_teams):
        """Save the selected TeamSnap teams to the config."""
        try:
            # First, save the main TeamSnap credentials
            if "TEAMSNAP" not in self.config:
                self.config.add_section("TEAMSNAP")

            self.config["TEAMSNAP"]["enabled"] = "true"
            self.config["TEAMSNAP"]["client_id"] = (
                self.teamsnap_client_id.text().strip()
            )
            self.config["TEAMSNAP"]["client_secret"] = (
                self.teamsnap_client_secret.text().strip()
            )
            self.config["TEAMSNAP"]["access_token"] = access_token

            # Remove existing team configurations
            for section in list(self.config.sections()):
                if section.startswith("TEAMSNAP.TEAM."):
                    self.config.remove_section(section)

            # Add new team configurations
            enabled_teams = []
            for i, (enabled, team) in enumerate(selected_teams):
                section_name = f"TEAMSNAP.TEAM.{i + 1}"
                self.config.add_section(section_name)
                self.config[section_name]["enabled"] = str(enabled).lower()
                self.config[section_name]["team_id"] = team.get("id", "")
                self.config[section_name]["team_name"] = team.get(
                    "name", "Unknown Team"
                )

                if enabled:
                    enabled_teams.append(team.get("name", "Unknown Team"))

            # Save config
            with FileLock(self.config_path):
                with open(self.config_path, "w") as f:
                    self.config.write(f)

            # Update status
            if enabled_teams:
                self.teamsnap_status_label.setText(
                    f"Connected: {len(enabled_teams)} teams enabled"
                )
                QMessageBox.information(
                    self,
                    "Success",
                    f"Successfully configured {len(enabled_teams)} TeamSnap teams",
                )
            else:
                self.teamsnap_status_label.setText("Connected: No teams enabled")
                QMessageBox.information(
                    self,
                    "Success",
                    "TeamSnap credentials saved, but no teams are enabled",
                )

            self.fetch_teamsnap_info_button.setEnabled(True)

            # Reload the config to update UI
            self.load_settings_into_ui()

        except Exception as e:
            logger.error(f"Error saving TeamSnap teams: {e}")
            self.teamsnap_status_label.setText("Error saving teams")
            self.fetch_teamsnap_info_button.setEnabled(True)
            QMessageBox.critical(
                self, "Error", f"Error saving TeamSnap teams: {str(e)}"
            )

    def fetch_playmetrics_info(self):
        """Fetch team information from PlayMetrics using the provided credentials."""
        # Check if username and password are provided
        username = self.playmetrics_username.text().strip()
        password = self.playmetrics_password.text().strip()

        if not username or not password:
            QMessageBox.warning(
                self,
                "Missing Credentials",
                "Please enter your PlayMetrics username and password.",
            )
            return

        # Update status
        self.playmetrics_status_label.setText("Connecting to PlayMetrics...")
        self.fetch_playmetrics_info_button.setEnabled(False)

        # Run in a separate thread to avoid freezing UI
        def playmetrics_thread():
            try:
                # Save username and password to a temporary config
                temp_config = configparser.ConfigParser()
                temp_config.add_section("PLAYMETRICS")
                temp_config.set("PLAYMETRICS", "enabled", "true")
                temp_config.set("PLAYMETRICS", "username", username)
                temp_config.set("PLAYMETRICS", "password", password)

                # Import here to avoid circular imports
                from video_grouper.api_integrations.playmetrics import PlayMetricsAPI

                # Initialize PlayMetrics API
                playmetrics_api = PlayMetricsAPI()
                playmetrics_api.enabled = True
                playmetrics_api.username = username
                playmetrics_api.password = password

                # Login to PlayMetrics
                success = playmetrics_api.login()

                if not success:

                    def show_error():
                        self.playmetrics_status_label.setText("Authentication failed")
                        self.fetch_playmetrics_info_button.setEnabled(True)
                        QMessageBox.critical(
                            self,
                            "Error",
                            "Failed to authenticate with PlayMetrics. Please check your credentials.",
                        )

                    QTimer.singleShot(0, show_error)
                    return

                # Get available teams
                teams = playmetrics_api.get_available_teams()

                if not teams:

                    def show_no_teams():
                        self.playmetrics_status_label.setText("No teams found")
                        self.fetch_playmetrics_info_button.setEnabled(True)
                        QMessageBox.warning(
                            self,
                            "No Teams",
                            "Could not find any teams in your PlayMetrics account.",
                        )

                    QTimer.singleShot(0, show_no_teams)
                    return

                def show_team_selection():
                    # Create dialog for team selection
                    dialog = QDialog(self)
                    dialog.setWindowTitle("Select PlayMetrics Teams")
                    dialog_layout = QVBoxLayout()

                    # Add instructions
                    instructions = QLabel(
                        "Select the teams you want to enable for video processing:"
                    )
                    dialog_layout.addWidget(instructions)

                    # Create scroll area for teams
                    scroll = QScrollArea()
                    scroll.setWidgetResizable(True)
                    scroll_content = QWidget()
                    scroll_layout = QVBoxLayout(scroll_content)

                    # Create checkboxes for each team
                    team_checkboxes = []
                    for team in teams:
                        checkbox = QCheckBox(f"{team['name']}")
                        checkbox.setChecked(True)  # Default to checked
                        checkbox.setProperty("team_data", team)
                        team_checkboxes.append(checkbox)
                        scroll_layout.addWidget(checkbox)

                    scroll_content.setLayout(scroll_layout)
                    scroll.setWidget(scroll_content)
                    dialog_layout.addWidget(scroll)

                    # Add buttons
                    button_layout = QHBoxLayout()
                    ok_button = QPushButton("OK")
                    cancel_button = QPushButton("Cancel")

                    ok_button.clicked.connect(dialog.accept)
                    cancel_button.clicked.connect(dialog.reject)

                    button_layout.addWidget(ok_button)
                    button_layout.addWidget(cancel_button)
                    dialog_layout.addLayout(button_layout)

                    dialog.setLayout(dialog_layout)

                    # Show dialog and process result
                    if dialog.exec() == QDialog.DialogCode.Accepted:
                        # Get selected teams
                        selected_teams = []
                        for checkbox in team_checkboxes:
                            if checkbox.isChecked():
                                team_data = checkbox.property("team_data")
                                selected_teams.append(team_data)

                        # Save selected teams
                        self.save_playmetrics_teams(username, password, selected_teams)
                    else:
                        # User cancelled
                        self.playmetrics_status_label.setText(
                            "Team selection cancelled"
                        )
                        self.fetch_playmetrics_info_button.setEnabled(True)

                QTimer.singleShot(0, show_team_selection)

            except Exception as e:
                logger.error(f"Error fetching PlayMetrics info: {e}")

                def show_error(err=e):
                    self.playmetrics_status_label.setText("Error")
                    self.fetch_playmetrics_info_button.setEnabled(True)
                    QMessageBox.critical(
                        self, "Error", f"Error fetching PlayMetrics info: {str(err)}"
                    )

                QTimer.singleShot(0, show_error)

        # Start thread
        threading.Thread(target=playmetrics_thread, daemon=True).start()

    def save_playmetrics_teams(self, username, password, selected_teams):
        """Save the selected PlayMetrics teams to the config file."""
        try:
            # Update status
            self.playmetrics_status_label.setText("Saving team configurations...")

            # Remove existing PlayMetrics team configurations
            for section in list(self.config.sections()):
                if section.startswith("PLAYMETRICS.") and section != "PLAYMETRICS":
                    self.config.remove_section(section)

            # Add base PlayMetrics section if it doesn't exist
            if not self.config.has_section("PLAYMETRICS"):
                self.config.add_section("PLAYMETRICS")

            # Update base PlayMetrics section
            self.config["PLAYMETRICS"]["enabled"] = "true"
            self.config["PLAYMETRICS"]["username"] = username
            self.config["PLAYMETRICS"]["password"] = password

            # Add selected teams
            enabled_teams = []
            for team in selected_teams:
                team_name = team["name"]
                team_id = team["id"]
                calendar_url = team["calendar_url"]

                # Create a sanitized name for the section
                section_name = f"PLAYMETRICS.{team_name.replace(' ', '_')}"

                # Add section
                self.config.add_section(section_name)
                self.config[section_name]["enabled"] = "true"
                self.config[section_name]["team_name"] = team_name
                self.config[section_name]["team_id"] = team_id
                if calendar_url:
                    self.config[section_name]["calendar_url"] = calendar_url

                # Copy credentials
                self.config[section_name]["username"] = username
                self.config[section_name]["password"] = password

                enabled_teams.append(team_name)

            # Save config
            with FileLock(self.config_path):
                with open(self.config_path, "w") as f:
                    self.config.write(f)

            # Update status
            if enabled_teams:
                self.playmetrics_status_label.setText(
                    f"Connected: {len(enabled_teams)} teams enabled"
                )
                QMessageBox.information(
                    self,
                    "Success",
                    f"Successfully configured {len(enabled_teams)} PlayMetrics teams",
                )
            else:
                self.playmetrics_status_label.setText("Connected: No teams enabled")
                QMessageBox.information(
                    self,
                    "Success",
                    "PlayMetrics credentials saved, but no teams are enabled",
                )

            self.fetch_playmetrics_info_button.setEnabled(True)

            # Reload the config to update UI
            self.load_settings_into_ui()

        except Exception as e:
            logger.error(f"Error saving PlayMetrics teams: {e}")
            self.playmetrics_status_label.setText("Error saving teams")
            self.fetch_playmetrics_info_button.setEnabled(True)
            QMessageBox.critical(
                self, "Error", f"Error saving PlayMetrics teams: {str(e)}"
            )

    # Methods for managing multiple TeamSnap configurations
    def add_teamsnap_config(self):
        """Add a new TeamSnap configuration."""
        name, ok = QInputDialog.getText(
            self, "Add TeamSnap Config", "Enter configuration name:"
        )
        if ok and name:
            new_config = TeamSnapTeamConfig(team_name=name, enabled=True)
            self.config.teamsnap_teams.append(new_config)
            self.teamsnap_configs_list.addItem(name)
            self.save_settings()

    def remove_teamsnap_config(self):
        """Remove the selected TeamSnap configuration."""
        selected_items = self.teamsnap_configs_list.selectedItems()
        if not selected_items:
            return

        item = selected_items[0]
        name = item.text()

        reply = QMessageBox.question(
            self, "Remove Config", f"Are you sure you want to remove '{name}'?"
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.config.teamsnap_teams = [
                t for t in self.config.teamsnap_teams if t.team_name != name
            ]
            self.teamsnap_configs_list.takeItem(self.teamsnap_configs_list.row(item))
            self.save_settings()

    def load_selected_teamsnap_config(self):
        """Load the selected TeamSnap configuration into the form."""
        selected_items = self.teamsnap_configs_list.selectedItems()
        if not selected_items:
            return

        item = selected_items[0]
        name = item.text()

        team_config = None
        if name == "Default":
            team_config = self.config.teamsnap
        else:
            for t in self.config.teamsnap_teams:
                if t.team_name == name:
                    team_config = t
                    break

        if team_config:
            self.teamsnap_config_name.setText(name)
            self.teamsnap_enabled.setChecked(team_config.enabled)
            self.teamsnap_client_id.setText(team_config.client_id)
            self.teamsnap_client_secret.setText(team_config.client_secret)
            self.teamsnap_access_token.setText(team_config.access_token)
            self.teamsnap_team_id.setText(team_config.team_id)
            self.teamsnap_team_name.setText(team_config.my_team_name)

    def save_teamsnap_config(self):
        """Save the currently edited TeamSnap configuration."""
        name = self.teamsnap_config_name.text()
        if not name:
            QMessageBox.warning(self, "Warning", "Configuration name cannot be empty.")
            return

        team_config = None
        if name == "Default":
            team_config = self.config.teamsnap
        else:
            for t in self.config.teamsnap_teams:
                if t.team_name == name:
                    team_config = t
                    break

        if not team_config:
            # This is a new team, create it
            team_config = TeamSnapTeamConfig(team_name=name)
            self.config.teamsnap_teams.append(team_config)

        team_config.enabled = self.teamsnap_enabled.isChecked()
        team_config.client_id = self.teamsnap_client_id.text()
        team_config.client_secret = self.teamsnap_client_secret.text()
        team_config.access_token = self.teamsnap_access_token.text()
        team_config.team_id = self.teamsnap_team_id.text()
        team_config.my_team_name = self.teamsnap_team_name.text()

        self.save_settings()
        QMessageBox.information(
            self, "Success", f"TeamSnap configuration '{name}' saved."
        )

    # Methods for managing multiple PlayMetrics configurations
    def add_playmetrics_config(self):
        """Add a new PlayMetrics configuration."""
        name, ok = QInputDialog.getText(
            self, "Add PlayMetrics Config", "Enter configuration name:"
        )
        if ok and name:
            new_config = PlayMetricsTeamConfig(team_name=name, enabled=True)
            self.config.playmetrics_teams.append(new_config)
            self.playmetrics_configs_list.addItem(name)
            self.save_settings()

    def remove_playmetrics_config(self):
        """Remove the selected PlayMetrics configuration."""
        selected_items = self.playmetrics_configs_list.selectedItems()
        if not selected_items:
            return

        item = selected_items[0]
        name = item.text()

        reply = QMessageBox.question(
            self, "Remove Config", f"Are you sure you want to remove '{name}'?"
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.config.playmetrics_teams = [
                t for t in self.config.playmetrics_teams if t.team_name != name
            ]
            self.playmetrics_configs_list.takeItem(
                self.playmetrics_configs_list.row(item)
            )
            self.save_settings()

    def load_selected_playmetrics_config(self):
        """Load the selected PlayMetrics configuration into the form."""
        selected_items = self.playmetrics_configs_list.selectedItems()
        if not selected_items:
            return

        item = selected_items[0]
        name = item.text()

        team_config = None
        if name == "Default":
            team_config = self.config.playmetrics
        else:
            for t in self.config.playmetrics_teams:
                if t.team_name == name:
                    team_config = t
                    break

        if team_config:
            self.playmetrics_config_name.setText(name)
            self.playmetrics_enabled.setChecked(team_config.enabled)
            self.playmetrics_username.setText(team_config.username)
            self.playmetrics_password.setText(team_config.password)
            self.playmetrics_team_id.setText(team_config.team_id)
            self.playmetrics_team_name.setText(team_config.team_name)

            # Update status label
            team_name = self.config.get(team_config.team_name, "team_name", fallback="")
            if team_name:
                self.playmetrics_status_label.setText(f"Connected: {team_name}")
            else:
                self.playmetrics_status_label.setText("Not connected")

    def save_playmetrics_config(self):
        """Save the currently edited PlayMetrics configuration."""
        name = self.playmetrics_config_name.text()
        if not name:
            QMessageBox.warning(self, "Warning", "Configuration name cannot be empty.")
            return

        team_config = None
        if name == "Default":
            team_config = self.config.playmetrics
        else:
            for t in self.config.playmetrics_teams:
                if t.team_name == name:
                    team_config = t
                    break

        if not team_config:
            # This is a new team, create it
            team_config = PlayMetricsTeamConfig(team_name=name)
            self.config.playmetrics_teams.append(team_config)

        team_config.enabled = self.playmetrics_enabled.isChecked()
        team_config.username = self.playmetrics_username.text()
        team_config.password = self.playmetrics_password.text()
        team_config.team_id = self.playmetrics_team_id.text()
        team_config.team_name = self.playmetrics_team_name.text()

        self.save_settings()
        QMessageBox.information(
            self, "Success", f"PlayMetrics configuration '{name}' saved."
        )

    def authenticate_with_google(self):
        """Authenticate with Google and save the updated token."""

        def auth_thread():
            try:
                # This would normally open a browser window for OAuth
                # For now, we just simulate the authentication
                asyncio.set_event_loop(asyncio.new_event_loop())
                auth_result = asyncio.get_event_loop().run_until_complete(
                    GoogleAuthProvider.authenticate()
                )

                if auth_result:
                    # Update UI in the main thread
                    def update_ui():
                        email = auth_result.get("email", "unknown")
                        self.google_auth_status.setText(f"Signed in as: {email}")
                        # Store the token in the password field for use during sync
                        self.cloud_password.setText(auth_result.get("access_token", ""))
                        QMessageBox.information(
                            self, "Success", "Successfully signed in with Google"
                        )
                        # Update UI state
                        self.update_cloud_sync_ui_state()

                    QApplication.instance().postEvent(
                        self, QTimer.singleShot(0, update_ui)
                    )
                else:

                    def show_error():
                        QMessageBox.warning(
                            self,
                            "Authentication Failed",
                            "Failed to authenticate with Google. Please try again.",
                        )

                    QApplication.instance().postEvent(
                        self, QTimer.singleShot(0, show_error)
                    )
            except Exception as e:
                logger.error(f"Error during Google authentication: {e}")

                def show_error(err=e):
                    QMessageBox.critical(
                        self,
                        "Error",
                        f"An error occurred during authentication: {str(err)}",
                    )

                QApplication.instance().postEvent(
                    self, QTimer.singleShot(0, show_error)
                )

        # Start authentication in a separate thread to avoid blocking the UI
        threading.Thread(target=auth_thread, daemon=True).start()

    def sync_to_cloud(self):
        """Sync configuration to the cloud."""
        if (
            not self.cloud_username.text()
            and "Signed in as:" not in self.google_auth_status.text()
        ):
            QMessageBox.warning(
                self,
                "Authentication Required",
                "Please sign in with your username/password or Google account.",
            )
            return

        # Save settings before syncing
        self.save_settings()

        def sync_thread():
            try:
                # Get endpoint URL from config
                endpoint_url = self.config.get(
                    "CLOUD", "endpoint_url", fallback="https://example.com/api/sync"
                )

                # Initialize cloud sync with the endpoint URL
                cloud_sync = CloudSync(endpoint_url)

                # Set credentials based on authentication method
                if self.cloud_username.text() and self.cloud_password.text():
                    cloud_sync.set_credentials(
                        self.cloud_username.text(), self.cloud_password.text()
                    )
                else:
                    # Extract email from Google auth status
                    email = self.google_auth_status.text().replace("Signed in as: ", "")
                    # For Google auth, we use the email as username and the token stored in cloud_password
                    cloud_sync.set_credentials(email, self.cloud_password.text())

                # Upload the config
                asyncio.set_event_loop(asyncio.new_event_loop())
                success = asyncio.get_event_loop().run_until_complete(
                    cloud_sync.upload_config(self.config_path)
                )

                if success:

                    def show_success():
                        QMessageBox.information(
                            self,
                            "Success",
                            "Configuration successfully synced to cloud.",
                        )

                    QApplication.instance().postEvent(
                        self, QTimer.singleShot(0, show_success)
                    )
                else:

                    def show_error():
                        QMessageBox.warning(
                            self,
                            "Sync Failed",
                            "Failed to sync configuration to cloud. Please check your credentials and try again.",
                        )
                        # Update UI state in case authentication is no longer valid
                        self.update_cloud_sync_ui_state()

                    QApplication.instance().postEvent(
                        self, QTimer.singleShot(0, show_error)
                    )
            except Exception as e:
                logger.error(f"Error during cloud sync: {e}")

                def show_error(err=e):
                    QMessageBox.critical(
                        self,
                        "Error",
                        f"An error occurred during cloud sync: {str(err)}",
                    )
                    # Update UI state in case of error
                    self.update_cloud_sync_ui_state()

                QApplication.instance().postEvent(
                    self, QTimer.singleShot(0, show_error)
                )

        # Start sync in a separate thread to avoid blocking the UI
        threading.Thread(target=sync_thread, daemon=True).start()

        # Show a message that sync is in progress
        QMessageBox.information(
            self,
            "Sync Started",
            "Cloud sync has started. You will be notified when it completes.",
        )

    def update_cloud_sync_ui_state(self):
        """Update the state of cloud sync UI elements based on authentication status."""
        try:
            is_authenticated = False

            # Check if authenticated with username/password
            if self.cloud_username.text() and self.cloud_password.text():
                is_authenticated = True
                self.signed_in_label.setText(
                    f"Signed in as: {self.cloud_username.text()}"
                )
                self.auth_widget.setVisible(False)
                self.signed_in_widget.setVisible(True)
                self.google_auth_button.setEnabled(False)

            # Check if authenticated with Google (based on status label)
            elif "Signed in as:" in self.google_auth_status.text():
                is_authenticated = True
                email = self.google_auth_status.text().replace("Signed in as: ", "")
                self.signed_in_label.setText(f"Signed in as: {email}")
                self.auth_widget.setVisible(False)
                self.signed_in_widget.setVisible(True)
                self.google_auth_button.setEnabled(False)
            else:
                # Not authenticated
                self.auth_widget.setVisible(True)
                self.signed_in_widget.setVisible(False)
                self.google_auth_button.setEnabled(True)

            # Enable sync button only if user is authenticated
            self.sync_to_cloud_button.setEnabled(is_authenticated)
        except Exception as e:
            logger.error(f"Error updating cloud sync UI state: {e}")

    def sign_out_cloud(self):
        """Sign out from cloud sync."""
        # Clear credentials
        self.cloud_username.clear()
        self.cloud_password.clear()
        self.google_auth_status.setText("Not signed in")

        # Update UI state
        self.auth_widget.setVisible(True)
        self.signed_in_widget.setVisible(False)
        self.google_auth_button.setEnabled(True)
        self.sync_to_cloud_button.setEnabled(False)

        QMessageBox.information(self, "Signed Out", "You have been signed out.")


def main():
    """Main entry point for the standalone configuration UI."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stdout,
    )
    logger.info("Running Configuration UI in standalone mode")

    app = QApplication(sys.argv)

    # Set the application icon
    script_dir = os.path.dirname(os.path.abspath(__file__))
    icon_path = os.path.join(script_dir, "..", "icon.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # Config path is now handled by the window itself
    window = ConfigWindow()
    window.show()
    app.setQuitOnLastWindowClosed(True)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
