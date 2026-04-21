# Reolink Duo 3 PoE — Firmware Patch Notes

End-to-end record of reverse-engineering the firmware container and shipping
working patches for the Reolink Duo 3 PoE running stock firmware
**v3.0.0.4867_2505072124** (May 2025 build, board `IPC_NT15NA416MP`).

## Recommended daily-driver build

For **most users**: `build_bitrate_cap.sh stock.pak out.pak 20480` — HTTP
unlock + 20 Mbps bitrate cap, nothing else.

For **soccer-cam users (Mark)**: `build_netstate.sh stock.pak out.pak
20480 admin <password> <home-gateway-mac>` — adds an /etc/init.d/S99_NetState
daemon that toggles the master `Rec.enable` flag based on the gateway MAC.
At home → `enable=0`, no recording fires. Anywhere else (or cable
unplugged) → `enable=1` + `TIMING` all-on, continuous recording starts
within ~60 s of boot. End-to-end verified 2026-04-20: home boot → idle,
unplug + boot → recording starts, replug + boot → idle. Decision log at
`/mnt/sda/netstate/log` (rotates at 256 KB), readable via the HTTP
unlock at `http://<cam>/downloadfile/netstate/log`.

Both builds carry only the two patches that have been empirically
verified to be both functional AND consistent:

- HTTP `/downloadfile/` unlock (LAN downloads at full PoE wire-speed)
- Main-stream max bitrate raised from 12288 to 20480 kbps

Verified consistency at 7680×2160 @ 20 fps gop=1 across 3 back-to-back
65-second recordings (2026-04-20):

| metric | clip 1 | clip 2 | clip 3 |
|---|---|---|---|
| avg fps | 20.005 | 20.005 | 19.997 |
| jitter (σ) | 0.71 ms | 0.71 ms | 0.16 ms |
| dropped frames | 0 | 0 | 0 |
| 50 ms-cadence frames | 99.9% | 99.9% | 100% |

The fps patches (`build_fps_cap.sh`) are kept in the repo for completeness
and for any future tinkering, but **not recommended for daily use**:
running the encoder above its native 20 fps target at 16MP introduces
~20% frame drops with significant jitter. The full investigation that
established this is in section 3 below.

Two distinct goals were pursued and resolved in this session:

