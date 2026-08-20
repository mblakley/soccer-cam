#!/usr/bin/env python3
"""Run commands on the camera's probe shell, with destructive ones refused.

The investigation builds start `tcpsvd -vE 0.0.0.0 2323 /bin/sh`, which gives
an unauthenticated root shell that survives `device` dying. That is the reason
recovery from the 2026-08-16 outage took minutes instead of waiting on a UART
cable, and it is also a loaded gun: the same shell has write access to
`/mnt/sda`, which is where every recording lives.

On 2026-08-16 a cleanup step ran `rm -f /mnt/sda/Mp4Record/*/*.mp4` intending to
remove one test clip. The glob matched everything -- 236.7 GB across 20 date
directories. The footage turned out to be archived on the server, so nothing was
ultimately lost, but that was luck, not design.

This module is the only sanctioned way to drive that shell. It refuses the
class of command that caused the incident *before* anything reaches the camera,
because a rule that nothing enforces is a comment. Cleanup is expected to name
exact paths; if that is inconvenient, leave the files.

Usage as a library:

    from camsh import sh, CameraCommandRefused
    print(sh("cat /proc/uptime"))

Usage as a CLI:

    python camsh.py 192.168.86.24 "cat /proc/uptime; ls /mnt/sda"
"""

from __future__ import annotations

import re
import socket
import sys
from collections.abc import Iterator
from contextlib import contextmanager

DEFAULT_HOST = "192.168.86.24"
DEFAULT_PORT = 2323

# The netstate daemon's kill switch. Asserting this is legitimate as a
# deliberate, released operator act and never as build state -- see
# hold_recording_override() and verify/check_recording_default.sh.
OVERRIDE_DIR = "/mnt/sda/netstate"
OVERRIDE_FLAG = f"{OVERRIDE_DIR}/override"

# Paths that hold the only on-camera copy of anything we care about. Nothing
# this tool sends may delete, move, truncate or reformat inside them.
PROTECTED = (
    "/mnt/sda/Mp4Record",
    "/mnt/para",
    "/dev/mtd",
    "/dev/mmcblk",
    "/dev/hd/sda",
)

# (regex, why) -- checked against the whole command string.
REFUSALS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\brm\b[^\n]*[*?\[]"),
        "rm with a wildcard. This is the exact command that wiped 236.7 GB of "
        "recordings on 2026-08-16. Delete by exact full path or not at all.",
    ),
    (
        re.compile(r"\brm\b[^\n]*(-[a-zA-Z]*r|--recursive)"),
        "recursive rm. Remove individual files by exact path instead.",
    ),
    (
        re.compile(r"\b(mkfs|fdisk|parted|flash_erase|nandwrite|dd)\b"),
        "a command that writes raw block or flash devices. Flashing goes "
        "through flash/flash_pak.py, which verifies a CRC first.",
    ),
    (
        re.compile(r"\b(reboot|halt|poweroff|shutdown)\b"),
        "a reboot. Reboot deliberately and explicitly, not as a side effect of "
        "a command batch -- the camera boots recording-enabled and writes a "
        "~200 MB stub before the netstate daemon disables it.",
    ),
    (
        re.compile(r">\s*/dev/(mtd|mmcblk|hd/sda)"),
        "a redirect onto a raw device node.",
    ),
    (
        re.compile(r"\b(mv|cp)\b[^\n]*/mnt/sda/Mp4Record"),
        "moving or overwriting inside Mp4Record.",
    ),
    (
        re.compile(r"\btouch\b[^\n]*/mnt/sda/netstate/override"),
        "asserting the netstate override. That flag makes the daemon yield and "
        "leaves the camera recording at home; see "
        "verify/check_recording_default.sh.",
    ),
)


class CameraCommandRefused(RuntimeError):
    """Raised instead of sending a command that could destroy data."""


class RecordingOverrideStuck(RuntimeError):
    """Raised when the override flag survives a release attempt.

    Loud on purpose: while it is set the camera records at home, and nothing
    else in the system will notice.
    """


