"""Automated visual demo of the onboarding wizard camera setup.

Drives the PyQt6 wizard UI programmatically with delays so you can
watch each step. Runs both the TTT restore path and manual camera setup.

Prerequisites:
    # Camera simulators (factory default passwords)
    docker run -d --name reolink-sim -p 8180:80 -e CAMERA_TYPE=reolink -e USERNAME=admin -e PASSWORD=admin camera-simulator:local
    docker run -d --name dahua-sim -p 8181:80 -e CAMERA_TYPE=dahua -e USERNAME=admin -e PASSWORD=admin camera-simulator:local

    # TTT backend (local Supabase + FastAPI)
    # Should already be running from team-tech-tools docker compose

Usage:
    uv run python demo_wizard_ui.py
"""

import sys
import tempfile
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QPushButton

SCREENSHOT_DIR = Path("docs/screenshots/wizard")
CAPTURE_SCREENSHOTS = "--screenshots" in sys.argv

# Patch the wizard to use local TTT infrastructure before importing
import video_grouper.tray.onboarding_wizard as wizard_module  # noqa: E402

wizard_module.TTT_SUPABASE_URL = "http://localhost:54321"
wizard_module.TTT_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9s"
    "ZSI6ImFub24iLCJleHAiOjE5ODM4MTI5OTZ9."
    "CRXP1A7WOeoJeXxjNni43kdQwgnWNReilDMblYTn_I0"
)
wizard_module.TTT_API_BASE_URL = "http://localhost:8000"

from video_grouper.tray.onboarding_wizard import OnboardingWizard  # noqa: E402

STEP_DELAY = 1800  # ms between each automated action