1. **HTTP download path** — fix the broken `cmd=Download` flow so soccer-cam
   can pull recordings at native LAN speed.
   **Result:** 0 → 86 Mbps (saturating the camera's PoE Ethernet).
   Baichuan, the previous fallback, runs at ~14 Mbps.

2. **Encoder bitrate cap** — lift the firmware-imposed 12 Mbps maximum
   bitrate so 16MP/8K recordings are less compressed.
   **Result:** 12 Mbps → 20 Mbps (proven 16.15 Mbps measured on a real recording).
   The 21+ Mbps ceiling is enforced by the encoder hardware/firmware and
   refuses higher values with `rspCode -13 "set config failed"`.

A third investigation was completed:

3. **Frame-rate cap (20 → 25 fps usable, full chain mapped)** — the
   surface-level cap is genuinely lifted but the recorded output stays at
   ~20 fps because the h.265 ASIC encoder is the real bottleneck at 16MP.
   Deeper tracing through the full videocap pipeline (kernel
   kflow_videocapture.ko ← userspace `device` binary ← Nvt52xAdapter.cpp)
   located the actual 20 fps hardcode:

   - **File**: `sections/app_extracted/device` (main userspace IPC binary)
   - **Function**: `Na_video_encoder_build_basic` in Nvt52xAdapter.cpp
     (Ghidra: `FUN_0048b630`)
   - **Site**: VMA `0x0048bb1c`, file offset `0x8bb1c`
   - **Instruction**: `movz w1, #0x14` (= load 20), encoded `81 02 80 52`
   - **Followed by**: `str w1, [x19, #0x2884]` which stores `fps` into the
     video-encoder config object
   - **Condition**: only executes for sensors matched by mask `0x1080c241`,
     which includes the OS08C10 (sensor type `0x26`)

   Downstream flow: `obj[+0x2884]` → `FUN_0047e230` passes it as `param_7` to
   `FUN_00488df0` → `cap_path[+0x34] = fps` → `FUN_00488af0` builds the frc
   fraction as `(fps << 16) | 1` and calls `hd_videocap_set(cap_path,
   HD_VIDEOCAP_PARAM_IN=0x80001016, &video_in_param)` → kernel
   `_isf_vdocap_do_setportstruct` case `0x80001016` copies `param_4[4..7]` to
   `ctx[+0xb8]` (= `HD_VIDEOCAP_IN.frc`). Any later fps request via
   `vendor_videocap_set(path, 0x80001024, &fps)` is clamped to `ctx[+0xb8]`
   in `_isf_vdocap_do_setportparam` case `-0x7fffefdc`, which prints
   `WRN:...could not greater than HD_VIDEOCAP_IN.frc(%d)` if you try.

   **Two minimal patches in `device` and `router` (build_fps_cap.sh)**:
   - `device` file offset `0x8bb1c`: `81 02 80 52` → `21 03 80 52`
     (`movz w1, #20` → `movz w1, #25`) — lifts the userspace HD_VIDEOCAP_IN.frc
     hardcode for the OS08C10 sensor mask
   - `router` file offset `0x6565c`: `80 02 80 52` → `20 03 80 52`
     (`movz w0, #20` → `movz w0, #25`) — lifts the 7680×2160 fps dropdown max
     in `FUN_00465584` so the web UI lets you pick 25
   - `router` FUN_004632b0 has 9 additional `mov w0, #0x14` sites that we
     also patch to 25; these raise the per-resolution fps caps for OTHER
     resolutions but are not strictly required for the 7680×2160 daily-driver

   **Flashed and tested (build 4888).** OS08C10 sensor demonstrably runs at
   25 fps after the patch (verified by frame-timing analysis: 74% of
   inter-frame gaps are exactly 40 ms = 1/25s cadence). The downstream
   pipeline forwards every frame. **However, the h.265 ASIC encoder cannot
   sustain 25 fps at 16MP** and silently drops ~20% of frames to fit its
   compute budget, producing ~19.83 fps in the recorded mp4.

   **Encoder bottleneck verified by bitrate test:** dropping the bitrate cap
   from 20 Mbps to 8 Mbps produced statistically-identical drop rates
   (74% / 73% on-cadence; 20% / 22% single drops). If drops were rate-limited
   the encoder would saturate the bitrate first; instead it leaves 27% of
   the budget unused and still drops. This is a hard ASIC pixel-throughput
   ceiling at ~330 Mpix/s (16.6 Mpix × 19.83 fps). Typical for a Novatek
   NT9856x marketed as "4K60" (~500 Mpix/s peak, lower sustained).

   **What the patch is still worth despite the encoder ceiling:** the
   sensor's per-frame exposure window shrinks from 50 ms (at 20 fps) to 40
   ms (at 25 fps), giving ~20% less motion blur on each retained frame.
   For sports/motion footage this is a real visible improvement even
   though the recorded fps stays around 20.

   **Per-resolution validator caps remain at 20 fps for sub-7680×2160 modes.**
   The 4096×1152 mode shows `frameRate: [20, 18, 16, ...]` because its cap
   is set by a *computed* csel branch in `FUN_004632b0` rather than a
   patchable literal — raising it would require non-trivial control-flow
   reverse engineering. Given the encoder ceiling above, doing so would only
   prove the obvious (lower-resolution input would be encoder-uncontended)
   and wouldn't unlock anything practically useful for the dual-sensor
   16MP product.

   Caveats / context:
   - Sensor driver `nvt_sen_os08c10.ko` exposes pclk = 150 MHz (not 198 as
     I initially mis-cited; the 198 MHz figure is a different speed_param
     field, likely the MIPI/ADC clock). At pclk=150 MHz with HTS=2592 and
     min VTS≈2170, max sensor fps at 4K is ~26.7 fps — so 25 fps is
     achievable but 30 is not without modifying the sensor PLL register
     table (risky).
   - All inspected Reolink products that ship the OS08C10 (Duo 3 variants,
     Elite W740) use bit-for-bit identical sensor driver configuration
     with base_FPS=25 and pclk=150 MHz. Reolink never configures this
     sensor above 25 fps in any product.
   - SIE input limit (`kdrv_sie_limit_98538` +0x04 = 150M) matches the
     pclk exactly, suggesting the SIE is sized for exactly 150 MHz of
     sensor input — no headroom without a kernel-side bandwidth patch.

The camera was never bricked. **Multiple flashes** total, all via the web
UI's manual upgrade button. No UART required.

---

## 1. Hardware / firmware

| Field | Value |
|---|---|
| Camera | Reolink Duo 3 PoE (16 MP dual-lens) |
| Board | IPC_NT15NA416MP, hardware B17B53916M_V1 |
| SoC | Novatek NT9856x (ARM64, dual core) |
| Linux | 5.10.168, Buildroot 2022.08 |
| Stock firmware | v3.0.0.4867_2505072124 (file `IPC_NT15NA416MP.4867_2505072124.Reolink-Duo-3-PoE.16MP.REOLINK.pak`) |
| Web UI | nginx with FastCGI to `cgiserver.cgi` on 127.0.0.1:9527 |

---

## 2. The PAK container

`.pak` is Reolink's firmware container — magic `0x32725913` ("PAK64" variant
in pakler nomenclature). Layout:

```
0x00..0x07    magic            u64 LE  (lower 4 = 0x32725913, upper 4 = 0)
0x08..0x0F    header CRC       u64 LE  (lower 4 = CRC32-variant, upper 4 = 0)
0x10..0x17    type             u64 LE  (observed 0x4b02 = 19202)
0x18 + i*0x48 section table    15 entries, each 72 bytes:
                               name[32] + version[24] + start_u64 + size_u64
0x18 + 15*0x48 = 0x450
                partition table (per-section /dev/mtdN map + flash offsets)
0x8c8         payload start (loader section)
EOF           after app section
```

**Important:** the section table has **15 entries** (8 used + 7 zero/empty).
The partition table maps each section to an MTD device:

| Section | mtd | flash offset | flash size |
|---|---|---|---|
| loader | mtd0 | 0x000000 | 0x040000 |
| fdt | mtd1 | 0x040000 | 0x040000 |
| atf | mtd2 | 0x080000 | 0x040000 |
| uboot | mtd3 | 0x0C0000 | 0x100000 |
| kernel | mtd4 | 0x1C0000 | 0x3C0000 |
| rootfs | mtd5 | 0x580000 | 0x800000 |
| ai | mtd6 | 0xD80000 | 0x700000 |
| app | mtd7 | 0x1480000 | 0xB00000 |
| para | mtd8 | 0x1F80000 | — |

Section payload formats:
- `loader`, `atf`, `ai` — opaque vendor blobs
- `fdt` — flat device tree (`d00dfeed`)
- `uboot` — u-boot binary
- `kernel` — uImage (legacy mkimage), uncompressed ARM64 Image
- `rootfs` — SquashFS xz, 256K block, busybox userland
- `app` — SquashFS xz, 256K block, Reolink camera application

