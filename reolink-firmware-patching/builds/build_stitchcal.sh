#!/bin/bash
# Comprehensive soccer-cam firmware + stitch-seam calibration boot hook.
#
# Everything build_soccercam_comprehensive.sh installs, plus:
#   - /usr/bin/lut2d_ioctl            (static aarch64; get / set / compose)   (rootfs)
#   - /etc/init.d/S98_StitchCal       (re-apply the seam correction at boot)  (rootfs)
#   - /etc/init.d/S36_RootShell       (tcp/2323 recovery channel)              (rootfs)
#
# WHY THIS NEEDS A FLASH AT ALL. /etc/init.d is squashfs and read-only, so the
# hook itself cannot be dropped onto the SD card. It is baked ONCE; every
# calibration afterwards is a few hundred bytes of anchors.txt copied to
# /mnt/sda/stitchcal/, no reflash. That is the S99_NetState pattern -- fixed
# script in firmware, mutable configuration on the card.
#
# The helper binary is baked too, rather than living on the SD card as the
# design first proposed. It is code, not configuration; baking it means the hook
# still works on a card that was reformatted, and it keeps the boot path from
# depending on a file an operator can delete. S98_StitchCal still prefers
# /mnt/sda/stitchcal/bin/lut2d_ioctl when present, so it can be iterated on
# without a reflash.
#
# TWO GATES RUN BEFORE THE PAK IS USABLE, and both refuse rather than warn:
#   1. The boot chain (loader, fdt, atf, uboot, kernel) must be byte-identical
#      to stock. Enforced inside pak_repack.repack() itself, before a byte is
#      assembled and long before the CRC is computed -- a pak with a good CRC
#      and a damaged loader is indistinguishable from a good one until after you
#      have flashed it. Re-asserted here against the finished file.
#   2. verify/check_recording_default.sh -- the pak must not record at home.
#
# No sudo needed (unsquashfs -no-xattrs, mksquashfs -all-root).
#
# Usage:
#   bash build_stitchcal.sh <stock.pak> <out.pak> <kbps> <user> <pass> <reserve_gb> <home_mac> [more_macs...]
#
# NOTE: <out.pak> MUST match the Reolink filename pattern or Local Upgrade
# rejects it regardless of contents:
#   IPC_NT15NA416MP.<build>_2505072124.Reolink-Duo-3-PoE.16MP.REOLINK_stitchcal.pak
set -euo pipefail
STOCK="${1:?}"; OUT="${2:?}"; KBPS="${3:?}"; USER="${4:?}"; PASS="${5:?}"; RES_GB="${6:?}"; shift 6
HOME_MACS="$*"; [[ -n "$HOME_MACS" ]] || { echo "ERROR: home MAC required"; exit 1; }
case "$(basename "$OUT")" in
  IPC_NT15NA416MP.*_*.Reolink-Duo-3-PoE.16MP.REOLINK*.pak) : ;;
  *) echo "WARNING: output name '$(basename "$OUT")' does NOT match the Reolink pattern;" >&2
     echo "         the camera's Local Upgrade will reject it." >&2 ;;
esac

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PAK_DIR="$ROOT/pak"
NS_TPL="$ROOT/runtime/netstate/S99_NetState_v2.template"
REC_SH="$ROOT/runtime/recover/S35_RecRecover"
REC_C="$ROOT/recover/recover_mp4.c"
SC_SH="$ROOT/runtime/stitchcal/S98_StitchCal"
RS_SH="$ROOT/runtime/rootshell/S36_RootShell"
LUT_C="$ROOT/vpe/lut2d_ioctl.c"
for x in "$NS_TPL" "$REC_SH" "$REC_C" "$SC_SH" "$LUT_C" "$RS_SH"; do
  [[ -f "$x" ]] || { echo "ERROR: missing $x"; exit 1; }
done
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
HELIX_SRC="$ROOT/recover/helix/ESP8266Audio/src/libhelix-aac"
HELIX_COMPAT="$ROOT/recover/helix/compat"
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
  AUDIO=yes
