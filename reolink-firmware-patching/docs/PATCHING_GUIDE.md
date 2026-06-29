# Step-by-step: patching a Reolink Duo 3 PoE

This guide takes you from a brand-new (or stock) Reolink Duo 3 PoE to a
flashed camera running the soccer-cam patches. It is intentionally
verbose; each step is a single concrete action with a verification cue.

## What this gets you

After completing the steps below, your camera will:

- Serve recordings over HTTP at LAN wire-speed (~86 Mbps) instead of the
  stock ~1 Mbps throttle.
- Allow main-stream bitrate up to 20 Mbps (stock cap is 12 Mbps).
- Auto-toggle recording: **idle on your home network, record continuously
  on any other network** (or with no network at all). Useful for taking
  the camera between home and a portable router at a soccer field.

The **comprehensive** soccer-cam build (Step 4, option C) additionally:

- **Never truncates the last segment when the card fills.** Stock keeps only
  ~500 MiB free, which is smaller than one 8K main-stream segment (~780 MB),
  so the final write fails and the segment is lost mid-game. The reserve is
  lifted to 20 GiB, so the camera's own recycler always keeps a full segment's
  worth of headroom free and the write never fails.
- **Deletes the junk "stub" clip** the camera records during the boot window
  when it powers on at home.
- **Recovers power-cut recordings at boot.** A clip cut by power loss has no
  moov index, so stock discards it. The recovery rebuilds the index in place
  — full video plus best-effort audio — so it becomes a normal, downloadable
  recording. It **never deletes** a clip it can't fully fix.

## Verified-against firmware

This tooling was developed and end-to-end verified against this exact
stock firmware (released by Reolink, May 2025):

| field | value |
|---|---|
| Model | Reolink Duo 3 PoE |
| Item number | P750 |
| Hardware version | `IPC_NT15NA416MP` |
| Firmware version | `v3.0.0.4867_2505072124` |
| Build day | `build 2505072124` |
| Stock pak file | `IPC_NT15NA416MP.4867_2505072124.Reolink-Duo-3-PoE.16MP.REOLINK.pak` |
| Stock pak sha256 | `1668c9df546b238de2a47c422d73555761cf4812af50fbb67dc2b6f5402bc235` |

The patches make byte-level edits at fixed offsets, and each builder
script `assert`s the stock-byte fingerprint before patching. **If you
have a different firmware version**, the patches will refuse to apply —
the assertion will tell you exactly which offset failed. You can either:

1. Downgrade to `v3.0.0.4867_2505072124` (known good) and patch that.
2. Re-derive the byte offsets for your version (see
   `FIRMWARE_PATCH_NOTES.md` for the reverse-engineering recipes).

If you're on a *newer* Reolink build and the stock interface still
behaves the same, the recipes in the notes should still locate the
right instructions; only the offsets shift.

## Prerequisites

### On the build machine

You need a Linux environment with a few tools. WSL Ubuntu on Windows
is what this was developed against; native Linux works the same.

```bash
sudo apt update
sudo apt install -y squashfs-tools python3 git
```

For the **comprehensive build** (Step 4, option C) you also need the aarch64
cross-compiler, and — to run the recovery self-test — qemu + ffmpeg:

```bash
sudo apt install -y gcc-aarch64-linux-gnu     # cross-compiles recover_mp4
sudo apt install -y qemu-user-static ffmpeg    # only for verify/test_recover_mp4.sh
```

Audio recovery uses the Helix AAC decoder, which is third-party RPSL source we
don't commit. Fetch it once (the build falls back to video-only if it's absent):

```bash
git clone --depth 1 https://github.com/earlephilhower/ESP8266Audio.git \
    reolink-firmware-patching/recover/helix/ESP8266Audio
```

### On the camera side

- An ethernet cable from your camera to your home network (PoE switch or
  PoE injector required).
- Camera reachable from the build machine on its IP.
- The admin password.

### Stock firmware

Download the stock `IPC_NT15NA416MP.4867_2505072124.Reolink-Duo-3-PoE.16MP.REOLINK.pak`
from one of:

- **Reolink download center**: <https://reolink.com/download-center/>
  (search by your hardware version)
