"""NullEntitlements: TTT off grants nothing and says how to fix it."""

import pytest

from video_grouper.integrations.entitlements import EntitlementUnavailable
from video_grouper.integrations.entitlements.sources.null import NullEntitlements


def test_grants_nothing():
    null = NullEntitlements()
    assert null.available is False
    assert null.licensee() is None
    assert null.has("premium.anything") is False
    assert null.list_plugins() == []


def test_a_licensed_model_explains_the_way_out(tmp_path):
    with pytest.raises(EntitlementUnavailable, match="model_path"):
        NullEntitlements().license_model("ball-v3")
    with pytest.raises(EntitlementUnavailable):
        NullEntitlements().fetch_plugin("p", tmp_path / "p.zip")


def test_always_accepts():
    assert isinstance(NullEntitlements.from_context(object()), NullEntitlements)
