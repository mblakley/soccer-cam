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
| FPS dropdown cap (web UI) | 20 | 25 (encoder still ceilings ~20) | `builds/build_fps_cap.sh` |
| Shutter / exposure mode | AE only | full Auto / LowNoise / Anti-Smearing / Manual | runtime API only — `runtime/set_exposure.sh` |

The `build_*.sh` scripts each carry the patches above them in this list,
so you can pick the highest-tier build for your needs:

- **`build_http_unlock.sh`** = HTTP unlock only.
- **`build_bitrate_cap.sh`** = HTTP unlock + bitrate cap. **Recommended for most users.**
- **`build_netstate.sh`** = HTTP unlock + bitrate cap + auto-toggle daemon. **Recommended for the soccer-cam use case.**
- **`build_fps_cap.sh`** = HTTP unlock + bitrate cap + fps dropdown lift. Available for experimentation; daily-driver use is **not** recommended (the h.265 ASIC drops ~20% of frames at 16MP@25fps so recorded output stays around 20 fps anyway, with added jitter).

See `docs/FIRMWARE_PATCH_NOTES.md` for the complete reverse-engineering
story, byte-level patch recipes, and the encoder-bottleneck investigation.

## Directory layout

```
reolink-firmware-patching/
├── README.md                        # this file
├── camera.env.example               # copy to camera.env and fill in
├── _camera_env.sh                   # sourced by runtime/verify scripts
├── .gitignore                       # excludes *.pak, secrets, artifacts
├── docs/
│   └── FIRMWARE_PATCH_NOTES.md      # complete reference
├── pak/                             # .pak container toolchain (Python)
│   ├── pak.py                       # parse / inspect
│   ├── extract.py                   # split into sections
│   ├── pak_repack.py                # rebuild with modified section(s)
│   └── reolink_crc.py               # the non-standard CRC algorithm
├── builds/                          # patch builders (produce flashable .pak files)
│   ├── build_http_unlock.sh
│   ├── build_bitrate_cap.sh
│   ├── build_fps_cap.sh
│   └── build_netstate.sh
├── runtime/                         # settings via live API (no flash needed)
│   ├── set_exposure.sh              # exposure mode / shutter / gain
│   ├── check_isp.sh                 # verify current ISP state
│   ├── probe_isp.sh                 # dump state + allowed ranges
│   └── netstate/
│       └── S99_NetState.template    # daemon installed by build_netstate.sh
└── verify/                          # empirical post-flash checks
    ├── dump_encoder_config.sh       # GetEnc action=0 + action=1
    ├── test_setenc.sh               # probe SetEnc at a target bitrate
    ├── get_performance.sh           # poll GetPerformance (live rate)
    └── fetch_and_analyze.sh         # download latest recording + ffprobe
```

## Prerequisites (one-time, on the build host)

You need a Linux environment with `squashfs-tools` installed. WSL Ubuntu
on Windows works fine. From WSL:

```bash
sudo apt update && sudo apt install -y squashfs-tools python3
```

You also need:
- A stock Reolink Duo 3 PoE `.pak` file (see below).
- Your camera's IP, admin username, admin password.
- For the netstate build: your home router's gateway MAC address.

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
  `/mnt/sda/netstate/home_macs.txt` (one MAC per line). Both files can
  be created/edited via the camera's SD-card management web UI.

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
the `app` squashfs section. The `build_netstate.sh` build additionally
modifies the `rootfs` squashfs section to install
`/etc/init.d/S99_NetState`. **No build touches** `loader`, `fdt`, `atf`,
`uboot`, `kernel`, or `ai` — the stock boot chain stays intact, which
keeps any secure-boot question moot.

Harder failure modes (one that survives reboot but breaks the web UI)
would require UART recovery; the Duo 3 PoE UART pinout is not publicly
documented and we have not characterized it.