- **AT0myks/reolink-fw-archive** (third-party mirror with full version
  history): <https://github.com/AT0myks/reolink-fw-archive>

Verify the sha256 matches:
```bash
sha256sum your_downloaded.pak
# expect: 1668c9df546b238de2a47c422d73555761cf4812af50fbb67dc2b6f5402bc235
```

If the sha256 does not match, you have a different version — see the
warning above.

## Step 1 — Note your home gateway MAC

This is the MAC address of the device that the camera sees as its
default gateway when it's on your home LAN. The netstate daemon uses
this to decide "I'm home, idle" vs "I'm somewhere else, record."

From any machine on the same LAN as the camera (Linux/macOS/WSL):
```bash
ip route | awk '$1=="default" {print "gateway IP:", $3}'
arp -n | grep <gateway-IP>     # the second column is the MAC
```

From Windows command prompt:
```cmd
ipconfig | findstr /i "default gateway"
arp -a | findstr <gateway-IP>
```

Write down the MAC in lower-case colon-separated form, e.g.,
`aa:bb:cc:dd:ee:ff`. You can include multiple "home" MACs (e.g., your
home router AND a known office router) — the daemon takes a space-
separated list.

## Step 2 — Clone this repo

```bash
git clone https://github.com/<your-fork>/soccer-cam.git
cd soccer-cam/reolink-firmware-patching
```

## Step 3 — Set up your camera credentials (one-time, never committed)

```bash
cp camera.env.example camera.env
${EDITOR:-nano} camera.env
```

Set `CAMERA_IP`, `CAMERA_USER`, `CAMERA_PASS` to your camera's values.
This file is in `.gitignore` and will not be committed.

## Step 4 — Build the patched .pak

Pick the tier you want. Each carries the patches of the tiers above it.

### Option A — HTTP unlock + bitrate cap (simplest)

```bash
sudo bash builds/build_bitrate_cap.sh \
    /path/to/stock.pak \
    /path/to/output_patched.pak \
    20480
```

### Option B — + auto-toggle recording by network

```bash
sudo bash builds/build_netstate.sh \
    /path/to/stock.pak \
    /path/to/output_patched.pak \
    20480 \
    admin \
    YOUR_CAMERA_PASSWORD \
    aa:bb:cc:dd:ee:ff           # your home gateway MAC
```
Argument order:
```
build_netstate.sh <stock.pak> <output.pak> <kbps> <admin-user> <admin-password> <home-mac> [more-home-macs...]
```

### Option C — comprehensive soccer-cam build (recommended)

Everything in Option B **plus** the full-card truncation fix, home power-on
stub cleanup, and boot-time power-cut recovery (video + best-effort audio).
Requires the cross-compiler (and, for audio, the Helix source) from the
Prerequisites section.

```bash
bash builds/build_soccercam_comprehensive.sh \
    /path/to/stock.pak \
    /path/to/soccercam_comprehensive.pak \
    20480 \
    admin \
    YOUR_CAMERA_PASSWORD \
    20 \                        # free-space reserve, GiB (must fit a movz immediate: 16/20/32)
    aa:bb:cc:dd:ee:ff           # your home gateway MAC
```
Argument order:
```
build_soccercam_comprehensive.sh <stock.pak> <output.pak> <kbps> <admin-user> <admin-password> <reserve-gb> <home-mac> [more-home-macs...]
```
(`build_soccercam_v2.sh` takes the **same arguments** but skips the power-cut
recovery binary — use it if you want the card-full truncation fix without the
cross-compiler / Helix dependency.)

### What the builder does (Options B/C)

1. Extract the `app` and `rootfs` squashfs sections from the stock .pak.
2. Patch the HTTP `/downloadfile/` location config in the `device` binary
   (in `app`) to remove the speed throttle.
3. Patch the bitrate cap immediate in the `router` binary (in `app`).
4. **(v2/comprehensive)** Patch the free-space reserve constant in
   `libStorageFileManager.so` (500 MiB → your `<reserve-gb>`) and install
   `S99_NetState` **v2** (with stub cleanup). The raised reserve is the whole
   truncation fix — the camera's own recycler then keeps that much free.
5. **(comprehensive)** Cross-compile `recover_mp4` (static aarch64, Helix AAC
   linked if present) into `/usr/bin`, install `S35_RecRecover` boot script.
