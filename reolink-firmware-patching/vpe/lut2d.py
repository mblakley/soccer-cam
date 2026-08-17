"""Read, edit and rebuild the Duo 3's DCE 2-D geometric warp mesh.

The VPE's Distortion Correction Engine warps each sensor image through a
coarse control-point mesh before the stitcher composes the panorama. The mesh
is what actually decides where every source pixel lands, so writing it directly
is the only route that gives an exactly-specified mapping — the firmware's own
`Na_calc_2dlut_data` is an iterative optimiser that reinterprets whatever camera
model you hand it (see docs/FIRMWARE_PATCH_NOTES.md).

Format, as confirmed against a live dump from `/dev/nvt_vpe`
(`VPE_IOC_GET_2DLUT = 0xc008760d`) on firmware v3.0.0.4867_2505072124:

    header      2 or 3 u32 (see `_find_table_offset`) — preserved verbatim
    table       n rows of `stride` u32, stride = align4(n)
    n           mesh dimension, 257 on this unit (`vpe_2dlut_size`)
    stride      260 for n=257; entries n..stride-1 are padding and read zero
    entry       (y << 16) | x, each half unsigned Q14.2

Each entry is the *source* pixel the destination control point samples from,
in quarter-pixel units, so the representable range is 0 .. 16383.75 and every
decodable coordinate is a multiple of 0.25.

On-camera read layout, established by sweeping against the procfs oracle
`get_2dlut_param`: `ioctl(fd, 0xc008760d, buf)` where the argument IS the
buffer, `buf[0]` is the VPE id and **`buf[1]` is the mesh dimension n**.
The driver writes `align4(n)*n` entries from `buf[2]`, so n=0 returns an
empty table *and* reports success.

A production mesh is never generated from scratch: it is the *factory* mesh
with a seam correction composed onto it (`compose_correction`). The factory
mesh is this physical unit's stitch calibration, recovered from `CamStitchPara`
in `mtd11` at every boot; regenerating one from a parametric model throws that
away and cannot be recovered without the vendor's own optimiser.

CLI:
    python lut2d.py info     <lut.bin>
    python lut2d.py dump     <lut.bin> [row]
    python lut2d.py compose  <factory.bin> <anchors.txt> <out.bin>
    python lut2d.py selftest [lut.bin]      # fixture optional; synthetic gates always run
"""

from __future__ import annotations

import math
import struct
import sys
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

# Quarter-pixel fixed point: 14 integer bits, 2 fractional.
FRAC_BITS = 2
FRAC_SCALE = 1 << FRAC_BITS
COORD_MAX = (1 << 16) - 1
COORD_MAX_PX = COORD_MAX / FRAC_SCALE

# Geometry of one warped half on the Duo 3. VIDEOPROC 0 takes 3840x2160 in and
# puts 3840x2160 out; VIDEOPROC 2 (VSP) butt-joins two of those into 7680x2160
# with a 256-px blend window centred on x = 3840. Measured from
# /proc/hdal/vprc/info, 2026-08-17.
DEFAULT_HALF_WIDTH = 3840.0
DEFAULT_HALF_HEIGHT = 2160.0
DEFAULT_PANORAMA_WIDTH = 7680.0

# Nothing physical needs a seam shear beyond this; a larger value in an anchor
# file means the file is corrupt, not that the camera is badly aligned.
MAX_ABS_DX_PX = 64.0
# Below this the mesh folds and the DCE tears the image.
MIN_MONOTONIC_FRACTION = 0.95
# A uniform destination-space shift walks the far edge of the half off the
# sensor; the outermost control columns then clamp. That is cosmetically
# irrelevant (it is the extreme edge of a 180-degree panorama) but it must stay
# rare and must never reach the seam.
MAX_CLAMP_FRACTION = 0.02
# Control columns adjacent to the seam that may never clamp. 32 columns is
# 480 destination px, comfortably covering the left half of the blend window
# ([3712, 3840), 128 px) and its shoulder.
PROTECTED_SEAM_COLS = 32

# Header layouts seen in the wild. The kernel's own ioctl buffer is
# {id, reserved, n} (12 bytes, per vpe_uti_calc_2dlut_ioctl_size); some dumps
# were saved from +4 and so start at {reserved, n}.
HEADER_SIZES = (8, 12)


