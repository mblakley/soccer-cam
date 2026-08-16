#!/bin/bash
# check_recording_default.sh -- refuse any pak that would record at home.
#
# The camera must idle on the home network and record everywhere else. That is
# S99_NetState's job: it polls the default-gateway MAC and disables recording on
# a confirmed home MAC. The daemon has one kill switch --
# /mnt/sda/netstate/override -- whose presence makes it yield entirely, handing
# recording control back to whatever the stored config says.
#
# That kill switch is fine as an operator action. It is NOT fine baked into a
# build. A build that asserts the override at boot silently defeats home
# detection on every power-on, and the failure is invisible: recording simply
# stays on at home and fills the card with footage of a garage.
#
# That is exactly what happened. The 2026-08-16 investigation builds carried an
# init script (S36_StitchProbe) that ran `touch /mnt/sda/netstate/override`
# twice during boot. Every boot from 13:31 onward logged
# "override present -- daemon yields", and the camera recorded at home until it
# was noticed by hand. 34 stub files had accumulated by the time the flag was
# removed.
#
# This runs against a BUILT pak, before flashing, and exits non-zero on any
# violation. It is a gate, not a warning -- per the project rule that in an
# automated chain a warning that nothing reads is not a guard.
#
# Usage:
#   verify/check_recording_default.sh <path/to.pak>
#
# Requires: unsquashfs (mtd-utils/squashfs-tools). On Windows run it under WSL.

set -euo pipefail

PAK="${1:-}"
if [ -z "$PAK" ] || [ ! -f "$PAK" ]; then
    echo "usage: $0 <path/to.pak>" >&2
    exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "==> extracting rootfs from $(basename "$PAK")"
python3 - "$PAK" "$WORK/rootfs.bin" <<'PY'
import sys
sys.path.insert(0, __import__("os").path.join(__import__("os").path.dirname(sys.argv[0]) or ".", ".."))
from pak import pak  # reolink-firmware-patching/pak/pak.py
data = open(sys.argv[1], "rb").read()
for s in pak.parse(data):
    if s["name"] == "rootfs" and s["size"]:
        open(sys.argv[2], "wb").write(data[s["offset"]: s["offset"] + s["size"]])
        break
else:
    raise SystemExit("no rootfs section in pak")
PY

unsquashfs -d "$WORK/rfs" "$WORK/rootfs.bin" > /dev/null
INIT="$WORK/rfs/etc/init.d"
FAIL=0

echo "==> 1) the netstate daemon must be present"
if ls "$INIT" | grep -q '^S99_NetState$'; then
    echo "    ok: S99_NetState installed"
else
    echo "    FAIL: no S99_NetState -- nothing would ever disable recording at home"
    FAIL=1
fi

echo "==> 2) no init script may assert the override kill switch"
HITS="$(grep -rln 'netstate/override' "$INIT" 2>/dev/null || true)"
if [ -z "$HITS" ]; then
    echo "    ok: no init script references the override flag"
else
    # Referencing it to *read* it is the daemon's own job; asserting it is not.
    for f in $HITS; do
        if grep -qE '^[^#]*(touch|>|cp|echo).*netstate/override' "$f"; then
            echo "    FAIL: $(basename "$f") asserts /mnt/sda/netstate/override"
            grep -nE '^[^#]*(touch|>|cp|echo).*netstate/override' "$f" | sed 's/^/           /'
            FAIL=1
        else
            echo "    ok: $(basename "$f") only reads the flag"
        fi
    done
fi

echo "==> 3) the daemon must carry a home MAC list"
NS="$INIT/S99_NetState"
if [ -f "$NS" ]; then
    MACS="$(grep -E '^HOME_MACS_DEFAULT=' "$NS" | head -1 | cut -d'"' -f2 || true)"
    if [ -n "$MACS" ]; then
        echo "    ok: HOME_MACS_DEFAULT=\"$MACS\""
    else
        echo "    FAIL: HOME_MACS_DEFAULT is empty -- every network looks like away"
        FAIL=1
    fi
fi

echo
if [ "$FAIL" -ne 0 ]; then
    echo "REFUSING: this pak would record on the home network."
    exit 1
fi
echo "PASS: recording defaults to disabled at home."