`version.json` (inside `app`) lists every section and a `forbid_ol_up` flag.
For our firmware, `loader`/`fdt`/`atf`/`uboot` all have `forbid_ol_up=1`
— Reolink's online updater won't touch them.

---

## 3. The CRC algorithm — fully reverse-engineered

The header CRC at file offset 0x08 is **not** standard zlib CRC32. The
existing `vmallet/pakler` tool uses zlib semantics and gets a different
value than the camera computes — its repacked paks won't pass validation
on the Duo 3 PoE.

The actual algorithm was traced from the `upgrade` ELF inside `app`:

- `bc_gen_crc` is at VMA `0x4195b8`, 56 bytes:
  ```c
  uint64_t bc_gen_crc(uint64_t init, const char *data, uint64_t len) {
      while (len--) {
          init = TABLE[(init ^ *data++) & 0xff] ^ (init >> 8);
      }
      return init;
  }
  ```
- The 256-entry table at VMA `0x4540d0` is the standard zlib CRC32
  polynomial `0xedb88320`, but stored as 8-byte cells (upper 32 bits 0).
- **No init = 0xffffffff, no final XOR.** Caller controls both.

The verifier function (around VMA `0x41af50`–`0x41b088` in `upgrade`)
does:

```python
crc = bc_gen_crc(0, file[0x8c8:])                       # all payload bytes
crc = bc_gen_crc(crc, b"\x02" + b"\x00"*7)              # 8-byte type marker
crc = bc_gen_crc(crc, file[0x18 : 0x18 + 15*0x48])      # full section table
expected = u32 LE at offset 0x08 in the header buffer
if crc != expected: reject
```

**The camera reads exactly `0x8c8` bytes as header** (hardcoded). So the
first section payload **must** start at `0x8c8`. Pakler shifts payloads
by -4 bytes (drops the 4-byte zero pad before loader); even with the
right CRC algorithm, pakler's output corrupts the camera's view of the
section table because it reads from the wrong offset.

Standalone tool: [`reolink_crc.py`](./reolink_crc.py) — produces the
correct CRC and matches the stock pak's stored value `0x23b254c5`
exactly.

---

## 4. The verification path

Two binaries handle firmware updates:

- `app/upgrade` — long-running daemon spawned by `start_app`. Listens
  for IPC messages, downloads online updates, stages files, and execs
  `update` to actually flash.
- `app/update` — short-lived flasher. Invocation pattern from inside
  `upgrade`: `update 0 <pak_path> all <board_ver> [offline_fw] [board]`.

What we found, in priority order for "will a modified pak be accepted":

1. **App-side magic check (UNCONDITIONAL, accepts both magics):**
   `upgrade` at VMA `0x42c1b0` accepts `0x32725913` **or** `0x32725923`
   without any flag gating. Our pak has `0x32725913`. Always passes.

2. **App-side CRC check:** the `bc_gen_crc` algorithm above. We compute
   it correctly via [`reolink_crc.py`](./reolink_crc.py). Passes.

3. **Per-section CRCs:** the per-section validator at VMA `0x41f120`
   compares each section's `bc_gen_crc(init=0, payload)` against an
   **expected value loaded from a struct field, not from the .pak file**.
   We searched all variants of all 8 section CRCs in the entire pak —
   zero matches. The expected values come from the **online manifest**
   that Reolink's CDN serves alongside the .pak. Manual web-UI uploads
   skip this check entirely.

4. **u-boot signature check:** `uboot` binary contains
   `enable_secure_boot`, `efuse_signature_rsa_en`, `is_signature_rsa`,
   etc. RSA verification capability exists but is **gated by an eFuse
   bit**. Whether Reolink blew the fuse on production cameras is the
   one bricking risk we couldn't confirm without UART. Empirically,
   our two patched flashes booted fine — so on this unit at least, the
   fuse is not blown.

5. **ATF / loader:** strings only mention "secure" but no active
   verification functions. Not enforcing.

6. **Kernel modules:** no signed-module enforcement, no dm-verity.
   Once the kernel boots, the rootfs and app are unprotected.

The dispatch table for cgiserver's HTTP commands is in `.data` starting
at file offset `0x106f04`. Each entry is **80 bytes** (0x50): 32-byte
name, then 4 function pointers and 2 small integer flags. The `Download`
entry is at `0x108994` with main handler at VMA `0x471a14` and
"response_type" byte = `0xad` (173) — a value the response emitter
doesn't have a case for. That's the `cmd=Download` bug: even if you
call it correctly, the response emitter falls into a default branch
that writes nothing, producing the empty-reply behavior.

---

## 5. What we patched

### Patch v1 (build 4868) — limit_rate only

Inside `device` ELF in the app SquashFS, two copies of:
```nginx
location /downloadfile/ {
    internal;
    limit_conn one 1;
    limit_rate 1024k;
    alias /mnt/sda/;
}
```

Replaced `limit_rate 1024k;` (17 bytes) with `limit_rate 0;    ` (17
bytes — `0` means unlimited in nginx; trailing spaces preserve length).
Camera flashed and rebooted cleanly. **But this alone did nothing**
because `internal;` keeps the location reachable only via FastCGI
`X-Accel-Redirect`, which nobody was generating since `cmd=Download`
is broken.

### Patch v2 (build 4869) — comment out `internal;`

Same-length 146-byte block replacement. Both `/downloadfile/` blocks
become:
```nginx
location /downloadfile/ {
           #internal;
            limit_conn one 1;
            limit_rate 0;    
            alias /mnt/sda/;
}
```