def align4(n: int) -> int:
    """Row stride in entries — the driver pads each row up to a multiple of 4."""
    return (n + 3) & ~3


class Lut2DError(Exception):
    """Raised when a blob does not match the documented mesh layout."""


class Lut2D:
    """A 2-D warp mesh, held as raw quarter-pixel integers.

    Coordinates are kept in their on-wire integer form so that a decode/encode
    round-trip is byte-identical by construction rather than by luck. Use
    `get`/`set` for pixel floats and `raw`/`set_raw` for the underlying ints.
    """

    def __init__(
        self, n: int, entries: list[int], header: bytes, stride: int | None = None
    ):
        self.n = n
        self.stride = align4(n) if stride is None else stride
        self.header = header
        if len(entries) != self.n * self.stride:
            raise Lut2DError(
                f"expected {self.n * self.stride} entries, got {len(entries)}"
            )
        self.entries = entries

    # -- construction ----------------------------------------------------

    @classmethod
    def from_bytes(cls, blob: bytes) -> Lut2D:
        n, off = _find_table_offset(blob)
        stride = align4(n)
        want = n * stride
        body = blob[off : off + want * 4]
        if len(body) != want * 4:
            raise Lut2DError(
                f"table truncated: need {want * 4} bytes at +{off}, have {len(body)}"
            )
        entries = list(struct.unpack(f"<{want}I", body))
        return cls(n, entries, blob[:off], stride)

    @classmethod
    def from_mapping(
        cls,
        n: int,
        fn: Callable[[float, float], tuple[float, float]],
        header: bytes = b"\x00\x00\x00\x00\x01\x01\x00\x00",
    ) -> Lut2D:
        """Build a mesh by sampling `fn(u, v) -> (src_x, src_y)` on an n x n grid.

        `u` and `v` run 0.0 .. 1.0 across the destination frame. Coordinates are
        quantised to quarter-pixels, so expect up to 0.125 px of rounding error.
        """
        stride = align4(n)
        entries = [0] * (n * stride)
        last = n - 1
        for row in range(n):
            v = row / last
            base = row * stride
            for col in range(n):
                x, y = fn(col / last, v)
                entries[base + col] = _pack(x, y)
        # header carries n; keep the caller's bytes but make n consistent
        hdr = bytearray(header)
        struct.pack_into("<I", hdr, len(hdr) - 4, n)
        return cls(n, entries, bytes(hdr), stride)

    @classmethod
    def identity(cls, n: int, width: float, height: float) -> Lut2D:
        """A pass-through mesh spanning `width` x `height` source pixels."""
        return cls.from_mapping(n, lambda u, v: (u * (width - 1), v * (height - 1)))

    # -- serialisation ---------------------------------------------------

    def to_bytes(self) -> bytes:
        return self.header + struct.pack(f"<{len(self.entries)}I", *self.entries)

    # -- accessors -------------------------------------------------------

    def raw(self, row: int, col: int) -> int:
        return self.entries[row * self.stride + col]

    def set_raw(self, row: int, col: int, value: int) -> None:
        self.entries[row * self.stride + col] = value & 0xFFFFFFFF

    def get(self, row: int, col: int) -> tuple[float, float]:
        return _unpack(self.raw(row, col))

    def set(self, row: int, col: int, x: float, y: float) -> None:
        self.set_raw(row, col, _pack(x, y))

    def padding(self) -> list[int]:
        """Every entry in the padded tail of every row."""
        return [
            self.entries[r * self.stride + c]
            for r in range(self.n)
            for c in range(self.n, self.stride)
        ]

    def bounds(self) -> tuple[float, float, float, float]:
        """(min_x, min_y, max_x, max_y) over the live n x n control points."""
        xs_min = ys_min = float("inf")
        xs_max = ys_max = float("-inf")
        for r in range(self.n):
            base = r * self.stride
            for c in range(self.n):
                x, y = _unpack(self.entries[base + c])
                xs_min = min(xs_min, x)
                xs_max = max(xs_max, x)
                ys_min = min(ys_min, y)
                ys_max = max(ys_max, y)
        return xs_min, ys_min, xs_max, ys_max

    def monotonic_fraction(self) -> tuple[float, float]:
        """Fraction of steps where x increases along rows / y increases down columns.

        A sane warp is close to 1.0 for both. A low value means the mesh folds,
        which the DCE will render as a torn image.
        """
        ok_x = tot_x = ok_y = tot_y = 0
        for r in range(self.n):
            base = r * self.stride
            for c in range(self.n - 1):
                tot_x += 1
                if (
                    _unpack(self.entries[base + c + 1])[0]
                    >= _unpack(self.entries[base + c])[0]
                ):
                    ok_x += 1
        for c in range(self.n):
            for r in range(self.n - 1):
                tot_y += 1
                a = _unpack(self.entries[r * self.stride + c])[1]
                b = _unpack(self.entries[(r + 1) * self.stride + c])[1]
                if b >= a:
                    ok_y += 1
        return ok_x / tot_x, ok_y / tot_y


