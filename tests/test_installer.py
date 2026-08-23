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

import os
import re
import shutil
import subprocess
import sys
import tempfile
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


@pytest.mark.integration
@pytest.mark.skipif(not _have_makensis(), reason=pytestmark_reason)
def test_uninstall_removes_everything_it_installed(tmp_path):
    """The round-trip the manual debugging session needed and did not have."""
    out_exe = tmp_path / "HarnessSetup.exe"
    harness = NSI.parent / "_pytest_harness.nsi"
    harness.write_text(_harness_source(out_exe), encoding="utf-8")
    try:
        built = subprocess.run(
            [_MAKENSIS, "/V2", str(harness)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert built.returncode == 0, f"makensis failed:\n{built.stdout}{built.stderr}"
        assert out_exe.is_file(), "installer was not produced"

        # A sandbox with no space in the path: /D takes the rest of the command
        # line verbatim, so quoting a spaced path does not work.
        sandbox = Path(tempfile.mkdtemp(prefix="vg-uninst-")) / "app"
        subprocess.run(
            f'"{out_exe}" /S /D={sandbox}', shell=True, check=True, timeout=180
        )
        assert (sandbox / "uninstall.exe").is_file(), "install did not land"

        subprocess.run(
            f'"{sandbox / "uninstall.exe"}" /S', shell=True, check=True, timeout=180
        )
        # NSIS relaunches itself from %TEMP%; give it a moment to finish.
        for _ in range(40):
            if not sandbox.exists():
                break
            _sleep(0.25)

        leftovers = (
            sorted(p.name for p in sandbox.iterdir()) if sandbox.exists() else []
        )
        assert not leftovers, f"uninstall left {leftovers} in {sandbox}"
    finally:
        harness.unlink(missing_ok=True)


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


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


if sys.platform != "win32":  # pragma: no cover - the installer is Windows-only
    pytestmark = pytest.mark.skip(reason="NSIS installer is Windows-only")
