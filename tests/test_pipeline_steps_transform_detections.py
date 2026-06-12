"""End-to-end tests for the ``transform_detections`` pipeline step.

These exercise the reprocess shortcut: existing raw-coord detections +
a fresh motion.json → stabilized-coord detections written back, with
the manifest's ``detections_path`` updated for downstream consumers.

Math correctness for the per-point transform itself is covered by
``test_stabilization.py::TestFrameStabilizerTransformPoints``; these
tests focus on the step's contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

import video_grouper.pipeline.register_steps  # noqa: F401
from video_grouper.pipeline import create_step, get_step_meta, list_steps
from video_grouper.pipeline.base import StepContext
from video_grouper.pipeline.manifest import PipelineManifest
from video_grouper.pipeline.steps.transform_detections import (
    TransformDetectionsStep,
)


def _ctx(tmp_path: Path) -> StepContext:
    return StepContext(group_dir=tmp_path, team_name=None, storage_path=tmp_path)


def _write_identity_motion_json(path: Path, src_size=(720, 1280), n_frames=10) -> None:
    """A motion.json whose per-frame M shifts by a known (dx, dy) so we
    can assert exact transformed coords without re-deriving the math."""
    from video_grouper.inference.stabilization import write_motion_json

    inset_y, inset_x = 20, 40
    sx, sy = 5.0, -3.0  # per-frame shift
    M = np.array([[1.0, 0.0, inset_x + sx], [0.0, 1.0, inset_y + sy]], dtype=np.float32)
    src_h, src_w = src_size
    write_motion_json(
        path,
        src_size=(src_h, src_w),
        output_size=(src_h - 2 * inset_y, src_w - 2 * inset_x),
        safe_inset=(inset_y, inset_x),
        transforms=[M.copy() for _ in range(n_frames)],
        confidences=[1.0] * n_frames,
    )


def _write_synthetic_detections(path: Path, n_frames=10) -> list[dict]:
    """One detection per frame, in raw-source coords."""
    detections = [
        {
            "frame_idx": i,
            "cx": 100.0 + 10.0 * i,
            "cy": 200.0 + 5.0 * i,
            "w": 24.0,
            "h": 24.0,
            "conf": 0.92,
        }
        for i in range(n_frames)
    ]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(detections, f)
    return detections


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_step_registered():
    assert "transform_detections" in list_steps()


def test_step_metadata():
    meta = get_step_meta("transform_detections")
    assert meta.runtime == "service"
    assert meta.available is True


def test_consumes_produces_contract():
    assert "detections_path" in TransformDetectionsStep.consumes
    assert "motion_path" in TransformDetectionsStep.consumes
    assert TransformDetectionsStep.produces == ("detections_path",)


def test_create_validates_config():
    step = create_step(
        "transform_detections",
        {"transform_detections_output_name": "alt.json"},
    )
    assert isinstance(step, TransformDetectionsStep)
    assert step.config.transform_detections_output_name == "alt.json"


# ---------------------------------------------------------------------------
# End-to-end (synthetic detections + motion.json → transformed JSON)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transforms_each_detection_and_updates_manifest(tmp_path: Path):
    """Happy path: every detection's (cx, cy) gets shifted by the same
    motion.json amount, and the manifest is repointed."""
    detections_path = tmp_path / "detections.json"
    motion_path = tmp_path / "motion.json"
    raw = _write_synthetic_detections(detections_path, n_frames=5)
    _write_identity_motion_json(motion_path, n_frames=5)

    manifest = PipelineManifest.load_or_init(
        tmp_path, str(tmp_path / "src.mp4"), str(tmp_path / "out.mp4")
    )
    manifest.put("detections_path", str(detections_path))
    manifest.put("motion_path", str(motion_path))

    step = create_step("transform_detections", {})
    ok = await step.run(manifest, _ctx(tmp_path))
    assert ok is True

    new_path = manifest.get("detections_path")
    assert Path(new_path).name == "detections_stabilized.json"
    transformed = json.loads(Path(new_path).read_text())
    assert len(transformed) == len(raw)
    # Identity-with-shift motion.json: each src point (x, y) ends up at
    # (x - inset_x - sx, y - inset_y - sy). From the helper above:
    # inset_x=40, inset_y=20, sx=5, sy=-3 → dx=-45, dy=-17.
    for raw_det, new_det in zip(raw, transformed, strict=False):
        assert new_det["cx"] == pytest.approx(raw_det["cx"] - 45.0, abs=1e-3)
        assert new_det["cy"] == pytest.approx(raw_det["cy"] - 17.0, abs=1e-3)
        # Non-coord fields are untouched.
        assert new_det["w"] == raw_det["w"]
        assert new_det["conf"] == raw_det["conf"]


@pytest.mark.asyncio
async def test_multiple_detections_per_frame_share_one_warp(tmp_path: Path):
    """Detections in the same frame must all use the same per-frame M
    (the implementation buckets by frame_idx then vectorises) — guards
    against an off-by-one or per-detection-bug."""
    detections_path = tmp_path / "detections.json"
    motion_path = tmp_path / "motion.json"
    _write_identity_motion_json(motion_path, n_frames=3)
    raw = [
        {"frame_idx": 0, "cx": 100.0, "cy": 100.0, "w": 1, "h": 1, "conf": 0.5},
        {"frame_idx": 0, "cx": 200.0, "cy": 200.0, "w": 1, "h": 1, "conf": 0.5},
        {"frame_idx": 1, "cx": 300.0, "cy": 300.0, "w": 1, "h": 1, "conf": 0.5},
    ]
    with open(detections_path, "w") as f:
        json.dump(raw, f)

    manifest = PipelineManifest.load_or_init(
        tmp_path, str(tmp_path / "src.mp4"), str(tmp_path / "out.mp4")
    )
    manifest.put("detections_path", str(detections_path))
    manifest.put("motion_path", str(motion_path))
    step = create_step("transform_detections", {})
    await step.run(manifest, _ctx(tmp_path))

    transformed = json.loads(Path(manifest.get("detections_path")).read_text())
    # The two frame-0 detections both shifted by (-45, -17).
    assert transformed[0]["cx"] == pytest.approx(55.0, abs=1e-3)
    assert transformed[1]["cx"] == pytest.approx(155.0, abs=1e-3)
    assert transformed[2]["cx"] == pytest.approx(255.0, abs=1e-3)


@pytest.mark.asyncio
async def test_zone_blend_motion_json_is_supported(tmp_path: Path):
    """Reprocess against a polygon-zone blend motion.json — each detection
    looks up its source-coord zone and uses that zone's stabilization."""
    from video_grouper.inference.stabilization import write_motion_json

    detections_path = tmp_path / "detections.json"
    motion_path = tmp_path / "motion.json"

    H, W = 240, 480
    iy, ix = 10, 20

    def M(sx: float, sy: float) -> np.ndarray:
        return np.array([[1.0, 0.0, ix + sx], [0.0, 1.0, iy + sy]], dtype=np.float32)

    poly = np.array(
        [
            [W * 0.18, H * 0.30],
            [W * 0.82, H * 0.30],
            [W * 0.92, H * 0.78],
            [W * 0.08, H * 0.78],
        ],
        dtype=np.float32,
    )
    write_motion_json(
        motion_path,
        src_size=(H, W),
        output_size=(H - 2 * iy, W - 2 * ix),
        safe_inset=(iy, ix),
        transforms=None,
        confidences=[1.0],
        zone_transforms={
            "sky": [M(+10.0, 0.0)],
            "field": [M(0.0, -20.0)],
            "near": [M(-30.0, 0.0)],
        },
        polygon=poly,
    )
    # One detection in each band — must end up shifted by that band's M.
    raw = [
        {
            "frame_idx": 0,
            "cx": W * 0.5,
            "cy": H * 0.10,
            "w": 1,
            "h": 1,
            "conf": 1.0,
        },  # sky
        {
            "frame_idx": 0,
            "cx": W * 0.5,
            "cy": H * 0.55,
            "w": 1,
            "h": 1,
            "conf": 1.0,
        },  # field
        {
            "frame_idx": 0,
            "cx": W * 0.5,
            "cy": H * 0.90,
            "w": 1,
            "h": 1,
            "conf": 1.0,
        },  # near
    ]
    with open(detections_path, "w") as f:
        json.dump(raw, f)

    manifest = PipelineManifest.load_or_init(
        tmp_path, str(tmp_path / "src.mp4"), str(tmp_path / "out.mp4")
    )
    manifest.put("detections_path", str(detections_path))
    manifest.put("motion_path", str(motion_path))
    step = create_step("transform_detections", {})
    await step.run(manifest, _ctx(tmp_path))

    transformed = json.loads(Path(manifest.get("detections_path")).read_text())
    # Sky band shifted x by +10.
    assert transformed[0]["cx"] == pytest.approx(W * 0.5 - ix - 10.0, abs=1e-3)
    # Field band shifted y by -20.
    assert transformed[1]["cy"] == pytest.approx(H * 0.55 - iy + 20.0, abs=1e-3)
    # Near band shifted x by -30.
    assert transformed[2]["cx"] == pytest.approx(W * 0.5 - ix + 30.0, abs=1e-3)