# -- helpers -------------------------------------------------------------


def quantise(px: float) -> int:
    """Pixels -> quarter-pixel integer, rounding halves away from zero.

    Stated explicitly because the on-camera composer (`lut2d_ioctl compose`)
    must produce output byte-identical to this module, and the two languages
    disagree by default: Python's `round` is banker's rounding, C's `lround` is
    half-away-from-zero. Picking C's rule keeps the C side a one-liner and the
    cross-check in `tests/test_lut2d_compose.py` exact.
    """
    scaled = px * FRAC_SCALE
    return (
        int(math.floor(scaled + 0.5))
        if scaled >= 0
        else -int(math.floor(-scaled + 0.5))
    )


def _pack(x: float, y: float) -> int:
    xi = quantise(x)
    yi = quantise(y)
    if not (0 <= xi <= COORD_MAX) or not (0 <= yi <= COORD_MAX):
        raise Lut2DError(f"({x}, {y}) outside representable range 0..{COORD_MAX_PX}")
    return (yi << 16) | xi


def _unpack(v: int) -> tuple[float, float]:
    return (v & 0xFFFF) / FRAC_SCALE, (v >> 16) / FRAC_SCALE


def _find_table_offset(blob: bytes) -> tuple[int, int]:
    """Locate the table by testing the padding invariant, not by assuming a header.

    Dumps exist with both a 2-word and a 3-word header, so pick whichever offset
    makes every row's padded tail read zero and the size come out exact.
    """
    for off in HEADER_SIZES:
        if len(blob) < off:
            continue
        n = struct.unpack_from("<I", blob, off - 4)[0]
        if not (3 <= n <= 1025):
            continue
        stride = align4(n)
        if len(blob) != off + n * stride * 4:
            continue
        entries = struct.unpack_from(f"<{n * stride}I", blob, off)
        tail = [entries[r * stride + c] for r in range(n) for c in range(n, stride)]
        if any(tail):
            continue
        return n, off
    raise Lut2DError(
        f"no header size in {HEADER_SIZES} explains a {len(blob)}-byte blob "
        "with a zero-padded n x align4(n) table"
    )


# -- seam correction: compose, never generate ----------------------------
#
# THE SIGN CONVENTION, ONCE.
#
# `dx(y)` is the downstream corrector's meaning and it does not change here:
# **the number of pixels the RIGHT half must move to the RIGHT, at row y, to
# register with the left half** (`video_grouper/utils/stitch_remap.py`, which
# does `np.roll(out[y, seam_x:], dx)`).
#
# The mesh warps the LEFT half, so it realises the same *relative* displacement
# with the opposite sense: the left half moves LEFT by dx(y).
#
# Moving rendered content left by d destination px means the destination pixel
# at u must show what used to be at u + d, i.e. M_new(u) = M_factory(u + d),
# and to first order
#
#     M_new.x(u, v) = M.x(u, v) + d * dM.x/du
#
# so the increment is **+d * s**, where s = dM.x/du is the local
# source-px-per-destination-px sampling rate. Two sign flips that cancel: this
# is the step that is easy to get wrong, and getting it wrong doubles the seam
# error instead of closing it.
#
# `s` is NOT 1. Measured on this unit's factory mesh (2026-08-17): 0.617 .. 1.075
# across the mesh, and 0.700 at the seam column. Applying `dx` to the mesh
# directly instead of `dx * s` overshoots at the seam by ~43%.


