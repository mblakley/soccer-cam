"""Per-row horizontal dx shift of the right half of a panoramic frame.

Dual-lens cameras (Reolink Duo 3, Dahua/EmpireTech multi-sensor) butt-join two
lens outputs at image-center. At depths away from the firmware's "match depth"
the two views don't register, so players and lines across the seam appear
doubled/offset. A per-row `dx(y)` shift of the right-half columns collapses
the duplicate without needing to stitch from raw lens streams.

This module is consumed by in-house downstream code (ball detection, tracking,
broadcast-perspective render). It is deliberately NOT called during combine or
trim — both remain stream-copy. The profile is stored **per-camera** at
`config.processing.seam_realign_profile_path` (populated by ttt_reporter when
the TTT calibration tool pushes an update). Readers call `load_profile` on
that path — there is no per-recording sidecar.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StitchProfile:
    """Calibration for a specific camera install.

    Fields mirror the JSON on disk 1:1. `dx_anchors` is a list of [y, dx] pairs,
    expressed in source-pixel units — downstream code scales to actual frame
    dimensions via build_dx_lookup().
    """

    source_width: int
    source_height: int
    seam_x: int
    dx_anchors: list[tuple[int, int]]

    @classmethod
    def from_dict(cls, d: dict) -> "StitchProfile":
        anchors = [(int(a[0]), int(a[1])) for a in d["dx_anchors"]]
        return cls(
            source_width=int(d["source_width"]),
            source_height=int(d["source_height"]),
            seam_x=int(d["seam_x"]),
            dx_anchors=anchors,
        )

    def to_dict(self) -> dict:
        return {
            "source_width": self.source_width,
            "source_height": self.source_height,
            "seam_x": self.seam_x,
            "dx_anchors": [list(a) for a in self.dx_anchors],
        }


def load_profile(path: str | Path) -> Optional[StitchProfile]:
    """Return the parsed profile, or None if the file doesn't exist or is invalid."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        return StitchProfile.from_dict(json.loads(p.read_text()))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.warning(f"Invalid stitch profile at {p}: {e}")
        return None


def write_profile(profile: StitchProfile, path: str | Path) -> None:
    """Write the profile as JSON atomically via rename."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(profile.to_dict(), indent=2))
    tmp.replace(p)


def build_dx_lookup(
    profile: StitchProfile, actual_width: int, actual_height: int
) -> np.ndarray:
    """Return an int32 array of length `actual_height` giving the per-row dx shift.

    If the actual input dimensions differ from the profile's source, scale the
    y anchors and dx values proportionally. `dx>0` moves the right half right;
    `dx<0` moves it left (the typical case, closing firmware overlap).
    """
    y_scale = actual_height / profile.source_height
    x_scale = actual_width / profile.source_width
    anchors_y = np.array([a[0] * y_scale for a in profile.dx_anchors], dtype=np.float32)
    anchors_dx = np.array(
        [a[1] * x_scale for a in profile.dx_anchors], dtype=np.float32
    )
    y_idx = np.arange(actual_height, dtype=np.float32)
    dx = np.interp(y_idx, anchors_y, anchors_dx)
    return np.round(dx).astype(np.int32)


def apply_shift_to_frame_nv12(
    y_plane: np.ndarray, uv_plane: np.ndarray, dx_lookup: np.ndarray, seam_x: int
) -> None:
    """In-place per-row shift of the right half of an NV12 frame.

    `y_plane` shape is (H, W); `uv_plane` is (H/2, W) with interleaved UV.
    Rows where `dx_lookup[y] == 0` are skipped entirely. `seam_x` should come
    from the profile (scaled by the caller if needed).
    """
    h_y = y_plane.shape[0]
    nonzero = np.nonzero(dx_lookup[:h_y])[0]
    for y in nonzero:
        dx = int(dx_lookup[y])
        y_plane[y, seam_x:] = np.roll(y_plane[y, seam_x:], dx)

    # UV plane: one chroma row per two luma rows; dx must stay even so U/V
    # pairs ([U, V, U, V, ...] interleaved) stay aligned.
    h_uv = uv_plane.shape[0]
    # seam_x in UV coords is the same as in Y coords because UV is horizontally
    # interleaved at the luma resolution (only vertically subsampled).
    for y_uv in range(h_uv):
        y_luma = y_uv * 2
        dx_luma = int(dx_lookup[min(y_luma, dx_lookup.size - 1)])
        dx_uv = (dx_luma // 2) * 2
        if dx_uv == 0:
            continue
        uv_plane[y_uv, seam_x:] = np.roll(uv_plane[y_uv, seam_x:], dx_uv)


def apply_shift_to_frame_rgb(
    frame: np.ndarray, dx_lookup: np.ndarray, seam_x: int
) -> np.ndarray:
    """Return a new RGB/BGR frame with the right half shifted per-row.

    Accepts shape (H, W, 3); works for both RGB and BGR.
    """
    out = frame.copy()
    nonzero = np.nonzero(dx_lookup[: frame.shape[0]])[0]
    for y in nonzero:
        dx = int(dx_lookup[y])
        out[y, seam_x:] = np.roll(out[y, seam_x:], dx, axis=0)
    return out
