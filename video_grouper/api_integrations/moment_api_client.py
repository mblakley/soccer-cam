"""
API client for the team-tech-tools moment tagging endpoints.

Communicates with the backend using Supabase service role key auth.
"""

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# Timeout for all requests (seconds)
REQUEST_TIMEOUT = 30.0


class MomentApiClient:
    """HTTP client for the team-tech-tools moment tagging API."""

    def __init__(self, api_base_url: str, service_role_key: str):
        self._base_url = api_base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {service_role_key}"},
            timeout=REQUEST_TIMEOUT,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Game sessions
    # ------------------------------------------------------------------

    async def get_game_session_by_dir(
        self, recording_group_dir: str
    ) -> Optional[dict[str, Any]]:
        """Find a game session by its recording_group_dir."""
        try:
            resp = await self._client.get(
                "/api/game-sessions",
                params={"recording_group_dir": recording_group_dir},
            )
            resp.raise_for_status()
            sessions = resp.json()
            return sessions[0] if sessions else None
        except httpx.HTTPError as exc:
            logger.error(
                "Failed to get game session by dir %s: %s", recording_group_dir, exc
            )
            return None

    # ------------------------------------------------------------------
    # Moment tags
    # ------------------------------------------------------------------

    async def get_pending_tags(self, game_session_id: str) -> list[dict[str, Any]]:
        """Get moment tags that don't have a video_offset_seconds yet."""
        try:
            resp = await self._client.get(
                "/api/moment-tags",
                params={"game_session_id": game_session_id, "pending_offset": "true"},
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            logger.error(
                "Failed to get pending tags for session %s: %s", game_session_id, exc
            )
            return []

    async def update_tag_offset(
        self,
        tag_id: str,
        video_offset: float,
        trimmed_offset: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        """Update a moment tag's computed offsets."""
        payload: dict[str, Any] = {"video_offset_seconds": video_offset}
        if trimmed_offset is not None:
            payload["trimmed_offset_seconds"] = trimmed_offset
        try:
            resp = await self._client.patch(f"/api/moment-tags/{tag_id}", json=payload)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            logger.error("Failed to update tag offset %s: %s", tag_id, exc)
            return None

    # ------------------------------------------------------------------
    # Moment clips
    # ------------------------------------------------------------------

    async def create_clip(
        self,
        moment_tag_id: str,
        game_session_id: str,
        clip_start: Optional[float] = None,
        clip_end: Optional[float] = None,
        clip_duration: float = 30.0,
    ) -> Optional[dict[str, Any]]:
        """Create a moment clip record."""
        payload: dict[str, Any] = {
            "moment_tag_id": moment_tag_id,
            "game_session_id": game_session_id,
            "clip_duration": clip_duration,
        }
        if clip_start is not None:
            payload["clip_start_offset"] = clip_start
        if clip_end is not None:
            payload["clip_end_offset"] = clip_end
        try:
            resp = await self._client.post("/api/moment-clips", json=payload)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            logger.error("Failed to create clip for tag %s: %s", moment_tag_id, exc)
            return None

    async def update_clip(
        self,
        clip_id: str,
        *,
        status: Optional[str] = None,
        file_path: Optional[str] = None,
        youtube_video_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Update a moment clip's status, file_path, or youtube_video_id."""
        payload: dict[str, Any] = {}
        if status is not None:
            payload["status"] = status
        if file_path is not None:
            payload["file_path"] = file_path
        if youtube_video_id is not None:
            payload["youtube_video_id"] = youtube_video_id
        try:
            resp = await self._client.patch(
                f"/api/moment-clips/{clip_id}", json=payload
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            logger.error("Failed to update clip %s: %s", clip_id, exc)
            return None

    # ------------------------------------------------------------------
    # Highlights
    # ------------------------------------------------------------------

    async def get_pending_highlights(self) -> list[dict[str, Any]]:
        """Get highlight reels with status='pending'."""
        try:
            resp = await self._client.get(
                "/api/highlights", params={"status": "pending"}
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            logger.error("Failed to get pending highlights: %s", exc)
            return []

    async def get_highlight_clips(self, highlight_id: str) -> list[dict[str, Any]]:
        """Get clips linked to a highlight reel, ordered by sequence."""
        try:
            resp = await self._client.get(f"/api/highlights/{highlight_id}/clips")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            logger.error("Failed to get highlight clips %s: %s", highlight_id, exc)
            return []

    async def update_highlight(
        self,
        highlight_id: str,
        *,
        status: Optional[str] = None,
        file_path: Optional[str] = None,
        youtube_video_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Update a highlight reel's status, file_path, or youtube_video_id."""
        payload: dict[str, Any] = {}
        if status is not None:
            payload["status"] = status
        if file_path is not None:
            payload["file_path"] = file_path
        if youtube_video_id is not None:
            payload["youtube_video_id"] = youtube_video_id
        try:
            resp = await self._client.patch(
                f"/api/highlights/{highlight_id}", json=payload
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            logger.error("Failed to update highlight %s: %s", highlight_id, exc)
            return None