@dataclass
class ComposeStats:
    """What the composition did, for the artifact's `stages[]` witness."""

    n_points: int = 0
    dx_min_px: float = 0.0
    dx_max_px: float = 0.0
    max_src_disp_px: float = 0.0
    s_min: float = 0.0
    s_max: float = 0.0
    s_at_seam: float = 0.0
    clamped_low: int = 0
    clamped_high: int = 0
    clamped_cols: list[int] = field(default_factory=list)
    monotonic_x: float = 1.0
    monotonic_y: float = 1.0
    max_span_delta_px: float = 0.0
    changed_entries: int = 0
    factory_crc32: int = 0
    result_crc32: int = 0

    @property
    def clamped_fraction(self) -> float:
        total = self.clamped_low + self.clamped_high
        return total / self.n_points if self.n_points else 0.0


def interp_dx(anchors: Sequence[tuple[float, float]], y: float) -> float:
    """Linear interpolation of `[y, dx]` anchors, clamped outside their range.

    Same shape as `numpy.interp`, which is what the downstream corrector uses
    (`stitch_remap.build_dx_lookup`), so one anchor list means one curve on both
    surfaces.
    """
    if not anchors:
        raise Lut2DError("no dx anchors")
    if y <= anchors[0][0]:
        return anchors[0][1]
    if y >= anchors[-1][0]:
        return anchors[-1][1]
    for i in range(1, len(anchors)):
        y1, d1 = anchors[i]
        if y <= y1:
            y0, d0 = anchors[i - 1]
            if y1 == y0:
                return d1
            return d0 + (d1 - d0) * (y - y0) / (y1 - y0)
    return anchors[-1][1]


def scale_anchors(
    anchors: Sequence[tuple[float, float]],
    src_width: float,
    src_height: float,
    dst_width: float = DEFAULT_HALF_WIDTH,
    dst_height: float = DEFAULT_HALF_HEIGHT,
) -> list[tuple[float, float]]:
    """Rescale panorama-space anchors into one half's destination space.

    Anchors are stored in the units of the frame they were measured on -- a
    7680x2160 panorama -- exactly as `StitchProfile` does. The mesh addresses
    one 3840x2160 half, so y scales by `dst_height / src_height` and dx by
    `2 * dst_width / src_width` (a dx is a panorama-width-relative length, and
    the panorama is two halves wide). Both are 1.0 in the shipping geometry;
    the code exists so a profile measured on a downscaled still still applies.
    """
    if src_width <= 0 or src_height <= 0:
        raise Lut2DError(f"bad source geometry {src_width}x{src_height}")
    y_scale = dst_height / src_height
    x_scale = (2.0 * dst_width) / src_width
    return [(y * y_scale, dx * x_scale) for y, dx in anchors]


