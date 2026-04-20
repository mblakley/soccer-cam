#!/bin/bash
# Build a patched .pak that unlocks the HTTP `/downloadfile/` path.
#
# What it does (in the `device` ELF inside the `app` SquashFS):
#   Original nginx block:
#       location /downloadfile/ {
#           internal;               <-- CGI-only, nobody generates the redirect
#           limit_conn one 1;
#           limit_rate 1024k;       <-- throttles to 1 MB/s
#           alias /mnt/sda/;
#       }
#   Patched nginx block (same length, same offsets):
#       location /downloadfile/ {
#          #internal;                <-- commented out, externally accessible
#           limit_conn one 1;
#           limit_rate 0;            <-- 0 = unlimited (nginx semantics)
#           alias /mnt/sda/;
#       }
# Effect: direct GET http://cam/downloadfile/<path> returns files at
# ~86 Mbps (saturates 100 Mbps PoE). No auth on this path.
#
# Caveats:
#   - `limit_conn one 1` is still in place: one concurrent download per
#     source IP.
#   - No authentication on the unlocked path. Don't expose the camera to
#     untrusted networks.
#
# Usage:
#   sudo bash build_http_unlock.sh <stock.pak> <output.pak>
#
# Dependencies (WSL Ubuntu):
#   sudo apt install squashfs-tools
set -euo pipefail

STOCK="${1:?usage: $0 <stock.pak> <output.pak>}"
OUT="${2:?usage: $0 <stock.pak> <output.pak>}"
[[ "$EUID" -eq 0 ]] || { echo "ERROR: run as root (unsquashfs/mksquashfs need it)"; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAK_DIR="$(cd "$HERE/../pak" && pwd)"
WORK="$(mktemp -d)"
trap "rm -rf '$WORK'" EXIT
cd "$WORK"

echo "==> 1) Extract app section from $STOCK"
python3 - <<PY
import struct
data = open("$STOCK","rb").read()
base = 0x18 + 7*0x48   # app is section index 7
off = struct.unpack("<Q", data[base+0x38:base+0x40])[0]
sz  = struct.unpack("<Q", data[base+0x40:base+0x48])[0]
print(f"   app section: off=0x{off:x} size=0x{sz:x}")
open("app_stock.bin","wb").write(data[off:off+sz])
PY

echo "==> 2) Unsquashfs"
unsquashfs -d app_unpacked -no-progress app_stock.bin >/dev/null

echo "==> 3) Patch /downloadfile/ in device binary"
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
assert len(SRC) == len(DST) == 146, f"len mismatch: src={len(SRC)} dst={len(DST)}"
data = bytearray(open("app_unpacked/device","rb").read())
n = data.count(SRC)
assert n == 2, f"expected 2 occurrences of the stock block, found {n}. Firmware may have changed; stop."
new = bytes(data).replace(SRC, DST)
assert len(new) == len(data)
open("app_unpacked/device","wb").write(new)
print(f"   patched. size unchanged: {len(new)} bytes")
PY

echo "==> 4) Repack app squashfs (deterministic: xz, 256K, all-root, no exports, mkfs-time=0)"
mksquashfs app_unpacked app_new.bin \
    -comp xz -b 262144 -noappend -no-progress \
    -no-exports -all-root -mkfs-time 0 -all-time 0 \
    >/dev/null

echo "==> 5) Repack pak with new app section"
PYTHONPATH="$PAK_DIR" python3 "$PAK_DIR/pak_repack.py" "$STOCK" "$OUT" app app_new.bin

echo "==> 6) Verify CRC"
python3 "$PAK_DIR/reolink_crc.py" compute "$OUT"

echo
echo "====================================================================="
echo " Build complete: $OUT"
echo " Flash via the camera's web UI -> Settings -> Maintenance -> Upgrade."
echo " If flash fails: original $STOCK is untouched, re-flash it to recover."
echo "====================================================================="
