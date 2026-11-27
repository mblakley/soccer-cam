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

import pytest

from video_grouper.utils.youtube_upload import (
    YouTubeChannelNotVerifiedError,
    YouTubeQuotaError,
    YouTubeUploader,
    YouTubeUploadRejectedError,
)


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
    """No upload is attempted at all while the channel is unverified.

    This is deliberately stricter than YouTube's own rule. YouTube only
    refuses videos over 15 minutes, so a short clip would technically go
    through -- but soccer-cam exists to deliver full games, and a channel that
    cannot receive them is not usable. Length is not consulted.
    """

    @pytest.mark.parametrize("status", ["eligible", "disallowed"])
    def test_blocks_every_upload_when_unverified(self, tmp_path, status):
        video = tmp_path / "game.mp4"
        video.write_bytes(b"x" * 32)
        up = _uploader(long_uploads_status=status)

        with pytest.raises(YouTubeChannelNotVerifiedError) as exc:
            up.upload_video(str(video), "t", "d")

        assert "youtube.com/verify" in str(exc.value)
        assert "15 minutes" in str(exc.value)
        up.youtube.videos.return_value.insert.assert_not_called()

    def test_length_is_not_consulted(self, tmp_path):
        """A two-second clip is blocked exactly like a 90-minute game."""
        video = tmp_path / "tiny.mp4"
        video.write_bytes(b"x" * 8)
        up = _uploader(long_uploads_status="eligible")

        with pytest.raises(YouTubeChannelNotVerifiedError):
            up.upload_video(str(video), "t", "d")
        up.youtube.videos.return_value.insert.assert_not_called()

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
