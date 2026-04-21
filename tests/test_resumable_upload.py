"""Tests for the resumable upload helper."""

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from video_grouper.utils.resumable_upload import (
    ResumableUploadError,
    upload_to_resumable_url,
)


@pytest.fixture
def tmp_file():
    fd, path = tempfile.mkstemp(suffix=".mp4")
    try:
        os.write(fd, b"\x00" * (256 * 1024 * 3 + 100))  # 3 chunks + tail
        os.close(fd)
        yield path
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _mock_response(status_code, *, headers=None, json_body=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.text = text
    if json_body is not None:
        resp.json = MagicMock(return_value=json_body)
    else:
        resp.json = MagicMock(side_effect=ValueError("no json"))
    return resp


def _patch_async_client(responses):
    """Patch httpx.AsyncClient to return a client whose put() yields given responses in order."""
    client = AsyncMock()
    client.put = AsyncMock(side_effect=responses)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return patch(
        "video_grouper.utils.resumable_upload.httpx.AsyncClient",
        return_value=ctx,
    ), client


class TestResumableUpload:
    @pytest.mark.asyncio
    async def test_happy_path_single_chunk_returns_drive_view_link(self, tmp_path):
        # Small file fits in one chunk
        f = tmp_path / "small.mp4"
        f.write_bytes(b"\x00" * 1024)

        success = _mock_response(200, json_body={"id": "drive-abc"})
        patcher, client = _patch_async_client([success])
        with patcher:
            url = await upload_to_resumable_url(
                str(f), "https://uploads.google.com/session", "video/mp4"
            )

        assert url == "https://drive.google.com/file/d/drive-abc/view"
        assert client.put.await_count == 1

    @pytest.mark.asyncio
    async def test_multi_chunk_with_308_resumes(self, tmp_file):
        # First chunk → 308, second → 200
        r1 = _mock_response(308, headers={"Range": f"bytes=0-{256 * 1024 - 1}"})
        r2 = _mock_response(308, headers={"Range": f"bytes=0-{2 * 256 * 1024 - 1}"})
        r3 = _mock_response(308, headers={"Range": f"bytes=0-{3 * 256 * 1024 - 1}"})
        r4 = _mock_response(200, json_body={"id": "drive-xyz"})
        patcher, client = _patch_async_client([r1, r2, r3, r4])
        with patcher:
            url = await upload_to_resumable_url(
                tmp_file, "https://uploads.google.com/session", "video/mp4"
            )

        assert url == "https://drive.google.com/file/d/drive-xyz/view"
        assert client.put.await_count == 4

    @pytest.mark.asyncio
    async def test_5xx_retries_then_succeeds(self, tmp_path, monkeypatch):
        """Transient 5xx is retried and eventually succeeds."""
        # Short-circuit backoff so the test is fast
        monkeypatch.setattr(
            "video_grouper.utils.resumable_upload.RETRY_BACKOFF_SECONDS",
            [0, 0, 0, 0, 0],
        )

        f = tmp_path / "s.mp4"
        f.write_bytes(b"\x00" * 512)

        bad = _mock_response(503, text="temporary")
        good = _mock_response(201, json_body={"id": "ok"})
        patcher, client = _patch_async_client([bad, good])
        with patcher:
            url = await upload_to_resumable_url(
                str(f), "https://uploads.google.com/session", "video/mp4"
            )

        assert url == "https://drive.google.com/file/d/ok/view"
        assert client.put.await_count == 2

    @pytest.mark.asyncio
    async def test_4xx_raises_immediately(self, tmp_path):
        f = tmp_path / "s.mp4"
        f.write_bytes(b"\x00" * 512)

        bad = _mock_response(403, text="forbidden")
        patcher, client = _patch_async_client([bad])
        with patcher:
            with pytest.raises(ResumableUploadError, match="403"):
                await upload_to_resumable_url(
                    str(f), "https://uploads.google.com/session", "video/mp4"
                )

    @pytest.mark.asyncio
    async def test_5xx_exhausts_retries_and_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "video_grouper.utils.resumable_upload.RETRY_BACKOFF_SECONDS",
            [0, 0, 0, 0, 0],
        )
        monkeypatch.setattr(
            "video_grouper.utils.resumable_upload.MAX_RETRIES",
            2,
        )
        f = tmp_path / "s.mp4"
        f.write_bytes(b"\x00" * 512)

        bad = _mock_response(500, text="boom")
        patcher, client = _patch_async_client([bad, bad, bad])
        with patcher:
            with pytest.raises(ResumableUploadError, match="2 retries"):
                await upload_to_resumable_url(
                    str(f), "https://uploads.google.com/session", "video/mp4"
                )

    @pytest.mark.asyncio
    async def test_empty_file_refuses(self, tmp_path, mock_file_system):
        # Autouse mock_file_system patches os.path.getsize to 1MB by default;
        # override to report 0 bytes for this single test.
        mock_file_system["getsize"].return_value = 0
        f = tmp_path / "empty.mp4"
        f.touch()

        with pytest.raises(ResumableUploadError, match="empty"):
            await upload_to_resumable_url(
                str(f), "https://uploads.google.com/session", "video/mp4"
            )

    @pytest.mark.asyncio
    async def test_webviewlink_preferred_over_id(self, tmp_path):
        f = tmp_path / "s.mp4"
        f.write_bytes(b"\x00" * 512)

        success = _mock_response(
            200,
            json_body={
                "id": "abc",
                "webViewLink": "https://drive.google.com/file/d/abc/view?usp=drivesdk",
            },
        )
        patcher, client = _patch_async_client([success])
        with patcher:
            url = await upload_to_resumable_url(
                str(f), "https://uploads.google.com/session", "video/mp4"
            )

        assert url == "https://drive.google.com/file/d/abc/view?usp=drivesdk"
