#!/bin/bash
# Sourced helper: loads camera.env (creating fresh if it doesn't exist).
# All runtime / verify scripts: `source "$(dirname "$0")/../_camera_env.sh"`
set -euo pipefail

# Walk up to find camera.env at the project root (this script's parent dir)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HERE/camera.env"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE not found. Copy camera.env.example and fill in your values." >&2
    exit 1
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

: "${CAMERA_IP:?CAMERA_IP must be set in camera.env}"
: "${CAMERA_USER:?CAMERA_USER must be set in camera.env}"
: "${CAMERA_PASS:?CAMERA_PASS must be set in camera.env}"

# Convenience: acquire an API token. Retries up to 3x if the camera is slow.
camera_login() {
    local attempt
    for attempt in 1 2 3; do
        local tok
        tok=$(curl -s -X POST -H "Content-Type: application/json" \
          -d "[{\"cmd\":\"Login\",\"action\":0,\"param\":{\"User\":{\"userName\":\"$CAMERA_USER\",\"password\":\"$CAMERA_PASS\"}}}]" \
          "http://$CAMERA_IP/cgi-bin/api.cgi?cmd=Login" 2>/dev/null \
          | python3 -c 'import json,sys; d=json.load(sys.stdin)[0]; print(d.get("value",{}).get("Token",{}).get("name","") if d.get("code")==0 else "")' 2>/dev/null || true)
        if [[ -n "${tok:-}" ]]; then
            echo "$tok"
            return 0
        fi
        sleep 2
    done
    echo "ERROR: could not obtain API token from $CAMERA_IP" >&2
    return 1
}
