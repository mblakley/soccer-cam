"""
Inspect SettingsWindow AFTER loading source/destination files.
Run: uv run python tests/e2e/inspect_settings_window.py
"""

import time
import subprocess
import sys
import win32gui
from pywinauto import Desktop


AUTOCAM_EXE = r"C:\Users\markb\AppData\Local\Programs\Autocam\GUI.exe"
TEST_INPUT = r"C:\Users\markb\projects\soccer-cam\tests\e2e\test_clips\clip_01.mp4"
TEST_OUTPUT = r"C:\Users\markb\projects\soccer-cam\tests\e2e\test_clips\clip_01_out.mkv"


def find_autocam_hwnd(timeout=30):
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
    return None


def find_window_by_title_fragment(title_fragment, timeout=15):
    """Find any visible window containing title_fragment."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = []

        def _cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if title_fragment.lower() in title.lower():
                    found.append((hwnd, title))

        win32gui.EnumWindows(_cb, None)
        if found:
            return found[0]
        time.sleep(0.5)
    return None, None


def set_file_via_dialog(main_window, browse_btn_title, dialog_title_re, file_path):
    """Click browse button and set file in dialog."""
    btn = main_window.child_window(title=browse_btn_title, control_type="Button")
    btn.click()
    time.sleep(2)

    # Find dialog as child first
    try:
        dlg = main_window.child_window(title_re=dialog_title_re, control_type="Window")
        dlg.wait("visible", timeout=10)
    except Exception:
        desktop = Desktop(backend="uia")
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                dlg = desktop.window(title_re=dialog_title_re)
                if dlg.exists():
                    break
            except Exception:
                pass
            time.sleep(0.5)

    # Find filename edit - try ComboBox auto_id=1148 first, then by title
    edit = None
    try:
        combo = dlg.child_window(
            title="File name:", auto_id="1148", control_type="ComboBox"
        )
        edit = combo.child_window(control_type="Edit")
        if not edit.exists(timeout=2):
            edit = None
    except Exception:
        pass

    if edit is None:
        try:
            combo = dlg.child_window(title="File name:", control_type="ComboBox")
            edit = combo.child_window(control_type="Edit")
        except Exception:
            pass

    edit.set_text(file_path)
    time.sleep(1)
    confirm = dlg.child_window(auto_id="1", control_type="Button")
    confirm.click()
    time.sleep(2)


def dump_all(parent, indent=0, max_depth=6):
    if indent > max_depth:
        return
    try:
        for child in parent.children():
            try:
                title = child.window_text()
                ctrl_type = child.element_info.control_type
                auto_id = child.element_info.automation_id
                print(
                    f"{'  ' * indent}[{ctrl_type}] title='{title}' auto_id='{auto_id}'"
                )
                if ctrl_type in ("Pane", "Group", "Tab", "TabItem", "Window", "Custom"):
                    dump_all(child, indent + 1, max_depth)
            except Exception as e:
                print(f"{'  ' * indent}  <error: {e}>")
    except Exception as e:
        print(f"{'  ' * indent}<listing error: {e}>")


def main():
    print("=" * 60)
    print("Autocam SettingsWindow Inspection (with files loaded)")
    print("=" * 60)

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
    print(f"Found window: '{main_window.window_text()}'")

    try:
        main_window.set_focus()
    except Exception as e:
        print(f"  Warning: {e}")
    time.sleep(3)

    # Step 1: Set source file
    print(f"\n--- Setting source file: {TEST_INPUT} ---")
    set_file_via_dialog(main_window, "Browse files", "Select video.*", TEST_INPUT)
    print("  Source set")

    # Step 2: Set destination file
    print(f"\n--- Setting destination file: {TEST_OUTPUT} ---")
    set_file_via_dialog(main_window, "Browse file", "Output.*|Save.*", TEST_OUTPUT)
    print("  Destination set")

    time.sleep(2)

    # Step 3: Check notification
    try:
        notif = main_window.child_window(auto_id="Notification", control_type="Text")
        print(f"\n--- Notification after file selection: '{notif.window_text()}' ---")
    except Exception as e:
        print(f"  Error reading notification: {e}")

    # Step 4: Click Processing Setup
    print("\n--- Clicking Processing Setup ---")
    try:
        setup_btn = main_window.child_window(
            auto_id="ShowSettingsButton", control_type="Button"
        )
        setup_btn.click()
        print("  Clicked Processing Setup")
    except Exception as e:
        print(f"  Error: {e}")
        subprocess.run(["taskkill", "/F", "/IM", "GUI.exe"], capture_output=True)
        sys.exit(1)

    time.sleep(3)

    # Step 5: Find SettingsWindow
    print("\n--- Searching for SettingsWindow ---")

    # Method 1: win32gui search for any new window
    settings_hwnd, settings_title = find_window_by_title_fragment("Setting", timeout=10)
    if settings_hwnd:
        print(f"  Found via win32gui: '{settings_title}' hwnd={settings_hwnd}")
        sw = desktop.window(handle=settings_hwnd)
        print("\n--- SettingsWindow all controls ---")
        dump_all(sw, indent=0, max_depth=5)

        print("\n--- Buttons in SettingsWindow ---")
        try:
            for btn in sw.descendants(control_type="Button"):
                title = btn.window_text()
                auto_id = btn.element_info.automation_id
                print(f"  Button: title='{title}' auto_id='{auto_id}'")
        except Exception as e:
            print(f"  Error: {e}")

        print("\n--- Text controls in SettingsWindow ---")
        try:
            for txt in sw.descendants(control_type="Text"):
                title = txt.window_text()
                auto_id = txt.element_info.automation_id
                if title or auto_id:
                    print(f"  Text: title='{title}' auto_id='{auto_id}'")
        except Exception as e:
            print(f"  Error: {e}")
    else:
        print(
            "  SettingsWindow not found via win32gui, checking as main_window child..."
        )
        dump_all(main_window, indent=0, max_depth=4)

    print("\n--- Closing Autocam ---")
    subprocess.run(["taskkill", "/F", "/IM", "GUI.exe"], capture_output=True)
    print("Done.")


if __name__ == "__main__":
    main()
