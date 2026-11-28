"""TTTEntitlements: maps the port onto the TTT client and follows its session."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from video_grouper.integrations.entitlements import EntitlementUnavailable
from video_grouper.integrations.entitlements.sources.ttt import TTTEntitlements
from video_grouper.integrations.registry import IntegrationContext


def _client(signed_in=True):
    client = MagicMock()
    client.ensure_signed_in.return_value = signed_in
    client.current_user_id.return_value = "user-1"
    client.check_entitlement.return_value = True
    client.acquire_model_license.return_value = {"wrapped_key": "k"}
    client.get_available_plugins.return_value = [{"key": "p"}]
    return client


def test_chosen_only_when_ttt_is_enabled():
    client = _client()
    on = IntegrationContext(
        SimpleNamespace(ttt=SimpleNamespace(enabled=True)), ".", {"ttt": client}
    )
    off = IntegrationContext(SimpleNamespace(ttt=SimpleNamespace(enabled=False)), ".")
    assert isinstance(TTTEntitlements.from_context(on), TTTEntitlements)
    assert TTTEntitlements.from_context(off) is None


def test_signed_in_maps_each_call():
    client = _client()
    ent = TTTEntitlements(client)
    assert ent.available and ent.licensee() == "user-1"
    assert ent.has("premium.x") is True
    client.check_entitlement.assert_called_once_with("premium.x")
    assert ent.license_model("m", channel="beta", pipeline_version="2") == {
        "wrapped_key": "k"
    }
    client.acquire_model_license.assert_called_once_with(
        "m", channel="beta", pipeline_version="2"
    )
    assert ent.list_plugins() == [{"key": "p"}]


def test_signing_in_later_takes_effect_without_rebuilding():
    client = _client(signed_in=False)
    ent = TTTEntitlements(client)
    assert ent.available is False and ent.has("premium.x") is False
    client.ensure_signed_in.return_value = True
    assert ent.available is True and ent.has("premium.x") is True


def test_not_signed_in_never_calls_the_api(tmp_path):
    client = _client(signed_in=False)
    ent = TTTEntitlements(client)
    # Signed out is "can't say", never "entitled to none" ([] would make the
    # plugin manager delete every installed plugin).
    with pytest.raises(EntitlementUnavailable):
        ent.list_plugins()
    client.get_available_plugins.assert_not_called()
    with pytest.raises(EntitlementUnavailable):
        ent.license_model("m")
    with pytest.raises(EntitlementUnavailable):
        ent.fetch_plugin("p", tmp_path / "p.zip")
    client.acquire_model_license.assert_not_called()
    client.download_plugin.assert_not_called()
    client.check_entitlement.assert_not_called()


def test_licensee_is_the_held_accounts_even_while_its_token_is_expired():
    # Installed plugins / licences are bound to the account and carry their
    # own signed expiry; an expired access token (offline) doesn't unbind them.
    client = _client(signed_in=False)
    assert TTTEntitlements(client).licensee() == "user-1"
    client.current_user_id.return_value = None  # no session held at all
    assert TTTEntitlements(client).licensee() is None


def test_an_expired_token_is_refreshed_not_read_as_signed_out(expired_ttt):
    expired_ttt.routes[("GET", "/api/plugins")] = [{"key": "p", "version": "1"}]
    ent = TTTEntitlements(expired_ttt.client)
    assert ent.available is True
    assert ent.list_plugins() == [{"key": "p", "version": "1"}]
    assert ent.licensee() == "user-1"
    assert expired_ttt.refreshes == 1


def test_an_expired_token_that_cannot_refresh_raises_rather_than_lists_nothing(
    tmp_path,
):
    from tests.integrations.expired_ttt import ExpiredTTTSession

    session = ExpiredTTTSession(str(tmp_path), refresh_ok=False)
    with pytest.raises(EntitlementUnavailable):
        TTTEntitlements(session.client).list_plugins()
    assert session.calls("GET", "/api/plugins") == []
