"""Tests for CommunityPluginLoader: namespaced load, isolation, error skip.

Community plugins are the user's own unsigned local packages: each is a
sub-directory with an ``__init__.py`` that calls
``video_grouper.pipeline.register_step`` at import time. The loader executes
them so their steps surface in the shared registry, without touching
``sys.path`` and without breaking sibling plugins on one bad import.
"""

import sys

import pytest

from video_grouper.pipeline import list_steps
from video_grouper.plugins.community_loader import CommunityPluginLoader

# A trivial plugin package: a tiny pydantic config + step that registers itself
# under a distinct step name when its ``__init__.py`` is imported.
_GOOD_PLUGIN = """\
from pydantic import BaseModel

from video_grouper.pipeline import register_step
from video_grouper.pipeline.base import PipelineStep


class {cfg}(BaseModel):
    factor: int = 1


class {cls}(PipelineStep):
    name = "{step}"
    config_model = {cfg}

    async def run(self, manifest, ctx):  # pragma: no cover - never invoked
        return True


register_step("{step}", {cls}, {cfg})
"""

_BAD_PLUGIN = 'raise RuntimeError("boom on import")\n'


def _write_plugin(plugins_dir, dir_name, *, init_body):
    pkg = plugins_dir / dir_name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(init_body, encoding="utf-8")
    return pkg


def _write_good(plugins_dir, dir_name, step):
    body = _GOOD_PLUGIN.format(
        cfg=f"Cfg_{step}",
        cls=f"Step_{step}",
        step=step,
    )
    return _write_plugin(plugins_dir, dir_name, init_body=body)


@pytest.fixture
def clean_registry():
    """Snapshot/restore the global step registry and synthetic sys.modules.

    The step registry is process-global; restore it so registrations from one
    test don't leak into another. Also drop any ``vg_community_*`` modules the
    loader stashed in ``sys.modules``.
    """
    from video_grouper import pipeline as pipeline_mod

    saved_registry = dict(pipeline_mod._STEP_REGISTRY)
    saved_modules = {
        k: v for k, v in sys.modules.items() if k.startswith("vg_community_")
    }
    try:
        yield
    finally:
        pipeline_mod._STEP_REGISTRY.clear()
        pipeline_mod._STEP_REGISTRY.update(saved_registry)
        for name in [n for n in sys.modules if n.startswith("vg_community_")]:
            if name not in saved_modules:
                del sys.modules[name]


def test_loads_two_plugins_registers_both(tmp_path, clean_registry):
    plugins_dir = tmp_path / "community"
    _write_good(plugins_dir, "alpha", "community_alpha")
    _write_good(plugins_dir, "beta", "community_beta")

    path_before = list(sys.path)
    loader = CommunityPluginLoader(plugins_dir)
    loader.load_plugins()

    assert "community_alpha" in list_steps()
    assert "community_beta" in list_steps()
    assert set(loader.get_loaded()) == {"alpha", "beta"}
    # The loader must never mutate sys.path (key difference vs the premium loader).
    assert sys.path == path_before


def test_bad_plugin_is_skipped_others_still_load(tmp_path, clean_registry, caplog):
    plugins_dir = tmp_path / "community"
    _write_good(plugins_dir, "good", "community_good")
    _write_plugin(plugins_dir, "broken", init_body=_BAD_PLUGIN)

    loader = CommunityPluginLoader(plugins_dir)
    with caplog.at_level("WARNING"):
        loader.load_plugins()

    # The good plugin still registered and loaded; the broken one was skipped.
    assert "community_good" in list_steps()
    assert set(loader.get_loaded()) == {"good"}
    assert "broken" not in loader.get_loaded()
    assert any("broken" in rec.message for rec in caplog.records)
    # The broken plugin's partial sys.modules entry was cleaned up.
    assert "vg_community_broken" not in sys.modules


def test_synthetic_module_names_do_not_collide(tmp_path, clean_registry):
    plugins_dir = tmp_path / "community"
    _write_good(plugins_dir, "one", "community_one")
    _write_good(plugins_dir, "two", "community_two")

    loader = CommunityPluginLoader(plugins_dir)
    loader.load_plugins()

    # Both get distinct synthetic module names and both survive.
    assert set(loader.get_loaded()) == {"one", "two"}
    assert "vg_community_one" in sys.modules
    assert "vg_community_two" in sys.modules
    assert sys.modules["vg_community_one"] is not sys.modules["vg_community_two"]


