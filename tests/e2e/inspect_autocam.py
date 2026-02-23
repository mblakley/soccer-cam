"""
Standalone script to inspect the Autocam GUI and find correct control identifiers.
Run this directly: uv run python tests/e2e/inspect_autocam.py
"""

import time
import subprocess
import sys
from pywinauto import Desktop
from pywinauto.application import Application

AUTOCAM_EXE = r"C:\Users\markb\AppData\Local\Programs\Autocam\GUI.exe"

# A real video file to use for testing the source dialog
TEST_INPUT = r"C:\Users\markb\projects\soccer-cam\tests\e2e\test_clips\clip_01.mp4"
TEST_OUTPUT = r"C:\Users\markb\projects\soccer-cam\tests\e2e\test_clips\clip_01_out.mkv"


def find_window(timeout=30):
    """Try both Application.start and Desktop approaches."""
    print("--- Trying Application(backend=uia).start() ---")
    try:
        app = Application(backend="uia").start(AUTOCAM_EXE)
        print(f"  Started via Application, PID: {app.process}")
        main_window = app.window(title_re="Once Sport Autocam.*")
        main_window.wait("visible", timeout=timeout)
        print(f"  Found via app.window(): '{main_window.window_text()}'")
        return main_window, app
    except Exception as e:
        print(f"  app.window() failed: {e}")

    print("--- Trying Desktop search ---")
    subprocess.Popen([AUTOCAM_EXE])
    desktop = Desktop(backend="uia")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            w = desktop.window(title_re="Once Sport Autocam.*")
            if w.exists(timeout=1):
                print(f"  Found via Desktop: '{w.window_text()}'")
                return w, None
        except Exception:
            pass
        time.sleep(1)
        print(f"  Still searching... ({int(deadline - time.time())}s left)")

    print("  Not found via Desktop either")
    return None, None


def print_children(window, indent=0):
    """Print all children of a window."""
    try:
        for child in window.children():
            try:
                title = child.window_text()
                ctrl_type = child.element_info.control_type
                auto_id = child.element_info.automation_id
                print(
                    f"{'  ' * indent}[{ctrl_type}] title='{title}' auto_id='{auto_id}'"
                )
                if ctrl_type in ("Pane", "Group", "Tab", "TabItem"):
                    print_children(child, indent + 1)
            except Exception as e:
                print(f"{'  ' * indent}  <error reading child: {e}>")
    except Exception as e:
        print(f"{'  ' * indent}<error listing children: {e}>")


def main():
    print("=" * 60)
    print("Autocam GUI Inspection Script")
    print("=" * 60)

    main_window, app = find_window(timeout=30)
    if main_window is None:
        print("FAILED: Could not find Autocam window")
        sys.exit(1)

    print("\n--- All direct children of main window ---")
    print_children(main_window)

    print("\n--- Searching for buttons ---")
    try:
        for btn in main_window.descendants(control_type="Button"):
            try:
                print(
                    f"  Button: title='{btn.window_text()}' auto_id='{btn.element_info.automation_id}' visible={btn.is_visible()}"
                )
            except Exception as e:
                print(f"  Button error: {e}")
    except Exception as e:
        print(f"  Error listing buttons: {e}")

    print("\n--- Looking for Browse files button and clicking it ---")
    try:
        browse_btn = main_window.child_window(
            title="Browse files", control_type="Button"
        )
        print(
            f"  Found 'Browse files' button: visible={browse_btn.is_visible()}, enabled={browse_btn.is_enabled()}"
        )
        print("  Clicking 'Browse files'...")
        browse_btn.click_input()
        time.sleep(3)

        print("\n--- Looking for file dialog after Browse files click ---")
        desktop = Desktop(backend="uia")
        for w in desktop.windows():
            title = w.window_text()
            if title and title != main_window.window_text():
                print(f"  Window: '{title}'")
    except Exception as e:
        print(f"  Error: {e}")

    print("\n--- Closing Autocam ---")
    subprocess.run(["taskkill", "/F", "/IM", "GUI.exe"], capture_output=True)
    print("Done.")


if __name__ == "__main__":
    main()
