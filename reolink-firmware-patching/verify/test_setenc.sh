#!/bin/bash
# Apply a SetEnc request with an explicit bitRate value and show the response.
# Used to find the encoder's actual hardware ceiling: try increasing values
# until the API returns rspCode -13 "set config failed".
#
# Usage:
#   bash test_setenc.sh <bitrate_kbps>
# Example:
#   bash test_setenc.sh 20480
set -euo pipefail

KBPS="${1:?usage: $0 <bitrate_kbps>}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_camera_env.sh
source "$HERE/../_camera_env.sh"

TOKEN=$(camera_login)

# Fetch current encoder config
curl -s -X POST -H "Content-Type: application/json" \
  -d '[{"cmd":"GetEnc","action":0,"param":{"channel":0}}]' \
  "http://$CAMERA_IP/cgi-bin/api.cgi?cmd=GetEnc&token=$TOKEN" > /tmp/getenc_pre.json

# Build a SetEnc request reusing every field from current, except the new
# main-stream bitrate
python3 - <<PY > /tmp/setenc_body.json
import json
cur = json.load(open("/tmp/getenc_pre.json"))[0]["value"]["Enc"]
cur["mainStream"]["bitRate"] = $KBPS
print(json.dumps([{"cmd":"SetEnc","action":0,"param":{"Enc":cur}}]))
PY

echo "==== SetEnc bitRate=$KBPS ===="
curl -s -X POST -H "Content-Type: application/json" -d @/tmp/setenc_body.json \
    "http://$CAMERA_IP/cgi-bin/api.cgi?cmd=SetEnc&token=$TOKEN" \
    | python3 -m json.tool
echo
echo "  rspCode meanings (partial list):"
echo "    0   : success"
echo "    -4  : param error (value not in allowed range)"
echo "    -13 : set config failed (encoder hardware refused)"
