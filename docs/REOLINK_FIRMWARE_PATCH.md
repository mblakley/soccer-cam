# Reolink Duo 3 PoE — Firmware Patch Notes

End-to-end record of reverse-engineering the firmware container, identifying
the broken HTTP download path, and shipping two working patches for the
Reolink Duo 3 PoE running stock firmware **v3.0.0.4867_2505072124** (May 2025
build, board `IPC_NT15NA416MP`). Goal was to get faster recording downloads
into the soccer-cam pipeline.

Result: HTTP downloads went from broken (or 1 Mbps if you counted the
throttle) → 86 Mbps (saturating the camera's PoE Ethernet). Baichuan
downloads, the previous workaround, run at ~14 Mbps.

Camera was never bricked. Two flashes total, both via the web UI's
manual upgrade button. No UART required.

> Build artifacts (the patcher scripts, the stock pak, and the patched
> paks) are **not** in this repo — they live in
> `C:\Users\markb\Downloads\Reolink_Duo_3_PoE_2505072124\`. This doc
> describes the format, algorithm, and procedure precisely enough to
> recreate them against any new stock pak. The scripts referenced in
> Section 6 are also in that working directory.

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

- Direct GET from `/downloadfile/<relative-path>` returns the file at
  ~85 Mbps with no auth, no CGI overhead.
- **No authentication on this path.** Anyone on your LAN who can reach
  the camera and guess a recording filename can download it. The
  filenames are obscure enough to be hard to guess but it's not a
  security boundary. Don't expose the camera to untrusted networks.
- `limit_conn one 1` is still in place — one concurrent download per
  source IP. Sequential downloads work fine; parallel from the same
  client will queue.
- Touches `/downloadfile/` only. `/playback/` still has `internal;`,
  so RTMP playback flow is unchanged.

---

## 6. Toolchain

All in this directory. Run from WSL Ubuntu when the script needs Linux
utilities (`unsquashfs`, `mksquashfs`).

| File | Purpose |
|---|---|
| `pak.py` | Low-level parse / extract / inspect. Standalone CLI: `python pak.py <pak>` shows section table and probes the checksum field. |
| `reolink_crc.py` | Standalone CRC compute/patch. `python reolink_crc.py compute <pak>` or `... patch <pak>` to recompute and rewrite the CRC field in place. |
| `pak_repack.py` | Clean PAK repacker. Preserves the exact byte layout the camera's verifier expects (first section at `0x8c8`, full 15-entry section table, correct CRC). No-op rebuild produces a byte-identical file. |
| `extract.py` | One-shot extractor that splits the .pak into `sections/<idx>_<name>.bin` files. |
| `build_nginx_patch.sh` | v1 build pipeline (limit_rate only). |
| `build_nginx_patch_v2.sh` | **v2 build pipeline (the one that actually works).** Apply this on a stock pak; produces a flashable `.pak` with build# +2. |
| `verify_patch.sh` | Re-extracts a patched pak and confirms the device binary contains the patched throttle/internal text. |
| `roundtrip.sh` | Validates the toolchain by extracting and repacking the stock pak through pakler and diffing. |
| `bench_http.sh` / `http_diag*.sh` / `test_dl*.sh` / `probe_ports.sh` | Diagnostic scripts used during the investigation. |

External dependencies:
- `vmallet/pakler` — used for the no-op round-trip test only. **Not used
  for production builds** because its CRC and section-shift behavior are
  wrong for this firmware. `pak_repack.py` replaces it.
- WSL Ubuntu 24.04 with `squashfs-tools` and `binutils-aarch64-linux-gnu`.

---

## 7. How to build a patched pak from scratch

For a future firmware version, or to apply the patch to a sibling
Reolink model, the procedure is:

```bash
# 1. Download the new stock .pak from Reolink's portal and save as
#    e.g. IPC_<board>.<build>_<date>.<model>.pak

# 2. Run the v2 builder (in WSL as root)
sudo bash build_nginx_patch_v2.sh

# 3. The script will assert that the original 146-byte
#    /downloadfile/ { ... internal; ... limit_rate 1024k; ... } block
#    appears EXACTLY 2 times in the device binary. If Reolink changed
#    the config text, the assertion fires and nothing is written.

# 4. If the assertion holds, you'll get
#    IPC_<board>.<build+1>_<date>.<model>_patched.pak
#    Bump the build number in the filename so the camera treats it as
#    a newer update than what it's running.
```

The pipeline:
1. Extract `app` section from stock pak via section table parsing.
2. `unsquashfs` the app section.
3. Same-length byte-replace the `/downloadfile/` block in `device`.
4. `mksquashfs -comp xz -b 262144 -no-exports -all-root -mkfs-time 0
   -all-time 0` to produce a deterministic new app squashfs.
5. `pak_repack.py <stock> <out> app <new_app.bin>` — copies the stock
   header byte-for-byte, swaps the app section payload, rewrites the
   section-table entry, recomputes the Reolink CRC, writes the result.

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

- Whether the secure-boot eFuse is blown. Empirically we've flashed twice
  without issue, but we'd only find out if u-boot itself was modified.
- The `cmd=Download` CGI bug (`response_type=0xad` not handled). Could be
  fixed in firmware by either (a) changing the byte to `0x52` ('R') in
  the dispatch table — but the Download handler may not populate the
  X-Accel-Redirect URL, so this needs more disassembly; (b) patching the
  response emitter's default case.
- Goal #1 (raise bitrate / framerate) — the kernel module hard-caps at
  128 Mbps; the app-side `device` binary likely has the per-resolution
  bitrate table that the web UI reads from. Untouched ground per prior
  art research; would require disassembling the encoder config code.
- Goal #3 (network-state-aware recording) — orthogonal to firmware.
  Cleanest path is to do it off-camera via a tiny Pi/script that watches
  its own internet reachability and POSTs `/api.cgi` to flip recording
  modes.
- Shutter-speed control investigation — open task; not yet started.

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
