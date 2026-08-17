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
        # round(), not int(): a v2 calibration carries sub-pixel anchors (the
        # camera's warp mesh is quarter-pixel), and int() truncates toward
        # zero. Truncation would bias every non-integer anchor toward the
        # seam by up to a pixel *before* build_dx_lookup does its own
        # rounding -- a silent under-correction on exactly the small objects
        # at the seam this module exists to help. Rounding here matches what
        # build_dx_lookup does downstream, so the two agree.
        anchors = [(round(float(a[0])), round(float(a[1]))) for a in d["dx_anchors"]]
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


SCHEMA_V2 = "seam_calibration/2"

# Who owns the correction. Exactly one surface does; the others contribute
# nothing. See docs/STITCH_CALIBRATION.md 3.1 for why summing them is a trap:
# the camera's mesh is runtime state that dies on reboot, so a split
# correction silently becomes a partial one at the next power cycle and
# nothing in the video says so. A single owner degrades to *no* correction,
# which is detectable.
CORRECTION_OWNERS = frozenset(
    {
        "camera_mesh",
        "camera_scalars",
        "camera_scalars+downstream",
        "downstream",
    }
)

# Only these two author a per-row curve. `camera_scalars` alone cannot express
# a shear at all -- it is three whole-frame integers -- so a curve stored under
# it would be a curve nothing applies.
CURVE_OWNERS = frozenset({"camera_mesh", "camera_scalars+downstream", "downstream"})


class SeamCalibrationError(ValueError):
    """A calibration artifact that must not be written or applied."""


def read_dx_anchors(obj: object) -> list[tuple[float, float]]:
    """Pull the `[y, dx]` curve out of whatever shape the producer emitted.

    Deliberately permissive on the way in and strict on the way out, because
    two independent producers write this curve -- the interactive tool and the
    automatic solver -- and pinning them to one envelope would couple them for
    no benefit. Accepted:

      * a bare list of pairs, ``[[y, dx], ...]``
      * any mapping carrying ``dx_anchors`` (v1 profile, v2 profile, or the
        solver's ``{"dx_anchors": [...], "metadata": {...}}``)

    Values stay floats. The mesh is quarter-pixel and the downstream corrector
    is whole-pixel; rounding is the consumer's business, not the reader's.
    """
    raw: object = obj
    if isinstance(obj, dict):
        if "dx_anchors" not in obj:
            raise SeamCalibrationError("no dx_anchors in calibration payload")
        raw = obj["dx_anchors"]
    if not isinstance(raw, list | tuple) or not raw:
        raise SeamCalibrationError(f"dx_anchors must be a non-empty list, got {raw!r}")

    anchors: list[tuple[float, float]] = []
    for pair in raw:
        if not isinstance(pair, list | tuple) or len(pair) != 2:
            raise SeamCalibrationError(f"anchor must be a [y, dx] pair, got {pair!r}")
        try:
            anchors.append((float(pair[0]), float(pair[1])))
        except (TypeError, ValueError) as exc:
            raise SeamCalibrationError(f"non-numeric anchor {pair!r}") from exc

    if any(b[0] <= a[0] for a, b in zip(anchors, anchors[1:], strict=False)):
        # np.interp (and therefore build_dx_lookup, and therefore the camera's
        # composer) silently produces nonsense for unsorted x. Refuse instead.
        raise SeamCalibrationError(f"anchor rows must strictly increase: {anchors}")
    return anchors


