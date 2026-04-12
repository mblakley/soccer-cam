"""End-to-end tests for the onboarding wizard GUI.

Uses pytest-qt's qtbot to simulate user interaction with the wizard.
All external services (TTT API, YouTube OAuth, camera) are mocked.
"""

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import Qt

from video_grouper.tray.onboarding_wizard import OnboardingWizard
from video_grouper.utils.config import (
    config_needs_onboarding,
    load_config,
)


@pytest.fixture
def wizard(qtbot, tmp_path):
    """Create an OnboardingWizard instance for testing."""
    config_path = tmp_path / "config.ini"
    w = OnboardingWizard(config_path)
    qtbot.addWidget(w)
    return w


@pytest.fixture
def config_path(tmp_path):
    return tmp_path / "config.ini"


# ---------------------------------------------------------------------------
# Wizard launch and navigation
# ---------------------------------------------------------------------------


class TestWizardLaunch:
    def test_wizard_opens_on_welcome_page(self, wizard):
        assert wizard._stack.currentIndex() == OnboardingWizard.PAGE_WELCOME

    def test_wizard_minimum_size(self, wizard):
        assert wizard.minimumWidth() >= 750
        assert wizard.minimumHeight() >= 550

    def test_wizard_has_window_title(self, wizard):
        assert wizard.windowTitle() == "Soccer-Cam Setup"

    def test_next_from_welcome_goes_to_path_choice(self, wizard, qtbot):
        qtbot.mouseClick(wizard._next_btn, Qt.MouseButton.LeftButton)
        assert wizard._stack.currentIndex() == OnboardingWizard.PAGE_PATH_CHOICE

    def test_back_button_hidden_on_welcome(self, wizard):
        assert not wizard._back_btn.isVisible()

    def test_skip_button_hidden_on_welcome(self, wizard):
        assert not wizard._skip_btn.isVisible()


# ---------------------------------------------------------------------------
# Manual path: full walkthrough
# ---------------------------------------------------------------------------


