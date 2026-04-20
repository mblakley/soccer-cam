# Reolink Duo 3 PoE — firmware patching tooling

This directory contains the tools, build scripts, and documentation
produced while modifying the Reolink Duo 3 PoE's stock firmware to
unblock higher throughput and better image capture for the soccer-cam
pipeline.

**We don't commit Reolink firmware binaries here.** Download stock
`.pak` files from Reolink's portal yourself.

## What the scripts enable

| Feature | Stock | Patched | Method |
|---|---|---|---|
| HTTP download speed | broken / 1 Mbps throttle | **~86 Mbps** | `.pak` flash (`builds/build_http_unlock.sh`) |
| Max main bitrate | 12288 kbps (12 Mbps) | **20480 kbps (20 Mbps)** | `.pak` flash (`builds/build_bitrate_cap.sh`) |
| Max fps at 16MP | 20 | 20 (hardware limit) | — (investigated, not patchable without rootfs mods) |
| Shutter / exposure mode | AE only | full Auto/LowNoise/Anti-Smearing/Manual with shutter+gain control | runtime API (`runtime/set_exposure.sh`) — no flash |

See `docs/FIRMWARE_PATCH_NOTES.md` for the full story including
reverse-engineering notes, byte-level patch recipes, and the A/B proof
that `Anti-Smearing` mode measurably reduces motion blur.

## Directory layout

```
reolink-firmware-patching/
├── README.md                   # this file
├── camera.env.example          # copy to camera.env and fill in
├── _camera_env.sh              # sourced by runtime/verify scripts
├── .gitignore                  # excludes *.pak, secrets, artifacts
├── docs/
│   └── FIRMWARE_PATCH_NOTES.md # complete reference
├── pak/                        # .pak container toolchain (Python)
│   ├── README.md
│   ├── pak.py                  # parse / inspect
│   ├── extract.py              # split into sections
│   ├── pak_repack.py           # rebuild with modified section
│   └── reolink_crc.py          # the non-standard CRC algorithm
├── builds/                     # patch builders (produce flashable .pak files)
│   ├── build_http_unlock.sh    # HTTP /downloadfile/ unlock
│   └── build_bitrate_cap.sh    # HTTP + bitrate cap lift
├── runtime/                    # settings via live API (no flash needed)
│   ├── set_exposure.sh         # exposure mode / shutter / gain
│   ├── check_isp.sh            # verify current ISP state
│   └── probe_isp.sh            # dump state + allowed ranges
└── verify/                     # empirical post-flash checks
    ├── dump_encoder_config.sh  # GetEnc action=0 + action=1
    ├── test_setenc.sh          # probe SetEnc at a target bitrate
    ├── get_performance.sh      # poll GetPerformance (live rate)
    └── fetch_and_analyze.sh    # download latest recording + ffprobe
```

## Quickstart — daily-driver patch

Assuming WSL Ubuntu with `squashfs-tools` installed and a stock
`.pak` on hand:

```bash
# 1. Set up credentials (one-time)
cp camera.env.example camera.env
${EDITOR:-nano} camera.env

# 2. Build the patched firmware (HTTP unlock + 20 Mbps bitrate)
sudo bash builds/build_bitrate_cap.sh \
    /path/to/stock.pak \
    /path/to/patched_out.pak \
    20480

# 3. Flash via the camera's web UI:
#    Settings -> Maintenance -> Local Upgrade -> select patched_out.pak
#    Wait for reboot (1-3 min).

# 4. Verify the API now shows 20480 kbps in the dropdown
bash verify/dump_encoder_config.sh | grep -A 3 'bitRate'

# 5. Verify actual recorded bitrate is above the old 12 Mbps cap
#    (record ~60s of motion first, e.g., via motion-triggered or manual)
bash verify/fetch_and_analyze.sh daily_driver_test
```

## Quickstart — motion-blur reduction (no flash)

For reducing motion blur on fast action (soccer, sports), switch the
camera into `Anti-Smearing` exposure mode. This is stock firmware
functionality, no patching required:

```bash
bash runtime/set_exposure.sh antismear
```

Settings persist across reboots. Revert with `bash runtime/set_exposure.sh auto`.

Verified to produce sharper action frames: edge sharpness (Laplacian
variance) nearly doubled on detail-rich frames, with visible motion-blur
ghost artifacts present in Auto mode absent in Anti-Smearing. Details
in `docs/FIRMWARE_PATCH_NOTES.md` section 13.

## Safety / recovery

Keep the stock `.pak` around as the recovery artifact. A bad flash can
usually be recovered via the same web-UI upload path. Harder failure
modes require UART; the Duo 3 PoE UART pinout is not publicly
documented and we have not characterized it.

We do NOT touch `loader`, `fdt`, `atf`, `uboot`, `kernel`, `rootfs`,
or `ai` partitions — only `app`. Stock boot chain is preserved, which
keeps the secure-boot eFuse question moot.

## What's NOT here

- **Research scripts** (Ghidra Jython, probe/scan shell scripts,
  investigation one-offs). Those were throwaway tooling to find the
  patch offsets; the durable output is the byte recipes in
  `docs/FIRMWARE_PATCH_NOTES.md`.
- **Stock or patched `.pak` files.** Reolink firmware is copyrighted;
  download it from Reolink's portal.
- **fps cap lift patches.** The fps API-level cap CAN be patched to show
  25/30 in the dropdown, but the actual encoder silently clamps to 20
  fps on this camera's 16MP mode regardless. See docs section 11 for the
  sensor investigation (OmniVision OS08C10 dual-sensor stitched) and
  why 20 fps is the real hardware ceiling here.
