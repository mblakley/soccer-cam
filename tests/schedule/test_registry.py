"""The source registry and builder, tested through the interface only."""

from types import SimpleNamespace

import pytest

from video_grouper.schedule import (
    IScheduleSource,
    ScheduleContext,
    base,
    build_schedule_sources,
    register_schedule_source,
    registered_schedule_sources,
)


@pytest.fixture
def isolated_registry(monkeypatch):
    """A registry holding only what the test registers."""
    monkeypatch.setattr(base, "_REGISTRY", {})
    monkeypatch.setattr(base, "_load_builtin_sources", lambda: None)
    return base._REGISTRY


def _source_class(name, priority, make):
    class _Source(IScheduleSource):
        def __init__(self, label):
            self.label = label

        @classmethod
        def from_context(cls, context):
            return [cls(label) for label in make(context)]

        def find_game(self, recording_start, recording_end):
            return None

    _Source.name = name
    _Source.priority = priority
    return _Source


def test_a_registered_source_is_built_with_no_other_change(isolated_registry):
    register_schedule_source(_source_class("New", 5, lambda ctx: ["one"]))
    sources = build_schedule_sources(SimpleNamespace())
    assert [s.name for s in sources] == ["New"]


def test_sources_come_back_lowest_priority_first(isolated_registry):
    register_schedule_source(_source_class("Late", 30, lambda ctx: ["x"]))
    register_schedule_source(_source_class("Early", 1, lambda ctx: ["y"]))
    assert [s.name for s in build_schedule_sources(SimpleNamespace())] == [
        "Early",
        "Late",
    ]


def test_a_source_can_build_zero_or_many_instances(isolated_registry):
    register_schedule_source(_source_class("None", 1, lambda ctx: []))
    register_schedule_source(_source_class("PerTeam", 2, lambda ctx: ["a", "b", "c"]))
    assert [s.label for s in build_schedule_sources(SimpleNamespace())] == [
        "a",
        "b",
        "c",
    ]


def test_config_and_named_services_reach_from_context(isolated_registry):
    seen: list[ScheduleContext] = []
    register_schedule_source(
        _source_class("Probe", 1, lambda ctx: seen.append(ctx) or [])
    )
    config = SimpleNamespace(teams={})
    build_schedule_sources(config, crm="crm-service")
    assert seen[0].config is config
    assert seen[0].services == {"crm": "crm-service"}


def test_a_source_needs_a_name(isolated_registry):
    with pytest.raises(ValueError):
        register_schedule_source(_source_class("", 1, lambda ctx: []))


def test_every_built_in_source_implements_the_interface():
    for name, cls in registered_schedule_sources().items():
        assert issubclass(cls, IScheduleSource), name
        assert cls.name == name
