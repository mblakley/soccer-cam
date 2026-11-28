#!/bin/bash
# test_setroi_patch.sh -- run a pak's `setroi` handler before flashing the pak.
#
# builds/build_roi_qp.sh changes kernel code in kflow_videoenc.ko: a format
# string always, and with ROIQP_MULTIWIN=1 twenty instructions and nine
# relocation entries. This pulls the module out of a built pak, links it into a
# user-space aarch64 program (verify/setroi_harness.c) and executes the real
# handler under qemu with command strings, checking what it writes.
#
# Usage (WSL / Linux; needs python3, unsquashfs, aarch64-linux-gnu-gcc,
# qemu-aarch64-static):
#   bash verify/test_setroi_patch.sh <pak> <stock|fix|multiwin>
#
#   stock     an unpatched module: expected to LOSE the QP (the original bug)
#   fix       format string fixed, one window
#   multiwin  built with ROIQP_MULTIWIN=1: ten windows, index range-checked
#
# The variant is not guessed from the pak: say which one you built, and the
# test fails if the module does not behave like it.
set -euo pipefail

PAK="${1:?usage: $0 <pak> <stock|fix|multiwin>}"
VARIANT="${2:?usage: $0 <pak> <stock|fix|multiwin>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KO_REL="lib/modules/5.10.168/hdal/kflow_videoenc/unit/kflow_videoenc.ko"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

for tool in unsquashfs aarch64-linux-gnu-gcc qemu-aarch64-static; do
    command -v "$tool" > /dev/null || { echo "ERROR: $tool not installed"; exit 1; }
done

echo "==> extract kflow_videoenc.ko from $(basename "$PAK")"
python3 - "$PAK" "$WORK/rootfs.bin" <<'PY'
import struct, sys
d = open(sys.argv[1], "rb").read()
b = 0x18 + 5 * 0x48                       # section 5 = rootfs
off, size = struct.unpack("<QQ", d[b + 0x38:b + 0x48])
open(sys.argv[2], "wb").write(d[off:off + size])
PY
unsquashfs -no-xattrs -d "$WORK/rfs" -no-progress "$WORK/rootfs.bin" "$KO_REL" > /dev/null
cp "$WORK/rfs/$KO_REL" "$WORK/kflow_videoenc.o"

echo "==> link the module into the harness (this applies its relocation table)"
# The module's 140 kernel imports are left unresolved on purpose: only the
# handler's own callees are supplied, and nothing else is ever executed.
aarch64-linux-gnu-gcc -O1 -Wall -Wextra -static -o "$WORK/harness" \
    "$HERE/setroi_harness.c" "$WORK/kflow_videoenc.o" \
    -Wl,--unresolved-symbols=ignore-all -Wl,--no-warn-rwx-segments 2> "$WORK/link.txt" \
    || { echo "LINK FAILED:"; cat "$WORK/link.txt"; exit 1; }
grep -iE "relocation|truncated|overflow" "$WORK/link.txt" && { echo "ERROR: relocation problems at link"; exit 1; }

echo "==> execute the handler"
qemu-aarch64-static "$WORK/harness" "$VARIANT"