def compose_correction(
    factory: Lut2D,
    dx_anchors: Sequence[tuple[float, float]],
    *,
    dst_width: float = DEFAULT_HALF_WIDTH,
    dst_height: float = DEFAULT_HALF_HEIGHT,
    max_abs_dx: float = MAX_ABS_DX_PX,
    min_monotonic: float = MIN_MONOTONIC_FRACTION,
    max_clamp_fraction: float = MAX_CLAMP_FRACTION,
    protected_seam_cols: int = PROTECTED_SEAM_COLS,
) -> tuple[Lut2D, ComposeStats]:
    """Return the factory mesh with a per-row seam shear composed onto it.

    `dx_anchors` are `(y, dx)` in this half's destination space -- run
    `scale_anchors` first if they were measured on a differently-sized frame.
    The correction is horizontal only: every entry's y half is copied verbatim
    from the factory mesh, bit for bit, so no amount of composing can drift the
    vertical mapping.

    dx is applied uniformly across destination columns, which is exactly right
    for the physical model: a relative lens roll of angle theta displaces a
    point at (X, Y) by theta*(-Y, +X), so its horizontal part -theta*Y depends
    on the row and not on the column. Only the local *source* increment varies
    with column, through `s`.

    Raises `Lut2DError` rather than warning on every guard -- a composer that
    warns and writes anyway is the one failure this path cannot afford, since
    the result goes straight into a boot hook nobody watches.
    """
    n = factory.n
    if n < 3:
        raise Lut2DError(f"mesh too small to finite-difference: n={n}")
    anchors = [(float(y), float(dx)) for y, dx in dx_anchors]
    if not anchors:
        raise Lut2DError("no dx anchors")
    for i in range(1, len(anchors)):
        if anchors[i][0] <= anchors[i - 1][0]:
            raise Lut2DError(
                f"dx anchors must be strictly increasing in y: "
                f"{anchors[i - 1][0]} then {anchors[i][0]}"
            )
    worst = max(abs(dx) for _, dx in anchors)
    if worst > max_abs_dx:
        raise Lut2DError(
            f"|dx| = {worst:.2f} px exceeds the {max_abs_dx:.0f} px limit; "
            "nothing physical needs that, so treat the anchor file as corrupt"
        )

    du = (dst_width - 1.0) / (n - 1)
    dv = (dst_height - 1.0) / (n - 1)
    st = ComposeStats(n_points=n * n)
    st.dx_min_px = min(dx for _, dx in anchors)
    st.dx_max_px = max(dx for _, dx in anchors)
    st.s_min = float("inf")
    st.s_max = float("-inf")

    out = list(factory.entries)
    clamped_cols: set[int] = set()
    span_headroom = 0.0
    for row in range(n):
        d = interp_dx(anchors, row * dv)
        base = row * factory.stride
        xs = [(factory.entries[base + c] & 0xFFFF) / FRAC_SCALE for c in range(n)]
        span_before = xs[n - 1] - xs[0]
        unclamped_first = unclamped_last = 0.0
        row_s_min, row_s_max = float("inf"), float("-inf")
        for col in range(n):
            if col == 0:
                s = (xs[1] - xs[0]) / du
            elif col == n - 1:
                s = (xs[n - 1] - xs[n - 2]) / du
            else:
                s = (xs[col + 1] - xs[col - 1]) / (2.0 * du)
            row_s_min = min(row_s_min, s)
            row_s_max = max(row_s_max, s)
            if row == n // 2 and col == n - 1:
                st.s_at_seam = s
            disp = d * s
            if abs(disp) > st.max_src_disp_px:
                st.max_src_disp_px = abs(disp)
            nx = xs[col] + disp
            if col == 0:
                unclamped_first = nx
            elif col == n - 1:
                unclamped_last = nx
            if nx < 0.0:
                nx = 0.0
                st.clamped_low += 1
                clamped_cols.add(col)
            elif nx > COORD_MAX_PX:
                nx = COORD_MAX_PX
                st.clamped_high += 1
                clamped_cols.add(col)
            xi = quantise(nx)
            out[base + col] = (factory.entries[base + col] & 0xFFFF0000) | xi
        st.s_min = min(st.s_min, row_s_min)
        st.s_max = max(st.s_max, row_s_max)
        # Measured before clamping: clamping is a separate, separately-gated
        # concern, and letting it inflate the span would make the FOV check
        # fire on the wrong fault.
        span_delta = abs((unclamped_last - unclamped_first) - span_before)
        if span_delta > st.max_span_delta_px:
            st.max_span_delta_px = span_delta
        # A translation in DESTINATION space is not a translation in source
        # space, because `s` varies across the row: the far edge and the seam
        # move by d*s(0) and d*s(n-1), which differ. The row's source span
        # therefore changes by |d| * |s(n-1) - s(0)|, and that is physics, not
        # a bug -- bounding it by the row's full s range is the tightest
        # statement that is true by construction for a correct composition and
        # violated by an implementation that scales instead of shifting.
        span_headroom = max(span_headroom, abs(d) * (row_s_max - row_s_min) + 0.5)

    st.clamped_cols = sorted(clamped_cols)
    result = Lut2D(n, out, factory.header, factory.stride)
    st.monotonic_x, st.monotonic_y = result.monotonic_fraction()
    st.changed_entries = sum(
        1 for a, b in zip(factory.entries, result.entries, strict=True) if a != b
    )
    st.factory_crc32 = crc32(factory)
    st.result_crc32 = crc32(result)

    # -- post-conditions. Each of these is a way the correction could tear the
    #    image or quietly stop being a correction, so each refuses.
    if st.monotonic_x < min_monotonic:
        raise Lut2DError(
            f"composed mesh folds horizontally: rows advance in x for only "
            f"{st.monotonic_x * 100:.1f}% of steps (need {min_monotonic * 100:.0f}%)"
        )
    if st.clamped_fraction > max_clamp_fraction:
        raise Lut2DError(
            f"{st.clamped_low + st.clamped_high} of {st.n_points} control points "
            f"({st.clamped_fraction * 100:.2f}%) fall off the sensor; the shear is "
            f"too large for this mesh's margins (limit {max_clamp_fraction * 100:.0f}%)"
        )
    seam_floor = n - protected_seam_cols
    hit = [c for c in st.clamped_cols if c >= seam_floor]
    if hit:
        raise Lut2DError(
            f"clamping reached the seam: columns {hit[:8]} are within the "
            f"protected band (>= {seam_floor}). A correction that runs out of "
            "sensor AT the seam is not a correction."
        )
    # Field of view: a shear may translate the sampling window, never rescale
    # it. See the per-row derivation of `span_headroom` above.
    if st.max_span_delta_px > span_headroom:
        raise Lut2DError(
            f"composed mesh changed a row's source span by "
            f"{st.max_span_delta_px:.2f} px (bound {span_headroom:.2f}); that is a "
            "scale, not a shear -- the field of view is not preserved"
        )
    return result, st


