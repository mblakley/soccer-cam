"""
Standalone script to inspect Autocam's Processing Setup dialog.
Run: uv run python tests/e2e/inspect_processing_setup.py
"""

import time
import subprocess
import sys
import win32gui
from pywinauto import Desktop


AUTOCAM_EXE = r"C:\Users\markb\AppData\Local\Programs\Autocam\GUI.exe"


def find_autocam_hwnd(timeout=30):
    """Find Autocam window via win32gui."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = []

        def _cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if title.startswith("Once Sport Autocam"):
                    found.append(hwnd)

        win32gui.EnumWindows(_cb, None)
        if found:
            return found[0]
        time.sleep(1)
        print(f"  Searching... ({int(deadline - time.time())}s left)")
    return None


def dump_controls(parent, indent=0, max_depth=5):
    """Recursively dump all controls."""
    if indent > max_depth:
        return
    try:
        for child in parent.children():
            try:
                title = child.window_text()
                ctrl_type = child.element_info.control_type
                auto_id = child.element_info.automation_id
                visible = child.is_visible()
                enabled = child.is_enabled() if hasattr(child, "is_enabled") else "?"
                prefix = "  " * indent
                print(
                    f"{prefix}[{ctrl_type}] title='{title}' auto_id='{auto_id}' visible={visible} enabled={enabled}"
                )
                if ctrl_type in ("Pane", "Group", "Tab", "TabItem", "Window"):
                    dump_controls(child, indent + 1, max_depth)
            except Exception as e:
                print(f"{'  ' * indent}  <error: {e}>")
    except Exception as e:
        print(f"{'  ' * indent}<listing error: {e}>")


def main():
    print("=" * 60)
    print("Autocam Processing Setup Inspection")
    print("=" * 60)

    # Kill any existing instance
    subprocess.run(["taskkill", "/F", "/IM", "GUI.exe"], capture_output=True)
    time.sleep(1)

    print(f"\nLaunching: {AUTOCAM_EXE}")
    subprocess.Popen([AUTOCAM_EXE])
    time.sleep(5)

    hwnd = find_autocam_hwnd(timeout=30)
    if hwnd is None:
        print("FAILED: Could not find Autocam window")
        sys.exit(1)

    desktop = Desktop(backend="uia")
    main_window = desktop.window(handle=hwnd)
    main_window.wait("visible", timeout=10)
    print(f"\nFound window: '{main_window.window_text()}' (hwnd={hwnd})")

    try:
        main_window.set_focus()
    except Exception as e:
        print(f"  Warning: set_focus failed: {e}")
    time.sleep(2)

    print("\n--- All Buttons in main window ---")
    try:
        for btn in main_window.descendants(control_type="Button"):
            try:
                title = btn.window_text()
                auto_id = btn.element_info.automation_id
                visible = btn.is_visible()
                enabled = btn.is_enabled()
                print(
                    f"  Button: title='{title}' auto_id='{auto_id}' visible={visible} enabled={enabled}"
                )
            except Exception as e:
                print(f"  Button error: {e}")
    except Exception as e:
        print(f"  Error: {e}")

    print("\n--- All Text controls (Notification area) ---")
    try:
        for txt in main_window.descendants(control_type="Text"):
            try:
                title = txt.window_text()
                auto_id = txt.element_info.automation_id
                if title or auto_id:
                    print(f"  Text: title='{title}' auto_id='{auto_id}'")
            except Exception as e:
                print(f"  Text error: {e}")
    except Exception as e:
        print(f"  Error: {e}")

    print("\n--- Clicking 'Processing Setup' button ---")
    try:
        setup_btn = main_window.child_window(
            title="Processing Setup", control_type="Button"
        )
        if not setup_btn.exists(timeout=3):
            # Try by auto_id
            setup_btn = main_window.child_window(
                auto_id="ShowSettingsButton", control_type="Button"
            )
        print(
            f"  Found: title='{setup_btn.window_text()}' auto_id='{setup_btn.element_info.automation_id}'"
        )
        setup_btn.click()
        time.sleep(3)
        print("  Clicked 'Processing Setup'")
    except Exception as e:
        print(f"  Error clicking Processing Setup: {e}")

    print("\n--- Searching for new windows after clicking Processing Setup ---")
    try:
        for w in desktop.windows():
            title = w.window_text()
            if title and title != main_window.window_text():
                print(f"\n  Window: '{title}'")
                dump_controls(w, indent=2)
    except Exception as e:
        print(f"  Error: {e}")

    print("\n--- Also checking main window children for a modal dialog ---")
    try:
        dump_controls(main_window, indent=0, max_depth=3)
    except Exception as e:
        print(f"  Error: {e}")

    print("\n--- Closing Autocam ---")
    subprocess.run(["taskkill", "/F", "/IM", "GUI.exe"], capture_output=True)
    print("Done.")


if __name__ == "__main__":
    main()
