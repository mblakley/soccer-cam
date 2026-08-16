# VPE 2-D warp mesh

The Duo 3's Video Processing Engine warps each sensor image through a coarse
control-point mesh (the DCE's "2-D LUT") before the stitcher composes the
panorama. The mesh decides where every source pixel lands, so it is the only
place where an *exactly specified* geometric mapping can be imposed.

| file | what |
|---|---|
| `lut2d.py` | decode / edit / rebuild a mesh off-camera; `selftest` gates the parser |
| `lut2d_ioctl.c` | read and write the live mesh on the camera via `/dev/nvt_vpe` |

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