The `#internal;` line is a valid nginx comment (whitespace + `#`),
making nginx skip it. The location is now externally accessible.

### Tradeoffs of v2

- ✅ Direct GET from `/downloadfile/<relative-path>` returns the file at
  ~86 Mbps with no auth, no CGI overhead. Confirmed 71 Mbps in a single-stream
  download of a 195 MB recording in 22 s.
- ⚠️ **No authentication on this path.** Anyone on your LAN who can
  reach the camera and guess a recording filename can download it. The
  filenames are obscure enough to be hard to guess but it's not a
  security boundary. Don't expose the camera to untrusted networks.
- ⚠️ `limit_conn one 1` is still in place — one concurrent download per
  source IP. Sequential downloads work fine; parallel from the same
  client will queue.
- ⚠️ Touches `/downloadfile/` only. `/playback/` still has `internal;`,
  so RTMP playback flow is unchanged.

### Patch v10 (build 4876) — bitrate cap PROVED

Single-byte-pair patch in `router` at file offset `0x6351c`:

```
before: 0b 00 86 52   ; mov w11, #0x3000   (12288 kbps = stock max)
after:  0b 00 8c 52   ; mov w11, #0x6000   (24576 kbps target)
```

`router` (not `device`) owns the encoder capability table. Source file is
`bc_cfg.cpp`; the function is `FUN_004632b0` which builds the per-resolution
cap struct that ends up in the API's `range.Enc[*].mainStream.bitRate`.

After flashing, the API range gained `24576` (replacing `12288`) and the web
UI dropdown showed `24576` as a selectable max. **However, attempting
`SetEnc bitRate=24576` returned `rspCode -13 "set config failed"`** — the
encoder hardware/firmware refused. Stream fell back to ~7 Mbps and preview
went to "Fluent" until a valid bitrate was re-set.

This is the patch that proves the patch site is correct; not the daily
driver.

### Patch v11 (build 4877) — 16 Mbps (works)

Same offset, `0b 00 88 52` (`mov w11, #0x4000` = 16384). `SetEnc bitRate=16384`
succeeds. Preview stays "Clear". First confirmed working bitrate above stock.

### Patches v12-v14 (builds 4878-4880) — binary-search the encoder ceiling

Same single-byte patch at `0x6351c`, parameterized by
`builds/build_bitrate_cap.sh`:

| build | kbps | result |
|---|---|---|
| 4877 | 16384 (16 M) | ✅ accepted |
| 4878 | 18432 (18 M) | (untested) |
| 4879 | **20480 (20 M)** | ✅ accepted — **daily driver** |
| 4881 | 21504 (21 M) | ❌ `rspCode -13` |
| 4880 | 22528 (22 M) | ❌ `rspCode -13` |
| 4876 | 24576 (24 M) | ❌ `rspCode -13` |

Encoder ceiling is in `(20, 21)` Mbps for 16MP/h265. We accept **20 Mbps as
the daily-driver value** — a 67% increase over stock with zero side effects.

### Patch v17 (build 4885) — fps dropdown lift (cosmetic only)

Bitrate-20-Mbps patch + single-byte change in `router::FUN_00465584` at
file offset `0x6565c`:

```
before: 80 02 80 52   ; mov w0, #0x14   (= 20)
after:  20 03 80 52   ; mov w0, #0x19   (= 25)
```

`FUN_00465584` builds the 9-entry main-stream fps array `[20, 18, 16, 15, 12,
10, 8, 6, 4]` via a sequence of 9 `mov w0, #N` instructions at file offsets
`0x6565c .. 0x656d4`. Patching the first slot to 25 (or 30 via `c0 03 80 52`)
shifts position-0 in the API range.

After flashing, the API showed `frameRate: [30, 25, 18, 16, 15, 12, 10, 8, 6, 4]`
in `range.Enc[0]`. SetEnc accepted `frameRate=30` without error.

**BUT the actual encoder ignored the requested fps and continued to encode at
20 fps.** Verified via ffprobe on a real recording:

```
nb_frames     = 2145
duration      = 107.666 s
avg_frame_rate = 19.92 fps      ← still 20 fps
```

The lower-resolution profile (4096×1152) caps natively at 20 fps too. So:
**20 fps is a real Duo 3 hardware ceiling.** The 25/30 fps paths visible in
firmware are for single-sensor 1080p models that share the same code base.
**Do not flash the fps patch as your daily driver** — it only lies in the
dropdown without delivering frames.

The bitrate-only patch (build 4879) is the recommended daily build.

---

## 5b. The encoder cap architecture (PROVEN)

The complete data flow for the GetEnc range API:

