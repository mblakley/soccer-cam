# Reolink Duo 3 PoE — brick postmortem (build 4917)

Forensic analysis of the camera that stopped responding after build **4917**
(`IPC_NT15NA416MP.4917_2505072124..._ioctltrace3.pak`) was flashed at
2026-08-16 03:13 UTC (23:13 EDT 2026-08-15). Written 2026-08-16 from the pak
images, the shipped binaries, the packet captures and the session transcript.
The camera was disassembled on a bench at the time of writing; **nothing here
was tested against hardware.** Every claim cites a file and an offset, line or
timestamp. Anything not directly verified is labelled.

Evidence roots used throughout:

| tag | path |
|---|---|
| `DL/` | `C:\Users\markb\Downloads\Reolink_Duo_3_PoE_2505072124\` |
| `SC/` | `C:\Users\markb\AppData\Local\Temp\claude\C--Users-markb-projects\17be6f51-3a7c-4ed7-854d-d0d6f082b2e1\scratchpad\duo3\` |
| `TX` | `C:\Users\markb\.claude\projects\C--Users-markb-projects\17be6f51-3a7c-4ed7-854d-d0d6f082b2e1.jsonl` (cited as `TX:<line> @<ts>`, ts = UTC) |

---

## 0. Verdict

**The recorded hypothesis is half right and half wrong, and the recorded
counter-argument that was used to doubt it is factually wrong.**

| claim as recorded (`SC/RECOVERY_README.md`) | finding |
|---|---|
| "4917 added pointer-following to the shim" | **False.** Pointer-following shipped in **4916**, which booted and ran normally. 4917 changed one thing: the pointer-*plausibility filter*. |
| "the shim validated 32 bytes and read 96 — that bug faulted `device`" | **The bug is real and is in the shipped 4917 binary — but it is equally present in 4916.** It is not the delta. It was *unreachable* in 4916 and became reachable in 4917. |
| "a crashing `device` would still leave the network up, so an interrupted flash write is at least as likely" | **Wrong.** `device` is the **only** binary in the firmware that assigns the camera's IP address. Boot brings `eth0` up on the hard-coded fallback **192.168.0.3**; `device` is what moves it to 192.168.86.200. A dead `device` produces exactly the observed symptom. |
| "150 s of 1-second polling found no boot window at all" | The poll only ever targeted **192.168.86.200**. `192.168.0.3` was **never probed** — zero occurrences of `192.168.0.` anywhere in `TX`. |
| "810 frames captured … promiscuously" | **The captures were non-promiscuous** (`tshark -p`). They cannot see the camera's IPv6 DAD/MLD/RS multicast, which is the only traffic a silent, statically-addressed camera would emit. |

**Verdict:** the 4917 `ioctllog.so` newly dereferenced ~32 000 pointers per boot
that 4916 never touched, `device` died during media bring-up, and because
`device` owns network configuration the camera has been sitting on
**192.168.0.3** — reachable, with `telnetd` and a root shell on `tcpsvd`
port 2323 — while every probe went to 192.168.86.200.

**Confidence:**

| proposition | confidence | basis |
|---|---|---|
| 4916→4917 differ in exactly one file, `/lib/ioctllog.so` | **certain** | §1, byte diff of the whole container |
| Boot chain (`loader/fdt/atf/uboot/kernel`) byte-identical across 4867/4914/4915/4916/4917 | **certain** | §2, SHA-256 |
| The flash uploaded completely and the camera accepted it | **certain** | §4, transcript + `flash_pak.py` control flow |
| The sole semantic change in 4917 is the pointer filter widening | **certain** | §3, instruction-level diff |
| That change made ~32 000 dereferences/boot live, from zero | **high** | §3.4, measured against 4916's own trace |
| `device` died as a result | **high** | §3.5 |
| The camera is alive on 192.168.0.3 (or was, before disassembly) | **medium-high** | §5, §6 — consistent with all evidence, not yet proven |
| NAND corruption / interrupted write | **low** — not excluded, but no evidence for it | §7.1 |
| NAND wear, SD-card exhaustion, `netstate`, `app` corruption, power | **excluded** | §7 |

The one measurement that separates the two surviving hypotheses is in §9 and §10:
put a host on `192.168.0.0/24` and probe `192.168.0.3`, or watch the UART.

---

## 1. What actually differed between 4916 and 4917

### 1.1 Container level

Parsed with `pak/pak.py`; CRC checked with `pak/reolink_crc.py`.

| section | 4867 | 4914 | 4915 | 4916 | 4917 |
|---|---|---|---|---|---|
| `loader` | `c30c64` | `c30c64` | `c30c64` | `c30c64` | `c30c64` |
| `fdt` | `26da0d` | `26da0d` | `26da0d` | `26da0d` | `26da0d` |
| `atf` | `bafa25` | `bafa25` | `bafa25` | `bafa25` | `bafa25` |
| `uboot` | `ae0eb1` | `ae0eb1` | `ae0eb1` | `ae0eb1` | `ae0eb1` |
| `kernel` | `907180` | `907180` | `907180` | `907180` | `907180` |
| `rootfs` | `a03994` | `5b3018` | `6fccd8` | `c0946f` | **`dd5504`** |
| `ai` | `aa4a1f` | `aa4a1f` | `aa4a1f` | `aa4a1f` | `aa4a1f` |
| `app` | `e74d10` | `32e373` | `dd1810` | `dd1810` | `dd1810` |

(SHA-256, first 6 hex.) 4915, 4916 and 4917 all have `rootfs` at
`off=0x003c26b8 size=0x0057d000` (5 754 880 B) and `app` at
`off=0x00f21ee0 size=0x00984000`. Section **offsets and sizes are identical**
between 4916 and 4917 — the rootfs image is the same length, only its contents
differ.

The header region (`0x000`–`0x8c8`: magic, CRC, 15-entry section table,
13-entry partition table) is **byte-identical between 4916 and 4917 except for
four bytes**:

```
0x0008: 4916 = d1 d7 29 19      (CRC 0x1929d7d1)
0x0008: 4917 = 69 b0 97 d4      (CRC 0xd497b069)
```

That is the Reolink CRC field and nothing else. The partition table — including
`rootfs → /dev/mtd5, flash offset 0x00580000, size 0x00800000` — is unchanged.

CRC verification, `reolink_crc.compute()` vs the stored u64 at `0x08`:

```
4867: stored=0x23b254c5 computed=0x23b254c5 MATCH
4914: stored=0xb5102d94 computed=0xb5102d94 MATCH
4915: stored=0x09b6cb2a computed=0x09b6cb2a MATCH
4916: stored=0x1929d7d1 computed=0x1929d7d1 MATCH
4917: stored=0xd497b069 computed=0xd497b069 MATCH
```

**Every pak on disk, including 4917, carries a valid Reolink CRC.** The
on-disk artefact was well formed; the camera's own CRC gate had nothing to
reject.

### 1.2 Inside `rootfs`

Both images are SquashFS 4.0, xz, 256 KB block. Extracted with
`unsquashfs 4.6.1`; **4917's image extracted cleanly** — 256 files, 145
directories, 345 symlinks, no errors — so the built image is not corrupt.

Full filename + per-file MD5 diff:

```
4914 -> 4915   ./lib/ioctllog.so                                     ADDED
               ./etc/init.d/start_app                                changed
               ./etc/soccercam_build                                 changed
               ./lib/modules/5.10.168/hdal/sen_os08c10/nvt_sen_os08c10.ko              changed
               ./lib/modules/5.10.168/hdal/sen_os08c10_slave/nvt_sen_os08c10_slave.ko  changed
