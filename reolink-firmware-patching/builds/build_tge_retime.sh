#!/bin/bash
# Retime the NT98530 timing generator (TGE) so capture runs above 25 fps.
#
# WHY THE SENSOR-SIDE PATCHES DID NOTHING
# ---------------------------------------
# Both OS08C10s run as slaves to the SoC timing generator (`tge_en 1` in
# /proc/hdal/vcap/info). The TGE, not the sensor, decides when a frame
# starts, so reprogramming the sensor's VTS register changes nothing: the
# sensor just gets re-triggered by the next TGE VD regardless.
#
# The TGE emits:
#     HD every  hd_period  TGE clocks      (stock 415)
#     VD every  vd_period  HD periods      (stock 2314)
# giving 24.95 fps measured (TGE_INT in /proc/interrupts). Both values are
# visible live in /proc/kflow_sen/info as
#     tge_signal(hd_sync 8, hd_period 415, vd_sync 2313, vd_period 2314)
#
# and both are *hardcoded immediates* in nvt_sen_os08c10_slave.ko:
#     mov  x?,#0x909              ; vd_sync  2313
#     movk x?,#0x19f, LSL #32     ; hd_period 415     <-- this script
#     movk x?,#0x90a, LSL #32     ; vd_period 2314
#     mov  w?,#0x90a              ; vd_period multiplier
#
# WHY hd_period AND NOT vd_period
# -------------------------------
# vd_period is not a constant on its own -- the driver recomputes it as
#     vd_period' = (2314 * dft_fps) / chgmode_fps
# and sen_calc_chgmode_vd_os08c10_slave clamps chgmode_fps <= dft_fps
# (2500). That makes vd_period' >= 2314 *always*, and the clamp is scale
# invariant: raising dft_fps raises the numerator by the same factor. So
# vd_period cannot be pushed below 2314 without surgery on the clamp AND
# on four separate constants that feed the vd_sync arithmetic.
#
# hd_period has none of that. It is a pure pass-through constant, used in
# no arithmetic anywhere, and it divides the TGE frame period linearly:
#     fps_new = 24.95 * 415 / hd_period
# vd_period stays 2314 and vd_sync stays 2313, so the signal stays
# self-consistent and every other driver bookkeeping value is untouched.
#
# CEILING
# -------
# The sensor still needs 2162 rows * (HTS 2592 / PCLK 150 MHz) = 37.359 ms
# to read a full frame out. The TGE VD interval must stay above that:
#     hd_period >= 415 * 37.359/40.080 = 386.8   ->  >= 387
# i.e. a hard ceiling of ~26.77 fps at 3840x2160. 28 fps at full height is
# not reachable by any TGE setting.
#
#   hd_period  predicted fps   VD period   guard over readout
#     415        24.95          40.080 ms    2.721 ms  (157 rows)  [stock]
#     400        25.89          38.631 ms    1.272 ms   (74 rows)
#     396        26.15          38.245 ms    0.886 ms   (51 rows)   <-- default
#     392        26.41          37.859 ms    0.499 ms   (29 rows)
#     390        26.55          37.666 ms    0.306 ms   (18 rows)
#     388        26.69          37.473 ms    0.113 ms    (7 rows)
#
# Below ~390 the guard is thin enough that torn or truncated frames are the
# expected failure mode. Walk down, don't jump.
#
# CAVEATS
# -------
#  * Userspace still believes 25 fps (chgmode_fps stays 2500). Recorded
#    files will carry a 25 fps label while frames arrive faster. Fixing the
#    label needs the app-side fps request too -- do NOT combine that with
#    this patch in one build, because the app request also divides
#    vd_period and the two compound past the readout limit.
#  * sen_set_expt_os08c10_slave reprograms the TGE signal on every AE
#    exposure update, which is why this script patches that copy too.
#    Long night exposures can still stretch the VD period and pull fps back
#    down -- test in daylight.
#
# Usage:
#   sudo bash build_tge_retime.sh <input.pak> <output.pak> [hd_period]
# Example:
#   sudo bash build_tge_retime.sh soccercam_comprehensive.pak duo3_tge396.pak 396
#
# The input may be stock or any already-patched .pak -- this only touches
# nvt_sen_os08c10_slave.ko in the rootfs, so it layers cleanly on top of the
# HTTP unlock / bitrate / netstate / recovery builds.
set -euo pipefail