```
HTTP /cgi-bin/api.cgi?cmd=GetEnc&action=1&token=...
       │
       ▼
cgiserver.cgi   (cgi_enc.cpp)
   FUN_00444c48 (cgi_cmd_get_enc) — state machine, sends 3 IPC msgs:
       │
       ├── MSG_CFG_ENC_DEF_GET    (0x8b2)  → 268-byte default-cfg struct
       ├── MSG_CFG_ENC_GET        (0x8ae)  → current cfg
       └── MSG_CFG_ENC_TABLE_GET  (0x8b1)  → 10628-byte (=0x2984) range table
       │
       ▼
Reolink IPC bus → router (the actual cap owner)
       │
       ├── on_enc_def_get  router::FUN_0041b114  → returns 268-byte struct
       │   from a stored vtable lookup. Holds CURRENT scalar values
       │   (channel + 23 fields per stream).
       │
       ├── on_enc_table_get router::FUN_0041a780  → memcpy 0x2984 bytes
       │   from the cached table at *(ctx + 0x48) + 0x4240 + 0x28.
       │   This is the table the GetEnc range comes from.
       │
       ▼
The 0x2984 cached table is BUILT by:
       router::FUN_004632b0  (~6 KB body, in bc_cfg.cpp)
           - branches by resolution code
           - writes per-resolution scalar caps (max_fps, max_bitrate, etc.)
           - calls FUN_00465584 for the fps array
           - calls FUN_00465bc4 for the reduced-fps array (need_reduce_fps=1)

       Key writes inside FUN_004632b0:
           file 0x6351c : mov w11, #0x3000  ← max bitrate (12288, OUR PATCH SITE)
           file 0x637fc..0x64384 : 9 sites of mov w0, #0x14   ← max-fps fields
                                                                (NOT the array)

       Key writes inside FUN_00465584 (the actual fps array):
           file 0x6565c : mov w0, #0x14   ← array[0] (= 20 fps, OUR FPS PATCH)
           file 0x65664 : mov w0, #0x12   ← array[1] (= 18)
           file 0x6566c : mov w0, #0x10   ← array[2] (= 16)
           file 0x65674 : mov w0, #0xf    ← array[3] (= 15)
           file 0x6567c : mov w0, #0xc    ← array[4] (= 12)
           file 0x656bc : mov w0, #0xa    ← array[5] (= 10)
           file 0x656c4 : mov w0, #0x8    ← array[6] (= 8)
           file 0x656cc : mov w0, #0x6    ← array[7] (= 6)
           file 0x656d4 : mov w0, #0x4    ← array[8] (= 4)
       │
       ▼
SetEnc (MSG_CFG_ENC_SET, 0x8ad) handler is also in router:
   router::FUN_00427f50  — validates the 268-byte payload, then calls
   router::FUN_00427de8  which makes a vtable call (*p_enc + 0x28)()
                          to actually apply the new config.

   That vtable call is the rejection point. Returns nonzero on
   unsupported values (e.g. bitrate > ~20 Mbps for 16MP). cgi sees the
   nonzero response code and reports `rspCode -13 "set config failed"`.

   No `cmp w_, #0xNNNN` against a hardcoded bitrate exists in any of
   {router, device, recorder, cgiserver.cgi}. The cap is computed
   dynamically inside the encoder layer (likely the kdrv_h26x.ko kernel
   module based on resolution × fps × quality factor).
```

Key facts established by static analysis:
- Stock firmware does have 25/30 fps code paths (confirmed in `FUN_004632b0`
  branches on resolution codes 0x28, 0x48, etc.) — but the Duo 3 doesn't
  expose those resolution codes via its sensor configuration.
- Each app binary has its own copy of the dvr.xml flag globals in `.bss`,
  populated by a 432-call dispatch loop in each binary's `FUN_0042b3d0`
  equivalent. The recorder binary parses the values but does not visibly
  read them — they're consumed by other binaries via IPC NOTIFY.
- Patching `device` (for v5/v8) had no effect on the API range because the
  range source is `router`, not `device`. **Lesson: the encoder cap lives in
  router/bc_cfg.cpp, not device/anything.**

---

## 6. Toolchain (committed under `reolink-firmware-patching/`)

All scripts run from WSL Ubuntu when they need Linux utilities
(`unsquashfs`, `mksquashfs`). Set up credentials once by copying
`camera.env.example` → `camera.env` and filling in CAMERA_IP / USER /
PASS. All runtime and verify scripts source `_camera_env.sh` which
picks these up.

### Pak container toolchain (`pak/`)
| File | Purpose |
|---|---|
| `pak/pak.py` | Low-level parse / extract / inspect. Standalone CLI: `python pak.py <pak>` shows section table and probes the checksum field. |
| `pak/reolink_crc.py` | Standalone CRC compute/patch. `python pak/reolink_crc.py compute <pak>` prints both stored and freshly-computed CRCs. |
| `pak/pak_repack.py` | Clean PAK repacker. Preserves the exact byte layout the camera's verifier expects (first section at `0x8c8`, full 15-entry section table, correct CRC). Usage: `python pak/pak_repack.py <stock.pak> <out.pak> <section_name> <new_payload.bin>`. |
| `pak/extract.py` | One-shot extractor that splits a `.pak` into `sections/<idx>_<name>.bin` files. |

### Flash-patch builders (`builds/`)
| Script | What it builds |
|---|---|
| `builds/build_http_unlock.sh` | HTTP `/downloadfile/` unlock. `sudo bash builds/build_http_unlock.sh <stock.pak> <out.pak>`. |
| `builds/build_bitrate_cap.sh` | Parameterized bitrate-cap lift (includes HTTP unlock). `sudo bash builds/build_bitrate_cap.sh <stock.pak> <out.pak> <kbps>`. Recommended `<kbps>`: 20480. |

The fps-dropdown patch was investigated and rejected as non-functional (it
only lies in the dropdown — the encoder clamps to 20 fps at 16MP regardless).
See section 11 for why. No fps patch builder is committed.

### Runtime API scripts (`runtime/`) — no firmware flash needed
| Script | What it does |
|---|---|
| `runtime/set_exposure.sh <mode> [shutter] [gain]` | Set ISP exposure mode via `SetIsp`. Modes: `auto`, `antismear`, `lownoise`, `manual`. |
| `runtime/check_isp.sh` | Print current ISP state (exposure/shutter/gain/etc.). Use to confirm persistence after reboot. |
| `runtime/probe_isp.sh` | Dump full GetIsp action=1 response (state + ranges). Use for discovery. |

### Verification (`verify/`)
| Script | What it checks |
|---|---|
| `verify/dump_encoder_config.sh` | Dump GetEnc action=0 + action=1. Confirms a patch changed the advertised range. |
| `verify/test_setenc.sh <kbps>` | Fire SetEnc at a specific bitrate. Use to find the encoder ceiling via binary search. |
| `verify/get_performance.sh` | Poll GetPerformance to watch live codec bitrate, CPU, net throughput. |
| `verify/fetch_and_analyze.sh [tag]` | Download latest recording via the unlocked HTTP path + ffprobe analysis. **The end-to-end validator** — proves what the encoder actually emits. |

### External dependencies
- **WSL Ubuntu** 24.04 with `squashfs-tools` (`sudo apt install squashfs-tools`).
- `aarch64-linux-gnu-binutils` for objdump/readelf when investigating
  binaries (not needed for the committed builders).
- **ffmpeg/ffprobe** for `verify/fetch_and_analyze.sh`. On Windows, a
  WinGet install works; the script detects WSL paths and translates.
- **Ghidra 11.4 PUBLIC** is only needed if you want to repeat the
  reverse-engineering from scratch. Investigation-only Ghidra Jython
  scripts are NOT committed — they were one-offs. The patch offsets
  and byte recipes in this document are the durable output.

### Stock pakler vs `pak_repack.py`
`vmallet/pakler` produces mismatched CRCs and shifts section payloads
by 4 bytes. Its output is rejected by the Duo 3 verifier. `pak_repack.py`
fixes both issues and produces byte-identical round-trips of stock paks.
Use `pak_repack.py` for production; `pakler` only for archaeology.

---

## 7. How to build a patched pak from scratch

```bash
# 1. Download the stock .pak from Reolink's portal.
# 2. Copy camera.env.example -> camera.env and set CAMERA_IP / USER / PASS.
# 3. Run the relevant builder under WSL as root:

