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
# omitted, ../runtime/roiqp/roi.conf.default is used. That default is DISABLED:
# the rectangle is per-field, so the pak is a no-op until a roi.conf is dropped
# on the SD card.
#
# The output must not record on the home network (verify/check_recording_default.sh),
# so the base has to carry S99_NetState. To build a minimal test image on a
# stock base anyway, say so: ROIQP_ALLOW_RECORD_AT_HOME=1.
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
CONF_SRC="${3:-$RT/roi.conf.default}"
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

if [[ "${ROIQP_MULTIWIN:-0}" == "1" ]]; then
echo "==> 2b) ROIQP_MULTIWIN=1: make setroi honour RoiIndex (ten windows)"
# The handler has two more defects beyond the format string (docs/ENCODER_ROI_QP.md
# section 4b). It zeroes a 168-byte local struct, parses into window 0 of it, and
# hands the whole struct to NMR_VdoEnc_SetROI -- so every call replaces all ten
# windows with one, and RoiIndex only ever indexes the read-back print. That
# index is not range-checked, so a large RoiIndex reads far off the stack.
#
# This rewrites the tail of the handler (the part after the print) so that it
# copies the 16 parsed bytes into window RoiIndex of the LIVE per-path struct
# and then calls NMR_VdoEnc_SetROI on that struct in place:
#
#       ldr  w3, [sp, #124]              ; RoiIndex
#       cmp  w3, #9 ;  b.hi fail         ; refuse > 9, encoder untouched
#       adrp x0, gNMRVdoEncObj ; ldr x0, [x0, :lo12:gNMRVdoEncObj]
#       mov  w4, #0xbe8 ; umaddl x0, w2, w4, x0
#       add  x1, x0, #0x388              ; live ROI struct for this path
#       add  x4, x1, w3, uxtw #4         ; &win[RoiIndex]
#       ldp  x5, x6, [x19] ; stp x5, x6, [x4]
#       mov  w0, w2 ; bl NMR_VdoEnc_SetROI ; mov w0, #1 ; b epilogue
#
# and makes the print read the window that was parsed (mov x8, #0) instead of
# indexing by the unchecked RoiIndex. Room comes from the handler's own
# "invalid path" debug print, which is dropped. Same function size; nothing
# outside 0x11bb4..0x11c6f of .text changes.
#
# Unlike the format string this is CODE, and it needs four relocation entries
# moved or retargeted and five neutralised (R_AARCH64_NONE). A wrong relocation
# is a module that fails to load, which is a camera with no encoder. Hence
# opt-in, a separate build number, and every old word asserted before it is
# replaced. NOT RUN ON HARDWARE.
python3 - "$KO_REL" <<'PY'
import struct, sys

KO = "rootfs_unpacked/" + sys.argv[1]
d = bytearray(open(KO, "rb").read())

# --- ELF64 section headers, by name
assert d[:4] == b"\x7fELF" and d[4] == 2 and d[5] == 1, "not a little-endian ELF64"
shoff, = struct.unpack_from("<Q", d, 0x28)
shentsize, shnum, shstrndx = struct.unpack_from("<HHH", d, 0x3A)
def sh(i):
    name, typ, flags, addr, off, size, link, info, align, entsize = struct.unpack_from("<IIQQQQIIQQ", d, shoff + i * shentsize)
    return dict(name=name, off=off, size=size, link=link, info=info, entsize=entsize)
strtab = sh(shstrndx)
def sname(s):
    a = strtab["off"] + s["name"]
    return d[a:d.index(b"\0", a)].decode()
secs = {sname(sh(i)): (i, sh(i)) for i in range(shnum)}
text_i, text = secs[".text"]
rela_i, rela = secs[".rela.text"]
bss_i, _ = secs[".bss"]
symtab_i, symtab = secs[".symtab"]
assert rela["info"] == text_i and rela["link"] == symtab_i and rela["entsize"] == 24

# --- the symbols the new relocations lean on
def sym(i):
    name, info, other, shndx, value, size = struct.unpack_from("<IBBHQQ", d, symtab["off"] + i * 24)
    return dict(name=name, type=info & 15, shndx=shndx, value=value)
symstr = sh(symtab["link"])
def symname(s):
    a = symstr["off"] + s["name"]
    return d[a:d.index(b"\0", a)].decode()
