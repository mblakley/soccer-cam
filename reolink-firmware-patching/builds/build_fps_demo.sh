#!/bin/bash
# build_fps_demo.sh -- build a pak that captures at a chosen frame rate, for the
# purpose of MEASURING the Duo 3's real sustainable fps from recorded video.
#
# Why this builder exists when build_fps_cap.sh already patches fps
# -----------------------------------------------------------------
# build_fps_cap.sh starts from the STOCK pak and asserts the stock byte
# (movz w1, #20). Every build after the first therefore cannot be re-based on a
# previous build. This builder accepts ANY `movz w1, #imm` at the site, so it
# can iterate from the last known-good pak instead of re-deriving the whole
# feature set from stock each time. It also adds three things the postmortem
# (docs/BRICK_POSTMORTEM.md) found missing from every existing builder:
#
#   1. It ASSERTS the boot chain is byte-identical to the base pak before
#      writing the CRC, and aborts if not. loader/fdt/atf/uboot/kernel/ai are
#      never touched by any patch here, so any difference means a repacker bug
#      -- and that is the one class of change that can cost the recovery path.
#   2. It STRIPS the netstate override assertion from the carried probe script
#      rather than dropping the script, so the 2323 root shell (the cheap
#      recovery channel) survives while recording stays off at home.
#   3. It carries no LD_PRELOAD shim of any kind.
#
# What sets the frame rate
# ------------------------
# Two independent values, and only the first one moves the sensor:
#
#   `device` +0x8bb1c  `movz w1, #N` in Na_video_encoder_build_basic. N becomes
#                      HD_VIDEOCAP_IN.frc and is what the SENSOR is driven at.
#                      Verified: with N=21 the camera reports frc 21/1 in
#                      /proc/hdal/vcap/info and chgmode_fpsx100 2100 in
#                      /proc/kflow_sen/info, and SetEnc frameRate=18 does NOT
#                      change either -- SetEnc only retimes the ENCODER.
#
#   `router` fps lists The values GetEnc advertises and SetEnc validates
#                      against. SetEnc rejects anything not in the list with
#                      "param error" rspCode -4, so the list must expose N or
#                      the encoder cannot be told to keep every captured frame.
#
# Both are patched to the same N.
#
# Optional sensor retime
# ----------------------
# sen_calc_chgmode_vd_os08c10() computes  vd = min_vd * dft_fps / fps  and, if
# vd < min_vd, discards the request and forces fps = dft_fps. So dft_fps is a
# hard ceiling on capture rate. dft_fps and min_vd are a matched pair: the
# sensor's pixel clock is fixed, so
#
#     HTS * min_vd * (dft_fps/100) = pclk = 2592 * 2314 * 25.00 = 149,947,200
#
# must hold or a requested fps stops equalling the delivered fps. Passing
# <dft_fpsx100> retimes BOTH sensor modules, recomputing min_vd to preserve that
# product. min_vd may not fall below the sensor's readout height (2162 rows) --
# the sensor cannot emit 2162 rows in fewer than 2162 line periods -- and this
# script refuses to build if it would.
#
# Usage:
#   sudo bash build_fps_demo.sh <base.pak> <out.pak> <capture_fps> [dft_fpsx100] [pump_sleep_us]
#
# Example:
#   sudo bash build_fps_demo.sh base.pak duo3_fps25.pak 25
#   sudo bash build_fps_demo.sh base.pak duo3_fps26.pak 26 2600
#   sudo bash build_fps_demo.sh base.pak duo3_fps25_pump.pak 25 0 2000
#
# ALWAYS run verify/check_recording_default.sh on the output before flashing.

set -euo pipefail

