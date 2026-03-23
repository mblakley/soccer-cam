"""
Mock TTT API client for end-to-end testing.

Provides a mock implementation of TTTApiClient that stores state in memory
and returns realistic test data without making network calls.

Two pre-generated games are created at init, timed to align with the simulator's
2 groups of 3 files:
  - Group 1 (0:00-3:00 from base_time) -> Eagles game
  - Group 2 (3:10-6:10 from base_time) -> Falcons game
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

MOCK_TEAM_ID = "00000000-0000-0000-0000-000000000001"
MOCK_USER_ID = "00000000-0000-0000-0000-000000000010"


class MockTTTApiClient:
    """Mock TTT API client that stores state in memory.

    Provides the same public interface as TTTApiClient for E2E testing.
    """

    def __init__(self, base_time: Optional[datetime] = None, **kwargs: Any) -> None:
        self._base_time = base_time or datetime.now(timezone.utc)
        self._authenticated = True

        # Pre-generate two games aligned with simulator groups
        self._games = [
            {
                "game_id": str(uuid.uuid4()),
                "opponent_name": "Eagles",
                "start_time": (self._base_time + timedelta(minutes=1)).isoformat(),
                "end_time": (self._base_time + timedelta(minutes=91)).isoformat(),
                "location": "Test Field A",
                "game_type": "league",
            },
            {
                "game_id": str(uuid.uuid4()),
                "opponent_name": "Falcons",
                "start_time": (self._base_time + timedelta(minutes=4)).isoformat(),
                "end_time": (self._base_time + timedelta(minutes=94)).isoformat(),
                "location": "Test Field B",
                "game_type": "league",
            },
        ]

        self._roster = [
            {
                "user_id": MOCK_USER_ID,
                "display_name": "Coach Smith",
                "role": "coach",
                "jersey_number": None,
            },
            {
                "user_id": str(uuid.uuid4()),
                "display_name": "Player One",
                "role": "player",
                "jersey_number": 10,
            },
            {
                "user_id": str(uuid.uuid4()),
                "display_name": "Player Two",
                "role": "player",
                "jersey_number": 7,
            },
        ]

        # Mutable state for game sessions, sync anchors, moment tags/clips
        self._game_sessions: dict[str, dict[str, Any]] = {}
        self._sync_anchors: dict[str, dict[str, Any]] = {}
        self._moment_tags: dict[str, dict[str, Any]] = {}
        self._moment_clips: dict[str, dict[str, Any]] = {}
        self._clip_requests: list[dict[str, Any]] = []
        self._recordings: dict[str, dict[str, Any]] = {}
        self._recording_statuses: list[dict[str, Any]] = []

        logger.info("MockTTTApiClient initialized with base_time=%s", self._base_time)

    # ------------------------------------------------------------------
    # Auth (no-ops)
    # ------------------------------------------------------------------

    def login(self, email: str, password: str) -> None:
        logger.debug("Mock login for %s", email)
        self._authenticated = True

    def refresh_token(self) -> None:
        pass

    def is_authenticated(self) -> bool:
        return self._authenticated

    # ------------------------------------------------------------------
    # Existing clip request methods
    # ------------------------------------------------------------------

    def get_team_assignments(self) -> list[dict[str, Any]]:
        return [
            {
                "camera_manager_id": str(uuid.uuid4()),
                "team_id": MOCK_TEAM_ID,
                "team_name": "Hawks",
            }
        ]

    def get_pending_clip_requests(self) -> list[dict[str, Any]]:
        return [r for r in self._clip_requests if r["status"] == "pending"]

    def start_clip_request(self, request_id: str) -> dict[str, Any]:
        for r in self._clip_requests:
            if r["id"] == request_id:
                r["status"] = "in_progress"
                return r
        return {"id": request_id, "status": "in_progress"}

    def fulfill_clip_request(
        self, request_id: str, url: str, notes: Optional[str] = None
    ) -> dict[str, Any]:
        for r in self._clip_requests:
            if r["id"] == request_id:
                r["status"] = "fulfilled"
                r["fulfilled_url"] = url
                r["fulfilled_notes"] = notes
                return r
        return {"id": request_id, "status": "fulfilled", "fulfilled_url": url}

    # ------------------------------------------------------------------
    # Schedule & game management
    # ------------------------------------------------------------------

    def get_schedule(
        self,
        team_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        logger.debug("Mock get_schedule for team %s", team_id)
        return list(self._games)

    def get_roster(self, team_id: str) -> list[dict[str, Any]]:
        logger.debug("Mock get_roster for team %s", team_id)
        return list(self._roster)

    def auto_match_video(
        self, team_id: str, video_url: str, recorded_at: str
    ) -> dict[str, Any]:
        logger.debug("Mock auto_match_video for team %s at %s", team_id, recorded_at)
        if self._games:
            return {
                "matched": True,
                "game_id": self._games[0]["game_id"],
                "opponent_name": self._games[0]["opponent_name"],
                "message": "Matched to nearest game",
            }
        return {
            "matched": False,
            "game_id": None,
            "opponent_name": None,
            "message": "No games found",
        }

    # ------------------------------------------------------------------
    # Game sessions
    # ------------------------------------------------------------------

    def get_game_sessions(
        self,
        team_id: str,
        recording_group_dir: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        sessions = list(self._game_sessions.values())
        if recording_group_dir:
            sessions = [
                s
                for s in sessions
                if s.get("recording_group_dir") == recording_group_dir
            ]
        return sessions

    def create_game_session(
        self,
        team_id: str,
        recording_group_dir: str,
        game_date: str,
        opponent_name: str,
        video_youtube_id: Optional[str] = None,
        status: str = "recording_complete",
    ) -> dict[str, Any]:
        session_id = str(uuid.uuid4())
        session = {
            "id": session_id,
            "team_id": team_id,
            "recording_group_dir": recording_group_dir,
            "game_date": game_date,
            "opponent_name": opponent_name,
            "video_youtube_id": video_youtube_id,
            "status": status,
        }
        self._game_sessions[session_id] = session
        logger.debug("Mock created game session %s", session_id)
        return session

    def update_game_session(self, session_id: str, **fields: Any) -> dict[str, Any]:
        if session_id in self._game_sessions:
            self._game_sessions[session_id].update(fields)
            return self._game_sessions[session_id]
        return {"id": session_id, **fields}

    # ------------------------------------------------------------------
    # Time sync
    # ------------------------------------------------------------------

    def get_sync_anchors(self, game_session_id: str) -> list[dict[str, Any]]:
        return [
            a
            for a in self._sync_anchors.values()
            if a.get("game_session_id") == game_session_id
        ]

    def update_sync_anchor(self, anchor_id: str, **fields: Any) -> dict[str, Any]:
        if anchor_id in self._sync_anchors:
            self._sync_anchors[anchor_id].update(fields)
            return self._sync_anchors[anchor_id]
        return {"id": anchor_id, **fields}

    def reconcile_game_session(self, session_id: str) -> dict[str, Any]:
        logger.debug("Mock reconcile for session %s", session_id)
        return {
            "sync_status": "synced",
            "true_recording_start": self._base_time.isoformat(),
            "tags_updated": len(
                [
                    t
                    for t in self._moment_tags.values()
                    if t.get("game_session_id") == session_id
                ]
            ),
        }

    # ------------------------------------------------------------------
    # Moment tags & clips
    # ------------------------------------------------------------------

    def get_pending_moment_tags(self, game_session_id: str) -> list[dict[str, Any]]:
        return [
            t
            for t in self._moment_tags.values()
            if t.get("game_session_id") == game_session_id
            and t.get("video_offset_seconds") is None
        ]

    def update_moment_tag(self, tag_id: str, **fields: Any) -> dict[str, Any]:
        if tag_id in self._moment_tags:
            self._moment_tags[tag_id].update(fields)
            return self._moment_tags[tag_id]
        return {"id": tag_id, **fields}

    def create_moment_clip(
        self,
        moment_tag_id: str,
        game_session_id: str,
        clip_start_offset: float,
        clip_end_offset: float,
        clip_duration: float = 30.0,
    ) -> dict[str, Any]:
        clip_id = str(uuid.uuid4())
        clip = {
            "id": clip_id,
            "moment_tag_id": moment_tag_id,
            "game_session_id": game_session_id,
            "clip_start_offset": clip_start_offset,
            "clip_end_offset": clip_end_offset,
            "clip_duration": clip_duration,
            "status": "pending",
        }
        self._moment_clips[clip_id] = clip
        logger.debug("Mock created moment clip %s", clip_id)
        return clip

    # ------------------------------------------------------------------
    # Recording pipeline reporting
    # ------------------------------------------------------------------

    def register_recordings(
        self, camera_id: str, team_id: str, files: list[dict]
    ) -> list[dict[str, Any]]:
        logger.debug(
            "Mock register_recordings for camera %s: %d file(s)", camera_id, len(files)
        )
        registered = []
        for f in files:
            rec_id = str(uuid.uuid4())
            rec = {
                "id": rec_id,
                "camera_id": camera_id,
                "team_id": team_id,
                "file_name": f.get("file_name"),
                "file_group": f.get("file_group"),
                "recording_start": f.get("recording_start"),
                "recording_end": f.get("recording_end"),
                "status": "registered",
            }
            self._recordings[rec_id] = rec
            registered.append(rec)
        return registered

    def update_recording_status(
        self,
        recording_id: str,
        stage: str,
        status: str,
        error_message: Optional[str] = None,
        youtube_url: Optional[str] = None,
        youtube_video_id: Optional[str] = None,
    ) -> dict[str, Any]:
        logger.debug(
            "Mock update_recording_status %s: %s=%s", recording_id, stage, status
        )
        entry = {
            "recording_id": recording_id,
            "stage": stage,
            "status": status,
            "error_message": error_message,
            "youtube_url": youtube_url,
            "youtube_video_id": youtube_video_id,
        }
        self._recording_statuses.append(entry)
        if recording_id in self._recordings:
            self._recordings[recording_id][f"{stage}_status"] = status
        return entry

    def get_high_water_mark(self, camera_id: str) -> Optional[str]:
        logger.debug("Mock get_high_water_mark for camera %s", camera_id)
        # Return the latest recording_end among all recordings for this camera
        latest = None
        for rec in self._recordings.values():
            if rec.get("camera_id") == camera_id:
                end = rec.get("recording_end")
                if end and (latest is None or end > latest):
                    latest = end
        return latest

    def update_moment_clip(self, clip_id: str, **fields: Any) -> dict[str, Any]:
        if clip_id in self._moment_clips:
            self._moment_clips[clip_id].update(fields)
            return self._moment_clips[clip_id]
        return {"id": clip_id, **fields}
