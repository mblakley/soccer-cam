"""Unit tests for the pipeline step registry (video_grouper.pipeline)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from video_grouper.pipeline import (
    create_step,
    get_step_config_class,
    get_step_meta,
    list_steps,
    register_step,
)
from video_grouper.pipeline.base import PipelineStep


class DummyConfig(BaseModel):
    threshold: float = 0.5
    label: str = "x"


class DummyStep(PipelineStep):
    name = "dummy"
    config_model = DummyConfig
    consumes = ("input_path",)
    produces = ("dummy_path",)
    runtime = "service"
    requires = ("os",)  # importable -> available
    resources = ("gpu",)

    async def run(self, manifest, ctx):  # pragma: no cover - not exercised here
        return True


class MissingDepStep(PipelineStep):
    name = "missingdep"
    config_model = DummyConfig
    requires = ("totally_not_a_real_module_xyz",)

    async def run(self, manifest, ctx):  # pragma: no cover
        return True


def test_register_and_create_from_dict():
    register_step(DummyStep.name, DummyStep, DummyConfig)
    assert "dummy" in list_steps()

    step = create_step("dummy", {"threshold": 0.9})
    assert isinstance(step, DummyStep)
    assert isinstance(step.config, DummyConfig)
    assert step.config.threshold == 0.9
    assert step.config.label == "x"  # default filled in


def test_create_from_model_instance_passes_through():
    register_step(DummyStep.name, DummyStep, DummyConfig)
    step = create_step("dummy", DummyConfig(threshold=0.1))
    assert step.config.threshold == 0.1


def test_create_unknown_raises_with_available_list():
    with pytest.raises(ValueError) as exc:
        create_step("does_not_exist", {})
    msg = str(exc.value)
    assert "Unknown pipeline step" in msg
    assert "Available:" in msg


def test_get_step_config_class():
    register_step(DummyStep.name, DummyStep, DummyConfig)
    assert get_step_config_class("dummy") is DummyConfig


def test_meta_reads_class_declarations():
    register_step(DummyStep.name, DummyStep, DummyConfig)
    meta = get_step_meta("dummy")
    assert meta.runtime == "service"
    assert meta.resources == ("gpu",)
    assert meta.requires == ("os",)
    assert meta.config_class is DummyConfig
    assert meta.available is True


def test_meta_available_false_for_missing_dep_but_still_listed():
    register_step(MissingDepStep.name, MissingDepStep, DummyConfig)
    meta = get_step_meta("missingdep")
    assert meta.available is False
    assert "missingdep" in list_steps()


def test_register_kwargs_override_class_declarations():
    register_step(
        "override", DummyStep, DummyConfig, runtime="tray", requires=(), resources=()
    )
    meta = get_step_meta("override")
    assert meta.runtime == "tray"
    assert meta.requires == ()
    assert meta.available is True
