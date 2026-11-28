#!/bin/sh
# Offline test of runtime/roiqp/S38_FieldRoi: when it looks, what it accepts,
# and that it leaves the encoder at stock whenever it is not sure.
#
#   sh verify/test_fieldroi_hook.sh            (also: bash, dash, busybox sh)
#
# The hook is sourced with FIELDROI_SOURCE_ONLY=1. Its two touch points with the
# camera are replaced: snapshot() succeeds or fails as scripted, and
# run_detector() plays back a list of answers ("far near", or "refuse"). Waiting
# is instant and counted. The recording state is read by the hook's own code
# from a stand-in for S99_NetState's log, and every roi.conf it writes is parsed
# by S37_RoiQp's own parser.

set -u
HERE=$(cd "$(dirname "$0")" && pwd)
HOOK="$HERE/../runtime/roiqp/S38_FieldRoi"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

FIELDROI_SOURCE_ONLY=1
FIELDROI_DIR="$WORK/card"
FIELDROI_DEFAULT="$HERE/../runtime/roiqp/fieldroi.conf.default"
FIELDROI_NETSTATE_LOG="$WORK/netstate.log"
FIELDROI_NETSTATE_INIT="$WORK/S99_NetState"
mkdir -p "$FIELDROI_DIR/bin"
# shellcheck disable=SC1090
. "$HOOK"

