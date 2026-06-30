# Reolink Duo 3 PoE — firmware patching tooling

Tools, build scripts, and documentation for modifying the Reolink Duo 3
PoE's stock firmware to unblock higher throughput, capture better images,
and add a network-state-aware recording toggle for the soccer-cam pipeline.

**We don't commit Reolink firmware binaries here.** Download stock `.pak`
files from Reolink's portal yourself (see "Acquiring stock firmware" below).

## What the patches enable

| Feature | Stock | Patched | Method |
|---|---|---|---|
| HTTP download speed | broken / 1 Mbps throttle | **~86 Mbps** | `builds/build_http_unlock.sh` |
| Max main bitrate | 12288 kbps | **20480 kbps** | `builds/build_bitrate_cap.sh` |
| Auto-toggle recording by network | manual UI toggling | **off at home, on at field** | `builds/build_netstate.sh` |
| Mid-game truncation on a full card | last 8K segment lost when the card fills | **free-space reserve 500 MiB → 20 GiB** so the recycler always keeps a full segment's headroom | `builds/build_soccercam_v2.sh` |
| Home power-on stub recordings | a junk clip every home boot | **auto-deleted** (netstate v2 stub cleanup) | `builds/build_soccercam_v2.sh` |
| Power-cut recording recovery | orphaned (moov-less) clip discarded | **rebuilt in place** — video + best-effort audio — at next boot | `builds/build_soccercam_comprehensive.sh` |
| FPS dropdown cap (web UI) | 20 | 25 (encoder still ceilings ~20) | `builds/build_fps_cap.sh` |
| Shutter / exposure mode | AE only | full Auto / LowNoise / Anti-Smearing / Manual | runtime API only — `runtime/set_exposure.sh` |

The `build_*.sh` scripts each carry the patches above them in this list,
so you can pick the highest-tier build for your needs:

- **`build_http_unlock.sh`** = HTTP unlock only.
- **`build_bitrate_cap.sh`** = HTTP unlock + bitrate cap. **Recommended for most users.**
- **`build_netstate.sh`** = HTTP unlock + bitrate cap + auto-toggle daemon.
- **`build_soccercam_v2.sh`** = the above **+ netstate v2 (stub cleanup) + free-space reserve**. Fixes the full-card mid-game truncation (the raised reserve makes the camera's own recycler keep a full segment's headroom free).
- **`build_soccercam_comprehensive.sh`** = `v2` **+ boot-time power-cut recovery** (`recover_mp4` + `S35_RecRecover`, video + best-effort AAC audio). **Recommended for the soccer-cam use case.** Audio recovery needs the Helix AAC source fetched locally (see `recover/helix/README.md`); without it the build falls back to video-only recovery automatically.
- **`build_fps_cap.sh`** = HTTP unlock + bitrate cap + fps dropdown lift. Available for experimentation; daily-driver use is **not** recommended (the h.265 ASIC drops ~20% of frames at 16MP@25fps so recorded output stays around 20 fps anyway, with added jitter).

The recovery binary has its own repeatable correctness gate:
`bash verify/test_recover_mp4.sh <a_good_recording.mp4>` builds `recover_mp4`,
strips/​truncates the recording to simulate both failure modes, recovers each, and
asserts the rebuilt video+audio decode cleanly and (for the intact case) match the
original byte-for-byte.

**Step-by-step patching procedure**: see `docs/PATCHING_GUIDE.md` —
walks a fresh user from a stock-firmware camera through to a flashed,
verified, daily-driver setup. Includes the exact firmware version
(SHA-256 verified) these patches were developed and tested against.

**Full reverse-engineering story**: see `docs/FIRMWARE_PATCH_NOTES.md` —
byte-level patch recipes, encoder-bottleneck investigation, sensor
characterization, and reasoning behind the daily-driver build choices.

## Directory layout

