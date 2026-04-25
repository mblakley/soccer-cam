"""Tests for the homegrown provider scaffolding + stage registry.

The stage *implementations* themselves call into ``video_grouper.inference``
(cv2 + onnxruntime) and need real video / model files to verify. Those
are exercised by integration tests once footage is available. These unit
tests cover the registry + orchestrator path: config defaults, stage
ordering, error propagation, output validation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from video_grouper.ball_tracking import _PROVIDER_REGISTRY, create_provider
from video_grouper.ball_tracking.base import ProviderContext
from video_grouper.ball_tracking.config import HomegrownProviderConfig
from video_grouper.ball_tracking.providers.homegrown import HomegrownProvider
from video_grouper.ball_tracking.providers.homegrown.stages import (
    ProcessingStage,
    list_stages,
    register_stage,
)


@pytest.fixture
def context(tmp_path):
    group = tmp_path / "flash__2024.06.01_vs_IYSA_home"
    group.mkdir()
    return ProviderContext(group_dir=group, team_name="flash", storage_path=tmp_path)


@pytest.fixture(autouse=True)
def _import_homegrown_for_registration():
    # Importing the package self-registers the homegrown provider + all stages.
    import video_grouper.ball_tracking.providers.homegrown  # noqa: F401


class TestRegistration:
    def test_homegrown_is_registered(self):
        assert "homegrown" in _PROVIDER_REGISTRY

    def test_default_stages_are_registered(self):
        names = set(list_stages())
        assert {"stitch_correct", "detect", "track", "render"} <= names

    def test_create_provider_returns_homegrown(self):
        cfg = HomegrownProviderConfig()
        provider = create_provider("homegrown", cfg)
        assert isinstance(provider, HomegrownProvider)


class TestConfigDefaults:
    def test_default_stage_list(self):
        cfg = HomegrownProviderConfig()
        assert cfg.enabled_stages == [
            "stitch_correct",
            "detect",
            "track",
            "render",
        ]

    def test_csv_string_is_split(self):
        cfg = HomegrownProviderConfig.model_validate(
            {"stages": "stitch_correct, detect, track, render"}
        )
        assert cfg.enabled_stages == [
            "stitch_correct",
            "detect",
            "track",
            "render",
        ]

    def test_render_defaults(self):
        cfg = HomegrownProviderConfig()
        assert cfg.render_output_width == 1920
        assert cfg.render_output_height == 1080
        assert cfg.render_ema == 0.975


class _RecordingStage(ProcessingStage):
    """Test double: records the order it ran in and writes to output_path."""

    name = "recording"
    invocations: list[str] = []

    async def run(self, artifacts, ctx):
        _RecordingStage.invocations.append(artifacts["input_path"])
        return None


class _OutputCreatingStage(ProcessingStage):
    """Test double: creates a small file at output_path so the provider
    can validate that the pipeline actually produced something."""

    name = "out_creating"

    async def run(self, artifacts, ctx):
        Path(artifacts["output_path"]).write_bytes(b"x" * 1024)
        return None


class _RaisingStage(ProcessingStage):
    name = "raising"

    async def run(self, artifacts, ctx):
        raise RuntimeError("stage blew up")


@pytest.fixture
def ephemeral_stages():
    """Register the test-double stages, restore registry afterwards."""
    from video_grouper.ball_tracking.providers.homegrown.stages import (
        _STAGE_REGISTRY,
    )

    snapshot = dict(_STAGE_REGISTRY)
    _RecordingStage.invocations = []
    register_stage(_RecordingStage.name, _RecordingStage)
    register_stage(_OutputCreatingStage.name, _OutputCreatingStage)
    register_stage(_RaisingStage.name, _RaisingStage)
    yield
    _STAGE_REGISTRY.clear()
    _STAGE_REGISTRY.update(snapshot)


class TestProviderRun:
    @pytest.mark.asyncio
    async def test_runs_stages_in_order(self, ephemeral_stages, context, tmp_path):
        cfg = HomegrownProviderConfig(stages=["recording", "out_creating"])
        provider = HomegrownProvider(cfg)
        out = tmp_path / "out.mp4"

        ok = await provider.run(str(tmp_path / "in.mp4"), str(out), context)

        assert ok is True
        assert out.exists() and out.stat().st_size > 0
        assert _RecordingStage.invocations == [str(tmp_path / "in.mp4")]

    @pytest.mark.asyncio
    async def test_unknown_stage_returns_false(
        self, ephemeral_stages, context, tmp_path
    ):
        cfg = HomegrownProviderConfig(stages=["nonexistent"])
        provider = HomegrownProvider(cfg)
        out = tmp_path / "out.mp4"

        ok = await provider.run(str(tmp_path / "in.mp4"), str(out), context)

        assert ok is False
        assert not out.exists()

    @pytest.mark.asyncio
    async def test_stage_exception_is_caught_and_returns_false(
        self, ephemeral_stages, context, tmp_path
    ):
        cfg = HomegrownProviderConfig(stages=["raising", "out_creating"])
        provider = HomegrownProvider(cfg)
        out = tmp_path / "out.mp4"

        ok = await provider.run(str(tmp_path / "in.mp4"), str(out), context)

        assert ok is False
        # Pipeline aborts on first failure — out_creating shouldn't run.
        assert not out.exists()

    @pytest.mark.asyncio
    async def test_empty_output_returns_false(
        self, ephemeral_stages, context, tmp_path
    ):
        cfg = HomegrownProviderConfig(stages=["recording"])
        provider = HomegrownProvider(cfg)
        out = tmp_path / "out.mp4"

        # No stage writes to out — provider must catch this.
        ok = await provider.run(str(tmp_path / "in.mp4"), str(out), context)

        assert ok is False


class TestStitchCorrectPassThrough:
    @pytest.mark.asyncio
    async def test_skips_when_no_profile_path(self, context, tmp_path):
        from video_grouper.ball_tracking.providers.homegrown.stages.stitch_correct import (
            StitchCorrectStage,
        )

        cfg = HomegrownProviderConfig()  # stitch_profile_path is None
        stage = StitchCorrectStage(cfg)
        artifacts = {"input_path": str(tmp_path / "in.mp4")}
        result = await stage.run(artifacts, context)

        # Returning None signals "no changes to artifacts dict"; subsequent
        # stages keep using the original input_path.
        assert result is None


class TestDetectStageRequiresModel:
    @pytest.mark.asyncio
    async def test_raises_when_model_path_missing(self, context, tmp_path):
        from video_grouper.ball_tracking.providers.homegrown.stages.detect import (
            DetectStage,
        )

        cfg = HomegrownProviderConfig()  # model_path is None
        stage = DetectStage(cfg)
        artifacts = {"input_path": str(tmp_path / "in.mp4")}
        with pytest.raises(RuntimeError, match="model_path is not configured"):
            await stage.run(artifacts, context)


class TestTrackStageRequiresDetections:
    @pytest.mark.asyncio
    async def test_raises_when_detections_missing(self, context, tmp_path):
        from video_grouper.ball_tracking.providers.homegrown.stages.track import (
            TrackStage,
        )

        cfg = HomegrownProviderConfig()
        stage = TrackStage(cfg)
        artifacts = {"input_path": str(tmp_path / "in.mp4")}
        with pytest.raises(RuntimeError, match="detections_path missing"):
            await stage.run(artifacts, context)


class TestRenderStageRequiresTrajectory:
    @pytest.mark.asyncio
    async def test_raises_when_trajectory_missing(self, context, tmp_path):
        from video_grouper.ball_tracking.providers.homegrown.stages.render import (
            RenderStage,
        )

        cfg = HomegrownProviderConfig()
        stage = RenderStage(cfg)
        artifacts = {
            "input_path": str(tmp_path / "in.mp4"),
            "output_path": str(tmp_path / "out.mp4"),
        }
        with pytest.raises(RuntimeError, match="trajectory_path missing"):
            await stage.run(artifacts, context)


class TestProviderResolvesHomegrown:
    """End-to-end: BallTrackingConfig.resolve_provider_for returns the
    homegrown provider when configured."""

    def test_homegrown_via_top_level_provider(self):
        from video_grouper.ball_tracking.config import BallTrackingConfig

        cfg = BallTrackingConfig(provider="homegrown")
        name, sub = cfg.resolve_provider_for(None)
        assert name == "homegrown"
        assert isinstance(sub, HomegrownProviderConfig)

    def test_homegrown_via_per_team_override(self):
        from video_grouper.ball_tracking.config import BallTrackingConfig

        cfg = BallTrackingConfig(
            provider="autocam_gui",
            per_team={"flash": "homegrown"},
        )
        name, sub = cfg.resolve_provider_for("flash")
        assert name == "homegrown"
        assert isinstance(sub, HomegrownProviderConfig)


class TestSmokeProviderRunMockedStages:
    """Smoke: provider routes through patched stages end-to-end."""

    @pytest.mark.asyncio
    async def test_stages_receive_provider_context(
        self, ephemeral_stages, context, tmp_path
    ):
        cfg = HomegrownProviderConfig(stages=["out_creating"])
        provider = HomegrownProvider(cfg)
        out = tmp_path / "out.mp4"

        with patch.object(
            _OutputCreatingStage, "run", new=AsyncMock(return_value=None)
        ) as mock_run:
            # Mock doesn't actually write; we expect provider to fail validation.
            ok = await provider.run(str(tmp_path / "in.mp4"), str(out), context)

        assert ok is False
        assert mock_run.await_count == 1
        # First arg: artifacts; second: ctx
        artifacts = mock_run.await_args.args[0]
        assert artifacts["input_path"] == str(tmp_path / "in.mp4")
        assert artifacts["output_path"] == str(out)
        ctx_arg = mock_run.await_args.args[1]
        assert ctx_arg.team_name == "flash"
