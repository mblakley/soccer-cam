"""
Camera implementations for video_grouper.

This module provides a registry-based camera system. Each camera type registers
itself by calling ``register_camera()`` at import time, and the pipeline
creates instances via ``create_camera()``.

To add a new camera type, see docs/ADDING_A_CAMERA.md.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from video_grouper.cameras.base import Camera
    from video_grouper.utils.config import CameraConfig

# ---------------------------------------------------------------------------
# Camera registry
#
# Stores (module_path, class_name) instead of a direct class reference so
# that ``unittest.mock.patch`` on the class attribute still works — the
# class is resolved at creation time via ``getattr(module, name)``.
# ---------------------------------------------------------------------------

_CAMERA_REGISTRY: dict[str, tuple[str, str]] = {}


def register_camera(type_name: str, camera_class: type[Camera]) -> None:
    """Register a camera implementation under *type_name*.

    Args:
        type_name: The string that appears in ``config.ini`` as
            ``type = <type_name>`` (e.g. ``"dahua"``, ``"reolink"``).
        camera_class: A concrete subclass of :class:`Camera`.
    """
    _CAMERA_REGISTRY[type_name] = (camera_class.__module__, camera_class.__name__)


def create_camera(cam_config: CameraConfig, storage_path: str) -> Camera:
    """Instantiate the correct camera for *cam_config*.

    Resolves the class from its module at call time so that test mocks
    patching the class attribute are respected.

    Raises:
        ValueError: If the camera type is not registered.
    """
    entry = _CAMERA_REGISTRY.get(cam_config.type)
    if entry is None:
        available = ", ".join(sorted(_CAMERA_REGISTRY)) or "(none)"
        raise ValueError(
            f"Unknown camera type: {cam_config.type!r}. Available types: {available}"
        )
    module_path, class_name = entry
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    return cls(config=cam_config, storage_path=storage_path)
