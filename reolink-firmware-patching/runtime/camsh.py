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

DEFAULT_HOST = "192.168.86.24"
DEFAULT_PORT = 2323

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


def sh(
    cmd: str, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: int = 40
) -> str:
    """Send one command set to the probe shell and return its combined output.

    The listener runs `sh` per connection: it reads stdin to EOF, executes, and
    exits. So one call is one batch, and state does not carry between calls.
    """
    check(cmd)
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
    return b"".join(chunks).decode(errors="replace")


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

    Callers MUST release it -- wrap in try/finally. Releasing also lets
    S99_NetState resume and clean this boot's stubs, which is how test clips
    get tidied without anyone running `rm` near Mp4Record.
    """
    path = "/mnt/sda/netstate/override"
    if on:
        cmd = f"mkdir -p /mnt/sda/netstate && touch {path} && echo held"
    else:
        # Exact path, no wildcard -- allowed by check(), but we bypass it here
        # too so the pair is symmetric and auditable in one place.
        cmd = f"rm -f {path} && echo released"
    s = socket.create_connection((host, port), timeout=40)
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
    return b"".join(chunks).decode(errors="replace")


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
    print(
        f"\n{len(blocked) + len(allowed) - fails}/{len(blocked) + len(allowed)} gates passed"
    )
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
