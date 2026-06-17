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
    motion: list[list[Candidate]] | None = None,
    cell_px: float = 40.0,
    occupancy_frac: float = 0.5,
    motion_guard_frac: float = 0.1,
    dilate: bool = True,
) -> tuple[list[list[Candidate]], set[tuple[int, int]]]:
    """Drop candidates in grid cells that hold a peak in most of the clip.

    Args:
        frame_lists: per-frame candidate lists (consecutive frames).
        motion: optional per-frame motion/action blobs (same length). When given,
            a static cell is **protected** (NOT suppressed) if it has motion nearby
            — a background line/marking has no motion next to it, but an in-play
            ball (even a nearly-static one in a deep-corner play) has players moving
            around it (the "action clusters around the ball" prior). This stops a
            static *ball* being deleted as background (EXP-8: clip-1 candidate-recall
            0.82 → 0.93 @R400, no Irondequoit/Fairport regression).
        cell_px: grid cell size in source pixels.
        occupancy_frac: a cell is a "static" candidate only if it contains a peak in
            more than this fraction of the **whole clip** (default 0.5). A brief
            restart ball stays well under this; a whole-clip background line exceeds it.
        motion_guard_frac: with ``motion``, a static cell is suppressed only if its
            3x3 neighbourhood has motion in <= this fraction of frames (no action → background).
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
    candidates = {cell for cell, count in occ.items() if count > occupancy_frac * n}

    if motion is not None:
        mocc: dict[tuple[int, int], int] = defaultdict(int)
        for cands in motion:
            for cell in {(int(c.x // cell_px), int(c.y // cell_px)) for c in cands}:
                mocc[cell] += 1
        static = set()
        for cx, cy in candidates:
            near = sum(
                mocc.get((cx + dx, cy + dy), 0)
                for dx in (-1, 0, 1)
                for dy in (-1, 0, 1)
            )
            if near <= motion_guard_frac * n:  # no action nearby → background, suppress
                static.add((cx, cy))
    else:
        static = candidates

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


def mask_person_candidates(
    frame_lists: list[list[Candidate]],
    person_boxes: list[list[tuple[float, float, float, float]]],
    expand: float = 0.3,
) -> list[list[Candidate]]:
    """Drop appearance candidates that fall inside a detected person box.

    A pretrained person detector (run **offline** at training-data-prep / as a
    cheap per-frame measurement, never a second heavy net at deploy) marks where
    the people are; a ball candidate inside a person box is almost always a
    detector firing on a body / kit, **not** the ball — most importantly the lone
    sideline distractors (an assistant referee, bench, spectators) that out-score
    the dim far ball and steal acquisition (EXP-9/11). Removing them lifts clip-1
    viewport area-recall **0.534 → 0.658 @R400** and moves acquisition off the
    linesman.

    **Caveat (by design, accepted for the viewport goal).** The ball is genuinely
    on a player ~25-35% of in-play frames (chesting / heading / at the feet); this
    mask deletes the ball candidate there too. That is fine for *viewport-area*
    recall — the player IS the action, so the tracker's action prior keeps the
    viewport on the right area — but it is NOT a precise ball locator. Use accurate
    per-frame boxes (EXP-12: a too-small/low-res detector under-detects people and
    the mask goes net-negative). Person info must be **per-frame fresh** — held
    stale boxes mask the wrong place once a person moves (EXP-12).

    Args:
        frame_lists: per-frame appearance candidate lists.
        person_boxes: per-frame person boxes as ``(x0, y0, x1, y1)`` in the same
            (source-pixel) coords as the candidates; same length as ``frame_lists``.
        expand: fractional box expansion (each side) before testing containment —
            0.3 (EXP-11 best) catches candidates at a person's edge / slight box
            slack.

    Returns:
        The filtered per-frame candidate lists (motion/action blobs are kept
        separately and are not masked — they ARE the action prior).
    """
    out: list[list[Candidate]] = []
    for cands, boxes in zip(frame_lists, person_boxes, strict=True):
        exp_boxes = []
        for x0, y0, x1, y1 in (b[:4] for b in boxes):
            w, h = x1 - x0, y1 - y0
            exp_boxes.append(
                (x0 - expand * w, y0 - expand * h, x1 + expand * w, y1 + expand * h)
            )
        out.append(
            [
                c
                for c in cands
                if not any(
                    bx0 <= c.x <= bx1 and by0 <= c.y <= by1
                    for bx0, by0, bx1, by1 in exp_boxes
                )
            ]
        )
    return out
