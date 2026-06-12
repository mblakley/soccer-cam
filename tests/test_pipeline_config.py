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
steps = stitch, ball_detect, track, render

[PIPELINE.stitch]
type = stitch_correct
stitch_profile_path = /calib/flash.json

[PIPELINE.ball_detect]
type = ball_detect
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
    assert [s.step_id for s in ordered] == ["stitch", "ball_detect", "track", "render"]
    assert [s.type for s in ordered] == [
        "stitch_correct",
        "ball_detect",
        "track",
        "render",
    ]
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
    assert reloaded.pipeline.steps == ["stitch", "ball_detect", "track", "render"]
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
    assert out["steps"] == ["stitch_correct", "ball_detect", "track", "render"]
    specs = out["step_specs"]
    assert specs["stitch_correct"]["config"] == {"stitch_profile_path": "/p.json"}
    # detect gets only detect fields, not track/render fields
    assert specs["ball_detect"]["config"] == {
        "model_key": "video.ball",
        "detect_confidence": "0.45",
    }
    assert specs["track"]["config"] == {"track_kalman_gate": "200"}
    assert specs["render"]["config"] == {"render_output_width": "1920"}
    pc = PipelineConfig.model_validate(out)
    assert [s.type for s in pc.ordered_steps()] == [
        "stitch_correct",
        "ball_detect",
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
steps = ball_detect, bad

[PIPELINE.ball_detect]
type = ball_detect
model_path = /m.onnx

[PIPELINE.bad]
model_path = /x.onnx
"""


def test_missing_type_section_skipped_not_fatal(tmp_path):
    # A [PIPELINE.<id>] without `type` must not brick the whole config load.
    cfg = load_config(_write(tmp_path, _REQUIRED_SECTIONS + _MISSING_TYPE))
    assert cfg.storage.path == "/data"  # rest of config still loaded
    assert [s.step_id for s in cfg.pipeline.ordered_steps()] == ["ball_detect"]


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
steps = ball_detect, ghost

[PIPELINE.ball_detect]
type = ball_detect
model_path = /m.onnx
"""


def test_undefined_step_id_skipped(tmp_path):
    cfg = load_config(_write(tmp_path, _REQUIRED_SECTIONS + _UNDEFINED_STEP))
    assert [s.step_id for s in cfg.pipeline.ordered_steps()] == ["ball_detect"]


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


def test_explicit_pipeline_wins_over_legacy_ball_tracking(tmp_path):
    # When BOTH [BALL_TRACKING] and an explicit [PIPELINE] are present, the
    # pipeline wins and the legacy [BALL_TRACKING] section is dropped (it is no
    # longer a Config field). Migration only fires when [PIPELINE] is absent.
    ini = _REQUIRED_SECTIONS + _BALL_TRACKING_LEGACY + _PIPELINE_INI
    cfg = load_config(_write(tmp_path, ini))
    assert not hasattr(cfg, "ball_tracking")
    assert cfg.pipeline.enabled is True

    out_path = tmp_path / "saved.ini"
    save_config(cfg, out_path)
    reloaded = load_config(out_path)
    assert not hasattr(reloaded, "ball_tracking")
    assert [s.type for s in reloaded.pipeline.ordered_steps()] == [
        "stitch_correct",
        "ball_detect",
        "track",
        "render",
    ]


_BALL_TRACKING_HOMEGROWN_ONLY = """\
[BALL_TRACKING]
enabled = true
provider = homegrown

[BALL_TRACKING.HOMEGROWN]
stages = stitch_correct, detect, track, render
stitch_profile_path = /calib/flash.json
detect_confidence = 0.45
track_kalman_gate = 200
render_output_width = 1920
"""


def test_load_migrates_ball_tracking_when_no_pipeline_section(tmp_path):
    # A pre-pipeline install (only [BALL_TRACKING], no [PIPELINE]) must
    # auto-adopt the config-driven pipeline at load time.
    cfg = load_config(
        _write(tmp_path, _REQUIRED_SECTIONS + _BALL_TRACKING_HOMEGROWN_ONLY)
    )
    pc = cfg.pipeline
    assert pc.enabled is True
    assert pc.is_active() is True
    ordered = pc.ordered_steps()
    assert [s.type for s in ordered] == [
        "stitch_correct",
        "ball_detect",
        "track",
        "render",
    ]
    # per-step fields split correctly through migration.
    by_id = {s.step_id: s for s in ordered}
    assert by_id["stitch_correct"].config["stitch_profile_path"] == "/calib/flash.json"
    assert by_id["ball_detect"].config["detect_confidence"] == "0.45"
    # The legacy [BALL_TRACKING] section is no longer a Config field — it is
    # consumed by migration and dropped before model_validate.
    assert not hasattr(cfg, "ball_tracking")


_BALL_TRACKING_AUTOCAM_ONLY = """\
[BALL_TRACKING]
enabled = true
provider = autocam_gui

[BALL_TRACKING.AUTOCAM_GUI]
executable = C:/once/GUI.exe
"""


def test_load_migrates_autocam_gui_when_no_pipeline_section(tmp_path):
    cfg = load_config(
        _write(tmp_path, _REQUIRED_SECTIONS + _BALL_TRACKING_AUTOCAM_ONLY)
    )
    assert cfg.pipeline.is_active() is True
    ordered = cfg.pipeline.ordered_steps()
    assert [s.type for s in ordered] == ["autocam"]
    assert ordered[0].config["executable"] == "C:/once/GUI.exe"


def test_load_does_not_migrate_when_pipeline_section_present(tmp_path):
    # An explicit [PIPELINE] section — even one with steps — must win and the
    # BALL_TRACKING dict must NOT be migrated on top of it.
    ini = _REQUIRED_SECTIONS + _BALL_TRACKING_HOMEGROWN_ONLY + _PIPELINE_INI
    cfg = load_config(_write(tmp_path, ini))
    # PIPELINE steps come from the explicit [PIPELINE.*] sections, in that order.
    assert [s.step_id for s in cfg.pipeline.ordered_steps()] == [
        "stitch",
        "ball_detect",
        "track",
        "render",
    ]


def test_load_no_ball_tracking_no_pipeline_stays_disabled(tmp_path):
    # No [BALL_TRACKING] and no [PIPELINE] -> nothing to migrate.
    cfg = load_config(_write(tmp_path, _REQUIRED_SECTIONS))
    assert cfg.pipeline.is_active() is False
    assert cfg.pipeline.ordered_steps() == []
