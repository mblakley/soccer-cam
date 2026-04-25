"""Ball-tracking provider registry.

Each provider type registers itself by calling :func:`register_provider`
at import time. The pipeline creates instances via :func:`create_provider`.

Mirrors the camera registry in ``video_grouper.cameras`` — see that module
for the rationale behind storing ``(module_path, class_name)`` tuples
instead of direct class references.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from video_grouper.ball_tracking.base import BallTrackingProvider


_PROVIDER_REGISTRY: dict[str, tuple[str, str]] = {}


def register_provider(name: str, provider_class: type[BallTrackingProvider]) -> None:
    """Register a ball-tracking provider implementation under *name*.

    Args:
        name: The string that appears in ``config.ini`` as
            ``provider = <name>`` (e.g. ``"autocam_gui"``, ``"homegrown"``).
        provider_class: A concrete subclass of
            :class:`video_grouper.ball_tracking.base.BallTrackingProvider`.
    """
    _PROVIDER_REGISTRY[name] = (provider_class.__module__, provider_class.__name__)


def create_provider(name: str, config: BaseModel) -> BallTrackingProvider:
    """Instantiate the provider registered under *name*.

    Resolves the class from its module at call time so test mocks patching
    the class attribute are respected.

    Raises:
        ValueError: If *name* is not registered.
    """
    entry = _PROVIDER_REGISTRY.get(name)
    if entry is None:
        available = ", ".join(sorted(_PROVIDER_REGISTRY)) or "(none)"
        raise ValueError(
            f"Unknown ball-tracking provider: {name!r}. Available: {available}"
        )
    module_path, class_name = entry
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    return cls(config=config)