```
reolink-firmware-patching/
├── README.md                        # this file
├── camera.env.example               # copy to camera.env and fill in
├── _camera_env.sh                   # sourced by runtime/verify scripts
├── .gitignore                       # excludes *.pak, secrets, artifacts
├── docs/
│   ├── FIRMWARE_PATCH_NOTES.md      # complete reference (RE recipes + byte offsets)
│   └── PATCHING_GUIDE.md            # step-by-step stock -> flashed -> verified
├── pak/                             # .pak container toolchain (Python)
│   ├── pak.py                       # parse / inspect
│   ├── extract.py                   # split into sections
│   ├── pak_repack.py                # rebuild with modified section(s)
│   └── reolink_crc.py               # the non-standard CRC algorithm
├── builds/                          # patch builders (produce flashable .pak files)
│   ├── build_http_unlock.sh
│   ├── build_bitrate_cap.sh
│   ├── build_fps_cap.sh
│   ├── build_netstate.sh
│   ├── build_soccercam_v2.sh        # + free-space reserve + netstate v2 (stub cleanup)
│   ├── build_soccercam_comprehensive.sh  # + power-cut recovery (video+audio)
│   └── BUILD_LOG.md                 # artifact tracker (sha256 / CRC / contents)
├── recover/                         # power-cut recovery (compiled into the comprehensive build)
│   ├── recover_mp4.c                # static aarch64 reindexer (rebuilds a moov-less mdat)
│   └── helix/                       # AAC audio recovery (Helix decoder, fetched locally)
│       ├── README.md                # how to fetch + build Helix (RPSL, gitignored)
│       ├── compat/                  # flat-memory stubs so Helix cross-compiles
│       └── aac_split_test.c         # validates the frame splitter vs ground-truth stsz
├── runtime/                         # files installed into rootfs / live-API settings
│   ├── set_exposure.sh              # exposure mode / shutter / gain (no flash)
│   ├── check_isp.sh                 # verify current ISP state
│   ├── probe_isp.sh                 # dump state + allowed ranges
│   ├── netstate/
│   │   ├── S99_NetState.template    # v1 daemon (build_netstate.sh)
│   │   └── S99_NetState_v2.template # v2: home/away + power-on stub cleanup
│   └── recover/
│       └── S35_RecRecover           # boot-time recovery init script
└── verify/                          # empirical checks
    ├── dump_encoder_config.sh       # GetEnc action=0 + action=1
    ├── test_setenc.sh               # probe SetEnc at a target bitrate
    ├── get_performance.sh           # poll GetPerformance (live rate)
    ├── fetch_and_analyze.sh         # download latest recording + ffprobe
    └── test_recover_mp4.sh          # build + round-trip-validate the recovery binary
```

## Prerequisites (one-time, on the build host)

You need a Linux environment with `squashfs-tools` installed. WSL Ubuntu
on Windows works fine. From WSL:

```bash
sudo apt update && sudo apt install -y squashfs-tools python3
```

The **comprehensive build** (power-cut recovery) additionally cross-compiles a
small aarch64 binary, so it also needs:

```bash
sudo apt install -y gcc-aarch64-linux-gnu        # cross-compiler for recover_mp4
sudo apt install -y qemu-user-static ffmpeg       # only to RUN verify/test_recover_mp4.sh
```

