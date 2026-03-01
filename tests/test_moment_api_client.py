"""Unit tests for MomentApiClient with mocked httpx."""

import pytest
import pytest_asyncio
import httpx
from unittest.mock import AsyncMock, MagicMock

from video_grouper.api_integrations.moment_api_client import MomentApiClient


@pytest_asyncio.fixture
async def client():
    """Create a MomentApiClient with a mocked httpx transport."""
    c = MomentApiClient.__new__(MomentApiClient)
    c._base_url = "http://test:8000"
    c._client = AsyncMock()
    yield c


def _ok_response(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


def _error_response(status_code=500):
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
    )
    return resp


# ---------------------------------------------------------------------------
# get_game_session_by_dir
# ---------------------------------------------------------------------------


class TestGetGameSessionByDir:
    @pytest.mark.asyncio
    async def test_returns_first_match(self, client):
        client._client.get.return_value = _ok_response(
            [{"id": "gs-1", "recording_group_dir": "2026-01-15"}]
        )
        result = await client.get_game_session_by_dir("2026-01-15")
        assert result["id"] == "gs-1"
        client._client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_none_when_empty(self, client):
        client._client.get.return_value = _ok_response([])
        result = await client.get_game_session_by_dir("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_error(self, client):
        client._client.get.return_value = _error_response()
        result = await client.get_game_session_by_dir("test")
        assert result is None


# ---------------------------------------------------------------------------
# get_pending_tags
# ---------------------------------------------------------------------------


class TestGetPendingTags:
    @pytest.mark.asyncio
    async def test_returns_tags(self, client):
        tags = [{"id": "t1"}, {"id": "t2"}]
        client._client.get.return_value = _ok_response(tags)
        result = await client.get_pending_tags("gs-1")
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self, client):
        client._client.get.return_value = _error_response()
        result = await client.get_pending_tags("gs-1")
        assert result == []


# ---------------------------------------------------------------------------
# update_tag_offset
# ---------------------------------------------------------------------------


class TestUpdateTagOffset:
    @pytest.mark.asyncio
    async def test_updates_both_offsets(self, client):
        client._client.patch.return_value = _ok_response(
            {"id": "t1", "video_offset_seconds": 120.0, "trimmed_offset_seconds": 90.0}
        )
        result = await client.update_tag_offset("t1", 120.0, 90.0)
        assert result["video_offset_seconds"] == 120.0
        call_args = client._client.patch.call_args
        payload = call_args.kwargs["json"]
        assert payload["video_offset_seconds"] == 120.0
        assert payload["trimmed_offset_seconds"] == 90.0

    @pytest.mark.asyncio
    async def test_updates_video_offset_only(self, client):
        client._client.patch.return_value = _ok_response({"id": "t1"})
        await client.update_tag_offset("t1", 60.0)
        payload = client._client.patch.call_args.kwargs["json"]
        assert "trimmed_offset_seconds" not in payload


# ---------------------------------------------------------------------------
# create_clip
# ---------------------------------------------------------------------------


class TestCreateClip:
    @pytest.mark.asyncio
    async def test_creates_clip(self, client):
        client._client.post.return_value = _ok_response(
            {"id": "c1", "status": "pending"}
        )
        result = await client.create_clip("t1", "gs-1", 85.0, 115.0, 30.0)
        assert result["id"] == "c1"

    @pytest.mark.asyncio
    async def test_returns_none_on_error(self, client):
        client._client.post.return_value = _error_response()
        result = await client.create_clip("t1", "gs-1")
        assert result is None


# ---------------------------------------------------------------------------
# update_clip
# ---------------------------------------------------------------------------


class TestUpdateClip:
    @pytest.mark.asyncio
    async def test_updates_status(self, client):
        client._client.patch.return_value = _ok_response(
            {"id": "c1", "status": "ready"}
        )
        result = await client.update_clip("c1", status="ready", youtube_video_id="yt1")
        assert result["status"] == "ready"


# ---------------------------------------------------------------------------
# highlights
# ---------------------------------------------------------------------------


class TestHighlights:
    @pytest.mark.asyncio
    async def test_get_pending_highlights(self, client):
        client._client.get.return_value = _ok_response(
            [{"id": "h1", "status": "pending"}]
        )
        result = await client.get_pending_highlights()
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_get_highlight_clips(self, client):
        client._client.get.return_value = _ok_response([{"id": "c1"}, {"id": "c2"}])
        result = await client.get_highlight_clips("h1")
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_update_highlight(self, client):
        client._client.patch.return_value = _ok_response(
            {"id": "h1", "status": "ready"}
        )
        result = await client.update_highlight(
            "h1", status="ready", youtube_video_id="yt1"
        )
        assert result["status"] == "ready"
