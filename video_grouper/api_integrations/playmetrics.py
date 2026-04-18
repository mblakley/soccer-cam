"""
PlayMetrics API client — pure HTTP, no browser dependencies.

PlayMetrics has a public-ish REST API behind Firebase Authentication. The
data flow is:

1. ``signInWithPassword`` against Google's Identity Toolkit, using
   PlayMetrics' Firebase Web API key, returns a Firebase ``id_token`` and
   a long-lived ``refresh_token``.
2. ``POST https://api.playmetrics.com/firebase/user/login`` with the
   ``Firebase-Token`` header and ``{current_role_id, client_type}`` body
   returns a role-scoped ``access_key``.
3. ``GET https://api.playmetrics.com/user/calendars`` with both the
   ``Firebase-Token`` and ``pm-access-key`` headers returns calendar
   objects, each containing a team with games and practices.

Subsequent runs refresh the ``id_token`` via Google's securetoken endpoint
using the cached ``refresh_token`` — only the very first login needs the
user's password. ``PlayMetricsAPI`` keeps the same public surface as the
previous Selenium implementation so callers (``PlayMetricsService``,
``MatchInfoService``, the tray onboarding wizard) work unchanged.

The Firebase Web API key is read from the ``PLAYMETRICS_FIREBASE_WEB_API_KEY``
environment variable. It must be provided at build time (or set in the
process environment) — the integration is disabled if it's missing.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, TypedDict
from urllib.parse import quote

import pytz
import requests

from video_grouper.utils.config import PlayMetricsConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

FIREBASE_SIGN_IN_URL = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
)
FIREBASE_REFRESH_URL = "https://securetoken.googleapis.com/v1/token"
PLAYMETRICS_LOGIN_URL = "https://api.playmetrics.com/firebase/user/login"
PLAYMETRICS_CALENDARS_URL = "https://api.playmetrics.com/user/calendars"

_HTTP_TIMEOUT = 30
_DEFAULT_TZ = "America/New_York"


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class TeamInfo(TypedDict, total=False):
    id: str
    name: str
    calendar_url: Optional[str]
    role_id: str


class GameInfo(TypedDict, total=False):
    id: str
    title: str
    description: str
    location: str
    start_time: datetime
    end_time: datetime
    is_game: bool
    is_home: bool
    opponent: Optional[str]
    my_team_name: str
    time: str


def _get_firebase_api_key() -> Optional[str]:
    """Read the Firebase Web API key, checking build-time secrets first.

    Returns None (instead of raising) so the integration can degrade
    gracefully — callers see ``enabled=False`` and skip the PlayMetrics
    code path entirely.
    """
    try:
        from video_grouper.utils._playmetrics_secrets import (
            PLAYMETRICS_FIREBASE_WEB_API_KEY,
        )

        return PLAYMETRICS_FIREBASE_WEB_API_KEY
    except ImportError:
        pass
    return os.environ.get("PLAYMETRICS_FIREBASE_WEB_API_KEY")


# ---------------------------------------------------------------------------
# PlayMetricsAPI
# ---------------------------------------------------------------------------


class PlayMetricsAPI:
    """Pure-HTTP PlayMetrics client.

    Public surface matches the legacy Selenium implementation so existing
    callers (PlayMetricsService, the tray wizard, tests) keep working:
    - ``login() -> bool``
    - ``get_available_teams() -> list[TeamInfo]``
    - ``get_events() -> list[GameInfo]``
    - ``get_games() -> list[GameInfo]``
    - ``find_game_for_recording(start, end) -> Optional[GameInfo]``
    - ``populate_match_info(match_info, start, end) -> bool``
    - ``close()``

    The construction-time ``team_id`` selects which calendar's games are
    returned. Set ``team_id="0"`` (or leave blank) during onboarding to
    discover all teams via ``get_available_teams``.
    """

    def __init__(self, config: Any, app_config: Any = None):
        self.config = config
        self.app_config = app_config

        if isinstance(config, PlayMetricsConfig):
            self.enabled = config.enabled
            self.username = config.username
            self.password = config.password
            self.team_id = config.team_id
            self.team_name = config.team_name
        else:
            enabled_str = str(getattr(config, "enabled", "true")).lower()
            self.enabled = enabled_str not in ("false", "0", "no")
            self.username = getattr(config, "username", None) or getattr(
                config, "email", None
            )
            self.password = getattr(config, "password", None)
            self.team_id = getattr(config, "team_id", None)
            self.team_name = getattr(config, "team_name", "Test Team")

        # Optional pre-supplied refresh_token + role_id (skips signInWithPassword)
        self.refresh_token: Optional[str] = getattr(config, "refresh_token", None)
        self.current_role_id: Optional[str] = getattr(config, "current_role_id", None)

        # Cached short-lived auth state
        self._id_token: Optional[str] = None
        self._id_token_expires_at: Optional[datetime] = None
        self._access_key: Optional[str] = None
        self._roles: Optional[List[dict]] = None

        # Public flag — preserved from the legacy class so callers that
        # check `api.logged_in` keep working.
        self.logged_in = False

        # Cache for events to avoid redundant fetches within a single run
        self.events_cache: List[GameInfo] = []
        self.last_cache_update: Optional[datetime] = None
        self.cache_duration = timedelta(hours=1)

    # ------------------------------------------------------------------
    # Resource management — kept as no-ops so callers can call .close()
    # without changing.
    # ------------------------------------------------------------------

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def close(self) -> None:
        """No-op for the HTTP client. Retained for API compatibility."""
        return None

    # ------------------------------------------------------------------
    # Timezone helper (unchanged behavior)
    # ------------------------------------------------------------------

    def _get_configured_timezone(self) -> Any:
        timezone_str = _DEFAULT_TZ
        if self.app_config and hasattr(self.app_config, "timezone"):
            timezone_str = self.app_config.timezone or _DEFAULT_TZ
        try:
            return pytz.timezone(timezone_str)
        except pytz.UnknownTimeZoneError:
            logger.warning(
                "Unknown timezone '%s', falling back to %s", timezone_str, _DEFAULT_TZ
            )
            return pytz.timezone(_DEFAULT_TZ)

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def login(self) -> bool:
        """Establish PlayMetrics auth state.

        On first call, exchanges email/password for a Firebase
        ``id_token`` + ``refresh_token`` via Google's Identity Toolkit.
        Subsequent calls are idempotent — they return immediately if the
        cached ``id_token`` is still fresh.

        Returns True on success, False otherwise. Mirrors the legacy
        contract so PlayMetricsService keeps working unchanged.
        """
        if not self.enabled:
            logger.warning("PlayMetrics integration not enabled - cannot log in")
            return False

        if not _get_firebase_api_key():
            logger.error(
                "PLAYMETRICS_FIREBASE_WEB_API_KEY is not set — PlayMetrics is disabled"
            )
            return False

        if self.logged_in and self._id_token_is_fresh():
            return True

        try:
            if self.refresh_token:
                # Skip signInWithPassword if we already have a refresh_token
                self._refresh_id_token()
            else:
                if not self.username or not self.password:
                    logger.error(
                        "PlayMetrics credentials missing — cannot sign in (need email/password)"
                    )
                    return False
                self._sign_in_with_password()

            # Always discover roles + select default after login
            self._roles = self._fetch_roles()
            if not self.current_role_id and self._roles:
                self.current_role_id = str(self._roles[0].get("id", ""))

            self.logged_in = True
            return True
        except Exception as e:
            logger.error("PlayMetrics login failed: %s", e)
            self.logged_in = False
            return False

    def _id_token_is_fresh(self) -> bool:
        return bool(
            self._id_token
            and self._id_token_expires_at
            and datetime.now(timezone.utc) < self._id_token_expires_at
        )

    def _sign_in_with_password(self) -> None:
        api_key = _get_firebase_api_key()
        response = requests.post(
            f"{FIREBASE_SIGN_IN_URL}?key={api_key}",
            json={
                "email": self.username,
                "password": self.password,
                "returnSecureToken": True,
            },
            timeout=_HTTP_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        self._id_token = data["idToken"]
        self.refresh_token = data["refreshToken"]
        expires_in = int(data.get("expiresIn", 3600))
        self._id_token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=expires_in - 60
        )
        self._access_key = None  # Bound to id_token, invalidate cached key

    def _refresh_id_token(self) -> None:
        api_key = _get_firebase_api_key()
        response = requests.post(
            f"{FIREBASE_REFRESH_URL}?key={api_key}",
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=_HTTP_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        self._id_token = data["id_token"]
        new_refresh = data.get("refresh_token")
        if new_refresh:
            self.refresh_token = new_refresh
        expires_in = int(data.get("expires_in", 3600))
        self._id_token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=expires_in - 60
        )
        self._access_key = None

    def _fetch_roles(self) -> List[dict]:
        """Discover the roles available to the current user.

        ``POST /firebase/user/login`` with an empty body returns a
        ``roles`` array. Used during onboarding to populate the team
        picker and as a side-effect to validate the id_token.
        """
        response = requests.post(
            PLAYMETRICS_LOGIN_URL,
            headers={
                "Firebase-Token": self._id_token or "",
                "Content-Type": "application/json",
            },
            data="{}",
            timeout=_HTTP_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        return payload.get("roles") or []

    def _ensure_access_key(self) -> str:
        """Exchange the current id_token for a role-scoped access_key."""
        if self._access_key:
            return self._access_key
        if not self.current_role_id:
            raise RuntimeError(
                "PlayMetrics current_role_id is not set; call login() first"
            )

        response = requests.post(
            PLAYMETRICS_LOGIN_URL,
            headers={
                "Firebase-Token": self._id_token or "",
                "Content-Type": "application/json",
            },
            json={
                "current_role_id": self.current_role_id,
                "client_type": "desktop",
            },
            timeout=_HTTP_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        access_key = data.get("access_key")
        if not access_key:
            raise RuntimeError(
                "PlayMetrics firebase/user/login returned no access_key — "
                "the stored current_role_id may no longer be valid."
            )
        self._access_key = access_key
        return access_key

    # ------------------------------------------------------------------
    # Calendar fetch
    # ------------------------------------------------------------------

    def _fetch_calendars(self, start_date: datetime, end_date: datetime) -> List[dict]:
        """Fetch raw calendar JSON for the given window.

        Retries once on 401 by refreshing both id_token and access_key.
        """
        # Ensure auth is fresh
        if not self._id_token_is_fresh():
            if self.refresh_token:
                self._refresh_id_token()
            else:
                self._sign_in_with_password()
        access_key = self._ensure_access_key()

        calendar_filter = {
            "start_date": start_date.date().isoformat(),
            "end_date": end_date.date().isoformat(),
            "limit": 100,
            "offset": 0,
            "only_my_events": True,
        }
        url = (
            f"{PLAYMETRICS_CALENDARS_URL}"
            "?populate=team,team:games,team:games:league,team:practices"
            f"&calendar_filter={quote(json.dumps(calendar_filter))}"
        )

        headers = {
            "Firebase-Token": self._id_token or "",
            "pm-access-key": access_key,
        }
        response = requests.get(url, headers=headers, timeout=_HTTP_TIMEOUT)

        if response.status_code == 401:
            # Token or access_key expired mid-session — refresh both and retry once
            self._id_token = None
            self._id_token_expires_at = None
            self._access_key = None
            if self.refresh_token:
                self._refresh_id_token()
            else:
                self._sign_in_with_password()
            access_key = self._ensure_access_key()
            headers = {
                "Firebase-Token": self._id_token or "",
                "pm-access-key": access_key,
            }
            response = requests.get(url, headers=headers, timeout=_HTTP_TIMEOUT)

        response.raise_for_status()
        data = response.json()

        if isinstance(data, dict):
            return [data]
        if not isinstance(data, list):
            logger.warning(
                "Unexpected PlayMetrics calendars response type: %s", type(data)
            )
            return []
        return data

    # ------------------------------------------------------------------
    # Public API: teams + events
    # ------------------------------------------------------------------

    def get_available_teams(self) -> List[TeamInfo]:
        """Return every team visible to the user, across all roles.

        Used by the tray onboarding wizard to populate the team picker.
        Walks every role and fetches a small calendar window per role to
        enumerate team objects, then deduplicates by team id.
        """
        if not self.enabled:
            return []

        if not self.logged_in and not self.login():
            logger.warning("PlayMetrics login failed; cannot list teams")
            return []

        seen: set = set()
        teams: List[TeamInfo] = []

        # Iterate every role (not just the current one) so the picker
        # shows everything the user has access to.
        roles = self._roles or []
        if not roles:
            return []

        original_role_id = self.current_role_id
        original_access_key = self._access_key

        try:
            for role in roles:
                role_id = str(role.get("id", ""))
                role_name = role.get("name", "")
                if not role_id:
                    continue

                # Switch to this role
                self.current_role_id = role_id
                self._access_key = None  # Force re-fetch under the new role

                # Narrow window — current month — to keep the response small
                today = datetime.now()
                start = today.replace(day=1)
                if today.month == 12:
                    end = datetime(today.year + 1, 1, 1) - timedelta(days=1)
                else:
                    end = datetime(today.year, today.month + 1, 1) - timedelta(days=1)

                try:
                    calendars = self._fetch_calendars(start, end)
                except Exception as e:
                    logger.warning(
                        "Failed to enumerate teams for role %s: %s", role_name, e
                    )
                    continue

                for cal in calendars:
                    if not isinstance(cal, dict):
                        continue
                    team = cal.get("team") or {}
                    team_id = str(team.get("id", ""))
                    team_name = team.get("name", "")
                    if not team_id or team_id in seen:
                        continue
                    seen.add(team_id)
                    display = (
                        f"{role_name} \u2014 {team_name}" if role_name else team_name
                    )
                    teams.append(
                        TeamInfo(
                            id=team_id,
                            name=display,
                            calendar_url=None,
                            role_id=role_id,
                        )
                    )
        finally:
            # Restore the originally selected role for subsequent calls
            self.current_role_id = original_role_id
            self._access_key = original_access_key

        return teams

    def get_events(self) -> List[GameInfo]:
        """Return all events (games + practices) for the configured team."""
        if not self.enabled:
            logger.warning("PlayMetrics integration not enabled")
            return []

        # Cache check
        if (
            self.last_cache_update
            and datetime.now() - self.last_cache_update < self.cache_duration
            and self.events_cache
        ):
            return list(self.events_cache)

        if not self.logged_in and not self.login():
            return []

        # Window: -1 month to +6 months — matches MatchInfoService usage
        today = datetime.now()
        start_month = today.month - 1
        start_year = today.year
        if start_month < 1:
            start_month += 12
            start_year -= 1
        start = datetime(start_year, start_month, 1)

        end_month = today.month + 6
        end_year = today.year
        while end_month > 12:
            end_month -= 12
            end_year += 1
        if end_month == 12:
            end = datetime(end_year + 1, 1, 1) - timedelta(days=1)
        else:
            end = datetime(end_year, end_month + 1, 1) - timedelta(days=1)

        try:
            calendars = self._fetch_calendars(start, end)
        except Exception as e:
            logger.error("Failed to fetch PlayMetrics calendars: %s", e)
            return []

        events = self._parse_api_calendars(calendars)
        self.events_cache = events
        self.last_cache_update = datetime.now()
        return events

    def get_games(self) -> List[GameInfo]:
        """Return only the game events from the configured team's calendar."""
        events = self.get_events()
        games = [e for e in events if e.get("is_game")]
        logger.info("PlayMetrics: %d games in calendar", len(games))
        return games

    def find_game_for_recording(
        self, recording_start: datetime, recording_end: datetime
    ) -> Optional[GameInfo]:
        """Return the calendar game that best matches a recording timespan."""
        if not self.enabled:
            return None

        games = self.get_games()
        if not games:
            return None

        local_tz = self._get_configured_timezone()
        if recording_start.tzinfo is None:
            recording_start = local_tz.localize(recording_start)
        if recording_end.tzinfo is None:
            recording_end = local_tz.localize(recording_end)
        recording_start = recording_start.astimezone(timezone.utc)
        recording_end = recording_end.astimezone(timezone.utc)

        candidates = []
        for game in games:
            game_start = game.get("start_time")
            game_end = game.get("end_time")
            if not game_start:
                continue
            if game_start.tzinfo is None:
                game_start = game_start.replace(tzinfo=timezone.utc)
            else:
                game_start = game_start.astimezone(timezone.utc)
            if game_end:
                if game_end.tzinfo is None:
                    game_end = game_end.replace(tzinfo=timezone.utc)
                else:
                    game_end = game_end.astimezone(timezone.utc)
            else:
                game_end = game_start + timedelta(hours=2)
            candidates.append((game, game_start, game_end))

        from video_grouper.utils.game_selection import select_best_game

        best = select_best_game(
            candidates,
            recording_start,
            recording_end,
            game_label_fn=lambda g: g.get("title", "Unknown"),
        )
        return best

    def populate_match_info(
        self, match_info: dict, recording_start: datetime, recording_end: datetime
    ) -> bool:
        """Populate a match_info dict from a matched game. Legacy contract."""
        if not self.enabled:
            return False
        game = self.find_game_for_recording(recording_start, recording_end)
        if not game:
            return False
        match_info["title"] = game.get("title", "")
        match_info["opponent"] = game.get("opponent", "")
        match_info["location"] = game.get("location", "")
        if game.get("start_time"):
            match_info["date"] = game["start_time"].strftime("%Y-%m-%d")
            match_info["time"] = game["start_time"].strftime("%H:%M")
        match_info["description"] = game.get("description", "")
        logger.info("Populated match info from PlayMetrics: %s", match_info["title"])
        return True

    # ------------------------------------------------------------------
    # Response parser — ported verbatim from the legacy implementation
    # ------------------------------------------------------------------

    def _parse_api_calendars(self, data: Any) -> List[GameInfo]:
        """Parse PlayMetrics calendar JSON into GameInfo dicts.

        Filters games to the configured team_id when one is set so the
        caller only sees its own games. Practices are returned alongside
        games (as ``is_game=False``) for parity with the legacy parser.
        """
        events: List[GameInfo] = []
        local_tz = self._get_configured_timezone()

        if not isinstance(data, list):
            if isinstance(data, dict):
                data = [data]
            else:
                return []

        for calendar in data:
            if not isinstance(calendar, dict):
                continue

            team = calendar.get("team") or {}
            team_id_in_response = str(team.get("id", ""))
            team_name_in_response = team.get("name", "")

            # If a specific team_id was configured, only emit events from
            # that team. team_id="0" or empty means "all teams" (used
            # during discovery).
            if (
                self.team_id
                and str(self.team_id) not in ("", "0")
                and team_id_in_response != str(self.team_id)
            ):
                continue

            for game in team.get("games") or []:
                try:
                    parsed = self._parse_game_dict(
                        game, team_name_in_response, local_tz
                    )
                    if parsed is not None:
                        events.append(parsed)
                except Exception as e:
                    logger.error("Error parsing PlayMetrics game: %s", e)

            for practice in team.get("practices") or []:
                try:
                    parsed = self._parse_practice_dict(
                        practice, team_name_in_response, local_tz
                    )
                    if parsed is not None:
                        events.append(parsed)
                except Exception as e:
                    logger.error("Error parsing PlayMetrics practice: %s", e)

        logger.info(
            "PlayMetrics: parsed %d events (%d games)",
            len(events),
            sum(1 for e in events if e.get("is_game")),
        )
        return events

    def _parse_game_dict(
        self, game: dict, team_name_in_response: str, local_tz
    ) -> Optional[GameInfo]:
        start_str = (
            game.get("start_datetime") or game.get("start_date") or game.get("date")
        )
        end_str = game.get("end_datetime") or game.get("end_date")
        if not start_str:
            return None

        start_time = self._parse_api_datetime(start_str, local_tz)
        end_time = (
            self._parse_api_datetime(end_str, local_tz)
            if end_str
            else start_time + timedelta(hours=2)
        )

        opponent = game.get("opponent_team_name", "Unknown Opponent")
        is_home = bool(game.get("is_home", False))

        field_obj = game.get("field") or {}
        location = (
            field_obj.get("display_name", "")
            or field_obj.get("facility_name", "")
            or game.get("field_name", "")
        )

        game_team_name = game.get("team_name", "") or team_name_in_response
        title = game.get("title", "")
        if not title:
            title = (
                f"{game_team_name} vs {opponent}"
                if is_home
                else f"{game_team_name} @ {opponent}"
            )

        league = game.get("league") or {}
        description = league.get("name", "")
        game_type = (game.get("extra") or {}).get("game_type", "")
        if game_type and not description:
            description = game_type

        chosen_dt = start_time
        if chosen_dt.tzinfo is None:
            chosen_dt = chosen_dt.replace(tzinfo=pytz.UTC)

        return GameInfo(
            id=str(game.get("id", hash(f"{start_time}-{title}"))),
            title=title,
            description=description,
            location=location,
            start_time=start_time,
            end_time=end_time,
            is_game=True,
            is_home=is_home,
            opponent=opponent,
            my_team_name=self.team_name or game_team_name,
            time=chosen_dt.astimezone(local_tz).strftime("%H:%M"),
        )

    def _parse_practice_dict(
        self, practice: dict, team_name_in_response: str, local_tz
    ) -> Optional[GameInfo]:
        start_str = (
            practice.get("start_datetime")
            or practice.get("start_date")
            or practice.get("date")
        )
        if not start_str:
            return None

        start_time = self._parse_api_datetime(start_str, local_tz)
        end_str = practice.get("end_datetime") or practice.get("end_date")
        end_time = (
            self._parse_api_datetime(end_str, local_tz)
            if end_str
            else start_time + timedelta(hours=1, minutes=30)
        )

        field_obj = practice.get("field") or {}
        location = (
            field_obj.get("display_name", "")
            or field_obj.get("facility_name", "")
            or practice.get("field_name", "")
        )
        title = practice.get("title") or f"{team_name_in_response} Practice"

        return GameInfo(
            id=str(practice.get("id", hash(f"{start_time}-{title}"))),
            title=title,
            description="",
            location=location,
            start_time=start_time,
            end_time=end_time,
            is_game=False,
            opponent=None,
            my_team_name=self.team_name or team_name_in_response,
        )

    @staticmethod
    def _parse_api_datetime(dt_str: str, local_tz) -> datetime:
        """Parse a PlayMetrics ISO datetime string."""
        if not dt_str:
            raise ValueError("Empty datetime string")

        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(dt_str, fmt)
                if fmt.endswith("Z"):
                    dt = dt.replace(tzinfo=pytz.UTC)
                elif dt.tzinfo is None:
                    dt = local_tz.localize(dt)
                return dt
            except ValueError:
                continue

        try:
            from dateutil import parser as dateutil_parser

            return dateutil_parser.parse(dt_str)
        except Exception as e:
            raise ValueError(f"Cannot parse datetime: {dt_str}") from e
