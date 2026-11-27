"""Calendar-feed (iCal) schedule source (decided 2026-11-26).

The fixture copies the shape of a real Byga member feed (read 2026-11-27):
VTIMEZONE, TZID'd team events titled ``<Type>: <team> (<location>)``, club
events with no type prefix. The real feed is private and not committed.
"""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from video_grouper.api_integrations.calendar_feed import (
    CalendarFeedError,
    classify,
    normalize_feed_url,
    parse_feed,
)
from video_grouper.task_processors.services.calendar_service import CalendarService
from video_grouper.task_processors.services.match_info_service import MatchInfoService

FEED = "\r\n".join(
    [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:icalendar-ruby",
        "BEGIN:VTIMEZONE",
        "TZID:America/New_York",
        "BEGIN:DAYLIGHT",
        "DTSTART:20260308T030000",
        "TZOFFSETFROM:-0500",
        "TZOFFSETTO:-0400",
        "RRULE:FREQ=YEARLY;BYDAY=2SU;BYMONTH=3",
        "TZNAME:EDT",
        "END:DAYLIGHT",
        "BEGIN:STANDARD",
        "DTSTART:20261101T010000",
        "TZOFFSETFROM:-0400",
        "TZOFFSETTO:-0500",
        "RRULE:FREQ=YEARLY;BYDAY=1SU;BYMONTH=11",
        "TZNAME:EST",
        "END:STANDARD",
        "END:VTIMEZONE",
        "BEGIN:VEVENT",
        "UID:practice-1",
        "DTSTART;TZID=America/New_York:20270410T080000",
        "DTEND;TZID=America/New_York:20270410T093000",
        "LOCATION:GRSP - Indoor",
        "SUMMARY:Practice: BU15 - Guzzetta (GRSP - Indoor)",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:meeting-1",
        "DTSTART;TZID=America/New_York:20270410T100000",
        "SUMMARY:Board Meeting (Board of Directors group)",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:game-away",
        "DTSTART;TZID=America/New_York:20270410T100000",
        "DTEND;TZID=America/New_York:20270410T113000",
        "LOCATION:Penfield HS",
        "SUMMARY:Game: BU15 - Guzzetta @ Penfield United (Penfield HS)",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:other-team",
        "DTSTART;TZID=America/New_York:20270410T100000",
        "SUMMARY:Game: GU12 - Smith vs Webster",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:cancelled",
        "STATUS:CANCELLED",
        "DTSTART;TZID=America/New_York:20270417T100000",
        "SUMMARY:Game: BU15 - Guzzetta vs Greece",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ]
).encode()


def _config(**calendar):
    defaults = {
        "enabled": True,
        "url": "webcal://club.example/cal.ics",
        "team_label": "BU15 - Guzzetta",
    }
    return SimpleNamespace(
        calendar=SimpleNamespace(**{**defaults, **calendar}, team_name=None)
    )


APP = SimpleNamespace(timezone="America/New_York")


@pytest.mark.parametrize(
    "title,location,expected",
    [
        (
            "Practice: BU15 - Guzzetta (GRSP - Indoor)",
            "GRSP - Indoor",
            ("practice", "BU15 - Guzzetta", None, None),
        ),
        (
            "Game: BU15 - Guzzetta vs Brighton Rangers",
            None,
            ("game", "BU15 - Guzzetta", "Brighton Rangers", "home"),
        ),
        (
            "Game: BU15 - Guzzetta @ Penfield United (Penfield HS)",
            "Penfield HS",
            ("game", "BU15 - Guzzetta", "Penfield United", "away"),
        ),
        (
            "Board Meeting (Board of Directors group)",
            "Zoom",
            ("other", None, None, None),
        ),
    ],
)
def test_classify(title, location, expected):
    assert classify(title, location) == expected


def test_parse_skips_cancelled_and_keeps_timezones():
    events = {e.uid: e for e in parse_feed(FEED)}
    assert "cancelled" not in events
    assert events["game-away"].start.utcoffset().total_seconds() == -4 * 3600


def test_webcal_and_bad_addresses():
    assert normalize_feed_url("webcal://a.example/x.ics") == "https://a.example/x.ics"
    with pytest.raises(CalendarFeedError):
        normalize_feed_url("ftp://a.example/x.ics")


def test_a_recording_matches_this_teams_game_not_the_practice_or_meeting():
    service = CalendarService(_config(), APP, fetch=lambda url: FEED)
    # Camera-local (naive) times, like the recording timespan from the camera.
    game = service.find_game_for_recording(
        datetime(2027, 4, 10, 9, 50), datetime(2027, 4, 10, 11, 40)
    )
    assert game is not None
    assert game["source"] == "Calendar"
    assert game["id"] == "game-away"
    assert (game["team_name"], game["opponent_name"], game["is_home"]) == (
        "BU15 - Guzzetta",
        "Penfield United",
        False,
    )
    assert game["start_time"].strftime("%Y-%m-%d %H:%M") == "2027-04-10 10:00"


def test_no_game_near_the_recording_returns_none():
    service = CalendarService(_config(), APP, fetch=lambda url: FEED)
    assert (
        service.find_game_for_recording(
            datetime(2027, 5, 1, 9, 0), datetime(2027, 5, 1, 11, 0)
        )
        is None
    )


def test_feed_is_fetched_once_then_cached():
    calls = []
    service = CalendarService(
        _config(), APP, fetch=lambda url: calls.append(url) or FEED
    )
    for _ in range(3):
        service.find_game_for_recording(
            datetime(2027, 4, 10, 9, 50), datetime(2027, 4, 10, 11, 40)
        )
    assert calls == ["webcal://club.example/cal.ics"]


def test_disabled_or_unconfigured_does_nothing():
    assert not CalendarService(_config(enabled=False), APP).enabled
    assert not CalendarService(_config(url=None), APP).enabled
    assert not CalendarService(SimpleNamespace(), APP).enabled


def test_a_broken_feed_does_not_raise():
    def broken(url):
        raise CalendarFeedError("Calendar returned HTTP 404")

    service = CalendarService(_config(), APP, fetch=broken)
    assert (
        service.find_game_for_recording(
            datetime(2027, 4, 10, 9, 50), datetime(2027, 4, 10, 11, 40)
        )
        is None
    )


def test_match_info_from_a_calendar_game():
    svc = MatchInfoService.__new__(MatchInfoService)
    info = svc._convert_game_to_match_info(
        {
            "source": "Calendar",
            "team_name": "BU15 - Guzzetta",
            "opponent_name": "Penfield United",
            "is_home": False,
            "start_time": datetime(2027, 4, 10, 10, 0),
        }
    )
    assert info == {
        "my_team_name": "BU15 - Guzzetta",
        "opponent_team_name": "Penfield United",
        "location": "Away",
        "date": "2027-04-10",
        "time": "10:00",
    }


def test_calendar_section_round_trips_through_config_ini(tmp_path: Path):
    from video_grouper.utils.config import (
        create_default_config,
        load_config,
        save_config,
    )

    path = tmp_path / "config.ini"
    config = create_default_config(path, str(tmp_path))
    assert config.calendar.enabled is False
    config.calendar.enabled = True
    config.calendar.url = "webcal://club.example/cal.ics"
    config.calendar.team_label = "BU15 - Guzzetta"
    save_config(config, path)
    loaded = load_config(path)
    assert (
        loaded.calendar.enabled,
        loaded.calendar.url,
        loaded.calendar.team_label,
    ) == (
        True,
        "webcal://club.example/cal.ics",
        "BU15 - Guzzetta",
    )
