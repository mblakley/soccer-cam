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
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

MOCK_TEAM_ID = "00000000-0000-0000-0000-000000000001"
MOCK_USER_ID = "00000000-0000-0000-0000-000000000010"


class MockTTTApiClient:
    """Mock TTT API client that stores state in memory.

    Provides the same public interface as TTTApiClient for E2E testing.
    """

    def __init__(self, base_time: datetime | None = None, **kwargs: Any) -> None:
        self._base_time = base_time or datetime.now(UTC)
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

        # player_id is the stable identity for every row; user_id is only
        # present for login-backed staff (coaches/managers) — accountless
        # youth players have user_id=None. Mirrors TTT's roster contract
        # (device-link/roster) post-PR #126.
        self._roster = [
            {
                "player_id": str(uuid.uuid4()),
                "user_id": MOCK_USER_ID,
                "full_name": "Coach Smith",
                "role": "coach",
                "jersey_number": None,
            },
            {
                "player_id": str(uuid.uuid4()),
                "user_id": None,
                "full_name": "Player One",
                "role": "player",
                "jersey_number": 10,
            },
            {
                "player_id": str(uuid.uuid4()),
                "user_id": None,
                "full_name": "Player Two",
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
        self._highlights: list[dict[str, Any]] = []
        self._highlight_game_clips: dict[str, list[dict[str, Any]]] = {}
        self._highlight_moment_clips: dict[str, list[dict[str, Any]]] = {}
        self._recordings: dict[str, dict[str, Any]] = {}
        self._recording_statuses: list[dict[str, Any]] = []
        self._pending_commands: list[dict[str, Any]] = []
        self._auto_record_rules: dict[str, Any] = {}
        self._acknowledged_commands: list[str] = []
        self._completed_commands: list[dict[str, Any]] = []

        # Tracks calls to register_as_camera_manager so tests can assert
        # the post-sign-in claim fired exactly once. ``register_response``
        # is what subsequent calls return; defaults to the same single-team
        # shape used by get_team_assignments() but tests can override.
        self.register_camera_manager_call_count: int = 0
        self.register_camera_manager_response: list[dict[str, Any]] = [
            {
                "id": str(uuid.uuid4()),
                "team_id": MOCK_TEAM_ID,
                "user_id": MOCK_USER_ID,
                "email": "coach@example.com",
                "name": "Coach Smith",
                "created_at": self._base_time.isoformat(),
            }
        ]

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

    def register_as_camera_manager(self) -> list[dict[str, Any]]:
        """Mock auto-claim — returns the configured response and counts calls."""
        self.register_camera_manager_call_count += 1
        logger.debug(
            "Mock register_as_camera_manager call #%d",
            self.register_camera_manager_call_count,
        )
        # Return a shallow copy so callers can't mutate the canned response.
        return list(self.register_camera_manager_response)

    def get_pending_clip_requests(self) -> list[dict[str, Any]]:
        return [r for r in self._clip_requests if r["status"] == "pending"]

    def start_clip_request(self, request_id: str) -> dict[str, Any]:
        for r in self._clip_requests:
            if r["id"] == request_id:
                r["status"] = "in_progress"
                return r
        return {"id": request_id, "status": "in_progress"}

    def fulfill_clip_request(
        self, request_id: str, url: str, notes: str | None = None
    ) -> dict[str, Any]:
        for r in self._clip_requests:
            if r["id"] == request_id:
                r["status"] = "fulfilled"
                r["fulfilled_url"] = url
                r["fulfilled_notes"] = notes
                return r
        return {"id": request_id, "status": "fulfilled", "fulfilled_url": url}

    # ------------------------------------------------------------------
    # Highlight reels
    # ------------------------------------------------------------------

    def get_pending_highlights(
        self, camera_id: str | None = None
    ) -> list[dict[str, Any]]:
        # camera_id is accepted but ignored (mirrors v1 TTT behavior).
        return [r for r in self._highlights if r.get("status") == "pending"]

    def get_highlight_game_clips(self, reel_id: str) -> list[dict[str, Any]]:
        return list(self._highlight_game_clips.get(reel_id, []))

    def get_highlight_moment_clips(self, reel_id: str) -> list[dict[str, Any]]:
        return list(self._highlight_moment_clips.get(reel_id, []))

    def get_highlight(self, reel_id: str) -> dict[str, Any]:
        for r in self._highlights:
            if r["id"] == reel_id:
                return dict(r)
        return {"id": reel_id, "status": "pending"}

    def claim_highlight(self, reel_id: str, camera_id: str) -> dict[str, Any] | None:
        """Return updated reel on success; return None if already claimed (409)."""
        for r in self._highlights:
            if r["id"] == reel_id:
                if r.get("status") != "pending":
                    return None
                r["status"] = "generating"
                r["claimed_by_camera_id"] = camera_id
                return dict(r)
        return {
            "id": reel_id,
            "status": "generating",
            "claimed_by_camera_id": camera_id,
        }

    def report_blocker(
        self, reel_id: str, camera_id: str, reason: str
    ) -> dict[str, Any] | None:
        for r in self._highlights:
            if r["id"] == reel_id:
                r["unrenderable_reason"] = reason[:500]
                return dict(r)
        return {"id": reel_id, "unrenderable_reason": reason[:500]}

    def update_highlight_progress(
        self,
        reel_id: str,
        *,
        stage: str,
        percent: int,
    ) -> dict[str, Any]:
        clamped = int(max(0, min(100, percent)))
        for r in self._highlights:
            if r["id"] == reel_id:
                r["progress_stage"] = stage
                r["progress_percent"] = clamped
                return r
        return {"id": reel_id, "progress_stage": stage, "progress_percent": clamped}

    def complete_highlight(
        self,
        reel_id: str,
        *,
        file_path: str,
        youtube_video_id: str | None,
    ) -> dict[str, Any]:
        for r in self._highlights:
            if r["id"] == reel_id:
                r["status"] = "ready"
                r["file_path"] = file_path
                r["youtube_video_id"] = youtube_video_id
                return r
        return {
            "id": reel_id,
            "status": "ready",
            "file_path": file_path,
            "youtube_video_id": youtube_video_id,
        }

    def fail_highlight(self, reel_id: str, error_message: str) -> dict[str, Any]:
        for r in self._highlights:
            if r["id"] == reel_id:
                r["status"] = "failed"
                r["error_message"] = error_message
                return r
        return {"id": reel_id, "status": "failed", "error_message": error_message}

    # ------------------------------------------------------------------
    # Schedule & game management
    # ------------------------------------------------------------------

    def get_schedule(
        self,
        team_id: str,
        start_date: str | None = None,
        end_date: str | None = None,
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
        recording_group_dir: str | None = None,
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
        video_youtube_id: str | None = None,
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

    def update_recording_step(
        self,
        recording_id: str,
        *,
        step_id: str,
        step_type: str,
        label: str,
        status: str,
        started_at: str | None = None,
        completed_at: str | None = None,
        error: str | None = None,
        config: dict[str, Any] | None = None,
        artifacts: dict[str, Any] | None = None,
        pipeline_preset: str | None = None,
    ) -> dict[str, Any]:
        logger.debug(
            "Mock update_recording_step %s: %s=%s", recording_id, step_id, status
        )
        entry: dict[str, Any] = {
            "recording_id": recording_id,
            "step_id": step_id,
            "type": step_type,
            "label": label,
            "status": status,
            "started_at": started_at,
            "completed_at": completed_at,
            "error": error,
            "config": config,
            "artifacts": artifacts,
            "pipeline_preset": pipeline_preset,
        }
        self._recording_statuses.append(entry)
        if recording_id in self._recordings:
            self._recordings[recording_id][f"{step_id}_status"] = status
        return entry

    def get_high_water_mark(self, camera_id: str) -> str | None:
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

    # ------------------------------------------------------------------
    # Command polling & auto-record rules
    # ------------------------------------------------------------------

    def get_auto_record_rules(self, camera_id: str) -> dict[str, Any] | None:
        logger.debug("Mock get_auto_record_rules for camera %s", camera_id)
        return self._auto_record_rules or None

    def get_pending_commands(self, camera_id: str) -> list[dict[str, Any]]:
        logger.debug("Mock get_pending_commands for camera %s", camera_id)
        return [c for c in self._pending_commands if c.get("status") == "pending"]

    def acknowledge_command(self, command_id: str) -> dict[str, Any]:
        logger.debug("Mock acknowledge_command %s", command_id)
        self._acknowledged_commands.append(command_id)
        for cmd in self._pending_commands:
            if cmd.get("id") == command_id:
                cmd["status"] = "acknowledged"
                return cmd
        return {"id": command_id, "status": "acknowledged"}

    def complete_command(self, command_id: str, result: dict) -> dict[str, Any]:
        logger.debug("Mock complete_command %s", command_id)
        entry = {"id": command_id, "result": result, "status": "completed"}
        self._completed_commands.append(entry)
        for cmd in self._pending_commands:
            if cmd.get("id") == command_id:
                cmd["status"] = "completed"
        return entry
