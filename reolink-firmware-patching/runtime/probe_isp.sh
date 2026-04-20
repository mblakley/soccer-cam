#!/bin/bash
# Dump the camera's full ISP state AND the allowed-value ranges (action=1).
# Use this to discover what settings are exposed on your firmware.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_camera_env.sh
source "$HERE/../_camera_env.sh"

TOKEN=$(camera_login)

echo "==== GetIsp action=1 (current + allowed ranges) ===="
curl -s -X POST -H "Content-Type: application/json" \
  -d '[{"cmd":"GetIsp","action":1,"param":{"channel":0}}]' \
  "http://$CAMERA_IP/cgi-bin/api.cgi?cmd=GetIsp&token=$TOKEN" \
  | python3 -m json.tool