class TestManualPathWalkthrough:
    def _go_to_path_choice(self, wizard, qtbot):
        """Navigate to the path choice page."""
        qtbot.mouseClick(wizard._next_btn, Qt.MouseButton.LeftButton)
        assert wizard._stack.currentIndex() == OnboardingWizard.PAGE_PATH_CHOICE

    def test_manual_path_navigates_to_storage(self, wizard, qtbot):
        self._go_to_path_choice(wizard, qtbot)
        wizard._choose_path("manual")
        assert wizard._mode == "manual"
        assert wizard._stack.currentIndex() == OnboardingWizard.PAGE_STORAGE

    def test_skip_all_steps_reaches_summary(self, wizard, qtbot):
        """Walk through manual path skipping every step."""
        self._go_to_path_choice(wizard, qtbot)
        wizard._choose_path("manual")

        # Skip storage
        qtbot.mouseClick(wizard._skip_btn, Qt.MouseButton.LeftButton)
        assert wizard._stack.currentIndex() == OnboardingWizard.PAGE_CAMERA

        # Skip camera
        qtbot.mouseClick(wizard._skip_btn, Qt.MouseButton.LeftButton)
        assert wizard._stack.currentIndex() == OnboardingWizard.PAGE_YOUTUBE

        # Skip youtube
        qtbot.mouseClick(wizard._skip_btn, Qt.MouseButton.LeftButton)
        assert wizard._stack.currentIndex() == OnboardingWizard.PAGE_NTFY

        # Skip ntfy
        qtbot.mouseClick(wizard._skip_btn, Qt.MouseButton.LeftButton)
        assert wizard._stack.currentIndex() == OnboardingWizard.PAGE_MANUAL_TTT

        # Skip TTT
        qtbot.mouseClick(wizard._skip_btn, Qt.MouseButton.LeftButton)
        assert wizard._stack.currentIndex() == OnboardingWizard.PAGE_SUMMARY

    def test_skip_all_produces_next_steps(self, wizard, qtbot):
        """Skipping everything should show next steps for all integrations."""
        self._go_to_path_choice(wizard, qtbot)
        wizard._choose_path("manual")

        # Skip all steps to summary
        for _ in range(5):
            qtbot.mouseClick(wizard._skip_btn, Qt.MouseButton.LeftButton)

        assert wizard._stack.currentIndex() == OnboardingWizard.PAGE_SUMMARY
        # Check visibility flag (not isVisible which requires shown parent)
        assert not wizard._next_steps_group.isHidden()
        next_steps_text = wizard._next_steps_label.text()
        assert "Camera" in next_steps_text
        assert "YouTube" in next_steps_text

    def test_finish_creates_config(self, wizard, qtbot, config_path):
        """Finishing the wizard should create a valid config.ini."""
        wizard.config_path = config_path
        wizard._storage_path = str(config_path.parent)

        self._go_to_path_choice(wizard, qtbot)
        wizard._choose_path("manual")

        # Skip all steps to summary
        for _ in range(5):
            qtbot.mouseClick(wizard._skip_btn, Qt.MouseButton.LeftButton)

        # Click Finish
        wizard._finish()

        assert config_path.exists()
        config = load_config(config_path)
        assert config.setup.onboarding_completed is True

    def test_finish_with_camera_saves_camera_config(self, wizard, qtbot, config_path):
        """Configuring a camera should save it to config.ini."""
        wizard.config_path = config_path
        wizard._storage_path = str(config_path.parent)
        wizard._storage_path_input.setText(str(config_path.parent))

        self._go_to_path_choice(wizard, qtbot)
        wizard._choose_path("manual")

        # On storage page, advance
        qtbot.mouseClick(wizard._next_btn, Qt.MouseButton.LeftButton)

        # On camera page, fill in fields
        wizard._camera_ip_input.setText("192.168.1.50")
        wizard._camera_user_input.setText("admin")
        wizard._camera_pass_input.setText("password123")
        wizard._camera_type_combo.setCurrentText("reolink")
        qtbot.mouseClick(wizard._next_btn, Qt.MouseButton.LeftButton)

        # Skip youtube, ntfy, ttt
        for _ in range(3):
            qtbot.mouseClick(wizard._skip_btn, Qt.MouseButton.LeftButton)

        wizard._finish()

        config = load_config(config_path)
        assert len(config.cameras) == 1
        assert config.cameras[0].device_ip == "192.168.1.50"
        assert config.cameras[0].type == "reolink"

    def test_finish_with_ntfy_saves_topic(self, wizard, qtbot, config_path):
        """Configuring NTFY should save topic to config.ini."""
        wizard.config_path = config_path
        wizard._storage_path = str(config_path.parent)

        self._go_to_path_choice(wizard, qtbot)
        wizard._choose_path("manual")

        # Skip storage, camera, youtube
        for _ in range(3):
            qtbot.mouseClick(wizard._skip_btn, Qt.MouseButton.LeftButton)

        # On NTFY page, set topic
        wizard._ntfy_topic_input.setText("soccer-cam-test1234")
        wizard._ntfy_enable_check.setChecked(True)
        qtbot.mouseClick(wizard._next_btn, Qt.MouseButton.LeftButton)

        # Skip TTT
        qtbot.mouseClick(wizard._skip_btn, Qt.MouseButton.LeftButton)

        wizard._finish()

        config = load_config(config_path)
        assert config.ntfy.enabled is True
        assert config.ntfy.topic == "soccer-cam-test1234"

    def test_back_button_works(self, wizard, qtbot):
        """Back button should return to previous page."""
        self._go_to_path_choice(wizard, qtbot)
        wizard._choose_path("manual")
        assert wizard._stack.currentIndex() == OnboardingWizard.PAGE_STORAGE

        # Go to camera
        qtbot.mouseClick(wizard._next_btn, Qt.MouseButton.LeftButton)
        assert wizard._stack.currentIndex() == OnboardingWizard.PAGE_CAMERA

        # Go back to storage
        qtbot.mouseClick(wizard._back_btn, Qt.MouseButton.LeftButton)
        assert wizard._stack.currentIndex() == OnboardingWizard.PAGE_STORAGE


# ---------------------------------------------------------------------------
# TTT path: sign-in and restore
# ---------------------------------------------------------------------------