and the Helix AAC source for **audio** recovery — fetch it once per
`recover/helix/README.md` (the build silently falls back to video-only recovery
if it's absent). Helix is RPSL-licensed third-party code and is **not committed**.

You also need:
- A stock Reolink Duo 3 PoE `.pak` file (see below).
- Your camera's IP, admin username, admin password.
- For the netstate / soccer-cam builds: your home router's gateway MAC address.

## Acquiring stock firmware

Reolink hosts firmware in their download center. Easier mirror: the
[AT0myks/reolink-fw-archive](https://github.com/AT0myks/reolink-fw-archive)
repo lists every version with download URLs. Look up the entry for your
exact `hw_ver` (printed on the camera label and shown by `GetDevInfo`)
and download the matching `.pak`.

For the IPC_NT15NA416MP variant of the Duo 3 PoE, the stock version
this tooling was developed against is `v3.0.0.4867_2505072124`.

## Quickstart — basic patch (any user)

```bash
# 1. Set up credentials (one-time)
cd reolink-firmware-patching
cp camera.env.example camera.env
${EDITOR:-nano} camera.env   # set CAMERA_IP, CAMERA_USER, CAMERA_PASS

# 2. Build the patched firmware (HTTP unlock + 20 Mbps bitrate)
sudo bash builds/build_bitrate_cap.sh \
    /path/to/stock.pak \
    /path/to/patched_out.pak \
    20480

# 3. Flash via the camera's web UI:
#    Settings -> Maintenance -> Local Upgrade -> select patched_out.pak
#    Wait for reboot (1-3 min).

# 4. Verify the API now offers 20480 kbps
bash verify/dump_encoder_config.sh | grep -A 3 bitRate

# 5. Verify actual recorded bitrate after a 60s motion clip
bash verify/fetch_and_analyze.sh daily_driver_test
```

## Quickstart — soccer-cam build (with auto-toggle netstate daemon)

This is the recommended build if you take the camera between home and
field locations. The daemon idles the camera at home and starts continuous
recording the moment it detects any other network (or no network at all,
which is what happens when you plug a portable router or just power it
up at the field).

You need the **MAC address of your home network's default gateway**. From
the camera's host LAN:

```bash
# On the same LAN as the camera, find the gateway IP, then its MAC:
ip route | awk '$1=="default" {print "gateway:", $3}'
arp -n   # find the gateway IP's MAC address (or `arp -a` on Windows)
```

Then build:

```bash
sudo bash builds/build_netstate.sh \
    /path/to/stock.pak \
    /path/to/patched_out.pak \
    20480 \
    admin \
    YOUR_CAMERA_PASSWORD \
    aa:bb:cc:dd:ee:ff       # your home gateway MAC (lowercase)
```

You can list multiple home MACs (e.g., your home router AND a known
office router) by appending more arguments after the first MAC.

After flashing:

- The daemon log lives at `/mnt/sda/netstate/log` and is fetchable via
  the unlocked HTTP path: `curl -u admin:PASS http://CAMERA_IP/downloadfile/netstate/log`
- Override the daemon temporarily by creating an empty
  `/mnt/sda/netstate/override` file — daemon yields, you control via UI/API.
- Update the home-MAC list without re-flashing by writing to
  `/mnt/sda/netstate/home_macs.txt` (one MAC per line). There is **no
  arbitrary-file web UI** on these cameras — write these files over `telnet`
  (the patched firmware leaves `telnetd` running, started in `S25_Net`) or by
  pulling the microSD card and editing it on a PC. For a hands-off setup, prefer
  baking the home MAC in at build time (the builders already do this) rather
  than relying on a runtime file.

## Quickstart — comprehensive soccer-cam build (recommended)

Everything in the netstate build, **plus** the full-card truncation fix and
boot-time recovery of power-cut recordings (video + best-effort audio).

```bash
# one-time, for the cross-compiled recovery binary:
sudo apt install -y gcc-aarch64-linux-gnu
# one-time, for AAC audio recovery (optional — video-only without it):
git clone --depth 1 https://github.com/earlephilhower/ESP8266Audio.git \
    recover/helix/ESP8266Audio

# build (args: stock out kbps user pass reserve_gb home_mac [more_macs...])
bash builds/build_soccercam_comprehensive.sh \
    /path/to/stock.pak \
    /path/to/soccercam_comprehensive.pak \
    20480 admin YOUR_CAMERA_PASSWORD 20 \
    aa:bb:cc:dd:ee:ff
```

The build prints `match: True` after recomputing the Reolink CRC. Before
flashing, you can prove the recovery binary is correct against any good
recording from the camera:

```bash
bash verify/test_recover_mp4.sh /path/to/a_good_recording.mp4
# => byte-exact video+audio tables, bit-identical audio PCM, both
#    failure modes decode clean -> "PASS"
```

After flashing, the recovery runs automatically at boot before the camera's
own scan; its log is fetchable over the unlocked HTTP path:
`curl -u admin:PASS http://CAMERA_IP/downloadfile/recover/log`.

Want the card-full truncation fix **without** the power-cut recovery binary
(no cross-compiler / Helix needed)? Use `build_soccercam_v2.sh` — same
arguments, minus the recovery step:

```bash
bash builds/build_soccercam_v2.sh \
    /path/to/stock.pak \
    /path/to/soccercam_v2.pak \
    20480 admin YOUR_CAMERA_PASSWORD 20 \
    aa:bb:cc:dd:ee:ff
```

## Quickstart — motion-blur reduction (no flash, any firmware)

Soccer / fast-motion footage benefits from `Anti-Smearing` exposure mode:

```bash
bash runtime/set_exposure.sh antismear
```

Settings persist across reboots. Revert with `bash runtime/set_exposure.sh auto`.
Detail and A/B proof in `docs/FIRMWARE_PATCH_NOTES.md` section 13.

## Safety / recovery

Keep the stock `.pak` around as your recovery artifact. A bad flash is
almost always recoverable via the same web-UI Local Upgrade path —
re-flash stock and you're back where you started.

The `build_http_unlock.sh` and `build_bitrate_cap.sh` builds modify only
the `app` squashfs section. The `build_netstate.sh`, `build_soccercam_v2.sh`,
and `build_soccercam_comprehensive.sh` builds additionally modify the `rootfs`
squashfs section (init scripts + `recover_mp4`) and patch a one-instruction
free-space constant in `libStorageFileManager.so` (inside `app`). **No build
touches** `loader`, `fdt`, `atf`, `uboot`, `kernel`, or `ai` — the stock boot
chain stays intact, which keeps any secure-boot question moot. The recovery
init script (`S35_RecRecover`) **never deletes** a recoverable orphan; it only
rebuilds the moov in place, so a recovery bug can't lose footage.

Harder failure modes (one that survives reboot but breaks the web UI)
would require UART recovery; the Duo 3 PoE UART pinout is not publicly
documented and we have not characterized it.
