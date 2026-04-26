"""Onboarding setup wizard for Soccer-Cam.

Guides first-time users through configuring storage, camera, YouTube,
NTFY, and Team Tech Tools.  Two paths: TTT (auto-configured) and Manual.
"""

import asyncio
import logging
import os
import platform
import secrets
import socket
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QIcon

from video_grouper.utils.config import (
    create_default_config,
    save_config,
    load_config,
)
from video_grouper.utils.paths import get_shared_data_path
from video_grouper.utils.youtube_upload import (
    authenticate_youtube_embedded,
)

logger = logging.getLogger(__name__)

# TTT infrastructure defaults (not secrets -- Supabase anon keys are public).
# Values are injected at build time via _ttt_config.py (generated from
# environment variables by the build script).  Fall back to production
# defaults when running from source or when the generated file is absent.
_TTT_DEFAULT_SUPABASE_URL = "https://zmuwmngqqiaectpcqlfj.supabase.co"
_TTT_DEFAULT_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inpt"
    "dXdtbmdxcWlhZWN0cGNxbGZqIiwicm9sZSI6"
    "ImFub24iLCJpYXQiOjE3NjU1MDE1MDksImV4"
    "cCI6MjA4MTA3NzUwOX0.UzAKgFWmXSFSN7uu"
    "JsmCXRR5c_0oSHyFjJYeBxbmzmY"
)
_TTT_DEFAULT_API_BASE_URL = "https://team-tech-tools.vercel.app"

try:
    from video_grouper.utils._ttt_config import (
        TTT_SUPABASE_URL,
        TTT_ANON_KEY,
        TTT_API_BASE_URL,
    )
except ImportError:
    TTT_SUPABASE_URL = _TTT_DEFAULT_SUPABASE_URL
    TTT_ANON_KEY = _TTT_DEFAULT_ANON_KEY
    TTT_API_BASE_URL = _TTT_DEFAULT_API_BASE_URL


# HTML served at /callback to extract the token from the URL hash fragment.
# Supabase returns tokens as a hash fragment (e.g. #access_token=...),
# which the browser never sends to the server.  This page uses JS to
# parse the fragment and forward the token to /receive-token.
_OAUTH_CALLBACK_HTML = """\
<!DOCTYPE html>
<html>
<head><title>Soccer-Cam Sign In</title></head>
<body>
<h2>Completing sign-in...</h2>
<p id="status">Processing authentication response...</p>
<script>
(function() {
    var hash = window.location.hash.substring(1);
    if (!hash) {
        document.getElementById('status').textContent =
            'Error: No authentication data received.';
        return;
    }
    var params = new URLSearchParams(hash);
    var accessToken = params.get('access_token');
    var error = params.get('error');
    var errorDesc = params.get('error_description');
    var url = 'http://localhost:{{PORT}}/receive-token?';
    if (accessToken) {
        url += 'access_token=' + encodeURIComponent(accessToken);
    } else {
        url += 'error=' + encodeURIComponent(error || 'unknown');
        if (errorDesc) {
            url += '&error_description=' + encodeURIComponent(errorDesc);
        }
    }
    window.location.replace(url);
})();
</script>
</body>
</html>
"""


