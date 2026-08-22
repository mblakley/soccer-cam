# Reolink Duo 3 PoE — build log / artifact tracker

Firmware `.pak` files are **not** committed (see `.gitignore`). They live under
`~/Downloads/Reolink_Duo_3_PoE_2505072124/`. This file tracks what each built
artifact contains, its sha256, and where it is, so we always know what to flash
or recover to.

> ⚠️ **The camera validates the `.pak` FILENAME.** Local Upgrade rejects any name not
> matching `IPC_NT15NA416MP.<build>_2505072124.Reolink-Duo-3-PoE.16MP.REOLINK*.pak`
> with **"Failed to recognize the file format"** — regardless of valid CRC/contents.
> The short `soccercam_*.pak` names under `…/BUILT/` are **byte-identical** to the
> correctly-named copies in the parent dir but **will NOT flash**. Always flash the
> `IPC_NT15NA416MP.…REOLINK…` name. (Confirmed on-camera 2026-06-29: same sha256, only
> the name differed — `soccercam_SIMPLE_fixes_4897.pak` rejected,
> `IPC_…4897…_soccercam_v2.pak` accepted.)

## Recovery artifacts (`…/RECOVERY/`)
| File | sha256 | What |
|---|---|---|
| `FACTORY_STOCK_4867.pak` | `1668c9df…` | Untouched stock Reolink firmware — factory recovery, always works. |
| `CURRENT_WORKING_netstate_4896.pak` | `678c1224…` | Reproduces the camera's pre-2026-06-29 live config (v1 netstate daemon + home router MAC [redacted — see netstate README on MAC discovery] + HTTP `/downloadfile/` unlock + 20480 bitrate). Flash to restore "what we had". |

## "Simple fixes" firmware — BUILT, ready to flash (`…/BUILT/`)
| File | sha256 |
|---|---|
| `soccercam_SIMPLE_fixes_4897.pak` (= `IPC_NT15NA416MP.4897_…_soccercam_v2.pak`) | `da14af6e…` |

Contents (layered on stock 4867):
- HTTP `/downloadfile/` unlock (carried)
- Main-stream bitrate cap 20480 kbps (carried)
- **`S99_NetState` v2** — home/away recording **+ home power-on stub cleanup** (deletes the boot-window stub created at home, only files newer than this boot)
- **Free-space reserve 500 MiB → 20 GiB** — one-instruction patch in `libStorageFileManager.so` `Get_storage_space` (`d2a3e800`→`a000c0d2` @ file off `0x44788`). Fixes mid-game **truncation**: the overwrite now keeps 20 GB free so the 8K main stream's 780 MB segment writes never fail.

Build: `builds/build_soccercam_v2.sh` (driven by `/tmp` wrapper that carries creds/MAC from 4896).

## "Stitchcal" firmware — comprehensive + the seam-calibration boot hook

Build: `builds/build_stitchcal.sh`. Everything the comprehensive pak carries,
plus `/usr/bin/lut2d_ioctl` (static aarch64: `get` / `set` / `compose`),
`/etc/init.d/S98_StitchCal`, and `/etc/init.d/S36_RootShell`.

The point of baking it once is that **a calibration is then a file drop, not a
reflash**: `/mnt/sda/stitchcal/anchors.txt` is a few hundred bytes and the hook
composes it onto whatever factory mesh the firmware generated that boot. With no
`anchors.txt` the hook exits 0 and the mesh stays factory, so the pak is a safe
no-op on an uncalibrated unit.

Two gates run inside the builder and both refuse rather than warn: the boot chain
(`loader`, `fdt`, `atf`, `uboot`, `kernel`) must be byte-identical to stock —
enforced inside `pak_repack.repack()` before assembly and long before the CRC is
computed — and `verify/check_recording_default.sh` must pass.

