"""The generic port registry and composition point, tested through a toy port."""

from abc import ABC, abstractmethod
from types import SimpleNamespace

import pytest

from video_grouper.integrations import build_integrations, registry
from video_grouper.integrations.device_link import IDeviceLink
from video_grouper.integrations.entitlements import IEntitlements
from video_grouper.integrations.recordings import IRecordingSink
from video_grouper.integrations.registry import (
    NULL_PRIORITY,
    IntegrationContext,
    Provider,
    build_port,
    provides,
)
from video_grouper.integrations.work import IWorkSource


class IGreeter(Provider, ABC):
    @abstractmethod
    def greet(self) -> str: ...


@pytest.fixture
def toy_port(monkeypatch):
    """A port whose implementations only exist inside the test."""
    monkeypatch.setattr(registry, "_REGISTRY", {})
    monkeypatch.setattr(registry, "_load_sources", lambda port: None)
    return IGreeter


def _impl(name, priority, accepts, text):
    class _Impl(IGreeter):
        @classmethod
        def from_context(cls, context):
            return cls() if accepts(context) else None

        def greet(self):
            return text

    _Impl.name = name
    _Impl.priority = priority
    return _Impl


def _ctx(**services):
    return IntegrationContext(SimpleNamespace(), ".", services)


def test_lowest_priority_implementation_that_accepts_wins(toy_port):
    provides(toy_port)(_impl("null", NULL_PRIORITY, lambda c: True, "nothing"))
    provides(toy_port)(_impl("late", 20, lambda c: True, "late"))
    provides(toy_port)(_impl("early", 10, lambda c: True, "early"))
    assert build_port(toy_port, _ctx()).greet() == "early"


def test_an_implementation_that_declines_is_skipped(toy_port):
    provides(toy_port)(_impl("null", NULL_PRIORITY, lambda c: True, "nothing"))
    provides(toy_port)(_impl("svc", 10, lambda c: "svc" in c.services, "svc"))
    assert build_port(toy_port, _ctx()).greet() == "nothing"
    assert build_port(toy_port, _ctx(svc=object())).greet() == "svc"


def test_a_new_provider_needs_no_other_change(toy_port):
    provides(toy_port)(_impl("null", NULL_PRIORITY, lambda c: True, "nothing"))
    provides(toy_port)(_impl("brand_new", 5, lambda c: True, "hello"))
    assert build_port(toy_port, _ctx()).greet() == "hello"


def test_registration_rules(toy_port):
    with pytest.raises(ValueError):
        provides(toy_port)(_impl("", 1, lambda c: True, "x"))

    class NotAGreeter(Provider):
        name = "wrong"

    with pytest.raises(TypeError):
        provides(toy_port)(NotAGreeter)


def test_no_implementation_is_an_error(toy_port):
    with pytest.raises(LookupError):
        build_port(toy_port, _ctx())


def test_every_built_in_port_resolves_with_ttt_off():
    # TTT off: every port still resolves, to its Null implementation.
    integrations = build_integrations(
        SimpleNamespace(ttt=SimpleNamespace(enabled=False)), "."
    )
    assert isinstance(integrations.entitlements, IEntitlements)
    assert integrations.entitlements.available is False
    assert isinstance(integrations.recordings, IRecordingSink)
    assert integrations.recordings.name == "none"
    assert integrations.phase_hints.name == "none"
    assert isinstance(integrations.work, IWorkSource)
    assert integrations.work.name == "none"
    assert isinstance(integrations.device_link, IDeviceLink)
    assert integrations.device_link.name == "none"


def test_ttt_on_shares_one_client_across_ports():
    client = SimpleNamespace(is_authenticated=lambda: False)
    integrations = build_integrations(
        SimpleNamespace(ttt=SimpleNamespace(enabled=True, camera_id="")),
        ".",
        ttt=client,
    )
    assert integrations.shared_ttt_client() is client
    assert integrations.entitlements.name == "ttt"
    assert integrations.recordings.name == "ttt"
    assert integrations.phase_hints.name == "ttt"
    assert integrations.work.name == "ttt"
    assert integrations.work._client is client
    assert integrations.device_link.name == "ttt"
    assert integrations.device_link._client is client


def test_active_defaults_to_the_null_set_and_activate_replaces_it(monkeypatch):
    from video_grouper import integrations as integ

    monkeypatch.setattr(integ, "_active", None)
    assert integ.active().recordings.name == "none"
    custom = build_integrations(
        SimpleNamespace(ttt=SimpleNamespace(enabled=False)), "."
    )
    assert integ.activate(custom) is custom and integ.active() is custom
