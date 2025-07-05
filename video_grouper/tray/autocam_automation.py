import time
import logging
import os
from pywinauto.application import Application
import datetime

logger = logging.getLogger(__name__)

AUTOCAM_EXE_PATH = (
    r"C:\Users\markb\AppData\Local\Programs\Once.Autocam\Once.Autocam.exe"
)


def run_autocam_on_file(input_path: str, output_path: str) -> bool:
    """
    Automates the Once Autocam GUI to process a video file.

    Args:
        input_path: The path to the trimmed video file.
        output_path: The path to save the processed video file.

    Returns:
        bool: True if processing was successful, False otherwise.
    """
    # Convert paths to absolute paths
    abs_input_path = os.path.abspath(input_path)
    abs_output_path = os.path.abspath(output_path)

    logger.info(f"Starting Once Autocam automation for {abs_input_path}")
    logger.info(f"Output path will be {abs_output_path}")

    try:
        app = Application(backend="uia").start(AUTOCAM_EXE_PATH)

        # Connect to the main window
        main_window = app.window(title_re="Once Autocam GUI.*")
        main_window.wait("visible", timeout=30)
        logger.info("Once Autocam main window found.")

        # Set source and destination paths using absolute paths
        main_window.child_window(title="Source:", control_type="Edit").set_text(
            abs_input_path
        )
        main_window.child_window(title="Destination:", control_type="Edit").set_text(
            abs_output_path
        )
        logger.info("Set source and destination paths.")

        # Handle Zoom Settings
        main_window.child_window(
            title="Zoom Settings", control_type="Pane"
        ).click_input()

        field_marking_window = app.window(title="Mark the playing field")
        field_marking_window.wait("visible", timeout=60)  # Wait up to 60 seconds
        logger.info("Mark the playing field window found.")

        # Click "Apply Settings"
        field_marking_window.child_window(
            title="Apply Settings", control_type="Pane"
        ).click_input()
        logger.info("Clicked 'Apply Settings' in Mark the playing field window.")

        field_marking_window.close()
        logger.info("Closing Mark the playing field window.")

        field_marking_window.wait_not("visible", timeout=10)
        logger.info("Mark the playing field window closed.")

        # Start processing
        logger.info("Starting processing...")
        main_window.child_window(
            title="Start Processing", control_type="Pane"
        ).click_input()

        # Wait for processing to finish by monitoring for the "Processing done" message
        # Wait until the text 'your video has finished processing' appears, up to 6 hours
        # TODO: Adjust the timeout based on the expected processing time, displayed in the window
        start_time = datetime.datetime.now()
        timeout_seconds = 60 * 60 * 6  # 6 hours
        poll_interval = 60  # 1 minute
        success_text = "your video has finished processing"
        found = False
        while (datetime.datetime.now() - start_time).total_seconds() < timeout_seconds:
            try:
                # Get all static texts in the main window
                texts = [
                    ctrl.window_text()
                    for ctrl in main_window.descendants()
                    if hasattr(ctrl, "window_text")
                ]
                if any(success_text in t for t in texts):
                    found = True
                    logger.info(
                        "Detected success message: 'your video has finished processing'"
                    )
                    break
            except Exception as e:
                logger.warning(f"Error while checking for success message: {e}")
            time.sleep(poll_interval)
        if not found:
            logger.error(
                f"Timeout waiting for success message after {timeout_seconds / 60} minutes."
            )
            # Optionally, you could return False here or raise an exception

        logger.info("Automation script finished, closing application.")
        app.kill()
        return True

    except Exception as e:
        logger.error(f"An error occurred during Once Autocam automation: {e}")
        # Ensure the app is killed even if there's an error
        if "app" in locals() and app.is_process_running():
            app.kill()
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