elif [[ "${ALLOW_NO_AUDIO:-0}" == "1" ]]; then
  echo "   ALLOW_NO_AUDIO=1: Helix AAC source absent -> VIDEO-ONLY recovery (-DNO_AUDIO)"
  aarch64-linux-gnu-gcc -O2 -DNDEBUG -DNO_AUDIO -static -o rootfs_unpacked/usr/bin/recover_mp4 "$REC_C"
  AUDIO=no
else
  # The comprehensive builder only warns here. That is fine when someone is
  # watching a terminal; it is not fine for a pak that gets flashed, because a
  # silent downgrade from audio=yes to audio=no is invisible afterwards and
  # costs the sound on every power-cut recovery. Fetch the source (see
  # recover/helix/README.md) or say ALLOW_NO_AUDIO=1 and mean it.
  echo "ERROR: Helix AAC source absent -- this pak would silently downgrade" >&2
  echo "       recover_mp4 to video-only. See recover/helix/README.md to fetch it," >&2
  echo "       or re-run with ALLOW_NO_AUDIO=1 to accept the downgrade." >&2
  exit 1
fi
chmod 755 rootfs_unpacked/usr/bin/recover_mp4
install -m 0755 "$REC_SH" rootfs_unpacked/etc/init.d/S35_RecRecover
echo "   recover_mp4 + S35_RecRecover installed"

echo "==> 6b) build + install lut2d_ioctl and S98_StitchCal"
aarch64-linux-gnu-gcc -O2 -Wall -Wextra -static \
    -o rootfs_unpacked/usr/bin/lut2d_ioctl "$LUT_C" -lm
chmod 755 rootfs_unpacked/usr/bin/lut2d_ioctl
file rootfs_unpacked/usr/bin/lut2d_ioctl | cut -d, -f1-3
# The helper must refuse to run without arguments rather than doing something;
# a boot hook that silently no-ops on a broken binary is the failure this whole
# path is trying to avoid.
if rootfs_unpacked/usr/bin/lut2d_ioctl >/dev/null 2>&1; then
  echo "ERROR: lut2d_ioctl with no arguments returned success"; exit 1
fi
install -m 0755 "$SC_SH" rootfs_unpacked/etc/init.d/S98_StitchCal
# Gates on the hook's actual code, comments stripped -- these encode the two
# invariants the whole design rests on, so they are checked in the build rather
# than trusted to review.
SC_CODE="$(sed 's/#.*//' rootfs_unpacked/etc/init.d/S98_StitchCal)"
grep -q '\$BIN" compose' <<<"$SC_CODE" \
  || { echo "ERROR: S98_StitchCal does not compose -- it would write a generated mesh"; exit 1; }
if grep -q 'require-baseline' <<<"$SC_CODE"; then
  echo "ERROR: the boot hook must NOT require a baseline match: one legitimate" >&2
  echo "       SetStitch would then disable it permanently (see S98 comments)." >&2
  exit 1
fi
grep -q 'give_up' <<<"$SC_CODE" \
  || { echo "ERROR: S98_StitchCal has no bounded failure path"; exit 1; }
# rcS runs S* scripts before the app mounts /dev/hd/sda1, so a hook that reads
# its config first exits silently on a read-only rootfs and leaves no trace.
# That shipped once; it does not ship again.
grep -q 'wait_for_sdcard' <<<"$SC_CODE" \
  || { echo "ERROR: S98_StitchCal must wait for /mnt/sda before reading config"; exit 1; }
awk '/^main\(\)/,/^}/' <<<"$SC_CODE" | grep -n 'wait_for_sdcard\|ANCHORS\|DISABLE' \
  | head -1 | grep -q 'wait_for_sdcard' \
  || { echo "ERROR: S98_StitchCal reads config before waiting for /mnt/sda"; exit 1; }
# Composing onto an already-corrected mesh silently doubles the shear, and the
# log looks healthy while it happens. The hook must recognise its own output.
grep -q 'APPLIED_SIG' <<<"$SC_CODE" \
  || { echo "ERROR: S98_StitchCal is not idempotent -- a re-run would double-apply"; exit 1; }
