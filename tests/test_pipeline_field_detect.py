"""Unit tests for the field_detect pipeline step.

Covers the pure helpers (sample-time spread, normalized->pixel override
scaling), registration + preset membership, and the graceful pass-through
(no model + no override writes a null polygon but still produces the
field_polygon_path artifact).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import video_grouper.pipeline.register_steps  # noqa: F401 — populate registry
from video_grouper.pipeline import create_step
from video_grouper.pipeline.base import StepContext
from video_grouper.pipeline.presets import get_preset
from video_grouper.pipeline.steps.field_detect import (
    FieldDetectStep,
    FieldDetectStepConfig,
    _normalize_points,
    _override_payload,
    _sample_times,
)


class _FakeManifest:
    """Minimal manifest stand-in — the step only uses get/put."""

    def __init__(self, artifacts: dict):
        self._a = dict(artifacts)

    def get(self, key: str):
        return self._a.get(key)

    def put(self, key: str, value: str):
        self._a[key] = value


def test_sample_times_spread():
    times = _sample_times(1000.0, 7)
    assert len(times) == 7
    assert times[0] == pytest.approx(100.0)
    assert times[-1] == pytest.approx(900.0)
    assert times == sorted(times)
    assert _sample_times(0.0, 7) == [0.0]


def test_override_payload_scales_to_pixels():
    norm = [[i / 10, 0.8 if i < 5 else 0.2] for i in range(10)]
    payload = _override_payload(norm, src_w=1000, src_h=500)
    assert payload["source"] == "user_override"
    assert len(payload["polygon"]) == 10
    # normalized (0.0, 0.8) -> (0, 400); (0.4, 0.8) -> (400, 400)
    assert payload["polygon"][0] == [0.0, 400.0]
    assert payload["polygon"][4] == [400.0, 400.0]
    # polygon_norm (the editor seed) round-trips the normalized input
    assert payload["polygon_norm"] == norm
    assert payload["src_w"] == 1000 and payload["src_h"] == 500
    # 10 keypoints with score 1.0; homography derivable (>=4 points)
    assert len(payload["keypoints"]) == 10
    assert payload["keypoints"][0][2] == 1.0
    assert payload["homography"] is not None


def test_normalize_points_clamps_to_unit_square():
    # (x, y) pixel coords -> [0,1]; out-of-frame points clamp, not wrap.
    kpts = [(500.0, 250.0, 0.9), (-10.0, 600.0, 0.1), (None, None, 0.0)]
    norm = _normalize_points(kpts, src_w=1000, src_h=500)
    assert norm[0] == [0.5, 0.5]
    assert norm[1] == [0.0, 1.0]  # x<0 -> 0, y>h -> 1
    assert norm[2] == [0.0, 0.0]  # None -> 0


def test_field_detect_registered_and_in_presets():
    step = create_step("field_detect", {})
    assert isinstance(step, FieldDetectStep)
    for preset in ("homegrown", "broadcast_stabilized"):
        types = [t for _id, t, _cfg in get_preset(preset)]
        assert "field_detect" in types


@pytest.mark.asyncio
async def test_passthrough_writes_null_polygon(tmp_path):
    step = FieldDetectStep(FieldDetectStepConfig())  # no model, no override
    manifest = _FakeManifest({"input_path": str(tmp_path / "combined.mp4")})
    ctx = StepContext(
        group_dir=tmp_path,
        team_name="heat",
        storage_path=tmp_path,
        ttt_config=None,
    )
    ok = await step.run(manifest, ctx)
    assert ok is True
    pp = manifest.get("field_polygon_path")
    assert pp is not None and Path(pp).exists()
    payload = json.loads(Path(pp).read_text())
    assert payload["polygon"] is None
    assert payload["source"] == "none"