class WizardDriver:
    """Drives the wizard UI through the full onboarding flow."""

    def __init__(self, wizard: OnboardingWizard):
        self.wizard = wizard
        self.steps: list[tuple[str, callable]] = []
        self._build_steps()
        self._step_index = 0

    def _build_steps(self):
        w = self.wizard

        # ── Welcome ─────────────────────────────────────────────
        self.steps.append(("Click Next on Welcome", self._click_next))

        # ── Path Choice -> TTT ──────────────────────────────────
        def pick_ttt():
            for child in w._stack.currentWidget().findChildren(QPushButton):
                if (
                    "tech tools" in child.text().lower()
                    or "ttt" in child.text().lower()
                ):
                    child.click()
                    return
            # Look for the first prominent button that isn't "manual"
            for child in w._stack.currentWidget().findChildren(QPushButton):
                if "manual" not in child.text().lower():
                    child.click()
                    return

        self.steps.append(("Select 'Sign in with Team Tech Tools'", pick_ttt))

        # ── TTT Sign In ────────────────────────────────────────
        def fill_ttt_creds():
            w._ttt_email_input.setText("mark.blakley@gmail.com")
            w._ttt_password_input.setText("password123")

        self.steps.append(("Fill TTT credentials", fill_ttt_creds))

        def click_signin():
            w._ttt_signin_btn.click()

        self.steps.append(("Click Sign In", click_signin))
        self.steps.append(("Waiting for TTT sign-in...", lambda: None))
        self.steps.append(("Waiting for team + device config fetch...", lambda: None))

        # After sign-in, wizard auto-navigates. If device config found -> restore page
        # If not -> storage page. We need to click Next to proceed.
        self.steps.append(("Checking for saved device config...", lambda: None))

        # ── TTT Restore (if device config exists) ──────────────
        def handle_restore_or_continue():
            # Check if we're on the restore page
            if w._stack.currentIndex() == w.PAGE_TTT_RESTORE:
                print(
                    "    -> Previous config found! Choosing 'Start Fresh' to show full setup"
                )
                # Click "Start Fresh" to go through the full flow
                for child in w._stack.currentWidget().findChildren(QPushButton):
                    if "fresh" in child.text().lower():
                        child.click()
                        return
            # Otherwise we're already past it
            print("    -> No saved config, proceeding with setup")

        self.steps.append(("Handle restore page", handle_restore_or_continue))

        # ── Storage -> skip ─────────────────────────────────────
        self.steps.append(("Skip storage setup", self._click_skip))

        # ── Camera Setup: Reolink ───────────────────────────────
        def fill_reolink():
            w._camera_type_combo.setCurrentText("reolink")
            w._camera_ip_input.setText("127.0.0.1:8180")
            w._camera_user_input.setText("admin")
            w._camera_pass_input.setText("admin")

        self.steps.append(
            ("Fill Reolink camera (127.0.0.1:8180, admin/admin)", fill_reolink)
        )

        self.steps.append(("Click Connect", lambda: w._camera_test_btn.click()))
        self.steps.append(("Connecting to camera...", lambda: None))
        self.steps.append(("Reading device info + current settings...", lambda: None))
        self.steps.append(("Reviewing current settings...", lambda: None))

        # ── Password (factory defaults detected) ────────────────
        def set_password():
            if w._cam_phase_b.isVisible():
                w._cam_new_pass.setText("Soccer2026!")
                w._cam_confirm_pass.setText("Soccer2026!")

        self.steps.append(("Enter new password: Soccer2026!", set_password))

        def click_set_password():
            if w._cam_phase_b.isVisible():
                w._cam_set_pass_btn.click()

        self.steps.append(("Click Set Password", click_set_password))
        self.steps.append(("Changing password...", lambda: None))

        # ── Apply Settings ──────────────────────────────────────
        def click_apply():
            if w._cam_phase_c.isVisible():
                w._cam_apply_btn.click()

        self.steps.append(("Click Apply Optimal Settings", click_apply))
        self.steps.append(("Applying recording + NTP + timezone...", lambda: None))
        self.steps.append(("Applying encoding + DST + static IP...", lambda: None))
        self.steps.append(("Reviewing results...", lambda: None))
        self.steps.append(("All settings applied!", lambda: None))

        # ── Continue through remaining pages ────────────────────
        self.steps.append(("Click Next (camera done)", self._click_next))
        self.steps.append(("Skip YouTube", self._click_skip))
        self.steps.append(("Skip NTFY", self._click_skip))

        # ── Summary ─────────────────────────────────────────────
        self.steps.append(("Reviewing setup summary...", lambda: None))
        self.steps.append(("Demo complete! Close window when ready.", lambda: None))

    def _click_next(self):
        self.wizard._next_btn.click()

    def _click_skip(self):
        if self.wizard._skip_btn.isVisible():
            self.wizard._skip_btn.click()
        else:
            self._click_next()

    def start(self):
        self._run_step()

    def _run_step(self):
        if self._step_index >= len(self.steps):
            return

        label, action = self.steps[self._step_index]
        page_idx = self.wizard._stack.currentIndex()
        page_names = {
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
        page_name = page_names.get(page_idx, f"Page {page_idx}")
        print(
            f"  [{self._step_index + 1:2d}/{len(self.steps)}] [{page_name:12s}] {label}"
        )

        if CAPTURE_SCREENSHOTS:
            SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            filename = (
                f"{self._step_index + 1:02d}-{page_name.lower().replace(' ', '-')}.png"
            )
            pixmap = self.wizard.grab()
            pixmap.save(str(SCREENSHOT_DIR / filename))
            print(f"           -> Saved {filename}")

        try:
            action()
        except Exception as e:
            print(f"           Error: {e}")

        self._step_index += 1
        QTimer.singleShot(STEP_DELAY, self._run_step)


def main():
    print()
    print("=" * 64)
    print("  Onboarding Wizard Demo -- TTT Path + Camera Setup")
    print("=" * 64)
    print()
    print("  Flow: TTT Sign-in -> Start Fresh -> Camera Setup (Reolink)")
    print("  Camera simulator at 127.0.0.1:8180 (admin/admin)")
    print("  TTT backend at localhost:8000 (local Supabase)")
    print()
    print("  Watch the wizard window...")
    print()

    app = QApplication(sys.argv)

    config_path = Path(tempfile.mkdtemp(prefix="demo_wizard_")) / "config.ini"
    wizard = OnboardingWizard(config_path)
    wizard.setWindowTitle("Soccer-Cam Setup")
    wizard.show()
    wizard.raise_()
    wizard.activateWindow()

    driver = WizardDriver(wizard)
    QTimer.singleShot(2000, driver.start)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
