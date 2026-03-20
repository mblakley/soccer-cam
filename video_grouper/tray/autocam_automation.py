import time
import logging
import os
import subprocess
import win32gui
from pywinauto import Desktop
import datetime

from video_grouper.utils.config import AutocamConfig

logger = logging.getLogger(__name__)


def _validate_autocam_inputs(
    autocam_config: AutocamConfig, input_path: str, output_path: str
) -> bool:
    """
    Validate autocam inputs before processing.

    Args:
        autocam_config: Autocam configuration
        input_path: Path to input video file
        output_path: Path for output video file

    Returns:
        bool: True if inputs are valid, False otherwise
    """
    # Check if autocam is enabled
    if not autocam_config.enabled:
        logger.warning("Autocam is disabled in configuration")
        return False

    # Check if executable path is provided
    if not autocam_config.executable:
        logger.error("Autocam executable path is not configured")
        return False

    # Check if input path is provided
    if not input_path:
        logger.error("Input path is required")
        return False

    # Check if output path is provided
    if not output_path:
        logger.error("Output path is required")
        return False

    # Convert to absolute paths for validation
    try:
        abs_input_path = os.path.abspath(input_path)
        abs_output_path = os.path.abspath(output_path)
    except (TypeError, OSError) as e:
        logger.error(f"Invalid path provided: {e}")
        return False

    # Check if input file exists
    if not os.path.isfile(abs_input_path):
        logger.error(f"Input file does not exist: {abs_input_path}")
        return False

    # Check if autocam executable exists
    if not os.path.isfile(autocam_config.executable):
        logger.error(f"Autocam executable not found: {autocam_config.executable}")
        return False

    logger.info(
        f"Input validation passed. Input: {abs_input_path}, Output: {abs_output_path}"
    )
    return True


def _find_file_dialog(main_window, dialog_title_re, timeout=10):
    """
    Find a Windows file dialog, searching as child of main_window first,
    then falling back to desktop-level search.

    Args:
        main_window: The main application window
        dialog_title_re: Regex pattern for the file dialog title
        timeout: Max seconds to wait for the dialog

    Returns:
        The dialog wrapper element
    """
    # Try as child of main_window first (works for Open dialogs)
    try:
        file_dlg = main_window.child_window(
            title_re=dialog_title_re, control_type="Window"
        )
        file_dlg.wait("visible", timeout=timeout)
        logger.info(f"File dialog found as child: '{file_dlg.window_text()}'")
        return file_dlg
    except Exception:
        logger.info("Dialog not found as child of main window, searching desktop...")

    # Fall back to desktop-level search (works for Save dialogs)
    desktop = Desktop(backend="uia")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            file_dlg = desktop.window(title_re=dialog_title_re)
            if file_dlg.exists():
                file_dlg.wait("visible", timeout=5)
                logger.info(f"File dialog found on desktop: '{file_dlg.window_text()}'")
                return file_dlg
        except Exception:
            pass
        time.sleep(0.5)

    raise TimeoutError(f"File dialog matching '{dialog_title_re}' not found")


def _find_filename_edit(file_dlg):
    """
    Find the File name edit control in a Windows file dialog.
    Tries multiple strategies since Open and Save dialogs can differ.

    Args:
        file_dlg: The file dialog wrapper

    Returns:
        The Edit control for entering the file name
    """
    # Strategy 1: ComboBox with auto_id="1148" (standard Open dialog)
    try:
        combo = file_dlg.child_window(
            title="File name:", auto_id="1148", control_type="ComboBox"
        )
        edit = combo.child_window(control_type="Edit")
        if edit.exists(timeout=2):
            logger.info("Found filename edit via ComboBox auto_id=1148")
            return edit
    except Exception:
        pass

    # Strategy 2: ComboBox by title only, no auto_id (Save dialogs)
    try:
        combo = file_dlg.child_window(title="File name:", control_type="ComboBox")
        edit = combo.child_window(control_type="Edit")
        if edit.exists(timeout=2):
            logger.info("Found filename edit via ComboBox title only")
            return edit
    except Exception:
        pass

    # Strategy 3: Direct Edit with auto_id="1148"
    try:
        edit = file_dlg.child_window(auto_id="1148", control_type="Edit")
        if edit.exists(timeout=2):
            logger.info("Found filename edit directly via auto_id=1148")
            return edit
    except Exception:
        pass

    # Strategy 4: Edit child of any ComboBox in the dialog
    try:
        for combo in file_dlg.children(control_type="ComboBox"):
            try:
                edit = combo.child_window(control_type="Edit")
                if edit.exists(timeout=1):
                    logger.info(
                        f"Found filename edit in ComboBox: "
                        f"title='{combo.window_text()}', "
                        f"auto_id='{combo.element_info.automation_id}'"
                    )
                    return edit
            except Exception:
                continue
    except Exception:
        pass

    raise LookupError("Could not find File name edit control in dialog")


