"""
Shared game selection logic for matching recordings to scheduled games.

Used by TeamSnap, PlayMetrics, and mock APIs to consistently select the best
game for a given recording timespan. The algorithm:

1. Proximity guard: reject games whose midpoint is more than 2 hours from the
   recording's midpoint. This prevents assigning a distant game when none was
   actually scheduled near the recording time. If nothing passes, return None
   so the NTFY fallback asks the user.

2. Midpoint heuristic: among candidates, prefer the game whose midpoint falls
   within the recording timespan and is closest to the recording's midpoint.
   Fallback: closest midpoint overall.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Type alias: each candidate is (game_object, game_start_utc, game_end_utc)
GameCandidate = Tuple[Any, datetime, datetime]

MAX_PROXIMITY = timedelta(hours=2)


def select_best_game(
    candidates: List[GameCandidate],
    recording_start: datetime,
    recording_end: datetime,
    game_label_fn=None,
) -> Optional[Any]:
    """
    Select the best game for a recording from a list of candidates.

    Args:
        candidates: List of (game, game_start_utc, game_end_utc) tuples.
            The game object can be any type (dict, TypedDict, etc.) — it is
            returned as-is.
        recording_start: Recording start time (timezone-aware UTC).
        recording_end: Recording end time (timezone-aware UTC).
        game_label_fn: Optional callable(game) -> str for logging. If None,
            uses repr(game).

    Returns:
        The best game object, or None if no candidate passes the proximity guard.
    """
    if not candidates:
        return None

    if game_label_fn is None:
        game_label_fn = repr

    recording_mid = recording_start + (recording_end - recording_start) / 2

    # Apply proximity guard
    nearby = []
    for game, g_start, g_end in candidates:
        g_mid = g_start + (g_end - g_start) / 2
        distance = abs(g_mid - recording_mid)

        if distance <= MAX_PROXIMITY:
            nearby.append((game, g_start, g_end, g_mid))
            logger.info(
                f"Game candidate: {game_label_fn(game)}, midpoint distance: {distance}"
            )
        else:
            logger.debug(
                f"Game rejected by proximity guard: {game_label_fn(game)}, "
                f"midpoint distance: {distance}"
            )

    if not nearby:
        logger.info(
            "No matching game found within 2-hour proximity of recording "
            f"(midpoint: {recording_mid})"
        )
        return None

    if len(nearby) == 1:
        selected = nearby[0][0]
        logger.info(f"Single game candidate: {game_label_fn(selected)}")
        return selected

    # Multiple candidates: prefer midpoint within recording range, closest to recording_mid
    best_game = None
    best_distance = float("inf")

    # First pass: games whose midpoint is within the recording window
    for game, g_start, g_end, g_mid in nearby:
        if recording_start <= g_mid <= recording_end:
            dist = abs((g_mid - recording_mid).total_seconds())
            if dist < best_distance:
                best_distance = dist
                best_game = game

    # Fallback: closest midpoint overall
    if best_game is None:
        for game, g_start, g_end, g_mid in nearby:
            dist = abs((g_mid - recording_mid).total_seconds())
            if dist < best_distance:
                best_distance = dist
                best_game = game

    logger.info(
        f"Selected best game from {len(nearby)} candidates: "
        f"{game_label_fn(best_game) if best_game else 'None'}"
    )
    return best_game
