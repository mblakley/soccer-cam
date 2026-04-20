#!/bin/bash
# Build a patched .pak that raises the userspace fps hardcode from 20 to a
# higher value for the OS08C10 sensor path in the `device` binary.
#
# What it does (in the `device` ELF inside the `app` SquashFS):
#   File offset 0x8bb1c in `device`:
#     before: 81 02 80 52   ; movz w1, #0x14   (= 20)
#     after:  ?? ?? 80 52   ; movz w1, #<new>
#   where the low two bytes are derived from the MOVZ W1 encoding:
#     inst = 0x52800001 | (imm16 << 5)
#
# This instruction is in Na_video_encoder_build_basic (Nvt52xAdapter.cpp,
# Ghidra: FUN_0048b630). The 4-byte str that follows (`str w1, [x19,
# #0x2884]`) writes the fps into the video-encoder config object; that
# value is then copied into cap_path[+0x34] and sent to the kernel as
# HD_VIDEOCAP_IN.frc = (fps<<16)|1 via hd_videocap_set(HD_VIDEOCAP_PARAM_IN
# = 0x80001016, ...).
#
# The hardcode is gated on the sensor type mask 0x1080c241, which matches
# the OS08C10 (sensor type 0x26) that the Duo 3 ships with, so this patch
# is specific to this hardware. Other sensors keep their existing fps
# defaults (loaded from DAT_00736870 / DAT_007314b0 instead of being
# overwritten with 20).
#
# This build also carries the HTTP /downloadfile/ unlock and the
# bitrate-cap patch (all three touch different files and compose cleanly).
#
# Usage:
#   sudo bash build_fps_cap.sh <stock.pak> <output.pak> <fps> <kbps>
# Example:
#   sudo bash build_fps_cap.sh stock.pak duo3_fps30_br20.pak 30 20480
#
# Constraints on fps:
#   Must be a positive integer <= 255 (fits in MOVZ imm16 trivially).
#   Try 25 first, then 30. 60 is a stretch goal -- sensor should support
#   it electrically, but SIE/VIE bandwidth may refuse.
set -euo pipefail

