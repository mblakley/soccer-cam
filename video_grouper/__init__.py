"""
Video Grouper - A tool for managing and processing soccer game recordings from IP cameras.
"""

from .video_grouper_app import VideoGrouperApp
from .version import __version__, __version_full__

__all__ = ["VideoGrouperApp", "__version__", "__version_full__"]
