"""Tests for [PIPELINE] config load/save round-trip + legacy migration."""

from __future__ import annotations

import textwrap

from video_grouper.pipeline.config import (
    PipelineConfig,
    migrate_ball_tracking_to_pipeline,
)
from video_grouper.utils.config import load_config, save_config

_REQUIRED_SECTIONS = """\
[STORAGE]
path = /data
[RECORDING]
[PROCESSING]
[LOGGING]
[APP]
[TEAMSNAP]
[PLAYMETRICS]
[NTFY]
[YOUTUBE]
"""

_PIPELINE_INI = """\
[PIPELINE]
enabled = true
gpu_concurrency = 2
steps = stitch, detect, track, render

[PIPELINE.stitch]
type = stitch_correct
stitch_profile_path = /calib/flash.json

[PIPELINE.detect]
type = detect
model_path = /m/model.onnx
detect_confidence = 0.5

[PIPELINE.track]
type = track
track_kalman_gate = 300

[PIPELINE.render]
type = render
render_output_width = 1920
"""


def _write(tmp_path, text, name="config.ini"):
    p = tmp_path / name
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return p


def test_load_pipeline_section(tmp_path):
    cfg = load_config(_write(tmp_path, _REQUIRED_SECTIONS + _PIPELINE_INI))
    pc = cfg.pipeline
    assert pc.enabled is True
    assert pc.gpu_concurrency == 2

    ordered = pc.ordered_steps()
    assert [s.step_id for s in ordered] == ["stitch", "detect", "track", "render"]
    assert [s.type for s in ordered] == ["stitch_correct", "detect", "track", "render"]
    assert ordered[0].config["stitch_profile_path"] == "/calib/flash.json"
    assert ordered[1].config["model_path"] == "/m/model.onnx"
    assert ordered[1].config["detect_confidence"] == "0.5"  # raw until create_step


def test_pipeline_round_trips(tmp_path):
    cfg = load_config(_write(tmp_path, _REQUIRED_SECTIONS + _PIPELINE_INI))
    out_path = tmp_path / "saved.ini"
    save_config(cfg, out_path)
    reloaded = load_config(out_path)

    assert reloaded.pipeline.enabled is True
    assert reloaded.pipeline.gpu_concurrency == 2
    assert reloaded.pipeline.steps == ["stitch", "detect", "track", "render"]
    orig = {s.step_id: (s.type, s.config) for s in cfg.pipeline.ordered_steps()}
    back = {s.step_id: (s.type, s.config) for s in reloaded.pipeline.ordered_steps()}
    assert orig == back


def test_missing_pipeline_defaults_to_disabled(tmp_path):
    cfg = load_config(_write(tmp_path, _REQUIRED_SECTIONS))
    assert cfg.pipeline.enabled is False
    assert cfg.pipeline.ordered_steps() == []
    # round-trips a default [PIPELINE] without error
    out_path = tmp_path / "saved.ini"
    save_config(cfg, out_path)
    assert load_config(out_path).pipeline.enabled is False


def test_per_team_round_trips(tmp_path):
    ini = _REQUIRED_SECTIONS + _PIPELINE_INI + "\n[PIPELINE.PER_TEAM]\nflash = stitch\n"
    cfg = load_config(_write(tmp_path, ini))
    assert cfg.pipeline.per_team == {"flash": "stitch"}
    out_path = tmp_path / "saved.ini"
    save_config(cfg, out_path)
    assert load_config(out_path).pipeline.per_team == {"flash": "stitch"}


def test_migrate_autocam():
    out = migrate_ball_tracking_to_pipeline(
        {
            "provider": "autocam_gui",
            "enabled": "true",
            "AUTOCAM_GUI": {"executable": "C:/once/GUI.exe"},
        }
    )
    assert out["steps"] == ["autocam"]
    assert out["step_specs"]["autocam"]["type"] == "autocam"
    assert out["step_specs"]["autocam"]["config"]["executable"] == "C:/once/GUI.exe"
    # migrated dict validates as a PipelineConfig
    pc = PipelineConfig.model_validate(out)
    assert [s.type for s in pc.ordered_steps()] == ["autocam"]


