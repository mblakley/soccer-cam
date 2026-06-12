"""End-to-end tests for the ``stabilize`` pipeline step.

These exercise the manifest contract + the full analysis pass against a
synthetic wobbling video. The pure-helper layer is covered by
``test_stabilization.py``.
"""

from __future__ import annotations

import json
import math
from fractions import Fraction
from pathlib import Path

import cv2
import numpy as np
import pytest

# Side-effect: registers all built-in steps including stabilize.
import video_grouper.pipeline.register_steps  # noqa: F401
from video_grouper.pipeline import create_step, get_step_meta, list_steps
from video_grouper.pipeline.base import StepContext
from video_grouper.pipeline.manifest import PipelineManifest
from video_grouper.pipeline.steps.stabilize import StabilizeStep

# Tests here need REAL PyAV (synthetic video write + decode) and REAL
# filesystem checks. The conftest's autouse mock_ffmpeg / mock_file_system
# fixtures would otherwise stub those out — override locally.


@pytest.fixture(autouse=True)
def mock_ffmpeg():  # noqa: PT004 — fixture override
    yield None


@pytest.fixture(autouse=True)
def mock_file_system():  # noqa: PT004 — fixture override
    yield None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(tmp_path: Path) -> StepContext:
    return StepContext(group_dir=tmp_path, team_name=None, storage_path=tmp_path)


def _textured_frame(
    rng_seed: int = 1, width: int = 640, height: int = 360
) -> np.ndarray:
    """High-contrast textured base frame ORB can index reliably."""
    rng = np.random.default_rng(rng_seed)
    img = np.full((height, width, 3), 128, dtype=np.uint8)
    for _ in range(400):
        x = int(rng.integers(20, width - 20))
        y = int(rng.integers(20, height - 20))
        r = int(rng.integers(3, 12))
        color = tuple(int(c) for c in rng.integers(0, 256, size=3))
        if rng.random() < 0.5:
            cv2.rectangle(img, (x - r, y - r), (x + r, y + r), color, -1)
        else:
            cv2.circle(img, (x, y), r, color, -1)
    return img


def _write_wobbling_video(
    path: Path,
    base: np.ndarray,
    wobble_amplitude_px: float = 6.0,
    n_frames: int = 30,
    fps: int = 10,
) -> None:
    """Write an mp4 where each frame is the base, sinusoidally translated."""
    import av

    height, width = base.shape[:2]
    container = av.open(str(path), mode="w")
    try:
        stream = container.add_stream("h264", rate=fps)
        stream.width = width
        stream.height = height
        stream.pix_fmt = "yuv420p"
        stream.codec_context.time_base = Fraction(1, fps)
        for i in range(n_frames):
            dx = wobble_amplitude_px * math.sin(2 * math.pi * i / 10)
            dy = wobble_amplitude_px * math.cos(2 * math.pi * i / 10) * 0.6
            M = np.array([[1, 0, dx], [0, 1, dy]], dtype=np.float32)
            frame_arr = cv2.warpAffine(
                base,
                M,
                (width, height),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REPLICATE,
            )
            vf = av.VideoFrame.from_ndarray(frame_arr, format="rgb24")
            vf.pts = i
            for packet in stream.encode(vf):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()
    assert path.exists() and path.stat().st_size > 0, f"PyAV failed to write {path}"


# ---------------------------------------------------------------------------
# Registry / metadata
# ---------------------------------------------------------------------------


def test_stabilize_registered():
    assert "stabilize" in list_steps()


def test_stabilize_step_metadata():
    meta = get_step_meta("stabilize")
    assert meta.runtime == "service"
    assert meta.resources == ("ram_heavy",)
    assert set(meta.requires) >= {"av", "cv2", "scipy"}
    assert meta.available is True  # deps present in dev venv


def test_create_stabilize_step_validates_config():
    step = create_step(
        "stabilize",
        {"stabilize_max_tx_px": 100.0, "stabilize_max_ty_px": 80.0},
    )
    assert isinstance(step, StabilizeStep)
    assert step.config.stabilize_max_tx_px == 100.0
    assert step.config.stabilize_max_ty_px == 80.0
    # default unchanged
    assert step.config.stabilize_max_rotation_deg == 1.5


def test_consumes_produces_contract():
    assert StabilizeStep.consumes == ("input_path",)
    assert StabilizeStep.produces == ("motion_path",)


# ---------------------------------------------------------------------------
# Strength preset enum
# ---------------------------------------------------------------------------


