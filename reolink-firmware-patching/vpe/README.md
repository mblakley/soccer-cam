# VPE 2-D warp mesh

The Duo 3's Video Processing Engine warps each sensor image through a coarse
control-point mesh (the DCE's "2-D LUT") before the stitcher composes the
panorama. The mesh decides where every source pixel lands, so it is the only
place where an *exactly specified* geometric mapping can be imposed.

| file | what |
|---|---|
| `lut2d.py` | decode / edit / rebuild a mesh off-camera; `compose_correction`; `selftest` gates the parser |
| `lut2d_ioctl.c` | read, write and **compose** the live mesh on the camera via `/dev/nvt_vpe` |
| `seam_metric.py` | measure the seam: SCR (px) and detrended \|ln SSR\|, plus the acceptance gate |
| `stitch_apply.py` | apply a calibration in the only correct order (scalars, then mesh) |

Design: `../docs/STITCH_CALIBRATION.md`. Boot hook: `../runtime/stitchcal/S98_StitchCal`,
baked by `../builds/build_stitchcal.sh`.

## Why write the mesh rather than the camera model

The firmware can regenerate a mesh from its stored calibration
(`Na_calc_2dlut_data`), and that path is available — `stitch_para.py` decodes and
rebuilds the calibration blob byte-identically. But `Na_calc_2dlut_data` is an
**iterative optimiser**, not a projection: it searches for a working angle in
0.1-degree steps, applies an angle-dependent shear to both homographies, and
finishes with a power-law x-warp whose barrel term is not Brown-Conrady. Hand it
a modified camera model and you get *a* valid mesh, not the one you asked for.

That is not a guess. A fully free 16-parameter cylindrical+Brown fit to the real
66 049-point mesh stalls at 26.8 px RMS / 133 px max — a free fit that cannot get
below 27 px means the model *family* is wrong. Two parameter-free checks agree:
undistorting the mesh never straightens its rows or columns (best over all 120
coefficient permutations: 87 px, versus 62/102 px for doing nothing), and the
mesh bulges outward horizontally but inward vertically, which no radially
symmetric map can do.

So: the parametric route is fine for small nudges, and writing the mesh directly
is the route that guarantees the mapping.

## Format

Confirmed against a live dump from firmware v3.0.0.4867_2505072124:

```
header   2 or 3 u32   {id, reserved, n} on the wire; some dumps saved from +4
table    n rows x align4(n) u32
n        257 on this unit (vpe_2dlut_size)
stride   260; entries n..stride-1 are padding and read zero
entry    (y << 16) | x, each half unsigned Q14.2
```

Each entry is the **source** pixel that a destination control point samples,
in quarter-pixels, so coordinates run 0 .. 16383.75 and every decodable value is
a multiple of 0.25.

`lut2d.py` does not assume a header size — it locates the table by testing which
offset makes every row's padded tail read zero *and* the total size come out
exact, then preserves the header bytes verbatim so a round-trip is byte-identical
by construction.

## Off-camera use

```
python lut2d.py info     lut_vpe0.bin
python lut2d.py dump     lut_vpe0.bin 128
python lut2d.py selftest lut_vpe0.bin
```

`selftest` runs nine synthetic gates that need no fixture (round-trip,
quantisation error, monotonicity, targeted-edit byte count, range rejection) and
eight more against a real dump (header detection, size, byte-identical
round-trip, zero padding, coordinate bounds, row/column monotonicity,
quarter-pixel exactness). Against the factory mesh from this camera all 17 pass:

```
n=257 header=8B stride=260
source x 4.50..3398.00   y 17.50..2154.75
monotonic  rows 100.0%   cols 100.0%
```

Building a new mesh is a mapping function from normalised destination
coordinates to source pixels:

```python
lut = Lut2D.from_mapping(257, lambda u, v: (u * 3839, v * 2159))
open("flat.bin", "wb").write(lut.to_bytes())
```

Factory dumps live in `F:\archive\duo3_stitch\dumps\` — they are calibration data
for one physical camera, not source, so they are not tracked here.

## Reading the mesh — the argument layout that actually works

```
ioctl(fd, 0xc008760d, buf)     the argument IS the buffer, not a pointer to it
buf[0] = vpe id
buf[1] = n                     <- the mesh dimension
buf[2..] = table, written by the driver
```

`n` goes in **`buf[1]`**. Put it anywhere else and the driver returns
`align4(0)*0` entries **and still reports success** — a structurally perfect,
entirely empty mesh. Every structural gate passes on that file by construction,
which is why `selftest` now checks liveness before anything else.

Established by sweeping calling conventions against the procfs oracle:

```
echo 'r get_2dlut_param 0' > /proc/hdal/vendor/vpe/cmd; cat /proc/hdal/vendor/vpe/cmd
2dlut[0] = 0x  4200A0, x = 40, y = 16
```

Any correct response must contain that word. Confirmed live: 66 049 non-zero of
66 049 control points, `x 4.25..3397.75  y 16.50..2154.75`, 19/19 gates.

## On-camera use

```
lut2d_ioctl get 0 /mnt/sda/lut_vpe0.bin
lut2d_ioctl set 0 /mnt/sda/lut_new.bin --i-have-a-recovery-path
```

**`get` is proven on hardware. `set` is not** — it was written after the camera
went offline and has never run. The write path therefore:

- refuses to run at all without the explicit `--i-have-a-recovery-path` flag;
- checks the mesh structure locally before handing anything to the driver;
- reads the mesh back afterwards and diffs it, because a `SET` that silently
  does nothing is indistinguishable from one that worked unless you check.

Before trusting it, dump the factory mesh, write that same mesh back, and confirm
the read-back is byte-identical and the image is unchanged. Only then write a
mesh you generated.

A bad mesh tears the image; it does not brick the camera. The mesh is runtime
state — the DCE is reprogrammed from stored calibration on every boot, so a power
cycle undoes anything written here.

### Buffers handed to the ioctl need slack

The driver is built around a **three**-word header `{id, reserved, n}` and writes
`align4(n)*n` entries after it. The layout that actually returns live data is the
**two**-word `{id, n}`, so the driver's last write lands past the end of an
exact-size allocation, on glibc chunk metadata. Both faces of this look like
different bugs:

- `get` returns a **correct** mesh and *then* aborts with
  `double free or corruption (out)` — one allocation, damage in the top chunk.
  It reads like the ioctl failed when it did not.
- `set` aborts with `double free or corruption (!prev)` at the first `free()`
  after the read-back — two allocations, so glibc sees the smashed header of the
  second one.

Every ioctl buffer is therefore `BUFSZ + IOCTL_SLACK`. The tool is also
line-buffered, because the abort took the whole buffered log with it and left
`Aborted` as the only evidence.

## Composing a seam correction

**Never generate, always compose.** The factory mesh is this physical unit's
stitch calibration, regenerated from `CamStitchPara` (`mtd11`) at every boot. A
mesh built from a parametric model discards it and cannot be recovered without
the vendor's optimiser, so `compose_correction` (Python) and `lut2d_ioctl
compose` (camera) both take a factory dump as *input* and neither has a mode that
manufactures one.

```
python lut2d.py compose factory.bin anchors.txt out.bin      # off-camera
lut2d_ioctl     compose factory.bin anchors.txt out.bin      # on-camera, no python there
```

The two are byte-identical by construction and `tests/test_lut2d_compose.py`
asserts it, so they cannot drift. (Both are pinned to
round-half-away-from-zero: Python's built-in `round` is banker's rounding and C's
`lround` is not, which would otherwise put them a quarter-pixel apart on ties.)

### The sign, and the scale

`dx(y)` keeps the downstream corrector's meaning: **the pixels the RIGHT half
must move RIGHT, at row y, to register with the left**
(`video_grouper/utils/stitch_remap.py`). The mesh warps the **left** half, so it
realises the same relative displacement with the opposite sense — the left half
moves left by `dx`. Because the mesh stores *source* coordinates, moving rendered
content left by `d` destination px means

```
M_new.x(u, v) = M.x(u, v) + d * s(u, v)
```

where `s = dM.x/du` is the local source-px-per-destination-px rate. **Two sign
flips that cancel: the increment is `+d*s`.**

`s` is not 1. Measured by finite-differencing this unit's factory mesh
(2026-08-17): **0.600 … 1.075 across the mesh, 0.700 at the seam column**.
Applying `dx` raw instead of `dx*s` overshoots at the seam by ~43%.

### Verified on hardware, 2026-08-17

Signed sanity gate, by phase-correlating a snapshot against the factory frame —
a differential measurement, so the indoor scene's huge parallax cancels:

| patch | `dx = +40` | `dx = −40` | restored |
|---|---|---|---|
| LEFT, far from seam (x 700–1500) | **−40.13** | **+40.33** | −0.04 |
| LEFT, near seam (x 2900–3700) | **−40.94** | **+40.34** | −0.01 |
| RIGHT, near seam (x 3990–4790) | −0.02 | −0.01 | +0.00 |
| RIGHT, far (x 6100–6900) | −0.02 | +0.00 | +0.03 |

Four things at once. **VPE 0 warps the panorama's LEFT half** — measured, not
inferred; the right half does not move at all. **The sign is right**: `dx>0`
moves the left half left. **The `s` scaling is right**: the shift is 40.1–40.9 px
for a requested 40, where omitting `s` would have given 40/0.70 ≈ 57 px at the
seam and 40/0.65 ≈ 62 px at the edge. And **restore is exact**.

A realistic ±3 px roll then tracked the expected ramp per row band to within
~0.5 px (rows 200→2050: measured +2.49, +0.85, +0.15, −0.94, −2.27, −2.60
against expected +2.44, +1.33, +0.22, −0.89, −2.00, −2.70), with the right half
still at +0.14 px.

### Gates

`compose` refuses — never warns — on: `|dx| > 64 px`; anchors not strictly
increasing in y; a dead (all-zero) input mesh; horizontal monotonic fraction
below 0.95; more than 2% of control points running off the sensor; **any**
clamping within 32 columns of the seam; and a source-span change beyond
`|d| · (s_max − s_min) + 0.5` per row, which is the tightest bound that is true
for a translation and violated by anything that rescales.

Clamping at the *far* edge is expected and allowed: a uniform destination-space
shift walks the outermost columns off the sensor (this unit's factory mesh starts
at source x = 4.25). At `dx = −40` that is 270 of 66,049 points in columns 0–2 —
the extreme edge of a 180° panorama, cosmetically irrelevant, and nowhere near
the seam.

## Corrections to `../docs/STITCH_CALIBRATION.md`

Implementation proved six things in the design wrong or incomplete. The design's
architecture survived all of them.

1. **`s` at the seam is 0.700, not ~0.68.** Measured directly off the factory
   mesh rather than derived from a magnification figure; the range is 0.600–1.075,
   not 0.63–1.04. The overshoot from ignoring it is ~43%, not ~47%.

2. **A baseline mismatch must NOT be fatal at boot.** §7.3 has `compose` refuse
   when `baseline_sha256` does not match, but §7.1 wants the hook to self-heal
   onto a new factory mesh after a `SetStitch`. Those contradict: making it fatal
   means one legitimate scalar change permanently disables the hook. Resolved by
   making it a *mode* — `--require-baseline` is set on the interactive path
   (`stitch_apply.py`, where a moved baseline means the operator changed
   something mid-flight) and deliberately not at boot. The shear is a property of
   the physical lens pair, not of any one mesh.

3. **CRC32, not sha256.** The device has no sha256 and the property needed is
   "did this change", not a security property. `crc32` covers the table only, so
   dumps saved with different header layouts still compare equal.

4. **The helper binary is baked into the firmware, not left on the SD card.**
   §7.2 puts `bin/lut2d_ioctl` on the card. It is code, not configuration; baking
   it keeps the boot path from depending on a file an operator can delete or a
   card that was reformatted. An SD-card copy still wins if present, so it can be
   iterated on without a reflash.

5. **Detrending is a safeguard, not the load-bearing correction §9.2 claims.**
   The photometric step is ramped in across the 256-px blend window, so its
   per-column gradient is ~step/256. On the live frame raw and detrended differ
   by 0.2% (3.633 vs 3.639) — not "swamps the structural signal". It stays,
   because a *hard* step does pollute the raw figure by ~50%.

6. **SSR's two-sidedness is a content effect, not a blending one.** §9.2 says
   gross disparity "butts unrelated content together and energy rises above 1".
   Controlled measurement says otherwise: with homogeneous content, blending two
   uncorrelated views always *lowers* energy relative to the shoulders —
   0/1/2/3/4 px give 1.011/0.885/0.657/0.557/0.583 and 8–200 px are flat at
   ~0.68. SSR exceeds 1 when the seam's content is intrinsically busier than the
   shoulders', which is what the live 0.3-m frame does (3.64). The conclusion is
   unchanged and now better founded: report `|ln SSR|`, and treat it as a
   **detector**, never an estimator — it saturates past ~4 px.

And one thing the design could not have known, found only by flashing:

7. **`/mnt/sda` is not mounted when `rcS` runs the `S*` scripts.** `rcS` does
   `mount -a` first, but the SD card is `/dev/hd/sda1`, brought up later by the
   app. The first flashed build followed §7.2's order — disable check, anchors
   check, *then* wait — found no `anchors.txt`, and exited. It could not even say
   so: `/` is read-only squashfs, so the log write failed too, and the hook left
   no trace whatsoever. `S98_StitchCal` now waits for the mount **before** it
   reads any configuration, and `build_stitchcal.sh` gates on that ordering.
