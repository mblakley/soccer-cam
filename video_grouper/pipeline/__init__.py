"""Pipeline step registry.

Each step type registers itself by calling :func:`register_step` at import
time; the runner creates instances via :func:`create_step`. Mirrors the
``video_grouper.cameras`` / old ``ball_tracking`` registries — entries store
``(module_path, class_name)`` tuples (resolved at call time) rather than direct
class references, so test mocks that patch the class attributes are respected
and so importing this module never forces the heavy step modules to load.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from video_grouper.pipeline.base import PipelineStep


# name -> (step_module, step_class, cfg_module, cfg_class, runtime, requires, resources)
_STEP_REGISTRY: dict[str, tuple[str, str, str, str, str, tuple, tuple]] = {}


@dataclass(frozen=True)
class StepMeta:
    """Metadata about a registered step, resolved for the running bundle.

    ``available`` is ``False`` when any module in ``requires`` can't be
    imported in this bundle (e.g. an ONNX step in the tray bundle) — the
    orchestrator uses it to refuse to *run* such a step with a clear error, and
    the config UI uses it to grey the step out.
    """

    name: str
    runtime: str
    requires: tuple[str, ...]
    resources: tuple[str, ...]
    config_class: type[BaseModel]
    available: bool


def register_step(
    name: str,
    step_class: type[PipelineStep],
    config_class: type[BaseModel],
    *,
    runtime: str | None = None,
    requires: tuple[str, ...] | None = None,
    resources: tuple[str, ...] | None = None,
) -> None:
    """Register a pipeline-step implementation under *name*.

    ``runtime`` / ``requires`` / ``resources`` default to the values declared
    on *step_class*; pass them explicitly only to override. Registering the
    same *name* twice is allowed (last writer wins) — the loader order
    built-in -> community -> TTT means a later tier can intentionally shadow an
    earlier one.
    """
    _STEP_REGISTRY[name] = (
        step_class.__module__,
        step_class.__name__,
        config_class.__module__,
        config_class.__name__,
        runtime if runtime is not None else getattr(step_class, "runtime", "any"),
        tuple(
            requires if requires is not None else getattr(step_class, "requires", ())
        ),
        tuple(
            resources if resources is not None else getattr(step_class, "resources", ())
        ),
    )


def _entry(name: str) -> tuple[str, str, str, str, str, tuple, tuple]:
    entry = _STEP_REGISTRY.get(name)
    if entry is None:
        available = ", ".join(sorted(_STEP_REGISTRY)) or "(none)"
        raise ValueError(f"Unknown pipeline step: {name!r}. Available: {available}")
    return entry


def get_step_config_class(name: str) -> type[BaseModel]:
    """Return the registered config class for step *name*."""
    _, _, c_module, c_class, _, _, _ = _entry(name)
    return getattr(importlib.import_module(c_module), c_class)


def create_step(name: str, config: BaseModel | dict) -> PipelineStep:
    """Instantiate the step registered under *name*.

    Accepts a validated config model or a raw dict (validated via the step's
    registered ``config_model``). Classes are resolved at call time.

    Raises:
        ValueError: If *name* is not registered.
    """
    s_module, s_class, c_module, c_class, _, _, _ = _entry(name)
    if not isinstance(config, BaseModel):
        cfg_cls = getattr(importlib.import_module(c_module), c_class)
        config = cfg_cls.model_validate(config)
    step_cls = getattr(importlib.import_module(s_module), s_class)
    return step_cls(config=config)


def _module_available(mod: str) -> bool:
    """True if *mod* can be imported in this bundle.

    An already-imported module counts as available — and we must check
    ``sys.modules`` first because ``find_spec`` raises ``ValueError`` for an
    imported module whose ``__spec__`` is ``None`` (opencv's ``cv2`` does
    exactly this). Any other failure (missing module, bad parent package) means
    unavailable.
    """
    if mod in sys.modules:
        return True
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def get_step_meta(name: str) -> StepMeta:
    """Return :class:`StepMeta` for *name*, computing bundle availability now."""
    _, _, c_module, c_class, runtime, requires, resources = _entry(name)
    config_class = getattr(importlib.import_module(c_module), c_class)
    available = all(_module_available(mod) for mod in requires)
    return StepMeta(
        name=name,
        runtime=runtime,
        requires=tuple(requires),
        resources=tuple(resources),
        config_class=config_class,
        available=available,
    )


def list_steps() -> list[str]:
    """Return the names of all registered steps."""
    return list(_STEP_REGISTRY)


__all__ = [
    "StepMeta",
    "register_step",
    "create_step",
    "get_step_config_class",
    "get_step_meta",
    "list_steps",
]
