# Record-at-home gate — design & roadmap (Reolink Duo 3 PoE)

Status: **design, pre-hardware-validation.** Nothing here is flashed/verified on a
camera yet. Target firmware: `IPC_NT15NA416MP` v3.0.0.4867_2505072124.

## Problem & goal

On every boot the camera writes a short (~4s) aborted segment
(`RecM09_DST..._000000_0_...mp4`) before anything can stop it. We want the
broader behavior: **record at the field, not at home** — driven by the network
the camera is on — with one hard rule.

### Behavior spec (the three invariants)

1. **Not connected to a network ⟹ RECORD** (no link, link-but-no-DHCP, no/unreachable gateway, error, timeout).
2. **Connected to a non-home network (field router / hotspot / venue) ⟹ RECORD.**
3. **Connected to the home network ⟹ STOP.**

**FAIL-ENABLED is the law:** the default is *record*; recording is suppressed
*only* on a positively-confirmed **home gateway MAC**. Any failure/uncertainty
records, so a game is never missed. (The field can have a real network, so plain
"any network ⟹ stop" is unsafe — we must identify *home* specifically.)

## Boot architecture (verified from the decompiled firmware)

`rcS` runs `/etc/init.d/S??*` in lexical order:

```
S00_PreReady  mount /mnt/app (squashfs, RO) ; watchdog_monitor_start &
S07_SysInit   mdev / SD (mmc)
S10_SysInit2  video/audio/sensor/codec kernel drivers
S25_Net       eth0 up (STATIC_IP 192.168.0.3) ; telnetd        ← no DHCP yet
S99_NetState  our daemon (sorts before Sysctl: 'N' < 'S')
S99_Sysctl    -> /etc/init.d/start_app
```

`start_app` (the only launcher of the app daemons):

```
mount /mnt/para (yaffs2, rw, /dev/mtdblock8)   # persistent config partition
./do_before_app          # line ~106, NOT backgrounded, file absent by default
./router &               # 108  — config owner; feeds /dev/watchdog after handoff
./device &               # 109  — launches udhcpc => DHCP/gateway appear HERE
./recorder &             # 110  — writes the boot segment
... ./cgiserver.cgi      # 131  — HTTP API (too late to pre-empt the segment)
```

Key facts that constrain every approach:

- **`recorder` gets its record-enable via IPC** (`MSG_CFG_REC_NOTIFY = 0x812`,
  `on_rec_config_notify`). It does **not** read `rec.cfg` (only `/mnt/para/format_info`).
- **`router` owns `rec.cfg`** (`/mnt/para`, `cfg_rec.cpp`, `rec_config_t`), and it is
  **AES-encrypted** (`support_record_encrypt="1"`, `file_aes_encrypt_decrypt`,
  `reckey.cfg`, `aes_ver`, hardcoded passphrase).
- **`/dev/watchdog` feeders:** `watchdog_monitor_start` (S00) → hands off to
  `router`. **`recorder` is NOT a watchdog feeder** ⇒ skipping/delaying it is watchdog-safe.
- **HTTP `/downloadfile/`** is nginx static-serve (config baked in the `device`
  binary) ⇒ **downloads at home work without `recorder`.** Baichuan replay (port
  9000) does appear to involve `recorder`, so the Baichuan *fallback* needs it —
  but our HTTP unlock is part of the same patch set, so HTTP is guaranteed.
- **No network exists before `device` (line 109)** — boot is STATIC_IP; `udhcpc`
  is launched by `device`. So any gateway-MAC check must run *after* line 109.
- **No SD-card web UI** exists for editing arbitrary files (the netstate README's
  "edit via the SD web UI" claim is wrong — to fix). Runtime file delivery is
  telnet or pulling the card; therefore **config is baked at build time.**

## Approaches considered

