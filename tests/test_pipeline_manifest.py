"""Unit tests for the pipeline manifest (filesystem state handoff + resume)."""

from __future__ import annotations

import json
import os

from video_grouper.pipeline.manifest import PipelineManifest


def test_load_or_init_fresh(tmp_path):
    m = PipelineManifest.load_or_init(tmp_path, "in.mp4", "out.mp4")
    assert m.get("input_path") == "in.mp4"
    assert m.get("output_path") == "out.mp4"
    assert m.output_path == "out.mp4"
    assert m.data["steps"] == []
    assert m.status_of("ball_detect") is None


def test_put_and_save_roundtrip(tmp_path):
    m = PipelineManifest.load_or_init(tmp_path, "in.mp4", "out.mp4")
    m.put("detections_path", "/abs/det.json")
    m.save()

    path = PipelineManifest.path_for(tmp_path)
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["artifacts"]["detections_path"] == "/abs/det.json"

    reloaded = PipelineManifest.load_or_init(tmp_path, "in.mp4", "out.mp4")
    assert reloaded.get("detections_path") == "/abs/det.json"


def test_mark_running_then_complete_persists(tmp_path):
    m = PipelineManifest.load_or_init(tmp_path, "in.mp4", "out.mp4")
    m.mark_running("ball_detect", "ball_detect", "fp1", "service")
    assert m.status_of("ball_detect") == "running"
    m.mark_complete("ball_detect", {"detections_path": "/abs/det.json"})

    reloaded = PipelineManifest.load_or_init(tmp_path, "in.mp4", "out.mp4")
    assert reloaded.status_of("ball_detect") == "complete"
    assert reloaded.get("detections_path") == "/abs/det.json"
    assert reloaded.produced_paths("ball_detect") == {
        "detections_path": "/abs/det.json"
    }


def test_is_complete_requires_matching_fingerprint(tmp_path):
    m = PipelineManifest.load_or_init(tmp_path, "in.mp4", "out.mp4")
    m.mark_running("ball_detect", "ball_detect", "fp1", "service")
    m.mark_complete("ball_detect", {})
    assert m.is_complete("ball_detect", "fp1") is True
    # config changed -> different fingerprint -> must re-run
    assert m.is_complete("ball_detect", "fp2") is False
    # unknown step is never complete
    assert m.is_complete("track", "fp1") is False


def test_mark_failed_records_error(tmp_path):
    m = PipelineManifest.load_or_init(tmp_path, "in.mp4", "out.mp4")
    m.mark_running("render", "render", "fp", "service")
    m.mark_failed("render", "boom")

    reloaded = PipelineManifest.load_or_init(tmp_path, "in.mp4", "out.mp4")
    rec = reloaded._find("render")
    assert rec is not None
    assert rec["status"] == "failed"
    assert rec["error"] == "boom"


def test_mark_awaiting_tray(tmp_path):
    m = PipelineManifest.load_or_init(tmp_path, "in.mp4", "out.mp4")
    m.mark_awaiting_tray("autocam", "autocam")
    reloaded = PipelineManifest.load_or_init(tmp_path, "in.mp4", "out.mp4")
    assert reloaded.status_of("autocam") == "awaiting_tray"


def test_unknown_version_is_reinitialized(tmp_path):
    path = PipelineManifest.path_for(tmp_path)
    os.makedirs(tmp_path, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 999, "garbage": True}, f)

    m = PipelineManifest.load_or_init(tmp_path, "in.mp4", "out.mp4")
    assert m.data["version"] == 1
    assert m.get("input_path") == "in.mp4"
    assert m.data["steps"] == []
