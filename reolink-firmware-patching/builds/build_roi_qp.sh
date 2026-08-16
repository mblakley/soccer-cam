#!/bin/bash
# Build a patched .pak that makes the encoder's ROI window (per-region QP bias)
# usable from the camera, and drives it from a per-game config file on the SD
# card. Everything this build touches is inside the `rootfs` SquashFS:
#
#   - kflow_videoenc.ko  : 26-byte in-place format-string fix (see below)  [rootfs]
#   - /etc/init.d/S37_RoiQp                  the daemon that applies the window
#   - /etc/soccercam_roi.conf.default        baked-in fallback config
#
# `app` is NOT touched, so this layers cleanly on top of any existing build:
# pass a stock pak for a minimal test image, or pass your comprehensive pak as
# the base to keep the HTTP unlock / bitrate cap / netstate / recovery.
#
# ---------------------------------------------------------------------------
# What the .ko patch does
#
# The kernel's ROI debug command is
#     echo vdoenc setroi <PathId> <RoiIndex> <Enable> <QP> <QPMode> <X> <Y> <W> <H> \
#         > /proc/hdal/venc/cmd
# and its handler (Cmd_VdoEnc_SetROI, kflow_videoenc.ko VMA 0x00111b44) parses
# those nine numbers with
#     sscanf_s(s, "%d %d %d %d %d %d %d %d %d", ...)
# into a 16-byte struct whose fields are NOT all int:
#     +0x0 u32 enable   +0x4 u16 x   +0x6 u16 y   +0x8 u16 w
#     +0xa u16 h        +0xc s8  qp  +0xd u8  qp_mode
# Nine `%d` conversions write nine 4-byte ints, so the writes overlap. The last
# one (Height, at +0x0a) spills its two high bytes over +0x0c and +0x0d, i.e.
# over qp and qp_mode. On stock firmware `setroi` therefore always ends up with
# QP=0 / QPMode=0 — an enabled window that requests no bit reallocation at all.
#
# The fix is a length qualifier per field. The replacement format is exactly the
# same 26 bytes as the original, so no relocation, section size or symbol moves:
#
#   before: "%d %d %d %d %d %d %d %d %d"   (26 bytes, file offset 0x36688)
#   after:  "%d%d%d%hhd%hhd%hd%hd%hd%hd"   (26 bytes)
#
# The spaces are droppable because numeric scanf conversions skip leading
# whitespace (kwrap.ko vsscanf_s @ 0x00109b70 does the _ctype space test before
# every numeric conversion), and kwrap's vsscanf_s implements both `h` and `hh`
# (case 0x68 sets short on the first 'h', char on the second).
#
# Full evidence: docs/ENCODER_ROI_QP.md.
#
# NOTHING HERE HAS BEEN RUN ON HARDWARE. The camera was offline when this was
# written. Treat a build from this script as staged, not proven.
# ---------------------------------------------------------------------------
#
# No sudo needed (unsquashfs -no-xattrs, mksquashfs -all-root).
# Usage:
#   bash build_roi_qp.sh <base.pak> <out.pak> [roi.conf]
# where [roi.conf] is an optional file baked in as the default window; if
# omitted, ../runtime/roiqp/roi.conf.example is used.
#
# NOTE: <out.pak> MUST be named to the Reolink pattern or the camera rejects it
# on upload:
#   IPC_NT15NA416MP.<build>_2505072124.Reolink-Duo-3-PoE.16MP.REOLINK_roiqp.pak
set -euo pipefail

BASE="${1:?usage: $0 <base.pak> <out.pak> [roi.conf]}"
OUT="${2:?usage: $0 <base.pak> <out.pak> [roi.conf]}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAK_DIR="$(cd "$HERE/../pak" && pwd)"
RT="$HERE/../runtime/roiqp"
CONF_SRC="${3:-$RT/roi.conf.example}"
INIT_SRC="$RT/S37_RoiQp"
KO_REL="lib/modules/5.10.168/hdal/kflow_videoenc/unit/kflow_videoenc.ko"

