"""Tests for the NSIS installer and, mostly, its uninstaller.

Nothing covered the installer before this. That gap cost real time: a Git Bash
invocation of ``uninstall.exe /S`` silently did nothing (MSYS rewrites a lone
``/S`` into a path, so the uninstaller never saw the silent flag), and with no
test to appeal to it looked like a broken uninstaller for a while. A sandboxed
round-trip answers that class of question in seconds.

Two layers here:

* **Static checks** over ``installer.nsi``. Fast, no tooling, and they catch
  the failure that actually bites uninstallers -- installing something and
  forgetting to remove it. Adding a shortcut or a registry key without a
  matching delete fails these.
* **A round-trip** that compiles, installs into a sandbox and uninstalls,
  asserting nothing is left. Marked ``integration``: it needs makensis, and it
  is slow.

The round-trip deliberately does **not** run the real install section. That
section registers a Windows service and a scheduled task machine-wide, which
no test should do to a developer's box. It swaps in a stub install and keeps
``Section "Uninstall"`` byte-identical, because the uninstaller is the thing
under test.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

NSI = (
    Path(__file__).resolve().parents[1]
    / "video_grouper"
    / "installer"
    / "installer.nsi"
)


def _source() -> str:
    return NSI.read_text(encoding="utf-8", errors="replace")


def _section(name: str) -> str:
    """Return the body of a named NSIS section."""
    src = _source()
    start = src.index(f'Section "{name}"')
    return src[start : src.index("SectionEnd", start)]


def _install() -> str:
    return _section("Install")


def _uninstall() -> str:
    return _section("Uninstall")


# ---------------------------------------------------------------------------
# Static: everything the installer creates, the uninstaller removes
# ---------------------------------------------------------------------------


def test_the_nsi_exists_and_has_both_sections():
    assert NSI.is_file(), f"installer script missing: {NSI}"
    assert 'Section "Install"' in _source()
    assert 'Section "Uninstall"' in _source()


def test_every_registry_key_written_is_deleted():
    """A leftover HKLM key means Add/Remove Programs still lists a ghost."""
    written = set(re.findall(r"WriteRegStr HKLM \"([^\"]+)\"", _install(), re.I))
    deleted = set(re.findall(r"DeleteRegKey HKLM \"([^\"]+)\"", _uninstall(), re.I))

    # A coverage check that matches nothing would pass while proving nothing.
    assert written, "found no WriteRegStr calls -- has the syntax changed?"

    # DeleteRegKey removes the whole key, so a written path is covered when it
    # is deleted outright or sits underneath something that is.
    orphans = sorted(
        key
        for key in written
        if not any(key == d or key.startswith(d + "\\") for d in deleted)
    )
    assert not orphans, f"written but never deleted: {orphans}"


def test_every_shortcut_created_is_deleted():
    """A shortcut to a deleted exe is the most visible kind of leftover.

    Case-insensitive on purpose: NSIS commands are, and the real script spells
    it ``CreateShortCut``. A case-sensitive pattern matched nothing here and
    the test passed while checking nothing at all.
    """
    created = set(re.findall(r"CreateShortCut \"([^\"]+\.lnk)\"", _install(), re.I))
    deleted = set(re.findall(r"Delete \"([^\"]+\.lnk)\"", _uninstall(), re.I))

    assert created, "found no shortcut creation -- has the syntax changed?"
    assert not created - deleted, f"shortcuts left behind: {sorted(created - deleted)}"


def test_the_service_is_deregistered_not_merely_stopped():
    """Stopping without removing leaves an auto-start service pointing at
    a path that no longer exists."""
    un = _uninstall()
    assert '.exe" remove' in un or "sc.exe delete" in un, (
        "the uninstaller must deregister VideoGrouperService, not just stop it"
    )


def test_the_scheduled_task_is_removed():
    """The tray is launched by a scheduled task; it outlives the files."""
    src = _source()
    task = re.search(r'!define TRAY_TASK_NAME "([^"]+)"', src)
    assert task, "TRAY_TASK_NAME should be defined"
    assert 'schtasks /Delete /F /TN "${TRAY_TASK_NAME}"' in _uninstall(), (
        f"the {task.group(1)} task must be deleted on uninstall"
    )


def test_the_uninstaller_removes_the_install_directory():
    un = _uninstall()
    assert 'RMDir "$INSTDIR"' in un or 'RMDir /r "$INSTDIR"' in un


def test_install_dir_reg_key_reads_a_value_the_installer_writes():
    """``InstallDirRegKey`` defaults $INSTDIR from the registry on re-run.

    It used to read ``Install_Dir``, which nothing wrote -- so an upgrade over
    a custom location silently retargeted to the default and left the old
    directory behind. The auto-updater runs the installer with no /D, so the
    upgrade path is exactly the one that hit it.
    """
    src = _source()
    read = re.search(r'InstallDirRegKey HKLM "([^"]+)" "([^"]+)"', src)
    assert read, "InstallDirRegKey should be declared"
    key, value = read.groups()

    assert f'WriteRegStr HKLM "{key}" "{value}"' in _install(), (
        f"InstallDirRegKey reads {key}\\{value}, which the installer never writes"
    )


@pytest.mark.parametrize(
    "shipped",
    ["VideoGrouperService.exe", "VideoGrouperTray.exe", "_internal", "icon.ico"],
)
def test_each_shipped_artefact_is_named_by_the_uninstaller(shipped: str):
    """RMDir "$INSTDIR" is not recursive, so anything the uninstaller does not
    name keeps the install directory alive. Adding a file to the payload means
    adding it here too."""
    un = _uninstall()
    assert shipped in un, f"{shipped} is installed but never removed"


# ---------------------------------------------------------------------------
# Round-trip: compile, install into a sandbox, uninstall, assert nothing left
# ---------------------------------------------------------------------------

_MAKENSIS = shutil.which("makensis") or r"C:\Program Files (x86)\NSIS\makensis.exe"

pytestmark_reason = "needs Windows + makensis"


def _have_makensis() -> bool:
    return os.name == "nt" and Path(_MAKENSIS).is_file()


def _harness_source(out_exe: Path) -> str:
    """The real script with a stub install section.

    The real one registers a service and a scheduled task machine-wide, which
    a test must not do. ``Section "Uninstall"`` is carried over untouched --
    that is what is being tested.
    """
    src = _source()
    head = src[: src.index('Section "Install"')]
    uninstall = src[src.index('Section "Uninstall"') :]

    stub = "\n".join(
        [
            'Section "Install" SecInstall',
            '    SetOutPath "$INSTDIR"',
            '    File "..\\icon.ico"',
            '    CreateDirectory "$INSTDIR\\_internal"',
            '    File /oname=_internal\\payload.bin "..\\icon.ico"',
            '    File /oname=VideoGrouperService.exe "..\\icon.ico"',
            '    File /oname=VideoGrouperTray.exe "..\\icon.ico"',
            '    WriteUninstaller "$INSTDIR\\uninstall.exe"',
            "SectionEnd",
            "",
            "",
        ]
    )
    harness = head + stub + uninstall
    return harness.replace(
        'OutFile "..\\dist\\VideoGrouperSetup.exe"',
        f'OutFile "{out_exe}"',
    )


def _build_and_install(tmp_path: Path) -> Path:
    """Compile the harness and install it into a fresh sandbox.

    Returns the install directory. The sandbox deliberately has no space in
    its path: NSIS's /D takes the rest of the command line verbatim, so a
    spaced path cannot be quoted.
    """
    out_exe = tmp_path / "HarnessSetup.exe"
    harness = NSI.parent / "_pytest_harness.nsi"
    harness.write_text(_harness_source(out_exe), encoding="utf-8")
    try:
        built = subprocess.run(
            [_MAKENSIS, "/V2", str(harness)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert built.returncode == 0, f"makensis failed:\n{built.stdout}{built.stderr}"
        assert out_exe.is_file(), "installer was not produced"
    finally:
        harness.unlink(missing_ok=True)

    sandbox = Path(tempfile.mkdtemp(prefix="vg-uninst-")) / "app"
    subprocess.run(f'"{out_exe}" /S /D={sandbox}', shell=True, check=True, timeout=180)
    assert (sandbox / "uninstall.exe").is_file(), "install did not land"
    return sandbox


@pytest.mark.integration
@pytest.mark.skipif(not _have_makensis(), reason=pytestmark_reason)
def test_uninstall_removes_everything_it_installed(tmp_path):
    """The round-trip the manual debugging session needed and did not have."""
    sandbox = _build_and_install(tmp_path)

    # /S alone is silent but NOT synchronous: NSIS copies itself to %TEMP%,
    # relaunches, and the first process returns immediately -- measured at
    # 1.06s to return against 3.19s to actually finish. Polling for the
    # directory to vanish would make this test a race.
    #
    # _?=<dir> runs the uninstaller in place and blocks until it is done. The
    # documented cost is that NSIS cannot delete a running executable, so
    # uninstall.exe (and therefore $INSTDIR) survive -- which is why the
    # assertion below excludes it. This is also exactly what the registered
    # QuietUninstallString passes.
    subprocess.run(
        f'"{sandbox / "uninstall.exe"}" /S _?={sandbox}',
        shell=True,
        check=True,
        timeout=180,
    )

    leftovers = (
        sorted(p.name for p in sandbox.iterdir() if p.name != "uninstall.exe")
        if sandbox.exists()
        else []
    )
    assert not leftovers, f"uninstall left {leftovers} in {sandbox}"


def test_a_silent_uninstall_string_is_registered():
    """Without QuietUninstallString, winget/MDM launch the interactive GUI.

    It also carries _?=, so the caller blocks until the uninstall finishes
    rather than racing a detached copy running out of %TEMP%.
    """
    install = _install()
    assert '"QuietUninstallString"' in install, (
        "register QuietUninstallString so unattended tooling can remove this"
    )
    # The value embeds NSIS-escaped quotes ($\"), so take the rest of the line
    # rather than trying to parse NSIS's quoting rules.
    quiet = re.search(r'"QuietUninstallString"(.*)', install)
    assert quiet, "QuietUninstallString should have a value"
    assert "/S" in quiet.group(1), "the quiet string must be silent"
    assert "_?=" in quiet.group(1), (
        "the quiet string must use _?= so the caller waits for it to finish"
    )


@pytest.mark.integration
@pytest.mark.skipif(not _have_makensis(), reason=pytestmark_reason)
def test_the_real_script_compiles(tmp_path):
    """A syntax error in installer.nsi otherwise surfaces at release time.

    Compiles the real script, payload and all. The output is redirected into
    tmp_path: left alone it writes ../dist/VideoGrouperSetup.exe, and a test
    must not overwrite the release artefact sitting there.
    """
    dist = NSI.parents[1] / "dist" / "VideoGrouper"
    if not dist.is_dir():
        pytest.skip(f"no built payload at {dist}")

    out_exe = tmp_path / "CompileCheck.exe"
    probe = NSI.parent / "_pytest_compile.nsi"
    probe.write_text(
        _source().replace(
            'OutFile "..\\dist\\VideoGrouperSetup.exe"', f'OutFile "{out_exe}"'
        ),
        encoding="utf-8",
    )
    try:
        built = subprocess.run(
            [_MAKENSIS, "/V2", str(probe)],
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(NSI.parent),
        )
        assert built.returncode == 0, (
            f"installer.nsi failed to compile:\n{built.stdout}{built.stderr}"
        )
        assert out_exe.is_file(), "compile reported success but produced nothing"
    finally:
        probe.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# The Add/Remove Programs path
# ---------------------------------------------------------------------------
#
# ARP does two things: reads UninstallString out of the registry, and runs it
# with no extra arguments -- so the interactive uninstaller. Silent tooling
# (winget, Chocolatey, MDM) reads QuietUninstallString instead. Both strings
# are read back from the registry here rather than composed in the test, so
# what is exercised is what the installer actually registers.

_ARP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\VideoGrouper"


def _registered_values(install_dir: Path) -> dict[str, str]:
    """The ARP values as installer.nsi declares them, resolved for a sandbox."""
    src = _install()
    out: dict[str, str] = {}
    for name in ("UninstallString", "QuietUninstallString"):
        match = re.search(r'"' + name + r'" "(.*)"\s*$', src, re.M)
        assert match, f"{name} not found in the installer source"
        # NSIS: $\" is an escaped quote; $INSTDIR is the install directory.
        value = match.group(1).replace('$\\"', '"')
        out[name] = value.replace("$INSTDIR", str(install_dir))
    return out


@contextlib.contextmanager
def _arp_entry(install_dir: Path):
    """Register the app in Add/Remove Programs, and always clean up.

    Uses the real key name so this is the path Windows would take. The
    uninstaller deletes the key itself, so a missing key on teardown is the
    expected outcome, not a failure.
    """
    import winreg

    values = _registered_values(install_dir)
    with winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, _ARP_KEY) as key:
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "VideoGrouper")
    try:
        yield values
    finally:
        try:
            winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, _ARP_KEY)
        except FileNotFoundError:
            pass


def _read_arp(name: str) -> str:
    """Read a value the way Add/Remove Programs does."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _ARP_KEY) as key:
        return winreg.QueryValueEx(key, name)[0]


