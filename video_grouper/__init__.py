"""
Video Grouper - A tool for managing and processing soccer game recordings from IP cameras.

The package root stays import-light on purpose: leaf modules like
``video_grouper.inference.*`` are numpy/cv2-only and are imported by training
tools on minimal GPU workers, so ``import video_grouper`` must not drag in the
orchestrator app (pydantic, httpx, ...). ``VideoGrouperApp`` is provided
lazily (PEP 562) for compatibility.
"""

from .version import __version__, __version_full__

__all__ = ["VideoGrouperApp", "__version__", "__version_full__"]


def __getattr__(name: str):
    if name == "VideoGrouperApp":
        from .video_grouper_app import VideoGrouperApp

        return VideoGrouperApp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