# --- stand-ins ---------------------------------------------------------------
SLEPT=0
LOOKS=0
ANSWERS=""      # one answer per look: "far:near", "refuse", or "nosnap"
STOP_AFTER=""   # seconds of waiting after which recording stops
pause() {
    SLEPT=$((SLEPT + $1))
    if [ -n "$STOP_AFTER" ] && [ $SLEPT -ge "$STOP_AFTER" ]; then
        echo "[t] state -> home: recording DISABLED" >>"$FIELDROI_NETSTATE_LOG"
        STOP_AFTER=""
    fi
}
next_answer() {
    ANSWER=${ANSWERS%% *}
    case "$ANSWERS" in *" "*) ANSWERS=${ANSWERS#* } ;; *) ANSWERS="" ;; esac
    [ -n "$ANSWER" ] || ANSWER=refuse
}
snapshot() {
    LOOKS=$((LOOKS + 1))
    next_answer
    if [ "$ANSWER" = nosnap ]; then SNAP_WHY="scripted failure"; return 1; fi
    echo jpeg >"$1"
}
run_detector() {
    if [ "$ANSWER" = refuse ]; then
        : >"$2"
        echo "fieldroi: REFUSED: scripted" >"$3"
        return 3
    fi
    FAR=${ANSWER%:*}; NEAR=${ANSWER#*:}
    echo "field rows $FAR..$NEAR  ->  window rows 0..1616 (5 far, 5 near keypoints)" >"$3"
    printf 'enable=1\npath=0\nmode=1\nframe_w=7680\nframe_h=2160\nwin0=0,%s,7680,256,-10\nwin1=0,%s,7680,320,-6\nwin2=0,%s,7680,384,-3\n' \
        "$FAR" "$((FAR + 256))" "$((FAR + 576))" >"$2"
}

# --- harness -----------------------------------------------------------------
FAILS=0
CHECKS=0
check() {  # check <what> <got> <want>
    CHECKS=$((CHECKS + 1))
    if [ "$2" = "$3" ]; then
        echo "  [ok] $1"
    else
        echo "  [FAIL] $1"
        echo "         got:  $2"
        echo "         want: $3"
        FAILS=$((FAILS + 1))
    fi
}
logged() { grep -c -- "$1" "$LOG" 2>/dev/null; }
roi() {  # what S37_RoiQp makes of the roi.conf the hook left
    [ -f "$ROI_CONF" ] || { echo "none"; return; }
    ( ROIQP_SOURCE_ONLY=1; ROIQP_CONF_DIR="$WORK/s37"; ROIQP_BUILD_INFO="$WORK/build"
      export ROIQP_SOURCE_ONLY ROIQP_CONF_DIR ROIQP_BUILD_INFO
      printf 'roiqp=S37+kflow_videoenc_setroi_fix+multiwin\n' >"$WORK/build"
      # shellcheck disable=SC1091
      . "$HERE/../runtime/roiqp/S37_RoiQp"
      if parse_conf "$ROI_CONF"; then
          if [ "$C_ENABLE" -eq 1 ]; then echo "on far=$(echo "$C_WIN_0" | cut -d' ' -f2)"; else echo "off"; fi
      else echo "INVALID: $REASON"; fi )
}
boot() {  # boot <recording on|off> <settings lines...>  -- a fresh start
    REC=$1; shift
    rm -f "$CONF" "$ROI_CONF" "$LOG" "$TRIGGER" "$FIELDROI_NETSTATE_LOG"
    [ $# -gt 0 ] && printf '%s\n' "$@" >"$CONF"
    : >"$BIN"; chmod 755 "$BIN"; : >"$WEIGHTS"
    printf 'USER="admin"\nPASS="x"\n' >"$FIELDROI_NETSTATE_INIT"
    if [ "$REC" = on ]; then echo "[t] state -> away: recording ENABLED (TIMING all-on)" >"$FIELDROI_NETSTATE_LOG"
    else echo "[t] state -> home: recording DISABLED" >"$FIELDROI_NETSTATE_LOG"; fi
    STATE=off; WARNED=""; SLEPT=0; LOOKS=0; ANSWERS=""; STOP_AFTER=""
}
go() { tick >/dev/null; }

echo "off by default"
boot on
ANSWERS="200:1400 200:1400"
go
check "no settings file: the baked default is auto=0, nothing is looked at, roi.conf untouched" "$LOOKS $(roi)" "0 none"
boot on auto=0
echo "enable=1" >"$ROI_CONF"
go
check "auto=0 leaves a roi.conf written by hand exactly as it is" "$LOOKS $(cat "$ROI_CONF")" "0 enable=1"

echo "a camera that is already in place"
boot on auto=1
ANSWERS="200:1400 212:1391"
go
check "waits the default 60 s before the first look" "$SLEPT" 60
check "two looks that agree install the bands of the second" "$LOOKS $(roi)" "2 on far=212"
check "  and the file says who wrote it" "$(head -1 "$ROI_CONF" | cut -c1-25)" "# written by S38_FieldRoi"
LOOKS=0; SLEPT=0
go
check "nothing more happens while the recording carries on" "$LOOKS $SLEPT" "0 0"

echo "a camera still being put up"
boot on auto=1 settle_secs=30
ANSWERS="900:2100 520:1700 204:1398 210:1405"
go
check "settle_secs is honoured" "$SLEPT" 30
check "looks that disagree are not used; the first pair that agrees is" "$LOOKS $(roi)" "4 on far=210"
boot on auto=1
ANSWERS="200:1400 300:1400 400:1400 500:1400 600:1400 600:1400"
go
check "five looks, each 100 rows from the last: gives up, bands off" "$LOOKS $(roi) $(logged 'no two looks in a row agreed')" "5 off 1"
boot on auto=1
ANSWERS="200:1400 200:1500"
go
check "a near edge that moved 100 rows is a disagreement too" "$(roi)" "off"
boot on auto=1
ANSWERS="200:1400 296:1496"
go
check "96 rows is still agreement (the measured jitter is 81)" "$(roi)" "on far=296"

echo "refusals and failures"
boot on auto=1
ANSWERS="refuse refuse refuse refuse refuse"
go
check "the detector refuses every frame: bands off" "$LOOKS $(roi)" "5 off"
boot on auto=1
ANSWERS="200:1400 refuse 204:1404 206:1398"
go
check "a refusal between two looks breaks the pair; the next two agree" "$LOOKS $(roi)" "4 on far=206"
boot on auto=1
ANSWERS="nosnap 200:1400 nosnap 200:1400 200:1400"
go
check "a failed snapshot breaks the pair as well" "$LOOKS $(roi)" "5 on far=200"
boot on auto=1
rm -f "$BIN"
go
check "auto=1 with no detector on the card: logged once, nothing written" "$LOOKS $(roi) $(logged 'are missing')" "0 none 1"
boot on auto=1 bands=-10,-6
go
check "a malformed settings file switches the hook off, with the reason" "$LOOKS $(roi) $(logged 'REJECTED')" "0 none 1"
boot on auto=1 settle_secs=60 surprise=1
go
check "so does an unknown key" "$LOOKS $(logged 'REJECTED')" "0 1"

echo "recording stops"
boot on auto=1
ANSWERS="200:1400 200:1400"
go
echo "[t] state -> home: recording DISABLED" >>"$FIELDROI_NETSTATE_LOG"
go
check "the bands are turned off when recording stops" "$(roi)" "off"
echo "[t] state -> away: recording ENABLED (TIMING all-on)" >>"$FIELDROI_NETSTATE_LOG"
ANSWERS="500:1700 505:1702"
LOOKS=0
go
check "the next recording is detected afresh" "$LOOKS $(roi)" "2 on far=505"
boot on auto=1
STOP_AFTER=20
ANSWERS="200:1400 200:1400"
go
check "recording that stops during the settle wait: no look is taken" "$LOOKS $(roi)" "0 off"

echo "not recording, and the manual trigger"
boot off auto=1
ANSWERS="200:1400 200:1400"
go
check "at home (not recording) nothing is looked at" "$LOOKS $(roi)" "0 none"
: >"$TRIGGER"
go
check "fieldroi.now runs a detection at once, with no settle wait" "$LOOKS $SLEPT $(roi)" "2 0 on far=200"
check "  and the trigger file is consumed" "$([ -f "$TRIGGER" ] && echo there || echo gone)" "gone"
boot off auto=0
: >"$TRIGGER"
go
check "the trigger does nothing while auto=0" "$LOOKS $(roi)" "0 none"

echo "switching it on in the middle of a game"
boot on auto=0
go
printf 'auto=1\n' >"$CONF"
ANSWERS="200:1400 200:1400"
go
check "auto=1 written while recording starts a detection" "$LOOKS $(roi)" "2 on far=200"

echo "the shipped files"
boot on
cp "$HERE/../runtime/roiqp/fieldroi.conf.example" "$CONF"
ANSWERS="200:1400 200:1400"
go
check "fieldroi.conf.example is accepted as written and turns the hook on" "$(roi)" "on far=200"

echo
if [ $FAILS -eq 0 ]; then
    echo "fieldroi hook: passed ($CHECKS checks)"
    exit 0
fi
echo "fieldroi hook: $FAILS of $CHECKS checks FAILED"
exit 1
