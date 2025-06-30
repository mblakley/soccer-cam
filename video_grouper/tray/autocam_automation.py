import time
import logging
from pywinauto.application import Application

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
    logger.info(f"Starting Once Autocam automation for {input_path}")
    logger.info(f"Output path will be {output_path}")

    try:
        app = Application(backend="uia").start(AUTOCAM_EXE_PATH)

        # Connect to the main window
        main_window = app.window(title_re="Once Autocam GUI.*")
        main_window.wait("visible", timeout=30)
        logger.info("Once Autocam main window found.")

        # Set source and destination paths
        main_window.child_window(title="Source:", control_type="Edit").set_text(
            input_path
        )
        main_window.child_window(title="Destination:", control_type="Edit").set_text(
            output_path
        )
        logger.info("Set source and destination paths.")

        # Handle Zoom Settings
        main_window.child_window(
            title="Zoom Settings", control_type="Pane"
        ).click_input()

        field_marking_window = app.window(title="Mark the playing field")
        field_marking_window.wait("visible", timeout=20)  # Wait up to 20 seconds
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
        # This part might need adjustment based on the actual success message/dialog
        time.sleep(120)  # Placeholder wait time

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