class TestStabilizationStrength:
    """The strength preset is the user-facing dial for the
    cheap↔aggressive trade-off; the UI dropdown and the reprocess
    request mechanism both pass strength names through to this config."""

    def test_no_preset_keeps_class_defaults(self):
        from video_grouper.pipeline.steps.stabilize import StabilizeStepConfig

        c = StabilizeStepConfig()
        assert c.stabilization_strength is None
        assert c.stabilize_max_tx_px == 60.0
        assert c.stabilize_max_rotation_deg == 1.5
        # Polygon-blend default off so the cheap single-warp path runs unless
        # the user explicitly opts in (via the field or via heavy/extreme).
        assert c.stabilize_polygon_blend is False

    @pytest.mark.parametrize(
        "name,expected_polygon_blend,expected_tx,expected_rot",
        [
            ("light", False, 30.0, 0.5),
            ("standard", False, 60.0, 1.0),
            ("heavy", True, 60.0, 1.5),
            ("extreme", True, 100.0, 2.5),
        ],
    )
    def test_each_strength_fills_in_budgets(
        self, name, expected_polygon_blend, expected_tx, expected_rot
    ):
        from video_grouper.pipeline.steps.stabilize import StabilizeStepConfig

        c = StabilizeStepConfig(stabilization_strength=name)
        assert c.stabilize_polygon_blend is expected_polygon_blend
        assert c.stabilize_max_tx_px == expected_tx
        assert c.stabilize_max_rotation_deg == expected_rot

    def test_strengths_form_monotonic_correction_budget(self):
        """The cost ranking light < standard < heavy < extreme must show
        up as monotonic non-decreasing per-axis budgets — otherwise the
        UI dial would lie about what users get."""
        from video_grouper.pipeline.steps.stabilize import (
            STABILIZATION_STRENGTH_PRESETS,
        )

        order = ["light", "standard", "heavy", "extreme"]
        for axis in (
            "stabilize_max_tx_px",
            "stabilize_max_ty_px",
            "stabilize_max_rotation_deg",
            "stabilize_max_log_scale",
        ):
            values = [STABILIZATION_STRENGTH_PRESETS[n][axis] for n in order]
            assert values == sorted(values), (
                f"{axis} is not monotonic across strengths: {values}"
            )

    def test_polygon_blend_engaged_only_for_heavy_and_extreme(self):
        """The 3× per-frame apply cost (the actually-expensive part)
        only switches on for heavy + extreme. light/standard stay
        single-warp so their cost is comparable to today's production."""
        from video_grouper.pipeline.steps.stabilize import (
            STABILIZATION_STRENGTH_PRESETS,
        )

        assert (
            STABILIZATION_STRENGTH_PRESETS["light"]["stabilize_polygon_blend"] is False
        )
        assert (
            STABILIZATION_STRENGTH_PRESETS["standard"]["stabilize_polygon_blend"]
            is False
        )
        assert (
            STABILIZATION_STRENGTH_PRESETS["heavy"]["stabilize_polygon_blend"] is True
        )
        assert (
            STABILIZATION_STRENGTH_PRESETS["extreme"]["stabilize_polygon_blend"] is True
        )

    def test_explicit_field_overrides_preset_value(self):
        """If a caller passes both a preset name AND an explicit override,
        the override wins — that's how power users keep full per-axis
        control."""
        from video_grouper.pipeline.steps.stabilize import StabilizeStepConfig

        c = StabilizeStepConfig(
            stabilization_strength="light",
            stabilize_max_rotation_deg=4.0,  # way above light's 0.5
            stabilize_polygon_blend=True,
        )
        # Light's other fields applied; the two overrides won.
        assert c.stabilize_max_tx_px == 30.0
        assert c.stabilize_max_rotation_deg == 4.0
        assert c.stabilize_polygon_blend is True

    def test_unknown_strength_rejected(self):
        from pydantic import ValidationError

        from video_grouper.pipeline.steps.stabilize import StabilizeStepConfig

        with pytest.raises(ValidationError):
            StabilizeStepConfig(stabilization_strength="ridiculous")


