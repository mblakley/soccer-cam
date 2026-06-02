"""Regression tests for the AutoCam output validator.

These tests intentionally use **real** filesystem operations and **real**
PyAV. The repository's `tests/conftest.py` defines autouse fixtures that
mock both (`mock_file_system` pins `os.path.getsize` to 1 MB; `mock_ffmpeg`
intercepts `av.open`). Those mocks are the exact reason tonight's
regression slipped through unit tests: every existing AutoCam test was
asking a MagicMock whether the output was real, not the actual file.

We override both fixtures at module scope below so this file gets real
filesystem and real PyAV. Don't drop the overrides -- if they go away
the validator tests become tautologies again.
"""

from pathlib import Path

import av
import pytest

from video_grouper.tray.autocam_automation import _validate_autocam_output


@pytest.fixture(autouse=True)
def mock_file_system():
    """Override conftest autouse mock; validator needs real os.path I/O."""
    yield None


@pytest.fixture(autouse=True)
def mock_ffmpeg():
    """Override conftest autouse mock; validator needs real av.open."""
    yield None


def _write_test_mp4(
    path: Path, duration_s: float, width: int = 320, height: int = 240
) -> None:
    """Write a real H264 MP4 of ~`duration_s` seconds using PyAV.

    Solid-color frames keep the file tiny (KB-scale) regardless of
    duration, which lets the duration-based tests run a synthetic
    "1 hour" video without producing a 1-hour-sized file on disk.
    """
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

    PyAV fails to open this with "Invalid data found when processing input".
    The header bytes are copied from the first 32 bytes of the corrupt
    output that AutoCam left behind tonight.
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


class TestValidateAutocamOutput:
    """Each case below corresponds to a real-world failure mode the old
    size-only check would have passed."""

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

    def test_valid_mp4_without_input_is_accepted(self, tmp_path):
        """No input_path supplied (resume path): pass on size + moov +
        non-zero duration only."""
        out = tmp_path / "real.mp4"
        _write_test_mp4(out, duration_s=120)
        # Solid-color H264 compresses to ~KB; bypass the 10 MB absolute floor.
        ok, reason = _validate_autocam_output(str(out), min_bytes=1024)
        assert ok is True, f"expected pass, got: {reason}"

    def test_duration_parity_below_threshold_is_rejected(self, tmp_path):
        """Output duration < 95% of input = AutoCam exited mid-pass."""
        input_mp4 = tmp_path / "input.mp4"
        output_mp4 = tmp_path / "output.mp4"
        _write_test_mp4(input_mp4, duration_s=120)
        _write_test_mp4(output_mp4, duration_s=60)  # 50% parity
        ok, reason = _validate_autocam_output(
            str(output_mp4), input_path=str(input_mp4), min_bytes=1024
        )
        assert ok is False
        assert "parity" in reason.lower() or "duration" in reason.lower()

    def test_duration_parity_at_threshold_is_accepted(self, tmp_path):
        """Verifies only the parity check; the bps floor is disabled here
        because solid-color H264 compresses below 0.5 Mbps regardless of
        duration. A real soccer game won't have this problem."""
        input_mp4 = tmp_path / "input.mp4"
        output_mp4 = tmp_path / "output.mp4"
        _write_test_mp4(input_mp4, duration_s=10)
        _write_test_mp4(output_mp4, duration_s=10)
        ok, reason = _validate_autocam_output(
            str(output_mp4),
            input_path=str(input_mp4),
            min_bytes=1024,
            min_bps=0,  # disable bps floor; this case isolates the parity check
        )
        assert ok is True, f"expected pass, got: {reason}"

    def test_implausibly_small_for_input_duration_is_rejected(self, tmp_path):
        """1h input + 1h output that's only KB-large fails the 0.5 Mbps
        floor. Catches a hypothetical regression where the output runs
        full-duration but encodes nothing useful (e.g. constant black)."""
        input_mp4 = tmp_path / "input.mp4"
        output_mp4 = tmp_path / "output.mp4"
        _write_test_mp4(output_mp4, duration_s=3600)
        _write_test_mp4(input_mp4, duration_s=3600)
        ok, reason = _validate_autocam_output(
            str(output_mp4), input_path=str(input_mp4), min_bytes=1024
        )
        assert ok is False
        assert "implausibly" in reason.lower() or "mbps" in reason.lower()
