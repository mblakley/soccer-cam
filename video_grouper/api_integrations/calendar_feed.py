"""Calendar-feed (iCal) schedules: Byga, Google Calendar, anything publishing .ics.

Decided 2026-11-26 (training/docs/DECISIONS.md): calendar feeds are a schedule
source in soccer-cam too, beside TeamSnap and PlayMetrics, for camera owners
who don't use Team Tech Tools. (With TTT, TTT reads the feed and soccer-cam
gets the games from TTT's schedule.)

What a Byga feed looks like (read 2026-11-27): it is the member's personal
calendar, mixing every group they belong to. Team events are titled
``<Type>: <team> (<location>)``, e.g. ``Practice: BU15 - Guzzetta (GRSP -
Indoor)``; club events have no type prefix. Times carry a TZID. Game titles
are taken to be ``Game: <team> vs <opponent>`` / ``Game: <team> @ <opponent>``
(away) / ``Match: ...``; the configured ``team_label`` picks this team's events
out of the feed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime

import httpx
from icalendar import Calendar

logger = logging.getLogger(__name__)

MAX_FEED_BYTES = 2_000_000
FETCH_TIMEOUT_SECONDS = 15

GAME_WORDS = ("game", "match", "scrimmage", "friendly", "tournament", "cup")
PRACTICE_WORDS = ("practice", "training")

_TITLE_RE = re.compile(
    r"^\s*(?P<type>[A-Za-z][A-Za-z /-]{0,30}?)\s*:\s*(?P<rest>.+?)\s*$"
)
_OPPONENT_RE = re.compile(
    r"\s+(?P<sep>vs\.?|v\.?|@|at)\s+(?P<opp>\S.*)$", re.IGNORECASE
)


class CalendarFeedError(ValueError):
    """The feed couldn't be fetched or read."""


@dataclass(frozen=True)
class CalendarEvent:
    uid: str
    start: datetime | date
    end: datetime | date | None
    summary: str
    location: str | None
    kind: str  # "game" | "practice" | "other"
    team_label: str | None
    opponent: str | None
    home_or_away: str | None  # "home" | "away" | None


def normalize_feed_url(url: str) -> str:
    """``webcal://`` is ``https://`` (what calendar apps subscribe to)."""
    url = (url or "").strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://") :]
    if not url.lower().startswith(("https://", "http://")):
        raise CalendarFeedError(
            "Calendar address must start with webcal:// or https://"
        )
    return url


def fetch_feed(url: str) -> bytes:
    """Download the feed (size-capped)."""
    url = normalize_feed_url(url)
    try:
        with httpx.Client(
            timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise CalendarFeedError(
                        f"Calendar returned HTTP {resp.status_code}"
                    )
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_FEED_BYTES:
                        raise CalendarFeedError("Calendar is too large to read")
                return bytes(body)
    except httpx.HTTPError as exc:
        raise CalendarFeedError(f"Couldn't fetch the calendar: {exc}") from exc


def _clean(text) -> str | None:
    if text is None:
        return None
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text or None


def team_key(label: str | None) -> str:
    """How team labels are compared (case and spacing don't matter)."""
    return re.sub(r"\s+", " ", (label or "")).strip().casefold()


def classify(
    summary: str, location: str | None
) -> tuple[str, str | None, str | None, str | None]:
    """(kind, team_label, opponent, home_or_away) from an event title."""
    title = _clean(summary) or ""
    m = _TITLE_RE.match(title)
    if not m:
        return "other", None, None, None
    event_type = m.group("type").casefold()
    rest = m.group("rest")
    loc = _clean(location)
    if loc and rest.endswith(f"({loc})"):
        rest = rest[: -len(f"({loc})")].rstrip()

    opponent = home_or_away = None
    om = _OPPONENT_RE.search(rest)
    if om:
        opponent = _clean(om.group("opp"))
        home_or_away = "away" if om.group("sep").lower() in ("@", "at") else "home"
        rest = rest[: om.start()]
    team_label = _clean(rest)

    if any(w in event_type for w in PRACTICE_WORDS):
        kind = "practice"
    elif any(w in event_type for w in GAME_WORDS) or opponent:
        kind = "game"
    else:
        kind = "other"
    return kind, team_label, opponent, home_or_away


def parse_feed(data: bytes) -> list[CalendarEvent]:
    """Every dated event in the feed, classified; cancelled ones skipped."""
    try:
        cal = Calendar.from_ical(data)
    except Exception as exc:
        raise CalendarFeedError("That address didn't return a calendar") from exc

    events: list[CalendarEvent] = []
    for component in cal.walk("VEVENT"):
        uid = _clean(component.get("uid"))
        if not uid or component.get("dtstart") is None:
            continue
        if str(component.get("status", "")).upper() == "CANCELLED":
            continue
        start = component.decoded("dtstart")
        if not isinstance(start, datetime | date):
            continue
        end = component.decoded("dtend") if component.get("dtend") is not None else None
        summary = _clean(component.get("summary")) or ""
        location = _clean(component.get("location"))
        kind, team_label, opponent, home_or_away = classify(summary, location)
        events.append(
            CalendarEvent(
                uid=uid,
                start=start,
                end=end,
                summary=summary,
                location=location,
                kind=kind,
                team_label=team_label,
                opponent=opponent,
                home_or_away=home_or_away,
            )
        )
    return events
