from datetime import datetime
import pytz


def get_all_timezones():
    """Returns a list of all available timezone strings."""
    return pytz.all_timezones


def convert_utc_to_local(utc_dt: datetime, tz_str: str) -> datetime:
    """Converts a timezone-aware UTC datetime to a local datetime."""
    if not utc_dt.tzinfo:
        # Assume UTC if datetime is naive
        utc_dt = pytz.utc.localize(utc_dt)

    try:
        local_tz = pytz.timezone(tz_str)
    except pytz.UnknownTimeZoneError:
        # Fallback to UTC if the timezone is invalid
        local_tz = pytz.utc

    return utc_dt.astimezone(local_tz)


def parse_utc_from_string(dt_str: str, fmt: str = "%Y.%m.%d-%H.%M.%S") -> datetime:
    """Parses a string into a timezone-aware UTC datetime."""
    dt_naive = datetime.strptime(dt_str, fmt)
    return pytz.utc.localize(dt_naive)


def parse_dt_from_string_with_tz(
    dt_str: str, tz_str: str, fmt: str = "%Y.%m.%d-%H.%M.%S"
) -> datetime:
    """Parses a string into a timezone-aware datetime."""
    dt_naive = datetime.strptime(dt_str, fmt)
    try:
        source_tz = pytz.timezone(tz_str)
    except pytz.UnknownTimeZoneError:
        source_tz = pytz.utc  # fallback to UTC
    return source_tz.localize(dt_naive)
