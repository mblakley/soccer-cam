"""YouTube can accept an upload and then destroy the video.

Measured 2026-08-23 against a real unverified channel, on both the multipart
and the resumable path that production uses:

    videos.insert    -> 200 with a real video id
    t+10s            -> uploadStatus="uploaded", rejectionReason=None
    t+20s onward     -> videos.list returns zero items, permanently
    uploads playlist -> "Deleted video"

Games are 60-90 minutes and unverified channels cap at 15, so this loses every
game silently: the old code returned the id, and the caller recorded the game
as uploaded. These tests cover the two defences -- refuse before uploading, and
disbelieve a returned id until the video is actually there.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import av
import pytest

from video_grouper.utils.youtube_upload import (
    YouTubeChannelNotVerifiedError,
    YouTubeQuotaError,
    YouTubeUploader,
    YouTubeUploadRejectedError,
)

# conftest's autouse mock_ffmpeg patches av.open during every test; keep the
# real one for the tests that read real files.
_REAL_AV_OPEN = av.open


def _uploader(long_uploads_status=None):
    up = YouTubeUploader.__new__(YouTubeUploader)
    up.credentials_file = "c.json"
    up.token_file = "t.json"
    up.youtube = MagicMock()
    # Only "allowed" is ever cached; any other status is read from the channel.
    up._long_uploads_status = (
        long_uploads_status if long_uploads_status == "allowed" else None
    )
    if long_uploads_status and long_uploads_status != "allowed":
        up.youtube.channels.return_value.list.return_value = _channels_response(
            long_uploads_status
        )
    up.skip_upload = False
    return up


def _channels_response(status):
    items = [] if status == "__none__" else [{"status": {"longUploadsStatus": status}}]
    resp = MagicMock()
    resp.execute.return_value = {"items": items}
    return resp


class TestUploadGate:
    """Long uploads wait for channel verification; short ones go through.

    Unverified channels keep videos up to 15 minutes and destroy longer ones
    after accepting them (measured 2026-11-27: 15 s..15:30 kept, 20:00 and
    60:00 destroyed within 15 s). So only uploads over 15:00 -- or whose
    length can't be read -- are held; clips and reels upload normally.
    """

    @staticmethod
    def _duration(seconds):
        return patch(
            "video_grouper.utils.youtube_upload._video_duration_seconds",
            return_value=seconds,
        )

    @pytest.mark.parametrize("status", ["eligible", "disallowed"])
    def test_holds_a_long_upload_on_an_unverified_channel(self, tmp_path, status):
        video = tmp_path / "game.mp4"
        video.write_bytes(b"x" * 32)
        up = _uploader(long_uploads_status=status)

        with (
            self._duration(90 * 60),
            pytest.raises(YouTubeChannelNotVerifiedError) as exc,
        ):
            up.upload_video(str(video), "t", "d")

        assert "youtube.com/verify" in str(exc.value)
        assert "15 minutes" in str(exc.value)
        up.youtube.videos.return_value.insert.assert_not_called()

    def test_unreadable_length_is_treated_as_long(self, tmp_path):
        video = tmp_path / "game.mp4"
        video.write_bytes(b"not a video")
        up = _uploader(long_uploads_status="eligible")

        with self._duration(None), pytest.raises(YouTubeChannelNotVerifiedError):
            up.upload_video(str(video), "t", "d")
        up.youtube.videos.return_value.insert.assert_not_called()

    @pytest.mark.parametrize("seconds", [30, 10 * 60, 15 * 60])
    def test_a_short_upload_goes_through_unverified(self, tmp_path, seconds):
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"x" * 32)
        up = _uploader(long_uploads_status="eligible")
        up.youtube.videos.return_value.insert.side_effect = RuntimeError("stop")

        with self._duration(seconds):
            up.upload_video(str(video), "t", "d")

        up.youtube.videos.return_value.insert.assert_called_once()
        # Short uploads never need the channel's status at all.
        up.youtube.channels.return_value.list.assert_not_called()

    def test_one_second_over_the_limit_is_held(self, tmp_path):
        video = tmp_path / "game.mp4"
        video.write_bytes(b"x" * 32)
        up = _uploader(long_uploads_status="eligible")
        with self._duration(15 * 60 + 1), pytest.raises(YouTubeChannelNotVerifiedError):
            up.upload_video(str(video), "t", "d")

    def test_reads_a_real_video_length(self, tmp_path):
        from video_grouper.utils.youtube_upload import _video_duration_seconds

        path = tmp_path / "two_seconds.mp4"
        junk = tmp_path / "junk.mp4"
        junk.write_bytes(b"not a video")
        with patch("av.open", _REAL_AV_OPEN):
            self._write_video(path)
            assert _video_duration_seconds(str(path)) == pytest.approx(2.0, abs=0.3)
            assert _video_duration_seconds(str(junk)) is None

    @staticmethod
    def _write_video(path):
        import numpy as np

        with av.open(str(path), "w") as c:
            s = c.add_stream("libx264", rate=5)
            s.width, s.height, s.pix_fmt = 64, 64, "yuv420p"
            for _ in range(10):
                frame = av.VideoFrame.from_ndarray(
                    np.zeros((64, 64, 3), dtype=np.uint8), format="rgb24"
                )
                for packet in s.encode(frame):
                    c.mux(packet)
            for packet in s.encode():
                c.mux(packet)

    def test_parks_like_a_quota_error(self):
        """The queue processor keys on YouTubeQuotaError to park and re-probe.

        Subclassing is what makes the upload resume by itself after the user
        verifies, instead of burning retries.
        """
        assert issubclass(YouTubeChannelNotVerifiedError, YouTubeQuotaError)

    def test_allows_upload_on_verified_channel(self, tmp_path):
        video = tmp_path / "game.mp4"
        video.write_bytes(b"x" * 32)
        up = _uploader(long_uploads_status="allowed")

        # Gate must not fire; the upload proceeds and fails later for an
        # unrelated reason, which is fine -- we only assert on the gate.
        up.youtube.videos.return_value.insert.side_effect = RuntimeError("stop")
        up.upload_video(str(video), "t", "d")
        up.youtube.videos.return_value.insert.assert_called_once()


class TestLongUploadsStatus:
    def test_reports_no_channel_as_unknown(self):
        """channels.list returns 200 with an empty list when the Google
        account has no YouTube channel. That is not an error, and it must not
        be mistaken for a verified channel."""
        up = _uploader()
        up.youtube.channels.return_value.list.return_value = _channels_response(
            "__none__"
        )
        assert up.get_long_uploads_status() is None

    def test_caches(self):
        up = _uploader()
        up.youtube.channels.return_value.list.return_value = _channels_response(
            "allowed"
        )
        assert up.get_long_uploads_status() == "allowed"
        assert up.get_long_uploads_status() == "allowed"
        up.youtube.channels.return_value.list.assert_called_once()


class TestSettleVerification:
    def _videos_returning(self, up, sequence):
        calls = {"n": 0}

        def _execute():
            i = min(calls["n"], len(sequence) - 1)
            calls["n"] += 1
            return {"items": sequence[i]}

        up.youtube.videos.return_value.list.return_value.execute.side_effect = _execute

    def test_disappearing_video_is_discarded(self):
        """Seen once, then gone -- exactly what the real 16-minute upload did."""
        up = _uploader()
        self._videos_returning(
            up,
            [[{"status": {"uploadStatus": "uploaded"}}], [], [], []],
        )
        with patch("video_grouper.utils.youtube_upload.time.sleep"):
            assert up._confirm_upload_settled("vid") == "discarded"

    def test_never_appearing_video_is_discarded(self):
        up = _uploader()
        self._videos_returning(up, [[]])
        with patch("video_grouper.utils.youtube_upload.time.sleep"):
            assert up._confirm_upload_settled("vid") == "discarded"

    def test_processed_is_success(self):
        up = _uploader()
        self._videos_returning(
            up,
            [
                [{"status": {"uploadStatus": "uploaded"}}],
                [{"status": {"uploadStatus": "processed"}}],
            ],
        )
        with patch("video_grouper.utils.youtube_upload.time.sleep"):
            assert up._confirm_upload_settled("vid") == "processed"

    def test_still_transcoding_is_accepted(self):
        """A real game takes a long time to transcode. Staying 'uploaded' is
        not a failure -- only disappearing is."""
        up = _uploader()
        self._videos_returning(up, [[{"status": {"uploadStatus": "uploaded"}}]])
        with patch("video_grouper.utils.youtube_upload.time.sleep"):
            assert up._confirm_upload_settled("vid") == "present"


class TestOnboardingGate:
    """The wizard must not let setup finish on a channel that cannot keep a game.

    Reaching the summary with an unverified channel means every subsequent
    game is accepted by YouTube and then deleted, with nothing in the UI ever
    having said so.
    """

    def _body(self, status):
        from video_grouper.web.setup.router import _render_youtube_body

        return _render_youtube_body(True, True, status)

    @pytest.mark.parametrize("status", ["eligible", "disallowed"])
    def test_blocks_continue_and_points_at_verification(self, status):
        body = self._body(status)
        assert "youtube.com/verify" in body
        assert "15 minutes" in body
        # Continue leads to the summary; it must not be offered here.
        assert "/setup/summary" not in body
        # Skipping YouTube entirely is still allowed.
        assert "/setup/youtube/skip" in body

    def test_allows_continue_once_verified(self):
        body = self._body("allowed")
        assert "/setup/summary" in body
        assert "youtube.com/verify" not in body

    def test_unknown_status_does_not_block(self):
        """If the probe fails we cannot claim there is a problem; the
        upload-time gate and the settle check are the backstops."""
        body = self._body(None)
        assert "/setup/summary" in body


class TestReviewFixes:
    """Code review of #145 (2026-11-27)."""

    def test_an_unverified_status_is_re_read_so_verifying_takes_effect(self):
        # The app keeps one uploader for clips/reels; caching "eligible" kept
        # uploads blocked after the user verified, until a restart.
        up = _uploader()
        up.youtube.channels.return_value.list.side_effect = [
            _channels_response("eligible"),
            _channels_response("allowed"),
        ]
        assert up.get_long_uploads_status() == "eligible"
        assert up.get_long_uploads_status() == "allowed"

    def test_status_probe_never_signs_in_interactively(self):
        # A failed refresh used to fall through to run_local_server, which
        # blocks the dashboard/wizard request thread waiting for a browser.
        up = _uploader()
        up.youtube = None
        with (
            patch(
                "video_grouper.utils.youtube_upload.ensure_valid_token",
                return_value=(False, "refresh failed"),
            ),
            patch.object(YouTubeUploader, "authenticate") as interactive,
        ):
            assert up.get_long_uploads_status() is None
        interactive.assert_not_called()

    def test_failed_status_checks_do_not_trigger_a_re_upload(self):
        # Every videos.list erroring (network, quota) is "unknown", not
        # "discarded": insert returned an id; re-uploading makes a duplicate.
        up = _uploader()
        up.youtube.videos.return_value.list.return_value.execute.side_effect = (
            RuntimeError("network")
        )
        with patch("video_grouper.utils.youtube_upload.time.sleep"):
            assert up._confirm_upload_settled("vid") == "present"

    def test_a_rejected_upload_is_a_permanent_failure(self):
        up = _uploader()
        up.youtube.videos.return_value.list.return_value.execute.return_value = {
            "items": [
                {"status": {"uploadStatus": "rejected", "rejectionReason": "duplicate"}}
            ]
        }
        with pytest.raises(YouTubeUploadRejectedError, match="duplicate"):
            up._confirm_upload_settled("vid")

    def test_a_rejection_escapes_upload_video(self, tmp_path):
        video = tmp_path / "game.mp4"
        video.write_bytes(b"x" * 32)
        up = _uploader(long_uploads_status="allowed")
        up._confirm_upload_settled = MagicMock(
            side_effect=YouTubeUploadRejectedError("rejected: duplicate")
        )
        insert = up.youtube.videos.return_value.insert.return_value
        insert.next_chunk.return_value = (None, {"id": "vid"})
        with pytest.raises(YouTubeUploadRejectedError):
            up.upload_video(str(video), "t", "d")
