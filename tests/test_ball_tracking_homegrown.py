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
        assert {"stitch_correct", "field_mask", "detect", "track", "render"} <= names

    def test_create_provider_returns_homegrown(self):
        cfg = HomegrownProviderConfig()
        provider = create_provider("homegrown", cfg)
        assert isinstance(provider, HomegrownProvider)


class TestConfigDefaults:
    def test_default_stage_list(self):
        cfg = HomegrownProviderConfig()
        assert cfg.enabled_stages == [
            "stitch_correct",
            "field_mask",
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

    def test_field_mask_defaults(self):
        cfg = HomegrownProviderConfig()
        assert cfg.field_mask_model_key is None
        assert cfg.field_mask_model_path is None
        assert cfg.field_mask_confidence == 0.7

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
    async def test_raises_when_neither_model_key_nor_model_path_set(
        self, context, tmp_path
    ):
        from video_grouper.ball_tracking.providers.homegrown.stages.detect import (
            DetectStage,
        )

        cfg = HomegrownProviderConfig()  # both model_key and model_path are None
        stage = DetectStage(cfg)
        artifacts = {"input_path": str(tmp_path / "in.mp4")}
        with pytest.raises(
            RuntimeError, match="neither model_key nor model_path is configured"
        ):
            await stage.run(artifacts, context)

    @pytest.mark.asyncio
    async def test_raises_when_model_key_set_but_no_ttt_config(self, context, tmp_path):
        from video_grouper.ball_tracking.providers.homegrown.stages.detect import (
            DetectStage,
        )

        cfg = HomegrownProviderConfig(model_key="video.ball_detection")
        stage = DetectStage(cfg)
        artifacts = {"input_path": str(tmp_path / "in.mp4")}
        # context fixture doesn't set ttt_config — production path needs it
        with pytest.raises(RuntimeError, match="TTT integration is disabled"):
            await stage.run(artifacts, context)

    @pytest.mark.asyncio
    async def test_uses_secure_loader_when_model_key_and_ttt_config_set(self, tmp_path):
        """End-to-end mock: model_key + ttt_config drives SecureLoader,
        the resulting session is passed to detect_video, detections JSON
        is written. Verifies the wiring without touching real ONNX/TTT."""
        from unittest.mock import MagicMock

        from video_grouper.ball_tracking.base import ProviderContext
        from video_grouper.ball_tracking.providers.homegrown.stages.detect import (
            DetectStage,
        )

        # Spell out the production-mode context — model_key + ttt_config set.
        ctx = ProviderContext(
            group_dir=tmp_path,
            team_name="flash",
            storage_path=tmp_path,
            ttt_config={
                "supabase_url": "https://test.supabase.co",
                "anon_key": "anon",
                "api_base_url": "https://api.test",
                "plugin_signing_public_keys": ["abcd1234"],
            },
        )

        # Source video file must exist for the path argument; contents
        # don't matter because detect_video is mocked.
        video = tmp_path / "in.mp4"
        video.write_bytes(b"fake video bytes")

        cfg = HomegrownProviderConfig(model_key="video.ball_detection")
        stage = DetectStage(cfg)

        loaded_session = MagicMock(name="loaded_session")
        fake_loaded = MagicMock(
            session=loaded_session,
            model_key="video.ball_detection",
            version="1.0.0",
            tier="premium",
            provider="CPUExecutionProvider",
        )

        # Patch SecureLoader.acquire — verifies the production path is taken.
        # Patch detect_video at the call site — verifies the session flows
        # through to the inference helper.
        captured = {}

        def fake_detect_video(video_path, session, frame_interval, conf_threshold):
            captured["session"] = session
            captured["video_path"] = video_path
            captured["frame_interval"] = frame_interval
            captured["conf_threshold"] = conf_threshold
            return [
                {"frame_idx": 1, "cx": 100, "cy": 200, "w": 10, "h": 10, "conf": 0.9}
            ]

        with (
            patch("video_grouper.ball_tracking.secure_loader.SecureLoader") as MockSL,
            patch(
                "video_grouper.ball_tracking.providers.homegrown.stages.detect.detect_video",
                side_effect=fake_detect_video,
            ),
        ):
            MockSL.return_value.acquire.return_value = fake_loaded

            artifacts = {"input_path": str(video)}
            result = await stage.run(artifacts, ctx)

        # The session that detect_video saw is the one SecureLoader produced
        assert captured["session"] is loaded_session
        # Detections JSON was written next to the source
        det_path = Path(result["detections_path"])
        assert det_path.exists()
        # SecureLoader was constructed with the configured public keys
        # and called with the configured model_key
        MockSL.assert_called_once()
        MockSL.return_value.acquire.assert_called_once_with(
            "video.ball_detection", channel=None, pipeline_version=None
        )


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
