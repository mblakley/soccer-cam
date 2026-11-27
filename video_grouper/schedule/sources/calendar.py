"""A team's calendar feed (Byga, Google Calendar, any iCal), decided 2026-11-26.

One instance per ``[TEAM.<key>]`` with a ``calendar_url``. A club member's feed
mixes every group they belong to; this team's games are the events whose team
label the team's ``name``/``aliases`` match (``TeamConfig.matches``, the same rule
used everywhere). Home/Away and date/time from the event.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytz

from video_grouper.api_integrations.calendar_feed import (
    CalendarEvent,
    CalendarFeedError,
    fetch_feed,
    parse_feed,
)
from video_grouper.utils.game_selection import select_best_game

from ..base import (
    IScheduleSource,
    ScheduleContext,
    ScheduledGame,
    register_schedule_source,
)

logger = logging.getLogger(__name__)

_DEFAULT_TZ = "America/New_York"


@register_schedule_source
class CalendarScheduleSource(IScheduleSource):
    name = "Calendar"
    priority = 30

    REFRESH_SECONDS = 30 * 60
    DEFAULT_GAME_LENGTH = timedelta(hours=2)

    def __init__(
        self,
        team: Any,
        timezone: str | None = None,
        fetch: Callable[[str], bytes] = fetch_feed,
    ):
        self._team = team
        self._tz_name = timezone or _DEFAULT_TZ
        self._fetch = fetch
        self._events: list[CalendarEvent] = []
        self._fetched_at = 0.0

    @classmethod
    def from_context(cls, context: ScheduleContext) -> list[IScheduleSource]:
        config = context.config
        timezone = getattr(getattr(config, "app", None), "timezone", None)
        fetch = context.services.get("calendar_fetch", fetch_feed)
        return [
            cls(team, timezone, fetch=fetch)
            for team in (getattr(config, "teams", None) or {}).values()
            if getattr(team, "calendar_url", "")
        ]

    @property
    def enabled(self) -> bool:
        return bool(self._team.enabled and self._team.calendar_url)

    def _tz(self):
        try:
            return pytz.timezone(self._tz_name)
        except pytz.UnknownTimeZoneError:
            return pytz.timezone(_DEFAULT_TZ)

    def _as_utc(self, value: datetime | date) -> datetime:
        moment: datetime = (
            value
            if isinstance(value, datetime)
            else datetime(value.year, value.month, value.day)  # all-day: local midnight
        )
        if moment.tzinfo is None:
            moment = self._tz().localize(moment)
        return moment.astimezone(UTC)

    def _team_games(self) -> list[CalendarEvent]:
        if (
            not self._events
            or time.monotonic() - self._fetched_at > self.REFRESH_SECONDS
        ):
            try:
                self._events = parse_feed(self._fetch(self._team.calendar_url))
                self._fetched_at = time.monotonic()
            except CalendarFeedError as e:
                logger.error("Calendar schedule for %s: %s", self._team.name, e)
        return [
            ev
            for ev in self._events
            if ev.kind == "game" and ev.team_label and self._team.matches(ev.team_label)
        ]

    def find_game(
        self, recording_start: datetime, recording_end: datetime
    ) -> ScheduledGame | None:
        tz = self._tz()
        if recording_start.tzinfo is None:
            recording_start = tz.localize(recording_start)
        if recording_end.tzinfo is None:
            recording_end = tz.localize(recording_end)
        recording_start = recording_start.astimezone(UTC)
        recording_end = recording_end.astimezone(UTC)

        candidates = []
        for ev in self._team_games():
            start = self._as_utc(ev.start)
            end = self._as_utc(ev.end) if ev.end else start + self.DEFAULT_GAME_LENGTH
            candidates.append((ev, start, end))

        best = select_best_game(
            candidates,
            recording_start,
            recording_end,
            game_label_fn=lambda e: e.summary,
        )
        if best is None:
            return None
        return ScheduledGame(
            source=self.name,
            team_name=self._team.name,
            opponent=best.opponent or "",
            location="Away" if best.home_or_away == "away" else "Home",
            kickoff=self._as_utc(best.start).astimezone(tz),
            raw={"uid": best.uid, "summary": best.summary, "venue": best.location},
        )
