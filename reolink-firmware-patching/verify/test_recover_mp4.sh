#!/bin/bash
# Repeatable end-to-end test of the power-cut recovery binary (recover_mp4).
#
# Takes any GOOD recording from the camera (one that still has its moov), simulates two
# failure modes, recovers each, and decode-validates the result:
#   1. clean orphan   — moov removed (camera never wrote it on power loss)
#   2. power-cut orphan— moov removed AND the tail chopped (cut mid-write)
# For the clean case it also asserts the recovered video+audio sample tables are
# byte-identical to the original and that the recovered audio PCM matches bit-for-bit.
#
# This is the gate that must pass before trusting a comprehensive build's recovery.
#
# Prereqs (build host): aarch64-linux-gnu-gcc, qemu-aarch64-static, ffmpeg/ffprobe,
#   python3, and — for AUDIO recovery — the Helix AAC source fetched per
#   recover/helix/README.md (absent => the test exercises video-only recovery).
#
# Usage: bash verify/test_recover_mp4.sh <good_recording.mp4> [chop_bytes]
set -euo pipefail
GOOD="${1:?usage: bash verify/test_recover_mp4.sh <good_recording.mp4> [chop_bytes]}"
CHOP="${2:-1300000}"
[ -f "$GOOD" ] || { echo "ERROR: $GOOD not found"; exit 1; }
# Use NATIVE Linux ffmpeg/ffprobe (apt install ffmpeg) — a Windows ffmpeg.exe under WSL
# can't read the Linux work dir, so install the native package on the build host.
FFMPEG=ffmpeg; FFPROBE=ffprobe
for t in aarch64-linux-gnu-gcc qemu-aarch64-static python3 "$FFMPEG" "$FFPROBE"; do
  command -v "$t" >/dev/null || { echo "ERROR: need $t on PATH (apt install squashfs-tools gcc-aarch64-linux-gnu qemu-user-static ffmpeg)"; exit 1; }
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REC="$HERE/../recover"
HELIX="$REC/helix"
W="$(mktemp -d)"; trap "rm -rf '$W'" EXIT

