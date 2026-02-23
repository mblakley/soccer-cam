"""Shared utilities for autocam processors."""

import os
from pathlib import Path


def get_autocam_input_output_paths(
    group_dir: Path, output_ext: str = "mp4"
) -> tuple[str, str]:
    """Find the raw video file and determine the autocam output path.

    Searches the group directory (recursively) for a file ending in '-raw.mp4'.

    Args:
        group_dir: Directory containing the video group.
        output_ext: Extension for the output file ('mp4' or 'mkv').

    Returns:
        Tuple of (input_path, output_path).

    Raises:
        FileNotFoundError: If no raw video file is found.
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
