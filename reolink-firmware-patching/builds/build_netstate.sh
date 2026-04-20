#!/bin/bash
# Build a patched .pak that:
#   - Carries the daily-driver patches (HTTP /downloadfile/ unlock + bitrate cap)
#   - Installs an /etc/init.d/S99_NetState script that toggles scheduled
#     recording based on the default-gateway MAC address.
#
# How it works on the camera:
#   - On boot, S99_NetState runs after the network is up.
#   - It polls the gateway MAC every 30s and compares against the home MAC list.
#   - If MAC matches a "home" entry => disables the TIMING recording schedule.
#   - If MAC does not match => enables continuous TIMING recording.
#   - Decisions are logged to /mnt/sda/netstate/log (auto-rotates at 256 KB).
#
# Runtime override (no re-flash needed):
#   - /mnt/sda/netstate/home_macs.txt: one MAC per line, replaces baked-in list.
#   - /mnt/sda/netstate/override:      presence => daemon yields, you control
#                                       recording via the API/UI as normal.
#
# Usage:
#   sudo bash build_netstate.sh <stock.pak> <output.pak> <kbps> <user> <pass> <home_mac> [more_macs...]
# Example (your current home gateway):
#   sudo bash build_netstate.sh stock.pak duo3_netstate.pak 20480 admin <PW> aa:bb:cc:dd:ee:ff
set -euo pipefail

STOCK="${1:?usage: $0 <stock.pak> <output.pak> <kbps> <user> <pass> <home_mac> [more_macs...]}"
OUT="${2:?usage}"
KBPS="${3:?usage}"
USER="${4:?usage}"
PASS="${5:?usage}"
shift 5
HOME_MACS="$*"
[[ -n "$HOME_MACS" ]] || { echo "ERROR: at least one home MAC required"; exit 1; }
[[ "$EUID" -eq 0 ]] || { echo "ERROR: run as root"; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAK_DIR="$(cd "$HERE/../pak" && pwd)"
TEMPLATE="$HERE/../runtime/netstate/S99_NetState.template"
[[ -f "$TEMPLATE" ]] || { echo "ERROR: template not found at $TEMPLATE"; exit 1; }
WORK="$(mktemp -d)"
trap "rm -rf '$WORK'" EXIT
cd "$WORK"

# Lower-case all MACs for consistent matching
HOME_MACS_LC=$(echo "$HOME_MACS" | tr 'A-Z' 'a-z')

echo "Target: HTTP unlock + bitrate=${KBPS} + netstate(home_macs=$HOME_MACS_LC)"
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

echo "==> 2) Extract rootfs section"
python3 - <<PY
import struct
data = open("$STOCK","rb").read()
base = 0x18 + 5*0x48     # section index 5 = rootfs
off = struct.unpack("<Q", data[base+0x38:base+0x40])[0]
sz  = struct.unpack("<Q", data[base+0x40:base+0x48])[0]
open("rootfs_stock.bin","wb").write(data[off:off+sz])
print(f"  rootfs orig size: {sz} bytes")
PY

echo "==> 3) Unsquashfs both"
unsquashfs -d app_unpacked  -no-progress app_stock.bin  >/dev/null
unsquashfs -d rootfs_unpacked -no-progress rootfs_stock.bin >/dev/null

echo "==> 4) Carry the /downloadfile/ HTTP unlock (in 'device' inside app)"
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
assert data.count(SRC) == 2, f"expected 2 hits, got {data.count(SRC)}"
open("app_unpacked/device","wb").write(bytes(data).replace(SRC, DST))
print("   device /downloadfile/ unlocked")
PY

echo "==> 5) Patch router bitrate cap instruction"
python3 - <<PY
PATCH_OFFSET = 0x6351c
SRC_BYTES = bytes.fromhex("0b008652")   # mov w11, #0x3000
inst = 0x5280000B | ($KBPS << 5)
DST_BYTES = bytes([inst & 0xff, (inst>>8)&0xff, (inst>>16)&0xff, (inst>>24)&0xff])
data = bytearray(open("app_unpacked/router","rb").read())
actual = bytes(data[PATCH_OFFSET:PATCH_OFFSET+4])
assert actual == SRC_BYTES, f"router[{hex(PATCH_OFFSET)}] mismatch: got {actual.hex()}"
data[PATCH_OFFSET:PATCH_OFFSET+4] = DST_BYTES
open("app_unpacked/router","wb").write(bytes(data))
print(f"   router bitrate cap: {SRC_BYTES.hex()} -> {DST_BYTES.hex()} ({$KBPS} kbps)")
PY

echo "==> 6) Render S99_NetState init script with baked-in config"
python3 - <<PY
template = open("$TEMPLATE").read()
out = (template
       .replace("%%HOME_MACS%%",   "$HOME_MACS_LC")
       .replace("%%CAMERA_USER%%", "$USER")
       .replace("%%CAMERA_PASS%%", "$PASS"))
import os
os.makedirs("rootfs_unpacked/etc/init.d", exist_ok=True)
target = "rootfs_unpacked/etc/init.d/S99_NetState"
with open(target, "w", newline='\n') as f:
    f.write(out)
os.chmod(target, 0o755)
print(f"   wrote {target}  ({len(out)} bytes)")
PY

echo "==> 7) Repack app squashfs"
mksquashfs app_unpacked app_new.bin \
    -comp xz -b 262144 -noappend -no-progress \
    -no-exports -all-root -mkfs-time 0 -all-time 0 \
    >/dev/null

echo "==> 8) Repack rootfs squashfs"
mksquashfs rootfs_unpacked rootfs_new.bin \
    -comp xz -b 262144 -noappend -no-progress \
    -no-exports -all-root -mkfs-time 0 -all-time 0 \
    >/dev/null

echo "==> 9) Repack pak (replace BOTH app and rootfs sections)"
PYTHONPATH="$PAK_DIR" python3 - <<PY
from pak_repack import repack
swaps = {
    "rootfs": open("rootfs_new.bin", "rb").read(),
    "app":    open("app_new.bin",    "rb").read(),
}
crc, size, secs = repack("$STOCK", "$OUT", swaps=swaps)
print(f"wrote $OUT  size={size}  crc=0x{crc:08x}")
for name, off, sz in secs:
    marker = "  (replaced)" if name in swaps else ""
    print(f"  {name:10s} start=0x{off:08x} size=0x{sz:08x}{marker}")
PY

echo "==> 10) Verify CRC"
python3 "$PAK_DIR/reolink_crc.py" compute "$OUT"

echo
echo "====================================================================="
echo " Build complete: $OUT"
echo "  - HTTP /downloadfile/ unlock:   carried"
echo "  - Main-stream max bitrate:       $KBPS kbps (was 12288)"
echo "  - /etc/init.d/S99_NetState:      installed"
echo "      home MACs (idle):  $HOME_MACS_LC"
echo "      api creds:         $USER / <hidden>"
echo
echo " First boot after flash:"
echo "  - Daemon waits ${INIT_GRACE:-45}s for the API, then begins polling."
echo "  - If on a 'home' MAC, it disables the TIMING recording schedule."
echo "  - On any other LAN, it enables continuous TIMING recording."
echo
echo " Runtime overrides (edit on SD card, no re-flash):"
echo "  /mnt/sda/netstate/home_macs.txt  - one MAC per line, overrides baked list"
echo "  /mnt/sda/netstate/override       - presence => daemon yields"
echo "  /mnt/sda/netstate/log            - decision log (auto-rotates at 256 KB)"
echo "====================================================================="