def crc32(lut: Lut2D) -> int:
    """CRC32 over the table only, header excluded.

    The header carries the VPE id and n, which the reader stamps itself, so a
    dump taken with a different header layout must still compare equal. Used as
    the `baseline` change-detector in `anchors.txt`: the on-camera composer has
    no sha256 and no room to grow one, and the property needed here is
    "did this change", not a security property.
    """
    return zlib.crc32(
        struct.pack(f"<{len(lut.entries)}I", *lut.entries)[: lut.n * lut.stride * 4]
    )


# -- anchors.txt: the artifact projection the camera can parse ------------
#
# The device has no python, no perl and no JSON parser, so the calibration
# artifact is projected to plain text for the boot hook. This is the only file
# routinely written to the camera; the composed mesh itself is never stored.


def format_anchors(
    anchors: Sequence[tuple[float, float]],
    *,
    calibration_id: str = "",
    baseline_crc32: int | None = None,
    src_width: float = DEFAULT_PANORAMA_WIDTH,
    src_height: float = DEFAULT_HALF_HEIGHT,
    seam_x: float = DEFAULT_HALF_WIDTH,
) -> str:
    lines = [f"# seam_calibration/2 {calibration_id}".rstrip()]
    if baseline_crc32 is not None:
        lines.append(f"# baseline_crc32 {baseline_crc32:08x}")
    lines.append(f"# src {src_width:.0f} {src_height:.0f} seam {seam_x:.0f}")
    lines += [f"dx {y:.0f} {dx:.4f}" for y, dx in anchors]
    return "\n".join(lines) + "\n"


def parse_anchors(text: str) -> tuple[list[tuple[float, float]], dict]:
    """Parse `anchors.txt`. Returns (anchors, meta) with meta keys src/seam/crc."""
    anchors: list[tuple[float, float]] = []
    meta: dict = {
        "src_width": DEFAULT_PANORAMA_WIDTH,
        "src_height": DEFAULT_HALF_HEIGHT,
        "seam_x": DEFAULT_HALF_WIDTH,
        "baseline_crc32": None,
        "calibration_id": "",
    }
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            tok = line[1:].split()
            if not tok:
                continue
            if tok[0] == "src" and len(tok) >= 3:
                meta["src_width"] = float(tok[1])
                meta["src_height"] = float(tok[2])
                if len(tok) >= 5 and tok[3] == "seam":
                    meta["seam_x"] = float(tok[4])
            elif tok[0] == "baseline_crc32" and len(tok) >= 2:
                meta["baseline_crc32"] = int(tok[1], 16)
            elif tok[0] == "seam_calibration/2" and len(tok) >= 2:
                meta["calibration_id"] = tok[1]
            continue
        tok = line.split()
        if len(tok) != 3 or tok[0] != "dx":
            raise Lut2DError(f"unparseable anchors line: {raw!r}")
        anchors.append((float(tok[1]), float(tok[2])))
    if not anchors:
        raise Lut2DError("anchors file carries no dx lines")
    return anchors, meta