@pytest.mark.integration
@pytest.mark.skipif(not _have_makensis(), reason=pytestmark_reason)
def test_quiet_uninstall_string_removes_the_install(tmp_path):
    """Run QuietUninstallString exactly as winget or MDM tooling would."""
    sandbox = _build_and_install(tmp_path)
    with _arp_entry(sandbox):
        command = _read_arp("QuietUninstallString")
        started = time.perf_counter()
        done = subprocess.run(command, shell=True, timeout=180)
        elapsed = time.perf_counter() - started

    assert done.returncode == 0, f"quiet uninstall failed: rc={done.returncode}"
    leftovers = (
        sorted(p.name for p in sandbox.iterdir() if p.name != "uninstall.exe")
        if sandbox.exists()
        else []
    )
    # _?= blocks, so the work is finished when the call returns -- no polling.
    assert not leftovers, f"quiet uninstall left {leftovers}"
    assert elapsed < 60, (
        "slow enough to suggest it was waiting on a person -- "
        "QuietUninstallString must not open the interactive uninstaller"
    )


# Driving the GUI happens in a subprocess. pywinauto's win32 backend goes
# through comtypes, which needs COM in a single-threaded apartment; inside
# pytest the process is already in MTA and connect() dies with "Error loading
# type library/DLL". A fresh interpreter gets a clean apartment, and the
# uninstaller is a separate process anyway.
_GUI_DRIVER = """
import subprocess, sys, time
from pathlib import Path

command, sandbox = sys.argv[1], Path(sys.argv[2])

from pywinauto import Application

from pywinauto import Desktop

def handles():
    out = set()
    try:
        for w in Desktop(backend="win32").windows():
            try:
                if "Uninstall" in w.window_text():
                    out.add(w.handle)
            except Exception:
                pass
    except Exception:
        pass
    return out

# Snapshot first, then launch, then wait for a window that was not already
# there. Matching on the title alone attaches to a leftover from an earlier
# test and clicks its buttons while the real one sits waiting.
before = handles()
subprocess.Popen(command, shell=True)

target = None
for _ in range(60):
    new = handles() - before
    if new:
        target = new.pop()
        break
    time.sleep(0.5)
if target is None:
    print("no new uninstaller window appeared")
    raise SystemExit(2)
app = Application(backend="win32").connect(handle=target)

# MUI_UNPAGE_CONFIRM then MUI_UNPAGE_INSTFILES: Uninstall, then Close.
for label in ("&Uninstall", "&Close", "&Finish"):
    try:
        window = app.window(handle=target)
        button = window.child_window(title=label, class_name="Button")
        if button.exists() and button.is_enabled():
            button.click()
            time.sleep(1.5)
    except Exception:
        continue

emptied = False
for _ in range(40):
    if not sandbox.exists() or not any(
        p.name != "uninstall.exe" for p in sandbox.iterdir()
    ):
        emptied = True
        break
    time.sleep(0.5)

# Close the window. Asserting the files are gone and walking away leaves a
# dialog sitting on the desktop -- one per run, which is exactly what
# happened before this.
pid = None
try:
    pid = app.window(handle=target).process_id()
except Exception:
    pass
for _ in range(20):
    try:
        window = app.window(handle=target)
        if not window.exists():
            break
        for label in ("&Close", "Close", "&Finish", "Finish", "Cancel"):
            button = window.child_window(title=label, class_name="Button")
            if button.exists() and button.is_enabled():
                button.click()
                break
    except Exception:
        break
    time.sleep(0.5)

# Backstop: never leave the process behind, whatever the UI did.
if pid:
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)

raise SystemExit(0 if emptied else 3)
"""