echo "   lut2d_ioctl + S98_StitchCal installed"

echo "==> 6d) install S36_RootShell (recovery channel)"
install -m 0755 "$RS_SH" rootfs_unpacked/etc/init.d/S36_RootShell
# The 2026-08-16 investigation builds asserted the netstate override at boot and
# the camera recorded at home for hours. check_recording_default.sh catches that
# at step 11; this is the same check one layer earlier, on the file we just
# wrote, so the failure is attributed to the right script.
if grep -qE '^[^#]*(touch|>|cp|echo).*netstate/override' rootfs_unpacked/etc/init.d/S36_RootShell; then
  echo "ERROR: S36_RootShell asserts the netstate override"; exit 1
fi
echo "   S36_RootShell installed (tcp/2323, one command set per connection)"

echo "==> 6c) bake build manifest (/etc/soccercam_build)"
COMMIT="${SOCCERCAM_COMMIT:-$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)}"
cat > rootfs_unpacked/etc/soccercam_build <<EOF
variant=stitchcal
pak=$(basename "$OUT")
base=v3.0.0.4867_2505072124
kbps=$KBPS
reserve_gb=$RES_GB
netstate=v2
recover=yes
audio=$AUDIO
stitchcal=S98+lut2d_ioctl
rootshell=S36
commit=$COMMIT
EOF
chmod 644 rootfs_unpacked/etc/soccercam_build

echo "==> 7) repack app + rootfs"
mksquashfs app_unpacked    app_new.bin    -comp xz -b 262144 -noappend -no-progress -no-exports -all-root -mkfs-time 0 -all-time 0 >/dev/null
mksquashfs rootfs_unpacked rootfs_new.bin -comp xz -b 262144 -noappend -no-progress -no-exports -all-root -mkfs-time 0 -all-time 0 >/dev/null
ROOTFS_SZ=$(stat -c %s rootfs_new.bin)
echo "   rootfs $ROOTFS_SZ bytes"
[[ "$ROOTFS_SZ" -lt $((8*1024*1024)) ]] || { echo "ERROR: rootfs exceeds its 8 MiB partition"; exit 1; }

echo "==> 8) repack pak (boot chain asserted inside repack, before the CRC)"
PYTHONPATH="$PAK_DIR" python3 - <<PY
from pak_repack import repack, BootChainChanged
try:
    crc,size,_=repack("$STOCK","$OUT",swaps={"rootfs":open("rootfs_new.bin","rb").read(),"app":open("app_new.bin","rb").read()})
except BootChainChanged as e:
    raise SystemExit(f"REFUSING: {e}")
print(f"   wrote $OUT size={size} crc=0x{crc:08x}")
PY

echo "==> 9) verify CRC"
python3 "$PAK_DIR/reolink_crc.py" compute "$OUT"

echo "==> 10) GATE: boot chain byte-identical to stock"
python3 "$PAK_DIR/pak_repack.py" --check-boot-chain "$STOCK" "$OUT"

echo "==> 11) GATE: this pak must not record at home"
bash "$ROOT/verify/check_recording_default.sh" "$OUT"

echo "==================================================================="
echo " STITCHCAL: comprehensive + S98_StitchCal + /usr/bin/lut2d_ioctl"
echo " Boot chain verified identical to stock; recording defaults to off at home."
echo
echo " After flashing, a calibration is a FILE DROP, not a reflash:"
echo "   /mnt/sda/stitchcal/anchors.txt   the correction"
echo "   /mnt/sda/stitchcal/disable       presence => hook exits 0, factory mesh"
echo "   /mnt/sda/stitchcal/log           what happened at boot"
echo "   /mnt/sda/stitchcal/state.json    what was applied, and against what baseline"
echo " With no anchors.txt the hook exits 0 and the mesh stays factory, so this"
echo " pak is a safe no-op until a calibration actually exists."
echo "==================================================================="