| Approach | Verdict |
|---|---|
| Shell-edit `rec.cfg` (enable=0) | ❌ recorder never reads it; AES-encrypted; no-op or corruption |
| Bootloader / uboot / kernel cmdline | ❌ no network that early; RSA/eFuse secure-boot ⇒ brick risk |
| Patch `router` boot-notify to enable=0 | ❌ **fail-DISABLED** (needs a re-enabler that doesn't exist) |
| `dvr.xml` pre-record off (`prerec_offset_sec=0`) | ⚠️ probe only — likely no-op (prerec consumed via IPC, not the xml); zero-risk experiment to bisect the mechanism |
| **Route A — `start_app` recorder-gate** | ✅ deterministic, no binary patch; recorder absent at home (HTTP-only downloads) |
| **Route B — `recorder` flag-file-gate** | ✅ lowest brick risk, recorder stays up; needs a Ghidra session + has a record-start race |

### Route A — `start_app` recorder-gate (rootfs, no binary patch)

Insert between `./device &` (109) and `./recorder &` (110): bounded-wait for the
home gateway MAC, then launch the recorder only if NOT home.

```sh
./router & ./device &                      # watchdog + DHCP up first
home=0; end=$(( $(cut -d. -f1 /proc/uptime) + 15 ))
while [ "$(cut -d. -f1 /proc/uptime)" -lt "$end" ]; do
    gw=$(awk '$2=="00000000"{print $3}' /proc/net/route)
    [ -n "$gw" ] && { ping -c1 -W1 "$gw_dotted" >/dev/null 2>&1
        mac=$(awk -v ip="$gw_dotted" '$1==ip && $4!="00:00:00:00:00:00"{print $4}' /proc/net/arp)
        [ -n "$mac" ] && { is_home_mac "$mac" && home=1; break; }; }
    sleep 1
done
[ "$home" = 1 ] || ./recorder &            # NO net / field net / timeout / error -> RECORD
```

- Deterministic (recorder never starts at home ⇒ zero segment).
- Watchdog-safe (router feeds it; recorder isn't a feeder).
- Trade: no `recorder` at home ⇒ HTTP-only downloads (accepted), no in-app
  playback at home. Edits the boot-critical launch script (soft-brick if botched;
  no characterized UART recovery — assert + bench-test).

### Route B — `recorder` flag-file-gate (byte patch, app-only)

Redirect one instruction in `recorder` (ELF base `0x400000`) to an
`access("/mnt/para/no_record")`-guarded stub; flag present ⇒ skip the segment,
absent/any error ⇒ records.

- Patch site (stock bytes, for the build-time assertion):
  `0x40fac0 = 37 00 80 52` (`mov w23,#1`), `0x40fac4 = 87 fe ff 97`
  (`bl 0x40f4e0` — **redirect this** to `B <stub>`), `0x40fac8 = 53 ff ff 17`
  (`b 0x40f814`, success tail).
- Stub goes in the 288 bytes of segment padding at file `0x8baa8..0x8bbc8`;
  uses `access@plt 0x406100`. Flag present → `b 0x40f814`; absent →
  `bl 0x40f4e0; b 0x40fac8` (replicate original).
- **Open RE item:** confirm `0x40f4e0` is side-effect-safe to skip (no half-open
  fd / held mutex); else gate at the scheduler `0x40ab84`.
- Lowest brick risk (app squashfs only, boot chain byte-identical; a bad patch
  just "records normally"), recorder stays up (full downloads/playback).
- Weakness: the `access()` check is at record-*start*, so on a home-boot-after-field
  the arming may not land before the first segment (race) — that residual stub is
  dropped by the soccer-cam runt filter. Deterministic prevention would need a
  bounded-wait in the stub (tight on the 288-byte headroom).

**Recommendation:** Route A for deterministic prevention + no Ghidra (given
HTTP-only home downloads are acceptable). Route B if keeping the recorder alive at
home matters and we accept a Ghidra session + the runt-filter backstop. Both share
the same home-MAC arming logic and both are fail-enabled.

## Config delivery — bake at build time

No SD web UI ⇒ the **home gateway MAC is baked into the patched `.pak` at build
time** (no runtime file). The patcher auto-detects it: the build host (the
soccer-cam PC) is on the home LAN, so its own default-gateway MAC *is* the home
gateway MAC (`Get-NetNeighbor`/`arp` on Windows). Zero manual entry.

Empty/no baked MAC ⇒ nothing matches ⇒ always records (fail-enabled default).

## soccer-cam integration

### Gateway-MAC drift monitor (buildable now; Reolink-only)

- `video_grouper/utils/network.py`: `get_default_gateway_mac()` (Windows:
  `Get-NetRoute`+`Get-NetNeighbor`/`arp`; Linux: `/proc/net/route`+`/proc/net/arp`).
- `CameraConfig.firmware_home_gateway_mac: str | None` — the MAC baked into that
  camera's firmware (set by the patcher). Dormant while unset.
- Startup + slow-interval check: if set and the live gateway MAC differs ⇒ raise a
  notice; if it matches again ⇒ clear.
- Surface: mirror `web/auth_status.py` (a `shared_data/*_attention.json` flag) ⇒
  **dashboard banner + tray toast** for free, plus a WARNING log. Message: "Home
  gateway changed (was X, now Y) — re-patch the camera firmware with the new value."

### 1-click firmware build (later; Reolink-only)

A "Patch camera firmware" panel in the web config, shown only for
`camera.type == "reolink"`:

1. Detect the camera's firmware version (Reolink API) → tell the user exactly
   which stock `.pak` to download from Reolink.
2. User uploads the stock `.pak`; tool verifies (SHA-256 + stock-byte assertions),
   fails closed on mismatch.
3. Auto-detect the home gateway MAC (host) → bake it + the gate → repack with the
   solved CRC → **offer the patched `.pak` for download + a link to the flashing
   instructions** (manual flash via the camera's Local Upgrade — not auto-flash).
4. The drift banner's action button routes into this same flow ("Rebuild firmware").

Implementation notes:
- The patcher is Python (pak parse/CRC/byte-patch/file-inject already exist) +
  **bundled `squashfs-tools-ng`** for the one squashfs step, shipped inside the
  PyInstaller package.
- **Licensing:** ship the patch *transform*, never Reolink's firmware — the user
  provides their own legal download.
- **Version-locking:** Route B's byte offsets are specific to v3.0.0.4867 (assert
  + fail closed); Route A's text edits are far more robust across firmware
  revisions ⇒ Route A is the better payload for a broadly-distributed tool.

## Staged roadmap

1. **Hardware validation** (spare camera) — settle the open items below.
2. Finalize the chosen gate (A or B) + `build_recordgate.sh`.
3. Package the patcher as a Python module (bundle `squashfs-tools-ng`).
4. Wire the Reolink-only web panel + drift banner; have the patcher write
   `CameraConfig.firmware_home_gateway_mac` so the drift monitor goes live.

## On-device test checklist (fail-enabled matrix)

- No cable / power-only injector (no DHCP) → **records**.
- Foreign gateway (phone hotspot / 2nd router) → **records**.
- Home gateway MAC → **no `Mp4Record` segment**.
- Daemon/script disabled or errored → **records**.
- Ethernet unplugged → bounded timeout → **records**.
- Mid-game reboot → **records** (not re-armed fast enough to gate session 2).
- Home cold-boot → box stays up >10 min (no watchdog reboot).
- Field cold-boot → records with full pre-roll (no clipped kickoff).
- HTTP `/downloadfile/` download of an existing clip works with recorder skipped (Route A).
- Re-flash stock `.pak` → clean revert.
- Cheap pre-step: flash `dvr.xml` `prerec_offset_sec=0`+`support_prerec_by_new=0`,
  cold-boot — does the stub vanish? (bisects the mechanism for free).

## Open items requiring the camera

- Exact `/mnt/para/rec.cfg` format (encrypted vs plaintext) — `od -c`.
- DHCP/route/ARP readiness latency after `device` launches `udhcpc` → sets the safe bounded-wait cap.
- Route B: is `0x40f4e0` side-effect-safe to skip?
- Confirm `recorder`-absent doesn't break the home download path beyond HTTP.