def compose_from_anchors_file(
    factory: Lut2D,
    text: str,
    *,
    require_baseline: bool = False,
    **kw,
) -> tuple[Lut2D, ComposeStats]:
    """`parse_anchors` + `scale_anchors` + `compose_correction`, as the camera does.

    `require_baseline` is the *interactive* mode: it refuses when the anchors
    were composed against a different factory mesh, which is how a `SetStitch`
    slipped in after the baseline dump gets caught (see `stitch_apply.py`). The
    boot hook deliberately does NOT set it -- see the note in that tool.
    """
    anchors, meta = parse_anchors(text)
    if meta["baseline_crc32"] is not None:
        actual = crc32(factory)
        if actual != meta["baseline_crc32"] and require_baseline:
            raise Lut2DError(
                f"anchors were composed against baseline crc32 "
                f"{meta['baseline_crc32']:08x}, this mesh is {actual:08x}. "
                "Something re-ran the firmware's optimiser (SetStitch?) after "
                "the baseline was taken."
            )
    dst_width = kw.get("dst_width", DEFAULT_HALF_WIDTH)
    dst_height = kw.get("dst_height", DEFAULT_HALF_HEIGHT)
    scaled = scale_anchors(
        anchors, meta["src_width"], meta["src_height"], dst_width, dst_height
    )
    return compose_correction(factory, scaled, **kw)


# -- self-test -----------------------------------------------------------


class _Gates:
    def __init__(self) -> None:
        self.failed = 0
        self.n = 0

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        self.n += 1
        mark = "PASS" if ok else "FAIL"
        if not ok:
            self.failed += 1
        print(f"  [{mark}] G{self.n:<2} {label}{('  — ' + detail) if detail else ''}")


def selftest(fixture: str | None = None) -> int:
    g = _Gates()

    print("synthetic gates (no fixture needed)")
    ident = Lut2D.identity(65, 3840, 2160)
    g.check("identity mesh has n*align4(n) entries", len(ident.entries) == 65 * 68)
    g.check("identity mesh pads with zeros", not any(ident.padding()))
    g.check(
        "identity mesh round-trips byte-identically",
        Lut2D.from_bytes(ident.to_bytes()).to_bytes() == ident.to_bytes(),
    )

    err = 0.0
    for r in range(0, 65, 8):
        for c in range(0, 65, 8):
            x, y = ident.get(r, c)
            err = max(err, abs(x - c / 64 * 3839), abs(y - r / 64 * 2159))
    g.check("identity quantisation <= 0.125 px", err <= 0.125, f"max {err:.3f} px")

    warp = Lut2D.from_mapping(
        33, lambda u, v: (100 + u * 3000, 50 + v * 2000 + 40 * (u - 0.5) ** 2)
    )
    g.check(
        "analytic warp round-trips byte-identically",
        Lut2D.from_bytes(warp.to_bytes()).to_bytes() == warp.to_bytes(),
    )
    mx, my = warp.monotonic_fraction()
    g.check(
        "analytic warp is monotonic", mx == 1.0 and my == 1.0, f"x {mx:.3f} y {my:.3f}"
    )

    before = warp.to_bytes()
    warp.set(5, 7, 123.25, 456.75)
    after = warp.to_bytes()
    diff = sum(1 for a, b in zip(before, after, strict=True) if a != b)
    g.check("editing one point changes at most 4 bytes", diff <= 4, f"{diff} bytes")
    g.check("edited point reads back exactly", warp.get(5, 7) == (123.25, 456.75))

    try:
        Lut2D.from_mapping(9, lambda u, v: (-1.0, 0.0))
    except Lut2DError:
        g.check("out-of-range coordinate is rejected", True)
    else:
        g.check("out-of-range coordinate is rejected", False)

    if not fixture:
        print("\nno fixture given — factory-mesh gates skipped")
        print(f"\n{g.n - g.failed}/{g.n} gates passed")
        return 1 if g.failed else 0

    print(f"\nfactory mesh gates ({fixture})")
    blob = open(fixture, "rb").read()
    lut = Lut2D.from_bytes(blob)

    # Liveness FIRST. An all-zero table satisfies every structural invariant
    # below by construction -- right size, zero padding, byte-identical
    # round-trip, whole quarter-pixels -- so without this the suite blesses an
    # empty read as a valid mesh. That is not hypothetical: the on-camera
    # reader put the mesh dimension in the wrong argument word, the driver
    # returned align4(0)*0 entries with a success code, and selftest reported
    # 17/17 on a file containing nothing.
    live = sum(1 for v in lut.entries if v)
    expect = lut.n * lut.n
    g.check(
        "table is populated, not an empty read",
        live >= expect * 0.99,
        f"{live} non-zero of {expect} control points",
    )
    bx0, by0, bx1, by1 = lut.bounds()
    g.check(
        "mesh spans a real frame, not a single point",
        (bx1 - bx0) > 100 and (by1 - by0) > 100,
        f"span {bx1 - bx0:.1f} x {by1 - by0:.1f} px",
    )

    g.check(
        "header located by padding invariant",
        True,
        f"n={lut.n} header={len(lut.header)}B stride={lut.stride}",
    )
    g.check(
        "size is exactly header + n*stride*4",
        len(blob) == len(lut.header) + lut.n * lut.stride * 4,
    )
    g.check("round-trip is byte-identical", lut.to_bytes() == blob)
    g.check("all padding entries are zero", not any(lut.padding()))

    x0, y0, x1, y1 = lut.bounds()
    g.check(
        "source coordinates inside one 4096x2304 sensor frame",
        0 <= x0 and x1 < 4096 and 0 <= y0 and y1 < 2304,
        f"x {x0:.2f}..{x1:.2f}  y {y0:.2f}..{y1:.2f}",
    )
    mx, my = lut.monotonic_fraction()
    g.check("rows advance in x for >= 95% of steps", mx >= 0.95, f"{mx * 100:.1f}%")
    g.check("columns advance in y for >= 95% of steps", my >= 0.95, f"{my * 100:.1f}%")
    g.check(
        "every coordinate is a whole quarter-pixel",
        all(
            _unpack(v)[0] * 4 % 1 == 0 and _unpack(v)[1] * 4 % 1 == 0
            for v in lut.entries
        ),
    )

    print(f"\n{g.n - g.failed}/{g.n} gates passed")
    return 1 if g.failed else 0


