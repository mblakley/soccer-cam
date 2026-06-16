"""Measurement-prep for the world-model.

Helpers that clean up the raw per-frame candidate set before track-before-detect.

**Fixed-camera static-feature suppression.** On a fixed camera, lines, markings
and other stationary bright features are *background* — the detector fires a peak
at the same place in (nearly) every frame. The game ball is defined by *motion*,
so a peak that is persistent and static across the clip is almost certainly NOT
the ball (cf. the ``static_ball`` class). Suppressing those static candidates
stops the global-MAP trajectory from latching onto a stationary distractor, which
otherwise *wins* the optimisation: a static point has zero acceleration penalty
and a high score every frame, beating the moving, intermittently-detected ball.

This is the candidate-level form of background subtraction (the R3/R8 lever) —
cheap, analytic, and exactly what the EXP-1 diagnostics showed was needed (the
naive TBD locked onto a static bright spot at y~245 across all 705 frames).
"""

from __future__ import annotations

from collections import defaultdict

from training.world_model.tbd import Candidate


def suppress_static_candidates(
    frame_lists: list[list[Candidate]],
    cell_px: float = 40.0,
    occupancy_frac: float = 0.25,
    dilate: bool = True,
) -> tuple[list[list[Candidate]], set[tuple[int, int]]]:
    """Drop candidates in grid cells that hold a peak in too many frames.

    Args:
        frame_lists: per-frame candidate lists (consecutive frames).
        cell_px: grid cell size in source pixels.
        occupancy_frac: a cell is "static background" if it contains a peak in
            more than this fraction of frames. The moving ball never dwells in a
            single small cell that long, so it survives.
        dilate: also suppress the 8 neighbouring cells (covers peak jitter).

    Returns:
        ``(filtered_frame_lists, static_cells)`` — the cleaned candidates and the
        set of static grid cells that were suppressed (for diagnostics).
    """
    n = len(frame_lists)
    if n == 0:
        return [], set()

    occ: dict[tuple[int, int], int] = defaultdict(int)
    for cands in frame_lists:
        for cell in {(int(c.x // cell_px), int(c.y // cell_px)) for c in cands}:
            occ[cell] += 1
    static = {cell for cell, count in occ.items() if count > occupancy_frac * n}

    blocked = set(static)
    if dilate:
        for cx, cy in static:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    blocked.add((cx + dx, cy + dy))

    filtered = [
        [c for c in cands if (int(c.x // cell_px), int(c.y // cell_px)) not in blocked]
        for cands in frame_lists
    ]
    return filtered, static