6. Otherwise install the v1 `S99_NetState` daemon (Option B).
7. Repack both squashfs sections and the .pak, recomputing the Reolink CRC.

You should see **"match: True"** on the CRC verification at the end. If you
don't, stop — something modified the stock .pak header layout and the camera
will reject the flash.

### (Comprehensive) Prove the recovery binary before flashing

```bash
bash verify/test_recover_mp4.sh /path/to/a_good_recording.mp4
```
This builds `recover_mp4`, simulates a clean orphan and a power-cut orphan from
the recording, recovers each, and asserts the rebuilt video+audio decode cleanly
— and, for the intact case, match the original byte-for-byte (and the audio PCM
bit-for-bit). It must end in `PASS`.

## Step 5 — Flash the patched .pak

1. Open the camera's web UI in your browser: `http://<CAMERA_IP>`.
2. Log in as admin.
3. Navigate to: **Settings → Maintenance → Local Upgrade**.
4. Click "Browse" / "Choose file", select your `output_patched.pak`.
5. Click "Upgrade" or "Apply".
6. Wait for the camera to reboot. This takes 1-3 minutes; the web UI
   will likely show a progress bar and then go offline briefly.
7. The camera comes back at the same IP.

**Do NOT check "Reset Configuration"** — you want to keep your existing
network/encoder/etc. settings.

## Step 6 — Verify the flash worked

Replace `<CAMERA_IP>` and `<PASSWORD>` with your values.

```bash
# 1. Get a session token
TOKEN=$(curl -s -X POST -H "Content-Type: application/json" \
  -d "[{\"cmd\":\"Login\",\"action\":0,\"param\":{\"User\":{\"userName\":\"admin\",\"password\":\"<PASSWORD>\"}}}]" \
  "http://<CAMERA_IP>/api.cgi?cmd=Login&token=null" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["value"]["Token"]["name"])')

# 2. Verify the bitrate dropdown was lifted to 20480
curl -s -X POST -H "Content-Type: application/json" \
  -d '[{"cmd":"GetEnc","action":1,"param":{"channel":0}}]' \
  "http://<CAMERA_IP>/api.cgi?cmd=GetEnc&token=$TOKEN" \
  | python3 -c 'import json,sys; ms=json.load(sys.stdin)[0]["range"]["Enc"][0]["mainStream"]; print("bitRate options:", ms["bitRate"])'
# expect to see 20480 as the last entry

# 3. Verify HTTP unlock works (try fetching a small file from /mnt/sda)
curl -I -u admin:<PASSWORD> "http://<CAMERA_IP>/downloadfile/test"
# expect HTTP 200 or 404 (file-not-found is fine — proves the path isn't 'internal')-throttled
```

If you flashed `build_netstate.sh`, also verify the daemon ran:
```bash
# Wait 90 seconds after boot for the daemon to make its first decision, then:
curl -u admin:<PASSWORD> "http://<CAMERA_IP>/downloadfile/netstate/log"
# expect lines like:
#   [<time>] state change: init -> home  (gw=<your-gw-ip> mac=aa:bb:cc:dd:ee:ff)
#   [<time>]   recording DISABLED (home, master enable=0)
```

## Step 7 — Set encoder for daily-driver consistency

Once the patches are applied, set the encoder to the verified-consistent
configuration via the web UI (or API):

- **Resolution**: 7680×2160
- **Frame rate**: 20
- **Bitrate**: 20480 kbps
- **GOP / I-frame interval**: 1× (= same as fps)

These settings produce a ~99.9%-consistent 20 fps stream with sub-ms
jitter and zero dropped frames over 65-second test windows. See
`FIRMWARE_PATCH_NOTES.md` for the consistency measurements.

## Step 8 — (Optional) Switch ISP exposure to Anti-Smearing

Reduces motion blur in fast action footage by ~half (verified A/B,
edge sharpness ~2× higher in detail-rich frames):

```bash
bash runtime/set_exposure.sh antismear
```

This is an ISP runtime setting, not a firmware patch. Persists across
reboots. Revert with `bash runtime/set_exposure.sh auto`.

## Recovery

If you ever need to roll back to stock:

1. Web UI → Maintenance → Local Upgrade → select the original stock
   `IPC_NT15NA416MP.4867_2505072124.Reolink-Duo-3-PoE.16MP.REOLINK.pak`.
2. Wait for reboot. The camera reverts cleanly.

The patched builds modify only the `app` and (for netstate/soccer-cam)
`rootfs` squashfs sections. The bootloader, kernel, and boot chain are
untouched, so the camera always comes back up to a known web-UI-flashable
state. There is no scenario in this tooling that requires UART recovery.

## (Comprehensive) Verify the card-full + power-cut fixes

These run on the camera, so verify them after a real game (or a deliberate
power-cut test). All paths are readable over the HTTP unlock.

```bash
# Power-cut recovery log (what the boot recovery did to each orphan):
curl -u admin:<PASSWORD> "http://<CAMERA_IP>/downloadfile/recover/log"
# expect, per recovered clip: "recovered N video samples ... N audio frames ... recovery OK"
```

The card-full truncation fix is **passive** — it's the raised free-space reserve,
which the firmware's own recycler enforces, so there's no daemon or log to check.
Verify it after a card-full game by confirming the **last** main-stream segment is
present and plays to the end (stock firmware would have dropped it).

To deliberately test recovery: start recording at the field, then pull power
mid-segment (the normal failure mode). On next boot the last clip — which stock
firmware would discard — is rebuilt in place and appears as a normal,
downloadable recording, a couple seconds shorter than the cut point.

If you want to confirm the recovery binary itself *before* flashing, run the
self-test in Step 4, option C against any good recording.

## Updating the netstate config without re-flashing

The daemon respects two runtime files on the SD card. Create them via
the camera's SD-card management UI or any other method that puts files
under `/mnt/sda/`:

| file | purpose |
|---|---|
| `/mnt/sda/netstate/home_macs.txt` | Override the baked-in MAC list. One MAC per line, lowercase. |
| `/mnt/sda/netstate/override` | Presence (any content) makes the daemon yield. You then control recording state via UI/API as normal. Delete the file to re-engage the daemon. |
| `/mnt/sda/netstate/log` | Decision log (auto-rotates at 256 KB). Read via the HTTP unlock. |

This means you can change networks, add a second known-home network,
or temporarily disable the daemon's behavior, all without re-flashing.

## Troubleshooting

| symptom | likely cause | fix |
|---|---|---|
| `build_*.sh` aborts with "router[0xXXXXXX] mismatch" | Stock pak is a different firmware version | Re-download `v3.0.0.4867_2505072124` exactly |
| Web UI rejects the patched pak | CRC mismatch (rare — would mean pak_repack.py bug) | Re-run the builder; if persistent, file an issue |
| Camera reboots but settings reverted | "Reset Configuration" was checked during upgrade | Just re-set encoder settings via web UI |
| `netstate/log` shows `api_login failed` | Wrong admin password baked into the daemon | Re-build with the correct password |
| Daemon log shows `init -> away` while you ARE at home | Wrong home MAC baked in | Either re-build with the correct MAC, or write the right MAC to `/mnt/sda/netstate/home_macs.txt` (no re-flash needed) |
| Daemon never logs anything | SD card not present (logs go to tmpfs) | Insert SD card; logs will start persisting on next boot |
| `build_soccercam_comprehensive.sh`: "need aarch64-linux-gnu-gcc" | Cross-compiler not installed | `sudo apt install -y gcc-aarch64-linux-gnu` |
| Comprehensive build says "Helix AAC source absent -> VIDEO-ONLY" | Helix not fetched | Clone it per `recover/helix/README.md` (or accept video-only recovery) |
| Builder: "`<N>` bytes not single-movz encodable" | Reserve GiB isn't a clean `movz` immediate | Use 16, 20, or 32 GiB |
| Builder: "reserve site mismatch" | Stock pak is a different firmware version | Re-download `v3.0.0.4867_2505072124` exactly |
| `recover/log` shows "unrecoverable" / "no video samples" | Clip cut before the first keyframe, or not a main-stream recording | Nothing to recover; the file is left in place (never deleted) |
| `verify/test_recover_mp4.sh` fails | Toolchain gap or a real regression | Confirm it ends in `PASS` on a known-good recording before trusting a build |