def test_non_package_entries_are_ignored(tmp_path, clean_registry):
    plugins_dir = tmp_path / "community"
    plugins_dir.mkdir(parents=True)
    # A directory without __init__.py
    (plugins_dir / "not_a_package").mkdir()
    (plugins_dir / "not_a_package" / "readme.txt").write_text("hi", encoding="utf-8")
    # A loose file at the top level
    (plugins_dir / "loose.py").write_text("x = 1\n", encoding="utf-8")
    # One real plugin alongside the noise
    _write_good(plugins_dir, "real", "community_real")

    loader = CommunityPluginLoader(plugins_dir)
    loader.load_plugins()

    assert set(loader.get_loaded()) == {"real"}
    assert "community_real" in list_steps()


def test_missing_plugins_dir_is_a_noop(tmp_path, clean_registry):
    loader = CommunityPluginLoader(tmp_path / "does_not_exist")
    loader.load_plugins()
    assert loader.get_loaded() == {}


def _write_package(plugins_dir, dir_name, files):
    pkg = plugins_dir / dir_name
    pkg.mkdir(parents=True)
    for fname, content in files.items():
        (pkg / fname).write_text(content, encoding="utf-8")
    return pkg


_SUBMODULE_INIT = """\
from pydantic import BaseModel

from video_grouper.pipeline import register_step
from video_grouper.pipeline.base import PipelineStep

from . import helper


class SubCfg(BaseModel):
    value: int = helper.VALUE


class SubStep(PipelineStep):
    name = "community_sub"
    config_model = SubCfg

    async def run(self, manifest, ctx):  # pragma: no cover
        return True


register_step("community_sub", SubStep, SubCfg)
"""

_BAD_INIT_WITH_SUB = "from . import helper  # noqa: F401\nraise RuntimeError('boom')\n"


def test_submodule_plugin_loads(tmp_path, clean_registry):
    # A real package doing `from . import x` must load under the synthetic name.
    plugins_dir = tmp_path / "community"
    _write_package(
        plugins_dir,
        "withsub",
        {"__init__.py": _SUBMODULE_INIT, "helper.py": "VALUE = 42\n"},
    )
    CommunityPluginLoader(plugins_dir).load_plugins()

    from video_grouper.pipeline import get_step_config_class

    assert "community_sub" in list_steps()
    assert get_step_config_class("community_sub")().value == 42


def test_partial_submodule_leak_is_cleaned(tmp_path, clean_registry):
    # A package that imports a submodule and THEN raises must leave zero
    # vg_community_leaky* entries (package + submodule) in sys.modules.
    plugins_dir = tmp_path / "community"
    _write_package(
        plugins_dir,
        "leaky",
        {"__init__.py": _BAD_INIT_WITH_SUB, "helper.py": "VALUE = 1\n"},
    )
    CommunityPluginLoader(plugins_dir).load_plugins()

    assert [n for n in sys.modules if n.startswith("vg_community_leaky")] == []


def test_last_writer_wins_on_name_collision(tmp_path, clean_registry):
    # Two plugins register the same step name; loaded in sorted dir order, the
    # later one (z_second) wins — the documented last-writer-wins behavior.
    plugins_dir = tmp_path / "community"
    _write_good(plugins_dir, "a_first", "community_dup")
    _write_good(plugins_dir, "z_second", "community_dup")
    CommunityPluginLoader(plugins_dir).load_plugins()

    from video_grouper.pipeline import create_step

    step = create_step("community_dup", {})
    assert type(step).__module__ == "vg_community_z_second"


def test_double_load_is_idempotent(tmp_path, clean_registry):
    plugins_dir = tmp_path / "community"
    _write_good(plugins_dir, "alpha", "community_alpha2")
    loader = CommunityPluginLoader(plugins_dir)
    loader.load_plugins()
    loader.load_plugins()  # second pass re-execs without error
    assert set(loader.get_loaded()) == {"alpha"}
    assert "community_alpha2" in list_steps()
