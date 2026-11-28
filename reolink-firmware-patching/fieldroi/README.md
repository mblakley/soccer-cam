# fieldroi — find the field on the camera

Runs the project's field-outline model on one frame and prints the encoder ROI
window (`roi.conf`) that covers the pitch. One C file, no runtime dependencies,
built static for the camera. Why it exists and how well the model does:
`docs/FAR_FIELD_BITS.md`, "Finding the field on the camera".

**Status (2026-11-28): runs on the camera; not yet used on a live game.** It
matches the PC model on real frames, the aarch64 build under qemu gives the same
numbers as the host build, and it reads the camera's own snapshot format
(baseline JPEG). On the camera it takes about 80 s at `nice 19` and about 23 MB
(EXP-ENC-07/08), and its output has been applied there by `S37_RoiQp`
(EXP-ENC-08, EXP-ENC-10). Nothing on the camera runs it automatically yet.

**Use `--bands -10,-6,-3` on the ten-window firmware (build 4923 or later).**
That writes three full-width windows — far band on window 0, then mid, then
near — instead of one rectangle. The grading was chosen on game footage at the
camera's bitrate (EXP-ENC-10): far band +4.7 dB, mid field +2.2 dB, near field
unchanged, foreground -2.6 dB. Without `--bands` the output is the original one
rectangle, which every ROI build accepts.

The output is two numbers: the field's top row and bottom row, padded, as a
full-width rectangle. The field spans nearly the whole width at every ground, so
rows are all a single ROI window needs.

| file | what |
|---|---|
| `fieldroi.c` | the network (ResNet18 trunk, two linear heads), image resize, the row logic and the refusals |
| `export_weights.py` | ONNX model → the flat `.frw` weights file; optionally a reference output for the test |
| `../verify/test_fieldroi.sh` | builds both targets, compares them with the real model, checks the refusals |

The model is not in the repo. It is `field_outline_v2.onnx` (F:
`training_checkpoints/field_outline/`, also published as the
`field-outline-v2.0.0` release asset).

```bash
# 1. weights, a reference from the real model, and the frame as ref.jpg
#    (any machine with the project env)
uv run --with onnx python reolink-firmware-patching/fieldroi/export_weights.py \
    field_outline_v2.onnx field_outline_v2.frw --reference frame.ppm ref.txt

# 2. build, compare, check refusals (WSL / Linux; the qemu step takes minutes)
bash reolink-firmware-patching/verify/test_fieldroi.sh \
    field_outline_v2.frw frame.ppm ref.txt ref.jpg

# 3. use: a camera snapshot in, a roi.conf out
./fieldroi field_outline_v2.frw snapshot.jpg > roi.conf
./fieldroi field_outline_v2.frw snapshot.jpg --bands -10,-6,-3 > roi.conf   # ten-window firmware
```

It exits non-zero and prints no config when the result does not look like a
field: too few keypoints, a field under a quarter of the frame tall, a field
whose middle is grey, black or white, claimed field rows that are mostly not the
colour of that middle, or a snapshot that was cut short.

**The field-colour check is adaptive and has no off switch.** The network always
answers, even for a black frame, so the picture is asked too. The field's colour
is the per-channel median of a patch in the middle of the claimed field (not one
pixel: the halfway line, a logo or a player may be there). That colour must be a
colour (not grey: this is what refuses a lens cap and night vision), and at
least 60 % of the claimed rows must match it, compared by channel share so sun
and shadow count as one surface. Green grass and the blue indoor dome both pass
(EXP-ENC-11).

**What it does not do:** it cannot tell that an outline is in the wrong place
when the surroundings are the field's colour too, which is the normal case
outdoors (grass beyond the touchline, trees). It also accepts a television
showing a pitch. It is a guard against "this is not a field at all", no more.

A JPEG is read at one-eighth scale from its DC coefficients, which is what
keeps a 7680x2160 snapshot inside the camera's memory. `--dump-input FILE`
writes the 768x384 picture the network was shown.

The window is full-width and padded 200 rows above and below by default. The
padding above is deliberate: the model's known error is a far line placed too
low, and that band is cheap to include.
