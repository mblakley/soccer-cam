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

CLI:
    python lut2d.py info     <lut.bin>
    python lut2d.py dump     <lut.bin> [row]
    python lut2d.py selftest [lut.bin]      # fixture optional; synthetic gates always run
"""

from __future__ import annotations

import struct
import sys
from collections.abc import Callable

# Quarter-pixel fixed point: 14 integer bits, 2 fractional.
FRAC_BITS = 2
FRAC_SCALE = 1 << FRAC_BITS
COORD_MAX = (1 << 16) - 1
COORD_MAX_PX = COORD_MAX / FRAC_SCALE

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


def _pack(x: float, y: float) -> int:
    xi = int(round(x * FRAC_SCALE))
    yi = int(round(y * FRAC_SCALE))
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