BASE="${1:?usage: $0 <base.pak> <out.pak> <capture_fps> [dft_fpsx100] [pump_sleep_us]}"
OUT="${2:?usage: $0 <base.pak> <out.pak> <capture_fps> [dft_fpsx100] [pump_sleep_us]}"
FPS="${3:?usage: $0 <base.pak> <out.pak> <capture_fps> [dft_fpsx100] [pump_sleep_us]}"
DFT="${4:-0}"
SLEEP_US="${5:-0}"
AUX_W="${6:-0}"
AUX_H="${7:-0}"
KBPS="${8:-0}"
[[ "$EUID" -eq 0 ]] || { echo "ERROR: run as root (needs unsquashfs/mksquashfs)"; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAK_DIR="$(cd "$HERE/../pak" && pwd)"
# pak/ contains BOTH pak.py and pak_repack.py, and pak.py shadows the pak/
# namespace package -- so `from pak import pak` can never resolve while pak/ is
# on sys.path. Put pak/ on the path and import both modules flat instead.
export PYTHONPATH="$PAK_DIR"
WORK="$(mktemp -d)"
trap "rm -rf '$WORK'" EXIT
cd "$WORK"

echo "==================================================================="
echo " base        : $BASE"
echo " out         : $OUT"
echo " capture fps : $FPS"
echo " sensor dft  : $([ "$DFT" = 0 ] && echo '(unchanged, 25.00 ceiling)' || echo "$(echo "$DFT" | sed 's/..$/.&/') fps")"
echo " pump sleep  : $([ "$SLEEP_US" = 0 ] && echo '(unchanged)' || echo "${SLEEP_US} us")"
echo " aux streams : $([ "$AUX_W" = 0 ] && echo '(unchanged)' || echo "shrunk to ${AUX_W}x${AUX_H}")"
echo " bitrate max : $([ "$KBPS" = 0 ] && echo '(unchanged)' || echo "${KBPS} kbps")"
echo "==================================================================="

echo "==> 1) Extract sections"
python3 - "$BASE" <<'PY'
import sys, os, hashlib
import pak
data = open(sys.argv[1], "rb").read()
os.makedirs("sec", exist_ok=True)
for s in pak.parse(data):
    if not s["size"]:
        continue
    blob = data[s["offset"]: s["offset"] + s["size"]]
    open(f"sec/{s['name']}.bin", "wb").write(blob)
    print(f"   {s['name']:8s} {s['size']:>9d}  {hashlib.sha256(blob).hexdigest()[:16]}")
PY

echo "==> 2) Unpack app + rootfs"
unsquashfs -d app -no-progress sec/app.bin >/dev/null
unsquashfs -d rfs -no-progress sec/rootfs.bin >/dev/null

echo "==> 3) Patch device capture-fps hardcode (+0x8bb1c)"
python3 - "$FPS" <<'PY'
import struct, sys
fps = int(sys.argv[1])
assert 0 < fps < 0x100, "fps must fit a byte"
OFF = 0x8bb1c
d = bytearray(open("app/device", "rb").read())
cur = struct.unpack_from("<I", d, OFF)[0]
# Accept any `movz w1, #imm16` (sf=0, opc=10, hw=0, Rd=1): 0x52800001 | imm<<5
assert (cur & 0xFFE0001F) == 0x52800001, \
    f"device[0x{OFF:x}] is {cur:08x}, not a `movz w1, #imm` -- refusing to patch blind"
old = (cur >> 5) & 0xFFFF
new = 0x52800001 | (fps << 5)
struct.pack_into("<I", d, OFF, new)
open("app/device", "wb").write(bytes(d))
print(f"   device capture fps: movz w1,#{old} -> movz w1,#{fps}")
PY

echo "==> 4) Patch router advertised fps lists"
python3 - "$FPS" <<'PY'
import struct, sys
fps = int(sys.argv[1])
# Sites verified present in the 4909 base, all currently holding imm=21.
W0_SITES = [0x637fc, 0x63c48, 0x63c68, 0x63cd4, 0x63ce4,   # per-resolution table
            0x63ddc, 0x63e8c, 0x640ac, 0x64384,
            0x6565c]                                        # dropdown max
X22_SITE = 0x655a4                                          # 64-bit preload
d = bytearray(open("app/router", "rb").read())
n = 0
for off in W0_SITES:
    cur = struct.unpack_from("<I", d, off)[0]
    if (cur & 0xFFE0001F) != 0x52800000:
        print(f"   WARNING: router[0x{off:x}] = {cur:08x} is not `movz w0,#imm` -- skipped")
        continue
    struct.pack_into("<I", d, off, 0x52800000 | (fps << 5))
    n += 1
cur = struct.unpack_from("<I", d, X22_SITE)[0]
assert (cur & 0xFFE0001F) == 0xD2800016, \
    f"router[0x{X22_SITE:x}] = {cur:08x}, not `movz x22, #imm` -- refusing"
struct.pack_into("<I", d, X22_SITE, 0xD2800016 | (fps << 5))
open("app/router", "wb").write(bytes(d))
print(f"   router fps lists: {n}/{len(W0_SITES)} w0 sites + x22 preload -> {fps}")
PY

echo "==> 4a) Optional main-stream bitrate ceiling"
# router +0x6351c, `movz w11, #imm` -- the largest value the API will advertise
# and accept for the main stream (stock 12288, this tooling has been shipping
# 20480). Raising it costs essentially nothing on this hardware: every limit
# measured in FPS_DEMO_RESULTS.md is a PIXEL-rate limit. Bitstream write at
# 20 Mbps is 2.5 MB/s against ~4900 MB/s of DDR frame traffic (0.05%), the ISP
# and encoder are charged per pixel not per bit, and rate control moves QP
# rather than throughput. Verified rather than assumed -- see the 20480 vs
# 40960 rows in the results table.
if [ "$KBPS" != "0" ]; then
python3 - "$KBPS" <<'PY'
import struct, sys
kbps = int(sys.argv[1])
assert 0 < kbps < 0x10000, "kbps must fit a movz imm16"
OFF = 0x6351c
d = bytearray(open("app/router", "rb").read())
cur = struct.unpack_from("<I", d, OFF)[0]
if (cur & 0xFFE0001F) != 0x5280000B:
    raise SystemExit(f"ABORT: router[0x{OFF:x}] = {cur:08x} is not `movz w11, #imm`")
old = (cur >> 5) & 0xFFFF
struct.pack_into("<I", d, OFF, 0x5280000B | (kbps << 5))
open("app/router", "wb").write(bytes(d))
print(f"   router max bitrate: movz w11,#{old} -> #{kbps} kbps")
PY
else
    echo "   (skipped -- bitrate ceiling unchanged)"
fi

echo "==> 4b) Optional pump poll-miss sleep"
# The userspace pump that carries the stitched main stream from VideoProc 2
# out[0] into VideoEnc 0 in[0] pulls NON-BLOCKING (wait_time = 0) and sleeps a
# fixed interval on every miss. That quantises the delivered frame period to
#     T = k * sleep + work
# so at 7680x2160 (work ~10 ms) a 20 ms sleep lands on k=2 -> 50 ms -> 20 fps
# no matter what rate the sensor is producing. It is the reason capture 21, 25
# and 28 all delivered ~20 fps (docs/FPS_DEMO_RESULTS.md §3).
#
# Shrinking the sleep shrinks the quantum. 2 ms gives ~1 fps of granularity
# instead of ~8, at the cost of more polling on a core that measured 88.8% idle.
#
# Three sites, all `movz w0, #imm`, all in the stitch/pump path:
#   0x081904  bc_stitch_main   poll-miss sleep   (stock 20000)
#   0x081b18  bc_stitch_main   success sleep     (stock 10000)
#   0x081e54  VSP->VENC pump   poll-miss sleep   (stock 20000)  <- the quantiser
# Any `movz w0, #imm` is accepted: the base pak is usually a previous build of
# this same tooling, not stock.
if [ "$SLEEP_US" != "0" ]; then
python3 - "$SLEEP_US" <<'PY'
import struct, sys
us = int(sys.argv[1])
assert 0 < us <= 0xffff, "sleep must fit a movz imm16"
SITES = [(0x081904, "bc_stitch_main poll-miss"),
         (0x081b18, "bc_stitch_main success"),
         (0x081e54, "VSP->VENC pump poll-miss (the quantiser)")]
d = bytearray(open("app/device", "rb").read())
for off, name in SITES:
    cur = struct.unpack_from("<I", d, off)[0]
    if (cur & 0xFFE0001F) != 0x52800000:
        raise SystemExit(f"ABORT: device[0x{off:x}] = {cur:08x} is not `movz w0, #imm`")
    old = (cur >> 5) & 0xFFFF
    struct.pack_into("<I", d, off, 0x52800000 | (us << 5))
    print(f"   0x{off:06x} {name:42s} movz w0,#{old} -> #{us}")
open("app/device", "wb").write(bytes(d))
PY
else
    echo "   (skipped -- pump sleeps left as found)"
fi

echo "==> 4c) Optional aux-stream shrink (starve VideoProc 3)"
# `device` carries its own stream-descriptor table in .data. Each entry is a
# pair of HDAL path ids (0x22xxxxxx / 0x24xxxxxx) followed by
# (max_w, max_h, cur_w, cur_h). The four entries below are exactly the live
# VideoProc 3 outputs seen in /proc/hdal/vprc/info:
#
#   0x336920  1536 x 432   -> VideoEnc 0 in[1]  (sub stream)
#   0x336960  2560 x 720   -> VideoEnc 0 in[3]  (ext stream)
#   0x336970  1280 x 352   -> VideoProc 3 out[2] (AI, 10/21 fps)
#   0x336990   480 x 136   -> VideoProc 3 out[3] (AI, 5/21 fps)
#
# VideoProc 3 costs ~20 of the ISP's ~66.7 jobs/s (`/proc/kdrv_ipp/utilization`
# reads usage 99, fps 66) and its two encoder outputs cost ~108 ms/s of encoder
# time. Runtime attempts to stop it failed: isf_unit_set_state on its out ports
# returns rc=0 but leaves IPP at 99/66, and /mnt/para/0_4|0_5 are not its
# config. Shrinking its geometry here starves it instead of removing it, which
# is a data-only change and trivially reversible.
#
# Sub and ext are lost. That is intended and approved.
if [ "$AUX_W" != "0" ]; then
python3 - "$AUX_W" "$AUX_H" <<'PY'
import struct, sys
aw, ah = int(sys.argv[1]), int(sys.argv[2])
SITES = [(0x336920, 1536, 432, "sub  -> VideoEnc in[1]"),
         (0x336960, 2560, 720, "ext  -> VideoEnc in[3]"),
         (0x336970, 1280, 352, "ai   -> VideoProc 3 out[2]"),
         (0x336990,  480, 136, "ai   -> VideoProc 3 out[3]")]
# The ISE scaler refuses more than a 16x downscale in either axis. All of these
# paths are scaled from the full 7680x2160 stitched frame, so anything below
# 480x135 is rejected at runtime with
#     ERR:gximg_scale_by_ise() scale factor over 16, SrcW=7680,SrcH=2160,...
# and `device` then loops on gfx_scale() failures and never finishes bringing
# the app up -- no nginx, no HTTP API, recoverable only through the 2323 shell.
# That is exactly what 320x180 did on 2026-08-17. Note the stock table's
# smallest entry is 480x136, i.e. the limit itself.
SRC_W, SRC_H, MAX_RATIO = 7680, 2160, 16
if SRC_W / aw > MAX_RATIO or SRC_H / ah > MAX_RATIO:
    raise SystemExit(
        f"REFUSING: {aw}x{ah} needs {SRC_W/aw:.1f}x/{SRC_H/ah:.1f}x downscale from "
        f"{SRC_W}x{SRC_H}; the ISE scaler caps at {MAX_RATIO}x. "
        f"Minimum is {SRC_W // MAX_RATIO}x{SRC_H // MAX_RATIO}.")

d = bytearray(open("app/device", "rb").read())
for off, w, h, name in SITES:
    cur = struct.unpack_from("<4I", d, off)
    if cur != (w, h, w, h):
        raise SystemExit(f"ABORT: device[0x{off:x}] = {cur}, expected {(w, h, w, h)}")
    struct.pack_into("<4I", d, off, aw, ah, aw, ah)
    print(f"   0x{off:06x} {name:28s} {w}x{h} -> {aw}x{ah}")
open("app/device", "wb").write(bytes(d))
PY
else
    echo "   (skipped -- aux streams left at full size)"
fi

echo "==> 5) Optional sensor retime"
if [ "$DFT" != "0" ]; then
python3 - "$DFT" <<'PY'
import struct, sys

dft = int(sys.argv[1])
PCLK_X100 = 2592 * 2314 * 2500        # HTS * min_vd * dft_fps, the fixed product
READOUT_ROWS = 2162                   # mode_basic_param +48; VTS cannot go below


def sym_off(path, want):
    d = open(path, "rb").read()
    shoff = struct.unpack_from("<Q", d, 0x28)[0]
    shent = struct.unpack_from("<H", d, 0x3A)[0]
    shnum = struct.unpack_from("<H", d, 0x3C)[0]
    secs = []
    for i in range(shnum):
        b = shoff + i * shent
        f = struct.unpack_from("<IIQQQQIIQQ", d, b)
        secs.append(dict(typ=f[1], off=f[4], size=f[5], link=f[6], entsize=f[9]))
    for s in secs:
        if s["typ"] != 2:      # SHT_SYMTAB
            continue
        st = secs[s["link"]]
        for i in range(s["size"] // s["entsize"]):
            b = s["off"] + i * s["entsize"]
            nm, info, other, shndx, val, sz = struct.unpack_from("<IBBHQQ", d, b)
            e = d.index(b"\x00", st["off"] + nm)
            if d[st["off"] + nm:e].decode() == want:
                return secs[shndx]["off"] + val
    raise SystemExit(f"{path}: symbol {want} not found")


for ko in ["rfs/lib/modules/5.10.168/hdal/sen_os08c10/nvt_sen_os08c10.ko",
           "rfs/lib/modules/5.10.168/hdal/sen_os08c10_slave/nvt_sen_os08c10_slave.ko"]:
    off = sym_off(ko, "mode_basic_param")
    d = bytearray(open(ko, "rb").read())
    old_dft = struct.unpack_from("<I", d, off + 24)[0]
    old_vd = struct.unpack_from("<I", d, off + 136)[0]
    hts = struct.unpack_from("<I", d, off + 128)[0]
    assert old_dft and old_vd and hts, f"{ko}: mode table looks empty"
    new_vd = round(PCLK_X100 / (hts * dft))
    if new_vd < READOUT_ROWS:
        raise SystemExit(
            f"REFUSING: dft_fps {dft/100:.2f} needs VTS {new_vd} < readout {READOUT_ROWS} rows. "
            f"The sensor cannot emit {READOUT_ROWS} rows in {new_vd} line periods.")
    struct.pack_into("<I", d, off + 24, dft)
    struct.pack_into("<I", d, off + 136, new_vd)
    open(ko, "wb").write(bytes(d))
    real = PCLK_X100 / 100.0 / (hts * new_vd)
    print(f"   {ko.split('/')[-1]}: dft_fps {old_dft}->{dft}  min_vd {old_vd}->{new_vd} "
          f"(vblank {new_vd - READOUT_ROWS} rows, delivers {real:.3f} fps)")
PY
else
    echo "   (skipped -- sensor keeps its 25.00 fps ceiling)"
fi

echo "==> 6) Recovery shell without the recording override"
# The carried probe script asserted /mnt/sda/netstate/override at every boot,
# which made S99_NetState yield and let the camera record at home. Strip that
# (and the unrelated one-shot stitch collection, which also wrote to a card
# that is 99% full) but KEEP the tcpsvd root shell -- that is the recovery path.
rm -f rfs/etc/init.d/S36_StitchProbe
cat > rfs/etc/init.d/S36_RootShell <<'EOS'
#!/bin/sh
# S36_RootShell -- recovery channel. One command set per TCP connection:
# /bin/sh reads stdin to EOF, executes, exits.
#
# This script deliberately does NOT touch /mnt/sda/netstate/override. Asserting
# that flag at boot makes S99_NetState yield permanently and the camera records
# at home; enabling recording is an explicit operator action, never a build
# default. See verify/check_recording_default.sh.
tcpsvd -vE 0.0.0.0 2323 /bin/sh >/dev/null 2>&1 &
exit 0
EOS
chmod 755 rfs/etc/init.d/S36_RootShell
echo "   S36_StitchProbe -> S36_RootShell (shell kept, override assertion removed)"

echo "==> 7) Build manifest"
cat > rfs/etc/soccercam_build <<EOS
build: fps_demo
capture_fps: $FPS
sensor_dft_fpsx100: $DFT
base: $(basename "$BASE")
built: $(date -u '+%Y-%m-%dT%H:%M:%SZ')
EOS
chmod 644 rfs/etc/soccercam_build

echo "==> 8) Repack squashfs"
mksquashfs app app_new.bin -comp xz -b 262144 -noappend -no-progress \
    -no-exports -all-root -mkfs-time 0 -all-time 0 >/dev/null
mksquashfs rfs rootfs_new.bin -comp xz -b 262144 -noappend -no-progress \
    -no-exports -all-root -mkfs-time 0 -all-time 0 >/dev/null
echo "   app    $(stat -c%s app_new.bin) bytes"
echo "   rootfs $(stat -c%s rootfs_new.bin) bytes"

echo "==> 9) Repack pak"
python3 - "$BASE" "$OUT" <<'PY'
import sys
from pak_repack import repack
crc, size, meta = repack(sys.argv[1], sys.argv[2],
                         swaps={"app": open("app_new.bin", "rb").read(),
                                "rootfs": open("rootfs_new.bin", "rb").read()})
print(f"   crc=0x{crc:016x} size={size}")
PY

echo "==> 10) ASSERT boot chain byte-identical to base"
# The one check whose absence the 4917 postmortem called out. loader/fdt/atf/
# uboot/kernel are what make the camera recoverable at all; nothing above
# touches them, so any difference here is a repacker bug and must stop the
# build before the pak is ever offered to the flasher.
python3 - "$BASE" "$OUT" <<'PY'
import sys, hashlib
import pak

IMMUTABLE = ("loader", "fdt", "atf", "uboot", "kernel", "ai")


def digest(path):
    d = open(path, "rb").read()
    return {s["name"]: hashlib.sha256(d[s["offset"]:s["offset"] + s["size"]]).hexdigest()
            for s in pak.parse(d) if s["size"]}


a, b = digest(sys.argv[1]), digest(sys.argv[2])
bad = []
for name in IMMUTABLE:
    if name not in a:
        continue
    same = a[name] == b.get(name)
    print(f"   {name:8s} {'IDENTICAL' if same else 'DIFFERS'}  {a[name][:16]}")
    if not same:
        bad.append(name)
for name in ("rootfs", "app"):
    print(f"   {name:8s} {'unchanged' if a.get(name) == b.get(name) else 'replaced'}")
if bad:
    raise SystemExit(f"ABORT: boot-chain section(s) modified: {', '.join(bad)}")
print("   boot chain intact")
PY

echo
echo "==================================================================="
echo " Built: $OUT"
echo " NEXT:  verify/check_recording_default.sh '$OUT'   <-- required gate"
echo "==================================================================="
