"""
Visual e2e test for the onboarding wizard.

Launches the wizard and automatically clicks through both paths
(Manual and TTT) so you can watch the UI in real time. Mocks all
external services (TTT API, YouTube OAuth, camera connection).

Run: uv run python tests/e2e/run_onboarding_visual_test.py
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

# How long to pause on each page so the user can see it (ms)
PAGE_DELAY_MS = 1500


class WizardVisualTest:
    """Drives the onboarding wizard through a scripted sequence."""

    def __init__(self, wizard, steps, label=""):
        self.wizard = wizard
        self.steps = list(steps)
        self.step_index = 0
        self.label = label
        self.passed = True

    def start(self):
        print(f"\n{'=' * 60}")
        print(f"  VISUAL TEST: {self.label}")
        print(f"{'=' * 60}")
        self._schedule_next()

    def _schedule_next(self):
        if self.step_index < len(self.steps):
            QTimer.singleShot(PAGE_DELAY_MS, self._run_step)
        else:
            print(f"\n  -- {self.label}: ALL STEPS COMPLETED --\n")
            self.wizard.close()

    def _run_step(self):
        step = self.steps[self.step_index]
        self.step_index += 1

        page_name = self._get_page_name()
        step_desc = step.__doc__ or step.__name__
        print(
            f"  Step {self.step_index}/{len(self.steps)}: "
            f"[Page: {page_name}] {step_desc.strip()}"
        )

        try:
            step(self.wizard)
        except Exception as exc:
            print(f"  ** FAILED: {exc}")
            self.passed = False

        self._schedule_next()

    def _get_page_name(self):
        idx = self.wizard._stack.currentIndex()
        names = {
            0: "Welcome",
            1: "Path Choice",
            2: "TTT Sign-In",
            3: "TTT Restore",
            4: "Storage",
            5: "Camera",
            6: "YouTube",
            7: "NTFY",
            8: "Manual TTT",
            9: "Summary",
        }
        return names.get(idx, f"Unknown({idx})")


# ---------------------------------------------------------------------------
# Manual path steps
# ---------------------------------------------------------------------------


def manual_step_welcome(w):
    """Click Next on Welcome page"""
    assert w._stack.currentIndex() == 0, "Expected Welcome page"
    w._next_btn.click()


def manual_step_choose_manual(w):
    """Choose Manual Setup"""
    assert w._stack.currentIndex() == 1, "Expected Path Choice page"
    w._choose_path("manual")


def manual_step_set_storage(w):
    """Set storage path and click Next"""
    assert w._stack.currentIndex() == 4, "Expected Storage page"
    w._storage_path_input.setText(w._test_storage_path)
    w._next_btn.click()


def manual_step_configure_camera(w):
    """Fill camera fields and click Next"""
    assert w._stack.currentIndex() == 5, "Expected Camera page"
    w._camera_ip_input.setText("192.168.1.50")
    w._camera_user_input.setText("admin")
    w._camera_pass_input.setText("test-password")
    w._camera_type_combo.setCurrentText("reolink")
    w._next_btn.click()


def manual_step_skip_youtube(w):
    """Skip YouTube setup"""
    assert w._stack.currentIndex() == 6, "Expected YouTube page"
    w._skip_btn.click()


def manual_step_configure_ntfy(w):
    """Generate NTFY topic and click Next"""
    assert w._stack.currentIndex() == 7, "Expected NTFY page"
    w._generate_ntfy_topic()
    w._ntfy_enable_check.setChecked(True)
    w._next_btn.click()


def manual_step_skip_ttt(w):
    """Skip TTT (manual path optional step)"""
    assert w._stack.currentIndex() == 8, "Expected Manual TTT page"
    w._skip_btn.click()


def manual_step_verify_summary(w):
    """Verify summary page shows correct state"""
    assert w._stack.currentIndex() == 9, "Expected Summary page"
    summary = w._summary_label.text()
    assert "Camera" in summary, "Camera should appear in summary"
    assert "NTFY" in summary, "NTFY should appear in summary"
    print(f"    Summary:\n{summary}")
    next_steps = w._next_steps_label.text()
    if next_steps:
        print(f"    Next steps:\n{next_steps}")


def manual_step_finish(w):
    """Click Finish to save config"""
    w._finish()
    config_path = w.config_path
    assert config_path.exists(), f"Config should exist at {config_path}"
    print(f"    Config saved to: {config_path}")

    from video_grouper.utils.config import load_config

    config = load_config(config_path)
    assert config.setup.onboarding_completed, "Onboarding should be marked complete"
    assert len(config.cameras) == 1, "Should have 1 camera"
    assert config.cameras[0].device_ip == "192.168.1.50"
    assert config.ntfy.enabled, "NTFY should be enabled"
    assert config.ntfy.topic.startswith("soccer-cam-")
    print("    Config verified!")


# ---------------------------------------------------------------------------
# TTT path steps
# ---------------------------------------------------------------------------


def ttt_step_welcome(w):
    """Click Next on Welcome page"""
    assert w._stack.currentIndex() == 0, "Expected Welcome page"
    w._next_btn.click()


def ttt_step_choose_ttt(w):
    """Choose TTT Setup"""
    assert w._stack.currentIndex() == 1, "Expected Path Choice page"
    w._choose_path("ttt")


def ttt_step_fill_signin(w):
    """Fill TTT sign-in form"""
    assert w._stack.currentIndex() == 2, "Expected TTT Sign-In page"
    w._ttt_email_input.setText("testuser@example.com")
    w._ttt_password_input.setText("test-password-123")


def ttt_step_simulate_signin(w):
    """Simulate successful TTT sign-in (mock)"""
    # Simulate what the background thread does on success
    w._ttt_client = MagicMock()
    w._ttt_email = "testuser@example.com"
    w._ttt_password = "test-password-123"
    w._ttt_enabled = True
    w._ttt_teams = [
        {"camera_manager_id": "cm-1", "team_id": "t-1", "team_name": "Eagles"},
        {"camera_manager_id": "cm-2", "team_id": "t-2", "team_name": "Falcons"},
    ]
    w._ttt_device_config = {
        "camera_ip": "10.0.0.5",
        "camera_username": "admin",
        "camera_password": "restored-cam-pass",
        "ntfy_topic": "soccer-cam-restored",
        "ntfy_server_url": "https://ntfy.sh",
        "youtube_configured": False,
        "gcp_project_id": None,
    }
    w._ttt_signin_status.setText("Signed in -- 2 team(s): Eagles, Falcons")
    print("    Simulated TTT login: 2 teams (Eagles, Falcons)")
    print("    Existing device config found in DB")


def ttt_step_click_next_to_restore(w):
    """Click Next to check for existing config"""
    w._next_btn.click()


def ttt_step_verify_restore_page(w):
    """Verify restore page shows stored config"""
    assert w._stack.currentIndex() == 3, "Expected TTT Restore page"
    details = w._restore_details.text()
    assert "10.0.0.5" in details, "Should show stored camera IP"
    assert "soccer-cam-restored" in details, "Should show stored NTFY topic"
    print(f"    Restore details:\n{details}")


def ttt_step_restore(w):
    """Click Restore Settings"""
    w._restore_ttt_config()


def ttt_step_verify_restored_summary(w):
    """Verify summary after restore"""
    assert w._stack.currentIndex() == 9, "Expected Summary page"
    summary = w._summary_label.text()
    assert "Camera" in summary
    assert "NTFY" in summary
    assert "Team Tech Tools" in summary
    print(f"    Summary:\n{summary}")


def ttt_step_finish(w):
    """Click Finish to save restored config"""
    # Mock the TTT save call
    w._ttt_client.save_device_config = MagicMock()
    w._finish()

    config_path = w.config_path
    assert config_path.exists(), f"Config should exist at {config_path}"

    from video_grouper.utils.config import load_config

    config = load_config(config_path)
    assert config.setup.onboarding_completed
    assert config.ntfy.enabled
    assert config.ntfy.topic == "soccer-cam-restored"
    assert config.ttt.enabled
    assert config.ttt.email == "testuser@example.com"
    print("    Config saved and verified!")
    print(
        f"    TTT save_device_config called: {w._ttt_client.save_device_config.called}"
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_test(label, steps):
    """Run one visual test sequence."""
    app = QApplication.instance() or QApplication(sys.argv)

    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = Path(tmp_dir) / "config.ini"

        from video_grouper.tray.onboarding_wizard import OnboardingWizard

        wizard = OnboardingWizard(config_path)
        wizard._test_storage_path = tmp_dir
        wizard.setWindowTitle(f"Soccer-Cam Setup  --  [{label}]")
        wizard.show()
        wizard.raise_()
        wizard.activateWindow()

        test = WizardVisualTest(wizard, steps, label)
        # Start after a short delay so window is fully rendered
        QTimer.singleShot(500, test.start)

        app.exec()

    return test.passed


def main():
    print("\n" + "=" * 60)
    print("  ONBOARDING WIZARD VISUAL E2E TESTS")
    print("=" * 60)
    print(f"  Page delay: {PAGE_DELAY_MS}ms per step")
    print("  Watch the wizard window as it clicks through each page.\n")

    results = []

    # Test 1: Manual path
    passed = run_test(
        "Manual Path",
        [
            manual_step_welcome,
            manual_step_choose_manual,
            manual_step_set_storage,
            manual_step_configure_camera,
            manual_step_skip_youtube,
            manual_step_configure_ntfy,
            manual_step_skip_ttt,
            manual_step_verify_summary,
            manual_step_finish,
        ],
    )
    results.append(("Manual Path", passed))

    # Test 2: TTT path with restore
    passed = run_test(
        "TTT Path (Restore)",
        [
            ttt_step_welcome,
            ttt_step_choose_ttt,
            ttt_step_fill_signin,
            ttt_step_simulate_signin,
            ttt_step_click_next_to_restore,
            ttt_step_verify_restore_page,
            ttt_step_restore,
            ttt_step_verify_restored_summary,
            ttt_step_finish,
        ],
    )
    results.append(("TTT Path (Restore)", passed))

    # Summary
    print("\n" + "=" * 60)
    print("  RESULTS")
    print("=" * 60)
    all_passed = True
    for name, passed in results:
        status = "PASSED" if passed else "FAILED"
        print(f"  {status}: {name}")
        if not passed:
            all_passed = False

    print("=" * 60)
    if all_passed:
        print("  ALL VISUAL TESTS PASSED")
    else:
        print("  SOME TESTS FAILED")
    print("=" * 60 + "\n")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
