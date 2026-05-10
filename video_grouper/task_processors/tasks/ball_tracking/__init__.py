"""Ball-tracking tasks.

Two concrete task types share :class:`BallTrackingTaskBase`:

* :class:`BallTrackingTask` (homegrown) — service-only; depends on PyAV
  / ONNX Runtime / OpenCV for the in-house inference stack.
* :class:`ExternalBallTrackingTask` (autocam_gui) — tray-bundle-safe;
  spawns the Once AutoCam GUI process.

Importing this package eagerly only pulls the base class. Callers that
need a concrete class import it from its own module so the tray bundle
never imports ``av`` transitively.
"""

from .base import BallTrackingTaskBase

__all__ = ["BallTrackingTaskBase"]
