"""Calendar-feed (iCal) parsing (decided 2026-11-26).

The fixture copies the shape of a real Byga member feed (read 2026-11-27):
VTIMEZONE, TZID'd team events titled ``<Type>: <team> (<location>)``, club
events with no type prefix. The real feed is private and not committed.
"""

import pytest

from video_grouper.api_integrations.calendar_feed import (
    CalendarFeedError,
    classify,
    normalize_feed_url,
    parse_feed,
)

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