# HTTP download unlock only:
sudo bash builds/build_http_unlock.sh stock.pak out.pak

# HTTP unlock + bitrate cap lift (daily driver):
sudo bash builds/build_bitrate_cap.sh stock.pak out.pak 20480
```

Each builder asserts that the expected stock byte sequence is present
before patching. If Reolink changes the firmware internals, the
assertion fires and no output is written — you'll know to re-investigate
rather than silently producing a bad pak.

The pipeline each builder runs:
1. Extract `app` section from stock pak via section table parsing.
2. `unsquashfs` the app section.
3. Same-length byte-replace(s) in the targeted ELF(s) inside `app`.
4. `mksquashfs -comp xz -b 262144 -no-exports -all-root -mkfs-time 0
   -all-time 0` to produce a deterministic new app squashfs.
5. `pak/pak_repack.py <stock> <out> app <new_app.bin>` — copies the
   stock header byte-for-byte, swaps the app section payload, rewrites
   the section-table entry, recomputes the Reolink CRC, writes the result.

---

## 8. How to flash

1. **Keep the stock `.pak` somewhere safe.** It's the recovery artifact.
2. Open the camera's web UI → Settings → Maintenance → Local Upgrade
   (label varies). Direct connection to a switch you control beats
   doing this through the production network.
3. Select the `*_patched.pak`, confirm.
4. Camera flashes, reboots (~1–3 min), no power interruption.
5. After reboot, log in. The displayed firmware version will still
   show the **stock build number** (e.g. 4867) because we don't modify
   `version.json`. That's correct — it means the camera will accept
   the next iteration's `_patched.pak` (build+1 in filename) as a
   newer update.

To verify the v2 patch worked:
```bash
curl -sI http://<camera>/downloadfile/Mp4Record/<YYYY-MM-DD>/<filename>.mp4
# Expect: HTTP 200, Content-Type: video/mp4
# If you get HTTP 404, the patch didn't take effect.
```

---

## 9. Performance results (Reolink Duo 3 PoE on 100 Mbps PoE switch)

### HTTP download throughput
Measured against a single 472 MB recording (5 min, main stream):

| Path | Time | Throughput | Notes |
|---|---|---|---|
| Stock `cmd=Download` HTTP CGI | — | 0 | Returns empty reply, broken handler |
| `/downloadfile/` direct (stock) | — | — | Returns 404, location is `internal` |
| Patched `/downloadfile/` direct | 44.3 s | **86 Mbps** | Saturates 100 Mbps PoE link |
| Baichuan port 9000 (existing) | 260.8 s | 14.5 Mbps | Real-time stream rate; needs remux |

Soccer-cam's `download_and_mux()` now probes HTTP first per camera, caches
the result, and falls back to Baichuan for cameras still on stock firmware.
Single-line change for callers — `http_port` defaults to 80.

### Encoded bitrate (16MP/h265, build 4879 = 20 Mbps cap)
Measured via ffprobe on real recordings:

| Test clip | Duration | Frames | avg fps | bitrate measured |
|---|---|---|---|---|
| Baseline (stock 4867, 12 Mbps cap) | — | — | 19.92 | up to ~12 Mbps (capped) |
| Patched 4879, mostly-static scene | 107.7 s | 2145 | 19.92 | **14.4 Mbps** |
| Patched 4879, busier scene | 92.9 s | 1851 | 19.92 | **16.15 Mbps** |
| Theoretical encoder ceiling | — | — | 19.92 | 20.5 Mbps (CBR test would confirm) |

VBR is the default — actual bitrate scales with scene complexity. Both
patched clips exceeded the stock 12 Mbps cap, confirming the patch works
end-to-end. Set `bit_ctrl=1` in SetEnc to force CBR if you need a constant
output rate.

### Frame rate
| Path | r_frame_rate | avg_frame_rate |
|---|---|---|
| Any (stock or patched) | 20/1 | 19.92 fps |

Sensor-limited at 20 fps regardless of patch state. The dropdown can be
made to lie (show 25/30) but the encoder ignores the request.

---

## 10. Recovery procedure if a future patch bricks something

What we have ready to go without UART:
1. **Web UI still loads but app daemons are broken** → re-flash stock
   `.pak` via the same upload UI. Tested working — the upload path
   doesn't depend on the app partition.
2. **Web UI dead but camera reachable on Baichuan port 9000** → use the
   Reolink Client desktop app's manual upgrade flow (it talks Baichuan).
3. **Camera unreachable but boots** → factory reset (long-press the
   reset pinhole 10 s while powered). Most Reolinks restore a known-good
   config.
4. **Camera doesn't boot at all** → UART required. Pinout for the Duo 3
   PoE is **not publicly documented**; expect 3 pads near the sensor at
   3.3 V / 115200 8N1. UART hookup procedure is in
   [`UART_HOOKUP.md`](#) (not yet written — would be the first thing
   if a brick ever happens).
5. **u-boot dead** → SOIC8 clip + CH341A external NOR flasher. Take a
   pre-modification SPI dump first if you ever go this route.

We did **not** modify `loader`, `fdt`, `atf`, `uboot`, `kernel`,
`rootfs`, or `ai` — only `app`. So the boot chain through u-boot and
kernel is byte-identical to stock, which keeps the secure-boot eFuse
question moot.

---

## 11. Things still unknown / future work

- Whether the secure-boot eFuse is blown. Empirically we've flashed many
  times without issue, but we'd only find out if u-boot itself was modified.
- The `cmd=Download` CGI bug (`response_type=0xad` not handled). Could be
  fixed in firmware by either (a) changing the byte to `0x52` ('R') in
  the dispatch table — but the Download handler may not populate the
  X-Accel-Redirect URL, so this needs more disassembly; (b) patching the
  response emitter's default case.
- **Frame-rate ceiling (INVESTIGATED, not achievable via flash)** —
  Sensor hardware is **OmniVision OS08C10** (found in
  `rootfs_extracted/lib/modules/5.10.168/hdal/sen_os08c10/nvt_sen_os08c10.ko`),
  which natively supports 4K@60fps. The Duo 3 uses TWO OS08C10's (master
  + slave, `sen_os08c10_slave`). So the 20 fps cap is **NOT a sensor
  limit**. Sensor driver has `sen_chg_fps_os08c10` which writes VTS
  registers 0x380E/0x380F/0x3840 — no hardcoded clamp.
  Router-side patches (v10, v17) successfully lift the API-level cap
  (dropdown shows 25/30) but the encoder silently clamps to 20 fps at
  16MP. No `cmp w_, #0x14` hardcoded cap exists in any app binary
  (cgi/router/device/recorder) NOR in kdrv_h26x.ko / kdrv_videocapture.ko.
  The 20 fps limit is therefore either a **computed pipeline bandwidth
  ceiling** (the dual-OS08C10-stitch-to-7680x2160 pipeline at
  497 Mpix/s seems system-bound) OR a clamp deep in the encoder/capture
  chain that the one-shot kernel-module scan didn't find.
  **Patching further would require rootfs partition modification, which
  is higher risk and requires UART recovery capability.** Not
  recommended without explicit need.