| File | Notes |
|---|---|
| **`IPC_…4921…_stitchcal.pak`** (`7514d015…`, CRC `0x317cfb07`) | **CURRENT.** 4920 + **`S99_NetState` v3: periodic segment rollover.** The camera finalises an MP4 only when recording *stops* — it pre-allocates the `moov` at the front of the file and fills it on close — so an abrupt power cut loses the **entire session**, not a truncated tail. Observed 2026-08-22: a ~435 MB main stream was **absent from the card**, `df` confirming the space was never consumed; only the sub-stream survived. v3 therefore toggles recording off→`sync`→on every `ROLLOVER_SECS` **while away**, bounding the loss to one interval. Default **60 s**, retunable with a file drop at `/mnt/sda/netstate/rollover_secs` (0 disables, 15 s floor). Also **releases API sessions** (`Logout`) — see below. Builder gates the three invariants (logout present, `do_rollover` away-guarded, `sync` inside it). |
| `IPC_…4920…_stitchcal.pak` | superseded — 4919 + **idempotent hook.** Re-running S98 within one boot composed the correction onto its *own output* and silently doubled the shear (3 px → 6 px) while the log read healthy. The hook now records a signature of what it wrote and, on recognising it, composes from the saved baseline instead. Verified on camera: three consecutive runs all produce mesh crc `0bf0934f`. |
| `IPC_…4919…_stitchcal.pak` | superseded — first build whose hook actually ran. 4918 + **wait for `/mnt/sda` before reading config** + `S36_RootShell` restored. **On-camera validated:** S98 applied a ±3 px calibration 38 s into boot, read-back ok, and the correction was visible in a snapshot. |
| `IPC_…4918…_stitchcal.pak` | superseded, and instructive. Gates passed, flashed clean — and the hook **did nothing, silently**. `rcS` does `mount -a` then runs `S*`, but `/mnt/sda` is `/dev/hd/sda1`, mounted later by the app; S98 checked for `anchors.txt` first, found none, and could not even log it because `/` is read-only squashfs. It also dropped `S36_RootShell` (the comprehensive builder never installed one), taking tcp/2323 with it. |

Known gap: `commit=` in the manifest reads `unknown` unless `SOCCERCAM_COMMIT` is
set, because `git rev-parse` refuses a `/mnt/c` worktree from WSL (dubious
ownership). Pass it explicitly.

### Segment rollover (v3) — measured, not assumed

Measured on the camera at build 4920 before the v3 build, driving `SetRecV20`
over HTTP with the exact call shape the daemon uses (login → set → logout):

| What | Measured |
|---|---|
| Natural rollover without intervention | **none** — one file grew for 3+ min hands-off |
| Toggle cost, off→`sync`→on, no settle delay | **0.62 s** end to end |
| Frames lost per rollover | **zero** |
| Content *duplicated* per rollover | **≈0.50 s (~10 frames @ 20 fps)** |

Over 10 consecutive rollovers, segment **start** times (independent camera-clock
stamps) advanced 208 s while 212.98 s of content was written — a 4.98 s surplus,
i.e. the camera's pre-record buffer **backfills** the toggle rather than dropping
frames. All 11 segments probed clean: 7680×2160 HEVC, real `moov`, playable.

Two consequences drove the design:

- **60 s is cheap.** A rollover costs duplicated frames, not lost ones, so a
  tight loss bound is nearly free (~0.8 % duplicated content at 60 s). For match
  footage the cost is ~90 half-second hitches over 90 minutes; raise
  `rollover_secs` to 300 if that reads badly, or teach the downstream concat to
  trim the overlap (the duplicate frames are present, so it is recoverable).
- **Sessions must be released.** The camera caps concurrent API sessions at
  **5** (`"max session"`, rspCode −5) with a 3600 s lease. v2 logged in per call
  and never logged out — harmless at one call per home/away transition, fatal at
  one rollover a minute: the pool empties within minutes, `SetRecV20` starts
  failing `"please login first"`, and **recording stops silently**. Verified: 5
  leaked logins exhaust the pool; with `Logout`, 12/12 succeed. This was found by
  measurement, not review, and it would have made the whole feature a no-op.