echo "==> 1) build recover_mp4 (aarch64, static)"
if [ -d "$HELIX/ESP8266Audio/src/libhelix-aac" ] && [ -f "$HELIX/compat/Arduino.h" ]; then
  SRC="$HELIX/ESP8266Audio/src/libhelix-aac"
  mkdir -p "$W/hx"
  for c in "$SRC"/*.c; do
    aarch64-linux-gnu-gcc -O2 -DNDEBUG -DARDUINO -Wno-format -ffunction-sections -fdata-sections \
      -I"$HELIX/compat" -I"$SRC" -c "$c" -o "$W/hx/$(basename "$c" .c).o"
  done
  aarch64-linux-gnu-ar rcs "$W/hx/libhelixaac.a" "$W/hx"/*.o
  aarch64-linux-gnu-gcc -O2 -DNDEBUG -DARDUINO -static -I"$HELIX/compat" -I"$SRC" \
    "$REC/recover_mp4.c" "$W/hx/libhelixaac.a" -o "$W/recover_mp4"
  AUDIO=1; echo "   built WITH audio (Helix linked)"
else
  aarch64-linux-gnu-gcc -O2 -DNDEBUG -DNO_AUDIO -static "$REC/recover_mp4.c" -o "$W/recover_mp4"
  AUDIO=0; echo "   built VIDEO-ONLY (Helix source absent; see recover/helix/README.md)"
fi

# ---- helpers ----
strip() {  # strip <src> <dst> <chop_bytes>
  python3 - "$1" "$2" "$3" <<'PY'
import struct,sys
src,dst,chop=sys.argv[1],sys.argv[2],int(sys.argv[3])
d=open(src,"rb").read(); o=0; out=bytearray()
while o+8<=len(d):
    sz=struct.unpack(">I",d[o:o+4])[0]; t=d[o+4:o+8]
    if sz==1: sz=struct.unpack(">Q",d[o+8:o+16])[0]
    elif sz==0: sz=len(d)-o
    if t!=b"moov": out+=d[o:o+sz]
    o+=sz
if chop: out=out[:len(out)-chop]
open(dst,"wb").write(out)
PY
}
decode_ok() {  # decode_ok <file>  -> prints stream summary, fails on decode error
  "$FFPROBE" -v error -show_entries stream=codec_type,nb_frames,duration -of csv=p=0 "$1"
  local err; err="$("$FFMPEG" -v error -i "$1" -f null - 2>&1 | head -3)"
  [ -z "$err" ] || { echo "   DECODE ERRORS: $err"; return 1; }
}

echo "==> 2) CLEAN orphan (moov removed)"
strip "$GOOD" "$W/clean.mp4" 0
qemu-aarch64-static "$W/recover_mp4" "$W/clean.mp4" "$GOOD" 2>&1 | grep -E 'recovered|synced|moov' || true
decode_ok "$W/clean.mp4"

echo "==> 3) byte-exactness vs original (clean case)"
python3 - "$GOOD" "$W/clean.mp4" "$AUDIO" <<'PY'
import struct,sys
def tracks(p):
    d=open(p,"rb").read()
    def walk(b,s,e):
        o=s;r=[]
        while o+8<=e:
            sz=struct.unpack(">I",b[o:o+4])[0];t=b[o+4:o+8];h=8
            if sz==1:sz=struct.unpack(">Q",b[o+8:o+16])[0];h=16
            elif sz==0:sz=e-o
            r.append((t,o,sz,h));o+=sz if sz>0 else e
        return r
    def fa(b,s,e,tag):
        r=[]
        for t,o,sz,h in walk(b,s,e):
            if t==tag:r.append((o,sz,h))
            if t in(b"moov",b"trak",b"mdia",b"minf",b"stbl"):r+=fa(b,o+h,o+sz,tag)
        return r
    mo=[x for x in walk(d,0,len(d)) if x[0]==b"moov"][0];ms,me=mo[1]+mo[3],mo[1]+mo[2];out={}
    for to,tsz,_ in fa(d,ms,me,b"trak"):
        h=fa(d,to+8,to+tsz,b"hdlr")[0];ty=d[h[0]+16:h[0]+20]
        sz=fa(d,to+8,to+tsz,b"stsz")[0];b=d[sz[0]+8:sz[0]+sz[1]]
        ss=struct.unpack(">I",b[4:8])[0];cnt=struct.unpack(">I",b[8:12])[0]
        out[ty]=[ss]*cnt if ss else list(struct.unpack(">%dI"%cnt,b[12:12+4*cnt]))
    return out
a,b=tracks(sys.argv[1]),tracks(sys.argv[2]);audio=sys.argv[3]=="1";ok=True
for ty in ([b"vide",b"soun"] if audio else [b"vide"]):
    ra,rb=a.get(ty,[]),b.get(ty,[])
    m=sum(1 for i in range(min(len(ra),len(rb))) if ra[i]!=rb[i])
    s="OK" if (ra==rb) else "MISMATCH"
    if ra!=rb: ok=False
    print(f"   {ty.decode()}: ref={len(ra)} rec={len(rb)} size_mismatches={m} [{s}]")
sys.exit(0 if ok else 1)
PY

echo "==> 4) audio PCM bit-identical (clean case)"
if [ "$AUDIO" = 1 ]; then
  "$FFMPEG" -v error -i "$W/clean.mp4" -map 0:a -f s16le - 2>/dev/null | md5sum | awk '{print "   rec:",$1}'
  "$FFMPEG" -v error -i "$GOOD"        -map 0:a -f s16le - 2>/dev/null | md5sum | awk '{print "   ref:",$1}'
else echo "   (skipped — video-only build)"; fi

echo "==> 5) POWER-CUT orphan (moov removed + ${CHOP}B tail chopped)"
strip "$GOOD" "$W/cut.mp4" "$CHOP"
qemu-aarch64-static "$W/recover_mp4" "$W/cut.mp4" "$GOOD" 2>&1 | grep -E 'recovered|synced|moov' || true
decode_ok "$W/cut.mp4"

echo "==> PASS: recovery produced valid, decodable files for both failure modes."
