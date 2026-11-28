#!/bin/sh
# Offline test of runtime/roiqp/S37_RoiQp: config parsing, what it sends to the
# kernel, and that it fails closed.
#
#   sh verify/test_roiqp_daemon.sh            (also: bash, dash, busybox sh)
#
# The daemon is sourced with ROIQP_SOURCE_ONLY=1, which gives its functions
# without starting its loop. Its two touch points with the kernel, send_cmd and
# last_echo, are replaced here by a model of the setroi handler in three forms:
#
#   stock     QP and QPMode are parsed as 0; one window; every write clears all
#   fix       QP parsed correctly; one window (slot 0 whatever the index says)
#   multiwin  ten independent windows; an index above 9 is refused
#
# These are the behaviours measured on builds 4921, 4922 and 4923. The model is
# not the kernel: verify/test_setroi_patch.sh executes the real handler.

set -u
HERE=$(cd "$(dirname "$0")" && pwd)
DAEMON="$HERE/../runtime/roiqp/S37_RoiQp"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

ROIQP_SOURCE_ONLY=1
ROIQP_CONF_DIR="$WORK/card"
ROIQP_DEFAULT="$WORK/default.conf"
ROIQP_CMD="$WORK/cmd"
ROIQP_BUILD_INFO="$WORK/build"
mkdir -p "$ROIQP_CONF_DIR"
# shellcheck disable=SC1090
. "$DAEMON"

# --- the model ---------------------------------------------------------------
MODULE=multiwin
SENT=0
ECHO_LINE=""
reset_slots() { for S in 0 1 2 3 4 5 6 7 8 9; do eval "SLOT_$S=off"; done; }
reset_slots

send_cmd() {
    # shellcheck disable=SC2086
    set -- $1
    # $1=vdoenc $2=setroi $3=path $4=index $5=enable $6=qp $7=mode $8..$11=x y w h
    SENT=$((SENT + 1))
    Q=$6; M=$7
    [ "$MODULE" = stock ] && { Q=0; M=0; }
    ECHO_LINE="[VDOENC][$3] Set ROI Index = $4, Enable = $5, QP = $Q, QPMode = $M, Coord_X = $8, Coord_Y = $9, Width = ${10}, Height = ${11}"
    if [ "$MODULE" = multiwin ]; then
        case "$4" in [0-9]) ;; *) return 0 ;; esac
        if [ "$5" -eq 1 ]; then eval "SLOT_$4='$Q $M $8 $9 ${10} ${11}'"; else eval "SLOT_$4=off"; fi
    else
        reset_slots
        [ "$5" -eq 1 ] && SLOT_0="$Q $M $8 $9 ${10} ${11}"
    fi
    return 0
}
last_echo() { echo "$ECHO_LINE"; }

