"""In-house field-boundary keypoint model — knowledge distillation.

A teacher ONNX model emits a 10-point field-boundary polygon from a
panoramic frame: points 0-4 trace the near sideline left->right (0 and 4
are the near corners) and points 5-9 trace the far boundary right->left.
We run the teacher over our Reolink game footage to auto-generate labels,
then train a student that reproduces the same contract — 10 ``(x, y)``
keypoints plus a per-point score — so the exported ``.onnx`` is a drop-in
replacement for :mod:`video_grouper.inference.field_detector` with no
downstream code changes.

This package holds the standalone label-generation, training, evaluation
and export tooling. The teacher is always referred to generically; no
teacher identity, filename, or path default lives in the repo.
"""

from __future__ import annotations

import re

# Teacher input geometry. Mirrored from
# ``video_grouper.inference.field_detector`` (INPUT_W / INPUT_H) but
# duplicated here so importing this package does not pull in onnxruntime.
INPUT_W = 768
INPUT_H = 384

NUM_KEYPOINTS = 10
# Index ranges within the 10-point polygon.
NEAR_RANGE = (0, 5)  # points 0-4: near sideline, left -> right
FAR_RANGE = (5, 10)  # points 5-9: far boundary, right -> left

# The teacher's own acceptance gate: a frame whose mean per-point score is
# at or above this is one the teacher would have trusted. Used to gate
# coordinate supervision and to measure student/teacher gate agreement.
GATE_THRESHOLD = 0.70

# Bumped whenever the on-disk label JSON schema changes.
LABEL_SCHEMA_VERSION = 1


def slugify(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace/dashes to ``_``.

    ``"Chili Vortex"`` -> ``"chili_vortex"``;
    ``"Grace & Truth (home)"`` -> ``"grace_truth_home"``.
    """
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)  # drop punctuation, parens, &
    text = re.sub(r"[\s_-]+", "_", text)  # whitespace / dashes -> _
    return text.strip("_")


def team_from_name(my_team_name: str | None) -> str | None:
    """Map a match_info ``my_team_name`` to ``"flash"``/``"heat"``.

    Returns ``None`` for anything unrecognized so the caller can flag the
    game for manual classification rather than silently misfiling it.
    """
    if not my_team_name:
        return None
    name = my_team_name.lower()
    if "flash" in name or "ecnl-rl rochester" in name:
        return "flash"
    if "guzzetta" in name or "heat" in name:
        return "heat"
    return None


def make_game_id(team: str, date: str, opponent: str, location: str) -> str:
    """Build a ``{team}__{date}_vs_{opponent}_{location}`` id.

    Follows the repo game-naming convention (see CLAUDE.md). ``date`` is the
    ``YYYY.MM.DD`` prefix; opponent and location are slugified. The location
    here is the venue name (e.g. ``davis_park``), which doubles as the
    field-placement key the dataset splitter clusters on.
    """
    parts = [f"{team}__{date}"]
    if opponent:
        parts.append(f"vs_{slugify(opponent)}")
    if location:
        parts.append(slugify(location))
    return "_".join(parts)