class OnboardingWizard(QDialog):
    """First-run setup wizard for Soccer-Cam."""

    # Thread-safe signals for background task results
    _ttt_sign_in_succeeded = pyqtSignal()
    _ttt_sign_in_failed = pyqtSignal(str)
    _ttt_oauth_succeeded = pyqtSignal()
    _ttt_oauth_failed = pyqtSignal(str)
    _yt_auth_finished = pyqtSignal(bool, str)
    _run_on_main = pyqtSignal(object)  # emit a callable to run on main thread

    # Page indices for TTT path
    PAGE_WELCOME = 0
    PAGE_PATH_CHOICE = 1
    # TTT pages
    PAGE_TTT_SIGNIN = 2
    PAGE_TTT_RESTORE = 3
    # Shared pages (both paths use these, but at different stack indices)
    PAGE_STORAGE = 4
    PAGE_CAMERA = 5
    PAGE_VIDEO_PROCESSING = 6
    PAGE_YOUTUBE = 7
    PAGE_NTFY = 8
    # Manual-only page
    PAGE_MANUAL_TTT = 9
    # Integration pages
    PAGE_PLAYMETRICS = 10
    PAGE_TEAMSNAP = 11
    PAGE_SUMMARY = 12
    # TTT machine setup (inserted after camera in TTT path)
    PAGE_MACHINE_SETUP = 13

    def __init__(self, config_path: Path, parent=None):
        super().__init__(parent)
        self.config_path = config_path
        self.setWindowTitle("Soccer-Cam Setup")
        self.setMinimumSize(750, 550)

        # Set window icon
        script_dir = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(script_dir, "..", "icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        # Wizard state
        self._mode: Optional[str] = None  # "ttt" or "manual"
        # Default storage: %LOCALAPPDATA%/SoccerCam (portable across machines).
        # Falls back to get_shared_data_path() only for dev/non-installed runs.
        _localappdata = os.environ.get("LOCALAPPDATA", "")
        if _localappdata:
            self._storage_path = str(Path(_localappdata) / "SoccerCam")
        else:
            self._storage_path = str(get_shared_data_path())
        self._camera_ip = ""
        self._camera_username = "admin"
        self._camera_password = ""
        self._camera_type = "reolink"
        self._camera_configured = False
        self._camera_settings_applied = False
        self._camera_settings_results: list[dict] = []
        self._camera_serial = ""
        self._camera_factory_defaults = False
        self._youtube_enabled = False
        self._youtube_authenticated = False
        self._youtube_playlist_map: dict[str, str] = {}
        self._gcp_project_id: Optional[str] = None
        self._ntfy_enabled = False
        self._ntfy_topic = ""
        self._ntfy_server_url = "https://ntfy.sh"
        self._ttt_enabled = False
        self._ttt_email = ""
        self._ttt_password = ""
        self._ttt_client = None
        self._ttt_teams = []
        self._ttt_device_config: Optional[dict] = None
        self._machine_id = ""
        self._machine_name = ""
        self._other_machines: list[dict] = []
        self._machine_setup_done = False
        self._ttt_sign_in_error = ""
        self._ttt_schedule_providers: dict[str, list[dict]] = {}

        # Video processing state (set properly in _build_video_processing_page)
        self._video_processor_type = "none"
        self._autocam_path = ""

        # PlayMetrics / TeamSnap state
        self._playmetrics_config: dict = {
            "username": "",
            "password": "",
            "refresh_token": "",
            # role_id_by_team_id maps team_id -> the PlayMetrics role under
            # which that team was discovered. Populated from TTT's connect
            # probe so the final create_schedule_provider call can send the
            # correct role-scoped credentials shape.
            "role_id_by_team_id": {},
            "teams": [],
        }
        self._teamsnap_config: dict = {
            "client_id": "",
            "client_secret": "",
            "access_token": "",
            "teams": [],
        }

        # Connect thread-safe signals for background task callbacks
        self._ttt_sign_in_succeeded.connect(self._on_ttt_sign_in_success)
        self._ttt_sign_in_failed.connect(self._on_ttt_sign_in_error)
        self._ttt_oauth_succeeded.connect(self._on_ttt_oauth_success)
        self._ttt_oauth_failed.connect(self._on_ttt_oauth_error)
        self._yt_auth_finished.connect(self._on_yt_auth_finished)
        self._run_on_main.connect(lambda fn: fn())

        # OAuth callback server (kept as instance var so we can shut it down)
        self._oauth_server: Optional[HTTPServer] = None

        # Navigation history (for Back button across paths)
        self._nav_history: list[int] = []

        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Main content area
        self._stack = QStackedWidget()
        layout.addWidget(self._stack, 1)

        # Build all pages
        self._stack.addWidget(self._build_welcome_page())  # 0
        self._stack.addWidget(self._build_path_choice_page())  # 1
        self._stack.addWidget(self._build_ttt_signin_page())  # 2
        self._stack.addWidget(self._build_ttt_restore_page())  # 3
        self._stack.addWidget(self._build_storage_page())  # 4
        self._stack.addWidget(self._build_camera_page())  # 5
        self._stack.addWidget(self._build_video_processing_page())  # 6
        self._stack.addWidget(self._build_youtube_page())  # 7
        self._stack.addWidget(self._build_ntfy_page())  # 8
        self._stack.addWidget(self._build_manual_ttt_page())  # 9
        self._stack.addWidget(self._build_playmetrics_page())  # 10
        self._stack.addWidget(self._build_teamsnap_page())  # 11
        self._stack.addWidget(self._build_summary_page())  # 12
        self._stack.addWidget(self._build_machine_setup_page())  # 13

        # Navigation bar
        nav_bar = QWidget()
        nav_layout = QHBoxLayout(nav_bar)
        nav_layout.setContentsMargins(16, 8, 16, 16)

        self._back_btn = QPushButton("Back")
        self._back_btn.clicked.connect(self._go_back)
        nav_layout.addWidget(self._back_btn)

        nav_layout.addStretch()

        self._step_label = QLabel()
        nav_layout.addWidget(self._step_label)

        nav_layout.addStretch()

        self._skip_btn = QPushButton("Skip")
        self._skip_btn.clicked.connect(self._skip_step)
        nav_layout.addWidget(self._skip_btn)

        self._next_btn = QPushButton("Next")
        self._next_btn.setDefault(True)
        self._next_btn.clicked.connect(self._go_next)
        nav_layout.addWidget(self._next_btn)

        layout.addWidget(nav_bar)

        self._stack.setCurrentIndex(self.PAGE_WELCOME)
        self._update_nav()

    # ------------------------------------------------------------------
    # Page builders
    # ------------------------------------------------------------------

    def _build_welcome_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 40, 40, 20)

        title = QLabel("Welcome to Soccer-Cam")
        title.setStyleSheet("font-size: 24px; font-weight: bold;")
        layout.addWidget(title)

        layout.addSpacing(20)

        desc = QLabel(
            "Soccer-Cam automatically records, processes, and uploads your "
            "team's game videos. This wizard will help you set up the key "
            "integrations:\n\n"
            "  - Camera connection for recording\n"
            "  - YouTube for automatic video uploads\n"
            "  - Push notifications to identify game times\n"
            "  - Team Tech Tools for schedule sync and clip sharing\n\n"
            "You can skip any step and configure it later from Settings."
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        layout.addStretch()
        return page

    def _build_path_choice_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 40, 40, 20)

        title = QLabel("How would you like to set up?")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        layout.addSpacing(20)

        # TTT button
        ttt_group = QGroupBox()
        ttt_layout = QVBoxLayout(ttt_group)
        ttt_btn = QPushButton("Sign in with Team Tech Tools (Recommended)")
        ttt_btn.setMinimumHeight(40)
        ttt_btn.clicked.connect(lambda: self._choose_path("ttt"))
        ttt_layout.addWidget(ttt_btn)
        ttt_desc = QLabel(
            "Get automatic setup with your team's configuration. "
            "If you've set this up before, your settings will be restored."
        )
        ttt_desc.setWordWrap(True)
        ttt_layout.addWidget(ttt_desc)
        layout.addWidget(ttt_group)

        layout.addSpacing(10)

        # Manual button
        manual_group = QGroupBox()
        manual_layout = QVBoxLayout(manual_group)
        manual_btn = QPushButton("Manual Setup")
        manual_btn.setMinimumHeight(40)
        manual_btn.clicked.connect(lambda: self._choose_path("manual"))
        manual_layout.addWidget(manual_btn)
        manual_desc = QLabel(
            "Configure everything yourself step by step. "
            "You can connect Team Tech Tools later."
        )
        manual_desc.setWordWrap(True)
        manual_layout.addWidget(manual_desc)
        layout.addWidget(manual_group)

        layout.addStretch()
        return page

    def _build_ttt_signin_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 30, 40, 20)

        title = QLabel("Sign in to Team Tech Tools")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        layout.addSpacing(10)

        # --- OAuth provider buttons ---
        self._oauth_providers = {
            "google": ("Sign in with Google", "#4285F4", "#3367D6"),
            "discord": ("Sign in with Discord", "#5865F2", "#4752C4"),
            "apple": ("Sign in with Apple", "#000000", "#333333"),
        }
        self._oauth_btns: dict[str, QPushButton] = {}
        for provider, (label, bg, hover_bg) in self._oauth_providers.items():
            btn = QPushButton(label)
            btn.setMinimumHeight(40)
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {bg}; color: white; "
                f"font-weight: bold; border-radius: 4px; padding: 8px 16px; }}"
                f"QPushButton:hover {{ background-color: {hover_bg}; }}"
                f"QPushButton:disabled {{ background-color: #a0a0a0; }}"
            )
            btn.clicked.connect(lambda checked, p=provider: self._sign_in_ttt_oauth(p))
            layout.addWidget(btn)
            self._oauth_btns[provider] = btn

        layout.addSpacing(10)

        # Separator
        separator_label = QLabel("-- or sign in with email --")
        separator_label.setStyleSheet("color: #888; padding: 4px 0;")
        layout.addWidget(separator_label)

        layout.addSpacing(10)

        # --- Email/password form ---
        form = QFormLayout()
        self._ttt_email_input = QLineEdit()
        self._ttt_email_input.setPlaceholderText("your@email.com")
        form.addRow("Email:", self._ttt_email_input)

        self._ttt_password_input = QLineEdit()
        self._ttt_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Password:", self._ttt_password_input)

        layout.addLayout(form)

        # Sign in button + status
        btn_layout = QHBoxLayout()
        self._ttt_signin_btn = QPushButton("Sign In")
        self._ttt_signin_btn.clicked.connect(self._sign_in_ttt)
        btn_layout.addWidget(self._ttt_signin_btn)

        self._ttt_signin_status = QLabel("")
        btn_layout.addWidget(self._ttt_signin_status, 1)
        layout.addLayout(btn_layout)

        layout.addSpacing(10)

        # Sign up link
        signup_label = QLabel(
            "Don't have an account? "
            '<a href="https://team-tech-tools.vercel.app/signup">'
            "Sign up for Team Tech Tools</a>"
        )
        signup_label.setOpenExternalLinks(True)
        layout.addWidget(signup_label)

        layout.addStretch()
        return page

    def _build_ttt_restore_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 30, 40, 20)

        title = QLabel("Existing Configuration Found")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        layout.addSpacing(10)

        self._restore_details = QLabel("Loading...")
        self._restore_details.setWordWrap(True)
        layout.addWidget(self._restore_details)

        self._restore_missing = QLabel("")
        self._restore_missing.setWordWrap(True)
        self._restore_missing.setStyleSheet("color: gray; font-style: italic;")
        self._restore_missing.setVisible(False)
        layout.addWidget(self._restore_missing)

        layout.addSpacing(20)

        btn_layout = QHBoxLayout()
        restore_btn = QPushButton("Restore Settings")
        restore_btn.setMinimumHeight(36)
        restore_btn.clicked.connect(self._restore_ttt_config)
        btn_layout.addWidget(restore_btn)

        fresh_btn = QPushButton("Start Fresh")
        fresh_btn.clicked.connect(self._start_fresh_ttt)
        btn_layout.addWidget(fresh_btn)
        layout.addLayout(btn_layout)

        layout.addStretch()
        return page

    def _build_storage_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 30, 40, 20)

        title = QLabel("Storage Location")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        layout.addSpacing(10)

        desc = QLabel(
            "Choose where Soccer-Cam stores configuration, logs, and "
            "downloaded video files. The default location works for most users."
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        layout.addSpacing(10)

        form = QFormLayout()
        path_layout = QHBoxLayout()
        self._storage_path_input = QLineEdit(self._storage_path)
        path_layout.addWidget(self._storage_path_input, 1)

        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_storage)
        path_layout.addWidget(browse_btn)

        form.addRow("Storage Path:", path_layout)
        layout.addLayout(form)

        layout.addStretch()
        return page

    def _build_camera_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 30, 40, 20)

        title = QLabel("Camera Setup")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        layout.addSpacing(10)

        # ── Phase A: Connection ──────────────────────────────────────
        self._cam_phase_a = QWidget()
        phase_a_layout = QVBoxLayout(self._cam_phase_a)
        phase_a_layout.setContentsMargins(0, 0, 0, 0)

        desc = QLabel(
            "Connect to your camera to configure it for 24/7 recording. "
            "If you don't have your camera connected yet, skip this step."
        )
        desc.setWordWrap(True)
        phase_a_layout.addWidget(desc)

        phase_a_layout.addSpacing(10)

        form = QFormLayout()
        self._camera_type_combo = QComboBox()
        self._camera_type_combo.addItems(["reolink", "dahua"])
        form.addRow("Camera Type:", self._camera_type_combo)

        self._camera_ip_input = QLineEdit()
        self._camera_ip_input.setPlaceholderText(self._get_gateway_placeholder())
        form.addRow("IP Address:", self._camera_ip_input)

        self._camera_user_input = QLineEdit("admin")
        form.addRow("Username:", self._camera_user_input)

        self._camera_pass_input = QLineEdit()
        self._camera_pass_input.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Password:", self._camera_pass_input)
        phase_a_layout.addLayout(form)

        btn_layout = QHBoxLayout()
        self._camera_test_btn = QPushButton("Connect")
        self._camera_test_btn.clicked.connect(self._test_camera)
        btn_layout.addWidget(self._camera_test_btn)

        self._camera_test_status = QLabel("")
        btn_layout.addWidget(self._camera_test_status, 1)
        phase_a_layout.addLayout(btn_layout)

        # Device info (hidden until connected)
        self._cam_device_info = QLabel("")
        self._cam_device_info.setStyleSheet("color: #555;")
        self._cam_device_info.setVisible(False)
        phase_a_layout.addWidget(self._cam_device_info)

        layout.addWidget(self._cam_phase_a)

        # ── Phase B: Password ────────────────────────────────────────
        self._cam_phase_b = QGroupBox("Camera Password")
        phase_b_layout = QVBoxLayout(self._cam_phase_b)

        self._cam_pass_desc = QLabel("")
        self._cam_pass_desc.setWordWrap(True)
        phase_b_layout.addWidget(self._cam_pass_desc)

        pass_form = QFormLayout()
        self._cam_new_pass = QLineEdit()
        self._cam_new_pass.setEchoMode(QLineEdit.EchoMode.Password)
        pass_form.addRow("New Password:", self._cam_new_pass)

        self._cam_confirm_pass = QLineEdit()
        self._cam_confirm_pass.setEchoMode(QLineEdit.EchoMode.Password)
        pass_form.addRow("Confirm:", self._cam_confirm_pass)
        phase_b_layout.addLayout(pass_form)

        pass_btn_layout = QHBoxLayout()
        self._cam_set_pass_btn = QPushButton("Set Password")
        self._cam_set_pass_btn.clicked.connect(self._set_camera_password)
        pass_btn_layout.addWidget(self._cam_set_pass_btn)
        self._cam_pass_status = QLabel("")
        pass_btn_layout.addWidget(self._cam_pass_status, 1)
        phase_b_layout.addLayout(pass_btn_layout)

        self._cam_phase_b.setVisible(False)
        layout.addWidget(self._cam_phase_b)

        # ── Phase C: Configuration ───────────────────────────────────
        self._cam_phase_c = QGroupBox("Camera Configuration")
        phase_c_layout = QVBoxLayout(self._cam_phase_c)

        self._cam_config_table = QTableWidget(0, 3)
        self._cam_config_table.setHorizontalHeaderLabels(
            ["Setting", "Current", "Proposed"]
        )
        self._cam_config_table.horizontalHeader().setStretchLastSection(True)
        self._cam_config_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self._cam_config_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._cam_config_table.verticalHeader().setVisible(False)
        phase_c_layout.addWidget(self._cam_config_table)

        config_btn_layout = QHBoxLayout()
        self._cam_apply_btn = QPushButton("Apply Optimal Settings")
        self._cam_apply_btn.clicked.connect(self._apply_camera_settings)
        config_btn_layout.addWidget(self._cam_apply_btn)
        self._cam_config_status = QLabel("")
        config_btn_layout.addWidget(self._cam_config_status, 1)
        phase_c_layout.addLayout(config_btn_layout)

        self._cam_phase_c.setVisible(False)
        layout.addWidget(self._cam_phase_c)

        layout.addStretch()
        return page

    def _build_video_processing_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 30, 40, 20)

        title = QLabel("Video Processing")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        layout.addSpacing(10)

        desc = QLabel(
            "Select which video processing to apply after recording. "
            "These run automatically on each game before uploading to YouTube."
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        layout.addSpacing(15)

        # Auto-detect AutoCam installation
        autocam_path = os.path.join(
            os.environ.get("LOCALAPPDATA", ""), "Programs", "Autocam", "GUI.exe"
        )
        autocam_installed = os.path.exists(autocam_path)
        self._autocam_path = autocam_path if autocam_installed else ""

        # Radio buttons
        self._vp_btn_group = QButtonGroup(page)

        self._vp_autocam_radio = QRadioButton(
            "AutoCam \u2014 AI camera tracking (follows the ball and players)"
        )
        self._vp_autocam_radio.setEnabled(autocam_installed)
        self._vp_btn_group.addButton(self._vp_autocam_radio)
        layout.addWidget(self._vp_autocam_radio)

        if not autocam_installed:
            autocam_note = QLabel(
                "    AutoCam is not installed. "
                'Install from <a href="https://autocam.app">autocam.app</a>'
            )
            autocam_note.setOpenExternalLinks(True)
            autocam_note.setStyleSheet("color: gray; font-style: italic;")
            layout.addWidget(autocam_note)

        layout.addSpacing(5)

        self._vp_none_radio = QRadioButton(
            "None \u2014 Upload raw full-field video without processing"
        )
        self._vp_btn_group.addButton(self._vp_none_radio)
        layout.addWidget(self._vp_none_radio)

        # Set default selection
        if autocam_installed:
            self._vp_autocam_radio.setChecked(True)
            self._video_processor_type = "autocam"
        else:
            self._vp_none_radio.setChecked(True)
            self._video_processor_type = "none"

        # Connect signals to update state
        self._vp_autocam_radio.toggled.connect(self._on_vp_radio_changed)

        layout.addStretch()
        return page

    def _on_vp_radio_changed(self, checked: bool):
        """Update video processor type when radio selection changes."""
        if self._vp_autocam_radio.isChecked():
            self._video_processor_type = "autocam"
        else:
            self._video_processor_type = "none"

    def _build_youtube_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 30, 40, 20)

        title = QLabel("YouTube Uploads")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        layout.addSpacing(10)

        desc = QLabel(
            "Soccer-Cam can automatically upload your game videos to "
            "your YouTube channel after processing."
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        layout.addSpacing(10)

        how_it_works = QLabel(
            "How it works:\n\n"
            "1. Click 'Authorize with YouTube' below\n"
            "2. A browser window will open for you to sign in to "
            "your Google account\n"
            "3. Google will ask you to grant Soccer-Cam permission "
            "to upload videos and manage playlists\n"
            "4. After you approve, the browser will redirect back to "
            "this app and you're done\n\n"
            "Soccer-Cam cannot access your email, contacts, or any "
            "other data -- only YouTube uploads and playlists. Your "
            "authorization is stored locally on this computer and can "
            "be revoked at any time from your Google Account settings."
        )
        how_it_works.setWordWrap(True)
        layout.addWidget(how_it_works)

        layout.addSpacing(20)

        # Authorize button + status
        auth_btn_layout = QHBoxLayout()
        self._yt_auth_btn = QPushButton("Authorize with YouTube")
        self._yt_auth_btn.setMinimumHeight(40)
        self._yt_auth_btn.clicked.connect(self._authenticate_youtube)
        auth_btn_layout.addWidget(self._yt_auth_btn)
        layout.addLayout(auth_btn_layout)

        layout.addSpacing(10)

        self._yt_auth_status = QLabel("")
        layout.addWidget(self._yt_auth_status)

        layout.addStretch()
        return page

    def _build_ntfy_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 30, 40, 20)

        title = QLabel("Push Notifications (NTFY)")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        layout.addSpacing(10)

        desc = QLabel(
            "NTFY sends push notifications to your phone asking you to "
            "confirm game start and end times. This helps Soccer-Cam "
            "trim your videos accurately."
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        layout.addSpacing(10)

        form = QFormLayout()

        # Topic with generate button
        topic_layout = QHBoxLayout()
        self._ntfy_topic_input = QLineEdit()
        self._ntfy_topic_input.setPlaceholderText("soccer-cam-xxxxxxxx")
        topic_layout.addWidget(self._ntfy_topic_input, 1)

        gen_btn = QPushButton("Generate Random")
        gen_btn.clicked.connect(self._generate_ntfy_topic)
        topic_layout.addWidget(gen_btn)

        form.addRow("Topic Name:", topic_layout)

        self._ntfy_server_input = QLineEdit("https://ntfy.sh")
        form.addRow("Server URL:", self._ntfy_server_input)

        layout.addLayout(form)

        layout.addSpacing(10)

        # Enable checkbox
        self._ntfy_enable_check = QCheckBox("Enable push notifications")
        self._ntfy_enable_check.setChecked(True)
        layout.addWidget(self._ntfy_enable_check)

        layout.addSpacing(10)

        # Instructions
        instructions = QLabel(
            "To receive notifications:\n"
            "1. Install the ntfy app on your phone\n"
            "   (available on Google Play and App Store)\n"
            "2. Subscribe to your topic name above\n"
            "3. When a game is detected, you'll get a notification"
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)

        # App download link
        app_link = QLabel('<a href="https://ntfy.sh">Download the ntfy app</a>')
        app_link.setOpenExternalLinks(True)
        layout.addWidget(app_link)

        layout.addStretch()
        return page

    def _build_machine_setup_page(self) -> QWidget:
        """Machine registration and camera conflict detection for TTT users."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 30, 40, 20)

        title = QLabel("Computer Setup")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        layout.addSpacing(10)

        self._machine_status_label = QLabel("Registering this computer...")
        self._machine_status_label.setWordWrap(True)
        layout.addWidget(self._machine_status_label)

        layout.addSpacing(10)

        # Machine name input
        form = QFormLayout()
        self._machine_name_input = QLineEdit(platform.node())
        form.addRow("Computer Name:", self._machine_name_input)
        layout.addLayout(form)

        layout.addSpacing(10)

        # Other machines info (populated dynamically)
        self._other_machines_label = QLabel("")
        self._other_machines_label.setWordWrap(True)
        layout.addWidget(self._other_machines_label)

        layout.addStretch()
        return page

    def _build_manual_ttt_page(self) -> QWidget:
        """Optional TTT step for the manual path."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 30, 40, 20)

        title = QLabel("Team Tech Tools (Optional)")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        layout.addSpacing(10)

        desc = QLabel(
            "Team Tech Tools provides game schedule integration, "
            "clip sharing, and more. Connect your account to get "
            "the most out of Soccer-Cam."
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        layout.addSpacing(10)

        form = QFormLayout()
        self._manual_ttt_email = QLineEdit()
        self._manual_ttt_email.setPlaceholderText("your@email.com")
        form.addRow("Email:", self._manual_ttt_email)

        self._manual_ttt_password = QLineEdit()
        self._manual_ttt_password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Password:", self._manual_ttt_password)
        layout.addLayout(form)

        btn_layout = QHBoxLayout()
        self._manual_ttt_signin_btn = QPushButton("Sign In")
        self._manual_ttt_signin_btn.clicked.connect(self._sign_in_manual_ttt)
        btn_layout.addWidget(self._manual_ttt_signin_btn)

        self._manual_ttt_status = QLabel("")
        btn_layout.addWidget(self._manual_ttt_status, 1)
        layout.addLayout(btn_layout)

        layout.addSpacing(10)

        signup_label = QLabel(
            "Don't have an account? "
            '<a href="https://team-tech-tools.vercel.app/signup">'
            "Sign up for Team Tech Tools</a>"
        )
        signup_label.setOpenExternalLinks(True)
        layout.addWidget(signup_label)

        layout.addStretch()
        return page

    def _build_playmetrics_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 30, 40, 20)

        title = QLabel("PlayMetrics")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        layout.addSpacing(10)

        desc = QLabel(
            "Connect your PlayMetrics account to automatically sync game "
            "schedules and populate match info. Enter the same email and "
            "password you use to sign in at playmetrics.com.\n\n"
            "Skip this step if you don't use PlayMetrics."
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        layout.addSpacing(10)

        form = QFormLayout()
        self._pm_username_input = QLineEdit()
        self._pm_username_input.setPlaceholderText("your@email.com")
        form.addRow("Email:", self._pm_username_input)

        self._pm_password_input = QLineEdit()
        self._pm_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Password:", self._pm_password_input)
        layout.addLayout(form)

        layout.addSpacing(10)

        # Sign In & Get Teams button + status
        pm_signin_layout = QHBoxLayout()
        self._pm_signin_btn = QPushButton("Sign In && Get Teams")
        self._pm_signin_btn.setMinimumHeight(36)
        self._pm_signin_btn.clicked.connect(self._pm_sign_in_and_get_teams)
        pm_signin_layout.addWidget(self._pm_signin_btn)

        self._pm_signin_status = QLabel("")
        pm_signin_layout.addWidget(self._pm_signin_status, 1)
        layout.addLayout(pm_signin_layout)

        layout.addSpacing(10)

        # Teams table
        teams_label = QLabel(
            "Teams — Add each team you want to sync. Find your Team ID in "
            "the PlayMetrics URL when viewing a team "
            "(e.g. playmetrics.com/teams/213119)."
        )
        teams_label.setWordWrap(True)
        teams_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(teams_label)

        self._pm_teams_table = QTableWidget(0, 5)
        self._pm_teams_table.setHorizontalHeaderLabels(
            ["Team Name", "Team ID", "YouTube Playlist", "Enabled", ""]
        )
        self._pm_teams_table.horizontalHeader().setStretchLastSection(False)
        self._pm_teams_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._pm_teams_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self._pm_teams_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self._pm_teams_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self._pm_teams_table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.ResizeToContents
        )
        self._pm_teams_table.verticalHeader().setVisible(False)
        layout.addWidget(self._pm_teams_table)

        add_btn = QPushButton("Add Team")
        add_btn.clicked.connect(self._pm_add_team_row)
        layout.addWidget(add_btn)

        layout.addStretch()
        return page

    def _pm_add_team_row(
        self,
        team_name: str = "",
        team_id: str = "",
        enabled: bool = True,
        playlist_name: str = "",
    ):
        """Add a row to the PlayMetrics teams table."""
        row = self._pm_teams_table.rowCount()
        self._pm_teams_table.insertRow(row)
        self._pm_teams_table.setItem(row, 0, QTableWidgetItem(team_name))
        self._pm_teams_table.setItem(row, 1, QTableWidgetItem(team_id))

        playlist_item = QTableWidgetItem(playlist_name)
        playlist_item.setToolTip("YouTube playlist name for this team's videos")
        self._pm_teams_table.setItem(row, 2, playlist_item)

        enabled_cb = QCheckBox()
        enabled_cb.setChecked(enabled)
        self._pm_teams_table.setCellWidget(row, 3, enabled_cb)

        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(lambda _, r=row: self._pm_remove_team_row(r))
        self._pm_teams_table.setCellWidget(row, 4, remove_btn)

    def _pm_remove_team_row(self, row: int):
        """Remove a row from the PlayMetrics teams table."""
        if 0 <= row < self._pm_teams_table.rowCount():
            self._pm_teams_table.removeRow(row)
            # Reconnect remove buttons with updated row indices
            for r in range(self._pm_teams_table.rowCount()):
                btn = self._pm_teams_table.cellWidget(r, 4)
                if btn:
                    btn.clicked.disconnect()
                    btn.clicked.connect(lambda _, r=r: self._pm_remove_team_row(r))

    def _pm_sign_in_and_get_teams(self):
        """Sign in to PlayMetrics and discover available teams."""
        username = self._pm_username_input.text().strip()
        password = self._pm_password_input.text()
        if not username or not password:
            QMessageBox.warning(
                self, "Missing Fields", "Please enter email and password."
            )
            return

        self._pm_signin_btn.setEnabled(False)
        self._pm_signin_status.setText("Signing in...")

        def do_pm_login():
            try:
                # Prefer TTT's connect probe when we're signed in to TTT —
                # it returns the canonical refresh_token + per-team role_id
                # mapping that the final create_schedule_provider call needs.
                # Fall back to the local PlayMetricsAPI for offline/legacy
                # setups that don't have a TTT account configured.
                refresh_token = ""
                role_id_by_team_id: dict[str, str] = {}
                discovered: list[dict] = []

                if self._ttt_client is not None:
                    result = self._ttt_client.connect_playmetrics(username, password)
                    refresh_token = str(result.get("refresh_token", ""))
                    for team in result.get("teams", []):
                        team_id = str(team.get("id", ""))
                        role_id = str(team.get("role_id", ""))
                        if not team_id:
                            continue
                        discovered.append({"id": team_id, "name": team.get("name", "")})
                        if role_id:
                            role_id_by_team_id[team_id] = role_id
                else:
                    from video_grouper.api_integrations.playmetrics import (
                        PlayMetricsAPI,
                    )

                    class _PMConfig:
                        pass

                    cfg = _PMConfig()
                    cfg.enabled = True
                    cfg.username = username
                    cfg.password = password
                    cfg.team_id = "0"
                    cfg.team_name = "discovery"

                    api = PlayMetricsAPI(cfg)
                    api.login()
                    raw_teams = api.get_available_teams()
                    api.close()
                    for t in raw_teams:
                        team_id = str(t.get("id", ""))
                        if team_id:
                            discovered.append(
                                {"id": team_id, "name": t.get("name", "")}
                            )

                def on_success(
                    teams=discovered,
                    rt=refresh_token,
                    role_map=role_id_by_team_id,
                ):
                    self._pm_signin_btn.setEnabled(True)
                    # Cache TTT-side credentials so _save_to_ttt sends the
                    # right shape (refresh_token + per-team current_role_id).
                    self._playmetrics_config["refresh_token"] = rt
                    self._playmetrics_config["role_id_by_team_id"] = role_map
                    # Clear existing teams table
                    self._pm_teams_table.setRowCount(0)
                    for t in teams:
                        name = t.get("name", "")
                        self._pm_add_team_row(
                            team_name=name,
                            team_id=str(t.get("id", "")),
                            enabled=True,
                            playlist_name=self._youtube_playlist_map.get(name, ""),
                        )
                    self._pm_signin_status.setText(f"Found {len(teams)} team(s)")

                self._run_on_main.emit(on_success)

            except Exception as exc:
                logger.error("PlayMetrics sign-in failed: %s", exc)

                def on_error(err=str(exc)):
                    self._pm_signin_btn.setEnabled(True)
                    self._pm_signin_status.setText(f"Failed: {err}")

                self._run_on_main.emit(on_error)

        thread = threading.Thread(target=do_pm_login, daemon=True)
        thread.start()

    def _build_teamsnap_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 30, 40, 20)

        title = QLabel("TeamSnap")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        layout.addSpacing(10)

        desc = QLabel(
            "Connect your TeamSnap account to sync game schedules. The "
            "easiest way is to sign in below — we'll create the integration "
            "automatically. If you'd rather paste your own credentials, "
            "scroll down to the manual section."
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        layout.addSpacing(10)

        # Auto-onboard form (Selenium dev portal automation)
        auto_label = QLabel("Sign in with TeamSnap")
        auto_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(auto_label)

        auto_form = QFormLayout()
        self._ts_auto_email_input = QLineEdit()
        self._ts_auto_email_input.setPlaceholderText("your@email.com")
        auto_form.addRow("Email:", self._ts_auto_email_input)

        self._ts_auto_password_input = QLineEdit()
        self._ts_auto_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        auto_form.addRow("Password:", self._ts_auto_password_input)
        layout.addLayout(auto_form)

        ts_auto_layout = QHBoxLayout()
        self._ts_auto_btn = QPushButton("Sign In && Create Application")
        self._ts_auto_btn.setMinimumHeight(36)
        self._ts_auto_btn.clicked.connect(self._ts_auto_onboard)
        ts_auto_layout.addWidget(self._ts_auto_btn)

        self._ts_auto_status = QLabel("")
        ts_auto_layout.addWidget(self._ts_auto_status, 1)
        layout.addLayout(ts_auto_layout)

        layout.addSpacing(16)

        # Manual paste form (fallback)
        manual_label = QLabel("Or paste credentials manually")
        manual_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(manual_label)

        manual_help = QLabel(
            "If sign-in fails or you'd rather provision the application "
            "yourself, create a new application at "
            "auth.teamsnap.com/oauth/applications/new and paste the "
            "Client ID and Client Secret below."
        )
        manual_help.setWordWrap(True)
        manual_help.setStyleSheet("color: #666; font-size: 11px;")
        layout.addWidget(manual_help)

        form = QFormLayout()
        self._ts_client_id_input = QLineEdit()
        self._ts_client_id_input.setPlaceholderText("e.g. tg_NSujH-SzkADEw...")
        form.addRow("Client ID:", self._ts_client_id_input)

        self._ts_client_secret_input = QLineEdit()
        self._ts_client_secret_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._ts_client_secret_input.setPlaceholderText("e.g. WlIUOoST_ul9ZOF...")
        form.addRow("Client Secret:", self._ts_client_secret_input)

        self._ts_access_token_input = QLineEdit()
        self._ts_access_token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._ts_access_token_input.setPlaceholderText(
            "(optional — obtained automatically)"
        )
        form.addRow("Access Token:", self._ts_access_token_input)
        layout.addLayout(form)

        layout.addSpacing(10)

        # Teams table
        teams_label = QLabel(
            "Teams — Add each team you want to sync. Find your Team ID in "
            "the TeamSnap URL when viewing a team "
            "(e.g. teamsnap.com/team/8200820)."
        )
        teams_label.setWordWrap(True)
        teams_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(teams_label)

        self._ts_teams_table = QTableWidget(0, 5)
        self._ts_teams_table.setHorizontalHeaderLabels(
            ["Team Name", "Team ID", "YouTube Playlist", "Enabled", ""]
        )
        self._ts_teams_table.horizontalHeader().setStretchLastSection(False)
        self._ts_teams_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._ts_teams_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self._ts_teams_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self._ts_teams_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self._ts_teams_table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.ResizeToContents
        )
        self._ts_teams_table.verticalHeader().setVisible(False)
        layout.addWidget(self._ts_teams_table)

        add_btn = QPushButton("Add Team")
        add_btn.clicked.connect(self._ts_add_team_row)
        layout.addWidget(add_btn)

        layout.addStretch()
        return page

    def _ts_add_team_row(
        self,
        team_name: str = "",
        team_id: str = "",
        enabled: bool = True,
        playlist_name: str = "",
    ):
        """Add a row to the TeamSnap teams table."""
        row = self._ts_teams_table.rowCount()
        self._ts_teams_table.insertRow(row)
        self._ts_teams_table.setItem(row, 0, QTableWidgetItem(team_name))
        self._ts_teams_table.setItem(row, 1, QTableWidgetItem(team_id))

        playlist_item = QTableWidgetItem(playlist_name)
        playlist_item.setToolTip("YouTube playlist name for this team's videos")
        self._ts_teams_table.setItem(row, 2, playlist_item)

        enabled_cb = QCheckBox()
        enabled_cb.setChecked(enabled)
        self._ts_teams_table.setCellWidget(row, 3, enabled_cb)

        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(lambda _, r=row: self._ts_remove_team_row(r))
        self._ts_teams_table.setCellWidget(row, 4, remove_btn)

    def _ts_remove_team_row(self, row: int):
        """Remove a row from the TeamSnap teams table."""
        if 0 <= row < self._ts_teams_table.rowCount():
            self._ts_teams_table.removeRow(row)
            # Reconnect remove buttons with updated row indices
            for r in range(self._ts_teams_table.rowCount()):
                btn = self._ts_teams_table.cellWidget(r, 4)
                if btn:
                    btn.clicked.disconnect()
                    btn.clicked.connect(lambda _, r=r: self._ts_remove_team_row(r))

    def _ts_auto_onboard(self):
        """Run the TeamSnap dev portal Selenium automation in a background thread.

        On success, drops the discovered ``client_id``/``client_secret``
        into the manual paste fields below — same downstream flow as if
        the user had pasted them themselves. The manual section stays
        usable as a fallback for any path the automation can't handle
        (broken Chrome, 2FA, CAPTCHA, dev portal layout drift).
        """
        email = self._ts_auto_email_input.text().strip()
        password = self._ts_auto_password_input.text()
        if not email or not password:
            QMessageBox.warning(
                self, "Missing Fields", "Please enter your TeamSnap email and password."
            )
            return

        self._ts_auto_btn.setEnabled(False)
        self._ts_auto_status.setText("Signing in to TeamSnap...")

        def do_ts_login():
            try:
                from video_grouper.api_integrations.teamsnap_dev_portal_automation import (
                    TeamSnapAutomationError,
                    obtain_teamsnap_credentials,
                )

                try:
                    creds = obtain_teamsnap_credentials(
                        email=email,
                        password=password,
                        headless=False,  # headed first run so user sees the flow
                    )
                except TeamSnapAutomationError as exc:
                    raise exc
                except Exception as exc:
                    # Wrap unexpected errors so the UI gets a clean message
                    raise RuntimeError(str(exc)) from exc

                def on_success(c=creds):
                    self._ts_auto_btn.setEnabled(True)
                    self._ts_client_id_input.setText(c.client_id)
                    self._ts_client_secret_input.setText(c.client_secret)
                    self._ts_auto_status.setText(
                        "Reused existing application."
                        if c.reused_existing
                        else "Created new application."
                    )

                self._run_on_main.emit(on_success)

            except Exception as exc:
                logger.error("TeamSnap auto-onboard failed: %s", exc)

                def on_error(err=str(exc)):
                    self._ts_auto_btn.setEnabled(True)
                    self._ts_auto_status.setText(
                        f"Failed: {err[:120]}. Use the manual fields below."
                    )

                self._run_on_main.emit(on_error)

        thread = threading.Thread(target=do_ts_login, daemon=True)
        thread.start()

    def _build_summary_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 30, 40, 20)

        title = QLabel("Setup Complete")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        layout.addSpacing(10)

        self._summary_label = QLabel("")
        self._summary_label.setWordWrap(True)
        layout.addWidget(self._summary_label)

        layout.addSpacing(10)

        self._next_steps_group = QGroupBox("Next Steps")
        self._next_steps_layout = QVBoxLayout(self._next_steps_group)
        self._next_steps_label = QLabel("")
        self._next_steps_label.setWordWrap(True)
        self._next_steps_layout.addWidget(self._next_steps_label)
        layout.addWidget(self._next_steps_group)

        layout.addSpacing(10)

        # NTFY status note (read-only; topic can be changed in tray settings)
        self._summary_ntfy_note = QLabel("")
        self._summary_ntfy_note.setWordWrap(True)
        self._summary_ntfy_note.setVisible(False)
        layout.addWidget(self._summary_ntfy_note)

        layout.addSpacing(10)

        rerun_label = QLabel("You can always re-run this wizard from the tray menu.")
        layout.addWidget(rerun_label)

        layout.addStretch()
        return page

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _get_page_sequence(self) -> list[int]:
        """Get the page sequence for the current mode."""
        if self._mode == "ttt":
            seq = [
                self.PAGE_WELCOME,
                self.PAGE_PATH_CHOICE,
                self.PAGE_TTT_SIGNIN,
            ]
            # Restore page is conditionally inserted in _go_next
            # NTFY page is skipped for TTT users -- topic is auto-configured
            # Machine setup is conditionally shown (only if other machines exist)
            seq.extend(
                [
                    self.PAGE_STORAGE,
                    self.PAGE_CAMERA,
                    self.PAGE_VIDEO_PROCESSING,
                    self.PAGE_MACHINE_SETUP,
                    self.PAGE_YOUTUBE,
                    self.PAGE_PLAYMETRICS,
                    self.PAGE_TEAMSNAP,
                    self.PAGE_SUMMARY,
                ]
            )
            return seq
        elif self._mode == "manual":
            return [
                self.PAGE_WELCOME,
                self.PAGE_PATH_CHOICE,
                self.PAGE_STORAGE,
                self.PAGE_CAMERA,
                self.PAGE_VIDEO_PROCESSING,
                self.PAGE_YOUTUBE,
                self.PAGE_NTFY,
                self.PAGE_MANUAL_TTT,
                self.PAGE_PLAYMETRICS,
                self.PAGE_TEAMSNAP,
                self.PAGE_SUMMARY,
            ]
        else:
            # Before mode is chosen, only welcome + path choice
            return [self.PAGE_WELCOME, self.PAGE_PATH_CHOICE]

    def _current_step_number(self) -> tuple[int, int]:
        """Return (current_step, total_steps) for display."""
        seq = self._get_page_sequence()
        current = self._stack.currentIndex()
        # Exclude welcome and path choice from the count
        visible_steps = [
            p for p in seq if p not in (self.PAGE_WELCOME, self.PAGE_PATH_CHOICE)
        ]
        if current in (self.PAGE_WELCOME, self.PAGE_PATH_CHOICE):
            return (0, len(visible_steps))
        if current in visible_steps:
            return (visible_steps.index(current) + 1, len(visible_steps))
        return (0, len(visible_steps))

    def _update_nav(self):
        """Update navigation bar based on current page."""
        current = self._stack.currentIndex()
        step, total = self._current_step_number()

        # Step label
        if step > 0:
            self._step_label.setText(f"Step {step} of {total}")
        else:
            self._step_label.setText("")

        # Back button
        self._back_btn.setVisible(len(self._nav_history) > 0)

        # Skip button (visible on skippable pages)
        skippable = current not in (
            self.PAGE_WELCOME,
            self.PAGE_PATH_CHOICE,
            self.PAGE_SUMMARY,
        )
        self._skip_btn.setVisible(skippable)

        # Next/Finish button
        if current == self.PAGE_SUMMARY:
            self._next_btn.setText("Finish")
        elif current == self.PAGE_PATH_CHOICE:
            self._next_btn.setVisible(False)
        else:
            self._next_btn.setText("Next")
            self._next_btn.setVisible(True)

    def _navigate_to(self, page_index: int):
        """Navigate to a page, recording history."""
        # Populate summary when reaching that page
        if page_index == self.PAGE_SUMMARY:
            self._collect_page_data(self._stack.currentIndex())
            # Auto-generate NTFY topic for TTT path if not already set
            if self._mode == "ttt" and not self._ntfy_topic:
                self._ntfy_topic = f"soccer-cam-{secrets.token_hex(4)}"
                self._ntfy_enabled = True
            self._populate_summary()

        # Auto-generate NTFY topic for manual path
        if page_index == self.PAGE_NTFY and self._mode != "ttt":
            if not self._ntfy_topic_input.text().strip():
                self._generate_ntfy_topic()

        # Machine setup: register and check for other machines
        if page_index == self.PAGE_MACHINE_SETUP and self._mode == "ttt":
            self._register_machine()

        # Pre-populate PlayMetrics page from state
        if page_index == self.PAGE_PLAYMETRICS:
            self._prepopulate_playmetrics_page()

        # Pre-populate TeamSnap page from state
        if page_index == self.PAGE_TEAMSNAP:
            self._prepopulate_teamsnap_page()

        self._nav_history.append(self._stack.currentIndex())
        self._stack.setCurrentIndex(page_index)
        self._update_nav()

    def _go_back(self):
        if self._nav_history:
            prev = self._nav_history.pop()
            self._stack.setCurrentIndex(prev)
            self._reset_page_state(prev)
            self._update_nav()

    def _reset_page_state(self, page_index: int):
        """Re-enable interactive controls when navigating back to a page."""
        if page_index == self.PAGE_TTT_SIGNIN:
            self._ttt_signin_btn.setEnabled(True)
            for _b in self._oauth_btns.values():
                _b.setEnabled(True)
        elif page_index == self.PAGE_MANUAL_TTT:
            if hasattr(self, "_manual_ttt_signin_btn"):
                self._manual_ttt_signin_btn.setEnabled(True)
        elif page_index == self.PAGE_CAMERA:
            if hasattr(self, "_camera_test_btn"):
                self._camera_test_btn.setEnabled(True)
        elif page_index == self.PAGE_YOUTUBE:
            if hasattr(self, "_yt_auth_btn"):
                self._yt_auth_btn.setEnabled(True)

    def _go_next(self):
        current = self._stack.currentIndex()

        # Collect data from current page before advancing
        self._collect_page_data(current)

        if current == self.PAGE_WELCOME:
            self._navigate_to(self.PAGE_PATH_CHOICE)
            return

        if current == self.PAGE_PATH_CHOICE:
            # Mode is set by button click, not Next
            return

        if current == self.PAGE_TTT_SIGNIN:
            if not self._ttt_enabled:
                QMessageBox.warning(
                    self,
                    "Sign In Required",
                    "Please sign in to Team Tech Tools first, or go Back "
                    "and choose Manual Setup.",
                )
                return
            # Check for existing device config
            if self._ttt_device_config:
                self._populate_restore_page()
                self._navigate_to(self.PAGE_TTT_RESTORE)
            else:
                self._navigate_to(self.PAGE_STORAGE)
            return

        if current == self.PAGE_TTT_RESTORE:
            # Handled by restore/fresh buttons
            return

        # Summary -> finish
        if current == self.PAGE_SUMMARY:
            self._finish()
            return

        # For all other pages, advance through sequence
        seq = self._get_page_sequence()
        if current in seq:
            idx = seq.index(current)
            if idx + 1 < len(seq):
                self._navigate_to(seq[idx + 1])
            return

    def _skip_step(self):
        """Skip the current step and advance."""
        current = self._stack.currentIndex()
        seq = self._get_page_sequence()
        if current in seq:
            idx = seq.index(current)
            if idx + 1 < len(seq):
                self._navigate_to(seq[idx + 1])

    def _choose_path(self, mode: str):
        """Handle path selection from the choice page."""
        self._mode = mode
        if mode == "ttt":
            self._navigate_to(self.PAGE_TTT_SIGNIN)
        else:
            self._navigate_to(self.PAGE_STORAGE)

    # ------------------------------------------------------------------
    # Data collection
    # ------------------------------------------------------------------

    def _collect_page_data(self, page_index: int):
        """Read widget values into instance variables for the given page."""
        if page_index == self.PAGE_STORAGE:
            self._storage_path = self._storage_path_input.text().strip()
            if not self._storage_path:
                self._storage_path = str(get_shared_data_path())

        elif page_index == self.PAGE_CAMERA:
            self._camera_ip = self._camera_ip_input.text().strip()
            self._camera_username = self._camera_user_input.text().strip()
            self._camera_password = self._camera_pass_input.text().strip()
            self._camera_type = self._camera_type_combo.currentText()
            self._camera_configured = bool(self._camera_ip)

        elif page_index == self.PAGE_VIDEO_PROCESSING:
            if self._vp_autocam_radio.isChecked():
                self._video_processor_type = "autocam"
            else:
                self._video_processor_type = "none"

        elif page_index == self.PAGE_NTFY:
            self._ntfy_topic = self._ntfy_topic_input.text().strip()
            self._ntfy_server_url = self._ntfy_server_input.text().strip()
            self._ntfy_enabled = self._ntfy_enable_check.isChecked() and bool(
                self._ntfy_topic
            )

        elif page_index == self.PAGE_PLAYMETRICS:
            self._playmetrics_config["username"] = (
                self._pm_username_input.text().strip()
            )
            self._playmetrics_config["password"] = self._pm_password_input.text()
            teams = []
            for row in range(self._pm_teams_table.rowCount()):
                name_item = self._pm_teams_table.item(row, 0)
                id_item = self._pm_teams_table.item(row, 1)
                playlist_item = self._pm_teams_table.item(row, 2)
                cb = self._pm_teams_table.cellWidget(row, 3)
                team_name = name_item.text() if name_item else ""
                playlist = playlist_item.text().strip() if playlist_item else ""
                teams.append(
                    {
                        "team_name": team_name,
                        "team_id": id_item.text() if id_item else "",
                        "enabled": cb.isChecked() if cb else True,
                    }
                )
                if team_name and playlist:
                    self._youtube_playlist_map[team_name] = playlist
            self._playmetrics_config["teams"] = teams

        elif page_index == self.PAGE_TEAMSNAP:
            self._teamsnap_config["client_id"] = self._ts_client_id_input.text().strip()
            self._teamsnap_config["client_secret"] = (
                self._ts_client_secret_input.text().strip()
            )
            self._teamsnap_config["access_token"] = (
                self._ts_access_token_input.text().strip()
            )
            teams = []
            for row in range(self._ts_teams_table.rowCount()):
                name_item = self._ts_teams_table.item(row, 0)
                id_item = self._ts_teams_table.item(row, 1)
                playlist_item = self._ts_teams_table.item(row, 2)
                cb = self._ts_teams_table.cellWidget(row, 3)
                team_name = name_item.text() if name_item else ""
                playlist = playlist_item.text().strip() if playlist_item else ""
                teams.append(
                    {
                        "team_name": team_name,
                        "team_id": id_item.text() if id_item else "",
                        "enabled": cb.isChecked() if cb else True,
                    }
                )
                if team_name and playlist:
                    self._youtube_playlist_map[team_name] = playlist
            self._teamsnap_config["teams"] = teams

        elif page_index == self.PAGE_SUMMARY:
            # No editable NTFY fields on the summary page; topic is
            # configured on the NTFY page (manual) or auto-set (TTT)
            # and can be changed later in the tray agent settings.
            pass

    # ------------------------------------------------------------------
    # TTT sign-in
    # ------------------------------------------------------------------

    def _sign_in_ttt(self):
        email = self._ttt_email_input.text().strip()
        password = self._ttt_password_input.text()
        if not email or not password:
            QMessageBox.warning(
                self, "Missing Fields", "Please enter email and password."
            )
            return

        self._ttt_signin_btn.setEnabled(False)
        self._ttt_signin_status.setText("Signing in...")

        # Stash credentials so the background thread can read them
        self._pending_ttt_email = email
        self._pending_ttt_password = password

        def do_login():
            try:
                from video_grouper.api_integrations.ttt_api import TTTApiClient

                client = TTTApiClient(
                    supabase_url=TTT_SUPABASE_URL,
                    anon_key=TTT_ANON_KEY,
                    api_base_url=TTT_API_BASE_URL,
                    storage_path=self._storage_path,
                )
                client.login(email, password)
                teams = client.get_team_assignments()
                device_config = client.get_device_config()

                # Fetch schedule providers for each team (best-effort)
                schedule_providers: dict[str, list[dict]] = {}
                for team in teams:
                    tid = team.get("team_id")
                    if not tid:
                        continue
                    try:
                        providers = client.list_schedule_providers(str(tid))
                        schedule_providers[str(tid)] = providers
                    except Exception as sp_exc:
                        logger.warning(
                            "Failed to fetch schedule providers for team %s: %s",
                            tid,
                            sp_exc,
                        )

                # Store results for the UI callback (safe — only read on main thread)
                self._pending_ttt_client = client
                self._pending_ttt_teams = teams
                self._pending_ttt_device_config = device_config
                self._pending_ttt_schedule_providers = schedule_providers

                # Signal the main thread (thread-safe)
                self._ttt_sign_in_succeeded.emit()

            except Exception as exc:
                logger.error("TTT sign-in failed: %s", exc)
                self._ttt_sign_in_failed.emit(str(exc))

        thread = threading.Thread(target=do_login, daemon=True)
        thread.start()

    def _on_ttt_sign_in_success(self):
        """Slot called on main thread after successful TTT sign-in."""
        self._ttt_client = self._pending_ttt_client
        self._ttt_email = self._pending_ttt_email
        self._ttt_password = self._pending_ttt_password
        self._ttt_enabled = True
        self._ttt_teams = self._pending_ttt_teams
        self._ttt_device_config = self._pending_ttt_device_config
        self._ttt_schedule_providers = getattr(
            self, "_pending_ttt_schedule_providers", {}
        )
        self._ttt_signin_btn.setEnabled(True)
        team_names = [t.get("team_name", "?") for t in self._ttt_teams]
        self._ttt_signin_status.setText(
            f"Signed in -- {len(self._ttt_teams)} team(s): {', '.join(team_names)}"
        )

        # Pre-fill PlayMetrics username from TTT email
        if self._ttt_email and not self._playmetrics_config.get("username"):
            self._playmetrics_config["username"] = self._ttt_email

        # Auto-populate NTFY from TTT device config
        dc = self._ttt_device_config
        if dc and dc.get("ntfy_topic"):
            self._ntfy_topic = dc["ntfy_topic"]
            self._ntfy_server_url = dc.get("ntfy_server_url", "https://ntfy.sh")
            self._ntfy_enabled = True

        # Auto-populate PlayMetrics / TeamSnap from TTT device config
        self._populate_integrations_from_device_config()

        # Pre-populate from schedule providers (takes precedence over device config)
        self._populate_integrations_from_schedule_providers()

        # Auto-advance to next step
        self._go_next()

    def _on_ttt_sign_in_error(self, err: str):
        """Slot called on main thread after failed TTT sign-in."""
        self._ttt_signin_btn.setEnabled(True)
        self._ttt_signin_status.setText(f"Sign-in failed: {err}")

    # ------------------------------------------------------------------
    # TTT Google OAuth sign-in
    # ------------------------------------------------------------------

    def _sign_in_ttt_oauth(self, provider: str = "google"):
        """Start OAuth flow for the given provider: open browser, listen for callback."""
        for btn in self._oauth_btns.values():
            btn.setEnabled(False)
        self._ttt_signin_btn.setEnabled(False)
        provider_label = provider.replace("_", " ").title()
        self._ttt_signin_status.setText(
            f"Opening browser for {provider_label} sign-in..."
        )

        def _run_oauth():
            try:
                # Find an available port
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
                sock.close()

                # Build the OAuth callback server
                token_holder: dict = {}
                error_holder: dict = {}
                wizard_ref = self

                class OAuthCallbackHandler(BaseHTTPRequestHandler):
                    """HTTP handler for the OAuth redirect."""

                    def log_message(self, fmt, *args):
                        # Suppress default stderr logging
                        logger.debug(fmt, *args)

                    def do_GET(self):
                        parsed = urlparse(self.path)

                        if parsed.path == "/callback":
                            # Supabase puts tokens in the URL fragment
                            # (hash), which browsers don't send to
                            # servers.  Serve a page with JS that
                            # extracts the hash and forwards the token.
                            self.send_response(200)
                            self.send_header("Content-Type", "text/html")
                            self.end_headers()
                            html = _OAUTH_CALLBACK_HTML.replace("{{PORT}}", str(port))
                            self.wfile.write(html.encode("utf-8"))
                            return

                        if parsed.path == "/receive-token":
                            qs = parse_qs(parsed.query)
                            access_token = qs.get("access_token", [None])[0]
                            if access_token:
                                token_holder["access_token"] = access_token
                                self.send_response(200)
                                self.send_header("Content-Type", "text/html")
                                self.end_headers()
                                self.wfile.write(
                                    b"<html><body><h2>Sign-in successful!</h2>"
                                    b"<p>You can close this tab and return to "
                                    b"Soccer-Cam.</p></body></html>"
                                )
                            else:
                                error_msg = qs.get(
                                    "error_description",
                                    qs.get("error", ["Unknown error"]),
                                )[0]
                                error_holder["error"] = error_msg
                                self.send_response(400)
                                self.send_header("Content-Type", "text/html")
                                self.end_headers()
                                self.wfile.write(
                                    f"<html><body><h2>Sign-in failed</h2>"
                                    f"<p>{error_msg}</p></body></html>".encode()
                                )
                            # Shut down the server after handling
                            threading.Thread(
                                target=self.server.shutdown, daemon=True
                            ).start()
                            return

                        # Unknown path
                        self.send_response(404)
                        self.end_headers()

                server = HTTPServer(("127.0.0.1", port), OAuthCallbackHandler)
                wizard_ref._oauth_server = server

                # Build the Supabase OAuth URL
                redirect_url = f"http://localhost:{port}/callback"
                auth_url = (
                    f"{TTT_SUPABASE_URL}/auth/v1/authorize"
                    f"?provider={provider}"
                    f"&redirect_to={redirect_url}"
                )

                # Open the browser
                webbrowser.open(auth_url)

                # Serve until we get a token or an error (or timeout)
                server.timeout = 300  # 5-minute timeout
                server.handle_timeout = lambda: server.shutdown()

                # serve_forever blocks until shutdown() is called
                server.serve_forever()
                server.server_close()
                wizard_ref._oauth_server = None

                if "access_token" in token_holder:
                    # We have the token -- create a TTT client session
                    from video_grouper.api_integrations.ttt_api import (
                        TTTApiClient,
                    )

                    client = TTTApiClient(
                        supabase_url=TTT_SUPABASE_URL,
                        anon_key=TTT_ANON_KEY,
                        api_base_url=TTT_API_BASE_URL,
                        storage_path=wizard_ref._storage_path,
                    )
                    client.set_session_from_token(token_holder["access_token"])
                    teams = client.get_team_assignments()
                    device_config = client.get_device_config()

                    # Fetch schedule providers for each team (best-effort)
                    schedule_providers: dict[str, list[dict]] = {}
                    for team in teams:
                        tid = team.get("team_id")
                        if not tid:
                            continue
                        try:
                            providers = client.list_schedule_providers(str(tid))
                            schedule_providers[str(tid)] = providers
                        except Exception as sp_exc:
                            logger.warning(
                                "Failed to fetch schedule providers for team %s: %s",
                                tid,
                                sp_exc,
                            )

                    # Extract email from the JWT for display
                    from video_grouper.api_integrations.ttt_api import (
                        _decode_jwt_payload,
                    )

                    try:
                        payload = _decode_jwt_payload(token_holder["access_token"])
                        email = payload.get("email", "Google user")
                    except Exception:
                        email = "Google user"

                    wizard_ref._pending_ttt_client = client
                    wizard_ref._pending_ttt_teams = teams
                    wizard_ref._pending_ttt_device_config = device_config
                    wizard_ref._pending_ttt_schedule_providers = schedule_providers
                    wizard_ref._pending_ttt_email = email
                    wizard_ref._pending_ttt_password = ""

                    wizard_ref._ttt_oauth_succeeded.emit()
                elif "error" in error_holder:
                    wizard_ref._ttt_oauth_failed.emit(error_holder["error"])
                else:
                    wizard_ref._ttt_oauth_failed.emit(
                        f"Timed out waiting for {provider_label} sign-in"
                    )

            except Exception as exc:
                logger.error("%s OAuth failed: %s", provider_label, exc, exc_info=True)
                wizard_ref._ttt_oauth_failed.emit(str(exc))

        thread = threading.Thread(target=_run_oauth, daemon=True)
        thread.start()

    def _on_ttt_oauth_success(self):
        """Slot called on main thread after successful Google OAuth."""
        self._ttt_client = self._pending_ttt_client
        self._ttt_email = self._pending_ttt_email
        self._ttt_password = self._pending_ttt_password
        self._ttt_enabled = True
        self._ttt_teams = self._pending_ttt_teams
        self._ttt_device_config = self._pending_ttt_device_config
        self._ttt_schedule_providers = getattr(
            self, "_pending_ttt_schedule_providers", {}
        )
        for _b in self._oauth_btns.values():
            _b.setEnabled(True)
        self._ttt_signin_btn.setEnabled(True)
        team_names = [t.get("team_name", "?") for t in self._ttt_teams]
        self._ttt_signin_status.setText(
            f"Signed in -- {len(self._ttt_teams)} team(s): {', '.join(team_names)}"
        )

        # Pre-fill PlayMetrics username from TTT email
        if self._ttt_email and not self._playmetrics_config.get("username"):
            self._playmetrics_config["username"] = self._ttt_email

        # Auto-populate NTFY from TTT device config
        dc = self._ttt_device_config
        if dc and dc.get("ntfy_topic"):
            self._ntfy_topic = dc["ntfy_topic"]
            self._ntfy_server_url = dc.get("ntfy_server_url", "https://ntfy.sh")
            self._ntfy_enabled = True

        # Auto-populate PlayMetrics / TeamSnap from TTT device config
        self._populate_integrations_from_device_config()

        # Pre-populate from schedule providers (takes precedence over device config)
        self._populate_integrations_from_schedule_providers()

        # Auto-advance to next step
        self._go_next()

    def _on_ttt_oauth_error(self, err: str):
        """Slot called on main thread after failed Google OAuth."""
        for _b in self._oauth_btns.values():
            _b.setEnabled(True)
        self._ttt_signin_btn.setEnabled(True)
        self._ttt_signin_status.setText(f"Sign-in failed: {err}")

    def _prepopulate_playmetrics_page(self):
        """Fill PlayMetrics page widgets from wizard state."""
        pm = self._playmetrics_config
        # Fall back to TTT email if no PlayMetrics username is configured
        if not pm.get("username") and self._ttt_email:
            pm["username"] = self._ttt_email
        if pm.get("username") and not self._pm_username_input.text().strip():
            self._pm_username_input.setText(pm["username"])
        if pm.get("password") and not self._pm_password_input.text():
            self._pm_password_input.setText(pm["password"])
        # Only populate teams table if it's currently empty
        if self._pm_teams_table.rowCount() == 0 and pm.get("teams"):
            for t in pm["teams"]:
                name = t.get("team_name", "")
                self._pm_add_team_row(
                    team_name=name,
                    team_id=t.get("team_id", ""),
                    enabled=t.get("enabled", True),
                    playlist_name=self._youtube_playlist_map.get(name, ""),
                )

    def _prepopulate_teamsnap_page(self):
        """Fill TeamSnap page widgets from wizard state."""
        ts = self._teamsnap_config
        if ts.get("client_id") and not self._ts_client_id_input.text().strip():
            self._ts_client_id_input.setText(ts["client_id"])
        if ts.get("client_secret") and not self._ts_client_secret_input.text():
            self._ts_client_secret_input.setText(ts["client_secret"])
        if ts.get("access_token") and not self._ts_access_token_input.text():
            self._ts_access_token_input.setText(ts["access_token"])
        # Only populate teams table if it's currently empty
        if self._ts_teams_table.rowCount() == 0 and ts.get("teams"):
            for t in ts["teams"]:
                name = t.get("team_name", "")
                self._ts_add_team_row(
                    team_name=name,
                    team_id=t.get("team_id", ""),
                    enabled=t.get("enabled", True),
                    playlist_name=self._youtube_playlist_map.get(name, ""),
                )

    def _populate_integrations_from_device_config(self):
        """Pre-populate PlayMetrics / TeamSnap state from TTT device config."""
        dc = self._ttt_device_config
        if not dc:
            return

        # PlayMetrics
        pm_data = dc.get("playmetrics")
        if isinstance(pm_data, dict):
            if pm_data.get("username"):
                self._playmetrics_config["username"] = pm_data["username"]
            if pm_data.get("password"):
                self._playmetrics_config["password"] = pm_data["password"]
            pm_teams = pm_data.get("teams")
            if isinstance(pm_teams, list) and pm_teams:
                self._playmetrics_config["teams"] = [
                    {
                        "team_name": t.get("team_name", ""),
                        "team_id": t.get("team_id", ""),
                        "enabled": t.get("enabled", True),
                    }
                    for t in pm_teams
                ]

        # TeamSnap
        ts_data = dc.get("teamsnap")
        if isinstance(ts_data, dict):
            if ts_data.get("client_id"):
                self._teamsnap_config["client_id"] = ts_data["client_id"]
            if ts_data.get("client_secret"):
                self._teamsnap_config["client_secret"] = ts_data["client_secret"]
            if ts_data.get("access_token"):
                self._teamsnap_config["access_token"] = ts_data["access_token"]
            ts_teams = ts_data.get("teams")
            if isinstance(ts_teams, list) and ts_teams:
                self._teamsnap_config["teams"] = [
                    {
                        "team_name": t.get("team_name", ""),
                        "team_id": t.get("team_id", ""),
                        "enabled": t.get("enabled", True),
                    }
                    for t in ts_teams
                ]

        # YouTube playlist map
        yt_playlist_map = dc.get("youtube_playlist_map")
        if isinstance(yt_playlist_map, dict) and yt_playlist_map:
            self._youtube_playlist_map = dict(yt_playlist_map)

    def _populate_integrations_from_schedule_providers(self):
        """Pre-populate PlayMetrics / TeamSnap state from TTT schedule providers."""
        if not self._ttt_schedule_providers:
            return

        for _team_id, providers in self._ttt_schedule_providers.items():
            for prov in providers:
                ptype = prov.get("provider_type")
                creds = prov.get("credentials") or {}
                ext_team_id = prov.get("external_team_id", "")
                ext_team_name = prov.get("external_team_name", "")

                if ptype == "playmetrics":
                    if creds.get("username"):
                        self._playmetrics_config["username"] = creds["username"]
                    if creds.get("password"):
                        self._playmetrics_config["password"] = creds["password"]
                    if ext_team_id:
                        # Merge into teams list if not already present
                        existing_ids = {
                            t.get("team_id")
                            for t in self._playmetrics_config.get("teams", [])
                        }
                        if ext_team_id not in existing_ids:
                            self._playmetrics_config.setdefault("teams", []).append(
                                {
                                    "team_name": ext_team_name,
                                    "team_id": ext_team_id,
                                    "enabled": True,
                                }
                            )

                elif ptype == "teamsnap":
                    if creds.get("client_id"):
                        self._teamsnap_config["client_id"] = creds["client_id"]
                    if creds.get("client_secret"):
                        self._teamsnap_config["client_secret"] = creds["client_secret"]
                    if creds.get("access_token"):
                        self._teamsnap_config["access_token"] = creds["access_token"]
                    if ext_team_id:
                        existing_ids = {
                            t.get("team_id")
                            for t in self._teamsnap_config.get("teams", [])
                        }
                        if ext_team_id not in existing_ids:
                            self._teamsnap_config.setdefault("teams", []).append(
                                {
                                    "team_name": ext_team_name,
                                    "team_id": ext_team_id,
                                    "enabled": True,
                                }
                            )

    def _sign_in_manual_ttt(self):
        """Sign in from the manual TTT page."""
        email = self._manual_ttt_email.text().strip()
        password = self._manual_ttt_password.text()
        if not email or not password:
            QMessageBox.warning(
                self, "Missing Fields", "Please enter email and password."
            )
            return

        self._manual_ttt_signin_btn.setEnabled(False)
        self._manual_ttt_status.setText("Signing in...")

        def do_login():
            try:
                from video_grouper.api_integrations.ttt_api import TTTApiClient

                client = TTTApiClient(
                    supabase_url=TTT_SUPABASE_URL,
                    anon_key=TTT_ANON_KEY,
                    api_base_url=TTT_API_BASE_URL,
                    storage_path=self._storage_path,
                )
                client.login(email, password)
                teams = client.get_team_assignments()

                def on_success():
                    self._ttt_client = client
                    self._ttt_email = email
                    self._ttt_password = password
                    self._ttt_enabled = True
                    self._ttt_teams = teams
                    self._manual_ttt_signin_btn.setEnabled(True)
                    team_names = [t.get("team_name", "?") for t in teams]
                    self._manual_ttt_status.setText(
                        f"Connected -- {len(teams)} team(s): {', '.join(team_names)}"
                    )

                self._run_on_main.emit(on_success)

            except Exception as exc:
                logger.error("TTT sign-in failed: %s", exc)

                def on_error(err=str(exc)):
                    self._manual_ttt_signin_btn.setEnabled(True)
                    self._manual_ttt_status.setText(f"Sign-in failed: {err}")

                self._run_on_main.emit(on_error)

        thread = threading.Thread(target=do_login, daemon=True)
        thread.start()

    # ------------------------------------------------------------------
    # TTT restore
    # ------------------------------------------------------------------

    def _populate_restore_page(self):
        """Fill the restore page with details from the stored device config."""
        cfg = self._ttt_device_config
        if not cfg:
            self._restore_details.setText("No stored configuration found.")
            return

        lines = ["Your previous settings were found:\n"]
        if cfg.get("camera_ip"):
            lines.append(f"  Camera: {cfg['camera_ip']}")
        if cfg.get("ntfy_topic"):
            lines.append(f"  NTFY Topic: {cfg['ntfy_topic']}")
        if cfg.get("youtube_configured"):
            lines.append("  YouTube: Previously configured")
        if cfg.get("gcp_project_id"):
            lines.append(f"  GCP Project: {cfg['gcp_project_id']}")
        pm_data = cfg.get("playmetrics")
        if isinstance(pm_data, dict) and pm_data.get("username"):
            pm_teams = pm_data.get("teams", [])
            lines.append(
                f"  PlayMetrics: {pm_data['username']} ({len(pm_teams)} team(s))"
            )
        ts_data = cfg.get("teamsnap")
        if isinstance(ts_data, dict) and (
            ts_data.get("client_id") or ts_data.get("access_token")
        ):
            ts_teams = ts_data.get("teams", [])
            lines.append(f"  TeamSnap: Configured ({len(ts_teams)} team(s))")
        self._restore_details.setText("\n".join(lines))

        # Show what's missing in a separate gray/italic label
        missing = []
        pm_data = cfg.get("playmetrics")
        if not isinstance(pm_data, dict) or not pm_data.get("username"):
            missing.append("PlayMetrics: Not configured (you'll be prompted)")
        ts_data_check = cfg.get("teamsnap")
        if not isinstance(ts_data_check, dict) or not ts_data_check.get("client_id"):
            missing.append("TeamSnap: Not configured (you'll be prompted)")
        if not cfg.get("camera_ip"):
            missing.append("Camera: Not configured (you'll be prompted)")
        if not cfg.get("youtube_configured"):
            missing.append("YouTube: Not configured (you'll be prompted)")

        if missing:
            missing_lines = ["Still needs setup:\n"]
            for item in missing:
                missing_lines.append(f"  {item}")
            self._restore_missing.setText("\n".join(missing_lines))
            self._restore_missing.setVisible(True)
        else:
            self._restore_missing.setVisible(False)

    def _restore_ttt_config(self):
        """Restore settings from TTT device config and jump to summary."""
        cfg = self._ttt_device_config
        if cfg:
            if cfg.get("camera_ip"):
                self._camera_ip = cfg["camera_ip"]
                self._camera_username = cfg.get("camera_username", "admin")
                self._camera_configured = True
                # Password is decrypted server-side and returned in response
                if cfg.get("camera_password"):
                    self._camera_password = cfg["camera_password"]
            if cfg.get("ntfy_topic"):
                self._ntfy_topic = cfg["ntfy_topic"]
                self._ntfy_server_url = cfg.get("ntfy_server_url", "https://ntfy.sh")
                self._ntfy_enabled = True
            if cfg.get("youtube_configured"):
                self._youtube_enabled = True
            if cfg.get("gcp_project_id"):
                self._gcp_project_id = cfg["gcp_project_id"]

            # Restore PlayMetrics / TeamSnap
            self._populate_integrations_from_device_config()

        self._populate_summary()
        self._navigate_to(self.PAGE_SUMMARY)

    def _start_fresh_ttt(self):
        """Ignore stored config and proceed through setup steps."""
        self._ttt_device_config = None
        self._navigate_to(self.PAGE_STORAGE)

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    @staticmethod
    def _get_gateway_placeholder() -> str:
        """Return the default gateway IP as a placeholder hint for camera IP."""
        try:
            import subprocess

            result = subprocess.run(
                [
                    "powershell",
                    "-Command",
                    "(Get-NetRoute -DestinationPrefix '0.0.0.0/0' | "
                    "Sort-Object -Property RouteMetric | "
                    "Select-Object -First 1).NextHop",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            gw = result.stdout.strip()
            if gw:
                return gw
        except Exception:
            pass
        return "192.168.1.100"

    def _browse_storage(self):
        path = QFileDialog.getExistingDirectory(
            self,
            "Select Storage Directory",
            self._storage_path,
            QFileDialog.Option.DontUseNativeDialog,
        )
        if path:
            self._storage_path_input.setText(path)
            self._storage_path = path

    # ------------------------------------------------------------------
    # Camera
    # ------------------------------------------------------------------

    def _create_camera_instance(self, ip, username, password, camera_type):
        """Create a camera instance with proper CameraConfig."""
        from video_grouper.utils.config import CameraConfig

        config = CameraConfig(
            name=camera_type,
            type=camera_type,
            device_ip=ip,
            username=username,
            password=password,
        )
        storage = self._storage_path or str(get_shared_data_path())
        if camera_type == "reolink":
            from video_grouper.cameras.reolink import ReolinkCamera

            return ReolinkCamera(config, storage)
        else:
            from video_grouper.cameras.dahua import DahuaCamera

            return DahuaCamera(config, storage)

    def _test_camera(self):
        ip = self._camera_ip_input.text().strip()
        username = self._camera_user_input.text().strip()
        password = self._camera_pass_input.text()
        camera_type = self._camera_type_combo.currentText()

        if not ip:
            QMessageBox.warning(
                self, "Missing IP", "Please enter the camera IP address."
            )
            return

        self._camera_test_btn.setEnabled(False)
        self._camera_test_status.setText("Connecting...")

        def do_test():
            try:
                cam = self._create_camera_instance(ip, username, password, camera_type)
                available = asyncio.run(cam.check_availability())
                device_info = None
                if available:
                    try:
                        device_info = asyncio.run(cam.get_device_info())
                    except Exception:
                        pass

                # Read current settings for Phase C
                settings = []
                if available:
                    try:
                        settings = asyncio.run(cam.get_current_settings())
                    except Exception as e:
                        logger.warning("Failed to read camera settings: %s", e)

                def on_result(ok=available, info=device_info, cfg=settings):
                    self._camera_test_btn.setEnabled(True)
                    if ok:
                        self._camera_test_status.setText("Connected!")
                        self._camera_configured = True
                        self._camera_ip = ip
                        self._camera_username = username
                        self._camera_password = password
                        self._camera_type = camera_type

                        # Show device info and capture serial
                        if info:
                            model = info.get("model", "")
                            fw = info.get("firmware_version", "")
                            mac = info.get("mac_address", "")
                            serial = info.get("serial_number", "")
                            if serial:
                                self._camera_serial = serial
                            parts = [p for p in [model, fw, mac] if p]
                            self._cam_device_info.setText(
                                "  ".join(parts) if parts else ""
                            )
                            self._cam_device_info.setVisible(bool(parts))

                        # Show Phase B (password) for factory defaults
                        if password == "admin" or password == "":
                            self._camera_factory_defaults = True
                            self._cam_pass_desc.setText(
                                "This camera has factory default credentials. "
                                "Set a secure password to protect your camera."
                            )
                            self._cam_phase_b.setVisible(True)
                        else:
                            self._cam_phase_b.setVisible(False)

                        # Show Phase C (config) with current settings
                        self._show_config_preview(cfg)
                        self._cam_phase_c.setVisible(True)
                    else:
                        self._camera_test_status.setText("Connection failed")

                self._run_on_main.emit(on_result)

            except Exception as exc:
                logger.error("Camera test failed: %s", exc)

                def on_error(err=str(exc)):
                    self._camera_test_btn.setEnabled(True)
                    self._camera_test_status.setText(f"Error: {err}")

                self._run_on_main.emit(on_error)

        thread = threading.Thread(target=do_test, daemon=True)
        thread.start()

    def _show_config_preview(self, settings):
        """Populate Phase C table with current settings and proposed values."""
        proposed = {
            "recording": "Always on (24/7)",
            "ntp": "Enabled, pool.ntp.org",
            "encoding": "Verify / optimize",
        }
        self._cam_config_table.setRowCount(len(settings) if settings else 3)

        if not settings:
            # Show defaults if we couldn't read
            for row, (name, prop) in enumerate(proposed.items()):
                self._cam_config_table.setItem(
                    row, 0, QTableWidgetItem(name.capitalize())
                )
                self._cam_config_table.setItem(row, 1, QTableWidgetItem("Unknown"))
                self._cam_config_table.setItem(row, 2, QTableWidgetItem(prop))
            return

        for row, result in enumerate(settings):
            setting = result.get("setting", "")
            current = result.get("current_value", "Unknown")
            prop = proposed.get(setting, "")
            self._cam_config_table.setItem(
                row, 0, QTableWidgetItem(setting.capitalize())
            )
            self._cam_config_table.setItem(row, 1, QTableWidgetItem(current))
            self._cam_config_table.setItem(row, 2, QTableWidgetItem(prop))

    def _set_camera_password(self):
        """Phase B: Change camera password."""
        new_pass = self._cam_new_pass.text()
        confirm = self._cam_confirm_pass.text()

        if not new_pass:
            QMessageBox.warning(self, "Error", "Password cannot be empty.")
            return
        if new_pass != confirm:
            QMessageBox.warning(self, "Error", "Passwords do not match.")
            return

        self._cam_set_pass_btn.setEnabled(False)
        self._cam_pass_status.setText("Changing password...")

        ip = self._camera_ip
        username = self._camera_username
        old_pass = self._camera_password
        camera_type = self._camera_type

        def do_change():
            try:
                cam = self._create_camera_instance(ip, username, old_pass, camera_type)
                ok = asyncio.run(cam.change_camera_password(old_pass, new_pass))

                def on_result(success=ok):
                    self._cam_set_pass_btn.setEnabled(True)
                    if success:
                        self._camera_password = new_pass
                        self._cam_pass_status.setText("Password changed!")
                        self._cam_phase_b.setVisible(False)
                    else:
                        self._cam_pass_status.setText("Failed to change password")

                self._run_on_main.emit(on_result)
            except Exception as exc:
                logger.error("Password change failed: %s", exc)

                def on_error(err=str(exc)):
                    self._cam_set_pass_btn.setEnabled(True)
                    self._cam_pass_status.setText(f"Error: {err}")

                self._run_on_main.emit(on_error)

        thread = threading.Thread(target=do_change, daemon=True)
        thread.start()

    def _apply_camera_settings(self):
        """Phase C: Apply optimal settings to the camera."""
        self._cam_apply_btn.setEnabled(False)
        self._cam_config_status.setText("Applying settings...")

        ip = self._camera_ip
        username = self._camera_username
        password = self._camera_password
        camera_type = self._camera_type

        def do_apply():
            # Timezone auto-detected from system clock inside camera classes.
            # Pass empty string -- each camera's apply method handles detection.
            tz = ""

            try:
                cam = self._create_camera_instance(ip, username, password, camera_type)
                results = asyncio.run(cam.apply_optimal_settings(timezone=tz))

                def on_result(res=results):
                    self._cam_apply_btn.setEnabled(True)
                    self._camera_settings_applied = True
                    self._camera_settings_results = res

                    # Update table with results
                    all_ok = True
                    for row, r in enumerate(res):
                        if row < self._cam_config_table.rowCount():
                            status = "[OK]" if r.get("success") else "[FAIL]"
                            applied = r.get("applied_value", "")
                            error = r.get("error", "")
                            display = applied if r.get("success") else error
                            self._cam_config_table.setItem(
                                row, 2, QTableWidgetItem(f"{status} {display}")
                            )
                            if not r.get("success"):
                                all_ok = False

                    if all_ok:
                        self._cam_config_status.setText(
                            "All settings applied successfully!"
                        )
                    else:
                        self._cam_config_status.setText(
                            "Some settings failed (see table above)"
                        )

                self._run_on_main.emit(on_result)
            except Exception as exc:
                logger.error("Apply settings failed: %s", exc)

                def on_error(err=str(exc)):
                    self._cam_apply_btn.setEnabled(True)
                    self._cam_config_status.setText(f"Error: {err}")

                self._run_on_main.emit(on_error)

        thread = threading.Thread(target=do_apply, daemon=True)
        thread.start()

    # ------------------------------------------------------------------
    # YouTube / GCP
    # ------------------------------------------------------------------

    def _authenticate_youtube(self):
        storage_path = self._storage_path
        if not storage_path:
            QMessageBox.warning(
                self, "No Storage Path", "Please set the storage path first."
            )
            return

        # Build the token path directly from the wizard's storage path so the
        # token lands where the service expects it (storage_path/youtube/token.json)
        # instead of relative to get_shared_data_path().
        yt_dir = Path(storage_path) / "youtube"
        yt_dir.mkdir(parents=True, exist_ok=True)
        token_file = str(yt_dir / "token.json")

        self._yt_auth_btn.setEnabled(False)
        self._yt_auth_status.setText("Opening browser for sign-in...")

        def do_auth():
            try:
                success, message = authenticate_youtube_embedded(token_file)
                self._yt_auth_finished.emit(success, message)
            except Exception as exc:
                logger.error("YouTube auth failed: %s", exc)
                self._yt_auth_finished.emit(False, str(exc))

        thread = threading.Thread(target=do_auth, daemon=True)
        thread.start()

    def _on_yt_auth_finished(self, success: bool, message: str):
        """Slot called on main thread after YouTube auth completes."""
        self._yt_auth_btn.setEnabled(True)
        if success:
            self._youtube_enabled = True
            self._youtube_authenticated = True
            self._yt_auth_status.setText("Authorized")
        else:
            self._yt_auth_status.setText(f"Failed: {message}")

    # ------------------------------------------------------------------
    # NTFY
    # ------------------------------------------------------------------

    def _register_machine(self):
        """Register this machine with TTT and check for other machines."""
        if not self._ttt_client or self._machine_setup_done:
            return

        self._machine_status_label.setText("Registering this computer...")

        def do_register():
            try:
                from video_grouper.utils.machine_id import get_or_create_machine_id

                machine_id = get_or_create_machine_id(self._storage_path)
                machine_name = (
                    self._machine_name_input.text().strip() or platform.node()
                )

                result = self._ttt_client.register_machine(machine_id, machine_name)
                other_machines = self._ttt_client.list_machines()
                # Filter out this machine
                others = [
                    m for m in other_machines if m.get("machine_id") != machine_id
                ]

                def on_success(
                    mid=machine_id, mname=machine_name, ms=others, reg=result
                ):
                    self._machine_id = mid
                    self._machine_name = mname
                    self._other_machines = ms
                    self._machine_setup_done = True

                    if ms:
                        lines = ["Other computers linked to this account:\n"]
                        for m in ms:
                            name = m.get("machine_name", "Unknown")
                            last = m.get("last_seen", "never")
                            lines.append(f"  - {name} (last seen: {last})")
                        lines.append(
                            "\nYou can manage camera assignments in the "
                            "Team Tech Tools website."
                        )
                        self._other_machines_label.setText("\n".join(lines))
                        self._machine_status_label.setText(
                            f"Registered as '{mname}'. "
                            f"{len(ms)} other computer(s) found."
                        )
                    else:
                        self._other_machines_label.setText("")
                        self._machine_status_label.setText(
                            f"Registered as '{mname}'. "
                            "This is the only computer on this account."
                        )

                self._run_on_main.emit(on_success)

            except Exception as exc:
                logger.error("Machine registration failed: %s", exc)

                def on_error(err=str(exc)):
                    self._machine_status_label.setText(
                        f"Registration failed: {err}\n"
                        "You can continue setup -- machine management "
                        "can be configured later."
                    )
                    self._machine_setup_done = True

                self._run_on_main.emit(on_error)

        thread = threading.Thread(target=do_register, daemon=True)
        thread.start()

    def _generate_ntfy_topic(self):
        topic = f"soccer-cam-{secrets.token_hex(4)}"
        self._ntfy_topic_input.setText(topic)

    # ------------------------------------------------------------------
    # Summary & Finish
    # ------------------------------------------------------------------

    def _populate_summary(self):
        """Build the summary text and next steps."""
        configured = []
        skipped = []

        # Storage
        configured.append(f"Storage: {self._storage_path}")

        # Camera
        if self._camera_configured and self._camera_ip:
            cam_line = f"Camera: {self._camera_ip} ({self._camera_type})"
            if self._camera_settings_applied:
                ok_count = sum(
                    1 for r in self._camera_settings_results if r.get("success")
                )
                total = len(self._camera_settings_results)
                cam_line += f" -- settings applied ({ok_count}/{total})"
            configured.append(cam_line)
        else:
            skipped.append(
                "Camera: Open Settings > Camera Settings to configure your camera"
            )

        # Video Processing
        if self._video_processor_type == "autocam":
            configured.append("Video Processing: AutoCam")
        else:
            configured.append("Video Processing: None (raw upload)")

        # YouTube
        if self._youtube_authenticated:
            configured.append("YouTube: Authenticated")
        elif self._youtube_enabled:
            configured.append("YouTube: Enabled (needs re-authentication)")
        else:
            skipped.append("YouTube: Open Settings > YouTube to set up video uploads")

        # NTFY
        if self._ntfy_enabled:
            configured.append(f"NTFY: Topic '{self._ntfy_topic}'")
        else:
            skipped.append("NTFY: Open Settings to configure push notifications")

        # PlayMetrics
        pm = self._playmetrics_config
        if pm.get("username"):
            pm_teams = [t for t in pm.get("teams", []) if t.get("enabled")]
            configured.append(
                f"PlayMetrics: {pm['username']} ({len(pm_teams)} team(s))"
            )
        else:
            skipped.append(
                "PlayMetrics: Open Settings to configure PlayMetrics integration"
            )

        # TeamSnap
        ts = self._teamsnap_config
        if ts.get("client_id") or ts.get("access_token"):
            ts_teams = [t for t in ts.get("teams", []) if t.get("enabled")]
            configured.append(f"TeamSnap: Configured ({len(ts_teams)} team(s))")
        else:
            skipped.append("TeamSnap: Open Settings to configure TeamSnap integration")

        # TTT
        if self._ttt_enabled:
            configured.append(f"Team Tech Tools: Connected as {self._ttt_email}")
        else:
            skipped.append(
                "Team Tech Tools: Re-run this wizard and choose "
                "'Sign in with Team Tech Tools'"
            )

        # Build summary text
        lines = []
        for item in configured:
            lines.append(f"  [OK]  {item}")
        for item in skipped:
            lines.append(f"  [ -- ]  {item.split(':')[0]}: Not configured")

        self._summary_label.setText("\n".join(lines))

        # Next steps
        if skipped:
            self._next_steps_group.setVisible(True)
            next_lines = [f"  * {item}" for item in skipped]
            self._next_steps_label.setText("\n".join(next_lines))
        else:
            self._next_steps_group.setVisible(False)

        # NTFY read-only note on summary page
        if self._ntfy_enabled:
            self._summary_ntfy_note.setText(
                "NTFY notifications are enabled. "
                "You can change the topic in the tray agent settings."
            )
            self._summary_ntfy_note.setVisible(True)
        else:
            self._summary_ntfy_note.setVisible(False)

    def _save_schedule_providers_to_ttt(self):
        """Create or update schedule providers in TTT for configured integrations."""
        if not self._ttt_client or not self._ttt_teams:
            return

        # Use the first TTT team for all providers in this first pass
        ttt_team_id = str(self._ttt_teams[0].get("team_id", ""))
        if not ttt_team_id:
            return

        # Build a lookup of existing providers by (provider_type, external_team_id)
        existing_providers: dict[tuple[str, str], dict] = {}
        for providers in self._ttt_schedule_providers.values():
            for prov in providers:
                key = (
                    prov.get("provider_type", ""),
                    prov.get("external_team_id", ""),
                )
                existing_providers[key] = prov

        # PlayMetrics providers — push the canonical TTT credential shape
        # (refresh_token + per-team current_role_id). The wizard's sign-in
        # step already populated `refresh_token` and `role_id_by_team_id`
        # via TTT's connect probe, so we don't send the raw password here.
        pm = self._playmetrics_config
        if pm.get("refresh_token"):
            role_map = pm.get("role_id_by_team_id", {}) or {}
            for team in pm.get("teams", []):
                if not team.get("enabled", True):
                    continue
                ext_id = team.get("team_id", "")
                ext_name = team.get("team_name", "")
                role_id = role_map.get(ext_id, "")
                if not role_id:
                    logger.warning(
                        "Skipping PlayMetrics team %s — no role_id from connect probe",
                        ext_name,
                    )
                    continue
                credentials = {
                    "refresh_token": pm["refresh_token"],
                    "current_role_id": role_id,
                }
                lookup_key = ("playmetrics", ext_id)
                try:
                    if lookup_key in existing_providers:
                        prov_id = existing_providers[lookup_key].get("id", "")
                        if prov_id:
                            self._ttt_client.update_schedule_provider(
                                str(prov_id),
                                {
                                    "credentials": credentials,
                                    "external_team_id": ext_id,
                                    "external_team_name": ext_name,
                                },
                            )
                    else:
                        self._ttt_client.create_schedule_provider(
                            {
                                "team_id": ttt_team_id,
                                "provider_type": "playmetrics",
                                "credentials": credentials,
                                "external_team_id": ext_id,
                                "external_team_name": ext_name,
                            }
                        )
                except Exception as exc:
                    logger.warning(
                        "Failed to save PlayMetrics provider for team %s: %s",
                        ext_name,
                        exc,
                    )

        # TeamSnap providers
        ts = self._teamsnap_config
        if ts.get("client_id") or ts.get("access_token"):
            for team in ts.get("teams", []):
                if not team.get("enabled", True):
                    continue
                ext_id = team.get("team_id", "")
                ext_name = team.get("team_name", "")
                lookup_key = ("teamsnap", ext_id)
                try:
                    if lookup_key in existing_providers:
                        prov_id = existing_providers[lookup_key].get("id", "")
                        if prov_id:
                            self._ttt_client.update_schedule_provider(
                                str(prov_id),
                                {
                                    "credentials": {
                                        "client_id": ts.get("client_id", ""),
                                        "client_secret": ts.get("client_secret", ""),
                                        "access_token": ts.get("access_token", ""),
                                    },
                                    "external_team_id": ext_id,
                                    "external_team_name": ext_name,
                                },
                            )
                    else:
                        self._ttt_client.create_schedule_provider(
                            {
                                "team_id": ttt_team_id,
                                "provider_type": "teamsnap",
                                "credentials": {
                                    "client_id": ts.get("client_id", ""),
                                    "client_secret": ts.get("client_secret", ""),
                                    "access_token": ts.get("access_token", ""),
                                },
                                "external_team_id": ext_id,
                                "external_team_name": ext_name,
                            }
                        )
                except Exception as exc:
                    logger.warning(
                        "Failed to save TeamSnap provider for team %s: %s",
                        ext_name,
                        exc,
                    )

    def _finish(self):
        """Save configuration and close the wizard."""
        try:
            self._finish_inner()
        except Exception as exc:
            logger.error("Finish failed: %s", exc, exc_info=True)
            QMessageBox.critical(
                self, "Setup Error", f"Failed to complete setup: {exc}"
            )

    def _finish_inner(self):
        # Collect data from current page
        self._collect_page_data(self._stack.currentIndex())

        # Use the wizard's storage path for config, not the exe directory
        config_dir = Path(self._storage_path)
        config_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = config_dir / "config.ini"
        logger.info("Saving config to %s", self.config_path)

        # Create or update config
        if self.config_path.exists():
            try:
                config = load_config(self.config_path)
            except Exception:
                config = create_default_config(self.config_path, self._storage_path)
        else:
            config = create_default_config(self.config_path, self._storage_path)

        # Apply wizard settings
        config.storage.path = self._storage_path

        # Camera
        if self._camera_configured and self._camera_ip:
            from video_grouper.utils.config import CameraConfig

            cam = CameraConfig(
                name=self._camera_type,
                type=self._camera_type,
                device_ip=self._camera_ip,
                username=self._camera_username,
                password=self._camera_password,
                serial=self._camera_serial,
            )
            config.cameras = [cam]

        # YouTube
        config.youtube.enabled = self._youtube_enabled

        # Build playlist map: start from any TTT-restored map, then ensure
        # every TTT team has an entry (team_name -> team_name as default).
        playlist_map = dict(self._youtube_playlist_map)
        if self._ttt_teams:
            for team in self._ttt_teams:
                team_name = team.get("team_name", "")
                if team_name and team_name not in playlist_map:
                    playlist_map[team_name] = team_name
        if playlist_map:
            from video_grouper.utils.config import YouTubePlaylistMapConfig

            config.youtube.playlist_map = YouTubePlaylistMapConfig(playlist_map)

        # NTFY
        config.ntfy.enabled = self._ntfy_enabled
        if self._ntfy_topic:
            config.ntfy.topic = self._ntfy_topic
        if self._ntfy_server_url:
            config.ntfy.server_url = self._ntfy_server_url
        if self._ntfy_enabled:
            config.ntfy.response_service = True

        # Ball tracking — wizard's "autocam" choice maps to autocam_gui provider.
        config.ball_tracking.enabled = self._video_processor_type == "autocam"
        config.ball_tracking.provider = "autocam_gui"
        if self._autocam_path:
            config.ball_tracking.autocam_gui.executable = self._autocam_path

        # PlayMetrics
        pm = self._playmetrics_config
        pm_has_creds = bool(pm.get("username"))
        config.playmetrics.enabled = pm_has_creds
        if pm_has_creds:
            config.playmetrics.username = pm["username"]
            config.playmetrics.password = pm.get("password", "")
            from video_grouper.utils.config import PlayMetricsTeamConfig

            config.playmetrics.teams = [
                PlayMetricsTeamConfig(
                    team_name=t.get("team_name", ""),
                    team_id=t.get("team_id") or None,
                    enabled=t.get("enabled", True),
                )
                for t in pm.get("teams", [])
            ]

        # TeamSnap
        ts = self._teamsnap_config
        ts_has_creds = bool(ts.get("client_id") or ts.get("access_token"))
        config.teamsnap.enabled = ts_has_creds
        if ts_has_creds:
            config.teamsnap.client_id = ts.get("client_id") or None
            config.teamsnap.client_secret = ts.get("client_secret") or None
            config.teamsnap.access_token = ts.get("access_token") or None
            from video_grouper.utils.config import TeamSnapTeamConfig

            config.teamsnap.teams = [
                TeamSnapTeamConfig(
                    team_name=t.get("team_name", ""),
                    team_id=t.get("team_id") or None,
                    enabled=t.get("enabled", True),
                )
                for t in ts.get("teams", [])
            ]

        # TTT
        if self._ttt_enabled:
            config.ttt.enabled = True
            config.ttt.supabase_url = TTT_SUPABASE_URL
            config.ttt.anon_key = TTT_ANON_KEY
            config.ttt.api_base_url = TTT_API_BASE_URL
            config.ttt.email = self._ttt_email
            config.ttt.password = self._ttt_password

        # Mark onboarding as completed
        config.setup.onboarding_completed = True

        # Save locally
        try:
            from video_grouper.utils.locking import FileLock

            with FileLock(self.config_path):
                save_config(config, self.config_path)
            logger.info("Configuration saved to %s", self.config_path)
        except Exception as exc:
            logger.error("Failed to save config: %s", exc)
            QMessageBox.critical(
                self, "Save Error", f"Failed to save configuration: {exc}"
            )
            return

        # Write StoragePath to registry so the Windows service can find config
        try:
            import winreg

            key = winreg.CreateKeyEx(
                winreg.HKEY_LOCAL_MACHINE,
                r"Software\VideoGrouper",
                0,
                winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY,
            )
            winreg.SetValueEx(
                key, "StoragePath", 0, winreg.REG_SZ, str(self._storage_path)
            )
            winreg.CloseKey(key)
            logger.info("Wrote StoragePath to registry: %s", self._storage_path)
        except Exception as exc:
            # Non-admin context or non-Windows — not fatal
            logger.warning("Could not write StoragePath to registry: %s", exc)

        # Save to TTT if connected
        if self._ttt_enabled and self._ttt_client:
            try:
                device_data = {
                    "ntfy_topic": self._ntfy_topic or None,
                    "ntfy_server_url": self._ntfy_server_url,
                    "youtube_configured": self._youtube_authenticated,
                    "gcp_project_id": self._gcp_project_id,
                    "camera_ip": self._camera_ip or None,
                    "camera_username": self._camera_username or None,
                }
                # Password sent over HTTPS; TTT backend encrypts at rest
                if self._camera_password:
                    device_data["camera_password"] = self._camera_password

                # PlayMetrics credentials for future restores
                pm = self._playmetrics_config
                if pm.get("username"):
                    device_data["playmetrics"] = {
                        "username": pm["username"],
                        "password": pm.get("password", ""),
                        "teams": pm.get("teams", []),
                    }

                # YouTube playlist map for future restores
                if playlist_map:
                    device_data["youtube_playlist_map"] = playlist_map

                # TeamSnap credentials for future restores
                ts = self._teamsnap_config
                if ts.get("client_id") or ts.get("access_token"):
                    device_data["teamsnap"] = {
                        "client_id": ts.get("client_id", ""),
                        "client_secret": ts.get("client_secret", ""),
                        "access_token": ts.get("access_token", ""),
                        "teams": ts.get("teams", []),
                    }

                self._ttt_client.save_device_config(device_data)
                logger.info("Device config saved to TTT")
            except Exception as exc:
                logger.warning("Failed to save device config to TTT: %s", exc)

            # Sync schedule providers to TTT
            self._save_schedule_providers_to_ttt()

        # Start the Windows service now that config is saved
        try:
            import subprocess

            subprocess.run(
                ["sc", "start", "VideoGrouperService"],
                capture_output=True,
                timeout=10,
            )
            logger.info("Started VideoGrouperService")
        except Exception as exc:
            logger.warning("Could not start service: %s", exc)

        self.accept()
