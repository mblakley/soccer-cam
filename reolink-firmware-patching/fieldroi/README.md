# fieldroi — find the field on the camera

Runs the project's field-outline model on one frame and prints the encoder ROI
window (`roi.conf`) that covers the pitch. One C file, no runtime dependencies,
built static for the camera. Why it exists and how well the model does:
`docs/FAR_FIELD_BITS.md`, "Finding the field on the camera".

**Status (2026-11-27): proven off the camera only.** It matches the PC model on
real frames, the aarch64 build under qemu gives the same numbers as the host
build, and it reads the camera's own snapshot format (baseline JPEG). It has
never run on the camera, and nothing there fetches a snapshot for it yet.

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
```

It exits non-zero and prints no config when the result does not look like a
field: too few keypoints, a field under a quarter of the frame tall, claimed
field rows that are not grass-coloured, or a snapshot that was cut short. The
grass check exists because the network itself always answers, even for a black
frame; the three archived camera snapshots (all indoors) are refused by it.

A JPEG is read at one-eighth scale from its DC coefficients, which is what
keeps a 7680x2160 snapshot inside the camera's memory. `--dump-input FILE`
writes the 768x384 picture the network was shown.

The window is full-width and padded 200 rows above and below by default. The
padding above is deliberate: the model's known error is a far line placed too
low, and that band is cheap to include.
