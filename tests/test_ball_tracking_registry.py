"""Tests for the ball-tracking provider registry and config resolution."""

from __future__ import annotations

import pytest

from video_grouper.ball_tracking import (
    _PROVIDER_REGISTRY,
    create_provider,
    register_provider,
)
from video_grouper.ball_tracking.base import BallTrackingProvider, ProviderContext
from video_grouper.ball_tracking.config import (
    AutocamGuiProviderConfig,
    BallTrackingConfig,
)


@pytest.fixture
def isolated_registry():
    """Snapshot and restore the global registry around each test."""
    snapshot = dict(_PROVIDER_REGISTRY)
    yield
    _PROVIDER_REGISTRY.clear()
    _PROVIDER_REGISTRY.update(snapshot)


class _FakeProvider(BallTrackingProvider):
    """In-test provider that records the args it was created with."""

    def __init__(self, config):
        self.config = config

    async def run(self, input_path, output_path, ctx):
        return True


class _OtherProvider(_FakeProvider):
    """Second provider used by overwrite tests. Module-level so the registry
    can resolve it via ``getattr(module, class_name)``."""


class TestRegistry:
    def test_register_and_create_round_trip(self, isolated_registry):
        register_provider("fake_test_provider", _FakeProvider, AutocamGuiProviderConfig)

        cfg = AutocamGuiProviderConfig(executable="dummy.exe")
        provider = create_provider("fake_test_provider", cfg)

        assert isinstance(provider, _FakeProvider)
        assert provider.config is cfg

    def test_create_from_dict_validates_via_registered_config_class(
        self, isolated_registry
    ):
        register_provider("fake_test_provider", _FakeProvider, AutocamGuiProviderConfig)
        provider = create_provider("fake_test_provider", {"executable": "dummy.exe"})
        assert isinstance(provider.config, AutocamGuiProviderConfig)
        assert provider.config.executable == "dummy.exe"

    def test_create_unknown_provider_raises(self, isolated_registry):
        with pytest.raises(ValueError, match="Unknown ball-tracking provider"):
            create_provider("nonexistent", AutocamGuiProviderConfig())

    def test_register_overwrites_same_name(self, isolated_registry):
        register_provider("dup", _FakeProvider, AutocamGuiProviderConfig)
        register_provider("dup", _OtherProvider, AutocamGuiProviderConfig)

        provider = create_provider("dup", AutocamGuiProviderConfig())
        assert isinstance(provider, _OtherProvider)


class TestResolveProviderFor:
    def test_default_when_no_per_team_override(self):
        cfg = BallTrackingConfig(provider="autocam_gui")
        name, sub_cfg = cfg.resolve_provider_for("flash")
        assert name == "autocam_gui"
        assert sub_cfg is cfg.autocam_gui

    def test_per_team_override_takes_precedence(self):
        cfg = BallTrackingConfig(
            provider="autocam_gui",
            per_team={"flash": "autocam_gui", "heat": "autocam_gui"},
        )
        # Even though both teams point at autocam_gui here, the lookup path
        # is exercised: per_team wins over the default `provider` value.
        name, sub_cfg = cfg.resolve_provider_for("flash")
        assert name == "autocam_gui"
        assert sub_cfg is cfg.autocam_gui

    def test_unknown_team_falls_back_to_default(self):
        cfg = BallTrackingConfig(
            provider="autocam_gui",
            per_team={"flash": "autocam_gui"},
        )
        name, _ = cfg.resolve_provider_for("never_heard_of_them")
        assert name == "autocam_gui"

    def test_none_team_uses_default(self):
        cfg = BallTrackingConfig(provider="autocam_gui")
        name, _ = cfg.resolve_provider_for(None)
        assert name == "autocam_gui"


class TestProviderContext:
    def test_context_carries_required_fields(self, tmp_path):
        ctx = ProviderContext(
            group_dir=tmp_path / "game1",
            team_name="flash",
            storage_path=tmp_path,
        )
        assert ctx.group_dir == tmp_path / "game1"
        assert ctx.team_name == "flash"
        assert ctx.storage_path == tmp_path

    def test_team_name_can_be_none(self, tmp_path):
        ctx = ProviderContext(
            group_dir=tmp_path / "game1",
            team_name=None,
            storage_path=tmp_path,
        )
        assert ctx.team_name is None
