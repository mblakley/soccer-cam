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
    def from_dict(cls, d: dict) -> StitchProfile:
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


def load_profile(path: str | Path) -> StitchProfile | None:
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


def shift_no_wrap(segment: np.ndarray, dx: int) -> np.ndarray:
    """Return `segment` shifted `dx` samples along axis 0 **without wrapping**.

    `np.roll` is circular: shifting the right half by `dx>0` rolls the far right
    edge of the panorama around into the first `dx` columns — which are exactly
    the seam columns this module exists to repair (and the mirror case at the
    outer edge for `dx<0`). This does the same shift with an edge policy
    instead: samples pushed past the end are dropped, and the vacated columns
    take a copy of the nearest surviving sample (replicate, as in
    `cv2.BORDER_REPLICATE` / `np.pad(mode="edge")`).

    Replicate over "leave a black sliver" because the vacated strip is real
    un-imaged parallax gap of at most a few tens of pixels: replicating extends
    the neighbouring content and stays invisible at that scale, whereas a black
    column is a hard synthetic gradient — a strong edge feature handed to the
    downstream ball detector and field-outline model on the one part of the
    frame we are trying to make *less* misleading.

    Works for 1-D rows (a luma row), (N, 2) interleaved chroma pairs, and
    (N, 3) RGB/BGR rows alike: only axis 0 moves.
    """
    if dx == 0 or segment.shape[0] == 0:
        return segment.copy()
    out = np.empty_like(segment)
    if dx > 0:
        # Shift right: out[i] = segment[i - dx]; the head (the seam) replicates
        # the sample that lands on the new boundary.
        out[dx:] = segment[:-dx]
        out[:dx] = segment[0]
    else:
        # Shift left: out[i] = segment[i + |dx|]; the tail (the outer edge of
        # the panorama) replicates the last surviving sample.
        out[:dx] = segment[-dx:]
        out[dx:] = segment[-1]
    return out


def chroma_aligned_dx(dx: int) -> int:
    """Round `dx` toward zero to an even number of luma pixels.

    4:2:0 chroma is subsampled 2:1 horizontally, so a chroma sample can only be
    moved in whole 2-luma-pixel steps — an odd luma dx is unrepresentable in
    chroma. Applying the exact dx to luma and an even dx to chroma leaves the
    planes disagreeing by up to 1 px, i.e. coloured fringing on exactly the
    vertical edges the seam is made of, so **both** planes use this value.

    Rounding is toward zero, not `dx // 2 * 2`: floor division rounds toward
    negative infinity, so odd dx always lost a pixel *leftward* regardless of
    sign (-35 → -36, +35 → +34) — a directional bias that displaced content one
    way. Toward zero is symmetric under sign flip and never over-shoots the
    calibrated shift; the residual is a ≤1 px under-correction of the shift
    magnitude, against dx values of 10-35 px, and luma/chroma stay in exact
    agreement.
    """
    return (abs(dx) // 2) * 2 * (1 if dx >= 0 else -1)


def apply_shift_to_frame_nv12(
    y_plane: np.ndarray, uv_plane: np.ndarray, dx_lookup: np.ndarray, seam_x: int
) -> None:
    """In-place per-row shift of the right half of an NV12 frame.

    `y_plane` shape is (H, W); `uv_plane` is (H/2, W) with interleaved UV.
    Both planes are shifted by `chroma_aligned_dx(dx)` so luma and chroma stay
    registered; rows whose dx is 0 (or rounds to 0) are skipped entirely.
    `seam_x` should come from the profile (scaled by the caller if needed).
    """
    h_y = y_plane.shape[0]
    nonzero = np.nonzero(dx_lookup[:h_y])[0]
    for y in nonzero:
        dx = chroma_aligned_dx(int(dx_lookup[y]))
        if dx == 0:
            continue
        y_plane[y, seam_x:] = shift_no_wrap(y_plane[y, seam_x:], dx)

    # UV plane: one chroma row per two luma rows.
    h_uv = uv_plane.shape[0]
    # seam_x in UV coords is the same as in Y coords because UV is horizontally
    # interleaved at the luma resolution (only vertically subsampled).
    for y_uv in range(h_uv):
        y_luma = y_uv * 2
        dx = chroma_aligned_dx(int(dx_lookup[min(y_luma, dx_lookup.size - 1)]))
        if dx == 0:
            continue
        row = uv_plane[y_uv, seam_x:]
        n_pairs = int(row.size) // 2
        if n_pairs == 0:
            continue
        # Shift (U, V) pairs, not raw bytes: dx is even so it is a whole number
        # of chroma samples, and replicating a *pair* at the edge keeps U and V
        # distinct (replicating one byte would smear a single component across
        # both and tint the vacated strip).
        pairs = row[: n_pairs * 2].reshape(n_pairs, 2)
        shifted = shift_no_wrap(pairs, dx // 2)
        uv_plane[y_uv, seam_x : seam_x + n_pairs * 2] = shifted.reshape(-1)


def apply_shift_to_frame_rgb(
    frame: np.ndarray, dx_lookup: np.ndarray, seam_x: int
) -> np.ndarray:
    """Return a new RGB/BGR frame with the right half shifted per-row.

    Accepts shape (H, W, 3); works for both RGB and BGR. Every pixel carries its
    own colour here, so unlike NV12 there is no subsampling constraint and the
    exact per-row dx is used.
    """
    out = frame.copy()
    nonzero = np.nonzero(dx_lookup[: frame.shape[0]])[0]
    for y in nonzero:
        dx = int(dx_lookup[y])
        out[y, seam_x:] = shift_no_wrap(out[y, seam_x:], dx)
    return out
