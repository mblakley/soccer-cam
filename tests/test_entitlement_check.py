"""Tests for the plugin entitlement_check decorator."""

from unittest.mock import MagicMock

import pytest

from video_grouper.plugins.entitlement_check import (
    PluginEntitlementError,
    clear_entitlement_cache,
    requires_entitlement,
)


@pytest.fixture(autouse=True)
def clear_cache():
    clear_entitlement_cache()
    yield
    clear_entitlement_cache()


def _make_client(entitled: bool) -> MagicMock:
    client = MagicMock()
    client.check_entitlement = MagicMock(return_value=entitled)
    return client


class TestRequiresEntitlement:
    def test_entitled_call_proceeds(self):
        client = _make_client(True)

        @requires_entitlement("premium.test.x")
        def op(ttt_client, value):
            return value * 2

        assert op(client, 21) == 42
        client.check_entitlement.assert_called_once_with("premium.test.x")

    def test_unentitled_call_raises(self):
        client = _make_client(False)

        @requires_entitlement("premium.test.x")
        def op(ttt_client):
            return "ran"

        with pytest.raises(PluginEntitlementError, match="premium.test.x"):
            op(client)

    def test_result_cached_within_ttl(self):
        client = _make_client(True)

        @requires_entitlement("premium.test.x", cache_ttl_seconds=60)
        def op(ttt_client):
            return True

        op(client)
        op(client)
        op(client)
        assert client.check_entitlement.call_count == 1

    def test_cache_separate_per_client_instance(self):
        a = _make_client(True)
        b = _make_client(True)

        @requires_entitlement("premium.test.x", cache_ttl_seconds=60)
        def op(ttt_client):
            return True

        op(a)
        op(b)
        assert a.check_entitlement.call_count == 1
        assert b.check_entitlement.call_count == 1

    def test_cache_expires(self, monkeypatch):
        client = _make_client(True)
        now = [1000.0]

        def fake_monotonic():
            return now[0]

        monkeypatch.setattr(
            "video_grouper.plugins.entitlement_check.time.monotonic", fake_monotonic
        )

        @requires_entitlement("premium.test.x", cache_ttl_seconds=10)
        def op(ttt_client):
            return True

        op(client)
        now[0] += 5  # still in TTL
        op(client)
        assert client.check_entitlement.call_count == 1
        now[0] += 10  # past TTL
        op(client)
        assert client.check_entitlement.call_count == 2

    def test_client_resolved_from_kwarg(self):
        client = _make_client(True)

        @requires_entitlement("premium.test.x")
        def op(value, *, ttt_client):
            return value

        assert op(7, ttt_client=client) == 7

    def test_missing_client_raises(self):
        @requires_entitlement("premium.test.x")
        def op():
            return "never"

        with pytest.raises(PluginEntitlementError, match="could not resolve"):
            op()
