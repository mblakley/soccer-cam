"""Schedule sources: one interface (``IScheduleSource``), any number of
implementations in ``video_grouper/schedule/sources/`` (one module each,
self-registering). See :mod:`video_grouper.schedule.base`.
"""

from .base import (
    IScheduleSource,
    ScheduleContext,
    ScheduledGame,
    build_schedule_sources,
    pick_game,
    register_schedule_source,
    registered_schedule_sources,
)

__all__ = [
    "IScheduleSource",
    "ScheduleContext",
    "ScheduledGame",
    "build_schedule_sources",
    "pick_game",
    "register_schedule_source",
    "registered_schedule_sources",
]
