#!/bin/bash
# test_fieldroi.sh -- build fieldroi for the host and for the camera, and prove
# both give the PC model's answer.
#
# fieldroi.c is a hand-written copy of a network that normally runs under
# onnxruntime. A copy that is subtly wrong (one layer's weights read in the
# wrong order, a padding off by one) still prints ten plausible keypoints, so
# "it runs" proves nothing. This compares its keypoints with a reference written
# by the real model on the same frame, and fails on any difference over half a
# pixel.
#
# It also checks the three ways the tool must REFUSE rather than answer: a
# truncated weights file, a frame with no grass in it, and noise.
#
# Usage (under WSL / Linux; needs gcc, aarch64-linux-gnu-gcc, python3):
#   1. on any machine with the project env, write the weights and a reference:
#        uv run --with onnx python fieldroi/export_weights.py MODEL.onnx OUT.frw \
#            --reference FRAME.ppm REF.txt
#   2. bash verify/test_fieldroi.sh OUT.frw FRAME.ppm REF.txt
#
# The aarch64 binary runs under qemu-aarch64-static when that is installed. That
# proves the camera build computes the same numbers; it says nothing about how
# long the camera takes.
set -euo pipefail

FRW="${1:?usage: $0 <weights.frw> <frame.ppm> <reference.txt>}"
PPM="${2:?usage: $0 <weights.frw> <frame.ppm> <reference.txt>}"
REF="${3:?usage: $0 <weights.frw> <frame.ppm> <reference.txt>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/../fieldroi/fieldroi.c"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "==> build (host + static aarch64), warnings are errors"
gcc -O3 -Wall -Wextra -Werror -o "$WORK/fieldroi_host" "$SRC" -lm
aarch64-linux-gnu-gcc -O3 -Wall -Wextra -Werror -static -o "$WORK/fieldroi_arm64" "$SRC" -lm

# Work on the local filesystem; inputs may sit on a slow mount.
cp "$FRW" "$WORK/w.frw"
cp "$PPM" "$WORK/frame.ppm"

compare() { # <label> <keypoint dump>
    python3 - "$REF" "$2" "$1" <<'PY'
import re, sys
def load(path):
    out = []
    for line in open(path):
        m = re.match(r"kpt (\d+)\s+x\s+(\S+)\s+y\s+(\S+)\s+score (\S+)", line)
        if m:
            out.append(tuple(float(v) for v in m.groups()[1:]))
    return out
ref, got = load(sys.argv[1]), load(sys.argv[2])
if len(ref) != 10 or len(got) != 10:
    sys.exit(f"{sys.argv[3]}: expected 10 keypoints, reference has {len(ref)}, binary printed {len(got)}")
dk = max(max(abs(a[0] - b[0]), abs(a[1] - b[1])) for a, b in zip(ref, got))
ds = max(abs(a[2] - b[2]) for a, b in zip(ref, got))
print(f"    {sys.argv[3]}: max keypoint difference {dk:.3f} px, max score difference {ds:.4f}")
if dk > 0.5 or ds > 0.01:
    sys.exit(f"{sys.argv[3]}: does NOT match the reference model")
PY
}

echo "==> 1) host binary matches the reference model"
"$WORK/fieldroi_host" "$WORK/w.frw" "$WORK/frame.ppm" --keypoints 2> "$WORK/host.txt"
compare host "$WORK/host.txt"

echo "==> 2) camera (aarch64) binary matches the reference model"
if command -v qemu-aarch64-static > /dev/null; then
    qemu-aarch64-static "$WORK/fieldroi_arm64" "$WORK/w.frw" "$WORK/frame.ppm" --keypoints 2> "$WORK/arm.txt"
    compare aarch64 "$WORK/arm.txt"
else
    echo "    SKIPPED: qemu-aarch64-static not installed -- the camera build is UNTESTED"
    exit 1
fi

echo "==> 3) it refuses what it must refuse"
refuses() { # <label> <weights> <frame>
    if "$WORK/fieldroi_host" "$2" "$3" > "$WORK/out.conf" 2> "$WORK/err.txt"; then
        echo "    FAIL: $1 produced a window:"; sed 's/^/        /' "$WORK/out.conf"; exit 1
    fi
    if [ -s "$WORK/out.conf" ]; then
        echo "    FAIL: $1 exited non-zero but still wrote a config"; exit 1
    fi
    echo "    ok: $1 refused ($(tail -1 "$WORK/err.txt" | cut -c1-90))"
}
python3 - "$WORK" <<'PY'
import os, sys
w = sys.argv[1]
hdr = b"P6\n768 384\n255\n"
open(f"{w}/grey.ppm", "wb").write(hdr + bytes([128]) * (768 * 384 * 3))
open(f"{w}/black.ppm", "wb").write(hdr + bytes(768 * 384 * 3))
open(f"{w}/noise.ppm", "wb").write(hdr + os.urandom(768 * 384 * 3))
PY
head -c $(( $(stat -c %s "$WORK/w.frw") / 2 )) "$WORK/w.frw" > "$WORK/half.frw"
refuses "truncated weights" "$WORK/half.frw" "$WORK/frame.ppm"
refuses "uniform grey frame" "$WORK/w.frw" "$WORK/grey.ppm"
refuses "black frame" "$WORK/w.frw" "$WORK/black.ppm"
refuses "noise frame" "$WORK/w.frw" "$WORK/noise.ppm"

echo "==> 4) and answers on the real frame"
"$WORK/fieldroi_host" "$WORK/w.frw" "$WORK/frame.ppm" > "$WORK/roi.conf" 2> "$WORK/err.txt" \
    || { echo "    FAIL: refused the reference frame:"; tail -2 "$WORK/err.txt"; exit 1; }
grep -E "window rows|green fraction" "$WORK/err.txt" | sed 's/^/    /'
grep -q '^enable=1$' "$WORK/roi.conf" || { echo "    FAIL: no enable=1 in the output"; exit 1; }

echo
echo "PASS: fieldroi matches the model on host and aarch64, and fails closed."
