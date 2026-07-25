"""Adapter: world-model track → broadcast renderer ``trajectory.json``.

The broadcast "follow the ball" renderer (`video_grouper/pipeline/steps/render.py`,
`_render_video(input, output, trajectory_path, field_polygon_path, cfg)`) consumes
a ``trajectory.json``: a **per-video-frame** list of ``[x, y]`` (source pixel
coords) or ``null`` for missing frames, indexed by frame number. The renderer
smooths pan/zoom/pitch internally, so we feed the raw per-frame world-model track
directly — no pre-smoothing needed.

This is the handoff point: **world-model → ball coords → renderer** (the
"render with ball coords + polygon" contract). The same `field_polygon.json` the
geometry was built from is passed to the renderer as `field_polygon_path`.

A world-model track point's ``frame_idx`` is the index *within the processed
window*; the renderer indexes by *video* frame, so pass ``frame_offset`` = the
first processed video frame (``lo`` from the dump).
"""

from __future__ import annotations

import json
from typing import Protocol


class _HasPoints(Protocol):
    points: (
        list  # TBDResult or GameBallResult — each point has frame_idx, x, y, detected
    )


def track_to_trajectory(
    result: _HasPoints,
    n_frames: int,
    frame_offset: int = 0,
    include_predicted: bool = True,
) -> list[list[float] | None]:
    """Convert a world-model track to the renderer's per-frame trajectory list.

    Args:
        result: a :class:`TBDResult` / :class:`GameBallResult` (anything with
            ``.points``, each a ``TrackPoint`` with ``frame_idx, x, y, detected``).
        n_frames: total number of frames in the target video (length of the output).
        frame_offset: video frame of the track's index 0 (``lo`` from the dump).
        include_predicted: if True (default) emit the physics-predicted positions
            during occlusion too (smoother follow); if False those become ``null``
            (renderer holds bearing + widens).

    Returns:
        A list of length ``n_frames``; each entry ``[x, y]`` (rounded) or ``None``.
    """
    traj: list[list[float] | None] = [None] * n_frames
    for p in result.points:
        if not include_predicted and not p.detected:
            continue
        idx = frame_offset + p.frame_idx
        if 0 <= idx < n_frames:
            traj[idx] = [round(float(p.x), 2), round(float(p.y), 2)]
    return traj


def save_trajectory_json(
    result: _HasPoints,
    n_frames: int,
    path: str,
    frame_offset: int = 0,
    include_predicted: bool = True,
) -> list[list[float] | None]:
    """Write the renderer ``trajectory.json`` for a world-model track; returns it."""
    traj = track_to_trajectory(result, n_frames, frame_offset, include_predicted)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(traj, f)
    return traj