def _set_file_via_browse_dialog(
    main_window, browse_button_title, dialog_title_re, file_path
):
    """
    Set a file path by clicking a Browse button and interacting with the Windows file dialog.

    Args:
        main_window: The main application window
        browse_button_title: Title of the Browse button to click
        dialog_title_re: Regex pattern for the file dialog title
        file_path: Absolute path to enter in the dialog

    Returns:
        bool: True if the file was set successfully
    """
    browse_btn = main_window.child_window(
        title=browse_button_title, control_type="Button"
    )
    browse_btn.click()
    time.sleep(2)

    file_dlg = _find_file_dialog(main_window, dialog_title_re)

    filename_edit = _find_filename_edit(file_dlg)
    filename_edit.set_text(file_path)
    time.sleep(1)

    # Click the confirm button (Open or Save, always has auto_id="1")
    confirm_btn = file_dlg.child_window(auto_id="1", control_type="Button")
    confirm_btn.click()
    time.sleep(2)

    return True


def _execute_autocam_gui_automation(
    executable_path: str, input_path: str, output_path: str
) -> bool:
    """
    Execute the autocam GUI automation process for Once Sport Autocam 3.x.

    The new GUI (3.0.6+) uses:
    - "Browse files" / "Browse file" buttons that open Windows file dialogs
    - "Processing Setup" button (replaces old "Zoom Settings")
    - "Start Processing" button (auto_id="StartProcessingButton")
    - Notification text control (auto_id="Notification") for status messages

    Args:
        executable_path: Path to the autocam executable
        input_path: Path to input video file
        output_path: Path for output video file

    Returns:
        bool: True if automation was successful, False otherwise
    """
    abs_input_path = os.path.abspath(input_path)
    abs_output_path = os.path.abspath(output_path)

    logger.info(f"Starting Once Autocam automation for {abs_input_path}")
    logger.info(f"Output path will be {abs_output_path}")

    try:
        # Kill any existing Autocam instance before launching a new one
        subprocess.run(["taskkill", "/F", "/IM", "GUI.exe"], capture_output=True)
        time.sleep(1)

        # Use Popen so we don't block on the launcher process.
        # The new Autocam (GUI.exe) spawns a child process for the actual window,
        # so app.window() cannot track it — we search the desktop instead.
        subprocess.Popen([executable_path])
        logger.info(f"Launched Autocam: {executable_path}")

        # Give Autocam time to start its child window process
        time.sleep(5)

        # Find the Autocam window using win32gui.EnumWindows (fast, non-blocking).
        # Desktop(backend="uia").window() can hang when enumerating all UIA elements.
        def _find_autocam_hwnd():
            found = []

            def _cb(hwnd, _):
                if win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd)
                    if title.startswith("Once Sport Autocam"):
                        found.append(hwnd)

            win32gui.EnumWindows(_cb, None)
            return found[0] if found else None

        hwnd = None
        deadline = time.time() + 30
        while time.time() < deadline:
            remaining = int(deadline - time.time())
            hwnd = _find_autocam_hwnd()
            if hwnd:
                logger.info(f"Found Autocam window via win32gui (hwnd={hwnd})")
                break
            logger.debug(f"Searching for Autocam window... ({remaining}s remaining)")
            time.sleep(1)

        if hwnd is None:
            raise TimeoutError("Once Autocam window not found within 35 seconds")

        # Wrap the hwnd in a pywinauto window wrapper for interaction
        desktop = Desktop(backend="uia")
        main_window = desktop.window(handle=hwnd)
        main_window.wait("visible", timeout=10)
        logger.info(f"Once Autocam main window found: '{main_window.window_text()}'")

        # Bring window to foreground and focus it before interacting
        try:
            main_window.set_focus()
            main_window.bring_to_front()
        except Exception as e:
            logger.warning(f"Could not focus main window: {e}")
        time.sleep(3)  # Allow app to fully initialize before clicking

        # Set source file via Browse files dialog
        _set_file_via_browse_dialog(
            main_window, "Browse files", "Select video.*", abs_input_path
        )
        logger.info(f"Set source path: {abs_input_path}")

        # Set destination file via Browse file dialog
        # Dialog title is "Output (save) to local file"
        _set_file_via_browse_dialog(
            main_window, "Browse file", "Output.*|Save.*", abs_output_path
        )
        logger.info(f"Set destination path: {abs_output_path}")

        time.sleep(2)

        # Open Processing Setup so the field gets auto-marked from the video frame.
        # The SettingsWindow auto-marks the playing field when the video preview loads.
        logger.info("Opening Processing Setup for field auto-marking...")
        main_window.child_window(
            auto_id="ShowSettingsButton", control_type="Button"
        ).click()
        time.sleep(3)

        # Find the SettingsWindow via win32gui (fast, non-blocking)
        def _find_settings_hwnd():
            found = []

            def _cb(hwnd, _):
                if win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd)
                    if "Setting" in title or title == "SettingsWindow":
                        found.append(hwnd)

            win32gui.EnumWindows(_cb, None)
            return found[0] if found else None

        settings_hwnd = None
        deadline = time.time() + 15
        while time.time() < deadline:
            settings_hwnd = _find_settings_hwnd()
            if settings_hwnd:
                break
            time.sleep(0.5)

        if settings_hwnd is None:
            logger.error("SettingsWindow not found — skipping field marking step")
        else:
            settings_window = desktop.window(handle=settings_hwnd)
            settings_window.wait("visible", timeout=10)
            logger.info(
                f"Processing Setup window found: '{settings_window.window_text()}'"
            )

            # Wait for the video preview to load, then click "Auto mark".
            # The Auto mark button triggers automatic field detection from the video frame.
            logger.info("Waiting for video preview to load in Processing Setup...")
            time.sleep(10)  # Minimum wait for UI to settle

            # Wait up to 20 more seconds for the loading spinner to disappear
            spinner_gone_deadline = time.time() + 20
            while time.time() < spinner_gone_deadline:
                try:
                    spinner = settings_window.child_window(
                        auto_id="imageLoadingSpinner", control_type="Custom"
                    )
                    if not spinner.is_visible():
                        logger.info("Video preview loaded (spinner gone)")
                        break
                except Exception:
                    break  # Spinner not found = already gone
                time.sleep(1)

            # Click "Auto mark" to trigger automatic field detection
            try:
                auto_mark_btn = settings_window.child_window(
                    auto_id="autoMarkingBtn", control_type="Button"
                )
                auto_mark_btn.click()
                logger.info("Clicked Auto mark button")
                time.sleep(3)
            except Exception as e:
                logger.warning(f"Could not click Auto mark button: {e}")

            # Wait for field marking to complete (up to 60s after clicking Auto mark)
            marking_complete = False
            deadline = time.time() + 60
            while time.time() < deadline:
                try:
                    for txt in settings_window.descendants(control_type="Text"):
                        text = txt.window_text()
                        if "/10" in text:
                            logger.debug(
                                f"Field marking progress: {text} points marked"
                            )
                            if text.strip().startswith("10"):
                                logger.info(
                                    "Field marking complete: 10/10 points marked"
                                )
                                marking_complete = True
                            break
                except Exception as e:
                    logger.debug(f"Error checking field marking: {e}")
                if marking_complete:
                    break
                time.sleep(2)

            if not marking_complete:
                logger.warning("Field auto-marking did not reach 10/10 within timeout")

            # Click Apply to save the field marking settings
            try:
                apply_btn = settings_window.child_window(
                    auto_id="applyBtn", control_type="Button"
                )
                apply_btn.click()
                logger.info("Clicked Apply in Processing Setup")
                time.sleep(2)
            except Exception as e:
                logger.warning(f"Could not click Apply in Processing Setup: {e}")

            # Close SettingsWindow if it's still open
            try:
                if settings_window.exists() and settings_window.is_visible():
                    close_btn = settings_window.child_window(
                        title="Close", control_type="Button"
                    )
                    close_btn.click()
                    logger.info("Closed Processing Setup window")
                    time.sleep(1)
            except Exception as e:
                logger.debug(f"Settings window already closed or error closing: {e}")

        # Re-focus main window before starting processing
        try:
            main_window.set_focus()
        except Exception as e:
            logger.warning(f"Could not focus main window before start: {e}")
        time.sleep(1)

        # Start processing
        logger.info("Starting processing...")
        main_window.child_window(
            title="Start Processing",
            auto_id="StartProcessingButton",
            control_type="Button",
        ).click()
        time.sleep(2)

        # Wait for processing to finish by monitoring the Notification text control
        start_time = datetime.datetime.now()
        timeout_seconds = 60 * 60 * 24  # 24 hours
        startup_timeout_seconds = 300  # 5 minutes to start processing
        poll_interval = 30  # 30 seconds
        found = False
        processing_started = False

        while (datetime.datetime.now() - start_time).total_seconds() < timeout_seconds:
            try:
                notification = main_window.child_window(
                    auto_id="Notification", control_type="Text"
                )
                notification_text = notification.window_text().lower()

                if "finished processing" in notification_text:
                    found = True
                    logger.info(
                        f"Detected success message: '{notification.window_text()}'"
                    )
                    break
                elif "error" in notification_text:
                    logger.error(
                        f"Autocam reported an error: '{notification.window_text()}'"
                    )
                    break
                elif (
                    "processing" in notification_text
                    or "processed" in notification_text
                ):
                    # Autocam reports "% of video processed" during active processing.
                    # Both "processing" and "processed" indicate the job is underway.
                    if not processing_started:
                        processing_started = True
                        logger.info(
                            f"Processing started: '{notification.window_text()}'"
                        )
                    else:
                        logger.debug(f"Autocam status: '{notification.window_text()}'")
                else:
                    logger.debug(f"Autocam status: '{notification.window_text()}'")
            except Exception as e:
                logger.warning(f"Error while checking for success message: {e}")

            # If processing hasn't started within 5 minutes, bail out
            elapsed = (datetime.datetime.now() - start_time).total_seconds()
            if not processing_started and elapsed > startup_timeout_seconds:
                logger.error(
                    "Autocam did not start processing within "
                    f"{startup_timeout_seconds // 60} minutes. "
                    "A reboot may be required."
                )
                break

            time.sleep(poll_interval)

        if not found:
            logger.error(
                f"Timeout waiting for success message after "
                f"{(datetime.datetime.now() - start_time).total_seconds() / 60:.1f} minutes."
            )

        logger.info("Automation script finished, closing application.")
        subprocess.run(["taskkill", "/F", "/IM", "GUI.exe"], capture_output=True)
        return found

    except Exception as e:
        logger.error(f"An error occurred during Once Autocam automation: {e}")
        subprocess.run(["taskkill", "/F", "/IM", "GUI.exe"], capture_output=True)
        return False


def run_autocam_on_file(
    autocam_config: AutocamConfig, input_path: str, output_path: str
) -> bool:
    """
    Automates the Once Autocam GUI to process a video file.

    Args:
        autocam_config: Autocam configuration
        input_path: The path to the trimmed video file.
        output_path: The path to save the processed video file.

    Returns:
        bool: True if processing was successful, False otherwise.
    """
    try:
        # Validate inputs
        if not _validate_autocam_inputs(autocam_config, input_path, output_path):
            return False

        # Execute GUI automation
        return _execute_autocam_gui_automation(
            autocam_config.executable, input_path, output_path
        )
    except Exception as e:
        logger.error(f"Error running autocam: {e}")
        return False


if __name__ == "__main__":
    # For testing purposes
    logging.basicConfig(level=logging.INFO)
    # This requires a file to exist at this path
    # test_file = "C:\\path\\to\\your\\test-file-raw.mp4"
    # if os.path.exists(test_file):
    #    run_autocam_on_file(test_file)
    # else:
    #    logger.error(f"Test file not found: {test_file}")
    pass