- **Sub-stream improvements** — patches focused on main stream. Sub stream
  bitrate range is `[256, 512, 1024, 1536, 2048]` (max 2 Mbps); separately
  capped, untouched.
- **Network-state-aware recording (Goal #3)** — orthogonal to firmware.
  Could be done off-camera via tiny Pi/script that watches reachability and
  POSTs `/api.cgi` to flip recording modes; or in-camera via a startup
  script in the rootfs partition (`netstate_init.sh` design).
- **Shutter-speed control (Goal #4) — RESOLVED via API, no patch needed.**
  `GetIsp` / `SetIsp` expose full ISP control including shutter and gain.
  See section 13 below for the API contract and soccer-tuning recipes.

---

## 13. Shutter / exposure control (no firmware patch required)

The ISP is controlled via the `GetIsp` / `SetIsp` plaintext JSON API.
No patched firmware needed — stock firmware already exposes the full
control surface. Verified on firmware 4867.

### API contract
```
GetIsp  action=0   -> current ISP state
GetIsp  action=1   -> state + range (= allowed values)
SetIsp  action=0   -> apply new ISP config
```

### Isp struct fields relevant to shutter / motion-blur
| field | current value | range | meaning |
|---|---|---|---|
| `exposure` | `"Auto"` | `["Auto", "LowNoise", "Anti-Smearing", "Manual"]` | AE priority mode |
| `shutter` | `{min:0, max:125}` | `{min:0, max:125}` | shutter table index (higher = faster) |
| `gain` | `{min:1, max:62}` | `{min:1, max:100}` | gain table index (higher = more amplification = brighter but noisier) |
| `antiFlicker` | `"Off"` | `["Other","50HZ","60HZ","Off"]` | powerline flicker compensation |
| `dayNight` | `"Color"` | `["Auto","Color","Black&White"]` | color mode |

**`exposure` modes explained** (standard OmniVision/Novatek ISP naming):
- `"Auto"` — AE balances shutter and gain for brightness (stock default).
- `"LowNoise"` — AE prefers lower gain, allows longer shutter. Best for
  static/low-light scenes; produces more motion blur.
- `"Anti-Smearing"` — AE prefers faster shutter, allows more gain.
  **This is the soccer-action mode** — minimizes motion blur at the
  cost of noise.
- `"Manual"` — user fixes both shutter and gain by setting
  `shutter.min == shutter.max` and `gain.min == gain.max` in the Isp
  struct. (The min/max notation is how the API takes scalars.)

### Soccer recipe (tested & working)
```bash
bash runtime/set_exposure.sh antismear
# -> exposure: Anti-Smearing (AE picks fastest sustainable shutter)
```

This was applied and confirmed via GetIsp round-trip. Actual shutter
speed impact best measured by recording action under known lighting
and comparing motion blur visually.

For full manual override:
```bash
bash runtime/set_exposure.sh manual 100 40
#                                 shutter gain
#         100 -> fast shutter (index)
#         40  -> moderate gain
```

### Persistence (CONFIRMED)
Isp settings are written to the camera's `para` partition and **persist
across reboots natively** — no cron, no patch, no startup script required.
Verified 2026-04-20 by setting `exposure: Anti-Smearing`, rebooting the
camera, and confirming `GetIsp` still reported Anti-Smearing.

Revert with `bash runtime/set_exposure.sh auto`.

### Effectiveness proof (A/B test, 2026-04-20)
Recorded ~60-77 second clips of the same cartoon fight scene under each
exposure mode. Findings:

| metric | Auto | Anti-Smearing | delta |
|---|---|---|---|
| bitrate | 14.51 Mbps | 14.80 Mbps | +2.0% |
| avg P-frame | 80.3 KB | 81.4 KB | +1.4% |
| max P-frame | 263 KB | 282 KB | **+7.0%** |
| peak Laplacian variance (edge sharpness) | 113 | **217** | **+92%** |

The peak edge-sharpness difference is the dispositive result:
Anti-Smearing captures ~2x the high-frequency edge detail during
static-but-detailed frames. A visible motion-blur ghost (double
silhouette of a moving character) is present in the Auto-mode busy
frame; the Anti-Smearing busy frame at similar scene complexity shows
no such artifact.

Scripts that produced this analysis:
- `runtime/set_exposure.sh <mode>` - switch modes
- `verify/fetch_and_analyze.sh <tag>` - download + ffprobe the most recent recording

### What about daytime vs. nighttime?
The Duo 3 auto-switches between `dayNight: Color` and `Black&White` via
the `dayNightThreshold` (currently 50 / 100). Manual exposure mode
applies to BOTH modes — be aware that a shutter index tuned for
daytime may be too dark at night. Consider leaving `exposure: Auto`
for overnight recordings and switching to `Anti-Smearing` for
scheduled game windows via a cron / scheduled API call.

---

## 14. Sensor details

Found via `rootfs_extracted/lib/modules/5.10.168/hdal/sen_os08c10/nvt_sen_os08c10.ko`:

- **Sensor**: OmniVision OS08C10 (8 MP 1/1.8" CMOS)
- **Config**: dual-sensor — master `sen_os08c10` + slave `sen_os08c10_slave`
- **Native capability**: 4K (3840×2160) at up to 60 fps
- **Stitched output**: 7680×2160 (= 16MP, side-by-side composite)
- **Mode-switch function**: `sen_chg_mode_os08c10` (1124 bytes)
- **FPS-change function**: `sen_chg_fps_os08c10` (288 bytes, writes VTS
  registers 0x380E/0x380F/0x3840)
- **Mode table**: `os08c10_mode_1` — 10496-byte register-write sequence
  for the sensor's single supported mode in this firmware

The 20 fps cap on 16MP composite is enforced in layers:
1. **Userspace cap**: a hardcoded `movz w1, #0x14` in `device`
   `Na_video_encoder_build_basic` (`FUN_0048b630`, file offset `0x8bb1c`) —
   patched to 25 in build 4888. See section 3.
2. **UI dropdown cap**: `router` `FUN_00465584` at file offset `0x6565c` —
   patched to 25. Lets the web UI offer 25 fps at 7680×2160.
3. **Encoder ASIC ceiling**: ~330 Mpix/s pixel throughput. Cannot sustain
   25 fps at 16MP; drops ~20% of frames. This is hardware, not firmware —
   not patchable. Confirmed via bitrate-invariance test (see section 3).

---

## 12. References / prior art

- [vmallet/pakler](https://github.com/vmallet/pakler) — base PAK unpacker.
  Has wrong CRC algorithm and section shift for this firmware.
- [gabest11/reolink_firmware_patcher](https://github.com/gabest11/reolink_firmware_patcher) —
  documented the "nginx config is in the device binary" trick. The
  `internal; → autoindex on;` patch idea came from his README.
- [AT0myks/reolink-fw](https://github.com/AT0myks/reolink-fw) and
  [AT0myks/reolink-fw-archive](https://github.com/AT0myks/reolink-fw-archive) —
  higher-level firmware tooling and a tracker of historical pak versions.
- [CVE-2025-60855](https://cybermaya.in/posts/Post-45/) — Reolink Doorbell
  Wi-Fi accepts unsigned repacked firmware; sibling-model evidence that
  the Duo 3 PoE's signature check is similarly cosmetic.
- [thirtythreeforty/neolink](https://github.com/thirtythreeforty/neolink) —
  Baichuan protocol reverse engineering reference.
- [OpenIPC NT9856x branch](https://github.com/OpenIPC/linux/tree/novatek-nt9856x) —
  community work on this SoC family; Duo 3 PoE not yet supported.

---

*Document covers work performed 2026-04-19 against firmware
`IPC_NT15NA416MP.4867_2505072124`. If Reolink ships a new firmware
version, re-run the v2 builder against the new stock pak — it will fail
loudly (assertion mismatch) if the config text changed, rather than
silently producing a bad pak.*