@pytest.mark.integration
@pytest.mark.skipif(not _have_makensis(), reason=pytestmark_reason)
def test_add_remove_programs_uninstall_completes(tmp_path):
    """Drive the interactive uninstaller the way clicking Uninstall does.

    UninstallString carries no arguments, so this is the GUI path -- the one a
    person actually takes, and the one no other test covers.
    """
    # Deliberately not importorskip: importing pywinauto in *this* process
    # loads comtypes, which fails under pytest's COM apartment. find_spec
    # checks availability without executing the module; the driver subprocess
    # does the importing.
    if importlib.util.find_spec("pywinauto") is None:
        pytest.skip("pywinauto not installed")

    sandbox = _build_and_install(tmp_path)
    with _arp_entry(sandbox):
        command = _read_arp("UninstallString")
        driver = subprocess.run(
            [sys.executable, "-c", _GUI_DRIVER, command, str(sandbox)],
            capture_output=True,
            text=True,
            timeout=240,
        )

    assert driver.returncode == 0, (
        f"GUI uninstall failed (rc={driver.returncode}): {driver.stdout}{driver.stderr}"
    )
    leftovers = (
        sorted(p.name for p in sandbox.iterdir() if p.name != "uninstall.exe")
        if sandbox.exists()
        else []
    )
    assert not leftovers, f"the GUI uninstall left {leftovers}"


