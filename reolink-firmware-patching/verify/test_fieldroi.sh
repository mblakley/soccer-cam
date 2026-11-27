#!/bin/bash
# test_fieldroi.sh -- build fieldroi for the host and for the camera, and prove
# both give the PC model's answer.
#
# fieldroi.c is a hand-written copy of a network that normally runs under
# onnxruntime, plus a hand-written JPEG reader. A copy that is subtly wrong (one
# layer's weights read in the wrong order, a padding off by one, a Huffman table
# misbuilt) still prints ten plausible keypoints, so "it runs" proves nothing.
# This checks, in order:
#
#   1. host build, PPM input       == the real model's keypoints (0.5 px)
#   2. host build, JPEG input      ~= the PPM result (the JPEG is read at 1/8
#                                     scale, so rows may move; 64 rows allowed)
#   3. aarch64 build, JPEG input   == host build, JPEG input (same arithmetic,
#                                     so the same numbers)
#   4. refusals: truncated weights, grey / black / noise frames, a truncated
#      JPEG -- each must exit non-zero AND print no config
#
# Step 3 is the camera's real case: its build, its input format. It runs under
# qemu-aarch64-static, which proves the numbers and says nothing about how long
# the camera takes.
#
# Usage (under WSL / Linux; needs gcc, aarch64-linux-gnu-gcc, python3, qemu):
#   1. on any machine with the project env, write the weights, a reference from
#      the real model, and a JPEG of the same frame (REF.jpg, next to REF.txt):
#        uv run --with onnx python fieldroi/export_weights.py MODEL.onnx OUT.frw \
#            --reference FRAME.ppm REF.txt
#   2. bash verify/test_fieldroi.sh OUT.frw FRAME.ppm REF.txt REF.jpg
set -euo pipefail

USAGE="usage: $0 <weights.frw> <frame.ppm> <reference.txt> <same-frame.jpg>"
FRW="${1:?$USAGE}"
PPM="${2:?$USAGE}"
REF="${3:?$USAGE}"
JPG="${4:?$USAGE}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/../fieldroi/fieldroi.c"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

command -v qemu-aarch64-static > /dev/null \
    || { echo "ERROR: qemu-aarch64-static not installed; the camera build cannot be tested"; exit 1; }

echo "==> build (host + static aarch64), warnings are errors"
gcc -O3 -Wall -Wextra -Werror -o "$WORK/fieldroi_host" "$SRC" -lm
aarch64-linux-gnu-gcc -O3 -Wall -Wextra -Werror -static -o "$WORK/fieldroi_arm64" "$SRC" -lm

# Work on the local filesystem; inputs may sit on a slow mount.
cp "$FRW" "$WORK/w.frw"
cp "$PPM" "$WORK/frame.ppm"
cp "$JPG" "$WORK/frame.jpg"
cp "$REF" "$WORK/ref.txt"
cd "$WORK"

compare() { # <label> <expected dump> <got dump> <max px> <max score>
    python3 - "$@" <<'PY'
import re, sys
label, exp_path, got_path, max_px, max_score = sys.argv[1:6]
def load(path):
    out = []
    for line in open(path):
        m = re.match(r"kpt (\d+)\s+x\s+(\S+)\s+y\s+(\S+)\s+score (\S+)", line)
        if m:
            out.append(tuple(float(v) for v in m.groups()[1:]))
    return out
exp, got = load(exp_path), load(got_path)
if len(exp) != 10 or len(got) != 10:
    sys.exit(f"{label}: expected 10 keypoints each, got {len(exp)} and {len(got)}")
dk = max(max(abs(a[0] - b[0]), abs(a[1] - b[1])) for a, b in zip(exp, got))
ds = max(abs(a[2] - b[2]) for a, b in zip(exp, got))
print(f"    {label}: max keypoint difference {dk:.3f} px, max score difference {ds:.4f}")
if dk > float(max_px) or ds > float(max_score):
    sys.exit(f"{label}: MISMATCH (allowed {max_px} px, {max_score} score)")
PY
}
window() { sed -n 's/.*window rows \([0-9]*\)\.\.\([0-9]*\).*/\1 \2/p' "$1"; }

echo "==> 1) host build, PPM input, matches the real model"
./fieldroi_host w.frw frame.ppm > ppm.conf 2> host_ppm.txt \
    || { echo "    FAIL: refused the reference frame:"; tail -2 host_ppm.txt; exit 1; }
compare "host/ppm vs model" ref.txt host_ppm.txt 0.5 0.01
grep -E "window rows|green fraction" host_ppm.txt | sed 's/^/    /'
grep -q '^enable=1$' ppm.conf || { echo "    FAIL: no enable=1 in the output"; exit 1; }

echo "==> 2) host build, JPEG input (1/8-scale read), lands on the same rows"
./fieldroi_host w.frw frame.jpg > jpg.conf 2> host_jpg.txt \
    || { echo "    FAIL: refused the JPEG of the same frame:"; tail -2 host_jpg.txt; exit 1; }
read -r PT PB < <(window host_ppm.txt)
read -r JT JB < <(window host_jpg.txt)
echo "    window rows: ppm $PT..$PB, jpeg $JT..$JB"
DT=$(( PT > JT ? PT - JT : JT - PT )); DB=$(( PB > JB ? PB - JB : JB - PB ))
[ "$DT" -le 64 ] && [ "$DB" -le 64 ] \
    || { echo "    FAIL: JPEG and PPM windows differ by $DT / $DB rows (64 allowed)"; exit 1; }

echo "==> 3) aarch64 build, JPEG input, equals the host build (qemu; takes minutes)"
qemu-aarch64-static ./fieldroi_arm64 w.frw frame.jpg > arm.conf 2> arm_jpg.txt \
    || { echo "    FAIL: the aarch64 build refused the frame:"; tail -2 arm_jpg.txt; exit 1; }
compare "aarch64/jpeg vs host/jpeg" host_jpg.txt arm_jpg.txt 0.01 0.0005
cmp -s jpg.conf arm.conf || { echo "    FAIL: aarch64 wrote a different roi.conf than the host"; exit 1; }
echo "    identical roi.conf"

echo "==> 4) it refuses what it must refuse"
refuses() { # <label> <weights> <frame>
    if ./fieldroi_host "$2" "$3" > out.conf 2> err.txt; then
        echo "    FAIL: $1 produced a window:"; sed 's/^/        /' out.conf; exit 1
    fi
    if [ -s out.conf ]; then
        echo "    FAIL: $1 exited non-zero but still wrote a config"; exit 1
    fi
    echo "    ok: $1 refused ($(tail -1 err.txt | cut -c1-90))"
}
python3 - <<'PY'
import os
hdr = b"P6\n768 384\n255\n"
open("grey.ppm", "wb").write(hdr + bytes([128]) * (768 * 384 * 3))
open("black.ppm", "wb").write(hdr + bytes(768 * 384 * 3))
open("noise.ppm", "wb").write(hdr + os.urandom(768 * 384 * 3))
PY
head -c $(( $(stat -c %s w.frw) / 2 )) w.frw > half.frw
head -c $(( $(stat -c %s frame.jpg) / 2 )) frame.jpg > half.jpg
refuses "truncated weights" half.frw frame.ppm
refuses "uniform grey frame" w.frw grey.ppm
refuses "black frame" w.frw black.ppm
refuses "noise frame" w.frw noise.ppm
refuses "truncated JPEG" w.frw half.jpg

echo
echo "PASS: fieldroi matches the model, reads the camera's format, and fails closed."