# ---------------------------------------------------------------------------
# End-to-end on a synthetic wobbling video
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stabilize_writes_motion_json(tmp_path: Path):
    """Full StabilizeStep.run on a tiny synthetic wobbling video produces a
    motion.json with the documented shape + a manifest entry."""
    video_path = tmp_path / "synthetic.mp4"
    _write_wobbling_video(
        video_path, _textured_frame(rng_seed=3), wobble_amplitude_px=5.0, n_frames=30
    )

    manifest = PipelineManifest.load_or_init(
        tmp_path, str(video_path), str(tmp_path / "out.mp4")
    )
    manifest.put("input_path", str(video_path))

    step = create_step(
        "stabilize",
        {
            # Permissive thresholds — the synthetic frames are very small so
            # the L1 budget shouldn't bind and the residual is tiny.
            "stabilize_max_tx_px": 30.0,
            "stabilize_max_ty_px": 30.0,
            "stabilize_max_rotation_deg": 1.0,
        },
    )
    ok = await step.run(manifest, _ctx(tmp_path))
    assert ok is True

    motion_path = manifest.get("motion_path")
    assert motion_path is not None
    assert Path(motion_path).exists()

    with open(motion_path, encoding="utf-8") as f:
        payload = json.load(f)

    assert "src_size" in payload
    assert "output_size" in payload
    assert "safe_inset" in payload
    assert "frames" in payload
    assert len(payload["frames"]) > 0
    # Per-frame shape
    first = payload["frames"][0]
    assert "M" in first
    assert "confidence" in first
    M = first["M"]
    assert len(M) == 2 and len(M[0]) == 3
    # output_size should be smaller than src_size by 2*inset
    src_h, src_w = payload["src_size"]
    out_h, out_w = payload["output_size"]
    iy, ix = payload["safe_inset"]
    assert out_h == src_h - 2 * iy
    assert out_w == src_w - 2 * ix


@pytest.mark.asyncio
async def test_stabilize_no_polygon_fallback(tmp_path: Path):
    """When ``field_polygon_path`` is absent the step falls back to the
    no-polygon (sky-strip-only) mask and still succeeds."""
    video_path = tmp_path / "synthetic.mp4"
    _write_wobbling_video(
        video_path, _textured_frame(rng_seed=4), wobble_amplitude_px=3.0, n_frames=20
    )

    manifest = PipelineManifest.load_or_init(
        tmp_path, str(video_path), str(tmp_path / "out.mp4")
    )
    manifest.put("input_path", str(video_path))
    # NOTE: no field_polygon_path put → step must fall back gracefully.

    step = create_step("stabilize", {})
    ok = await step.run(manifest, _ctx(tmp_path))
    assert ok is True
    assert manifest.get("motion_path") is not None


@pytest.mark.asyncio
async def test_stabilize_residual_within_budget(tmp_path: Path):
    """Residuals (cumulative − smoothed) should never exceed the configured
    per-axis budget — the L1 LP's box constraints guarantee this."""
    video_path = tmp_path / "synthetic.mp4"
    base = _textured_frame(rng_seed=5)
    _write_wobbling_video(video_path, base, wobble_amplitude_px=2.5, n_frames=40)

    manifest = PipelineManifest.load_or_init(
        tmp_path, str(video_path), str(tmp_path / "out.mp4")
    )
    manifest.put("input_path", str(video_path))

    R_tx = 10.0
    R_ty = 10.0
    step = create_step(
        "stabilize",
        {"stabilize_max_tx_px": R_tx, "stabilize_max_ty_px": R_ty},
    )
    await step.run(manifest, _ctx(tmp_path))
    motion_path = manifest.get("motion_path")
    with open(motion_path, encoding="utf-8") as f:
        payload = json.load(f)
    # Each per-frame matrix's translation must satisfy |M[0,2]|, |M[1,2]| <= R + inset
    # (matrix is residual ∘ inset, so the translation = residual + inset.cos(theta) etc.).
    iy, ix = payload["safe_inset"]
    for frame in payload["frames"]:
        M = frame["M"]
        # Absolute translation in the matrix is (residual + inset) — bounded
        # by ((budget) + inset) per axis.
        assert abs(M[0][2] - ix) <= R_tx + 1.0
        assert abs(M[1][2] - iy) <= R_ty + 1.0


# ---------------------------------------------------------------------------
# Integration with FrameStabilizer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frame_stabilizer_loads_step_output(tmp_path: Path):
    """The FrameStabilizer can consume the motion.json the step writes,
    expose the right output_shape, and warp a frame without error."""
    from video_grouper.inference.stabilization import FrameStabilizer

    video_path = tmp_path / "synthetic.mp4"
    base = _textured_frame(rng_seed=6)
    _write_wobbling_video(video_path, base, wobble_amplitude_px=4.0, n_frames=25)

    manifest = PipelineManifest.load_or_init(
        tmp_path, str(video_path), str(tmp_path / "out.mp4")
    )
    manifest.put("input_path", str(video_path))
    step = create_step("stabilize", {})
    await step.run(manifest, _ctx(tmp_path))

    motion_path = manifest.get("motion_path")
    stabilizer = FrameStabilizer.from_json(motion_path)
    src_h, src_w = stabilizer.src_size
    out_h, out_w = stabilizer.output_shape
    assert out_h < src_h and out_w < src_w
    # Apply to a dummy frame.
    rgb = base.copy()  # same dims as the encoded source
    stabilized = stabilizer.apply(rgb, frame_idx=0)
    assert stabilized.shape == (out_h, out_w, 3)
