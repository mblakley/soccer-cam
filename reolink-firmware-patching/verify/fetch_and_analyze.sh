#!/bin/bash
# Download the most recent main-stream recording via the unlocked HTTP path
# and run ffprobe on it to measure the actual encoded bitrate, fps, and
# per-frame characteristics.
#
# This is the end-to-end validator for any encoder patch: the ONLY truth
# is what the encoder actually emitted, not what the API or dropdown says.
#
# Usage:
#   bash fetch_and_analyze.sh [tag]
#
# Optional [tag] gets appended to filenames so you can do A/B runs
# without overwriting.
#
# Requires:
#   - /downloadfile/ HTTP unlock must be flashed (build_http_unlock.sh)
#   - ffprobe.exe on $PATH, or adjust FFPROBE below
#   - camera must have a recording in the last 10 minutes (motion-triggered
#     recording enabled, OR you started a manual recording)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_camera_env.sh
source "$HERE/../_camera_env.sh"

TAG="${1:-latest}"
OUT_DIR="${OUT_DIR:-$HERE}"
FFPROBE="${FFPROBE:-ffprobe}"

TOKEN=$(camera_login)

# Search last 10 minutes
NOW=$(date +%s)
python3 - <<PY > /tmp/search.json
import json, datetime
s = datetime.datetime.fromtimestamp($NOW - 600)
e = datetime.datetime.fromtimestamp($NOW)
def t(d): return {"year":d.year,"mon":d.month,"day":d.day,"hour":d.hour,"min":d.minute,"sec":d.second}
print(json.dumps([{"cmd":"Search","action":0,"param":{"Search":{"channel":0,"streamType":"main","onlyStatus":0,"StartTime":t(s),"EndTime":t(e)}}}]))
PY
curl -s -X POST -H "Content-Type: application/json" -d @/tmp/search.json \
    "http://$CAMERA_IP/cgi-bin/api.cgi?cmd=Search&token=$TOKEN" > /tmp/search_resp.json

FILE=$(python3 - <<'PY'
import json
d = json.load(open("/tmp/search_resp.json"))[0]
vals = d.get("value", {}).get("SearchResult", {}).get("File", [])
if not vals:
    print("NONE"); exit()
vals.sort(key=lambda f: (f["StartTime"]["year"], f["StartTime"]["mon"], f["StartTime"]["day"],
                         f["StartTime"]["hour"], f["StartTime"]["min"], f["StartTime"]["sec"]),
          reverse=True)
print(vals[0]["name"])
PY
)

if [[ "$FILE" == "NONE" ]]; then
    echo "No recording found in last 10 minutes. Enable motion recording or start a manual recording."
    exit 1
fi

URL_PATH="${FILE#/mnt/sda/}"
DEST="$OUT_DIR/capture_${TAG}.mp4"
echo "==== downloading /downloadfile/$URL_PATH"
time curl -s -u "$CAMERA_USER:$CAMERA_PASS" "http://$CAMERA_IP/downloadfile/$URL_PATH" -o "$DEST"
echo "   -> $DEST  ($(stat -c%s "$DEST") bytes)"
echo

# Convert to Windows path for ffprobe.exe if we're in WSL
if [[ "$DEST" == /mnt/c/* ]]; then
    PROBE_PATH=$(echo "$DEST" | sed 's|/mnt/c/|C:/|')
else
    PROBE_PATH="$DEST"
fi

echo "==== ffprobe summary"
"$FFPROBE" -v error \
    -show_entries stream=codec_name,width,height,avg_frame_rate,r_frame_rate,bit_rate,nb_frames \
    -show_entries format=duration,bit_rate,size \
    -of default=noprint_wrappers=0 "$PROBE_PATH"
echo

echo "==== per-frame stats"
"$FFPROBE" -v error -select_streams v -show_entries frame=pkt_size,pict_type \
    -of csv=p=0 "$PROBE_PATH" > "$OUT_DIR/capture_${TAG}_frames.csv"

python3 - <<PY
import csv, statistics
rows = [r for r in csv.reader(open("$OUT_DIR/capture_${TAG}_frames.csv")) if len(r) >= 2]
sizes = [int(r[0]) for r in rows]
types = [r[1] for r in rows]
n, total = len(sizes), sum(sizes)
if n == 0: print("NO FRAMES"); exit()
print(f"  frames          : {n}  (I={types.count('I')} P={types.count('P')} B={types.count('B')})")
print(f"  total           : {total:,} bytes")
print(f"  avg frame       : {total//n:,} bytes")
print(f"  stdev           : {int(statistics.stdev(sizes) if n>1 else 0):,} bytes")
i_sz = [sizes[i] for i in range(n) if types[i]=='I']
p_sz = [sizes[i] for i in range(n) if types[i]=='P']
if i_sz: print(f"  avg I-frame     : {sum(i_sz)//len(i_sz):,} bytes  (max {max(i_sz):,})")
if p_sz: print(f"  avg P-frame     : {sum(p_sz)//len(p_sz):,} bytes  (max {max(p_sz):,})")
PY

echo
echo "Compare capture_${TAG}.mp4 vs a baseline by running this script twice"
echo "with different tags between configuration changes."
