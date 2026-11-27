"""Schedule sources: one interface, any number of implementations.

A schedule source answers one question — *which scheduled game is this
recording?* — and returns a :class:`ScheduledGame`. ``MatchInfoService`` works
only with :class:`IScheduleSource`; it never names a provider.

**Adding a source is one file.** Put a module in ``video_grouper/schedule/sources/``
with a subclass of :class:`IScheduleSource` decorated with
:func:`register_schedule_source`. The package imports every module there, and
:func:`build_schedule_sources` asks each registered class which instances this
install has (``from_context``). Nothing else in the app changes.
"""

from __future__ import annotations

import importlib
import pkgutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar


@dataclass(frozen=True)
class ScheduledGame:
    """A scheduled game as any source reports it.

    ``location`` is what ``match_info.ini`` records as the location — the
    source decides it (``"Home"``/``"Away"`` when it knows, else venue text).
    ``kickoff`` is set only when the source's kickoff should be written to
    match_info's date/time. ``raw`` keeps the source's own record for logs.
    """

    source: str
    team_name: str
    opponent: str
    location: str = ""
    kickoff: datetime | None = None
    # The Team Tech Tools game this is, when the source knows it. Written to
    # match_info.ini and reported to TTT with the recording, so the video
    # attaches to that game instead of TTT re-matching on time.
    ttt_game_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def to_match_info(self) -> dict[str, str]:
        """The ``[MATCH]`` fields this game fills in."""
        info = {
            "my_team_name": self.team_name,
            "opponent_team_name": self.opponent,
            "location": self.location,
        }
        if self.kickoff is not None:
            info["date"] = self.kickoff.strftime("%Y-%m-%d")
            info["time"] = self.kickoff.strftime("%H:%M")
        if self.ttt_game_id:
            info["ttt_game_id"] = self.ttt_game_id
        return info


@dataclass
class ScheduleContext:
    """What a source may need to construct itself: the loaded ``Config`` and
    the long-lived services the app already runs (by name, e.g.
    ``"teamsnap"``). A source takes what it needs and ignores the rest."""

    config: Any
    services: dict[str, Any] = field(default_factory=dict)


class IScheduleSource(ABC):
    """A place games are scheduled.

    ``name`` labels it in logs and match_info; ``priority`` breaks ties when
    several sources match (lower wins); an ``authoritative`` source's match
    ends the search.
    """

    name: ClassVar[str] = ""
    priority: ClassVar[int] = 50
    authoritative: ClassVar[bool] = False

    @classmethod
    @abstractmethod
    def from_context(cls, context: ScheduleContext) -> list[IScheduleSource]:
        """Every instance of this source the install has (often zero or one;
        one per team for per-team sources)."""

    @property
    def enabled(self) -> bool:
        return True

    @abstractmethod
    def find_game(
        self, recording_start: datetime, recording_end: datetime
    ) -> ScheduledGame | None:
        """The scheduled game overlapping the recording, or None."""

    def close(self) -> None:  # noqa: B027 - optional hook, most sources hold nothing
        """Release anything the source holds."""


_REGISTRY: dict[str, type[IScheduleSource]] = {}


def register_schedule_source(cls: type[IScheduleSource]) -> type[IScheduleSource]:
    """Class decorator: make a source available to :func:`build_schedule_sources`."""
    if not cls.name:
        raise ValueError(f"{cls.__name__} needs a name")
    _REGISTRY[cls.name] = cls
    return cls


def registered_schedule_sources() -> dict[str, type[IScheduleSource]]:
    """Every registered source class, by name (loads the built-in ones)."""
    _load_builtin_sources()
    return dict(_REGISTRY)


def _load_builtin_sources() -> None:
    from . import sources as package

    for module in pkgutil.iter_modules(package.__path__):
        importlib.import_module(f"{package.__name__}.{module.name}")


def build_schedule_sources(config: Any, **services: Any) -> list[IScheduleSource]:
    """Every schedule source this install has, lowest priority first.

    ``services`` are the long-lived services the app already runs, by name
    (``teamsnap=...``, ``playmetrics=...``, ``ttt_schedule=...``); each source
    picks what it needs in its ``from_context``.
    """
    context = ScheduleContext(config=config, services=services)
    built: list[IScheduleSource] = []
    for cls in registered_schedule_sources().values():
        built.extend(cls.from_context(context))
    return sorted(built, key=lambda s: s.priority)


def pick_game(
    found: list[tuple[IScheduleSource, ScheduledGame]],
) -> ScheduledGame | None:
    """The game to use among every source's match: lowest priority wins
    (ties keep source order)."""
    if not found:
        return None
    return min(enumerate(found), key=lambda item: (item[1][0].priority, item[0]))[1][1]
