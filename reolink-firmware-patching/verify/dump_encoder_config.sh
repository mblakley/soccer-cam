#!/bin/bash
# Dump the camera's full encoder configuration (GetEnc action=0 + action=1).
# Use to confirm a flashed patch took effect at the API level (e.g. to see
# a new max bitrate or fps value in the range).
#
# action=0 -> current config (scalar values)
# action=1 -> current + allowed ranges + initial/default
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_camera_env.sh
source "$HERE/../_camera_env.sh"

TOKEN=$(camera_login)

echo "==== GetEnc action=0 (current values) ===="
curl -s -X POST -H "Content-Type: application/json" \
  -d '[{"cmd":"GetEnc","action":0,"param":{"channel":0}}]' \
  "http://$CAMERA_IP/cgi-bin/api.cgi?cmd=GetEnc&token=$TOKEN" \
  | python3 -m json.tool

echo
echo "==== GetEnc action=1 (current + range + initial) ===="
curl -s -X POST -H "Content-Type: application/json" \
  -d '[{"cmd":"GetEnc","action":1,"param":{"channel":0}}]' \
  "http://$CAMERA_IP/cgi-bin/api.cgi?cmd=GetEnc&token=$TOKEN" \
  | python3 -m json.tool
