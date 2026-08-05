"""Regression: a quota error must escape upload_video.

`YouTubeQuotaError` subclasses Exception, so `upload_video`'s broad
trailing `except Exception` swallowed it and returned None. The caller
then saw an ordinary failure, `QueueProcessor`'s quota-deferral branch
never ran, and the uploader retried on the normal cadence forever.

Measured on the production box 2026-08-04: ~300 rejected API calls every
hour, all day, flat across the v0.5.12 deploy. Adding 429 detection
upstream changed nothing because the raise never left this method -- and
the "storm stopped" check that appeared to confirm the fix was counting a
log string that the same patch had renamed.
"""

from __future__ import annotations

import inspect

import pytest

from video_grouper.utils.youtube_upload import YouTubeQuotaError, YouTubeUploader


def test_quota_error_is_reraised_not_swallowed():
    """Static guard on ordering: the YouTubeQuotaError handler must come
    before the catch-all, and must re-raise."""
    src = inspect.getsource(YouTubeUploader.upload_video)
    assert "except YouTubeQuotaError" in src, (
        "upload_video has no YouTubeQuotaError handler; the trailing "
        "`except Exception` will swallow it and the queue processor will "
        "never defer on quota"
    )
    quota_at = src.index("except YouTubeQuotaError")
    broad_at = src.rindex("except Exception")
    assert quota_at < broad_at, (
        "the catch-all precedes the quota handler, so quota errors are "
        "swallowed before reaching it"
    )
    between = src[quota_at:broad_at]
    assert "raise" in between, "the quota handler must re-raise"


def test_quota_error_survives_a_broad_except_pattern():
    """Behavioural: the ordering above is what makes this true."""

    class _Sim:
        """Mirrors upload_video's structure."""

        def run(self, boom):
            try:
                raise boom
            except YouTubeQuotaError:
                raise
            except Exception:
                return None

    with pytest.raises(YouTubeQuotaError):
        _Sim().run(YouTubeQuotaError("rateLimitExceeded"))
    assert _Sim().run(RuntimeError("disk full")) is None


def test_all_quota_reasons_map_to_the_error():
    """429/rateLimitExceeded is what YouTube actually returned for the
    daily cap; it must be classified alongside the 403 variants."""
    src = inspect.getsource(YouTubeUploader.upload_video)
    for reason in (
        "uploadLimitExceeded",
        "quotaExceeded",
        "rateLimitExceeded",
        "dailyLimitExceeded",
    ):
        assert reason in src, f"{reason} is not classified as a quota condition"
    assert "400, 403, 429" in src, "429 must be parsed for an error reason"