def validate_v2_profile(d: dict) -> None:
    """Raise unless the artifact is internally consistent.

    These are the anti-double-correction rules of STITCH_CALIBRATION.md 3.2,
    and they are refusals rather than warnings on purpose: the one failure
    this format cannot afford is a profile that names the camera as owner
    while also carrying a downstream payload, because then the correction is
    applied twice and the seam ends up worse than uncorrected.

    A legacy v1 profile -- no ``schema``, no ``correction_owner`` -- is not
    checked here at all. It means ``downstream`` and applies as it always has.
    """
    if d.get("schema") != SCHEMA_V2:
        return

    owner = d.get("correction_owner")
    if owner not in CORRECTION_OWNERS:
        raise SeamCalibrationError(
            f"correction_owner {owner!r} is not one of {sorted(CORRECTION_OWNERS)}"
        )

    anchors = read_dx_anchors(d)
    nonzero = any(dx != 0.0 for _y, dx in anchors)

    if owner not in CURVE_OWNERS and nonzero:
        raise SeamCalibrationError(
            f"correction_owner is {owner!r}, which cannot express a per-row "
            "shear, but dx_anchors is non-zero. A curve nothing applies is a "
            "packaging bug, not a calibration."
        )

    stages = d.get("stages") or []
    applied = {
        s.get("surface") for s in stages if s.get("state") in ("applied", "would_set")
    }
    if "downstream" in owner and "camera_mesh" in applied:
        raise SeamCalibrationError(
            "correction_owner names downstream but a camera_mesh stage is "
            "recorded as applied. That is the double-correction, and it is "
            "the one case that must never be a warning."
        )

    if d.get("dy_anchors") is not None and "downstream" in owner:
        # The downstream corrector is horizontal-only by construction. Dropping
        # a field silently is how calibrations become mysterious.
        if "dy_anchors" not in (d.get("dropped") or []):
            raise SeamCalibrationError(
                "dy_anchors is set but the downstream surface cannot apply it; "
                'record the loss explicitly with "dropped": ["dy_anchors"]'
            )


def build_v2_profile(
    anchors: list[tuple[float, float]],
    *,
    correction_owner: str,
    calibration_id: str,
    source_width: int = 7680,
    source_height: int = 2160,
    seam_x: int = 3840,
    blend_w: int = 128,
    scalars: dict | None = None,
    factory_scalars: dict | None = None,
    stages: list[dict] | None = None,
    validation: dict | None = None,
    calibrated_for: dict | None = None,
    provenance: dict | None = None,
) -> dict:
    """Build the on-disk calibration artifact.

    Not a new format wrapping the old one: this **is** a `StitchProfile` JSON
    with optional keys added, and `StitchProfile.from_dict` ignores every one
    of them. A v1 consumer keeps working with no code change, which is the
    whole reason the schema is shaped this way.

    The `sense` block is not decoration. It is the one paragraph that stops a
    sign error, and a sign error here applies the misregistration twice rather
    than removing it.
    """
    if correction_owner not in CORRECTION_OWNERS:
        raise SeamCalibrationError(
            f"correction_owner {correction_owner!r} is not one of "
            f"{sorted(CORRECTION_OWNERS)}"
        )
    half = blend_w
    profile = {
        # v1 core -- read by the shipped downstream corrector, unchanged.
        "source_width": source_width,
        "source_height": source_height,
        "seam_x": seam_x,
        "dx_anchors": [[float(y), round(float(dx), 4)] for y, dx in anchors],
        # v2 additions -- ignored by v1 readers.
        "schema": SCHEMA_V2,
        "calibration_id": calibration_id,
        "correction_owner": correction_owner,
        "sense": {
            "dx_means": (
                "px the RIGHT half must move right, at row y, to register with the left"
            ),
            "downstream_moves": "right_half",
            "camera_mesh_moves": "left_half_with_opposite_sense",
        },
        "geometry": {
            "panorama": [source_width, source_height],
            "half": [source_width // 2, source_height],
            "blend_w": blend_w,
            "blend_window": [seam_x - half, seam_x + half],
            "warped_half": "left",
            "mesh": {"n": 257, "stride": 260, "frac_bits": 2},
        },
        "dy_anchors": None,
        "calibrated_for": calibrated_for
        or {
            "subject_distance_m": None,
            "basis": None,
            "fb_px_m": None,
            "residual_px_at": {},
        },
        "stages": stages
        if stages is not None
        else _default_stages(correction_owner, scalars, factory_scalars),
        "validation": validation or {},
        "provenance": provenance or {},
    }
    validate_v2_profile(profile)
    return profile


def _default_stages(
    owner: str, scalars: dict | None, factory: dict | None
) -> list[dict]:
    """The `stages[]` witness for a calibration that has not been applied yet."""
    stages: list[dict] = []
    if scalars is not None or factory is not None:
        stages.append(
            {
                "surface": "camera_scalars",
                "state": "baseline",
                "values": scalars,
                "factory": factory,
            }
        )
    if owner == "camera_mesh":
        stages.append({"surface": "camera_mesh", "state": "pending", "vpe_id": 0})
        stages.append(
            {
                "surface": "downstream",
                "state": "disabled",
                "reason": "owned by camera_mesh",
            }
        )
    return stages


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