4915 -> 4916   ./lib/ioctllog.so                                     changed
               ./etc/soccercam_build                                 changed
4916 -> 4917   ./lib/ioctllog.so                                     changed     <-- the only difference
```

**Between 4916 and 4917 exactly one file in the entire 25.8 MB pak changed:
`/lib/ioctllog.so`.** Not `start_app`, not `app`, not the boot chain, not even
the build stamp.

| build | `/lib/ioctllog.so` size | MD5 |
|---|---|---|
| 4915 | 67 560 | `42d85db21196835eed3da32e3497c07e` |
| 4916 | 67 576 | `01b6b5f12a386beceeeb22ee4b5ec269` |
| 4917 | 67 576 | `9d0a831c1ac3762b3648d858dfc855df` |

The injection point is `/etc/init.d/start_app` line 109, **identical in 4915,
4916 and 4917**:

```sh
./router &
LD_PRELOAD=/lib/ioctllog.so ./device &
```

### 1.3 Two unintended changes found (neither is the cause)

1. **The 4915 build silently reverted 4914's sensor patches.** Every build in
   the shim series was rebased from `4907_stitchprobe.pak`, so
   `nvt_sen_os08c10.ko` and `nvt_sen_os08c10_slave.ko` went back to their stock
   4867 hashes (`3f05b8…` / `0cf8ce…`), and `app` reverted from 4914's
   `32e373…` to 4907's `dd1810…` (`device` and `router` both differ). The
   4914 720p60 experiment was therefore not in the tree for 4915–4917. This is
   visible in the build stamps: 4914 ends `stream=7680x720 fps=60 EXPERIMENTAL2`,
   4915 ends `ioctl_trace=yes`. Consistent with intent, but it was never stated.

2. **4917 shipped with 4916's build stamp.** `/etc/soccercam_build` is
   byte-identical in 4916 and 4917 (`dece308cea7e97e4360391433db057aa`). The
   4917 build command (`TX:2165 @03:04:41.168Z`) contains no `sed` of the stamp
   and, critically, **no compiler invocation** — it only does
   `install -m 0755 /var/tmp/ioctllog.so rfs/lib/ioctllog.so`. A running 4917
   would have identified itself as 4916 at
   `/downloadfile/soccercam/build.txt`. That is a provenance hole in the build
   process (§11).

---

## 2. Is the boot chain intact? — Yes, verified independently

This is load-bearing for the UART recovery plan, so it was re-verified from the
pak bytes rather than trusting the build-time assertion.

`loader`, `fdt`, `atf`, `uboot`, `kernel` and `ai` are **byte-identical
(SHA-256) across all five paks**, factory 4867 included (table in §1.1). Sizes
and offsets are also identical: `loader` 0x8c8/0x10000, `fdt`
0x108c8/0x21c70, `atf` 0x32538/0xa088, `uboot` 0x3c5c0/0x803c8, `kernel`
0xbc988/0x305d30. The camera's flash partition map in the header is unchanged.

Decoded partition table (`0x450`…, 76-byte entries), for the UART work:

| section | node | flash offset | flash size | payload used |
|---|---|---|---|---|
| loader | mtd0 | 0x00000000 | 0x00040000 | 0x10000 |
| fdt | mtd1 | 0x00040000 | 0x00040000 | 0x21c70 |
| atf | mtd2 | 0x00080000 | 0x00040000 | 0xa088 |
| uboot | mtd3 | 0x000c0000 | 0x00100000 | 0x803c8 |
| kernel | mtd4 | 0x001c0000 | 0x003c0000 | 0x305d30 |
| **rootfs** | **mtd5** | **0x00580000** | **0x00800000** | **0x57d000 (68.6 %)** |
| ai | mtd6 | 0x00d80000 | 0x00700000 | 0x5e2828 |
| app | mtd7 | 0x01480000 | 0x00b00000 | 0x984000 |
| para | mtd8 | 0x01f80000 | 0x00200000 | yaffs2, mounted at `/mnt/para` |
| sp | mtd9 | 0x02180000 | 0x00080000 | factory calibration |
| ext_para | mtd10 | 0x02200000 | 0x00080000 | |
| stitch | mtd11 | 0x02280000 | 0x00100000 | |
| download | mtd12 | 0x02380000 | 0x01c80000 | |

Total 0x04000000 = 64 MB SPI NAND.

**Conclusion: u-boot was never modified. UART recovery via u-boot remains
valid.** From the FDT (`/chosen`, decoded from `fdt.bin`):

```
bootargs    = 'earlycon rootfstype=squashfs ro init=/linuxrc '
stdout-path = 'serial0:115200n8'
```

`earlycon` is on, so console output starts at the very first kernel prints. Six
`ns16550a` UARTs are declared at `0x2f0110000`–`0x2f0115000`; `serial0` is the
first. 115200 8N1 as already assumed.

---

## 3. What the shipped 4917 shim actually did

`/lib/ioctllog.so` was extracted from each pak's rootfs and disassembled
(`aarch64-linux-gnu-objdump`). The `.c` in `SC/ioctllog.c` is the **post-fix**
source and does not match anything that shipped; the binaries are authoritative.

### 3.1 Which build gained what

| | 4915 | 4916 | 4917 |
|---|---|---|---|
| `ioctl` function size | 1224 B | 1600 B | 1608 B |
| `.rodata` | 0x12e | 0x156 | 0x156 (**identical bytes**, shifted +8) |
| imports `open`, `write` | no | **yes** | yes |
| string `/dev/null` | no | **yes** | yes |
| string ` @+%u->[` | no | **yes** | yes |
| `MAX_LINES` | 400 000 | 40 000 | 40 000 |
| `DUMP_BYTES` | 64 | 320 | 320 |

**Pointer-following, the `/dev/null` write-probe and the 96-byte read all
shipped in 4916.** 4916 booted, ran, and produced a full trace. The premise
that 4917 introduced them is wrong.

`MAX_LINES` is confirmed empirically, not inferred: `SC/dumps/trace2.txt` is the
accumulated log (`fopen(…, "a")` on the persistent SD card) and contains exactly
two session banners —

```
400000 lines  ===== ioctllog pid=755 (early_buffered=7182 bytes, overflow=0) =====   <- 4915
 40000 lines  ===== ioctllog pid=756 (early_buffered=7580 bytes, overflow=0) =====   <- 4916
```

— both exactly at their caps.

### 3.2 The one semantic change, at instruction level

Everything else in the `.text` diff is a ±8-byte address shift caused by this
code growing by 8 bytes.

`ioctllog_4916.so` @ `0x1044`:

```
1038: ldr   x26, [x21]              ; v = *(u64*)(arg + i)
103c: cmp   x26, #0xfff
1040: b.ls  1024                    ; v < 0x1000 -> skip
1044: lsr   x0, x26, #40            ; (v >> 40)
1048: cmp   x0, #0x7f
104c: b.ne  1024                    ; keep only (v>>40) == 0x7f
```

`ioctllog_4917.so` @ `0x1044`:

```
1038: ldr   x26, [x21]
103c: cmp   x26, #0xfff
1040: b.ls  1024
1044: lsr   x0, x26, #32            ; (v >> 32)
1048: mov   x1, #0xffbf
104c: sub   x0, x0, #0x40
1050: cmp   x0, x1
1054: b.hi  1024                    ; keep 0x40 <= (v>>32) <= 0xffff
```

This matches the transcript exactly. `TX:2160 @03:04:09.641Z` is a single
`sed` on the source:

```
if (v < 0x1000ULL || (v >> 40) != 0x7fULL) continue;
->
if (v < 0x1000ULL || (v >> 32) < 0x40ULL || (v >> 32) > 0xffffULL) continue;
```

followed by `aarch64-linux-gnu-gcc … && strip`, result
`rebuilt 67576 bytes` (`TX:2161 @03:04:19.213Z`). **There is no other edit to
`ioctllog.c` between the 4916 build (`TX:2138 @02:59:05Z`) and the 4917 build
(`TX:2165 @03:04:41Z`)**, and the 4917 build command does not recompile — it
installs whatever `/var/tmp/ioctllog.so` contained.

The change was itself a correct bug fix. Userspace addresses on this kernel are
39-bit (`0x7f_xxxxxxxx`), so `(v >> 40) == 0x7f` can never be true; the session
diagnosed this correctly (`TX:2159 @03:04:00Z`). The replacement is simply far
too permissive: it accepts any u64 whose high word is anywhere in
`[0x40, 0xffff]`, a range 65 472× wider than the one real value (`0x7f`).

### 3.3 The validate-32/read-96 bug — present in both builds

Identical in `ioctllog_4916.so` and `ioctllog_4917.so`. From 4917:

```
1064: mov   x1, x26
1068: mov   x2, #0x20               ; n = 32
106c: bl    a00 <write@plt>         ; write(devnull_fd, v, 32)
1070: cmp   x0, #0x20
1074: b.ne  1024                    ; probe must return exactly 32
...
10d0: ldr   x4, [sp, #48]
10d8: ldr   w5, [x26], #4           ; *q++            <-- the actual read
10f0: add   w28, w28, #0x1
10f8: bl    990 <__snprintf_chk@plt>
1100: cmp   w28, #0x17              ; loop while counter <= 23
1110: b.le  10d0                    ; => 24 iterations = 96 bytes
```

**32 bytes validated, 96 bytes read.** Real, shipped in 4917, and equally
shipped in 4916 — where it was dead code.

### 3.4 The blast radius, measured against 4916's own trace

4916's session (`trace2.txt`, banner 2, pid 756, 40 000 lines) is *exactly* the
workload 4917 replayed: same `MAX_LINES`, same `DUMP_BYTES`, same filter list,
same boot. Replaying 4917's filter over those 40 000 lines:

| | 4916 (shipped) | 4917 (shipped) |
|---|---|---|
| `@+` pointer-follow records actually written | **0** (grep of `trace.txt` and `trace2.txt`) | — |
| u64 slots that pass the filter | **0** | **32 379** |
| …of which real `0x7f_xxxxxxxx` user pointers | 0 | **32 081** (611 distinct) |
| …of which junk (write-probe returns `EFAULT`) | 0 | 298 |
| `open("/dev/null")` calls | **0** | 1 (first at trace line 3) |
| `write()` syscalls added | **0** | 32 379 |
| 96-byte reads performed | **0** | 32 081 |
| distinct target addresses where a 32-byte probe succeeds but a 96-byte read crosses the page end | 0 | **9** |

Zero pointer-follow records in either shipped trace is direct proof that 4916's
filter never fired: `grep -c '@+' trace.txt` = 0, `grep -c '@+' trace2.txt` = 0.
The `/dev/null` fd was never even opened under 4916.

The nine page-crossing addresses (page offset in the fatal band
`4000 < off ≤ 4064`, so `[v, v+32)` is in-page but `[v, v+96)` is not):

```
0x7f8450cfd0  0x7f8455dfc8  0x7f84562fa8  0x7f84564fb0  0x7f84565fc8
0x7f9c126fe0  0x7f9c131fa8  0x7f9c13bfd0  0x7f9c141fa8
```

Source devices for the 32 379 candidates: `/dev/nvtmpp` 18 277,
`/dev/isf_flow0` 7 978, `/dev/kflow_ai_net{1..4}` ~5 900, rest negligible.

### 3.5 How this kills `device` — mechanisms, ranked

1. **Page-boundary SIGSEGV (primary).** Nine distinct addresses per boot pass a
   32-byte probe and then get read for 96 bytes across a page boundary. Each
   faults if the following page is unmapped. `device` installs no `SIGSEGV`
   handler that could survive this. *Confidence: high that this path executes;
   medium that at least one of the nine actually faults — whether the adjacent
   page is mapped cannot be determined offline.*
2. **fd theft during bring-up (secondary).** 4917 is the first build to call
   `open("/dev/null")`, and it does so at the **third** traced ioctl —
   mid-way through `device` opening its device nodes (`/dev/isf_flow0` = fd 16,
   `/dev/nvtmpp` = fd 18, `/dev/nvt_isp` = fd 21, `/dev/kflow_ai_net3` = fd 27).
   The shim takes the lowest free descriptor and shifts every subsequent
   `open()` in `device` by one. HDAL is statically linked into `device`
   (`APP_REPLACEMENT_DESIGN.md` §2a) and its fd bookkeeping is unaudited.
   *Inferred; would need the running camera or deep RE of `device` to confirm.*
3. **Per-call cost (contributory, not fatal on its own).** 32 379 extra
   `write()` syscalls plus ~8 MB of extra formatted output inside a
   `pthread_mutex`-serialised region, during time-critical media bring-up. Not
   sufficient to kill the process; sufficient to change timing.
4. **Log volume / `/mnt/sda`** — ruled out, see §7.4.

Note also that the shim's own doc comment claims "Safety: observation only",
which the `write()`-to-`/dev/null` probe already violates: the shim writes 32
bytes of `device`'s memory to a descriptor it opened, 32 379 times per boot. If
that descriptor were ever recycled by `device`, the shim would inject data into
a live channel. Not believed to have happened here (the fd is
`O_WRONLY|O_CLOEXEC` and never closed by the shim), but the "observation only"
claim is false as shipped.

---

## 4. Did the flash complete? — Yes, provably

`flash/flash_pak.py` control flow is the proof. `upload()` calls `sys.exit()`
on **any** of: a non-200 HTTP status on any part, or three consecutive socket
failures on one part. `wait_for_reboot()` — and therefore the string
`camera went down (flashing)` — can only run **after** `upload()` returns
normally, i.e. after all parts have been accepted.

The 4917 flash (`TX:2165 @03:04:41.168Z` → `TX:2175 @03:13:20.781Z`) produced:

```
boot chain + app unchanged: YES
computed: 0xd497b069
match:    True
cleared
  [  8s] camera went down (flashing)
WARNING: timed out waiting for the camera to come back; check it manually
```

(The `| tail -2` on the flash command is why `upload complete` and
`camera response` are not shown.) Reaching the `[8s]` line establishes:

- local CRC gate passed before a byte was sent (`verify_crc_locally`);
- login + `UpgradePrepare` returned `code: 0`;
- all **665** parts of 38 912 B each returned HTTP 200;
- the camera then took itself off the network to flash.

Compare with the fifteen flashes that night that came back — the down-time is
**8 s in every single case**, and the return is 54–56 s:

| transcript line | down | up |
|---|---|---|
| 1035 @23:09:30 | 2 s | 43 s |
| 1359, 1442, 1607, 1663 | 8 s | 55 s |
| 1685, 1758 | 8 s | 56 s |
| 1764 | 8 s | 55 s |
| 1875, 1916, 1958 | 8 s | 56 s |
| 1977 | 8 s | 55 s |
| 1981 | 8 s | 56 s |
| 2096 (**4915**) | 8 s | 54 s |
| 2153 (**4916**) | 8 s | 56 s |
| **2175 (4917)** | **8 s** | **never (420 s timeout)** |

An earlier flash records the camera's own acknowledgement of the final part
(`TX:1035 @23:06:58Z`):

```
665/665 (100.0%)   46.2s
upload complete in 46.2s
camera response: {"cmd":"Upgrade","code":0,"value":{"rspCode":200}}
```

**4917 behaved identically to fifteen successes right up to the reboot.** The
camera reassembled the pak, ran its own CRC gate (§1.1: the CRC is correct), and
began writing. Nothing in the transcript is consistent with an upload that was
truncated, rejected or interrupted.

What this does *not* prove: that the NAND program of `mtd5` itself finished. The
HTTP transaction ended before the erase/program started. That residual is the
only surviving path for the corruption hypothesis (§7.1).

---

## 5. What the packet captures say — and what they cannot say

Five captures in `SC/`, all parsed here directly from the pcapng blocks.

| file | frames | span | distinct source MACs |
|---|---|---|---|
| `cam_recovery.pcapng` | 6 | 08:40:56–08:44:26 | 1 (`60:b7:6e:ac:8d:34`, the LAN gateway) |
| `eth_listen.pcapng` | 117 | 08:49:32–08:50:09 | 1 (`6c:24:08:f9:07:1f`, the PC) |
| `direct_boot.pcapng` | 155 | 08:51:26–08:52:30 | 1 (the PC) |
| `direct_boot2.pcapng` | 810 | 08:55:30–09:00:33 | 1 (the PC) |
| `post_reset.pcapng` | 289 | 09:24:25–09:25:53 | 1 (the PC) |

Zero frames from `ec:71:db:44:56:12` in any capture. That much is confirmed.

### 5.1 The captures were **not** promiscuous

Both `direct_boot` and `direct_boot2` were started with
(`TX:2443 @12:51:25Z`, `TX:2468 @12:55:27Z`):

```
tshark.exe -i Ethernet -p -a duration:360 -w direct_boot2.pcapng
```

`tshark -h` on this machine:

```
  -p, --no-promiscuous-mode
                           don't capture in promiscuous mode
```

The narration at `TX:2470` and `TX:2494` ("capturing every frame
promiscuously") is the opposite of what the flag does. A non-promiscuous NIC
delivers only: frames to its own unicast MAC, broadcast, and multicast groups
the host has joined. The traffic a booted-but-idle camera would emit is
precisely what gets filtered:

| camera traffic | destination MAC | captured? |
|---|---|---|
| IPv6 DAD Neighbour Solicitation for its own link-local | `33:33:ff:44:56:12` | **no** — PC has not joined that solicited-node group |
| IPv6 Router Solicitation | `33:33:00:00:00:02` (all-routers) | **no** — PC is not a router |
| MLDv2 Report | `33:33:00:00:00:16` | **unlikely** |
| ARP / DHCP | broadcast | yes — but see below |

So the only camera traffic these captures could have seen is broadcast. And a
camera in the failure mode of §6 has no reason to broadcast: it is on a
**static** address (no DHCP client), it has **no default route** (so it never
ARPs), and its discovery daemons (`cloud`, `push`, `netserver` P2P) are
downstream of `device`.

**The captures do not rule out a camera that booted normally onto 192.168.0.3.**

### 5.2 What they *do* establish: the board has power and the PHY links

`Get-NetAdapter` at `TX:2469 @12:55:34Z`, **before** the power cycle:

```
Status LinkSpeed
------ ---------
Up     100 Mbps
```

The link had been up for the 13.5 hours the camera was "dead". In
`direct_boot2.pcapng` the timeline is unambiguous:

```
t+  8.1s  last frame before the unplug          (08:55:38)
          --- 29.7 s of total silence ---
t+ 37.7s  LLDP fast-start burst begins           (08:56:08)
t+ 38.1s  ARP 0.0.0.0 -> 192.168.86.50   (DAD)
t+ 38.1s  ICMPv6 type 133 (Router Solicitation)
```

Mark reported "ok, I plugged it in" at `TX:2474 @12:56:06Z` = 08:56:06 local.
**The link came back 2 seconds after PoE power was restored.**

Two seconds is far too fast for this boot: the kernel plus `S00`–`S10_SysInit2`
(≈40 `insmod` calls) plus `S25_Net`'s `modprobe ntkimethmac; ifconfig eth0 up`
takes tens of seconds — the successful flashes above took 54–56 s from
reboot to a serving HTTP daemon. So the PHY autonegotiates **in hardware, before
and independent of Linux**.

Consequences, both important:

- Link-up proves **only** that PoE power is present and the PHY is out of reset.
  It says nothing about whether the SoC booted.
- Conversely, silence on the wire does not prove the SoC is dead.

### 5.3 One unexplained observation

`direct_boot2` contains a **second** link-up event with nobody touching the
cable:

```
t+223.1s  last frame                              (08:59:13)
          --- 49.3 s of total silence ---
t+274.4s  LLDP fast-start burst + DAD + RS        (09:00:03)
```

LLDP appears in 1-second fast-start bursts at t+37.7…46.9 and t+274.4…289.1 and
**nowhere in between** — the signature of exactly two media-connect events. The
transcript records no second unplug between 12:59 and 13:00. Candidates:

- a PoE PSE disconnect/re-detect cycle, which happens when the powered device's
  current draw falls below the maintain-power signature — i.e. when the SoC is
  *not* running;
- an SoC reset (watchdog, §7.5) that also resets the PHY;
- a host-side NIC reset.

~238 s after power-on is late for a watchdog. **Not resolvable from the
captures.** A scope or a UART trace settles it (§10).

`post_reset.pcapng` shows the same shape: silence 09:24:37→09:24:54 followed by
an LLDP/DAD burst at t+28.6. The physical reset button therefore *did* produce a
link event — consistent with either a real reset or a hand on the cable.

---

## 6. Why "no boot window" is not evidence of a failed boot

This is the section that overturns the recorded counter-argument. Everything
here is read out of 4917's own rootfs and app images.

### 6.1 Boot order

`/etc/inittab` → `::sysinit:sh /etc/init.d/rcS`. `rcS` runs
`/etc/init.d/S[0-9][0-9]*` in glob order:

```
S00_PreReady  S07_SysInit  S10_SysInit2  S15_NvtAppInit  S25_Net
S35_RecRecover  S36_StitchProbe  S99_NetState  S99_Sysctl
```

`start_app` does **not** match the glob. It is invoked by the **last** script,
`S99_Sysctl` line 26:

```sh
/etc/init.d/start_app
```

and `start_app` line 109 is where `./router &` and
`LD_PRELOAD=/lib/ioctllog.so ./device &` finally launch. **`device` is the very
last thing to start.** Network init, the SSH-equivalent shell and the netstate
daemon are all already running by then.

### 6.2 The camera boots on 192.168.0.3, not 192.168.86.200

`S25_Net`:

```sh
NETWORK_SETUP_SCRIPT="/etc/init.d/net_init.sh"
...
    modprobe ntkimethmac
    ifconfig eth0 up
    # nvtsystem will generate this network setup script
    if [ -f "$NETWORK_SETUP_SCRIPT" ]; then
        $NETWORK_SETUP_SCRIPT
    else
        ifconfig lo 127.0.0.1
        ...
        else
            ifconfig eth0 192.168.0.3
        fi
    fi
...
telnetd
```

Two facts settle which branch runs:

- **`/etc/init.d/net_init.sh` does not exist** in the rootfs image (checked in
  all of 4867/4914/4915/4916/4917), and `/etc` lives in the SquashFS root
  mounted **read-only** (`bootargs … rootfstype=squashfs ro`, no overlay in
  `/etc/fstab`). It can never be generated. The `else` branch always runs.
- `/etc/profile_prjcfg` line 39:
  `export NVT_DEFAULT_NETWORK_BOOT_PROTOCOL="NVT_DEFAULT_NETWORK_BOOT_PROTOCOL_STATIC_IP"`
  — so within the `else`, the branch taken is `ifconfig eth0 192.168.0.3`, not
  `udhcpc`.

So **every** boot — including all fifteen successful ones that night — brings
`eth0` up on the hard-coded **192.168.0.3**, with no default route and no DHCP
client, and starts `telnetd`.

`S36_StitchProbe` then starts a root shell listener bound to all addresses:

```sh
PORT=2323
tcpsvd -vE 0.0.0.0 $PORT /bin/sh >/dev/null 2>&1 &
```

### 6.3 `device` is the only thing that moves the address

String scan of every binary in the `app` squashfs:

| binary | `eth0` | `ifconfig` | `udhcpc` | `route add default` |
|---|---|---|---|---|
| **`device`** | **1** | **8** | **2** | **yes** |
| `netserver` | 1 | 0 | 0 | no |
| `router` | 0 | 0 | 0 | no |
| `netclient`, `cloud`, `factory` | 0 | 0 | 0 | no |

`device`'s format strings:

```
ifconfig %s %u.%u.%u.%u
ifconfig %s netmask %u.%u.%u.%u
ifconfig %s up            ifconfig %s down
ifconfig %s hw ether %s   ifconfig %s 0.0.0.0
route add default dev %s gw %u.%u.%u.%u
route del default
udhcpc -i %s -x hostname:'%s' -p /var/run/udhcpc.pid -r %s --retries 100 &
killall -9 udhcpc
POE error!!! cur_tc - m_last_discon_tc(%llu), down up eth0 now.
```

**`device` owns the IP address.** If `device` dies during media bring-up — which
happens seconds after it starts, at trace line 3 — it never reaches its network
configuration, and `eth0` stays at 192.168.0.3 for the life of that boot.

This holds under a reboot loop too: every cycle re-runs `S25_Net`
(192.168.0.3), re-runs `S36_StitchProbe` (shell on 2323), then re-kills
`device`. **There is no boot cycle in which 192.168.86.200 ever appears.** The
150 s of 1-second polling was looking at an address the camera cannot reach in
this failure mode.

### 6.4 The reset button

`device` contains no reset-key or factory-reset strings; nothing in the rootfs
init scripts handles a GPIO reset either. The observation that the physical
button "does nothing" is consistent with the reset handler living in a userspace
daemon that is either dead or downstream of `device` — it is not independent
evidence of a dead SoC.

### 6.5 The measurement nobody made

`grep` of the entire transcript for `192\.168\.0\.[0-9]`: **zero matches.** The
camera's boot-default address was never pinged, never ARP'd, never port-scanned.
The LAN sweep at `TX:2339 @12:39:11Z` covered `192.168.86.0/24` only.

---

## 7. Other hypotheses, ruled in or out

### 7.1 Interrupted or corrupted NAND write to `rootfs` — **not excluded, but unsupported**

For: the HTTP transaction ends before the NAND program starts, so nothing in the
transcript covers the write itself.

Against:

- the pak's CRC is valid (§1.1) and the camera's own gate is upstream of the
  write (`flash/flash_pak.py` docstring, RE'd from `/mnt/app/upgrade`);
- the camera went down at **8 s**, the same as fifteen consecutive successes
  through the identical code path that night (§4);
- `rootfs` is 5 754 880 B into an 8 388 608 B partition — 68.6 % full, no
  boundary effects;
- if the SquashFS were unmountable the kernel would panic on
  `VFS: Unable to mount root fs`, and the console would say so (§10);
- **it does not explain the specific timing**: 4915 and 4916, byte-identical in
  every section but one file, wrote successfully minutes earlier.

It is the leading alternative only because it cannot be excluded without the
UART. It has no positive evidence.

### 7.2 NAND wear / bad block — **excluded**

Sixteen pak writes in one night (fifteen successes + 4917, §4), each rewriting
`mtd5` (8 MB = 64 erase blocks). SLC SPI NAND endurance is 60 000–100 000 P/E
cycles per block; 16 cycles is ~0.02 % of budget. Vendors also ship
firmware-managed bad-block handling on `mtd`. Not a credible mechanism.

### 7.3 `app` squashfs corrupt — **excluded**

`app` is **byte-identical** (SHA-256 `dd1810…`) across 4915, 4916 and 4917, and
was not rewritten by the 4917 flash beyond being present in the pak. 4915 and
4916 booted with the same bytes.

### 7.4 `/mnt/sda` full, or the 3 MB early buffer — **excluded**

- The 4917 flash command explicitly deleted the old trace first
  (`camsh.py 'rm -f /mnt/sda/ioctllog/trace.txt'`, `TX:2165`, output `cleared`).
  4917 started with an empty log directory.
- Under 4916 the early RAM buffer held **7 580 bytes** before `/mnt/sda`
  mounted (session banner, §3.1) — 0.25 % of the 3 MB cap. Even a 40× expansion
  stays under 300 KB.
- Projected 4917 log volume: 40 000 lines × ~350 B ≈ 14 MB. 4916 wrote 27 MB to
  the same card without incident.
- `emit()` fails safe: if the card is absent it buffers, and if the buffer is
  full it sets `early_full` and drops. No path aborts.

### 7.5 The watchdog — **real, and the amplifier rather than the cause**

`S00_PreReady` (the **first** init script):

```sh
cd /mnt/app
if [ -f watchdog_monitor_start ];then
        ./watchdog_monitor_start &
fi
```

`watchdog_monitor_start` (10 312 B, ARM64 ELF, stripped) references
`/dev/watchdog`, `/mnt/tmp/wdt_flag`, and:

```
route run now, the management of the watchdog will be transferred to the route
from watchdog_monitor_start.
```

`router` references `/dev/watchdog`, `echo wdt > /mnt/tmp/wdt_flag`,
`killall -9 watchdog_monitor_start`, `watchdog_feed fatal error`,
`watchdog not enabled!`.

So: the **hardware** watchdog (`wdt@2,f0240000`, `nvt,nvt_wdt` in the FDT) is
armed from the first line of boot, kicked by `watchdog_monitor_start`, then
handed to `router`. `router` starts one line *before* `device` in `start_app`.
Whether `router` stops feeding the watchdog when its `device` peer dies cannot
be determined offline — but if it does, the result is a hardware reset loop, and
that is the most likely explanation for the unexplained link flap in §5.3. In
either case the watchdog does not *cause* the outage; it recycles it.

`/etc/sysctl.conf` also sets `kernel.panic=10` / `kernel.panic_on_oops=0`, but
these are applied by `S99_Sysctl`, after the interesting part of boot.

### 7.6 `netstate` / `override` recording disable — **excluded**

`S99_NetState` backgrounds its work (`( main_loop ) & ; disown ; exit 0`) so it
cannot block `rcS`. It touches `Rec.enable` over `http://127.0.0.1` only; it
never configures the network. `S36_StitchProbe` writes
`/mnt/sda/netstate/override` so the daemon yields entirely. All three of 4915,
4916 and 4917 carry identical copies.

### 7.7 Power delivery — **excluded as a root cause**

Link was Up at 100 Mbps continuously (§5.2), including 13.5 hours into the
outage and 2 s after a fresh power-up. PoE is being delivered and the PHY is
running. (The §5.3 flap may be a *consequence* of a hung SoC drawing too little
current, not a cause.)

### 7.8 The shim's early `fopen` racing the card mount — **excluded**

`try_open()` retries every 64 traced calls and buffers until it succeeds; 4916
recorded `early_buffered=7580 bytes, overflow=0`, so the race is handled and
was handled identically in 4917 (same code — `.rodata` and the `try_open` path
are byte-identical between the two builds).

---

## 8. Reconstructed sequence

All times UTC (local = UTC−4).

| time | event | source |
|---|---|---|
| 02:50:22 | `ioctllog.c` written | `TX:2042` |
| 02:52:23 | 4915 built (rootfs + `app` from 4907 base; `.so` + `start_app` + stamp) | `TX:2079` |
| 02:53:28 | 4915 flashed | `TX:2092` |
| 02:56:48 | back up, 201 614 trace lines, 0 ipp errors | `TX:2096` |
| 02:58:01 | pointer-following **added** — `(v>>40)==0x7f`, probe 32, read 96 | `TX:2122` |
| 02:58:10 | `looks_readable()` added (`/dev/null` write-probe) | `TX:2131` |
| 02:59:05 | `MAX_LINES 400000 → 40000`; **4916 built** | `TX:2138` |
| 03:00:11 | 4916 flashed | `TX:2141` |
| 03:03:39 | back up in 56 s, 50 MB trace fetched, 0 ipp errors | `TX:2153` |
| 03:04:00 | noticed `>>40` never fires (correct diagnosis) | `TX:2159` |
| 03:04:09 | `sed`: filter → `0x40 ≤ (v>>32) ≤ 0xffff`; recompiled, 67 576 B | `TX:2160/2161` |
| 03:04:41 | **4917 built** (installs the `.so`, no recompile, no stamp update) and flashed | `TX:2165` |
| 03:13:20 | camera down at 8 s, never returned in 420 s | `TX:2175` |
| 03:31:13 | validate-32/read-96 identified as the cause | `TX:2219` |
| 03:39:10 | `RECOVERY_README.md` written | `TX:2288` |
| 12:55:34 | link measured **Up, 100 Mbps** — before any power cycle | `TX:2469` |
| 12:56:06 | PoE replugged; link back at 12:56:08 (**2 s**) | `TX:2474`, `direct_boot2.pcapng` |
| 13:00:45 | 810 frames, 0 from the camera MAC (non-promiscuous) | `TX:2492` |

---

## 9. Verdict and the deciding measurement

**Primary (high confidence): `device` was killed by the 4917 shim, and the
camera has been unreachable because `device` is what assigns its IP address.**

The chain, every link of which is verified above:

1. 4916 → 4917 differ in one file (§1.2).
2. That file's only semantic change is a pointer filter that goes from matching
   **nothing** to matching **32 379 u64 slots per boot** (§3.2, §3.4).
3. Behind that filter sit a 32-byte validation and a 96-byte read (§3.3), plus
   an `open()` and 32 379 `write()`s that had never executed on this camera
   before (§3.4).
4. `device` starts last in boot, after networking (§6.1).
5. `device` is the only binary that can move `eth0` off the boot-default
   `192.168.0.3` (§6.2, §6.3).
6. Therefore a dead `device` = no host at 192.168.86.200, no ARP reply, no boot
   window, on every cycle — exactly what was observed (§6.3).
7. Every contrary observation dissolves: the "no camera frames" captures were
   non-promiscuous and could not see a silent static host (§5.1); link-up is a
   hardware PHY property that proves only that power is present (§5.2); the dead
   reset button is handled by userspace (§6.4).

**Surviving alternative (low confidence, not excluded): the `mtd5` NAND program
did not complete.** No positive evidence; §7.1.

**The measurement that separates them, in order of cost:**

1. **Put a host on `192.168.0.0/24` and probe `192.168.0.3`.** Two minutes, no
   hardware. Power the board, set the PC to `192.168.0.10/24`, direct cable,
   then `ping 192.168.0.3`, `nc 192.168.0.3 2323`, `telnet 192.168.0.3`.
   `S36_StitchProbe` binds `tcpsvd` to `0.0.0.0:2323` and `S25_Net` starts
   `telnetd`, both **before** `device` — so if the camera boots at all, a **root
   shell answers on 192.168.0.3:2323**. A reply is a complete diagnosis and a
   complete recovery path (reflash 4909 from that shell). No reply after 90 s
   moves the weight decisively to §7.1.
2. If a promiscuous capture is easier than reconfiguring: rerun
   `tshark -i Ethernet -w cap.pcapng` (**without `-p`**) across a power cycle and
   look for any frame from `ec:71:db:44:56:12`, especially an IPv6 DAD
   Neighbour Solicitation ~15–30 s after power-on. Its presence proves the
   kernel reached `S25_Net`.
3. The UART console — §10.

---

## 10. What to look for on the UART console

`serial0` @ **115200 8N1**, `earlycon` enabled, from the FDT `/chosen` node
(§2). Under this verdict the first thirty seconds should look **completely
normal**, and the interesting event is late and in userspace. Read the boot in
these checkpoints:

| # | expected output | if present | if absent |
|---|---|---|---|
| 1 | u-boot banner + `Novatek` / `NT98530` init, DRAM size | boot ROM and `loader`/`atf`/`uboot` intact — expected, since none was modified (§2) | the failure is below the kernel; unrelated to any pak this session wrote |
| 2 | `Uncompressing Linux… done` / `Starting kernel …` and the `earlycon` banner `Linux version 5.10.168 (lmy@ubuntu) …` | kernel image on `mtd4` is good | u-boot could not load `mtd4` — but `mtd4` was never written |
| 3 | `VFS: Mounted root (squashfs filesystem) readonly on device …` | **`mtd5` is intact — §7.1 is dead and the verdict is confirmed** | `VFS: Unable to mount root fs on unknown-block(…)` / `SQUASHFS error: … unable to read` / `Kernel panic - not syncing: No working init` ⇒ **§7.1 is confirmed instead**; the NAND write did not complete |
| 4 | `[Start] /etc/init.d/S00_PreReady` … `[Start] /etc/init.d/S25_Net` (rcS echoes each) | init is running | a hang between these names localises the failure to that script |
| 5 | around `S25_Net`: `ntkimethmac` probe messages, PHY link, then silence | networking is up at **192.168.0.3** — go straight to §9 step 1 | — |
| 6 | `[Start] /etc/init.d/S36_StitchProbe` and `[Start] /etc/init.d/S99_Sysctl` | the 2323 root shell is listening | — |
| 7 | `start_app` output: `cat /proc/mounts`, `----------------mem limit set ok-----------------------`, the green `>>>>>>>>>>start_app support_ptz:` line | reached the daemon launch | — |
| 8 | **the decisive line** — after `./device &`, a kernel fault report naming `device`: `Unable to handle kernel paging request` / `potentially unexpected fatal signal 11` / `device[NNN]: unhandled level 2 translation fault` with a `pc`/`far` and a `x` register dump | **verdict confirmed at instruction level.** Note `far` (the faulting address); it should be a `0x7f…` value ending near a page boundary, matching the nine addresses in §3.4 | if `device` runs on and the camera simply never gets to 192.168.86.200, the failure is in `device`'s network config path, not a fault |
| 9 | whether the console then **repeats from step 1** | watchdog reset loop (§7.5) — measure the period; ~238 s would match the §5.3 link flap | a single boot that then sits idle means no loop, and the camera is quietly alive |

Practical notes: hold a key at u-boot to get the prompt before step 3, since
that is the recovery path if step 3 fails. If step 3 succeeds, do **not** bother
with u-boot — the camera is alive and §9 step 1 recovers it over the network.

---

## 11. Process changes that would have prevented this

Recommendations only — `builds/*.sh` is not edited here. Each is specific and
testable.

**Flashing workflow**

1. **Never flash the only camera without an out-of-band recovery path present
   and tested.** The UART pinout was uncharacterised at flash time
   (`FIRMWARE_PATCH_NOTES.md` §11 says so explicitly), and `APP_REPLACEMENT_DESIGN.md`
   §1 already records "there is no out-of-band recovery" as the reason the boot
   chain is never touched. Sixteen flashes were performed anyway, overnight,
   with no one able to power-cycle. Gate `flash/flash_pak.py` on an env var
   (`FLASH_RECOVERY_READY=1`) that the operator sets only when UART or a second
   camera is available.
2. **`flash_pak.py` must probe the boot-default address on failure.** When
   `wait_for_reboot()` times out, it currently prints
   "check it manually" and stops. It should then probe the addresses the
   firmware can come up on — `192.168.0.3:80`, `:23`, `:2323` — and say so. That
   single change would have found the camera within seconds of the failure and
   made this postmortem unnecessary. Read the fallback address out of
   `rootfs:/etc/init.d/S25_Net` + `/etc/profile_prjcfg` at build time and record
   it in the build manifest.
3. **Record the full flash log, not `| tail -2`.** The 4917 flash discarded
   `upload complete in …s` and `camera response: {…rspCode…}`, the two lines that
   directly evidence acceptance. Redirect to a file and tail from it.
4. **One change per flash, and diff the artefact, not the intent.** 4917 was
   built from an already-compiled `/var/tmp/ioctllog.so` with no compiler in the
   build command; the build could not have failed loudly if the `.so` had been
   stale or wrong.

**Build scripts**

5. **Extend the existing pre-CRC assertion from "boot chain unchanged" to a full
   manifest diff.** The current check (`TX:2165`) hashes
   `loader/fdt/atf/uboot/kernel/ai/app` and prints `YES`. It should instead
   unsquash both the base and the output `rootfs` and print **every** added,
   removed and content-changed path, and fail unless that set matches an
   explicit `--expect-changed` list passed on the command line. Applied to 4917
   this would have printed exactly `lib/ioctllog.so` — and would have flagged
   the 4915 build's silent revert of `nvt_sen_os08c10*.ko` and `app/device`
   (§1.3) as an unexpected change.
6. **Fail the build if `/etc/soccercam_build` is not rewritten.** 4917 shipped
   4916's stamp, so a running 4917 would have misidentified itself. Make the
   stamp mandatory and include: the `.so`/binary SHA-256s of every injected
   file, the base pak name, and the output CRC.
7. **Make the build compile from source in the same invocation.** No
   `install`-from-`/var/tmp`. Record the compiler command line in the stamp.

**The shim, and anything else `LD_PRELOAD`ed into `device`**

8. **A build that dereferences pointers out of another process's structs must be
   dry-run first.** The shim's filter and dereference logic is pure and
   testable: feed it the previous build's captured trace (`u32=` lists are
   already in the log) and assert the number of dereferences it *would* perform.
   Run against `trace2.txt`, the 4917 filter yields 32 379 where 4916 yielded 0 —
   a 4-order-of-magnitude change from a one-line `sed`, which no reviewer saw
   because the change looked like a bug fix. **Any build whose projected
   dereference count changes by more than ~10× from the last known-good build
   should require an explicit acknowledgement.**
9. **Validate exactly what you read.** The generic rule the 32-vs-96 bug
   violates. Better still: never dereference a pointer read out of another
   process's memory from inside that process — copy the struct and resolve
   pointers offline, on the PC, from the trace.
10. **Cap the exposure of any experimental instrumentation in time, not just in
    line count.** `MAX_LINES` bounds volume but not the window in which a fault
    can occur. A shim that disables itself N seconds after process start (or
    after the first `ioctl` on `/dev/isf_vdoenc0`) confines the risk to the
    phase being studied.
11. **`LD_PRELOAD` on `device` should be conditional on a file the operator can
    remove.** `start_app` could read `/mnt/sda/preload_enable`; a camera that
    boots far enough to mount the card but crashes in `device` is then
    recoverable by pulling the SD card. As shipped, the preload is baked into a
    read-only SquashFS and there is no way to disable it without reflashing.

**Documentation**

12. **Do not record an unverified mechanism as the cause.** `RECOVERY_README.md`
    named the 32-vs-96 bug as the cause 18 minutes after the failure, and its own
    counter-argument ("a crashing application would still let the network come
    up") was asserted without checking which process assigns the IP — a check
    that takes one `strings` invocation against `app/device` and would have
    reversed the conclusion the same night.
13. **Verify tool flags before describing behaviour.** `tshark -p` was described
    as "promiscuous" three times, and the resulting "conclusive" negative result
    was neither.

---

## 12. Immediate next steps

1. **Before the UART cable arrives**, do §9 step 1: power the board, PC on
   `192.168.0.10/24`, direct cable, probe `192.168.0.3` on 80/23/2323 for 90 s.
   Two minutes, no hardware, and it may end the whole problem — a root shell on
   2323 is enough to reflash 4909.
2. If that answers, reflash
   `IPC_NT15NA416MP.4909_..._fps21probe.pak` (or `4906_..._comprehensive.pak`
   for the exact pre-session state) and **do not** flash any pointer-following
   shim again.
3. If it does not, use the UART checklist in §10; the answer is at checkpoint 3
   (rootfs mount) and checkpoint 8 (`device` fault).
4. The fixed `SC/ioctllog_fixed.so` still contains the widened filter — it fixes
   the length mismatch but keeps 32 379 dereferences per boot. It should not be
   flashed as-is. Restore a narrow filter (`(v >> 32) == 0x7f`) *and* the length
   fix before any further tracing.

---

## 13. Outcome — recovered 2026-08-16, verdict confirmed

The camera was **never bricked**. §12 step 1 was executed and answered on the
first try. Everything below is observed on the live device, not inferred.

### The prediction held

Root shell on `192.168.0.3:2323`, uid 0, MAC `EC:71:DB:44:56:12`. `ps` showed
**every** daemon running — `router`, `recorder`, `alarmcenter`, `netserver`,
`netclient`, `upgrade`, `cloud`, `push`, `factory`, `rtsp`, `ftp`, `onvif`,
`cgiserver.cgi` — with `device` the sole absentee, and `device` is the process
`start_app:109` had wrapped in `LD_PRELOAD=/lib/ioctllog.so`. Fourteen hours of
"no response" were fourteen hours of probing `192.168.86.200`, an address
nothing was ever going to answer on.

### What recovery taught us that the analysis did not

| # | Finding | Evidence |
|---|---|---|
| 1 | `device` cannot be started by hand without the app dir on the library path | `./device: error while loading shared libraries: libbase.so`. `start_app:3` exports `LD_LIBRARY_PATH="$LD_LIBRARY_PATH:$APP_WORK_PATH"`. Correct invocation: `cd /mnt/app && export LD_LIBRARY_PATH="$LD_LIBRARY_PATH:/mnt/app" && ./device` |
| 2 | On a direct PC link `device` never finishes bring-up | it loops on `udhcpc: broadcasting discover` forever; no DHCP server, no lease, no HTTP. Recovery needs the camera on a real network |
| 3 | nginx has a build-host prefix compiled in | `open() "/home/lzq/nginx_all/trunk_new/trunk/source/_install/logs/error.log" failed`. Needs `-p <writable dir>`; `/mnt/app` is read-only squashfs so `mkdir() ".../proxy_temp" failed (30: Read-only file system)`. Support files (`mime.types`, `fastcgi_params`, …) must be copied from `/mnt/app/nginx_conf/conf/` |
| 4 | The generated HTTP vhost is **localhost-only by design** | `/mnt/tmp/nginx.conf`: `if ( $remote_addr != 127.0.0.1 ) { set $http_flag 1001; }` then `if ( $http_flag = "1001" ) { return 500; }`. Neutralise for a bench session by making the remote branch set `100` |
| 5 | There is no `443` server block at all in the generated conf | `grep -n 'listen' /mnt/tmp/nginx.conf` yields only `127.0.0.1:1935` and `80`. HTTPS was never going to bind; the missing `/mnt/para/*.crt` is a symptom, not the cause |
| 6 | **A pending factory reset fired.** The reset button is handled by `device`; the 2026-08-16 button attempt sat pending until `device` finally ran, then reset all of `/mnt/para` at 13:20 | every file in `/mnt/para` stamped `Aug 16 13:20`; `usr_v2.cfg` → `pwd="0000"` and an **empty** admin password logs in |
| 7 | The reset is why HTTP stayed down — **not** the missing SD card | `/mnt/para/service.cfg`: `<web port="80" enable_http="0" https_port="443" enable_https="0" …/>`, plus `<rtsp enable="0">` and `<onvif enable="0">`. With no service enabled, `device` never starts nginx. Setting `enable_http="1"` and rebooting brought port 80 straight up |
| 8 | `SetUser` is not supported on this firmware | `{"cmd":"Unknown","error":{"detail":"not support","rspCode":-9}}`. Password restoration is a web-UI job |
| 9 | **`/etc/soccercam_build` is unreliable** — it still reads `pak=…4907…` on a camera running 4909 | confirms §11's stale-stamp finding. Identify the running image by hashing files instead |
| 10 | Calibration survived the reset intact | `sp` (mtd9) md5 `32542b85a52e89168c072539ace080e6`, byte-identical to the pre-session archive |

### Available on-camera tooling (busybox 1.36.0)

Present: `dd`, `nandwrite`, `nanddump`, `md5sum`, `wget`.
**Absent**: `nc`, `flash_erase`, `setsid`, `nohup`, `timeout`, `curl`.
`which` lies about applets — test by running them.

### Verification that the reflash took

4909 was uploaded over the restored HTTP path (665 parts, 47 s) and the camera
rebooted unattended. Identity confirmed by hash, since the build stamp cannot be
trusted:

```
                        live camera                        4909 image
S36_StitchProbe   fb7d3ce2e7dc3ba21c7f8a8c44ae2c66   fb7d3ce2e7dc3ba21c7f8a8c44ae2c66
start_app         40d13ccc6f1402dea249b0192440d30e   40d13ccc6f1402dea249b0192440d30e
S99_NetState      ae619472a5ed952cc230074be3e9c12f   ae619472a5ed952cc230074be3e9c12f
```

`start_app` contains zero `LD_PRELOAD` lines and `/lib/ioctllog.so` is gone.
`GetEnc` reports `7680*2160 @ 21fps h265`.

### Post-recovery state

Config is at reset defaults: **admin password empty**, bitrate 10240 kbps (the
build intends 20480), HTTPS/RTMP/ONVIF off. HTTP and RTSP were re-enabled by
editing `service.cfg` directly; the rest is a web-UI task.

### One workstation trap worth recording

Both `New-NetIPAddress` and `netsh interface ipv4 add address` **silently switch
a Windows adapter from DHCP to static**. Adding a `192.168.0.10/24` helper
address to reach the fallback subnet therefore breaks the adapter's real
connectivity, and removing the address afterwards leaves it with none. Restore
with `Set-NetIPInterface -Dhcp Enabled` plus `Set-DnsClientServerAddress
-ResetServerAddresses`. Prefer putting the camera on a real network over
adding helper addresses.
