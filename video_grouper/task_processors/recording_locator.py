"""Locate a local recording group dir + its combined.mp4 from a TTT-supplied
``recording_group_dir`` value.

Used by any processor that needs to resolve a TTT-side recording reference
(e.g., a clip request's ``game_session.recording_group_dir``, a highlight reel
game-clip's ``recording_group_dir``) back to a local file path on this
soccer-cam install.

The resolution strategy mirrors what ``ClipRequestProcessor`` has done since
clip requests existed: TTT sends a directory string; try it as an absolute
path first, then fall back to joining it under ``storage_path``. Once the
dir is resolved, ``combined.mp4`` is looked up either directly inside it or
one level of subdirectory deep (the camera-manager's group/subgroup layout).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def resolve_recording_dir(
    storage_path: str, recording_group_dir: str | None
) -> str | None:
    """Resolve a TTT-supplied recording_group_dir to a local absolute path.

    Returns the absolute directory path if it exists locally, else ``None``.
    """
    if not recording_group_dir:
        return None

    if os.path.isabs(recording_group_dir) and os.path.isdir(recording_group_dir):
        return recording_group_dir

    abs_path = os.path.join(storage_path, recording_group_dir)
    if os.path.isdir(abs_path):
        return abs_path

    logger.debug("Recording dir not found: %s", recording_group_dir)
    return None


def find_combined_video(recording_dir: str) -> str | None:
    """Find ``combined.mp4`` in a recording directory tree.

    Looks for ``combined.mp4`` directly inside ``recording_dir``, then one
    level of subdirectory deep. Returns the absolute path or ``None``.
    """
    combined = os.path.join(recording_dir, "combined.mp4")
    if os.path.isfile(combined):
        return combined

    try:
        entries = os.listdir(recording_dir)
    except OSError:
        return None

    for entry in entries:
        subdir = os.path.join(recording_dir, entry)
        if os.path.isdir(subdir):
            combined = os.path.join(subdir, "combined.mp4")
            if os.path.isfile(combined):
                return combined

    return None