# -- CLI -----------------------------------------------------------------


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "selftest":
        return selftest(argv[2] if len(argv) > 2 else None)
    if cmd == "compose":
        if len(argv) < 5:
            print("usage: lut2d.py compose <factory.bin> <anchors.txt> <out.bin>")
            return 2
        factory = Lut2D.from_bytes(open(argv[2], "rb").read())
        text = open(argv[3]).read()
        try:
            result, st = compose_from_anchors_file(factory, text)
        except Lut2DError as e:
            print(f"REFUSED: {e}")
            return 1
        open(argv[4], "wb").write(result.to_bytes())
        print(f"composed {argv[2]} + {argv[3]} -> {argv[4]}")
        print(f"  dx {st.dx_min_px:+.2f}..{st.dx_max_px:+.2f} px")
        print(f"  s  {st.s_min:.4f}..{st.s_max:.4f}  (seam column {st.s_at_seam:.4f})")
        print(f"  max source displacement {st.max_src_disp_px:.2f} px")
        print(
            f"  clamped {st.clamped_low + st.clamped_high}/{st.n_points} "
            f"({st.clamped_fraction * 100:.2f}%) cols {st.clamped_cols[:8]}"
        )
        print(
            f"  monotonic rows {st.monotonic_x * 100:.1f}% cols {st.monotonic_y * 100:.1f}%"
        )
        print(
            f"  changed {st.changed_entries} entries, span delta {st.max_span_delta_px:.3f} px"
        )
        print(f"  crc32 {st.factory_crc32:08x} -> {st.result_crc32:08x}")
        return 0
    if cmd in ("info", "dump"):
        lut = Lut2D.from_bytes(open(argv[2], "rb").read())
        x0, y0, x1, y1 = lut.bounds()
        mx, my = lut.monotonic_fraction()
        print(f"n={lut.n} stride={lut.stride} header={lut.header.hex(' ')}")
        print(f"source x {x0:.2f}..{x1:.2f}   y {y0:.2f}..{y1:.2f}")
        print(f"monotonic  rows {mx * 100:.1f}%   cols {my * 100:.1f}%")
        if cmd == "dump":
            row = int(argv[3]) if len(argv) > 3 else 0
            for c in range(lut.n):
                x, y = lut.get(row, c)
                print(f"  [{row:3d},{c:3d}] = {x:9.2f}, {y:9.2f}")
        return 0
    print(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