def test_migrate_homegrown_splits_fields_per_step():
    out = migrate_ball_tracking_to_pipeline(
        {
            "provider": "homegrown",
            "enabled": "true",
            "HOMEGROWN": {
                "stages": "stitch_correct, detect, track, render",
                "stitch_profile_path": "/p.json",
                "model_key": "video.ball",
                "detect_confidence": "0.45",
                "track_kalman_gate": "200",
                "render_output_width": "1920",
            },
        }
    )
    assert out["steps"] == ["stitch_correct", "detect", "track", "render"]
    specs = out["step_specs"]
    assert specs["stitch_correct"]["config"] == {"stitch_profile_path": "/p.json"}
    # detect gets only detect fields, not track/render fields
    assert specs["detect"]["config"] == {
        "model_key": "video.ball",
        "detect_confidence": "0.45",
    }
    assert specs["track"]["config"] == {"track_kalman_gate": "200"}
    assert specs["render"]["config"] == {"render_output_width": "1920"}
    pc = PipelineConfig.model_validate(out)
    assert [s.type for s in pc.ordered_steps()] == [
        "stitch_correct",
        "detect",
        "track",
        "render",
    ]


def test_migrate_unknown_provider_returns_none():
    assert migrate_ball_tracking_to_pipeline({"provider": "something_else"}) is None
    assert migrate_ball_tracking_to_pipeline({}) is None
    assert migrate_ball_tracking_to_pipeline(None) is None


_MISSING_TYPE = """\
[PIPELINE]
enabled = true
steps = detect, bad

[PIPELINE.detect]
type = detect
model_path = /m.onnx

[PIPELINE.bad]
model_path = /x.onnx
"""


def test_missing_type_section_skipped_not_fatal(tmp_path):
    # A [PIPELINE.<id>] without `type` must not brick the whole config load.
    cfg = load_config(_write(tmp_path, _REQUIRED_SECTIONS + _MISSING_TYPE))
    assert cfg.storage.path == "/data"  # rest of config still loaded
    assert [s.step_id for s in cfg.pipeline.ordered_steps()] == ["detect"]


_PER_TEAM_AS_STEP = """\
[PIPELINE]
enabled = true
steps = PER_TEAM

[PIPELINE.PER_TEAM]
flash = autocam
"""


def test_per_team_is_reserved_not_a_step(tmp_path):
    cfg = load_config(_write(tmp_path, _REQUIRED_SECTIONS + _PER_TEAM_AS_STEP))
    assert cfg.pipeline.per_team == {"flash": "autocam"}
    assert cfg.pipeline.ordered_steps() == []  # PER_TEAM never becomes a step


_UNDEFINED_STEP = """\
[PIPELINE]
enabled = true
steps = detect, ghost

[PIPELINE.detect]
type = detect
model_path = /m.onnx
"""


def test_undefined_step_id_skipped(tmp_path):
    cfg = load_config(_write(tmp_path, _REQUIRED_SECTIONS + _UNDEFINED_STEP))
    assert [s.step_id for s in cfg.pipeline.ordered_steps()] == ["detect"]


_EMPTY_STEP_CONFIG = """\
[PIPELINE]
enabled = true
steps = track

[PIPELINE.track]
type = track
"""


def test_step_with_no_config_round_trips(tmp_path):
    cfg = load_config(_write(tmp_path, _REQUIRED_SECTIONS + _EMPTY_STEP_CONFIG))
    assert cfg.pipeline.ordered_steps()[0].config == {}
    out_path = tmp_path / "saved.ini"
    save_config(cfg, out_path)
    reloaded = load_config(out_path)
    assert reloaded.pipeline.ordered_steps()[0].type == "track"
    assert reloaded.pipeline.ordered_steps()[0].config == {}


def test_migrate_drops_legacy_per_team_and_carries_disabled():
    out = migrate_ball_tracking_to_pipeline(
        {
            "provider": "autocam_gui",
            "enabled": "false",
            "AUTOCAM_GUI": {"executable": "g.exe"},
            "PER_TEAM": {"flash": "homegrown"},  # stale provider name
        }
    )
    assert "PER_TEAM" not in out
    pc = PipelineConfig.model_validate(out)
    assert pc.enabled is False
    assert pc.per_team == {}


_BALL_TRACKING_LEGACY = """\
[BALL_TRACKING]
enabled = true
provider = autocam_gui

[BALL_TRACKING.AUTOCAM_GUI]
executable = once.exe
"""


def test_ball_tracking_and_pipeline_coexist(tmp_path):
    ini = _REQUIRED_SECTIONS + _BALL_TRACKING_LEGACY + _PIPELINE_INI
    cfg = load_config(_write(tmp_path, ini))
    assert cfg.ball_tracking.provider == "autocam_gui"
    assert cfg.pipeline.enabled is True

    out_path = tmp_path / "saved.ini"
    save_config(cfg, out_path)
    reloaded = load_config(out_path)
    assert reloaded.ball_tracking.provider == "autocam_gui"
    assert [s.type for s in reloaded.pipeline.ordered_steps()] == [
        "stitch_correct",
        "detect",
        "track",
        "render",
    ]