@pytest.mark.asyncio
async def test_alternate_field_names(tmp_path: Path):
    """If detections use different centroid field names (player boxes,
    keypoints), the step honours the config."""
    detections_path = tmp_path / "detections.json"
    motion_path = tmp_path / "motion.json"
    _write_identity_motion_json(motion_path, n_frames=2)
    raw = [
        {"frame_idx": 0, "x_centre": 100.0, "y_centre": 200.0, "tag": "a"},
        {"frame_idx": 1, "x_centre": 150.0, "y_centre": 250.0, "tag": "b"},
    ]
    with open(detections_path, "w") as f:
        json.dump(raw, f)
    manifest = PipelineManifest.load_or_init(
        tmp_path, str(tmp_path / "src.mp4"), str(tmp_path / "out.mp4")
    )
    manifest.put("detections_path", str(detections_path))
    manifest.put("motion_path", str(motion_path))
    step = create_step(
        "transform_detections",
        {
            "transform_detections_x_field": "x_centre",
            "transform_detections_y_field": "y_centre",
        },
    )
    await step.run(manifest, _ctx(tmp_path))

    transformed = json.loads(Path(manifest.get("detections_path")).read_text())
    assert transformed[0]["x_centre"] == pytest.approx(55.0, abs=1e-3)
    assert transformed[0]["y_centre"] == pytest.approx(183.0, abs=1e-3)
    assert transformed[0]["tag"] == "a"
