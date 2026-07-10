"""Tests for built-in pipeline presets (video_grouper.pipeline.presets)."""

from __future__ import annotations

import textwrap

import pytest

# Importing register_steps registers the five built-in step types so we can
# assert each preset's step types are real registered names.
import video_grouper.pipeline.register_steps  # noqa: F401
from video_grouper.pipeline import create_step, list_steps
from video_grouper.pipeline.config import PipelineConfig
from video_grouper.pipeline.presets import (
    PRESETS,
    apply_preset,
    get_preset,
    list_presets,
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


def test_list_presets_matches_registry_keys():
    assert set(list_presets()) == set(PRESETS)
    assert "homegrown" in list_presets()
    assert "autocam" in list_presets()


@pytest.mark.parametrize("name", list_presets())
def test_every_preset_applies_to_valid_pipeline_config(name):
    """apply_preset yields a PipelineConfig whose step types are all registered
    and whose per-step config validates via the step's own config_model."""
    registry = set(list_steps())
    pc = apply_preset(name)
    assert isinstance(pc, PipelineConfig)
    # Seeded pipelines start disabled until the user fills in blanks.
    assert pc.enabled is False
    ordered = pc.ordered_steps()
    assert ordered, f"preset {name!r} produced no ordered steps"
    for spec in ordered:
        assert spec.type in registry, (name, spec.type, registry)
        # Each step config must validate against its registered config_model
        # (create_step raises if a field is wrong-typed / unknown).
        create_step(spec.type, spec.config)


def test_apply_preset_enabled_flag():
    assert apply_preset("homegrown").enabled is False
    assert apply_preset("homegrown", enabled=True).enabled is True


@pytest.mark.parametrize("name", list_presets())
def test_preset_round_trips_through_save_load(tmp_path, name):
    """A preset survives save_config -> load_config: steps order, types, and
    each step's config are preserved."""
    p = tmp_path / "config.ini"
    p.write_text(textwrap.dedent(_REQUIRED_SECTIONS), encoding="utf-8")
    cfg = load_config(p)
    cfg.pipeline = apply_preset(name, enabled=True)
    save_config(cfg, p)

    reloaded = load_config(p)
    orig = cfg.pipeline.ordered_steps()
    back = reloaded.pipeline.ordered_steps()
    assert [s.step_id for s in back] == [s.step_id for s in orig]
    assert [s.type for s in back] == [s.type for s in orig]
    # Config values survive (raw strings after reload — same contract as the
    # rest of the [PIPELINE] layer).
    for o, b in zip(orig, back, strict=False):
        assert {k: str(v) for k, v in o.config.items()} == b.config


def test_homegrown_preset_has_expected_six_steps_in_order():
    pc = apply_preset("homegrown")
    ordered = pc.ordered_steps()
    assert [s.type for s in ordered] == [
        "stitch_correct",
        "field_detect",
        "ball_detect",
        "ball_select",
        "plan_camera",
        "render",
    ]
    assert [s.step_id for s in ordered] == [
        "stitch_correct",
        "field_detect",
        "ball_detect",
        "ball_select",
        "plan_camera",
        "render",
    ]


@pytest.mark.parametrize("step_id", ["ball_detect", "field_detect"])
def test_homegrown_model_steps_leave_model_source_unset(step_id):
    """Model-running steps must NOT seed a model source — the user supplies a
    model_key (TTT login) or model_path (local .onnx)."""
    pc = apply_preset("homegrown")
    cfg = pc.step_specs[step_id].config
    assert "model_key" not in cfg
    assert "model_path" not in cfg
    # but inference tunables are seeded
    assert len(cfg) > 0


def test_autocam_preset_is_single_autocam_step():
    pc = apply_preset("autocam")
    ordered = pc.ordered_steps()
    assert len(ordered) == 1
    assert ordered[0].type == "autocam"
    assert ordered[0].step_id == "autocam"


def test_get_preset_returns_independent_copy():
    """Mutating get_preset's result must not corrupt the shared template."""
    rows = get_preset("homegrown")
    # mutate a returned config dict
    rows[1][2]["model_path"] = "/tmp/mine.onnx"
    fresh = get_preset("homegrown")
    assert "model_path" not in fresh[1][2]


def test_get_preset_unknown_raises():
    with pytest.raises(KeyError):
        get_preset("does-not-exist")


def test_apply_preset_unknown_raises():
    with pytest.raises(KeyError):
        apply_preset("does-not-exist")