STOCK="${1:?usage: $0 <stock.pak> <output.pak> <fps> <kbps>}"
OUT="${2:?usage: $0 <stock.pak> <output.pak> <fps> <kbps>}"
FPS="${3:?usage: $0 <stock.pak> <output.pak> <fps> <kbps>}"
KBPS="${4:?usage: $0 <stock.pak> <output.pak> <fps> <kbps>}"
[[ "$EUID" -eq 0 ]] || { echo "ERROR: run as root"; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAK_DIR="$(cd "$HERE/../pak" && pwd)"
WORK="$(mktemp -d)"
trap "rm -rf '$WORK'" EXIT
cd "$WORK"

# Encode the fps immediate as the ARM64 MOVZ W1 instruction bytes
FPS_PATCH_HEX=$(python3 -c "
imm = $FPS
assert 0 < imm < 0x100, 'fps must be positive and fit in a byte'
inst = 0x52800001 | (imm << 5)
print('%02x %02x %02x %02x' % (inst & 0xff, (inst>>8)&0xff, (inst>>16)&0xff, (inst>>24)&0xff))
")
echo "Target: fps hardcode = ${FPS}"
echo "Will write: $FPS_PATCH_HEX to device file offset 0x8bb1c (stock: 81 02 80 52)"
echo "Target: main-stream max bitrate = ${KBPS} kbps"
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

echo "==> 3) Carry the /downloadfile/ HTTP unlock"
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

echo "==> 4) Patch device fps hardcode"
python3 - <<PY
PATCH_OFFSET = 0x8bb1c
SRC_BYTES = bytes.fromhex("81028052")          # movz w1, #0x14 (=20)
inst = 0x52800001 | ($FPS << 5)
DST_BYTES = bytes([inst & 0xff, (inst>>8)&0xff, (inst>>16)&0xff, (inst>>24)&0xff])
data = bytearray(open("app_unpacked/device","rb").read())
actual = bytes(data[PATCH_OFFSET:PATCH_OFFSET+4])
assert actual == SRC_BYTES, f"device[{hex(PATCH_OFFSET)}] mismatch: got {actual.hex()}, expected {SRC_BYTES.hex()}. Firmware may have changed; stop."
data[PATCH_OFFSET:PATCH_OFFSET+4] = DST_BYTES
open("app_unpacked/device","wb").write(bytes(data))
print(f"   device fps hardcode: {SRC_BYTES.hex()} -> {DST_BYTES.hex()} (= {$FPS} fps)")
PY

echo "==> 5) Patch router bitrate cap instruction"
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
print(f"   router bitrate cap: {SRC_BYTES.hex()} -> {DST_BYTES.hex()} (= {$KBPS} kbps)")
PY

echo "==> 5b) Patch router fps dropdown max"
# router FUN_00465584 builds the fps-range array the GetEnc API returns.
# Last entry is 'movz w0, #0x14' (= 20 fps max shown in the web UI dropdown).
# Patch to the same fps target so the UI lets the user SELECT the higher value.
python3 - <<PY
PATCH_OFFSET = 0x6565c
SRC_BYTES = bytes.fromhex("80028052")          # movz w0, #0x14 (=20)
inst = 0x52800000 | ($FPS << 5)
DST_BYTES = bytes([inst & 0xff, (inst>>8)&0xff, (inst>>16)&0xff, (inst>>24)&0xff])
data = bytearray(open("app_unpacked/router","rb").read())
actual = bytes(data[PATCH_OFFSET:PATCH_OFFSET+4])
assert actual == SRC_BYTES, f"router[{hex(PATCH_OFFSET)}] mismatch: got {actual.hex()}, expected {SRC_BYTES.hex()}. Firmware may have changed; stop."
data[PATCH_OFFSET:PATCH_OFFSET+4] = DST_BYTES
open("app_unpacked/router","wb").write(bytes(data))
print(f"   router fps dropdown: {SRC_BYTES.hex()} -> {DST_BYTES.hex()} (= {$FPS} fps max)")
PY

echo "==> 5c) Patch per-resolution max-fps table in FUN_004632b0"
# FUN_00465bc4 asks FUN_004632b0 to populate a per-resolution config table
# (one 0x14c-byte entry per resolution). Each entry has max_fps at offset 0x3c.
# FUN_004632b0 writes that value via 'mov w0, #N; stur w0, [x28, #-0xbc]'
# across multiple resolution branches. Stock caps every sub-7680x2160 resolution
# at 20. Patch every 'mov w0, #0x14' in FUN_004632b0 to 'mov w0, #<fps>' so
# the per-resolution dropdowns offer the higher fps too.
python3 - <<PY
# VMA offsets of 'mov w0, #0x14' sites in FUN_004632b0 (0x004632b0..0x0046459f).
# All 9 were verified by direct byte search (80 02 80 52 at file offset).
SITES = [0x637fc, 0x63c48, 0x63c68, 0x63cd4, 0x63ce4,
         0x63ddc, 0x63e8c, 0x640ac, 0x64384]
SRC_BYTES = bytes.fromhex("80028052")          # mov w0, #0x14 (=20)
inst = 0x52800000 | ($FPS << 5)
DST_BYTES = bytes([inst & 0xff, (inst>>8)&0xff, (inst>>16)&0xff, (inst>>24)&0xff])
data = bytearray(open("app_unpacked/router","rb").read())
patched = 0
for off in SITES:
    actual = bytes(data[off:off+4])
    if actual != SRC_BYTES:
        print(f"   WARNING: router[{hex(off)}] mismatch: got {actual.hex()}, expected {SRC_BYTES.hex()} -- skipping")
        continue
    data[off:off+4] = DST_BYTES
    patched += 1
open("app_unpacked/router","wb").write(bytes(data))
print(f"   per-resolution fps table: patched {patched}/{len(SITES)} sites to {$FPS} fps")
PY

echo "==> 6) Repack app squashfs"
mksquashfs app_unpacked app_new.bin \
    -comp xz -b 262144 -noappend -no-progress \
    -no-exports -all-root -mkfs-time 0 -all-time 0 \
    >/dev/null

echo "==> 7) Repack pak"
PYTHONPATH="$PAK_DIR" python3 "$PAK_DIR/pak_repack.py" "$STOCK" "$OUT" app app_new.bin

echo "==> 8) Verify CRC"
python3 "$PAK_DIR/reolink_crc.py" compute "$OUT"

echo
echo "====================================================================="
echo " Build complete: $OUT"
echo "  - HTTP /downloadfile/ unlock: carried"
echo "  - Main-stream max bitrate:    $KBPS kbps (was 12288 kbps stock)"
echo "  - FPS hardcode (OS08C10):     $FPS fps (was 20 stock)"
echo "  - FPS dropdown max:           $FPS fps (was 20 stock)"
echo
echo " After flashing, verify via camera API:"
echo "   GetEnc action=0 -> mainStream.frameRate should be <= $FPS and the"
echo "   observed encoded stream (ffprobe on a recording) should match the"
echo "   requested frameRate. If it still shows ~20 fps, the downstream"
echo "   SIE/VIE bandwidth limiter rejected the higher request."
echo "====================================================================="
