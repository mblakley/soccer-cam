"""Behavior fixtures for the game-phase detector fusion.

Each fixture is a real cached signal set (player curve + whistle blasts + ball restarts) captured
from the training box, paired with the golden phase boundaries that the pre-refactor detector wrote
to <gid>.fit.json. The test proves the extracted ``fuse_phases`` reproduces those boundaries exactly
(within 0.05s) from the cached signals -- a behavior gate for the S0 move into video_grouper."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from video_grouper.inference.phase_detector import fuse_phases

FIX = Path(__file__).parent / "fixtures" / "phase"
GIDS = [
    "heat__2026.06.08_vs_Hilton_Heat_Flaitz_away",
    "heat__2026.06.06_vs_Lakefront_SC_Sullivan_away",
    "heat__2026.05.28_vs_Fairport_away",
]


def _load(gid):
    data = json.loads((FIX / f"{gid}.json").read_text(encoding="utf-8"))
    fx = data["fixture"]
    # rebuild the signals dict exactly as the CLI does before fusing (ts/cnt as np arrays, poly
    # injected from the upright source-px field polygon).
    signals = {
        "ts": np.array(fx["ts"]),
        "cnt": np.array(fx["cnt"], np.float32),
        "dur": fx["dur"],
        "blasts": fx["blasts"],
        "multis": fx["multis"],
        "blast_loud": fx["blast_loud"],
        "ball_ev": fx["ball_ev"],
        "ball_center": fx["ball_center"],
        "sr": fx["sr"],
        "poly": fx["poly"],
    }
    return signals, data["golden_times"]


@pytest.mark.parametrize("gid", GIDS)
def test_fuse_phases_matches_golden(gid):
    signals, golden = _load(gid)
    result = fuse_phases(signals)
    assert result is not None
    times = dict(result["times"])
    times["kickoff"] = max(0.0, times["kickoff"])  # CLI clamps max(0, ko) at output
    for key in ("kickoff", "halftime", "second_half", "end"):
        assert abs(times[key] - golden[key]) <= 0.05, (
            f"{gid} {key}: {times[key]} != golden {golden[key]}"
        )
