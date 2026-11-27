"""TTTEntitlements: maps the port onto the TTT client and follows its session."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from video_grouper.integrations.entitlements import EntitlementUnavailable
from video_grouper.integrations.entitlements.sources.ttt import TTTEntitlements
from video_grouper.integrations.registry import IntegrationContext


def _client(signed_in=True):
    client = MagicMock()
    client.is_authenticated.return_value = signed_in
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
    client.is_authenticated.return_value = True
    assert ent.available is True and ent.has("premium.x") is True


def test_not_signed_in_never_calls_the_api(tmp_path):
    client = _client(signed_in=False)
    ent = TTTEntitlements(client)
    assert ent.licensee() is None
    assert ent.list_plugins() == []
    with pytest.raises(EntitlementUnavailable):
        ent.license_model("m")
    with pytest.raises(EntitlementUnavailable):
        ent.fetch_plugin("p", tmp_path / "p.zip")
    client.acquire_model_license.assert_not_called()
    client.download_plugin.assert_not_called()
    client.check_entitlement.assert_not_called()
