#!/bin/bash
# Comprehensive soccer-cam firmware. Layers on STOCK, everything in one pak:
#   - HTTP /downloadfile/ unlock                       (device, app)
#   - Main-stream bitrate cap -> <kbps>                (router, app)
#   - Free-space reserve 500MiB -> <reserve_gb> GiB    (libStorageFileManager, app)  [fixes truncation]
#   - /etc/init.d/S99_NetState v2 (home/away + stub cleanup)        (rootfs)
#   - /usr/bin/recover_mp4 (static aarch64 reindexer)               (rootfs)         [power-cut recovery]
#   - /etc/init.d/S35_RecRecover (boot recovery, runs before scan)  (rootfs)
#
# No sudo needed (unsquashfs -no-xattrs, mksquashfs -all-root).
# Usage: bash build_soccercam_comprehensive.sh <stock.pak> <out.pak> <kbps> <user> <pass> <reserve_gb> <home_mac> [more_macs...]
set -euo pipefail
STOCK="${1:?}"; OUT="${2:?}"; KBPS="${3:?}"; USER="${4:?}"; PASS="${5:?}"; RES_GB="${6:?}"; shift 6
HOME_MACS="$*"; [[ -n "$HOME_MACS" ]] || { echo "ERROR: home MAC required"; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAK_DIR="$(cd "$HERE/../pak" && pwd)"
NS_TPL="$HERE/../runtime/netstate/S99_NetState_v2.template"
REC_SH="$HERE/../runtime/recover/S35_RecRecover"
REC_C="$HERE/../recover/recover_mp4.c"
for x in "$NS_TPL" "$REC_SH" "$REC_C"; do [[ -f "$x" ]] || { echo "ERROR: missing $x"; exit 1; }; done
command -v aarch64-linux-gnu-gcc >/dev/null || { echo "ERROR: need aarch64-linux-gnu-gcc"; exit 1; }
WORK="$(mktemp -d)"; trap "rm -rf '$WORK'" EXIT; cd "$WORK"
HOME_MACS_LC=$(echo "$HOME_MACS" | tr 'A-Z' 'a-z')

echo "==> 1) extract app + rootfs"
python3 - "$STOCK" <<'PY'
import struct,sys
d=open(sys.argv[1],"rb").read()
for i,n in [(5,"rootfs"),(7,"app")]:
    b=0x18+i*0x48; off=struct.unpack("<Q",d[b+0x38:b+0x40])[0]; sz=struct.unpack("<Q",d[b+0x40:b+0x48])[0]
    open(n+"_stock.bin","wb").write(d[off:off+sz])
PY
unsquashfs -no-xattrs -d app_unpacked    -no-progress app_stock.bin    >/dev/null
unsquashfs -no-xattrs -d rootfs_unpacked -no-progress rootfs_stock.bin >/dev/null

echo "==> 2) HTTP /downloadfile/ unlock"
python3 - <<'PY'
SRC=(b"location /downloadfile/ {\n            internal;\n            limit_conn one 1;\n            limit_rate 1024k;\n            alias /mnt/sda/;\n        }")
DST=(b"location /downloadfile/ {\n           #internal;\n            limit_conn one 1;\n            limit_rate 0;    \n            alias /mnt/sda/;\n        }")
d=bytearray(open("app_unpacked/device","rb").read())
assert d.count(SRC)==2, f"http: expected 2, got {d.count(SRC)}"
open("app_unpacked/device","wb").write(bytes(d).replace(SRC,DST)); print("   http unlocked")
PY

echo "==> 3) bitrate cap -> ${KBPS}"
python3 - <<PY
OFF=0x6351c; SRC=bytes.fromhex("0b008652"); inst=0x5280000B|($KBPS<<5)
DST=bytes([inst&0xff,(inst>>8)&0xff,(inst>>16)&0xff,(inst>>24)&0xff])
d=bytearray(open("app_unpacked/router","rb").read())
assert bytes(d[OFF:OFF+4])==SRC, "bitrate site mismatch"
d[OFF:OFF+4]=DST; open("app_unpacked/router","wb").write(bytes(d)); print(f"   bitrate {$KBPS}")
PY

echo "==> 4) free-space reserve 500MiB -> ${RES_GB}GiB"
python3 - <<PY
OFF=0x44788; CUR=(0xd2a3e800).to_bytes(4,"little"); SO="app_unpacked/libStorageFileManager.so"
V=$RES_GB*(1<<30); enc=None
for sh in (0,16,32,48):
    imm=V>>sh
    if (imm<<sh)==V and 0<=imm<=0xffff: enc=(imm,sh); break
assert enc, f"{V} not single-movz"
imm,sh=enc; word=0xD2800000|((sh//16)<<21)|(imm<<5); new=word.to_bytes(4,"little")
d=bytearray(open(SO,"rb").read())
assert bytes(d[OFF:OFF+4])==CUR, "reserve site mismatch"
d[OFF:OFF+4]=new; open(SO,"wb").write(bytes(d)); print(f"   reserve {$RES_GB}GiB ({new.hex()})")
PY

echo "==> 5) install S99_NetState v2"
python3 - <<PY
t=open("$NS_TPL").read().replace("%%HOME_MACS%%","$HOME_MACS_LC").replace("%%CAMERA_USER%%","$USER").replace("%%CAMERA_PASS%%","$PASS")
import os; os.makedirs("rootfs_unpacked/etc/init.d",exist_ok=True)
open("rootfs_unpacked/etc/init.d/S99_NetState","w",newline="\n").write(t); os.chmod("rootfs_unpacked/etc/init.d/S99_NetState",0o755)
print(f"   S99_NetState ({len(t)}b)")
PY

echo "==> 6) build + install recover_mp4 (static aarch64) + boot script"
HELIX_SRC="$HERE/../recover/helix/ESP8266Audio/src/libhelix-aac"
HELIX_COMPAT="$HERE/../recover/helix/compat"
if [[ -d "$HELIX_SRC" && -f "$HELIX_COMPAT/Arduino.h" ]]; then
  echo "   building libhelix-aac -> recover_mp4 WITH best-effort audio recovery"
  HXB="$WORK/hx"; mkdir -p "$HXB"
  for c in "$HELIX_SRC"/*.c; do
    aarch64-linux-gnu-gcc -O2 -DNDEBUG -DARDUINO -Wno-format -ffunction-sections -fdata-sections \
      -I"$HELIX_COMPAT" -I"$HELIX_SRC" -c "$c" -o "$HXB/$(basename "$c" .c).o"
  done
  aarch64-linux-gnu-ar rcs "$HXB/libhelixaac.a" "$HXB"/*.o
  aarch64-linux-gnu-gcc -O2 -DNDEBUG -DARDUINO -static \
    -I"$HELIX_COMPAT" -I"$HELIX_SRC" \
    -o rootfs_unpacked/usr/bin/recover_mp4 "$REC_C" "$HXB/libhelixaac.a"
else
  echo "   WARNING: Helix AAC source absent -> VIDEO-ONLY recovery (-DNO_AUDIO)"
  aarch64-linux-gnu-gcc -O2 -DNDEBUG -DNO_AUDIO -static -o rootfs_unpacked/usr/bin/recover_mp4 "$REC_C"
fi
chmod 755 rootfs_unpacked/usr/bin/recover_mp4
file rootfs_unpacked/usr/bin/recover_mp4 | cut -d, -f1-3
install -m 0755 "$REC_SH" rootfs_unpacked/etc/init.d/S35_RecRecover
echo "   recover_mp4 + S35_RecRecover installed"

echo "==> 7) repack app + rootfs"
mksquashfs app_unpacked    app_new.bin    -comp xz -b 262144 -noappend -no-progress -no-exports -all-root -mkfs-time 0 -all-time 0 >/dev/null
mksquashfs rootfs_unpacked rootfs_new.bin -comp xz -b 262144 -noappend -no-progress -no-exports -all-root -mkfs-time 0 -all-time 0 >/dev/null

echo "==> 8) repack pak"
PYTHONPATH="$PAK_DIR" python3 - <<PY
from pak_repack import repack
crc,size,_=repack("$STOCK","$OUT",swaps={"rootfs":open("rootfs_new.bin","rb").read(),"app":open("app_new.bin","rb").read()})
print(f"   wrote $OUT size={size} crc=0x{crc:08x}")
PY
echo "==> 9) verify CRC"; python3 "$PAK_DIR/reolink_crc.py" compute "$OUT"
echo "==================================================================="
echo " COMPREHENSIVE: HTTP+${KBPS}kbps + reserve ${RES_GB}GiB + netstate-v2 + power-cut recovery"
echo " Flash via web UI. Recover with RECOVERY/*.pak if needed."
echo "==================================================================="