def check(cmd: str) -> None:
    """Raise CameraCommandRefused if `cmd` is in the forbidden class."""
    for pattern, why in REFUSALS:
        if pattern.search(cmd):
            raise CameraCommandRefused(
                f"refusing to send {why}\n  command: {cmd.strip()[:200]}"
            )

    # A destructive verb aimed at a protected path, even without a wildcard.
    if re.search(r"\b(rm|mv|truncate|shred)\b", cmd):
        for path in PROTECTED:
            if path in cmd:
                raise CameraCommandRefused(
                    f"refusing to send a destructive command touching {path}\n"
                    f"  command: {cmd.strip()[:200]}"
                )


def _send_bytes(
    cmd: str, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: int = 40
) -> bytes:
    """Put `cmd` on the wire and return its output as raw bytes. NO refusal checks.

    Private. Every caller from outside this module goes through `sh()` or
    `sh_bytes()`, which check first; the only other user is the override pair
    below, which sends two fixed strings and no free-form input.
    """
    s = socket.create_connection((host, port), timeout=timeout)
    try:
        s.sendall((cmd + "\n").encode())
        s.shutdown(socket.SHUT_WR)
        chunks = []
        try:
            while True:
                buf = s.recv(65536)
                if not buf:
                    break
                chunks.append(buf)
        except TimeoutError:
            pass
    finally:
        s.close()
    return b"".join(chunks)


def _send(
    cmd: str, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: int = 40
) -> str:
    return _send_bytes(cmd, host, port, timeout).decode(errors="replace")


def sh(
    cmd: str, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: int = 40
) -> str:
    """Send one command set to the probe shell and return its combined output.

    The listener runs `sh` per connection: it reads stdin to EOF, executes, and
    exits. So one call is one batch, and state does not carry between calls.
    """
    check(cmd)
    return _send(cmd, host, port, timeout)


def sh_bytes(
    cmd: str, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: int = 40
) -> bytes:
    """`sh()` without the decode, for retrieving a binary file with `cat`.

    `cat` through this shell is binary-safe in both directions -- a 16.6 MB
    transfer came back with an exact md5 -- and the device has no `base64`, so
    raw `cat` is the retrieval path rather than an encoding. Decoding it as text
    would corrupt every byte that is not valid UTF-8, which for an image plane
    is most of them. Refusal checks are identical to `sh()`; only the return
    type differs.
    """
    check(cmd)
    return _send_bytes(cmd, host, port, timeout)


def hold_recording_override(
    on: bool, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT
) -> str:
    """Assert or release /mnt/sda/netstate/override as a deliberate operator act.

    `check()` refuses `touch .../netstate/override` in a general command, and
    must keep doing so: the 2026-08-16 incident was an *init script* asserting
    that flag at every boot, so the camera recorded at home invisibly. That is a
    build defect and `verify/check_recording_default.sh` gates paks against it
    independently of this module.

    Capturing a sample recording at home is the one legitimate reason to hold
    the flag, and it needs to be expressible without weakening the general rule
    or tempting anyone to word their way around the regex. So it lives here: a
    named function that touches exactly one path, takes no free-form command,
    and pairs with its own release. Every other refusal still applies to
    everything else.

    Prefer `recording_override_held()` over calling this directly: it releases
    in a `finally` and then *verifies* the flag is gone, so a crash between hold
    and release cannot leave the camera recording at home. Releasing also lets
    S99_NetState resume and clean this boot's stubs, which is how test clips get
    tidied without anyone running `rm` near Mp4Record.
    """
    if on:
        cmd = f"mkdir -p {OVERRIDE_DIR} && touch {OVERRIDE_FLAG} && echo held"
    else:
        # Exact path, no wildcard -- allowed by check(), but we bypass it here
        # too so the pair is symmetric and auditable in one place. Report the
        # post-state rather than the exit code: `rm -f` succeeds on a path it
        # did not remove, so only an existence test proves the release.
        cmd = (
            f"rm -f {OVERRIDE_FLAG}; "
            f"[ -e {OVERRIDE_FLAG} ] && echo STILL-HELD || echo released"
        )
    return _send(cmd, host, port)


