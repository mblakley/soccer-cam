"""Processing-stage registry for the homegrown provider.

Each stage module calls :func:`register_stage` at import time. The
provider iterates ``config.enabled_stages`` and looks each name up here.
"""

from __future__ import annotations

from typing import Any

from .base import ProcessingStage

_STAGE_REGISTRY: dict[str, type[ProcessingStage]] = {}


def register_stage(name: str, stage_class: type[ProcessingStage]) -> None:
    _STAGE_REGISTRY[name] = stage_class


def create_stage(name: str, provider_config: Any) -> ProcessingStage:
    cls = _STAGE_REGISTRY[name]
    return cls(provider_config)


def list_stages() -> list[str]:
    return list(_STAGE_REGISTRY.keys())


__all__ = [
    "ProcessingStage",
    "create_stage",
    "list_stages",
    "register_stage",
]
