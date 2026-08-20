#!/bin/bash
# Two small aarch64 helpers used to read the pre-blend seam layer pair off a
# running camera. Read-only tools: they pull one frame descriptor and release it
# microseconds later, and they read physical memory. Neither writes anything on
# the device.
#
# WHY THESE ARE NOT BAKED INTO FIRMWARE, unlike lut2d_ioctl. lut2d_ioctl is on
# the boot path -- S98_StitchCal needs it at every boot, so it has to survive a
# reformatted SD card. These two are an operator-initiated diagnostic: they are
# uploaded when the seam-calibration page asks for a layer pair and removed
# again in the same call. Nothing on the camera depends on them being present,
# so baking them would enlarge the flashed image for no gain.
#
# WHY THE BINARIES ARE NOT COMMITTED. Same rule the rest of this tree follows:
# C sources are tracked, build outputs are not. `video_grouper/web`'s layer pull
# looks for them in builds/out/ and refuses with an explicit "not built" message
# naming the path, rather than half-working.
#
# Freestanding: both make raw syscalls and link no libc, which is why they are
# ~4 KB and why -nostdlib is not optional.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/builds/out"
CC="${CC:-aarch64-linux-gnu-gcc}"

command -v "$CC" >/dev/null || {
  echo "ERROR: need $CC (apt install gcc-aarch64-linux-gnu)" >&2
  exit 1
}

mkdir -p "$OUT"

# pmemdump.c builds to `pemdump` -- the name the camera-side call site uses.
build() {
  local src="$1" bin="$2"
  "$CC" -O2 -static -nostdlib -ffreestanding -Wall -Wextra \
    -o "$OUT/$bin" "$ROOT/vpe/$src"
  echo "  $bin  $(stat -c%s "$OUT/$bin") bytes"
}

echo "==> building seam layer helpers into $OUT"
build isfpull2.c isfpull2
build pmemdump.c pemdump

# A wrong-architecture binary uploads fine and then fails on the camera with
# nothing but a silent non-zero, so check it here where the message is useful.
for b in isfpull2 pemdump; do
  arch="$(aarch64-linux-gnu-readelf -h "$OUT/$b" | awk -F: '/Machine/{print $2}' | xargs)"
  [ "$arch" = "AArch64" ] || { echo "ERROR: $b built for '$arch', not AArch64" >&2; exit 1; }
done

echo "==> ok. The /stitch page will upload these on demand and remove them after."
