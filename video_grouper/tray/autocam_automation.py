import datetime
import logging
import os
import subprocess
import time

import win32gui
from pywinauto import Desktop

from video_grouper.models.directory_state import DirectoryState
from video_grouper.utils.config import AutocamConfig

logger = logging.getLogger(__name__)


# Window title prefix the AutoCam main window uses on Windows. Keeping
# this as a module constant lets the resume + fresh-launch paths agree.
_AUTOCAM_WINDOW_PREFIX = "Once Sport Autocam"
_AUTOCAM_PROCESS_NAME = "GUI.exe"

# Suppress the console flash on every tasklist/wmic poll -- the tray is
# a GUI app and these run on a 30s discovery loop.
_NO_WINDOW = 0x08000000

# Substrings (case-insensitive) that mark AutoCam's C-level shutdown
# phase. When the notification reaches one of these AND the output
# file is at expected size, AutoCam has finished writing and is just
# tearing down the FrameReader / GUI process. This build never emits
# "finished processing" on its own, so framereader_close is the
# authoritative end-of-run signal.
_SHUTDOWN_MARKERS = ("framereader_close", "finished processing")


def _taskkill_autocam_tree() -> None:
    """Kill GUI.exe AND autocam.exe. AutoCam 3.0.7 spawns autocam.exe
    as a child of GUI.exe; killing only GUI.exe leaves autocam.exe
    orphaned, eating CPU and holding the output file handle so the
    next pass can't delete the partial.
    """
    for image in ("GUI.exe", "autocam.exe"):
        subprocess.run(
            ["taskkill", "/F", "/IM", image],
            capture_output=True,
            creationflags=_NO_WINDOW,
        )


def _find_autocam_hwnd() -> int | None:
    """Return the hwnd of an Once Sport Autocam window, or None.

    Uses win32gui.EnumWindows (fast, non-blocking) instead of
    Desktop(backend="uia").window(), which can hang while enumerating
    UIA elements on a busy desktop.
    """
    found = []

    def _cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title.startswith(_AUTOCAM_WINDOW_PREFIX):
                found.append(hwnd)

    win32gui.EnumWindows(_cb, None)
    return found[0] if found else None