case "$(basename "$OUT")" in
  IPC_NT15NA416MP.*_*.Reolink-Duo-3-PoE.16MP.REOLINK*.pak) : ;;
  *) echo "WARNING: output name '$(basename "$OUT")' does NOT match the Reolink pattern;" >&2
     echo "         the camera's Local Upgrade will reject it ('Failed to recognize the file format')." >&2
     echo "         e.g. IPC_NT15NA416MP.4910_2505072124.Reolink-Duo-3-PoE.16MP.REOLINK_roiqp.pak" >&2 ;;
esac

for x in "$INIT_SRC" "$CONF_SRC"; do
    [[ -f "$x" ]] || { echo "ERROR: missing $x"; exit 1; }
done

WORK="$(mktemp -d)"
trap "rm -rf '$WORK'" EXIT
cd "$WORK"

echo "==> 1) extract rootfs"
python3 - "$BASE" <<'PY'
import struct, sys
d = open(sys.argv[1], "rb").read()
b = 0x18 + 5 * 0x48                       # section 5 = rootfs
off = struct.unpack("<Q", d[b + 0x38:b + 0x40])[0]
sz = struct.unpack("<Q", d[b + 0x40:b + 0x48])[0]
open("rootfs_stock.bin", "wb").write(d[off:off + sz])
PY
unsquashfs -no-xattrs -d rootfs_unpacked -no-progress rootfs_stock.bin >/dev/null

echo "==> 2) patch kflow_videoenc.ko setroi format string"
python3 - "$KO_REL" <<'PY'
import sys
KO = "rootfs_unpacked/" + sys.argv[1]
OFF = 0x36688
OLD = b"%d %d %d %d %d %d %d %d %d"
NEW = b"%d%d%d%hhd%hhd%hd%hd%hd%hd"
assert len(NEW) == len(OLD) == 26, "replacement must be the same 26 bytes"
d = bytearray(open(KO, "rb").read())
got = bytes(d[OFF:OFF + len(OLD)])
assert got == OLD, f"{KO}[{hex(OFF)}] mismatch: got {got!r}, expected {OLD!r}. Firmware may have changed; stop."
assert d[OFF + len(OLD)] == 0, "format string is not NUL-terminated at the expected length; stop."
# The 9-int format also appears as a substring of longer format strings in this
# module; only the NUL-delimited whole string is ours, and there must be exactly
# one of it or we are patching the wrong site.
whole = b"\x00" + OLD + b"\x00"
assert bytes(d).count(whole) == 1, f"expected exactly 1 whole-string match, got {bytes(d).count(whole)}"
d[OFF:OFF + len(NEW)] = NEW
open(KO, "wb").write(bytes(d))
print(f"   {OLD.decode()} -> {NEW.decode()}  (26 bytes, in place)")
PY

echo "==> 3) install S37_RoiQp + default config"
install -m 0755 "$INIT_SRC" rootfs_unpacked/etc/init.d/S37_RoiQp
install -m 0644 "$CONF_SRC" rootfs_unpacked/etc/soccercam_roi.conf.default
echo "   /etc/init.d/S37_RoiQp + /etc/soccercam_roi.conf.default"

echo "==> 4) repack rootfs squashfs"
mksquashfs rootfs_unpacked rootfs_new.bin \
    -comp xz -b 262144 -noappend -no-progress \
    -no-exports -all-root -mkfs-time 0 -all-time 0 \
    >/dev/null

echo "==> 5) pre-flight: boot-chain sections must be handed through untouched"
# This runs BEFORE any CRC is computed. It asserts that the only replacement
# blob we are about to give the repacker is `rootfs`, so loader/fdt/atf/uboot/
# kernel are, by construction, the base pak's own bytes.
python3 - <<'PY'
SWAPS = {"rootfs"}
BOOT = ["loader", "fdt", "atf", "uboot", "kernel"]
overlap = SWAPS.intersection(BOOT)
assert not overlap, f"REFUSING: this build would replace boot sections {sorted(overlap)}"
print("   swapping only: " + ", ".join(sorted(SWAPS)))
PY

