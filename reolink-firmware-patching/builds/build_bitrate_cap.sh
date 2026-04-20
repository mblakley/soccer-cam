#!/bin/bash
# Build a patched .pak that raises the main-stream max bitrate from the stock
# 12288 kbps (12 Mbps) cap. Also carries the HTTP /downloadfile/ unlock from
# build_http_unlock.sh (they modify different files and compose cleanly).
#
# What it does (in the `router` ELF inside the `app` SquashFS):
#   File offset 0x6351c in `router`:
#     before: 0b 00 86 52   ; mov w11, #0x3000  (= 12288 kbps)
#     after:  0b 00 ?? 52   ; mov w11, #<new>   (= your target kbps)
#   where the ?? byte is derived from the MOVZ W11 encoding:
#     inst = 0x5280000B | (imm16 << 5)
#
# This single instruction is the last entry of the main-stream bitrate
# range array builder inside router's FUN_004632b0 (bc_cfg.cpp). Whatever
# value this instruction loads becomes the highest entry in
# range.Enc[0].mainStream.bitRate that cgi returns via GetEnc.
#
# Empirical encoder ceiling for the Duo 3 at 7680x2160/h265:
#   accepted : 16384 (16M), 18432 (18M), 20480 (20M)
#   rejected : 21504, 22528, 24576 — SetEnc returns rspCode -13
# So the practical daily-driver value is 20480 kbps.
#
# The new value REPLACES the stock 12288 entry in the dropdown (the array
# stays 9 entries long). If you want the old 12288 option preserved too,
# that requires also patching the count; not done here.
#
# Usage:
#   sudo bash build_bitrate_cap.sh <stock.pak> <output.pak> <kbps>
# Example:
#   sudo bash build_bitrate_cap.sh stock.pak out.pak 20480
#
# Constraints on kbps:
#   Must fit ARM64's 16-bit MOVZ immediate (LSL #0), so must be < 65536.
#   For cleanliness, use a value that's a multiple of 32.
set -euo pipefail

STOCK="${1:?usage: $0 <stock.pak> <output.pak> <kbps>}"
OUT="${2:?usage: $0 <stock.pak> <output.pak> <kbps>}"
KBPS="${3:?usage: $0 <stock.pak> <output.pak> <kbps>}"
[[ "$EUID" -eq 0 ]] || { echo "ERROR: run as root"; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAK_DIR="$(cd "$HERE/../pak" && pwd)"
WORK="$(mktemp -d)"
trap "rm -rf '$WORK'" EXIT
cd "$WORK"

# Encode the bitrate as the ARM64 MOVZ W11 instruction bytes
PATCH_HEX=$(python3 -c "
imm = $KBPS
assert 0 < imm < 0x10000, 'kbps must fit in ARM64 MOVZ 16-bit immediate'
inst = 0x5280000B | (imm << 5)
print('%02x %02x %02x %02x' % (inst & 0xff, (inst>>8)&0xff, (inst>>16)&0xff, (inst>>24)&0xff))
")
echo "Target: max main-stream bitrate = ${KBPS} kbps"
echo "Will write: $PATCH_HEX to router file offset 0x6351c (stock: 0b 00 86 52)"
echo

echo "==> 1) Extract app section"
python3 - <<PY
import struct
data = open("$STOCK","rb").read()
base = 0x18 + 7*0x48
off = struct.unpack("<Q", data[base+0x38:base+0x40])[0]
sz  = struct.unpack("<Q", data[base+0x40:base+0x48])[0]
open("app_stock.bin","wb").write(data[off:off+sz])
PY

echo "==> 2) Unsquashfs"
unsquashfs -d app_unpacked -no-progress app_stock.bin >/dev/null

echo "==> 3) Carry the /downloadfile/ HTTP unlock (same bytes as build_http_unlock.sh)"
python3 - <<'PY'
SRC = (b"location /downloadfile/ {\n"
       b"            internal;\n"
       b"            limit_conn one 1;\n"
       b"            limit_rate 1024k;\n"
       b"            alias /mnt/sda/;\n"
       b"        }")
DST = (b"location /downloadfile/ {\n"
       b"           #internal;\n"
       b"            limit_conn one 1;\n"
       b"            limit_rate 0;    \n"
       b"            alias /mnt/sda/;\n"
       b"        }")
data = bytearray(open("app_unpacked/device","rb").read())
assert data.count(SRC) == 2
open("app_unpacked/device","wb").write(bytes(data).replace(SRC, DST))
print("   device /downloadfile/ unlocked")
PY

echo "==> 4) Patch router bitrate cap instruction"
python3 - <<PY
PATCH_OFFSET = 0x6351c
SRC_BYTES = bytes.fromhex("0b008652")          # mov w11, #0x3000 (=12288)
inst = 0x5280000B | ($KBPS << 5)
DST_BYTES = bytes([inst & 0xff, (inst>>8)&0xff, (inst>>16)&0xff, (inst>>24)&0xff])
data = bytearray(open("app_unpacked/router","rb").read())
actual = bytes(data[PATCH_OFFSET:PATCH_OFFSET+4])
assert actual == SRC_BYTES, f"router[{hex(PATCH_OFFSET)}] mismatch: got {actual.hex()}, expected {SRC_BYTES.hex()}. Firmware may have changed; stop."
data[PATCH_OFFSET:PATCH_OFFSET+4] = DST_BYTES
open("app_unpacked/router","wb").write(bytes(data))
print(f"   router bitrate cap: {SRC_BYTES.hex()} -> {DST_BYTES.hex()} (={$KBPS} kbps)")
PY

echo "==> 5) Repack app squashfs"
mksquashfs app_unpacked app_new.bin \
    -comp xz -b 262144 -noappend -no-progress \
    -no-exports -all-root -mkfs-time 0 -all-time 0 \
    >/dev/null

echo "==> 6) Repack pak"
PYTHONPATH="$PAK_DIR" python3 "$PAK_DIR/pak_repack.py" "$STOCK" "$OUT" app app_new.bin

echo "==> 7) Verify CRC"
python3 "$PAK_DIR/reolink_crc.py" compute "$OUT"

echo
echo "====================================================================="
echo " Build complete: $OUT"
echo "  - HTTP /downloadfile/ unlock: carried"
echo "  - Main-stream max bitrate:    $KBPS kbps (was 12288 kbps stock)"
echo
echo " After flashing, verify via camera API:"
echo "   GetEnc action=1 -> range.Enc[0].mainStream.bitRate should now"
echo "   end in $KBPS instead of 12288."
echo
echo " Then try SetEnc bitRate=$KBPS. If it returns rspCode -13, the"
echo " encoder hardware refuses — lower the target and rebuild."
echo "====================================================================="
