"""Measurement-prep for the world-model.

Helpers that clean up the raw per-frame candidate set before track-before-detect.

**Fixed-camera static-BACKGROUND suppression.** On a fixed camera, lines,
markings and other stationary bright features are *background* — the detector
fires a peak at the same place in (nearly) **every** frame of the clip. These win
the global-MAP trajectory unfairly: a static point has zero acceleration penalty
and a high score every frame, so the tracker latches onto it (EXP-1: the naive
TBD locked onto a static spot at y~245 across all 705 frames).

**Crucial caveat — the ball CAN be static.** It legitimately sits still at every
restart: kickoff, throw-in, free-kick, goal-kick, corner, PK, injury stoppage.
That is *brief* (it arrives moving, pauses, then leaves moving) — unlike a
background feature, which is static for the *whole clip*. So the discriminator is
**duration of persistence**, not "static = not ball": only suppress a cell that
holds a peak in a *large fraction of the entire clip* (``occupancy_frac`` default
0.5). A restart ball — static for a few seconds out of a long game — stays well
under that and survives. (This is a first cut; a stronger discriminator combines
persistence with size, and with the world-model restart mode that models the ball
arriving-pausing-leaving. On a short eval clip dominated by one restart the
fraction can be high, so tune ``occupancy_frac`` up for short clips.)

The candidate-level form of background subtraction (the R3/R8 lever) — cheap and
analytic.
"""

from __future__ import annotations

from collections import defaultdict

from training.world_model.tbd import Candidate


def suppress_static_candidates(
    frame_lists: list[list[Candidate]],
    cell_px: float = 40.0,
    occupancy_frac: float = 0.5,
    dilate: bool = True,
) -> tuple[list[list[Candidate]], set[tuple[int, int]]]:
    """Drop candidates in grid cells that hold a peak in most of the clip.

    Args:
        frame_lists: per-frame candidate lists (consecutive frames).
        cell_px: grid cell size in source pixels.
        occupancy_frac: a cell is "static background" only if it contains a peak
            in more than this fraction of the **whole clip** (default 0.5). A
            background line/marking is present nearly every frame; a ball sitting
            still at a restart (throw-in, free-kick, ...) is brief and stays well
            under this, so it survives. Tune up for short, restart-dominated clips.
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