Caveat not closed at home: the overlap was measured on a static indoor scene
(~1.2 Mbps actual, against the 20480 kbps cap). If the pre-record buffer is
size-bounded it may cover less at true field bitrate, in which case the rollover
degrades from ~0.5 s of overlap toward a gap of at most the 0.62 s toggle time.
Bounded and sub-second either way, but it is inference, not measurement.

## "Comprehensive" firmware — ready to flash
| File | sha256 | Notes |
|---|---|---|
| **`IPC_NT15NA416MP.4906_…REOLINK_soccercam_comprehensive.pak`** | `de5490e6…` | **CURRENT / recommended.** 4904 **+ netstate home-stub cleanup fix.** `clean_home_stubs` built its `Mp4Record/<date>` path from the shell's `date` (UTC) while the camera names those dirs in **local** time, so after ~20:00 local it scanned the wrong (next-UTC-day) dir and never deleted the home boot stub. Now scans **every** date subdir and deletes only files with **mtime ≥ this boot's epoch** (timezone-independent), catches **sub-stream `RecS09`** stubs the old two-dir scan missed, and logs each `rm` + a re-`stat` confirming the file is gone. **On-camera validated 2026-06-30:** a boot stub (main+sub) was found (`candidates=2`) and deleted with `removed -- gone after rm`; 665 pre-boot recordings untouched. commit `6830e54`. |
| `IPC_…4904…` | `9453eb6a…` | superseded — crash-safe recovery commit (placeholder moov tag flipped to "moov" last) + `box_in` 64-bit fix are good and carried forward, but `clean_home_stubs` missed the home boot stub (UTC vs local date dir — fixed in 4906). commit `382eb7d`. |
| `IPC_…4903…` | `6bfc9555…` | superseded — first build with the mmap recovery (camera has ~240 MB RAM / ~790 MB segments). **On-camera validated:** a real 790 MB orphan recovers cleanly (5993 video + 4691 audio, valid moov). commit `4ec2e28`. |
| `IPC_…4901…` | `acf0ce7e…` | superseded — netstate/manifest fixes are good, but `recover_mp4` was still load-based (OOMs on a full ~790 MB orphan; only recovers the small ~15–20 MB clips). CRC `0x1354234d`. |
| `IPC_…4905…_debug` | (throwaway, sha `e280bf8b…`) | **debug build, not for daily use** — 4904 + the netstate cleanup fix + a passwordless root shell on tcp/9999 (`S26_DebugShell` + `/usr/bin/debug_shell`). Used to validate the cleanup fix live. **Gotcha:** the bind shell execs `sh -i`, which is non-functional over a raw socket (no tty → the session drops after the banner); verification was actually done by reading `/downloadfile/netstate/log` over HTTP (the daemon's own before/after re-stat) + driving recording via the JSON API, not the shell. A future debug shell should exec `sh` (non-interactive) instead. Reflash 4906 to remove. |
| `IPC_…4902…_debug` | (throwaway) | **debug build, not for daily use** — 4901 + a passwordless root shell on tcp/9999 (`S26_DebugShell` + `/usr/bin/debug_shell`), used to RE the camera's RAM and validate the mmap fix on-device. Reflash 4903 to remove. |
| `IPC_…4900…` | `9f245cf8…` | superseded. **Flash-verified on camera 2026-06-29** (netstate v2 + fail-enabled fixes confirmed working), but the manifest surfaced too early at boot so `build.txt` didn't appear — fixed in 4901. CRC `0x086a57e2`. |
| `IPC_…4899…` (= `BUILT/soccercam_COMPREHENSIVE_4899.pak`) | `52ed6ac5…` | prior — audio recovery; pre-manifest, `INIT_GRACE=45`, old (latching) netstate loop. CRC `0x70ef529f`. |
| `IPC_…4898…` (= `BUILT/soccercam_COMPREHENSIVE_4898.pak`) | `6e96223d…` | video-only recovery @ fixed 20 fps. **Flash-verified on the camera 2026-06-29.** |

Everything in the simple-fixes pak **plus** boot-time power-cut recovery:
- **`/usr/bin/recover_mp4`** — static aarch64 reindexer (no runtime deps). Conservative NAL-chaining walk rebuilds the **video** track from a moov-less mdat (audio bytes can't be mislabeled as video), copies hvc1/hvcC + box templates from a reference good recording on the card, appends a valid moov in place under the original `RecM09…` name. Loses ≤1 GOP (~2 s).
  - **AUDIO (4899+):** the camera interleaves strict V-A-V-A… with each audio chunk a run of raw AAC-LC frames starting exactly at the preceding video chunk's end. The same walk that skips audio to resync video yields each audio chunk's byte range; **Helix AAC** (`AACDecode`, bytes-consumed == frame size) splits them into a real `soun` trak. A plausible-size + outputSamps + stream-param filter rejects the unreferenced padding hole that trails each chunk. Static-linked `libhelixaac.a` (Helix is RPSL, fetched locally — see `recover/helix/README.md`); absent → builder falls back to `-DNO_AUDIO` video-only.
  - **Timing (4899+):** video frame spacing is derived from the **audio clock** (audio duration / N video frames) instead of a fixed FPS — the recorded rate varies with exposure (this test clip is 12.5 fps, not the configured 20), so this matches the real wall-clock duration and keeps A/V in sync. Falls back to 20 fps only when no audio was recovered.
  - **Truncation fix (4899+):** rewrites the `mdat` box size to the real (power-cut-shortened) content length + `ftruncate`s before appending moov, so players don't hunt for moov past the lost data ("moov atom not found").
  - **Validated end-to-end** (actual aarch64 binary extracted from the 4899 pak, run via qemu, decoded with ffmpeg):
    - *Clean orphan* → 432 video / 35 kf + 539 audio frames; **byte-perfect** vs reference (0/432 video, 0/539 audio size mismatches); recovered **audio PCM is bit-identical** to the reference (md5 `b0a31583…`); 0 decode errors; 34.49 s @ 12.5 fps, A/V synced.
    - *Power-cut orphan* (1.3 MB tail chopped) → 412 video / 510 audio, 0 decode errors, video 32.64 s == audio 32.64 s (best-effort: drops the partial trailing frames).
- **`/etc/init.d/S35_RecRecover`** — at boot (before the camera's scan), finds a reference, recovers each orphan in place, renames the end-time to the recovered duration so the camera's duration check passes, re-indexes as a normal `/downloadfile/` video. **Never deletes** a recoverable file; leaves unparseable ones in place.
- **`/etc/soccercam_build`** (4900+) — build manifest (variant, options, git commit), copied to `/mnt/sda/soccercam/build.txt` at boot by `S99_NetState`. Identify which firmware a camera runs over the HTTP unlock: `curl http://<cam>/downloadfile/soccercam/build.txt` (every pak otherwise reports the same stock `v3.0.0.4867` version string).

Build: `builds/build_soccercam_comprehensive.sh` (source `recover/recover_mp4.c` + `recover/helix/`, `runtime/recover/S35_RecRecover`). 4900 verified pak contents: recover_mp4 (aarch64, +x, Helix-linked 847 KB), S35_RecRecover, S99_NetState v2 (`INIT_GRACE=5` + fail-enabled retry), `/etc/soccercam_build` manifest, http unlock, bitrate 20480, reserve 20 GiB; rootfs 5.75 MB < 8 MB partition; CRC `0x1354234d` verified (4901).

Known limits: main stream (`RecM09`, H.265) only; `RecS09` sub-stream recovery is a follow-on. Audio is best-effort — a corrupt audio chunk drops that chunk's sound (video unaffected).
