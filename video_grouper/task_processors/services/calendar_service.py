"""Calendar-feed (iCal) schedule source beside TeamSnap and PlayMetrics.

Decided 2026-11-26 (training/docs/DECISIONS.md): for camera owners not using
Team Tech Tools, a Byga / Google Calendar feed is a schedule source. Configured
in ``[CALENDAR]``: ``url`` (the iCal / webcal address) and ``team_label`` (which
team in a member's feed is this camera's team, e.g. ``BU15 - Guzzetta``).

The feed is re-read at most every ``REFRESH_SECONDS``; matching uses the shared
``select_best_game`` (proximity guard + midpoint heuristic) like the others.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytz

from video_grouper.api_integrations.calendar_feed import (
    CalendarEvent,
    CalendarFeedError,
    fetch_feed,
    parse_feed,
    team_key,
)

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 30 * 60
DEFAULT_GAME_LENGTH = timedelta(hours=2)
_DEFAULT_TZ = "America/New_York"


class CalendarService:
    """Finds the calendar-feed game a recording belongs to."""

    def __init__(self, config, app_config=None, fetch=fetch_feed):
        cal = getattr(config, "calendar", None)
        self.url: str | None = getattr(cal, "url", None) if cal else None
        self.team_label: str | None = getattr(cal, "team_label", None) if cal else None
        self.team_name: str = (
            (getattr(cal, "team_name", None) if cal else None) or self.team_label or ""
        )
        self.enabled = bool(cal and getattr(cal, "enabled", False) and self.url)
        if cal and getattr(cal, "enabled", False) and not self.url:
            logger.warning("Calendar schedule enabled but no [CALENDAR] url is set")
        self._app_config = app_config
        self._fetch = fetch
        self._events: list[CalendarEvent] = []
        self._fetched_at = 0.0

    def _local_tz(self):
        name = getattr(self._app_config, "timezone", None) or _DEFAULT_TZ
        try:
            return pytz.timezone(name)
        except pytz.UnknownTimeZoneError:
            return pytz.timezone(_DEFAULT_TZ)

    def _team_games(self) -> list[CalendarEvent]:
        if not self._events or time.monotonic() - self._fetched_at > REFRESH_SECONDS:
            try:
                self._events = parse_feed(self._fetch(self.url or ""))
                self._fetched_at = time.monotonic()
            except CalendarFeedError as e:
                logger.error("Calendar schedule: %s", e)
        ours = team_key(self.team_label)
        return [
            ev
            for ev in self._events
            if ev.kind == "game" and (not ours or team_key(ev.team_label) == ours)
        ]

    def _as_utc(self, value: datetime | date) -> datetime:
        moment: datetime = (
            value
            if isinstance(value, datetime)
            else datetime(value.year, value.month, value.day)  # all-day: local midnight
        )
        if moment.tzinfo is None:
            moment = self._local_tz().localize(moment)
        return moment.astimezone(UTC)

    def find_game_for_recording(
        self, recording_start: datetime, recording_end: datetime
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        tz = self._local_tz()
        if recording_start.tzinfo is None:
            recording_start = tz.localize(recording_start)
        if recording_end.tzinfo is None:
            recording_end = tz.localize(recording_end)
        recording_start = recording_start.astimezone(UTC)
        recording_end = recording_end.astimezone(UTC)

        candidates = []
        for ev in self._team_games():
            start = self._as_utc(ev.start)
            end = self._as_utc(ev.end) if ev.end else start + DEFAULT_GAME_LENGTH
            candidates.append((ev, start, end))

        from video_grouper.utils.game_selection import select_best_game

        best = select_best_game(
            candidates,
            recording_start,
            recording_end,
            game_label_fn=lambda e: e.summary,
        )
        if best is None:
            return None
        return {
            "source": "Calendar",
            "id": best.uid,
            "team_name": self.team_name,
            "opponent_name": best.opponent or "",
            "is_home": best.home_or_away != "away",
            "venue": best.location or "",
            "start_time": self._as_utc(best.start).astimezone(tz),
            "title": best.summary,
        }

    def close(self) -> None:
        """Nothing to release (kept for parity with the other schedule services)."""
