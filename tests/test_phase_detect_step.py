"""Unit tests for the phase_detect pipeline step.

The real detector (onnxruntime + video decode) is stubbed; these exercise the
step's plumbing: it loads the field polygon, calls detect_phases, writes the
phases.json artifact, records phases_path, and persists the fused phases to the
group state (source phase_fused).
"""

from __future__ import annotations

import json

import pytest

# Importing register_steps registers all built-ins as a side effect.
import video_grouper.pipeline.register_steps  # noqa: F401
from video_grouper.pipeline import create_step, get_step_meta
from video_grouper.pipeline.base import StepContext
from video_grouper.pipeline.manifest import PipelineManifest
from video_grouper.pipeline.steps.phase_detect import PhaseDetectStep, _build_payload


def _ctx(tmp_path):
    return StepContext(group_dir=tmp_path, team_name=None, storage_path=tmp_path)


def _manifest_with_polygon(tmp_path):
    """A manifest seeded with input_path + a field_polygon.json artifact."""
    poly_path = tmp_path / "field_polygon.json"
    poly_path.write_text(
        json.dumps(
            {
                "polygon": [[0.0, 0.0], [100.0, 0.0], [100.0, 50.0], [0.0, 50.0]],
                "source": "full_frame",
            }
        ),
        encoding="utf-8",
    )
    manifest = PipelineManifest.load_or_init(
        tmp_path, str(tmp_path / "game.mp4"), str(tmp_path / "out.mp4")
    )
    manifest.put("field_polygon_path", str(poly_path))
    return manifest


def test_phase_detect_registered_with_expected_metadata():
    meta = get_step_meta("phase_detect")
    assert meta.runtime == "service"
    assert meta.resources == ("gpu",)
    assert meta.requires == ("onnxruntime", "cv2", "av")


def test_phase_detect_consumes_polygon_and_produces_phases():
    step = create_step("phase_detect", {})
    assert isinstance(step, PhaseDetectStep)
    assert step.consumes == ("input_path", "field_polygon_path")
    assert step.produces == ("phases_path",)
    assert step.config.phase_step_seconds == 12.0


def test_build_payload_none_is_ok_false_no_play():
    payload = _build_payload(None)
    assert payload == {
        "ok": False,
        "source": "phase_fused",
        "times": {},
        "reasons": ["no_play"],
    }


def test_build_payload_clamps_negative_kickoff():
    result = {
        "ok": True,
        "times": {
            "kickoff": -3.0,
            "halftime": 1500.0,
            "second_half": 2100.0,
            "end": 3600.0,
        },
        "reasons": [],
        "used": "whistle+kick",
    }
    payload = _build_payload(result)
    assert payload["ok"] is True
    assert payload["source"] == "phase_fused"
    assert payload["times"]["kickoff"] == 0.0  # clamped
    assert payload["times"]["end"] == 3600.0
    assert payload["used"] == "whistle+kick"


@pytest.mark.asyncio
async def test_phase_detect_writes_artifact_manifest_and_state(tmp_path, monkeypatch):
    """ok=True fit: phases.json + manifest phases_path + state.json game_phases."""
    fake = {
        "ok": True,
        "times": {
            "kickoff": 120.0,
            "halftime": 1500.0,
            "second_half": 2100.0,
            "end": 3600.0,
        },
        "reasons": [],
        "used": "whistle+kick+player",
    }
    captured = {}

    def fake_detect_phases(video_path, polygon, *, step=12.0):
        captured["video_path"] = video_path
        captured["polygon"] = polygon
        captured["step"] = step
        return fake

    monkeypatch.setattr(
        "video_grouper.pipeline.steps.phase_detect.detect_phases", fake_detect_phases
    )

    manifest = _manifest_with_polygon(tmp_path)
    step = create_step("phase_detect", {"phase_step_seconds": 8.0})
    ok = await step.run(manifest, _ctx(tmp_path))
    assert ok is True

    # Detector got the loaded polygon + configured step cadence.
    assert captured["step"] == 8.0
    assert captured["polygon"] == [[0.0, 0.0], [100.0, 0.0], [100.0, 50.0], [0.0, 50.0]]

    phases_path = tmp_path / "phases.json"
    assert manifest.get("phases_path") == str(phases_path)
    payload = json.loads(phases_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["source"] == "phase_fused"
    assert payload["times"] == {
        "kickoff": 120.0,
        "halftime": 1500.0,
        "second_half": 2100.0,
        "end": 3600.0,
    }

    # Persisted to the group state for the later TTT push (S2).
    from video_grouper.models import DirectoryState

    stored = DirectoryState(str(tmp_path)).get_game_phases()
    assert stored is not None
    assert stored["source"] == "phase_fused"
    assert stored["ok"] is True
    assert stored["times"]["kickoff"] == 120.0


@pytest.mark.asyncio
async def test_phase_detect_records_rejected_fit(tmp_path, monkeypatch):
    """A sanity-gate-rejected fit (ok=False) is still recorded — the output
    artifact always exists so the runner's non-empty contract holds."""
    monkeypatch.setattr(
        "video_grouper.pipeline.steps.phase_detect.detect_phases",
        lambda *a, **k: {
            "ok": False,
            "times": {
                "kickoff": 10.0,
                "halftime": 50.0,
                "second_half": 60.0,
                "end": 70.0,
            },
            "reasons": ["asym=5.0"],
            "used": "player",
        },
    )
    manifest = _manifest_with_polygon(tmp_path)
    step = create_step("phase_detect", {})
    ok = await step.run(manifest, _ctx(tmp_path))
    assert ok is True

    payload = json.loads((tmp_path / "phases.json").read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert payload["reasons"] == ["asym=5.0"]


@pytest.mark.asyncio
async def test_phase_detect_no_play_writes_empty_artifact(tmp_path, monkeypatch):
    """detect_phases returning None (no play) still writes a non-empty output."""
    monkeypatch.setattr(
        "video_grouper.pipeline.steps.phase_detect.detect_phases",
        lambda *a, **k: None,
    )
    manifest = _manifest_with_polygon(tmp_path)
    step = create_step("phase_detect", {})
    ok = await step.run(manifest, _ctx(tmp_path))
    assert ok is True

    payload = json.loads((tmp_path / "phases.json").read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert payload["times"] == {}
    assert payload["reasons"] == ["no_play"]