@contextmanager
def recording_override_held(
    host: str = DEFAULT_HOST, port: int = DEFAULT_PORT
) -> Iterator[None]:
    """Hold the override for the duration of the block, then prove it released.

    The docstring above used to say callers *must* release in a try/finally.
    That is an instruction, not a guard, and this project's rule is that a
    guard nothing enforces is a comment: the failure it invites -- an exception
    between hold and release -- leaves the camera recording at home silently,
    discovered only by noticing the card filling. Exactly the 2026-08-16 shape,
    reached by a different road.

    So the release is structural. It runs in `finally`, and it then tests for
    the flag's absence rather than trusting `rm`'s exit code; if the flag is
    still there the block raises, because a hold believed released is worse
    than one known held.

    Not covered: a hold that outlives this *process* (a hard kill between the
    two calls). Closing that needs a camera-side self-expiring watchdog, which
    is unverified on hardware and therefore not shipped -- see the note in
    docs/FPS_CEILING.md rather than assuming this is airtight.
    """
    hold_recording_override(True, host, port)
    try:
        yield
    finally:
        out = hold_recording_override(False, host, port)
        if "released" not in out:
            raise RecordingOverrideStuck(
                "the recording override did not release; the camera may still "
                f"be recording at home. Remove {OVERRIDE_FLAG} by hand.\n"
                f"  camera said: {out.strip()[:200]}"
            )


def _selftest() -> int:
    blocked = [
        "rm -f /mnt/sda/Mp4Record/*/*.mp4",
        "rm -rf /mnt/sda/stitchprobe",
        "rm /mnt/sda/Mp4Record/2026-08-16/clip.mp4",
        "dd if=/dev/zero of=/dev/mtd5",
        "reboot",
        "touch /mnt/sda/netstate/override",
        "cat /proc/uptime; rm -f /tmp/*.bin",
    ]
    allowed = [
        "cat /proc/uptime",
        "rm -f /tmp/dev.log",
        "ls -la /mnt/sda/Mp4Record",
        "cat /proc/hdal/venc/top",
        "md5sum /etc/init.d/start_app",
    ]
    fails = 0
    for c in blocked:
        try:
            check(c)
            print(f"  [FAIL] should have been refused: {c}")
            fails += 1
        except CameraCommandRefused:
            print(f"  [ok]   refused: {c}")
    for c in allowed:
        try:
            check(c)
            print(f"  [ok]   allowed: {c}")
        except CameraCommandRefused as e:
            print(f"  [FAIL] should have been allowed: {c}  ({e})")
            fails += 1
    total = len(blocked) + len(allowed)

    # The override context manager, exercised against a fake camera. These are
    # the gates that matter most: the flag left set is the failure that recorded
    # at home for hours without anyone noticing.
    global _send  # noqa: PLW0603 -- test seam; restored below
    real_send = _send
    for name, ok, body, camera_says in (
        ("releases on the happy path", True, lambda: None, "released"),
        (
            "releases when the block raises",
            True,
            lambda: (_ for _ in ()).throw(ValueError("boom")),
            "released",
        ),
        ("raises when the flag survives release", False, lambda: None, "STILL-HELD"),
    ):
        total += 1
        sent: list[str] = []

        def fake(
            cmd: str,
            *a: object,
            says: str = camera_says,
            log: list[str] = sent,
            **k: object,
        ) -> str:
            log.append(cmd)
            return "held" if "touch" in cmd else says

        _send = fake  # type: ignore[assignment]
        stuck = False
        try:
            with recording_override_held():
                body()
        except RecordingOverrideStuck:
            stuck = True
        except ValueError:
            pass
        finally:
            _send = real_send  # type: ignore[assignment]
        released = any("rm -f" in c and OVERRIDE_FLAG in c for c in sent)
        if released and stuck is not ok:
            print(f"  [ok]   {name}")
        else:
            print(f"  [FAIL] {name}: release_attempted={released} raised={stuck}")
            fails += 1

    print(f"\n{total - fails}/{total} gates passed")
    return 1 if fails else 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "selftest":
        sys.exit(_selftest())
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    try:
        print(sh(sys.argv[2], host=sys.argv[1]))
    except CameraCommandRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        sys.exit(3)
