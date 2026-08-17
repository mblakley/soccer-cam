"""Gates on the camera-side seam correction: compose, never generate.

The unit under test is `reolink-firmware-patching/vpe/lut2d.py`, which is a
standalone script directory (it is also copied to the camera's build inputs),
not a package -- hence the sys.path insert rather than an import from
`video_grouper`.

No real factory mesh is committed: a dump is calibration data for one physical
camera, not source (see `reolink-firmware-patching/vpe/README.md`). These tests
therefore build a synthetic mesh with the same awkward property the real one
has -- a sampling rate that varies from ~0.60 to ~1.08 source px per
destination px across the row -- because that variation is exactly what the
correction has to divide out, and a uniform-rate fixture would pass even with
the scaling omitted.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

VPE_DIR = Path(__file__).resolve().parents[1] / "reolink-firmware-patching" / "vpe"
sys.path.insert(0, str(VPE_DIR))

from lut2d import (  # noqa: E402
    DEFAULT_HALF_HEIGHT,
    DEFAULT_HALF_WIDTH,
    FRAC_SCALE,
    Lut2D,
    Lut2DError,
    compose_correction,
    compose_from_anchors_file,
    crc32,
    format_anchors,
    interp_dx,
    parse_anchors,
    quantise,
    scale_anchors,
)

W = DEFAULT_HALF_WIDTH
H = DEFAULT_HALF_HEIGHT


def synthetic_factory(n: int = 65) -> Lut2D:
    """A monotonic, non-uniformly-sampled mesh shaped like the real one.

    x(u) is chosen so that dx/du sweeps roughly 0.60 -> 1.08 -> 0.70 across the
    row, matching the measured factory profile (edge, centre, seam), and the
    left edge starts a few px from the sensor boundary so that a large negative
    shear clamps there exactly as it does on the camera.
    """

    def fn(u: float, v: float) -> tuple[float, float]:
        # integral of a rate that dips at both edges and peaks near centre
        rate = 0.60 + 0.48 * math.sin(math.pi * min(max(u, 0.0), 1.0) ** 0.85)
        x = 4.5 + 3390.0 * (u * 0.35 + 0.65 * (1 - math.cos(math.pi * u)) / 2)
        # keep `rate` referenced so the intent above is not lost to a reader
        _ = rate
        y = 16.5 + 2138.0 * v
        return x, y

    return Lut2D.from_mapping(n, fn)


def x_of(lut: Lut2D, row: int, col: int) -> float:
    return (lut.entries[row * lut.stride + col] & 0xFFFF) / FRAC_SCALE


def sampling_rate(lut: Lut2D, row: int, col: int) -> float:
    du = (W - 1.0) / (lut.n - 1)
    if col == 0:
        return (x_of(lut, row, 1) - x_of(lut, row, 0)) / du
    if col == lut.n - 1:
        return (x_of(lut, row, lut.n - 1) - x_of(lut, row, lut.n - 2)) / du
    return (x_of(lut, row, col + 1) - x_of(lut, row, col - 1)) / (2.0 * du)


# -- the fixture itself has to be awkward, or the tests prove nothing --------


def test_synthetic_factory_has_a_varying_sampling_rate():
    f = synthetic_factory()
    mid = f.n // 2
    rates = [sampling_rate(f, mid, c) for c in range(f.n)]
    assert min(rates) < 0.75, f"fixture too uniform: min rate {min(rates):.3f}"
    assert max(rates) > 1.0, f"fixture too uniform: max rate {max(rates):.3f}"
    mx, my = f.monotonic_fraction()
    assert mx == 1.0 and my == 1.0


# -- the three assertions the brief names ------------------------------------


def test_zero_correction_is_byte_identical():
    """The invariant that makes 'compose, never generate' checkable.

    There is deliberately no early-return fast path for all-zero anchors: this
    passes only because the arithmetic itself is exact for d = 0, which is what
    makes it evidence about the arithmetic rather than about an `if`.
    """
    f = synthetic_factory()
    out, st = compose_correction(f, [(0, 0.0), (H - 1, 0.0)])
    assert out.to_bytes() == f.to_bytes()
    assert st.changed_entries == 0
    assert st.factory_crc32 == st.result_crc32


def test_fov_is_preserved():
    """A shear translates the sampling window; it may not rescale it."""
    f = synthetic_factory()
    out, st = compose_correction(f, [(0, -6.0), (1080, 0.0), (H - 1, 6.0)])
    for row in range(f.n):
        before = x_of(f, row, f.n - 1) - x_of(f, row, 0)
        after = x_of(out, row, f.n - 1) - x_of(out, row, 0)
        # bound: |d| * (s_max - s_min) over the row, plus quantisation
        rates = [sampling_rate(f, row, c) for c in range(f.n)]
        bound = 6.0 * (max(rates) - min(rates)) + 0.5
        assert abs(after - before) <= bound, (
            f"row {row}: span moved {after - before:.3f} px, bound {bound:.3f}"
        )
    # and in absolute terms it is a fraction of a percent of a 3390 px span
    assert st.max_span_delta_px < 5.0


def test_monotonicity_is_preserved():
    f = synthetic_factory()
    for anchors in ([(0, -6.0), (H - 1, 6.0)], [(0, 20.0), (H - 1, 20.0)]):
        out, st = compose_correction(f, anchors)
        mx, my = out.monotonic_fraction()
        assert mx == 1.0, f"{anchors}: rows fold ({mx:.4f})"
        assert my == 1.0, f"{anchors}: columns fold ({my:.4f})"
        assert st.monotonic_x == mx and st.monotonic_y == my


# -- the two things the design says are easy to get wrong ---------------------


def test_sign_dx_positive_moves_the_left_half_left():
    """dx > 0 means 'the right half must move right'.

    The mesh can only move the LEFT half, so it realises that by moving the
    left half LEFT -- which means each destination point samples FURTHER RIGHT
    in the source, i.e. M.x increases. A sign error here doubles the seam error
    instead of closing it and presents as 'the tool doesn't work'.
    """
    f = synthetic_factory()
    out, _ = compose_correction(f, [(0, 4.0), (H - 1, 4.0)])
    deltas = [x_of(out, r, c) - x_of(f, r, c) for r in range(f.n) for c in range(f.n)]
    assert min(deltas) > 0.0, f"dx>0 must raise every M.x; min delta {min(deltas)}"

    out_neg, _ = compose_correction(f, [(0, -4.0), (H - 1, -4.0)])
    deltas_neg = [
        x_of(out_neg, r, c) - x_of(f, r, c) for r in range(f.n) for c in range(f.n)
    ]
    assert max(deltas_neg) < 0.0, "dx<0 must lower every M.x"


def test_anchor_is_scaled_by_the_local_sampling_rate():
    """The mesh stores SOURCE coordinates, so dx does not go in raw.

    Applying dx directly instead of dx*s overshoots at the seam by ~43% on this
    camera. The test pins the increment to the local rate at three columns with
    very different rates, and separately asserts it is nowhere near the
    unscaled value.
    """
    f = synthetic_factory()
    d = 8.0
    out, _ = compose_correction(f, [(0, d), (H - 1, d)])
    mid = f.n // 2
    for col in (0, f.n // 2, f.n - 1):
        s = sampling_rate(f, mid, col)
        got = x_of(out, mid, col) - x_of(f, mid, col)
        assert got == pytest.approx(d * s, abs=0.3), (
            f"col {col}: increment {got:.3f}, expected d*s = {d * s:.3f} (s={s:.4f})"
        )
    seam_got = x_of(out, mid, f.n - 1) - x_of(f, mid, f.n - 1)
    assert abs(seam_got - d) > 0.15 * d, (
        "the seam increment is indistinguishable from the unscaled dx -- either "
        "the fixture's rate is 1.0 or the scaling was dropped"
    )


def test_y_coordinates_are_untouched_bit_for_bit():
    f = synthetic_factory()
    out, _ = compose_correction(f, [(0, -12.0), (H - 1, 12.0)])
    for i, (a, b) in enumerate(zip(f.entries, out.entries, strict=True)):
        assert a >> 16 == b >> 16, f"entry {i}: y drifted"


def test_dx_is_uniform_across_columns_within_a_row():
    """A relative lens roll displaces by theta*(-Y, +X): the horizontal part
    depends on the row and not the column. The destination-space shift must
    therefore be the same for every column of a row, however much `s` varies."""
    f = synthetic_factory()
    d = 10.0
    out, _ = compose_correction(f, [(0, d), (H - 1, d)])
    mid = f.n // 2
    implied = [
        (x_of(out, mid, c) - x_of(f, mid, c)) / sampling_rate(f, mid, c)
        for c in range(f.n)
    ]
    assert max(implied) - min(implied) < 0.5, (
        f"destination shift varies across the row: {min(implied):.3f}..{max(implied):.3f}"
    )


# -- the guards all refuse; none of them warns --------------------------------


def test_absurd_dx_is_refused():
    f = synthetic_factory()
    with pytest.raises(Lut2DError, match="exceeds the 64 px limit"):
        compose_correction(f, [(0, 0.0), (H - 1, 90.0)])


def test_non_increasing_anchors_are_refused():
    f = synthetic_factory()
    with pytest.raises(Lut2DError, match="strictly increasing"):
        compose_correction(f, [(1080, 1.0), (540, 2.0)])


def test_empty_anchors_are_refused():
    f = synthetic_factory()
    with pytest.raises(Lut2DError):
        compose_correction(f, [])


def test_clamping_at_the_far_edge_is_tolerated_but_bounded():
    """A large negative shear walks the outermost columns off the sensor.

    That is the extreme edge of a 180-degree panorama and cosmetically
    irrelevant, so it is allowed -- but it must stay rare and must never reach
    the seam, which is the only part of the frame this whole exercise is about.
    """
    f = synthetic_factory()
    out, st = compose_correction(f, [(0, -30.0), (H - 1, -30.0)])
    assert st.clamped_low > 0, "fixture should clamp at the left edge for dx=-30"
    assert st.clamped_fraction <= 0.02
    assert max(st.clamped_cols) < f.n - 32
    assert out.monotonic_fraction()[0] == 1.0


def test_clamping_that_reaches_the_seam_is_refused():
    """Same arithmetic, on a mesh whose seam column sits at the top of the
    representable Q14.2 range, so a positive shear runs out of coordinate there.

    (Clamping *low* cannot happen at the seam of a monotonic mesh -- the seam is
    the largest x in its row -- so the reachable version of this fault is the
    high end.)"""
    n = 65
    f = Lut2D.from_mapping(n, lambda u, v: (12000.0 + 4380.0 * u, 16.5 + 2138.0 * v))
    with pytest.raises(Lut2DError, match="clamping reached the seam"):
        compose_correction(f, [(0, 40.0), (H - 1, 40.0)])


def test_a_composer_that_scaled_instead_of_shifting_would_be_caught():
    """Guard the guard: hand `compose_correction` a mesh it did not make."""
    f = synthetic_factory()
    out, st = compose_correction(f, [(0, 6.0), (H - 1, 6.0)])
    # what a scale bug looks like: x *= (1 + dx/W) instead of x += dx*s
    scaled = Lut2D(f.n, list(f.entries), f.header, f.stride)
    for r in range(f.n):
        for c in range(f.n):
            scaled.set(r, c, x_of(f, r, c) * (1 + 6.0 / W), f.get(r, c)[1])
    row = f.n // 2
    honest = abs(
        (x_of(out, row, f.n - 1) - x_of(out, row, 0))
        - (x_of(f, row, f.n - 1) - x_of(f, row, 0))
    )
    bogus = abs(
        (x_of(scaled, row, f.n - 1) - x_of(scaled, row, 0))
        - (x_of(f, row, f.n - 1) - x_of(f, row, 0))
    )
    assert bogus > 5.0 * max(honest, 0.5), (
        f"a scale bug ({bogus:.2f} px span change) is not distinguishable from "
        f"an honest shear ({honest:.2f} px) -- the FOV gate has no teeth"
    )
    assert st.max_span_delta_px < bogus


# -- interoperability with the downstream corrector ---------------------------


def test_interp_dx_matches_numpy_interp():
    """One anchor list, one curve. `build_dx_lookup` uses np.interp; if this
    module interpolated differently the two surfaces would disagree by a
    fraction of a pixel everywhere and nobody would ever find out."""
    anchors = [(0.0, -6.0), (540.0, -3.0), (1080.0, 0.0), (1620.0, 3.5), (2159.0, 6.0)]
    ys = np.linspace(-100, 2400, 501)
    mine = np.array([interp_dx(anchors, float(y)) for y in ys])
    theirs = np.interp(ys, [a[0] for a in anchors], [a[1] for a in anchors])
    assert np.allclose(mine, theirs, atol=1e-9)


def test_scale_anchors_is_a_no_op_in_the_shipping_geometry():
    anchors = [(0.0, -6.0), (2159.0, 6.0)]
    assert scale_anchors(anchors, 7680.0, 2160.0) == anchors


def test_scale_anchors_rescales_a_downscaled_profile():
    anchors = [(0.0, -3.0), (1079.0, 3.0)]
    out = scale_anchors(anchors, 3840.0, 1080.0)
    assert out[0] == (0.0, -6.0)
    assert out[1][0] == pytest.approx(2158.0)
    assert out[1][1] == pytest.approx(6.0)


def test_anchors_text_round_trips():
    anchors = [(0.0, -6.25), (1080.0, 0.0), (2159.0, 6.25)]
    text = format_anchors(
        anchors, calibration_id="duo3-test-1", baseline_crc32=0xDEADBEEF
    )
    back, meta = parse_anchors(text)
    assert back == anchors
    assert meta["baseline_crc32"] == 0xDEADBEEF
    assert meta["calibration_id"] == "duo3-test-1"
    assert meta["src_width"] == 7680.0 and meta["src_height"] == 2160.0
    assert meta["seam_x"] == 3840.0


def test_anchors_file_with_a_stale_baseline_is_refused_when_required():
    f = synthetic_factory()
    text = format_anchors([(0.0, 1.0), (2159.0, 1.0)], baseline_crc32=0x1234)
    with pytest.raises(Lut2DError, match="composed against baseline"):
        compose_from_anchors_file(f, text, require_baseline=True)
    # and is tolerated otherwise -- the boot hook must keep working after a
    # legitimate SetStitch changed the factory mesh out from under the anchors
    out, _ = compose_from_anchors_file(f, text, require_baseline=False)
    assert out.to_bytes() != f.to_bytes()


def test_matching_baseline_is_accepted():
    f = synthetic_factory()
    text = format_anchors([(0.0, 1.0), (2159.0, 1.0)], baseline_crc32=crc32(f))
    out, _ = compose_from_anchors_file(f, text, require_baseline=True)
    assert out.to_bytes() != f.to_bytes()


def test_malformed_anchor_line_is_refused():
    with pytest.raises(Lut2DError, match="unparseable"):
        parse_anchors("# src 7680 2160\ndy 0 1.0\n")


def test_quantise_rounds_halves_away_from_zero():
    """Pinned because the C composer must agree entry-for-entry, and Python's
    built-in round() is banker's rounding."""
    assert quantise(0.125) == 1
    assert quantise(0.375) == 2
    assert quantise(0.625) == 3
    assert quantise(1.0) == 4