BSS_SECSYM = 6
s = sym(BSS_SECSYM)
assert s["type"] == 3 and s["shndx"] == bss_i, "symbol 6 is not the .bss section symbol"
obj = next(sym(i) for i in range(symtab["size"] // 24) if symname(sym(i)) == "gNMRVdoEncObj")
assert obj["shndx"] == bss_i and obj["value"] == 0x2C0, "gNMRVdoEncObj is not at .bss+0x2c0"
assert symname(sym(0x289)) == "NMR_VdoEnc_SetROI" and sym(0x289)["value"] == 0x11A44
assert symname(sym(0x1F5)) == "__stack_chk_fail"

# --- instructions: .text offset -> (stock word, new word)
WORDS = {
    0x11BB4: (0xD37C7C48, 0xD2800008),  # ubfiz x8,x2,#4,#32      -> mov x8,#0
    0x11BF4: (0x90000000, 0x52800000),  # adrp x0,<dbg flag>      -> mov w0,#0
    0x11BF8: (0xB9400000, 0x14000002),  # ldr w0,[x0]             -> b epilogue
    0x11BFC: (0x35000280, 0x94000000),  # cbnz w0,<dbg print>     -> bl __stack_chk_fail
    0x11C14: (0x540001A1, 0x54FFFF41),  # b.ne 11c48              -> b.ne 11bfc
    0x11C34: (0x2A0203E0, 0xB9407FE3),  # mov w0,w2               -> ldr w3,[sp,#124]
    0x11C38: (0xAA1303E1, 0x7100247F),  # mov x1,x19              -> cmp w3,#9
    0x11C3C: (0x94000000, 0x54FFFDC8),  # bl NMR_VdoEnc_SetROI    -> b.hi 11bf4
    0x11C40: (0x52800020, 0x90000000),  # mov w0,#1               -> adrp x0,gNMRVdoEncObj
    0x11C44: (0x17FFFFEF, 0xF9400000),  # b epilogue              -> ldr x0,[x0,:lo12:]
    0x11C48: (0x94000000, 0x52817D04),  # bl __stack_chk_fail     -> mov w4,#0xbe8
    0x11C4C: (0x90000001, 0x9BA40040),  # (dbg print) ...         -> umaddl x0,w2,w4,x0
    0x11C50: (0x91000021, 0x910E2001),  #                         -> add x1,x0,#0x388
    0x11C54: (0x9116C021, 0x8B235024),  #                         -> add x4,x1,w3,uxtw #4
    0x11C58: (0x90000000, 0xA9401A65),  #                         -> ldp x5,x6,[x19]
    0x11C5C: (0x91000000, 0xA9001885),  #                         -> stp x5,x6,[x4]
    0x11C60: (0x94000000, 0x2A0203E0),  #                         -> mov w0,w2
    0x11C64: (0x52800000, 0x94000000),  #                         -> bl NMR_VdoEnc_SetROI
    0x11C68: (0x17FFFFE6, 0x52800020),  #                         -> mov w0,#1
    0x11C6C: (0xD503201F, 0x17FFFFE5),  # nop                     -> b epilogue
}
for off, (old, new) in WORDS.items():
    got, = struct.unpack_from("<I", d, text["off"] + off)
    assert got == old, f".text+{off:#x}: {got:08x}, expected {old:08x}. Module differs; stop."
for off, (old, new) in WORDS.items():
    struct.pack_into("<I", d, text["off"] + off, new)

# --- relocations: (stock offset, stock info, stock addend) -> (offset, info, addend)
ADRP, LDST64, NONE = 0x113, 0x11E, 0
RELAS = {
    (0x11BF4, 0x0000000300000113, 0x2990): (0x11C40, (BSS_SECSYM << 32) | ADRP, 0x2C0),
    (0x11BF8, 0x000000030000011D, 0x2990): (0x11C44, (BSS_SECSYM << 32) | LDST64, 0x2C0),
    (0x11C3C, 0x000002890000011B, 0): (0x11C64, 0x000002890000011B, 0),   # bl NMR_VdoEnc_SetROI moves
    (0x11C48, 0x000001F50000011B, 0): (0x11BFC, 0x000001F50000011B, 0),   # bl __stack_chk_fail moves
    (0x11C4C, 0x0000000400000113, 0x4C8): (0x11C4C, NONE, 0),             # the dropped debug print
    (0x11C50, 0x0000000400000115, 0x4C8): (0x11C50, NONE, 0),
    (0x11C58, 0x0000000500000113, 0x2508): (0x11C58, NONE, 0),
    (0x11C5C, 0x0000000500000115, 0x2508): (0x11C5C, NONE, 0),
    (0x11C60, 0x000002E00000011B, 0): (0x11C60, NONE, 0),
}
found = {}
n = rela["size"] // 24
for i in range(n):
    at = rela["off"] + i * 24
    ent = struct.unpack_from("<QQq", d, at)
    if 0x11BB4 <= ent[0] < 0x11C70:
        found[ent] = at
# Every relocation in the rewritten range must be one we account for: an
# unlisted one would be applied on top of a new instruction.
KEEP = {
    (0x11BDC, 0x000002E00000011B, 0),   # bl printk -- the read-back line, kept
    (0x11BE4, 0x0000040C00000113, 0),   # adrp g_vdoenc_path_max_count
    (0x11BE8, 0x0000040C0000011D, 0),   # ldr  g_vdoenc_path_max_count
}
extra = set(found) - set(RELAS) - KEEP
missing = set(RELAS) - set(found)
assert not extra, f"unexpected relocations in the patched range: {[tuple(hex(v) for v in e) for e in extra]}"
assert not missing, f"expected relocations not found: {[tuple(hex(v) for v in e) for e in missing]}"
for old, new in RELAS.items():
    struct.pack_into("<QQq", d, found[old], *new)

open(KO, "wb").write(bytes(d))
print(f"   {len(WORDS)} instructions rewritten, {len(RELAS)} relocations updated, size unchanged ({len(d)} bytes)")
PY
MULTIWIN_TAG="+multiwin"
else
MULTIWIN_TAG=""
fi

echo "==> 2c) GATE: the daemon's own test must pass on the script being installed"
if ! sh "$HERE/../verify/test_roiqp_daemon.sh" > "$WORK/daemon_test.txt" 2>&1; then
    cat "$WORK/daemon_test.txt"
    echo "ERROR: verify/test_roiqp_daemon.sh failed; not building a pak around this daemon"
    exit 1
fi
echo "   $(tail -1 "$WORK/daemon_test.txt")"

echo "==> 3) install S37_RoiQp + default config"
install -m 0755 "$INIT_SRC" rootfs_unpacked/etc/init.d/S37_RoiQp
install -m 0644 "$CONF_SRC" rootfs_unpacked/etc/soccercam_roi.conf.default
echo "   /etc/init.d/S37_RoiQp + /etc/soccercam_roi.conf.default"

echo "==> 3b) record this layer in the build manifest (/etc/soccercam_build)"
# Every pak reports the same stock version string, so build.txt is the only way
# to tell from the network which firmware a camera is running -- and therefore
# the only way to tell that THIS flash took. Inherit the base's manifest and
# overwrite the lines this layer owns.
COMMIT="${SOCCERCAM_COMMIT:-$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo unknown)}"
MAN=rootfs_unpacked/etc/soccercam_build
touch "$MAN"
grep -vE '^(pak|roiqp|roiqp_default|roiqp_commit)=' "$MAN" > "$MAN.new" || true
{
    cat "$MAN.new"
    echo "pak=$(basename "$OUT")"
    echo "roiqp=S37+kflow_videoenc_setroi_fix${MULTIWIN_TAG}"
    echo "roiqp_default=$(grep -E '^enable=' "$CONF_SRC" | head -1 | tr -d '\r')"
    echo "roiqp_commit=$COMMIT"
} > "$MAN"
rm -f "$MAN.new"
chmod 644 "$MAN"
sed 's/^/   /' "$MAN"

echo "==> 4) repack rootfs squashfs"
mksquashfs rootfs_unpacked rootfs_new.bin \
    -comp xz -b 262144 -noappend -no-progress \
    -no-exports -all-root -mkfs-time 0 -all-time 0 \
    >/dev/null
ROOTFS_SZ=$(stat -c %s rootfs_new.bin)
echo "   rootfs $ROOTFS_SZ bytes"
[[ "$ROOTFS_SZ" -lt $((8*1024*1024)) ]] || { echo "ERROR: rootfs exceeds its 8 MiB partition"; exit 1; }

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

echo "==> 7b) GATE: this pak must not record at home"
# Runs on the finished pak and before it is moved into place, so a failure
# leaves no output file behind to be flashed by mistake.
if [[ "${ROIQP_ALLOW_RECORD_AT_HOME:-0}" == "1" ]]; then
    echo "   SKIPPED: ROIQP_ALLOW_RECORD_AT_HOME=1 (this pak may record on the home network)"
else
    bash "$HERE/../verify/check_recording_default.sh" "$WORK/out.pak"
fi

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
echo "  - /etc/soccercam_build:                   pak= and roiqp= lines updated"
echo
echo " First checks after flashing (docs/FAR_FIELD_BITS.md, runbook D and E):"
echo "   1. curl http://<cam>/downloadfile/soccercam/build.txt  -> names this pak."
echo "   2. tail /mnt/sda/soccercam/roi.log"
echo "      -> a daemon start line, then what it parsed and the kernel's echo of"
echo "         every write. With the default config: 'encoder untouched'."
echo "   3. echo vdoenc setroi 0 0 1 -6 1 0 760 7680 900 > /proc/hdal/venc/cmd"
echo "      dmesg | grep 'Set ROI Index' | tail -1"
echo "      -> MUST read back QP = -6, QPMode = 1; then disable it again."
echo "   /proc/hdal/venc/info never lists these windows; it is not a readback."
echo "====================================================================="
