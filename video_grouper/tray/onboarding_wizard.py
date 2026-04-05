"""Onboarding setup wizard for Soccer-Cam.

Guides first-time users through configuring storage, camera, YouTube,
NTFY, and Team Tech Tools.  Two paths: TTT (auto-configured) and Manual.
"""

import asyncio
import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
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
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QIcon

from video_grouper.utils.config import (
    create_default_config,
    save_config,
    load_config,
)
from video_grouper.utils.paths import get_shared_data_path
from video_grouper.utils.youtube_upload import (
    authenticate_youtube_embedded,
    get_youtube_paths,
)

logger = logging.getLogger(__name__)

# TTT infrastructure defaults (not secrets -- Supabase anon keys are public)
TTT_SUPABASE_URL = "https://lfnqnfbkresbbprgribe.supabase.co"
TTT_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imxm"
    "bnFuZmJrcmVzYmJwcmdyaWJlIiwicm9sZSI6"
    "ImFub24iLCJpYXQiOjE3MzMxNjI5NjYsImV4"
    "cCI6MjA0ODczODk2Nn0.bVPADfD5v6E5a7Y3"
    "2BGeJJYuiVqsMnyexVj0KNpblh4"
)
TTT_API_BASE_URL = "https://team-tech-tools.vercel.app"


