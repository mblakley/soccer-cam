"""MP4 container checks shared by every renderer-output gate.

These two functions decide whether a just-finished render produced a *real*
video or a partial. They started life inside
:mod:`video_grouper.tray.autocam_automation`, which imports ``pywinauto`` /
``win32gui`` at module top and is therefore unimportable from the Windows
service (Session 0, no desktop). Lifting them here lets the service-side
``autocam_cli`` step reuse the exact same gate instead of growing a second copy
that could drift.

Deliberately pure stdlib (no PyAV): the tray PyInstaller bundle doesn't ship
PyAV, and v0.4.11 shipped a validator whose ``av.open`` call raised
``AttributeError`` at runtime — the broad handler relabelled that as "no moov
atom", which happened to be right for the broken file and would have been
catastrophically wrong for a good one.
"""

from __future__ import annotations

import os

# Absolute size floor for a completed render. A broadcast-crop output runs
# roughly input_duration_seconds * 1 MB/s, so 10 MB is a backstop for
# empty/header-only files rather than a real quality bar; the moov-atom scan
# below is the load-bearing reject.
MIN_OUTPUT_BYTES_ABSOLUTE = 10 * 1024 * 1024  # 10 MB

# Containers the moov-atom walk understands.
_MP4_SUFFIXES = (".mp4", ".m4v", ".mov")

# Matroska/WebM. AutoCam's GUI defaults its destination to `.mkv`, and
# `get_ball_tracking_io_paths()` already takes `output_ext="mp4" | "mkv"`, so a
# non-MP4 output is a reachable configuration — and an MP4-only gate would
# reject a perfectly good render as "no moov atom".
_MATROSKA_SUFFIXES = (".mkv", ".mka", ".webm")

# EBML header ID that opens every Matroska/WebM file (RFC 8794).
_EBML_MAGIC = b"\x1a\x45\xdf\xa3"


def has_ebml_header(path: str) -> bool:
    """True if *path* starts with the EBML header ID (Matroska/WebM)."""
    try:
        with open(path, "rb") as f:
            return f.read(4) == _EBML_MAGIC
    except OSError:
        return False


def mp4_has_moov_atom(path: str) -> bool:
    """Walk top-level MP4 boxes looking for the ``moov`` atom.

    A clean exit writes ``ftyp + ... + moov + mdat`` (or, with
    ``movflags=faststart``, ``ftyp + moov + mdat``). A mid-write or truncated
    output has the ``ftyp`` box but no ``moov`` — the same shape as the 15.5 MB
    file produced 2026-06-01 that v0.4.10 wrongly marked
    ``ball_tracking_complete``.

    Returns ``False`` on any I/O error or malformed box header.
    """
    try:
        with open(path, "rb") as f:
            while True:
                header = f.read(8)
                if len(header) < 8:
                    return False
                size = int.from_bytes(header[:4], "big")
                box_type = header[4:8]
                if box_type == b"moov":
                    return True
                if size == 1:
                    # 'largesize' extension box: real size is the next 8 bytes.
                    ext = f.read(8)
                    if len(ext) < 8:
                        return False
                    size = int.from_bytes(ext, "big")
                    skip = size - 16
                elif size == 0:
                    # Box extends to EOF -- no further boxes, so no moov.
                    return False
                else:
                    skip = size - 8
                if skip < 0:
                    return False
                f.seek(skip, 1)
    except OSError:
        return False


def validate_video_output(
    output_path: str,
    input_path: str | None = None,
    min_bytes: int = MIN_OUTPUT_BYTES_ABSOLUTE,
) -> tuple[bool, str]:
    """Validate a rendered output is a real processed video, not a partial.

    Two checks, in order:

    1. Absolute size floor (default 10 MB). Cheap reject for empty or
       barely-started files; primarily a backstop in case the moov scan
       returns spuriously ``True`` on a tiny well-formed test fixture.
    2. moov-atom presence via :func:`mp4_has_moov_atom`. The moov atom is
       written only at clean exit, so its absence means the file is a partial —
       the exact 2026-06-01 failure mode where a 15.5 MB header-only MP4 passed
       the size-only check and got marked ``ball_tracking_complete``.

    Returns ``(ok, reason)``. On a ``False`` result the caller deletes the file
    as a crashed partial before retry.

    ``input_path`` is accepted for forward compatibility but unused: the
    PyAV-based duration-parity / bitrate-floor check v0.4.11 attempted was
    unreachable in the production tray binary. It returns when the tray bundle
    properly ships PyAV.
    """
    del input_path  # kept in signature for caller compatibility

    if not os.path.isfile(output_path):
        return False, f"output file does not exist: {output_path}"
    try:
        size = os.path.getsize(output_path)
    except OSError as e:
        return False, f"could not stat output: {e}"
    if size < min_bytes:
        return (
            False,
            f"output {size / 1024 / 1024:.1f} MB below "
            f"{min_bytes / 1024 / 1024:.0f} MB absolute floor",
        )

    mb = size / 1024 / 1024
    suffix = os.path.splitext(output_path)[1].lower()

    if suffix in _MATROSKA_SUFFIXES:
        # Matroska has no moov atom. We can prove it IS a Matroska file (the
        # EBML header ID is fixed by the spec) but not, without an EBML
        # parser, that the writer finalized it — so this is a weaker gate than
        # the MP4 one. Stated in the reason so a caller reading logs knows
        # exactly how much was checked.
        if not has_ebml_header(output_path):
            return False, f"output {mb:.1f} MB is not a Matroska file (no EBML header)"
        return True, f"OK: {mb:.1f} MB, EBML header present (finalization unverified)"

    if suffix and suffix not in _MP4_SUFFIXES:
        # Unknown container: the size floor is all we can honestly assert.
        return True, f"OK: {mb:.1f} MB (no container check for '{suffix}' output)"

    if not mp4_has_moov_atom(output_path):
        return (
            False,
            f"output {mb:.1f} MB has no moov atom "
            f"(the renderer exited before finalizing the MP4 container)",
        )

    return True, f"OK: {mb:.1f} MB, moov atom present"
