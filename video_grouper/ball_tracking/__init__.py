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


# Each entry is (provider_module, provider_class, config_module, config_class).
# Tracking the config class lets BallTrackingTask round-trip its provider config
# through JSON state without hardcoding a name -> class map at the call site.
_PROVIDER_REGISTRY: dict[str, tuple[str, str, str, str]] = {}


def register_provider(
    name: str,
    provider_class: type[BallTrackingProvider],
    config_class: type[BaseModel],
) -> None:
    """Register a ball-tracking provider implementation under *name*.

    Args:
        name: The string that appears in ``config.ini`` as
            ``provider = <name>`` (e.g. ``"autocam_gui"``, ``"homegrown"``).
        provider_class: A concrete subclass of
            :class:`video_grouper.ball_tracking.base.BallTrackingProvider`.
        config_class: The Pydantic model class for this provider's config.
    """
    _PROVIDER_REGISTRY[name] = (
        provider_class.__module__,
        provider_class.__name__,
        config_class.__module__,
        config_class.__name__,
    )


def create_provider(name: str, config: BaseModel | dict) -> BallTrackingProvider:
    """Instantiate the provider registered under *name*.

    Accepts either a Pydantic model instance or a dict (which will be validated
    via the registered config class). Resolves both classes at call time so
    test mocks patching the class attributes are respected.

    Raises:
        ValueError: If *name* is not registered.
    """
    entry = _PROVIDER_REGISTRY.get(name)
    if entry is None:
        available = ", ".join(sorted(_PROVIDER_REGISTRY)) or "(none)"
        raise ValueError(
            f"Unknown ball-tracking provider: {name!r}. Available: {available}"
        )
    p_module_path, p_class_name, c_module_path, c_class_name = entry

    if not isinstance(config, BaseModel):
        cfg_cls = getattr(importlib.import_module(c_module_path), c_class_name)
        config = cfg_cls.model_validate(config)

    provider_cls = getattr(importlib.import_module(p_module_path), p_class_name)
    return provider_cls(config=config)


def get_config_class(name: str) -> type[BaseModel]:
    """Return the registered config class for provider *name*.

    Used by serialization paths that need to materialize a Pydantic config
    from a dict payload outside of :func:`create_provider`.
    """
    entry = _PROVIDER_REGISTRY.get(name)
    if entry is None:
        available = ", ".join(sorted(_PROVIDER_REGISTRY)) or "(none)"
        raise ValueError(
            f"Unknown ball-tracking provider: {name!r}. Available: {available}"
        )
    _, _, c_module_path, c_class_name = entry
    return getattr(importlib.import_module(c_module_path), c_class_name)