class OnboardingWizard(QDialog):
    """First-run setup wizard for Soccer-Cam."""

    # Page indices for TTT path
    PAGE_WELCOME = 0
    PAGE_PATH_CHOICE = 1
    # TTT pages
    PAGE_TTT_SIGNIN = 2
    PAGE_TTT_RESTORE = 3
    # Shared pages (both paths use these, but at different stack indices)
    PAGE_STORAGE = 4
    PAGE_CAMERA = 5
    PAGE_YOUTUBE = 6
    PAGE_NTFY = 7
    # Manual-only page
    PAGE_MANUAL_TTT = 8
    PAGE_SUMMARY = 9

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
        self._stack.addWidget(self._build_youtube_page())  # 6
        self._stack.addWidget(self._build_ntfy_page())  # 7
        self._stack.addWidget(self._build_manual_ttt_page())  # 8
        self._stack.addWidget(self._build_summary_page())  # 9

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
        self._camera_ip_input.setPlaceholderText("192.168.1.100")
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

        # Advanced NTFY settings (collapsed by default for TTT users)
        self._advanced_ntfy_group = QGroupBox("Advanced: Notification Settings")
        self._advanced_ntfy_group.setCheckable(False)
        self._advanced_ntfy_group.setVisible(False)
        adv_layout = QVBoxLayout(self._advanced_ntfy_group)

        self._advanced_ntfy_toggle = QPushButton("Change notification topic...")
        self._advanced_ntfy_toggle.setFlat(True)
        self._advanced_ntfy_toggle.setStyleSheet(
            "text-align: left; color: #0066cc; text-decoration: underline;"
        )
        self._advanced_ntfy_toggle.clicked.connect(self._toggle_advanced_ntfy)
        layout.addWidget(self._advanced_ntfy_toggle)

        adv_form = QFormLayout()
        self._summary_ntfy_topic_input = QLineEdit()
        self._summary_ntfy_topic_input.setPlaceholderText("soccer-cam-xxxxxxxx")
        adv_form.addRow("NTFY Topic:", self._summary_ntfy_topic_input)
        self._summary_ntfy_server_input = QLineEdit("https://ntfy.sh")
        adv_form.addRow("Server URL:", self._summary_ntfy_server_input)
        adv_layout.addLayout(adv_form)

        adv_note = QLabel(
            "To receive notifications on your phone, install the ntfy app "
            "and subscribe to this topic."
        )
        adv_note.setWordWrap(True)
        adv_layout.addWidget(adv_note)

        layout.addWidget(self._advanced_ntfy_group)

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
            seq.extend(
                [
                    self.PAGE_STORAGE,
                    self.PAGE_CAMERA,
                    self.PAGE_YOUTUBE,
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
                self.PAGE_YOUTUBE,
                self.PAGE_NTFY,
                self.PAGE_MANUAL_TTT,
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

        self._nav_history.append(self._stack.currentIndex())
        self._stack.setCurrentIndex(page_index)
        self._update_nav()

    def _go_back(self):
        if self._nav_history:
            prev = self._nav_history.pop()
            self._stack.setCurrentIndex(prev)
            self._update_nav()

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

        # For all other pages, advance through sequence
        seq = self._get_page_sequence()
        if current in seq:
            idx = seq.index(current)
            if idx + 1 < len(seq):
                self._navigate_to(seq[idx + 1])
            return

        # Summary -> finish
        if current == self.PAGE_SUMMARY:
            self._finish()
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

        elif page_index == self.PAGE_NTFY:
            self._ntfy_topic = self._ntfy_topic_input.text().strip()
            self._ntfy_server_url = self._ntfy_server_input.text().strip()
            self._ntfy_enabled = self._ntfy_enable_check.isChecked() and bool(
                self._ntfy_topic
            )

        elif page_index == self.PAGE_SUMMARY:
            # Read back advanced NTFY edits if the user changed them
            if self._mode == "ttt" and self._advanced_ntfy_group.isVisible():
                topic = self._summary_ntfy_topic_input.text().strip()
                server = self._summary_ntfy_server_input.text().strip()
                if topic:
                    self._ntfy_topic = topic
                if server:
                    self._ntfy_server_url = server

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

                def on_success():
                    self._ttt_client = client
                    self._ttt_email = email
                    self._ttt_password = password
                    self._ttt_enabled = True
                    self._ttt_teams = teams
                    self._ttt_device_config = device_config
                    self._ttt_signin_btn.setEnabled(True)
                    team_names = [t.get("team_name", "?") for t in teams]
                    self._ttt_signin_status.setText(
                        f"Signed in -- {len(teams)} team(s): {', '.join(team_names)}"
                    )

                    # Auto-populate NTFY from TTT device config
                    if device_config and device_config.get("ntfy_topic"):
                        self._ntfy_topic = device_config["ntfy_topic"]
                        self._ntfy_server_url = device_config.get(
                            "ntfy_server_url", "https://ntfy.sh"
                        )
                        self._ntfy_enabled = True

                QTimer.singleShot(0, on_success)

            except Exception as exc:
                logger.error("TTT sign-in failed: %s", exc)

                def on_error(err=str(exc)):
                    self._ttt_signin_btn.setEnabled(True)
                    self._ttt_signin_status.setText(f"Sign-in failed: {err}")

                QTimer.singleShot(0, on_error)

        thread = threading.Thread(target=do_login, daemon=True)
        thread.start()

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

                QTimer.singleShot(0, on_success)

            except Exception as exc:
                logger.error("TTT sign-in failed: %s", exc)

                def on_error(err=str(exc)):
                    self._manual_ttt_signin_btn.setEnabled(True)
                    self._manual_ttt_status.setText(f"Sign-in failed: {err}")

                QTimer.singleShot(0, on_error)

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
        self._restore_details.setText("\n".join(lines))

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

        self._populate_summary()
        self._navigate_to(self.PAGE_SUMMARY)

    def _start_fresh_ttt(self):
        """Ignore stored config and proceed through setup steps."""
        self._ttt_device_config = None
        self._navigate_to(self.PAGE_STORAGE)

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    def _browse_storage(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select Storage Directory", self._storage_path
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

                QTimer.singleShot(0, on_result)

            except Exception as exc:
                logger.error("Camera test failed: %s", exc)

                def on_error(err=str(exc)):
                    self._camera_test_btn.setEnabled(True)
                    self._camera_test_status.setText(f"Error: {err}")

                QTimer.singleShot(0, on_error)

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

                QTimer.singleShot(0, on_result)
            except Exception as exc:
                logger.error("Password change failed: %s", exc)

                def on_error(err=str(exc)):
                    self._cam_set_pass_btn.setEnabled(True)
                    self._cam_pass_status.setText(f"Error: {err}")

                QTimer.singleShot(0, on_error)

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

                QTimer.singleShot(0, on_result)
            except Exception as exc:
                logger.error("Apply settings failed: %s", exc)

                def on_error(err=str(exc)):
                    self._cam_apply_btn.setEnabled(True)
                    self._cam_config_status.setText(f"Error: {err}")

                QTimer.singleShot(0, on_error)

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

        _, token_file = get_youtube_paths(storage_path)

        self._yt_auth_btn.setEnabled(False)
        self._yt_auth_status.setText("Opening browser for sign-in...")

        def do_auth():
            try:
                success, message = authenticate_youtube_embedded(token_file)

                def on_done(ok=success, msg=message):
                    self._yt_auth_btn.setEnabled(True)
                    if ok:
                        self._youtube_enabled = True
                        self._youtube_authenticated = True
                        self._yt_auth_status.setText("Authorized")
                    else:
                        self._yt_auth_status.setText(f"Failed: {msg}")

                QTimer.singleShot(0, on_done)

            except Exception as exc:
                logger.error("YouTube auth failed: %s", exc)

                def on_error(err=str(exc)):
                    self._yt_auth_btn.setEnabled(True)
                    self._yt_auth_status.setText(f"Error: {err}")

                QTimer.singleShot(0, on_error)

        thread = threading.Thread(target=do_auth, daemon=True)
        thread.start()

    # ------------------------------------------------------------------
    # NTFY
    # ------------------------------------------------------------------

    def _generate_ntfy_topic(self):
        topic = f"soccer-cam-{secrets.token_hex(4)}"
        self._ntfy_topic_input.setText(topic)

    def _toggle_advanced_ntfy(self):
        """Toggle visibility of the advanced NTFY settings on summary page."""
        visible = not self._advanced_ntfy_group.isVisible()
        self._advanced_ntfy_group.setVisible(visible)
        self._advanced_ntfy_toggle.setText(
            "Hide notification settings" if visible else "Change notification topic..."
        )

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

        # Advanced NTFY settings on summary page (for TTT users who skipped NTFY page)
        if self._mode == "ttt" and self._ntfy_enabled:
            self._summary_ntfy_topic_input.setText(self._ntfy_topic)
            self._summary_ntfy_server_input.setText(self._ntfy_server_url)
            self._advanced_ntfy_toggle.setVisible(True)
            self._advanced_ntfy_group.setVisible(False)  # collapsed by default
        else:
            self._advanced_ntfy_toggle.setVisible(False)
            self._advanced_ntfy_group.setVisible(False)

    def _finish(self):
        """Save configuration and close the wizard."""
        # Collect data from current page
        self._collect_page_data(self._stack.currentIndex())

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

        # NTFY
        config.ntfy.enabled = self._ntfy_enabled
        if self._ntfy_topic:
            config.ntfy.topic = self._ntfy_topic
        if self._ntfy_server_url:
            config.ntfy.server_url = self._ntfy_server_url
        if self._ntfy_enabled:
            config.ntfy.response_service = True

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
                self._ttt_client.save_device_config(device_data)
                logger.info("Device config saved to TTT")
            except Exception as exc:
                logger.warning("Failed to save device config to TTT: %s", exc)

        self.accept()