def _run_console(cmd: list[str], timeout: float = 10.0) -> str:
    """Run a Windows console helper (tasklist/wmic), return stdout, or ''
    on timeout / OSError. Swallow-and-log matches the posture of the
    callers, which treat absent data as 'no match' rather than fatal.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.debug("console helper %s failed: %s", cmd[0], e)
        return ""
    return result.stdout or ""


def _parse_tasklist_csv_pids(stdout: str) -> list[int]:
    """Parse `tasklist /FO CSV /NH` output and return PIDs (column 2).

    Returns an empty list if tasklist's "no tasks running" banner is
    present (some Windows versions route that to stdout instead of
    stderr) or any other unparseable content appears.
    """
    pids: list[int] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) < 2:
            continue
        try:
            pids.append(int(parts[1]))
        except ValueError:
            continue
    return pids


def _parse_wmi_datetime(s: str) -> float | None:
    """Parse a WMI CIM_DATETIME field to Unix epoch seconds.

    Format: ``yyyymmddHHMMSS.ffffff±UUU`` (UUU = minutes east of UTC).
    Example: ``20260529182708.123456-300`` is 2026-05-29 18:27:08.123456
    in UTC-5h. Returns None on any parse failure.
    """
    s = s.strip()
    if len(s) < 25:
        return None
    try:
        year = int(s[0:4])
        month = int(s[4:6])
        day = int(s[6:8])
        hour = int(s[8:10])
        minute = int(s[10:12])
        second = int(s[12:14])
        micro = int(s[15:21])
        sign = s[21]
        tz_minutes = int(s[22:25])
        if sign == "-":
            tz_minutes = -tz_minutes
        dt = datetime.datetime(
            year,
            month,
            day,
            hour,
            minute,
            second,
            micro,
            tzinfo=datetime.timezone(datetime.timedelta(minutes=tz_minutes)),
        )
        return dt.timestamp()
    except (ValueError, IndexError):
        return None


def _wmi_creation_time(pid: int) -> float | None:
    """Return Unix-epoch creation time for ``pid`` via wmic, or None.

    `wmic` is deprecated on newer Windows but still ships on Win10/11.
    None means we couldn't determine it; callers treat that as
    'no filter' (fail-open) -- a false positive is harmless because
    every downstream consumer revalidates via _live_autocam_pids.
    """
    out = _run_console(
        [
            "wmic",
            "process",
            "where",
            f"ProcessId={pid}",
            "get",
            "CreationDate",
            "/FORMAT:CSV",
        ]
    )
    for line in out.splitlines():
        line = line.strip()
        if (
            not line
            or line.lower().startswith("node")
            or "creationdate" in line.lower()
        ):
            continue
        parts = line.split(",")
        if len(parts) < 2:
            continue
        epoch = _parse_wmi_datetime(parts[-1])
        if epoch is not None:
            return epoch
    return None


def _find_autocam_gui_pids(
    since_epoch: float | None = None,
) -> list[int]:
    """Return PIDs of running GUI.exe processes (case-insensitive).

    The AutoCam launcher (subprocess.Popen target) is GUI.exe and it
    spawns a grandchild — also GUI.exe — for the actual UI window.
    Both PIDs go into the resume marker; reattach validates that at
    least one is still alive.

    Uses `tasklist` (+ `wmic` for the optional since_epoch filter)
    rather than psutil: psutil's PyInstaller bundle is unreliable in
    this tray's onedir build (v0.4.x bundles raised AttributeError on
    psutil.process_iter even with the metrics extra synced). Subprocess
    over stdlib is what every other process-enum site in the repo uses.

    Args:
        since_epoch: If provided, only return processes whose creation
            time is at-or-after this Unix timestamp (seconds). Used by
            the launch path to avoid grabbing PIDs of unrelated GUI.exe
            instances that were already running.
    """
    pids = _parse_tasklist_csv_pids(
        _run_console(
            [
                "tasklist",
                "/FI",
                f"IMAGENAME eq {_AUTOCAM_PROCESS_NAME}",
                "/FO",
                "CSV",
                "/NH",
            ]
        )
    )
    if since_epoch is None:
        return pids
    filtered: list[int] = []
    for pid in pids:
        created = _wmi_creation_time(pid)
        if created is None or created >= since_epoch:
            filtered.append(pid)
    return filtered


def _live_autocam_pids(candidate_pids: list[int]) -> list[int]:
    """Filter ``candidate_pids`` to those that are alive AND named GUI.exe.

    Uses a single ``tasklist`` call to get all GUI.exe PIDs, then
    intersects with ``candidate_pids``. This avoids spawning one
    subprocess per PID on every 30-second poll.
    """
    all_gui = set(
        _parse_tasklist_csv_pids(
            _run_console(
                [
                    "tasklist",
                    "/FI",
                    f"IMAGENAME eq {_AUTOCAM_PROCESS_NAME}",
                    "/FO",
                    "CSV",
                    "/NH",
                ]
            )
        )
    )
    return [pid for pid in candidate_pids if pid in all_gui]


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


def _wait_for_completion_and_cleanup(
    main_window,
    state: DirectoryState | None,
    output_path: str | None = None,
    tracked_pids: list[int] | None = None,
) -> bool:
    """Poll AutoCam's Notification text until processing finishes, then clean up.

    Both the fresh-launch path (after Browse/Mark/Start) and the resume
    path (skipping setup, attaching to a running window) end here. The
    polling loop reads ``auto_id="Notification"`` every 30s and looks
    for the terminal strings "finished processing" or "error".

    A second exit-detection signal runs alongside the notification poll
    when ``output_path`` and ``tracked_pids`` are supplied: if every
    tracked GUI.exe PID has exited AND the expected output file exists
    with non-trivial size, treat the run as a success. Some AutoCam
    builds (observed 2026-05-10) print a C-level ``FrameReader_close``
    cleanup message instead of "finished processing" right before
    exiting, which would otherwise hang this loop until the 24h
    timeout. Watching the process plus the output file removes that
    dependency on AutoCam's UI strings.

    A 24h ceiling keeps a stuck job from pinning the queue forever; a
    5-min "did processing actually start?" guard catches AutoCam
    getting wedged on boot. Always taskkills GUI.exe at the end and
    clears the resume marker, regardless of outcome.
    """
    start_time = datetime.datetime.now()
    timeout_seconds = 60 * 60 * 24  # 24 hours
    startup_timeout_seconds = 300  # 5 minutes to start processing
    poll_interval = 30  # 30 seconds
    # Min size below which we treat the output file as a partial-write
    # rather than a real success. Anything smaller than this means
    # AutoCam exited before producing a usable processed video.
    output_min_bytes = 10 * 1024 * 1024  # 10 MB
    found = False
    processing_started = False

    try:
        while (datetime.datetime.now() - start_time).total_seconds() < timeout_seconds:
            try:
                notification = main_window.child_window(
                    auto_id="Notification", control_type="Text"
                )
                raw_notification = notification.window_text()
                notification_text = raw_notification.lower()
                logger.info("Autocam status: %r", raw_notification)

                # Shutdown-marker fast path: when AutoCam's notification
                # contains a shutdown marker (e.g. "framereader_close"),
                # the render pipeline has finished writing the output
                # and is just tearing down the C-level framereader
                # struct. The user-facing "finished processing" string
                # is not always emitted in this build, so without this
                # branch the loop would have to wait for GUI.exe to
                # exit on its own -- which can take an indeterminate
                # amount of time after the shutdown marker first
                # appears. Treating the marker as authoritative end-of-
                # run lets us break+taskkill within one poll.
                if (
                    processing_started
                    and output_path
                    and any(m in notification_text for m in _SHUTDOWN_MARKERS)
                ):
                    out_size = 0
                    if os.path.isfile(output_path):
                        try:
                            out_size = os.path.getsize(output_path)
                        except OSError:
                            out_size = 0
                    if out_size >= output_min_bytes:
                        found = True
                        logger.info(
                            "AutoCam shutdown marker observed "
                            "(notification=%r, output=%.1f MB); "
                            "treating as success and breaking out.",
                            raw_notification,
                            out_size / 1024 / 1024,
                        )
                        break

                if "finished processing" in notification_text:
                    found = True
                    logger.info("Detected success message: %r", raw_notification)
                    break
                elif "error" in notification_text:
                    logger.error("Autocam reported an error: %r", raw_notification)
                    if output_path and os.path.isfile(output_path):
                        try:
                            os.remove(output_path)
                        except OSError:
                            pass
                    break
                elif (
                    "processing" in notification_text
                    or "processed" in notification_text
                ):
                    if not processing_started:
                        processing_started = True
                        logger.info("Processing started: %r", raw_notification)
            except Exception as e:
                logger.warning(f"Error while checking for success message: {e}")

            # Exit-detection fallback: if AutoCam's GUI processes have
            # all exited, infer success/failure from the output file
            # size. >= 10 MB at GUI exit = real run; below = crashed
            # partial, delete so next attempt re-runs from scratch.
            #
            # Size-only is safe here because mid-pass wedges (which can
            # leave a multi-hundred-MB partial that looks like a real
            # video) are eliminated by locking download_protocol to a
            # single transport per session -- mixed-protocol GOP
            # boundaries were the only known wedge trigger.
            live_pids = _live_autocam_pids(tracked_pids) if tracked_pids else None
            if tracked_pids and not live_pids:
                if output_path and os.path.isfile(output_path):
                    try:
                        size = os.path.getsize(output_path)
                    except OSError:
                        size = 0
                    if size >= output_min_bytes:
                        found = True
                        logger.info(
                            "AutoCam GUI exited with output at %.1f MB; "
                            "treating as success.",
                            size / 1024 / 1024,
                        )
                        break
                    logger.error(
                        "AutoCam GUI exited with sub-threshold output at "
                        "%d bytes -- treating as crash. Deleting so next "
                        "attempt re-runs.",
                        size,
                    )
                    try:
                        os.remove(output_path)
                    except OSError as e:
                        logger.warning(
                            "Could not remove crashed partial %s: %s",
                            output_path,
                            e,
                        )
                    jsonl = output_path + ".jsonl"
                    if os.path.isfile(jsonl):
                        try:
                            os.remove(jsonl)
                        except OSError:
                            pass
                    break
                logger.error(
                    "AutoCam GUI exited without producing the expected "
                    f"output at {output_path}; treating as failure."
                )
                break

            # If processing hasn't started within 5 minutes, bail out.
            # (Skip this guard on the resume path: an in-flight pass has
            # already started — we just attached late.)
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
        return found
    finally:
        logger.info("Automation script finished, closing application.")
        _taskkill_autocam_tree()
        if state is not None:
            state.clear_autocam_run()


def _execute_autocam_gui_automation(
    executable_path: str,
    input_path: str,
    output_path: str,
    group_dir: str | None = None,
) -> bool:
    """
    Execute the autocam GUI automation process for Once Sport Autocam 3.x.

    The new GUI (3.0.6+) uses:
    - "Browse files" / "Browse file" buttons that open Windows file dialogs
    - "Processing Setup" button (replaces old "Zoom Settings")
    - "Start Processing" button (auto_id="StartProcessingButton")
    - Notification text control (auto_id="Notification") for status messages

    When ``group_dir`` is provided, an "autocam_run" marker is written to
    ``<group_dir>/state.json`` containing the launcher PID and any GUI.exe
    PIDs we observed after launch. If a marker already exists for the same
    input_path AND its PIDs are still alive AND a matching AutoCam window
    is present on the desktop, the function reattaches to the existing run
    instead of killing-and-relaunching — the resume path that lets a tray
    crash mid-pass not cost the user 1-2 hours of GPU work.

    Args:
        executable_path: Path to the autocam executable
        input_path: Path to input video file
        output_path: Path for output video file
        group_dir: Optional video group directory; enables resume tracking
            via state.json. When omitted, behaves like the old launch-fresh
            path (no marker writes, no resume check).

    Returns:
        bool: True if automation was successful, False otherwise
    """
    abs_input_path = os.path.abspath(input_path)
    abs_output_path = os.path.abspath(output_path)

    logger.info(f"Starting Once Autocam automation for {abs_input_path}")
    logger.info(f"Output path will be {abs_output_path}")

    state = DirectoryState(group_dir) if group_dir else None

    # Already-done short-circuit: if the output mp4 exists at non-trivial
    # size, AutoCam already produced it. Re-running would waste 1-2 hours
    # of GPU work on output we already have. Most common trigger: the
    # tray crashed after AutoCam finished but before the success was
    # recorded; the in_progress task was restored from disk on the next
    # tray boot and would otherwise relaunch GUI.exe from scratch.
    #
    # Size-only check is safe because every failure path inside the
    # poll loop deletes its partial output before breaking, so a >= 10
    # MB file on disk is always a real completed run.
    if os.path.isfile(abs_output_path):
        try:
            existing_size = os.path.getsize(abs_output_path)
        except OSError:
            existing_size = 0
        if existing_size >= 10 * 1024 * 1024:
            logger.info(
                "AutoCam output already exists at %s (%.1f MB); skipping re-run.",
                abs_output_path,
                existing_size / 1024 / 1024,
            )
            if state is not None:
                state.clear_autocam_run()
            return True
        # Sub-10MB output is junk from a previous failed Save-dialog
        # interaction; delete it before relaunching so the overwrite-
        # confirm overlay (which the dialog automation can't drive)
        # never appears.
        try:
            os.remove(abs_output_path)
            logger.info(
                "Removed sub-threshold pre-existing output at %s (%.1f MB) "
                "before relaunch",
                abs_output_path,
                existing_size / 1024 / 1024,
            )
        except OSError as e:
            logger.warning(
                "Could not remove pre-existing output %s: %s; continuing anyway",
                abs_output_path,
                e,
            )

    desktop = Desktop(backend="uia")

    # ------------------------------------------------------------------
    # Resume path: if a previous tray run wrote an autocam_run marker for
    # this same input AND those processes + window are still alive, skip
    # the kill+launch+setup sequence and drop straight into polling.
    # The "finished processing" notification persists in the AutoCam UI
    # until the window is closed, so a delayed reattach can still detect
    # completion of a job that finished while the tray was down.
    # ------------------------------------------------------------------
    if state is not None:
        existing = state.get_autocam_run()
        if existing and existing.get("input_path") == abs_input_path:
            live = _live_autocam_pids(existing.get("gui_pids", []))
            hwnd = _find_autocam_hwnd() if live else None
            if live and hwnd:
                logger.info(
                    "Reattaching to running AutoCam: pids=%s hwnd=%s "
                    "(skipping kill+launch+setup)",
                    live,
                    hwnd,
                )
                main_window = desktop.window(handle=hwnd)
                try:
                    main_window.wait("visible", timeout=10)
                except Exception as e:
                    logger.warning(
                        "Could not attach to AutoCam window %s: %s; "
                        "falling through to fresh launch",
                        hwnd,
                        e,
                    )
                else:
                    return _wait_for_completion_and_cleanup(
                        main_window,
                        state,
                        output_path=existing.get("output_path", abs_output_path),
                        tracked_pids=live,
                    )
            else:
                logger.info(
                    "Stale autocam_run marker (live_pids=%s, hwnd=%s); clearing and relaunching",
                    live,
                    bool(hwnd),
                )
                state.clear_autocam_run()

    try:
        # Kill any existing Autocam instance before launching a new one
        _taskkill_autocam_tree()
        time.sleep(1)

        # Use Popen so we don't block on the launcher process.
        # The new Autocam (GUI.exe) spawns a child process for the actual window,
        # so app.window() cannot track it — we search the desktop instead.
        launcher = subprocess.Popen([executable_path])
        logger.info(
            f"Launched Autocam: {executable_path} (launcher pid={launcher.pid})"
        )

        # Give Autocam time to start its child window process
        time.sleep(5)

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

        # Persist PIDs to state.json so a tray crash mid-pass can reattach
        # on restart. Get the window-owning PID via win32process (instant,
        # no subprocess spawn) — this is the child GUI.exe that actually
        # owns the AutoCam window, not just the launcher which may exit.
        import win32process

        _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
        if state is not None:
            gui_pids = list({launcher.pid, window_pid})
            state.set_autocam_run(
                {
                    "launcher_pid": launcher.pid,
                    "gui_pids": gui_pids,
                    "input_path": abs_input_path,
                    "output_path": abs_output_path,
                    "started_at": datetime.datetime.utcnow().isoformat() + "Z",
                }
            )
            logger.info(
                "Recorded autocam_run marker: launcher_pid=%s gui_pids=%s",
                launcher.pid,
                gui_pids,
            )

        # Wrap the hwnd in a pywinauto window wrapper for interaction
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

        # Track both the launcher and the window-owning PID for exit
        # detection. No subprocess spawning — window_pid was already
        # captured via win32process above.
        return _wait_for_completion_and_cleanup(
            main_window,
            state,
            output_path=abs_output_path,
            tracked_pids=list({launcher.pid, window_pid}),
        )

    except Exception as e:
        logger.error(f"An error occurred during Once Autocam automation: {e}")
        _taskkill_autocam_tree()
        if state is not None:
            state.clear_autocam_run()
        return False


def run_autocam_on_file(
    autocam_config: AutocamConfig,
    input_path: str,
    output_path: str,
    group_dir: str | None = None,
) -> bool:
    """
    Automates the Once Autocam GUI to process a video file.

    Args:
        autocam_config: Autocam configuration
        input_path: The path to the trimmed video file.
        output_path: The path to save the processed video file.
        group_dir: Optional video group directory. When provided,
            ``_execute_autocam_gui_automation`` writes a resume marker to
            ``<group_dir>/state.json`` and reattaches to a running
            AutoCam on tray restart instead of relaunching from scratch.

    Returns:
        bool: True if processing was successful, False otherwise.
    """
    try:
        # Validate inputs
        if not _validate_autocam_inputs(autocam_config, input_path, output_path):
            return False

        # Execute GUI automation
        return _execute_autocam_gui_automation(
            autocam_config.executable, input_path, output_path, group_dir=group_dir
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
