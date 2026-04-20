#!/bin/bash
# Set the camera's exposure mode via the Reolink JSON API.
# No firmware patch required -- this is vanilla stock-firmware functionality
# that the web UI just doesn't expose directly.
#
# Settings persist across reboots (Reolink stores them in the `para` partition).
#
# Usage:
#   bash set_exposure.sh auto
#   bash set_exposure.sh antismear                   # best for fast motion (soccer)
#   bash set_exposure.sh lownoise                    # best for low-light / static
#   bash set_exposure.sh manual <shutter> <gain>
#
# shutter: 0..125  (higher = faster shutter = less motion blur, more noise)
# gain:    1..100  (higher = more amplification = brighter but noisier)
#
# Typical recipes:
#   soccer action: antismear
#   harsh sun:     manual 110 20
#   indoor low-light: lownoise
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_camera_env.sh
source "$HERE/../_camera_env.sh"

MODE="${1:?usage: $0 <auto|antismear|lownoise|manual> [shutter] [gain]}"
SHUTTER="${2:-60}"
GAIN="${3:-32}"

case "$MODE" in
    auto)      EXP="Auto" ;;
    antismear) EXP="Anti-Smearing" ;;
    lownoise)  EXP="LowNoise" ;;
    manual)    EXP="Manual" ;;
    *) echo "unknown mode: $MODE"; exit 1 ;;
esac

TOKEN=$(camera_login)

# Fetch current config so we only modify what's relevant
for attempt in 1 2 3; do
    CUR=$(curl -s -X POST -H "Content-Type: application/json" \
      -d '[{"cmd":"GetIsp","action":0,"param":{"channel":0}}]' \
      "http://$CAMERA_IP/cgi-bin/api.cgi?cmd=GetIsp&token=$TOKEN")
    echo "$CUR" | python3 -c 'import json,sys; d=json.load(sys.stdin)[0]; exit(0 if d.get("code")==0 else 1)' 2>/dev/null && break
    echo "  (retry $attempt)"; sleep 1
done

BODY=$(python3 - <<PY
import json
cur = json.loads('''$CUR''')[0]["value"]["Isp"]
cur["exposure"] = "$EXP"
if "$EXP" == "Manual":
    cur["shutter"] = {"min": $SHUTTER, "max": $SHUTTER}
    cur["gain"] = {"min": $GAIN, "max": $GAIN}
else:
    # Restore full ranges so AE can adapt within each mode's policy
    cur["shutter"] = {"min": 0, "max": 125}
    cur["gain"] = {"min": 1, "max": 100}
print(json.dumps([{"cmd":"SetIsp","action":0,"param":{"Isp":cur}}]))
PY
)

echo "==> SetIsp exposure=$EXP"
curl -s -X POST -H "Content-Type: application/json" -d "$BODY" \
    "http://$CAMERA_IP/cgi-bin/api.cgi?cmd=SetIsp&token=$TOKEN" \
    | python3 -m json.tool

echo
echo "==> Verify:"
curl -s -X POST -H "Content-Type: application/json" \
  -d '[{"cmd":"GetIsp","action":0,"param":{"channel":0}}]' \
  "http://$CAMERA_IP/cgi-bin/api.cgi?cmd=GetIsp&token=$TOKEN" \
  > /tmp/isp_verify.json
python3 - <<'PY'
import json
isp = json.load(open("/tmp/isp_verify.json"))[0]["value"]["Isp"]
print("  exposure    :", isp["exposure"])
print("  shutter     :", isp["shutter"])
print("  gain        :", isp["gain"])
print("  antiFlicker :", isp["antiFlicker"])
print("  dayNight    :", isp["dayNight"])
PY
