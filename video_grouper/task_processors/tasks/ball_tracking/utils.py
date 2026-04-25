"""Shared utilities for ball-tracking tasks/processors."""

from __future__ import annotations

import os
from pathlib import Path


def get_ball_tracking_io_paths(
    group_dir: Path, output_ext: str = "mp4"
) -> tuple[str, str]:
    """Find the trimmed source video and the broadcast-output path.

    Searches the group directory (recursively) for a file ending in
    ``-raw.mp4`` (the trimmed-but-unprocessed panoramic source).

    Args:
        group_dir: Directory containing the video group.
        output_ext: Extension for the output file (``mp4`` or ``mkv``).

    Returns:
        Tuple of (input_path, output_path).

    Raises:
        FileNotFoundError: If no ``-raw.mp4`` file is found.
    """
    for root, _, files in os.walk(group_dir):
        for file in files:
            if file.endswith("-raw.mp4"):
                input_path = Path(root) / file
                output_path = input_path.with_name(
                    input_path.name.replace("-raw.mp4", f".{output_ext}")
                )
                return str(input_path), str(output_path)

    raise FileNotFoundError(
        f"No raw video file ending with '-raw.mp4' found in {group_dir}"
    )