def _uninstaller_window_titles() -> list[str]:
    """Top-level windows that look like an uninstaller, right now."""
    script = (
        "Get-Process | Where-Object {$_.MainWindowTitle -like '*Uninstall*'} "
        "| ForEach-Object { $_.MainWindowTitle }"
    )
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


@pytest.mark.integration
@pytest.mark.skipif(not _have_makensis(), reason=pytestmark_reason)
@pytest.mark.parametrize("flags", ["/S", "/S _?={box}"])
def test_a_silent_uninstall_shows_no_window(tmp_path, flags):
    """Silent must mean silent -- nothing on screen, for either silent form.

    Both are exercised because they take different code paths inside NSIS:
    plain /S copies the uninstaller to %TEMP% and relaunches it, while _?=
    runs in place. A window from either would be one a person has to dismiss,
    which defeats unattended removal.
    """
    box = _build_and_install(tmp_path)
    before = set(_uninstaller_window_titles())

    seen: list[str] = []
    stop = threading.Event()

    def watch() -> None:
        while not stop.is_set():
            for title in _uninstaller_window_titles():
                if title not in before and title not in seen:
                    seen.append(title)
            time.sleep(0.2)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        subprocess.run(
            f'"{box / "uninstall.exe"}" {flags.format(box=box)}',
            shell=True,
            check=True,
            timeout=180,
        )
        # Plain /S returns before its relaunched copy finishes, so keep
        # watching -- a late window still counts as a window.
        time.sleep(5)
    finally:
        stop.set()
        watcher.join(timeout=5)

    assert not seen, f"a silent uninstall put a window on screen: {seen}"

    leftovers = (
        sorted(p.name for p in box.iterdir() if p.name != "uninstall.exe")
        if box.exists()
        else []
    )
    assert not leftovers, f"silent uninstall left {leftovers}"


if sys.platform != "win32":  # pragma: no cover - the installer is Windows-only
    pytestmark = pytest.mark.skip(reason="NSIS installer is Windows-only")