slots() {
    OUT=""
    for S in 0 1 2 3 4 5 6 7 8 9; do
        eval "V=\$SLOT_$S"
        [ "$V" = off ] || OUT="$OUT[$S: $V]"
    done
    echo "${OUT:-none}"
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

start() {  # start <module> <build line>  -- a fresh boot
    MODULE=$1
    printf 'variant=test\nroiqp=%s\n' "$2" >"$ROIQP_BUILD_INFO"
    reset_slots
    SENT=0
    APPLIED=0
    LAST_SIG=""
    SINCE_APPLY=$REAPPLY_INTERVAL
    rm -f "$CONF" "$LOG"
}
conf() { printf '%s\n' "$@" >"$CONF"; }
step() { SENT=0; tick >/dev/null; }

HEAD="enable=1 path=0 mode=1 frame_w=7680 frame_h=2160"
printf 'enable=0\npath=0\nx=0\ny=0\nw=7680\nh=2160\nqp=0\nmode=1\n' >"$ROIQP_DEFAULT"

echo "ten-window build"
start multiwin "S37+kflow_videoenc_setroi_fix+multiwin"
step
check "disabled default: nothing is sent to the kernel" "$SENT $(slots)" "0 none"
check "  and the log says the encoder was left alone" "$(logged 'encoder untouched')" 1

conf enable=1 path=0 x=0 y=128 w=7680 h=704 qp=-12 mode=1
step
check "one-window config in the original keys lands in window 0" "$(slots)" "[0: -12 1 0 128 7680 704]"

# shellcheck disable=SC2086
conf $HEAD win0=1472,128,4544,704,-12 win1=0,832,7680,512,-8 win5=0,1344,7680,640,-4
step
check "three winN lines land in windows 0, 1 and 5" "$(slots)" \
    "[0: -12 1 1472 128 4544 704][1: -8 1 0 832 7680 512][5: -4 1 0 1344 7680 640]"

# shellcheck disable=SC2086
conf $HEAD win1=0,832,7680,512,-8
step
check "a new config that drops windows 0 and 5 clears them" "$(slots)" "[1: -8 1 0 832 7680 512]"

reset_slots   # what `device` does when it rebuilds the encode path
SINCE_APPLY=$REAPPLY_INTERVAL
step
check "the periodic re-apply restores a window the encoder lost" "$(slots)" "[1: -8 1 0 832 7680 512]"
check "  with one write, not a clear of all ten" "$SENT" 1

SINCE_APPLY=0
step
check "an unchanged config between re-applies sends nothing" "$SENT" 0

printf 'enable=1\r\npath=0\r\nmode=1\r\nwin0=0,128,7680,704,-10\r\nwin2=0,832,7680,512,-6\r\n' >"$CONF"
step
check "a config saved on Windows (CRLF) is accepted" "$(slots)" "[0: -10 1 0 128 7680 704][2: -6 1 0 832 7680 512]"

conf enable=0 path=0 mode=1 win0=0,128,7680,704,-10
step
check "enable=0 clears every window that was applied" "$(slots) $APPLIED" "none 0"

cp "$HERE/../runtime/roiqp/roi.conf.example" "$CONF"
step
check "the shipped roi.conf.example is accepted as written" "$(slots)" \
    "[0: -10 1 0 704 7680 256][1: -6 1 0 960 7680 320][2: -3 1 0 1280 7680 384]"
cp "$HERE/../runtime/roiqp/roi.conf.default" "$CONF"
step
check "the shipped roi.conf.default turns everything off" "$(slots)" "none"

echo "bad configs never reach the encoder, and undo a good one"
reject() {  # reject <what> <reason fragment> <config lines...>
    WHAT=$1; WHY=$2; shift 2
    # shellcheck disable=SC2086
    conf $HEAD win0=0,128,7680,704,-10
    step
    conf "$@"
    step
    check "$WHAT" "$(slots) $(logged "REJECTED.*$WHY")" "none 1"
    rm -f "$LOG"
}
# shellcheck disable=SC2086
reject "the same window twice" "given twice" $HEAD win3=0,0,64,64,-2 win3=0,64,64,64,-2
# shellcheck disable=SC2086
reject "winN lines mixed with x/y/w/h/qp keys" "same file" $HEAD win0=0,0,64,64,-2 qp=-4
# shellcheck disable=SC2086
reject "a window past the bottom of the frame" "exceeds frame_h" $HEAD win0=0,2000,7680,200,-4
# shellcheck disable=SC2086
reject "a delta below -32" "qp out of range" $HEAD win0=0,0,64,64,-40
# shellcheck disable=SC2086
reject "index 10" "malformed line" $HEAD win10=0,0,64,64,-2
# shellcheck disable=SC2086
reject "a window with a missing field" "malformed line" $HEAD win0=0,0,64,64
# shellcheck disable=SC2086
reject "no window at all" "missing key: x" $HEAD
reject "no mode" "missing key: mode" enable=1 path=0 win0=0,0,64,64,-2
# shellcheck disable=SC2086
reject "an unknown key" "unknown key" $HEAD win0=0,0,64,64,-2 strength=3

echo "single-window build (4922)"
start fix "S37+kflow_videoenc_setroi_fix"
# shellcheck disable=SC2086
conf $HEAD win0=0,128,7680,704,-12 win1=0,832,7680,512,-8
step
check "two windows are refused before anything is sent" "$SENT $(slots) $(logged 'need the ten-window module')" "0 none 1"
# shellcheck disable=SC2086
conf $HEAD win3=0,832,7680,512,-8
step
check "so is one window on an index other than 0" "$SENT $(slots)" "0 none"
# shellcheck disable=SC2086
conf $HEAD win0=0,128,7680,704,-12
step
check "window 0 alone is applied, with a single write" "$SENT $(slots)" "1 [0: -12 1 0 128 7680 704]"
conf enable=1 path=0 x=0 y=128 w=7680 h=704 qp=-6 mode=1
step
check "the original one-window config still works" "$(slots)" "[0: -6 1 0 128 7680 704]"

echo "unpatched module (4921)"
start stock "S37"
conf enable=1 path=0 x=0 y=128 w=7680 h=704 qp=-6 mode=1
step
check "a QP the kernel echoes back as 0 is an error, and the window is removed" \
    "$(slots) $APPLIED $(logged 'ERROR: the kernel did not echo')" "none 0 1"

echo "a write that half-lands is undone"
start multiwin "S37+kflow_videoenc_setroi_fix+multiwin"
REAL_ECHO=1
last_echo() { if [ "$SENT" -ge 9 ] && [ $REAL_ECHO -eq 1 ]; then echo ""; else echo "$ECHO_LINE"; fi; }
# shellcheck disable=SC2086
conf $HEAD win0=0,128,7680,704,-12 win1=0,832,7680,512,-8
SENT=0
tick >/dev/null   # 8 clears, then window 0 (echoed), then window 1 (no echo)
REAL_ECHO=0
check "window 0 landed, window 1 gave no echo: both are cleared" "$(slots) $APPLIED" "none 0"

echo
if [ $FAILS -eq 0 ]; then
    echo "roiqp daemon: passed ($CHECKS checks)"
    exit 0
fi
echo "roiqp daemon: $FAILS of $CHECKS checks FAILED"
exit 1
