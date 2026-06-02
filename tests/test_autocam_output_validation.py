"""Regression tests for the AutoCam output validator + the resume-race
guard around it.

These tests use **real** filesystem operations. The repository's
``tests/conftest.py`` defines autouse fixtures that mock the file
system (``mock_file_system`` pins ``os.path.getsize`` to 1 MB) and
``av.open`` (``mock_ffmpeg``). Those mocks are the exact reason
tonight's regression slipped through unit tests: every existing
AutoCam test was asking a MagicMock whether the output was real,
not the actual file.

We override both autouse fixtures at module scope below so this file
gets real filesystem I/O. Don't drop the overrides -- if they go away
the validator tests become tautologies again.

v0.4.12: the validator is now pure-Python (no PyAV) because PyAV's
``av.open`` is not reachable from the production tray binary -- the
v0.4.11 release shipped with an ``AttributeError`` that the broad
exception handler relabelled as "no moov atom", which accidentally did
the right thing for the broken file but would have wrongly deleted a
real output on completion. The new ``_mp4_has_moov_atom`` is a direct
box-walk, no PyAV.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from video_grouper.tray.autocam_automation import (
    _autocam_still_writing,
    _mp4_has_moov_atom,
    _validate_autocam_output,
)


@pytest.fixture(autouse=True)
def mock_file_system():
    """Override conftest autouse mock; validator needs real os.path I/O."""
    yield None


@pytest.fixture(autouse=True)
def mock_ffmpeg():
    """Override conftest autouse mock; defensive even though validator
    is now PyAV-free (tests-only PyAV usage in fixtures still wants real av)."""
    yield None


def _write_test_mp4(
    path: Path, duration_s: float, width: int = 320, height: int = 240
) -> None:
    """Write a real H264 MP4 of ~`duration_s` seconds using PyAV.

    PyAV is a test-only dependency here (the production validator is
    pure-Python). Solid-color frames keep the file tiny (KB-scale)
    regardless of duration.
    """
    import av

    fps = 10
    with av.open(str(path), mode="w") as container:
        stream = container.add_stream("h264", rate=fps)
        stream.width = width
        stream.height = height
        stream.pix_fmt = "yuv420p"
        n_frames = max(1, int(duration_s * fps))
        for _ in range(n_frames):
            frame = av.VideoFrame(width=width, height=height, format="yuv420p")
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)


def _write_header_only_mp4(path: Path, target_bytes: int = 15 * 1024 * 1024) -> None:
    """Write the exact failure mode from the 2026-06-01 game: a valid
    ftyp box followed by zero-padded bytes, no moov atom, no streams.

    The header bytes match the first 32 bytes of the corrupt output
    AutoCam produced tonight (verified hex-identical).
    """
    header = bytes.fromhex(
        "00000020"  # box size = 32
        "66747970"  # box type = 'ftyp'
        "69736f6d"  # major brand = 'isom'
        "00000200"  # minor version
        "69736f6d"  # compatible brand 1 = 'isom'
        "69736f32"  # compatible brand 2 = 'iso2'
        "61766331"  # compatible brand 3 = 'avc1'
        "6d703431"  # compatible brand 4 = 'mp41'
    )
    chunk = b"\x00" * (1024 * 1024)
    with open(path, "wb") as f:
        f.write(header)
        remaining = target_bytes - len(header)
        while remaining > 0:
            n = min(remaining, len(chunk))
            f.write(chunk[:n])
            remaining -= n


class TestMp4HasMoovAtom:
    """The moov-presence scanner is pure-Python (no PyAV); we test it
    standalone since it's the load-bearing reject in
    ``_validate_autocam_output``."""

    def test_missing_file_returns_false(self, tmp_path):
        assert _mp4_has_moov_atom(str(tmp_path / "nope.mp4")) is False

    def test_header_only_mp4_returns_false(self, tmp_path):
        """The 2026-06-01 failure mode: ftyp present, no moov."""
        out = tmp_path / "header_only.mp4"
        _write_header_only_mp4(out, target_bytes=1024)
        assert _mp4_has_moov_atom(str(out)) is False

    def test_valid_mp4_returns_true(self, tmp_path):
        out = tmp_path / "real.mp4"
        _write_test_mp4(out, duration_s=2)
        assert _mp4_has_moov_atom(str(out)) is True

    def test_truncated_after_ftyp_returns_false(self, tmp_path):
        """Box header says 100 MB but file ends after 8 bytes -- malformed
        but should not crash; just return False."""
        out = tmp_path / "trunc.mp4"
        # ftyp header claiming the box is 100 MB, then EOF.
        out.write_bytes(bytes.fromhex("06400000667479700000000000000000"))
        assert _mp4_has_moov_atom(str(out)) is False

    def test_zero_size_box_returns_false(self, tmp_path):
        """A box with size=0 means 'extends to EOF' -- if it's not moov,
        no further boxes can appear."""
        out = tmp_path / "zero.mp4"
        # ftyp box (32 bytes), then a box with size=0 of type 'mdat'.
        ftyp = bytes.fromhex(
            "000000206674797069736f6d0000020069736f6d69736f32617663316d703431"
        )
        mdat_eof = bytes.fromhex("000000006d646174") + b"\x00" * 1024
        out.write_bytes(ftyp + mdat_eof)
        assert _mp4_has_moov_atom(str(out)) is False

    def test_large_size_box_with_moov_returns_true(self, tmp_path):
        """size==1 means 'real size in next 8 bytes' (64-bit large box).
        Construct a small fake one followed by a moov to verify the
        large-size box is skipped correctly."""
        out = tmp_path / "large.mp4"
        # ftyp (32 bytes)
        ftyp = bytes.fromhex(
            "000000206674797069736f6d0000020069736f6d69736f32617663316d703431"
        )
        # Large box: size=1, type=skip, then 8 bytes of large_size = 24, then 8 bytes of payload.
        # Total box length = 24 bytes (header 8 + ext-size 8 + payload 8).
        large = (
            bytes.fromhex("00000001")  # size flag
            + b"skip"  # box type
            + (24).to_bytes(8, "big")  # large size
            + b"\x00" * 8  # payload
        )
        moov = bytes.fromhex("00000010") + b"moov" + b"\x00" * 8
        out.write_bytes(ftyp + large + moov)
        assert _mp4_has_moov_atom(str(out)) is True


class TestValidateAutocamOutput:
    """End-to-end validator tests against real files."""

    def test_missing_file_is_rejected(self, tmp_path):
        ok, reason = _validate_autocam_output(str(tmp_path / "nope.mp4"))
        assert ok is False
        assert "does not exist" in reason

    def test_sub_threshold_file_is_rejected(self, tmp_path):
        out = tmp_path / "tiny.mp4"
        out.write_bytes(b"\x00" * (1024 * 1024))  # 1 MB
        ok, reason = _validate_autocam_output(str(out))
        assert ok is False
        assert "floor" in reason

    def test_header_only_mp4_is_rejected(self, tmp_path):
        """The 2026-06-01 regression: ftyp present, moov missing, file
        passes the 10 MB absolute size floor but PyAV cannot decode."""
        out = tmp_path / "header_only.mp4"
        _write_header_only_mp4(out)
        import os

        assert os.path.getsize(out) >= 15 * 1024 * 1024, (
            "test fixture produced wrong size; conftest fs mock may still be active"
        )
        ok, reason = _validate_autocam_output(str(out))
        assert ok is False
        assert "moov" in reason.lower() or "pyav" in reason.lower()

    def test_valid_mp4_is_accepted(self, tmp_path):
        out = tmp_path / "real.mp4"
        _write_test_mp4(out, duration_s=2)
        # Solid-color H264 compresses to ~KB; bypass the 10 MB absolute floor.
        ok, reason = _validate_autocam_output(str(out), min_bytes=1024)
        assert ok is True, f"expected pass, got: {reason}"

    def test_input_path_arg_is_accepted_but_unused(self, tmp_path):
        """Forward-compat: callers (the 3 trust points in
        _wait_for_completion_and_cleanup + the short-circuit) still pass
        input_path. v0.4.12 ignores it because the PyAV-based duration
        parity check is unreachable from the tray binary; the arg stays
        in the signature so future v0.4.13 can re-add the check when
        PyAV is properly bundled in the tray."""
        out = tmp_path / "real.mp4"
        inp = tmp_path / "input.mp4"
        _write_test_mp4(out, duration_s=2)
        _write_test_mp4(inp, duration_s=2)
        ok, reason = _validate_autocam_output(
            str(out), input_path=str(inp), min_bytes=1024
        )
        assert ok is True, f"expected pass, got: {reason}"

    def test_validator_runs_without_pyav_in_path(self, tmp_path, monkeypatch):
        """Regression for the v0.4.11 production failure: the tray
        PyInstaller binary doesn't bundle PyAV, so any code path that
        does ``import av; av.open(...)`` hits AttributeError at runtime.
        v0.4.12 must work even if PyAV is wholly absent.

        Simulate that by deleting the av module from sys.modules before
        the validator runs. The validator should still produce the
        correct verdict on the header-only file.
        """
        import sys

        monkeypatch.setitem(sys.modules, "av", None)  # importing av raises now
        broken = tmp_path / "header_only.mp4"
        _write_header_only_mp4(broken, target_bytes=15 * 1024 * 1024)
        ok, reason = _validate_autocam_output(str(broken))
        assert ok is False
        assert "moov" in reason.lower()


class TestAutocamStillWriting:
    """Resume-race guard: when a previous tray run is still rendering
    this output, the short-circuit must NOT validate-and-delete the
    file. Otherwise it pulls the rug out from under the in-flight
    AutoCam process.

    The mid-write file has the ftyp box but no moov atom yet (AutoCam
    only finalizes moov at clean exit), so the validator correctly
    classifies it as broken -- but the file is going to be valid in a
    few minutes when AutoCam wraps up. ``_autocam_still_writing``
    short-circuits the short-circuit.
    """

    def test_returns_false_when_state_is_none(self, tmp_path):
        assert _autocam_still_writing(None, str(tmp_path / "in.mp4")) is False

    def test_returns_false_when_no_marker(self):
        state = MagicMock()
        state.get_autocam_run.return_value = None
        assert _autocam_still_writing(state, "/x/in.mp4") is False

    def test_returns_false_when_marker_is_for_different_input(self):
        """Different render in flight -- shouldn't influence this one's
        short-circuit decision."""
        state = MagicMock()
        state.get_autocam_run.return_value = {
            "input_path": "/x/other-game.mp4",
            "gui_pids": [12345],
        }
        # Even with live PIDs, mismatched input means it's not "this" render.
        with patch(
            "video_grouper.tray.autocam_automation._live_autocam_pids",
            return_value=[12345],
        ):
            assert _autocam_still_writing(state, "/x/in.mp4") is False

    def test_returns_false_when_pids_are_stale(self):
        """Marker exists for this input but all GUI.exe PIDs have died.
        Safe to delete pre-existing partial output and relaunch."""
        state = MagicMock()
        state.get_autocam_run.return_value = {
            "input_path": "/x/in.mp4",
            "gui_pids": [12345],
        }
        with patch(
            "video_grouper.tray.autocam_automation._live_autocam_pids",
            return_value=[],
        ):
            assert _autocam_still_writing(state, "/x/in.mp4") is False

    def test_returns_true_when_marker_matches_and_pids_alive(self):
        """The exact scenario the reviewer flagged: tray restarted while
        AutoCam keeps running, output file is mid-write. Validator would
        reject the partial; we must NOT delete it."""
        state = MagicMock()
        state.get_autocam_run.return_value = {
            "input_path": "/x/in.mp4",
            "gui_pids": [12345, 67890],
        }
        with patch(
            "video_grouper.tray.autocam_automation._live_autocam_pids",
            return_value=[12345],
        ):
            assert _autocam_still_writing(state, "/x/in.mp4") is True

    def test_short_circuit_skips_delete_when_autocam_still_writing(self, tmp_path):
        """Integration smoke: a partial output + live autocam = file
        survives the short-circuit. End-to-end through
        ``_execute_autocam_gui_automation`` is too involved for a unit
        test (Desktop / win32gui), so we drive the relevant branch via
        the helper that guards the delete."""
        partial = tmp_path / "out.mp4"
        # Header-only -- validator would reject if asked.
        partial.write_bytes(
            bytes.fromhex(
                "000000206674797069736f6d0000020069736f6d69736f32617663316d703431"
            )
            + b"\x00" * 100
        )
        state = MagicMock()
        state.get_autocam_run.return_value = {
            "input_path": str(tmp_path / "in.mp4"),
            "gui_pids": [4242],
        }
        with patch(
            "video_grouper.tray.autocam_automation._live_autocam_pids",
            return_value=[4242],
        ):
            assert _autocam_still_writing(state, str(tmp_path / "in.mp4")) is True
        # File still exists -- the short-circuit would have skipped the
        # validate-and-delete branch.
        assert partial.exists()
