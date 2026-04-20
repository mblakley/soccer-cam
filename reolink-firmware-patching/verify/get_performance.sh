#!/bin/bash
# Poll GetPerformance in a loop to watch the camera's encoder output rate
# and CPU load. Useful to watch the actual bitrate spike as you expose the
# camera to busier scenes, and to confirm a patched max-bitrate is being
# used.
#
# Usage:
#   bash get_performance.sh            # poll every 2s indefinitely
#   bash get_performance.sh 1          # poll every 1s
#   bash get_performance.sh 0.5 30     # poll every 0.5s for 30 iterations
set -euo pipefail

INTERVAL="${1:-2}"
MAX_ITERS="${2:-0}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_camera_env.sh
source "$HERE/../_camera_env.sh"

TOKEN=$(camera_login)

echo "Polling GetPerformance every ${INTERVAL}s (Ctrl-C to stop)"
echo
printf "%-20s  %-14s  %-10s  %-14s\n" "time" "codecRate_kbps" "cpu_%" "net_KB/s"
i=0
while true; do
    resp=$(curl -s -X POST -H "Content-Type: application/json" \
      -d '[{"cmd":"GetPerformance","action":0,"param":{}}]' \
      "http://$CAMERA_IP/cgi-bin/api.cgi?cmd=GetPerformance&token=$TOKEN")
    python3 - <<PY
import json, sys, time
d = json.loads('''$resp''')[0]
if d.get("code") != 0:
    print(f"  ERR: {d.get('error')}"); sys.exit()
p = d["value"]["Performance"]
ts = time.strftime("%H:%M:%S")
print(f"{ts:<20}  {p['codecRate']:<14}  {p['cpuUsed']:<10}  {p['netThroughput']:<14}")
PY
    i=$((i + 1))
    if [[ "$MAX_ITERS" -gt 0 && "$i" -ge "$MAX_ITERS" ]]; then break; fi
    sleep "$INTERVAL"
done
