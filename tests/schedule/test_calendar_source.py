"""The calendar-feed schedule source: one per [TEAM.<key>] with a calendar_url.

Fixture shape copied from a real Byga member feed (read 2026-11-27); the real
feed is private and not committed.
"""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from video_grouper.api_integrations.calendar_feed import CalendarFeedError
from video_grouper.schedule import ScheduleContext
from video_grouper.schedule.sources.calendar import CalendarScheduleSource
from video_grouper.utils.config import TeamConfig

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


def _team(**kw):
    return TeamConfig(
        name="BU15 - Guzzetta", calendar_url="webcal://club.example/cal.ics", **kw
    )


def test_this_teams_away_game_is_found_among_practices_meetings_and_other_teams():
    source = CalendarScheduleSource(_team(), "America/New_York", fetch=lambda url: FEED)
    game = source.find_game(datetime(2027, 4, 10, 9, 50), datetime(2027, 4, 10, 11, 40))
    assert game is not None
    assert (game.opponent, game.location) == ("Penfield United", "Away")
    assert game.kickoff.strftime("%Y-%m-%d %H:%M") == "2027-04-10 10:00"


def test_no_game_near_the_recording():
    source = CalendarScheduleSource(_team(), "America/New_York", fetch=lambda url: FEED)
    assert (
        source.find_game(datetime(2027, 5, 1, 9, 0), datetime(2027, 5, 1, 11, 0))
        is None
    )


def test_a_broken_feed_finds_nothing_and_does_not_raise():
    def broken(url):
        raise CalendarFeedError("Calendar returned HTTP 404")

    source = CalendarScheduleSource(_team(), "America/New_York", fetch=broken)
    assert (
        source.find_game(datetime(2027, 4, 10, 9, 50), datetime(2027, 4, 10, 11, 40))
        is None
    )


def test_calendar_url_round_trips_through_config_ini(tmp_path: Path):
    from video_grouper.utils.config import (
        create_default_config,
        load_config,
        save_config,
    )

    path = tmp_path / "config.ini"
    config = create_default_config(path, str(tmp_path))
    config.teams["heat"] = _team()
    save_config(config, path)
    assert (
        load_config(path).teams["heat"].calendar_url == "webcal://club.example/cal.ics"
    )


def test_found_by_alias_too():
    team = TeamConfig(
        name="Hilton Heat 2011 Boys",
        aliases=["BU15 - Guzzetta"],
        calendar_url="webcal://club.example/cal.ics",
    )
    source = CalendarScheduleSource(team, "America/New_York", fetch=lambda url: FEED)
    game = source.find_game(datetime(2027, 4, 10, 9, 50), datetime(2027, 4, 10, 11, 40))
    assert game is not None and game.team_name == "Hilton Heat 2011 Boys"


def test_feed_is_fetched_once_then_cached():
    calls = []
    source = CalendarScheduleSource(
        _team(), "America/New_York", fetch=lambda url: calls.append(url) or FEED
    )
    for _ in range(3):
        source.find_game(datetime(2027, 4, 10, 9, 50), datetime(2027, 4, 10, 11, 40))
    assert calls == ["webcal://club.example/cal.ics"]


def test_a_disabled_team_is_not_enabled():
    assert not CalendarScheduleSource(_team(enabled=False)).enabled


def test_one_source_per_team_with_a_feed():
    config = SimpleNamespace(
        app=SimpleNamespace(timezone="America/New_York"),
        teams={
            "heat": _team(),
            "flash": TeamConfig(name="Flash"),
        },
    )
    sources = CalendarScheduleSource.from_context(ScheduleContext(config=config))
    assert len(sources) == 1