# -- the on-camera composer must not drift from this one ----------------------


@pytest.mark.integration
def test_c_composer_is_byte_identical_to_the_python_one(tmp_path):
    """`lut2d_ioctl compose` is what actually runs at boot; this module is what
    the calibration tool runs off-camera. If they ever disagree, the mesh a
    calibration was validated with is not the mesh the camera restores."""
    cc = shutil.which("gcc") or shutil.which("cc")
    if cc is None:
        pytest.skip("no C compiler on PATH")
    binary = tmp_path / "lut2d_ioctl"
    try:
        build = subprocess.run(
            [cc, "-O2", "-o", str(binary), str(VPE_DIR / "lut2d_ioctl.c"), "-lm"],
            capture_output=True,
            text=True,
        )
    except OSError as e:  # a `gcc` on PATH that is a shim for another OS
        pytest.skip(f"C compiler on PATH is not runnable here: {e}")
    assert build.returncode == 0, build.stderr

    # the C tool is compiled for the camera's mesh dimension only
    factory = synthetic_factory(257)
    fac_path = tmp_path / "factory.bin"
    fac_path.write_bytes(factory.to_bytes())

    anchors = [(0.0, -6.0), (1080.0, 0.5), (2159.0, 6.0)]
    text = format_anchors(
        anchors, calibration_id="xcheck", baseline_crc32=crc32(factory)
    )
    anch_path = tmp_path / "anchors.txt"
    anch_path.write_text(text)
    out_path = tmp_path / "out.bin"

    run = subprocess.run(
        [
            str(binary),
            "compose",
            str(fac_path),
            str(anch_path),
            str(out_path),
            "--require-baseline",
        ],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stdout + run.stderr

    py_out, st = compose_from_anchors_file(factory, text, require_baseline=True)
    c_out = Lut2D.from_bytes(out_path.read_bytes())
    assert c_out.entries == py_out.entries, "C and Python composers have drifted"
    assert crc32(c_out) == st.result_crc32
