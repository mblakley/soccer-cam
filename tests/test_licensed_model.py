"""licensed_model: a step's licensed model comes through the entitlements port."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from video_grouper.integrations.entitlements.fake import FakeEntitlements
from video_grouper.pipeline.base import StepContext
from video_grouper.pipeline.steps.licensed_model import build_secure_loader_session


def _ctx(entitlements, keys=("pub",)):
    return StepContext(
        group_dir=Path("g"),
        team_name=None,
        storage_path=Path("s"),
        entitlements=entitlements,
        signing_public_keys=list(keys),
    )


@pytest.mark.parametrize("entitlements", [None, FakeEntitlements(licensee=None)])
def test_no_signed_in_provider_is_a_clear_error(entitlements):
    with pytest.raises(RuntimeError, match="model_path"):
        build_secure_loader_session("detect", "ball-v3", None, None, _ctx(entitlements))


def test_loads_through_the_port_with_soccer_cams_own_keys():
    ent = FakeEntitlements(licensee="u1")
    loaded = MagicMock(model_key="ball-v3", version="3", tier="t", provider="cpu")
    with patch("video_grouper.ball_tracking.secure_loader.SecureLoader") as loader_cls:
        loader_cls.return_value.acquire.return_value = loaded
        session = build_secure_loader_session(
            "detect", "ball-v3", "beta", "2.0", _ctx(ent, keys=("k1", "k2"))
        )
    assert session is loaded.session
    args = loader_cls.call_args
    assert args.args[0] is ent and args.args[1] == ["k1", "k2"]
    loader_cls.return_value.acquire.assert_called_once_with(
        "ball-v3", channel="beta", pipeline_version="2.0"
    )