STOCK="${1:?usage: $0 <input.pak> <output.pak> [hd_period]}"
OUT="${2:?usage: $0 <input.pak> <output.pak> [hd_period]}"
HD="${3:-396}"
[[ "$EUID" -eq 0 ]] || { echo "ERROR: run as root"; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAK_DIR="$(cd "$HERE/../pak" && pwd)"
WORK="$(mktemp -d)"
trap "rm -rf '$WORK'" EXIT
cd "$WORK"

python3 - <<PY
hd = $HD
assert 387 <= hd <= 415, "hd_period must be 387..415 (below 387 the sensor cannot finish readout; above 415 is slower than stock)"
print("Target: TGE hd_period = %d  ->  predicted %.2f fps (stock 415 -> 24.95)" % (hd, 24.95*415/hd))
PY
echo

echo "==> 1) Extract rootfs section"
python3 - <<PY
import struct
data = open("$STOCK","rb").read()
base = 0x18 + 5*0x48     # section index 5 = rootfs
off = struct.unpack("<Q", data[base+0x38:base+0x40])[0]
sz  = struct.unpack("<Q", data[base+0x40:base+0x48])[0]
open("rootfs_stock.bin","wb").write(data[off:off+sz])
print(f"  rootfs orig size: {sz} bytes")
PY

echo "==> 2) Unsquashfs rootfs"
unsquashfs -d rootfs_unpacked -no-progress rootfs_stock.bin >/dev/null

echo "==> 3) Patch TGE hd_period in nvt_sen_os08c10_slave.ko"
python3 - <<PY
import struct, sys

KO = "rootfs_unpacked/lib/modules/5.10.168/hdal/sen_os08c10_slave/nvt_sen_os08c10_slave.ko"
HD = $HD

# file offset -> (owning function, expected stock word)
# Each is MOVK X<d>, #0x19f, LSL #32  == the TGE hd_period literal.
SITES = {
    0x0305c: ("sen_chg_fps_os08c10_slave",  0xf2c033e5),
    0x03380: ("sen_set_expt_os08c10_slave", 0xf2c033e3),
    0x03b10: ("sen_chg_mode_os08c10_slave", 0xf2c033e2),
    0x03fb0: ("sen_get_info_os08c10_slave", 0xf2c033e2),
}

d = bytearray(open(KO, "rb").read())
for off, (fn, want) in sorted(SITES.items()):
    got = struct.unpack_from("<I", d, off)[0]
    if got != want:
        sys.exit(f"ERROR: {KO} 0x{off:05x} ({fn}): expected {want:08x}, found {got:08x} "
                 f"-- firmware layout differs, refusing to patch")
    # MOVK keeps opcode/hw/Rd, only imm16 (bits 20:5) changes
    new = (got & ~(0xffff << 5)) | (HD << 5)
    struct.pack_into("<I", d, off, new)
    print(f"  0x{off:05x} {fn:32s} {want:08x} -> {new:08x}")
open(KO, "wb").write(bytes(d))

# Belt and braces: no stray copy of the literal left anywhere in .text
rest = bytes(d).count(struct.pack("<I", 0xf2c033e2)) + \\
       bytes(d).count(struct.pack("<I", 0xf2c033e3)) + \\
       bytes(d).count(struct.pack("<I", 0xf2c033e5))
if rest:
    sys.exit(f"ERROR: {rest} unpatched hd_period literal(s) remain")
print("  all hd_period literals patched")
PY

echo "==> 4) Repack rootfs squashfs"
mksquashfs rootfs_unpacked rootfs_new.bin \
    -comp xz -b 262144 -noappend -no-progress \
    -no-exports -all-root -mkfs-time 0 -all-time 0 \
    >/dev/null

echo "==> 5) Repack pak (replace rootfs section)"
PYTHONPATH="$PAK_DIR" python3 - <<PY
from pak_repack import repack
swaps = {"rootfs": open("rootfs_new.bin", "rb").read()}
crc, size, secs = repack("$STOCK", "$OUT", swaps=swaps)
print(f"wrote $OUT  size={size}  crc=0x{crc:08x}")
for name, off, sz in secs:
    marker = "  (replaced)" if name in swaps else ""
    print(f"  {name:10s} start=0x{off:08x} size=0x{sz:08x}{marker}")
PY

echo "==> 6) Verify CRC"
python3 "$PAK_DIR/reolink_crc.py" compute "$OUT"

cat <<EOF

=====================================================================
 Build complete: $OUT
   TGE hd_period: 415 -> $HD

 Measure after flashing. Do it with a client streaming, so the whole
 chain is live (the ISP and encoder are idle otherwise):

   # per-sensor capture rate. NEW/PROC/PUSH are live per-second rates,
   # not counters -- just read them. Stock reads 25 / 25 / 25, drops 0.
   grep -A3 'OUT WORK STATUS' /proc/hdal/vcap/info

   # driver's view of the retimed signal -- expect hd_period $HD
   grep -A1 tge_signal /proc/kflow_sen/info

   # shared-ISP + encoder load, sampled the same way before and after
   grep -E 'TGE_INT|SIE1|SIE3|ife_eng0|ipe_eng0|H26X' /proc/interrupts

 Confirmed if: NEW/PROC/PUSH read ~$(python3 -c "print('%.0f' % (24.95*415/$HD))") on BOTH VIDEOCAP 0 and 2 with
 drop/wrn/err still 0, and tge_signal shows hd_period $HD.

 Refuted if: NEW stays at 25 -- hd_period is not the TGE's VD divider, and
 the lever is the vd_period constant instead (0x90a at file offsets
 0x03060/0x03064, 0x03384/0x03388, 0x03b14/0x03b18, 0x03fb4, plus the
 vd_sync 0x909 at 0x03058, 0x030a0, 0x0337c, 0x033bc, 0x03870, 0x03b0c,
 0x03fac -- all must move together to keep vd_sync = vd_period - 1).

 Partly refuted if: NEW rises but drop/err climb, or frames come out torn
 or short -- the sensor can no longer finish readout inside the VD
 interval. Back off toward hd_period 400.

 Watch also: ISP is a single shared IFE/IPE/IME at 450 MHz for both
 sensors, so 2 x 3840x2160 at 1 px/clk theoretically tops out near 27 fps.
 If ife_eng0's rate does not scale with NEW, that is the next wall.
=====================================================================
EOF