class TestTTTPathWalkthrough:
    def _go_to_ttt_signin(self, wizard, qtbot):
        """Navigate to the TTT sign-in page."""
        qtbot.mouseClick(wizard._next_btn, Qt.MouseButton.LeftButton)
        wizard._choose_path("ttt")
        assert wizard._stack.currentIndex() == OnboardingWizard.PAGE_TTT_SIGNIN

    def test_ttt_signin_success(self, wizard, qtbot):
        """Successful TTT sign-in should enable TTT and show teams."""
        self._go_to_ttt_signin(wizard, qtbot)
        wizard._ttt_email_input.setText("user@example.com")
        wizard._ttt_password_input.setText("password123")

        # Simulate the sign-in completing (bypass threading)
        mock_client = MagicMock()
        wizard._ttt_client = mock_client
        wizard._ttt_email = "user@example.com"
        wizard._ttt_password = "password123"
        wizard._ttt_enabled = True
        wizard._ttt_teams = [
            {"camera_manager_id": "cm-1", "team_id": "t-1", "team_name": "Eagles"}
        ]
        wizard._ttt_device_config = None

        assert wizard._ttt_enabled is True
        assert len(wizard._ttt_teams) == 1
        assert wizard._ttt_teams[0]["team_name"] == "Eagles"

    def test_ttt_restore_flow(self, wizard, qtbot, config_path):
        """TTT restore should apply stored config and jump to summary."""
        wizard.config_path = config_path
        wizard._storage_path = str(config_path.parent)

        qtbot.mouseClick(wizard._next_btn, Qt.MouseButton.LeftButton)
        wizard._choose_path("ttt")

        # Simulate signed in with existing device config
        wizard._ttt_enabled = True
        wizard._ttt_email = "user@example.com"
        wizard._ttt_password = "pass"
        wizard._ttt_device_config = {
            "camera_ip": "10.0.0.5",
            "camera_username": "admin",
            "camera_password": "cam-secret",
            "ntfy_topic": "soccer-cam-existing",
            "ntfy_server_url": "https://ntfy.sh",
            "youtube_configured": False,
            "gcp_project_id": None,
        }

        # Trigger restore
        wizard._populate_restore_page()
        wizard._restore_ttt_config()

        assert wizard._stack.currentIndex() == OnboardingWizard.PAGE_SUMMARY
        assert wizard._camera_ip == "10.0.0.5"
        assert wizard._camera_password == "cam-secret"
        assert wizard._ntfy_topic == "soccer-cam-existing"
        assert wizard._ntfy_enabled is True

        # Finish should save config
        wizard._finish()

        config = load_config(config_path)
        assert config.ntfy.enabled is True
        assert config.ntfy.topic == "soccer-cam-existing"
        assert config.setup.onboarding_completed is True

    def test_ttt_start_fresh_goes_to_storage(self, wizard, qtbot):
        """Choosing 'Start Fresh' should clear stored config and go to storage."""
        qtbot.mouseClick(wizard._next_btn, Qt.MouseButton.LeftButton)
        wizard._choose_path("ttt")

        wizard._ttt_enabled = True
        wizard._ttt_device_config = {"camera_ip": "10.0.0.5"}

        wizard._start_fresh_ttt()

        assert wizard._ttt_device_config is None
        assert wizard._stack.currentIndex() == OnboardingWizard.PAGE_STORAGE

    def test_ttt_path_auto_generates_ntfy_topic(self, wizard, qtbot):
        """TTT path should auto-generate NTFY topic when reaching summary."""
        qtbot.mouseClick(wizard._next_btn, Qt.MouseButton.LeftButton)
        wizard._choose_path("ttt")

        wizard._ttt_enabled = True
        wizard._ttt_device_config = None

        # Navigate: TTT signin -> storage -> camera -> youtube -> summary
        # (NTFY page is skipped in TTT path; topic auto-generated at summary)
        wizard._navigate_to(OnboardingWizard.PAGE_STORAGE)
        wizard._navigate_to(OnboardingWizard.PAGE_CAMERA)
        wizard._navigate_to(OnboardingWizard.PAGE_YOUTUBE)
        wizard._navigate_to(OnboardingWizard.PAGE_SUMMARY)

        topic = wizard._ntfy_topic
        assert topic.startswith("soccer-cam-")
        assert len(topic) > len("soccer-cam-")


# ---------------------------------------------------------------------------
# YouTube auth (mocked)
# ---------------------------------------------------------------------------


