# Helix AAC decoder (external, not committed)

`recover_mp4`'s best-effort **audio** recovery splits the camera's raw interleaved AAC-LC
frames using the **Helix AAC decoder** (`AACSetRawBlockParams` + `AACDecode`, where each
call's bytes-consumed == one frame's size). Helix is third-party source under the
**RealNetworks Public Source License (RPSL)** and is therefore **fetched locally, not
committed** to this repo (see `../.gitignore`).

## What's committed here (ours)
- `compat/Arduino.h`, `compat/pgmspace.h`, `compat/hlxclib/stdlib.h` — flat-memory stubs so
  the Arduino fork of Helix cross-compiles for plain aarch64 Linux (PROGMEM no-ops, pgm_read_*
  → plain dereferences).
- `aac_split_test.c` — standalone harness that validates the frame splitter against a
  reference recording's ground-truth `stsz` (exact per-frame byte sizes).

## What's gitignored (external)
- `ESP8266Audio/` — the cloned Helix AAC source (RPSL).
- `build/` — cross-compiled `libhelixaac.a` + objects.

## How to fetch + build
```bash
# 1) clone the Arduino fork that carries libhelix-aac
git clone --depth 1 https://github.com/earlephilhower/ESP8266Audio.git \
  recover/helix/ESP8266Audio

# 2) cross-compile the lib for the camera (aarch64). -DARDUINO selects Helix's
#    pure-C fixed-point math branch (no inline asm); the compat/ stubs satisfy the
#    Arduino-only includes.
SRC=recover/helix/ESP8266Audio/src/libhelix-aac
mkdir -p recover/helix/build
for c in "$SRC"/*.c; do
  aarch64-linux-gnu-gcc -O2 -DNDEBUG -DARDUINO -ffunction-sections -fdata-sections \
    -Irecover/helix/compat -I"$SRC" -c "$c" -o "recover/helix/build/$(basename "$c" .c).o"
done
aarch64-linux-gnu-ar rcs recover/helix/build/libhelixaac.a recover/helix/build/*.o
```

`builds/build_soccercam_comprehensive.sh` does this automatically when `ESP8266Audio/` is
present, linking it into `recover_mp4`. If it's absent the builder falls back to a
**video-only** recovery (`-DNO_AUDIO`) — recovered power-cut tails still play, just silent.

## Validation gate (must pass before trusting audio recovery)
`aac_split_test` over a known-good recording's audio elementary stream must reproduce its
moov `stsz` byte-for-byte (the camera's stream is 16 kHz mono AAC-LC, ~218–315 B/frame).
Confirmed: 539/539 frames, all sizes identical, all 134216 bytes consumed.
