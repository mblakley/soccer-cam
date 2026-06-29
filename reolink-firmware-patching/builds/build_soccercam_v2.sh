#!/bin/bash
# Build the soccer-cam v2 patched .pak. Layers on STOCK:
#   - HTTP /downloadfile/ unlock        (device, app)        [carried from v1]
#   - Main-stream max bitrate -> <kbps> (router, app)        [carried from v1]
#   - /etc/init.d/S99_NetState  v2      (rootfs)  home/away recording + HOME-STUB CLEANUP
#   - /etc/init.d/S98_SdKeep            (rootfs)  proactive SD headroom (fixes truncation)
#
# No sudo required: unsquashfs runs with -no-xattrs and mksquashfs with -all-root.
#
# Usage:
#   bash build_soccercam_v2.sh <stock.pak> <out.pak> <kbps> <user> <pass> \
#        <min_free_gb> <target_gb> <home_mac> [more_macs...]
# Example:
#   bash build_soccercam_v2.sh stock.pak duo3_v2.pak 20480 admin <PW> 30 40 <home_router_mac>
set -euo pipefail

STOCK="${1:?usage: see header}"
OUT="${2:?usage}"
KBPS="${3:?usage}"
USER="${4:?usage}"
PASS="${5:?usage}"
MIN_FREE_GB="${6:?usage}"
TARGET_GB="${7:?usage}"
shift 7
HOME_MACS="$*"
[[ -n "$HOME_MACS" ]] || { echo "ERROR: at least one home MAC required"; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAK_DIR="$(cd "$HERE/../pak" && pwd)"
NS_TPL="$HERE/../runtime/netstate/S99_NetState_v2.template"
SK_TPL="$HERE/../runtime/sdkeep/S98_SdKeep.template"
[[ -f "$NS_TPL" ]] || { echo "ERROR: missing template $NS_TPL"; exit 1; }
WORK="$(mktemp -d)"; trap "rm -rf '$WORK'" EXIT; cd "$WORK"
HOME_MACS_LC=$(echo "$HOME_MACS" | tr 'A-Z' 'a-z')

echo "==> 1) Extract app + rootfs sections from stock"
python3 - "$STOCK" <<'PY'
import struct,sys
d=open(sys.argv[1],"rb").read()
for i,n in [(5,"rootfs"),(7,"app")]:
    b=0x18+i*0x48
    off=struct.unpack("<Q",d[b+0x38:b+0x40])[0]; sz=struct.unpack("<Q",d[b+0x40:b+0x48])[0]
    open(n+"_stock.bin","wb").write(d[off:off+sz]); print(f"  {n}: off=0x{off:x} size={sz}")
PY

echo "==> 2) Unsquashfs both (no-xattrs, non-root)"
unsquashfs -no-xattrs -d app_unpacked  -no-progress app_stock.bin    >/dev/null
unsquashfs -no-xattrs -d rootfs_unpacked -no-progress rootfs_stock.bin >/dev/null

echo "==> 3) HTTP /downloadfile/ unlock (device)"
python3 - <<'PY'
SRC=(b"location /downloadfile/ {\n            internal;\n            limit_conn one 1;\n            limit_rate 1024k;\n            alias /mnt/sda/;\n        }")
DST=(b"location /downloadfile/ {\n           #internal;\n            limit_conn one 1;\n            limit_rate 0;    \n            alias /mnt/sda/;\n        }")
assert len(SRC)==len(DST), (len(SRC),len(DST))
d=bytearray(open("app_unpacked/device","rb").read())
assert d.count(SRC)==2, f"expected 2 nginx blocks, found {d.count(SRC)} -- firmware changed; stop"
open("app_unpacked/device","wb").write(bytes(d).replace(SRC,DST)); print("   device unlocked")
PY

echo "==> 4) Bitrate cap (router) -> ${KBPS} kbps"
python3 - <<PY
OFF=0x6351c; SRC=bytes.fromhex("0b008652")
inst=0x5280000B | ($KBPS<<5)
DST=bytes([inst&0xff,(inst>>8)&0xff,(inst>>16)&0xff,(inst>>24)&0xff])
d=bytearray(open("app_unpacked/router","rb").read())
assert bytes(d[OFF:OFF+4])==SRC, f"router[{hex(OFF)}]={bytes(d[OFF:OFF+4]).hex()} != stock; stop"
d[OFF:OFF+4]=DST; open("app_unpacked/router","wb").write(bytes(d)); print(f"   router cap -> {DST.hex()} ({$KBPS})")
PY

echo "==> 5) Install S99_NetState v2 (home/away + stub cleanup)"
python3 - <<PY
t=open("$NS_TPL").read().replace("%%HOME_MACS%%","$HOME_MACS_LC").replace("%%CAMERA_USER%%","$USER").replace("%%CAMERA_PASS%%","$PASS")
import os
os.makedirs("rootfs_unpacked/etc/init.d",exist_ok=True)
open("rootfs_unpacked/etc/init.d/S99_NetState","w",newline="\n").write(t)
os.chmod("rootfs_unpacked/etc/init.d/S99_NetState",0o755); print(f"   S99_NetState ({len(t)} bytes)")
PY

echo "==> 6) RESERVE patch: libStorageFileManager.so free-space reserve 500MiB -> ${MIN_FREE_GB}GiB"
python3 - <<PY
OFF=0x44788                      # Get_storage_space free-space threshold (only occurrence)
CUR=(0xd2a3e800).to_bytes(4,"little")   # movz x0,#0x1f40,lsl#16 = 0x1f400000 = 500 MiB (LE in file)
SO="app_unpacked/libStorageFileManager.so"
V=$MIN_FREE_GB*(1<<30)
enc=None
for sh in (0,16,32,48):
    imm=V>>sh
    if (imm<<sh)==V and 0<=imm<=0xffff:
        enc=(imm,sh); break
assert enc, f"{V} bytes not single-movz encodable; pick a reserve like 16/20/32 GiB"
imm,sh=enc
word=0xD2800000 | ((sh//16)<<21) | (imm<<5)   # movz x0,#imm,lsl#sh
new=word.to_bytes(4,"little")
d=bytearray(open(SO,"rb").read())
assert bytes(d[OFF:OFF+4])==CUR, f"{SO}[{hex(OFF)}]={bytes(d[OFF:OFF+4]).hex()} != stock 500MiB reserve; firmware changed, stop"
d[OFF:OFF+4]=new; open(SO,"wb").write(bytes(d))
print(f"   reserve 500MiB -> {$MIN_FREE_GB}GiB  (d2a3e800 -> {new.hex()}, movz x0,#{hex(imm)},lsl#{sh})")
PY

echo "==> 7) Repack app + rootfs squashfs (deterministic, all-root)"
mksquashfs app_unpacked    app_new.bin    -comp xz -b 262144 -noappend -no-progress -no-exports -all-root -mkfs-time 0 -all-time 0 >/dev/null
mksquashfs rootfs_unpacked rootfs_new.bin -comp xz -b 262144 -noappend -no-progress -no-exports -all-root -mkfs-time 0 -all-time 0 >/dev/null

echo "==> 8) Repack pak (swap app + rootfs)"
PYTHONPATH="$PAK_DIR" python3 - <<PY
from pak_repack import repack
swaps={"rootfs":open("rootfs_new.bin","rb").read(),"app":open("app_new.bin","rb").read()}
crc,size,secs=repack("$STOCK","$OUT",swaps=swaps)
print(f"   wrote $OUT size={size} crc=0x{crc:08x}")
PY

echo "==> 9) Verify CRC"
python3 "$PAK_DIR/reolink_crc.py" compute "$OUT"
echo
echo "===================================================================="
echo " soccer-cam v2 built: $OUT"
echo "  HTTP unlock + ${KBPS}kbps + netstate-v2(stub-clean) + free-space reserve ${MIN_FREE_GB}GiB (was 500MiB)"
echo " Flash via web UI -> Settings -> Maintenance -> Local Upgrade."
echo " Recover with RECOVERY/CURRENT_WORKING_netstate_4896.pak or FACTORY_STOCK_4867.pak."
echo "===================================================================="