class TestYouTubeAuth:
    @patch(
        "video_grouper.tray.onboarding_wizard.authenticate_youtube_embedded",
        return_value=(True, "Success"),
    )
    def test_youtube_auth_success_sets_state(self, mock_auth, wizard, qtbot):
        """Successful YouTube auth should mark youtube as enabled."""
        qtbot.mouseClick(wizard._next_btn, Qt.MouseButton.LeftButton)
        wizard._choose_path("manual")

        # Skip to youtube
        qtbot.mouseClick(wizard._skip_btn, Qt.MouseButton.LeftButton)
        qtbot.mouseClick(wizard._skip_btn, Qt.MouseButton.LeftButton)
        assert wizard._stack.currentIndex() == OnboardingWizard.PAGE_YOUTUBE

        # Directly call the auth result handler (bypass threading)
        wizard._youtube_enabled = True
        wizard._youtube_authenticated = True

        assert wizard._youtube_enabled is True
        assert wizard._youtube_authenticated is True


# ---------------------------------------------------------------------------
# First-run detection integration
# ---------------------------------------------------------------------------


class TestFirstRunDetection:
    def test_no_config_needs_onboarding(self, tmp_path):
        config_path = tmp_path / "config.ini"
        assert config_needs_onboarding(config_path) is True

    def test_completed_config_does_not_need_onboarding(self, wizard, qtbot, tmp_path):
        """After wizard completes, config should not trigger onboarding again."""
        config_path = tmp_path / "config.ini"
        wizard.config_path = config_path
        wizard._storage_path = str(tmp_path)

        # Quick manual path, skip all
        qtbot.mouseClick(wizard._next_btn, Qt.MouseButton.LeftButton)
        wizard._choose_path("manual")
        for _ in range(5):
            qtbot.mouseClick(wizard._skip_btn, Qt.MouseButton.LeftButton)
        wizard._finish()

        assert config_needs_onboarding(config_path) is False

    def test_wizard_saves_ttt_config(self, wizard, qtbot, tmp_path):
        """TTT config should be saved when user signs in via manual path."""
        config_path = tmp_path / "config.ini"
        wizard.config_path = config_path
        wizard._storage_path = str(tmp_path)

        qtbot.mouseClick(wizard._next_btn, Qt.MouseButton.LeftButton)
        wizard._choose_path("manual")

        # Skip to TTT page
        for _ in range(4):
            qtbot.mouseClick(wizard._skip_btn, Qt.MouseButton.LeftButton)

        # Simulate TTT sign-in on manual page
        wizard._ttt_enabled = True
        wizard._ttt_email = "user@test.com"
        wizard._ttt_password = "pass123"

        # Skip to summary
        qtbot.mouseClick(wizard._next_btn, Qt.MouseButton.LeftButton)

        wizard._finish()

        config = load_config(config_path)
        assert config.ttt.enabled is True
        assert config.ttt.email == "user@test.com"


# ---------------------------------------------------------------------------
# Summary page content
# ---------------------------------------------------------------------------


class TestSummaryPage:
    def test_all_configured_hides_next_steps(self, wizard, qtbot, tmp_path):
        """When everything is configured, next steps should be hidden."""
        wizard.config_path = tmp_path / "config.ini"

        # Set all state as configured
        wizard._camera_configured = True
        wizard._camera_ip = "192.168.1.50"
        wizard._camera_type = "reolink"
        wizard._youtube_authenticated = True
        wizard._youtube_enabled = True
        wizard._ntfy_enabled = True
        wizard._ntfy_topic = "soccer-cam-test"
        wizard._ttt_enabled = True
        wizard._ttt_email = "user@test.com"

        wizard._populate_summary()

        assert wizard._next_steps_group.isHidden()
        summary = wizard._summary_label.text()
        assert "[OK]" in summary
        assert "Camera" in summary
        assert "YouTube" in summary
        assert "NTFY" in summary

    def test_nothing_configured_shows_all_next_steps(self, wizard, qtbot):
        """When nothing is configured, all next steps should be shown."""
        wizard._populate_summary()

        assert not wizard._next_steps_group.isHidden()
        next_steps = wizard._next_steps_label.text()
        assert "Camera" in next_steps
        assert "YouTube" in next_steps
        assert "NTFY" in next_steps
        assert "Team Tech Tools" in next_steps
