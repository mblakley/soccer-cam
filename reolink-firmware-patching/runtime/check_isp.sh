#!/bin/bash
# Print the camera's current ISP state (exposure, shutter, gain, etc.).
# Useful as a persistence-across-reboot check after running set_exposure.sh.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_camera_env.sh
source "$HERE/../_camera_env.sh"

TOKEN=$(camera_login)

curl -s -X POST -H "Content-Type: application/json" \
  -d '[{"cmd":"GetIsp","action":0,"param":{"channel":0}}]' \
  "http://$CAMERA_IP/cgi-bin/api.cgi?cmd=GetIsp&token=$TOKEN" > /tmp/check_isp.json

python3 - <<'PY'
import json
d = json.load(open("/tmp/check_isp.json"))[0]
if d.get("code") != 0:
    print("ERR:", d.get("error")); exit(1)
isp = d["value"]["Isp"]
print("  exposure   :", isp["exposure"])
print("  shutter    :", isp["shutter"])
print("  gain       :", isp["gain"])
print("  antiFlicker:", isp["antiFlicker"])
print("  dayNight   :", isp["dayNight"])
print("  backLight  :", isp["backLight"])
PY