echo "==> 6) repack pak"
PYTHONPATH="$PAK_DIR" python3 - "$BASE" "$WORK/out.pak" <<'PY'
import sys
from pak_repack import repack
crc, size, _ = repack(sys.argv[1], sys.argv[2],
                      swaps={"rootfs": open("rootfs_new.bin", "rb").read()})
print(f"   built size={size} crc=0x{crc:08x}")
PY

echo "==> 7) HARD GUARD: loader/fdt/atf/uboot/kernel byte-identical to the base"
# A pak whose boot chain differs from a known-good base is not recoverable by
# reflashing over the network. If this check fails the output is deleted, not
# warned about: a warning in a log nobody reads is not a guard.
python3 - "$BASE" "$WORK/out.pak" <<'PY'
import hashlib, struct, sys

BOOT = {"loader", "fdt", "atf", "uboot", "kernel"}
# `ai` and `app` are not touched by this build either; check them too, so an
# accidental swap shows up here rather than on the camera.
ALSO = {"ai", "app"}


def sections(path):
    d = open(path, "rb").read()
    out = {}
    for i in range(8):
        b = 0x18 + i * 0x48
        name = d[b:b + 0x20].split(b"\x00")[0].decode("ascii", "replace")
        off = struct.unpack("<Q", d[b + 0x38:b + 0x40])[0]
        sz = struct.unpack("<Q", d[b + 0x40:b + 0x48])[0]
        if sz:
            out[name] = d[off:off + sz]
    return out


base, new = sections(sys.argv[1]), sections(sys.argv[2])
bad = []
for name in sorted(BOOT | ALSO):
    b, n = base.get(name), new.get(name)
    if b is None or n is None:
        bad.append(f"{name}: present in base={b is not None} out={n is not None}")
        continue
    hb, hn = hashlib.sha256(b).hexdigest(), hashlib.sha256(n).hexdigest()
    tag = "BOOT" if name in BOOT else "    "
    if hb == hn:
        print(f"   {tag} {name:8s} identical  {hb[:16]} ({len(b)} bytes)")
    else:
        bad.append(f"{name}: {hb[:16]} != {hn[:16]}")
if bad:
    print("\nABORT: sections that must be byte-identical are not:")
    for m in bad:
        print("   " + m)
    sys.exit(1)
if base["rootfs"] == new["rootfs"]:
    print("\nABORT: rootfs is unchanged — the patch did not take.")
    sys.exit(1)
print("   rootfs  replaced as intended")
PY

mv "$WORK/out.pak" "$OUT"

echo "==> 8) verify CRC"
python3 "$PAK_DIR/reolink_crc.py" compute "$OUT"

echo
echo "====================================================================="
echo " Build complete: $OUT"
echo "  - kflow_videoenc.ko setroi format string: fixed (26 B, in place)"
echo "  - /etc/init.d/S37_RoiQp:                  installed"
echo "  - /etc/soccercam_roi.conf.default:        $(basename "$CONF_SRC")"
echo "  - loader/fdt/atf/uboot/kernel/ai/app:     byte-identical to base"
echo
echo " NOT VERIFIED ON HARDWARE. First checks after flashing (see"
echo " docs/ENCODER_ROI_QP.md section 7, 'Prove-out'):"
echo "   1. cat /proc/hdal/venc/info"
echo "      -> baseline: out 0 is H265 7680x2160 and the ROI table is EMPTY."
echo "   2. echo vdoenc setroi 0 0 1 -6 1 0 760 7680 900 > /proc/hdal/venc/cmd"
echo "      dmesg | grep 'Set ROI Index' | tail -1"
echo "      -> MUST read back QP = -6, QPMode = 1. If it reads QP = 0,"
echo "         QPMode = 0 the .ko patch did not take effect."
echo "   3. cat /proc/hdal/venc/info"
echo "      -> the ROI table must now have a row for out 0. This is the"
echo "         driver's own readback, and the strongest single check."
echo "   4. cat /mnt/sda/soccercam/roi.log  -> what the daemon parsed/applied."
echo "====================================================================="
