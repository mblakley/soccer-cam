# Duo 3 PoE frame rate — per-stage evidence

Supporting detail for [`FPS_CEILING.md`](FPS_CEILING.md). That document states
the result and the verdicts; **this one only shows the readings** — register
values, file offsets, counter dumps, decompiled loops. It draws no conclusions
of its own. If a number here disagrees with `FPS_CEILING.md`, `FPS_CEILING.md`
is the error and should be fixed.

Camera `192.168.86.24`, firmware base `IPC_NT15NA416MP.4867_2505072124`, HDAL
`00305000:00305001`, kernel `5.10.168`, board `Novatek NS02302`. Sessions
2026-08-15 → 2026-08-17.

### Confidence tiers

| tier | meaning |
|---|---|
| **S** | static — read out of the shipped binary; file + symbol/offset given |
| **L** | live — read off the running camera; the command is given |
| **I** | inferred — the arithmetic is shown and the assumption is named inline |

---

## 1. Sensor — one mode, 25.00 fps, and a hard clamp

### 1.1 The mode table, read out of the ELF (S)

`nvt_sen_os08c10.ko`, `.data` @ file offset `0x008c20`. Symbol sizes from
`.symtab`:

| symbol | `.data` offset | size | note |
|---|---|---|---|
| `mode_basic_param` | `+0x0000` | 320 | stride `0xa0` ⇒ 2 slots, **only slot 0 populated** |
| `speed_param` | `+0x0c90` | 40 | stride `0x14` ⇒ 2 slots, **only slot 0 populated** |
| `mipi_param` | `+0x0cb8` | 120 | `{0, 1, 2}` then all zero |
| `os08c10_mode_1` | `+0x0dd0` | 10496 | 656 × 16-byte `{u32 addr, u32 len=1, u32 val, u32 pad}` I2C entries |

`mode_basic_param[1]` and `speed_param[1]` are all zero. **There is exactly one
populated sensor mode and no faster mode already compiled in.** Confirmed live
as `max_senmode 1` / `MODE_1` (L).

`mode_basic_param[0]`, fields confirmed by their use sites:

| offset | value | what it is | how confirmed |
|---|---|---|---|
| `+0x18` | **2500** | `fps_base`, ×100 ⇒ 25.00 fps | `sen_chg_fps_os08c10` @ `00102c10` assigns it to `cur_fps`/`chgmode_fps` |
| `+0x24` | 10 | bit depth | (I) |
| `+0x2c/+0x30` | 3840 × **2162** | crop | matches reg `0x3808/0x380a` |
| `+0x3c/+0x40` | 3840 × 2160 | output | matches `/proc/hdal/vcap/info` (L) |
| `+0x80` | **2592** | HTS | used as `HTS*10/(pclk/1e6)` in `sen_get_info_os08c10` |
| `+0x88` | **2314** | VTS_base | `sen_chg_fps_os08c10` writes it to the VTS registers |

`speed_param[0]` = `{0, 0, 24000000, 150000000, 198000000}` =
`{clk_src, clk_sel, mclk, pclk, data_rate}`. **MCLK 24 MHz, PCLK 150 MHz.**
`data_rate = 198000000` does not decode to anything sensible in bps/Mbps/MHz for
any lane count — **unit unresolved, do not use it.**

> **Ghidra caveat.** `ghidra_sensor_spec.log` from an early session dumps
> `mode_basic_param`/`speed_param`/`mipi_param` as **all zeros** — Ghidra loaded
> the relocatable `.ko` with an uninitialised `.data` block. Every data value
> above was re-read by parsing the ELF section headers directly. Ghidra was used
> only for code.

### 1.2 The I2C register table agrees exactly (S)

Decoded from `os08c10_mode_1`:

| reg | value | meaning |
|---|---|---|
| `0x3800`..`0x3807` | 0,0 → 3871, 2191 | **3872 × 2192** full active array |
| `0x3808/09` | `0x0f00` = 3840 | output width |
| `0x380a/0b` | `0x0872` = **2162** | output height |
| `0x380c/0d` | `0x0a20` = **2592** | HTS |
| `0x380e/0f` | `0x090a` = **2314** | VTS |
| `0x3814/15` | `0x11`/`0x11` | X inc 1/1 — no binning, no skipping |
| `0x3820/21` | `0x80`/`0x04` | no binning bits set |

3872 × 2192 matches the OS08C10 product brief exactly, confirming the part.

### 1.3 The timing closes three ways (S)

```
line rate = pclk / HTS  = 150 000 000 / 2592 = 57 870.37 lines/s
fps_base  = line / VTS  = 57 870.37 / 2314   = 25.008  == fps_base field 2500/100
line time = HTS*10/150  = 2592*10/150        = 172     == 17.28 us  (the code's own formula)
```

Three independent fields plus `pclk` agree to 0.03 %. Cross-check against the
only public OS08C10 driver (`themactep/ingenic-sdk` `t41/os08c10.c`): it uses
`HTS_reg 2976`, `VTS 2520`, `SCLK 300 MHz`, where the HTS register counts **2
SCLK per tick**. Applying that convention here, `2592 × 2 = 5184` ticks at
300 MHz = 17.28 µs — identical line time. Novatek books it as 2592 ticks of a
150 MHz half-rate clock. Independent sources, same answer.

Confirmed live: `/proc/kflow_sen/info` reports `pclk 150000000` (L).

### 1.4 The clamp (S, decompiled and verified)

`sen_calc_chgmode_vd_os08c10` @ `00101a80`:

```c
VTS = (VTS_base * fps_base) / fps_req;          // (2314 * 2500) / fps_x100
if (0xffff < VTS) { VTS = 0xffff; fps = ...; }  // low-fps clamp -> 0.88 fps floor
if (VTS < VTS_base) {                           // <<<< HIGH-FPS CLAMP
    chgmode_fps = fps_base;                     // silently forced to 2500
    cur_fps     = fps_base;
    VTS         = VTS_base;                     // 2314
}
return VTS;                                     // -> regs 0x3840[23:16], 0x380e, 0x380f
```

| requested | computed VTS | outcome |
|---|---|---|
| 21.00 fps | 2754 | accepted → 57870.37/2754 = 21.01 fps |
| 25.00 fps | 2314 | accepted, exactly at the floor |
| 30.00 fps | 1928 < 2314 | **clamped — silently reset to 25.00** |

Two consequences:

1. **21 fps is produced by making VTS *longer*, not shorter.** The sensor is
   throttled below its own mode base.
2. **Cropping rows buys nothing on the shipped firmware.** The clamp compares
   against `VTS_base` (2314), *not* the active row count. Ask for 720 rows at
   60 fps and you still get VTS 2314 and 25 fps.

Demonstrated live (L). Build with the capture hardcode set to 28:

```
VIDEOCAP 0 IN FRAME   frc = 28/1                                <- device asks for 28
/proc/kflow_sen/info  fpsxbase(dft(max), chgmode, cur) = (2500, 2500, 2500)
VD_TO_SIE0 intervals  40366 39736 39992 40220 39864 39941 us    -> 24.99 fps
```

### 1.5 The full-height arithmetic ceiling (I, from measured constants)

With `fps = pclk/(HTS × VTS)` and VTS floored by the 2162-row readout:

| fps | required VTS | vs 2162-row readout |
|---|---|---|
| 25.00 | 2314 | ok, 152 rows vblank |
| 26.00 | 2225 | ok, 63 rows vblank |
| **26.76** | **2162** | zero vblank — the floor |
| 28.00 | 2066 | **96 rows short — impossible** |

28 fps would need the sensor to emit 2162 rows in 2066 line periods. It needs a
different HTS or a re-derived sensor PLL, with only two MIPI lanes to carry it.
`build_fps_demo.sh` refuses any retime whose VTS would fall below the readout
height, so this is enforced at build time.

### 1.6 What the silicon is rated for (external)

OmniVision OS08C10 product brief v1.1 (Jul 2024), under *maximum image transfer
rate*:

| mode | published rate |
|---|---|
| full size, 10-bit | 60 fps |
| full size, **12-bit** | **48 fps** |
| DAG HDR, 12/14-bit | 30 fps |

Also 1.449 µm pixel, 1/2.82" optical format, 3872 × 2192 array, 4-lane MIPI,
296 mW. (The "2.0 µm / 1/1.8"" figures belong to the siblings OS08A10/OS08A20,
not this part.)

This pipeline captures **RAW12** (`/proc/hdal/vcap/info`, L), where the part is
rated 48 fps. The shipped firmware uses 25.00 — **52 % of the sensor's rating.**

---

## 2. The TGE — what actually sets the frame period

Both sensors run as **slaves** to the SoC timing generator (`tge_en 1` in
`/proc/hdal/vcap/info`, `signal_type SLV` in `/proc/kflow_sen/info`). The TGE,
not the sensor, decides when a frame starts. This is why the sensor-VTS retime
did nothing (`FPS_CEILING.md` F5).

The TGE emits HD every `hd_period` TGE clocks and VD every `vd_period` HD
periods. Live, for **both** chips, byte-identical (L):

```
tge_signal(hd_sync 8, hd_period 415, vd_sync 2313, vd_period 2314)
tge_vdhd 0x00000001   tge_vd_evt 0x00000001   signal_type SLV
```

Both are hardcoded immediates in `nvt_sen_os08c10_slave.ko` (S):

```
mov  x?,#0x909              ; vd_sync   2313
movk x?,#0x19f, LSL #32     ; hd_period  415
movk x?,#0x90a, LSL #32     ; vd_period 2314
mov  w?,#0x90a              ; vd_period multiplier
```

**`vd_period` is unusable as a lever.** The driver recomputes it as
`vd_period' = (2314 × dft_fps) / chgmode_fps`, and
`sen_calc_chgmode_vd_os08c10_slave` clamps `chgmode_fps <= dft_fps`. That makes
`vd_period' >= 2314` always, and the clamp is **scale invariant** — raising
`dft_fps` raises the numerator by the same factor.

**`hd_period` is the lever.** A pure pass-through constant, used in no
arithmetic anywhere, dividing the TGE frame period linearly:
`fps_new = 24.95 × 415 / hd_period`. `vd_period` and `vd_sync` stay put, so the
signal stays self-consistent.

Ceiling: the sensor still needs `2162 × (2592 / 150e6)` = **37.359 ms** to read
a full frame. The VD interval must stay above that, so
`hd_period >= 415 × 37.359/40.080 = 386.8` ⇒ **>= 387**, a hard ceiling of
**~26.77 fps** at 3840 × 2160 (I).

| `hd_period` | predicted fps | VD period | guard over readout |
|---|---|---|---|
| 415 | 24.95 | 40.080 ms | 2.721 ms (157 rows) — stock |
| 400 | 25.89 | 38.631 ms | 1.272 ms (74 rows) |
| **396** | **26.15** | 38.245 ms | 0.886 ms (51 rows) — builder default |
| 392 | 26.41 | 37.859 ms | 0.499 ms (29 rows) |
| 388 | 26.69 | 37.473 ms | 0.113 ms (7 rows) |

Below ~390 the guard is thin enough that torn or truncated frames are the
expected failure mode. Walk down, don't jump. Builder: `builds/build_tge_retime.sh`
(**not flashed** — see `FPS_CEILING.md` O3).

The TGE's four VD/HD generators (`tge_setVdHd` / `Vd2Hd2` / `Vd3Hd3` / `Vd4Hd4`,
register stride `0x1C` from `0x28`) are all enabled by one store to bits 16–19
of reg `0x00` and reloaded by one global LOAD strobe (`tge_setLoad` writes
`0xf06`), so any two channels are frequency-locked regardless. TGE "phase"
(reg `0x08`) is **rising/falling edge only** — `kdrv_tge_set_vdhd_param` rejects
anything but 0/1 with `"Unknown vd_phase %u"`. There is no phase-offset register.

---

## 3. Capture front end — SIE, MIPI

### 3.1 Which blocks are powered (L)

From `/sys/kernel/debug/clk/clk_summary`; `enable_count` is the column that
matters:

| clock | rate | enable | in the path? |
|---|---|---|---|
| `2f0310000.sie1` | 320 MHz | 1 | yes — sensor 0 |
| `2f0312000.sie3` | 320 MHz | 1 | yes — sensor 1 |
| `2f0311000.sie2` / `sie4` / `sie5` | 320 MHz | 0 | no |
| `2f0320000.vie1` | 320 MHz | **0** | **no — VIE is off** |
| `2f0340000.ife` | 450 MHz | 2 | yes |
| `2f0400000.ipe` | 450 MHz | 2 | yes |
| `2f0410000.ime` | 450 MHz | 2 | yes |
| `2f0500000.vpe` | 440 MHz | 1 | only the 1280×352 sub-path |
| `2f0c20000.dce` | 12 MHz | **0** | **no — dewarp engine powered down** |
| `venc_clk` (pll15) | 480 MHz | 1 | yes |
| `cpu_clk` (pll8) | 162.5 MHz | 1 | 2× Cortex-A53 |
| `pll3` | 333.25 MHz | 1 | no Linux child — DDR PLL candidate (I) |

Corroborated by `dmesg`: `SIE_CLK [O]`, `SIE3_CLK [O]`, `SIE2/4/5_CLK [X]`,
`VIE_CLK [X]`, `IFE_CLK [O]`.

**VIE and DCE are both out of the picture.** The 400 Mpix/s `kdrv_vie_limit` is
irrelevant to this pipeline, and there is no hardware dewarp running.

### 3.2 The SIE limit table (S)

`kdrv_videocapture.ko`, `.data+0x1e8`, symbol `kdrv_sie_limit_98538`, 2920 bytes
= 5 × `0x248`. Entry 0, first line:

```
+0x00  00 84 d7 17  80 d1 f0 08  04 00 00 00  03 00 00 00
       400,000,000  150,000,000  4            3
```

| field | value | reading |
|---|---|---|
| `+0x00` | 400 000 000 | SIE pixel-rate ceiling ⇒ 3840×2160 @ **48.2 fps** — not binding |
| `+0x04` | 150 000 000 | max SIE input pixel clock — the sensor runs at **exactly 150 MHz** (I) |

The `+0x04` matching `speed_param.pclk` to the digit is unlikely to be
coincidence: **the sensor's pixel clock is pinned to the capture block's
maximum**, so you cannot buy frame rate by raising PCLK.

**Caveat, stated plainly:** no site was found that compares either constant
against a computed rate. `vie_chk_limitation` @ `00173498` is a 1-byte external
thunk (unimplemented in this module) and `tge_chk_limitation` @ `0011e920` is
`return 0;`. The only in-module references to `kdrv_vie_limit` are `+0x70`/`+0x78`,
which are **feature bits**, not rates. So both constants are **advertised
capability, not enforced ceilings**, as far as `kdrv_videocapture.ko` goes.

### 3.3 MIPI — two lanes of four

| source | finding |
|---|---|
| `/proc/hdal/vcap/info` (L) | `sen_2_serial_pin_map[0:7] = 0 1 -1 -1 -1 -1 -1 -1` — **2 pins mapped** |
| `device`, `nvt_vcap.cpp` (S) | `local_78 = 2; FUN_0059b2e0(path, 5, &local_78);` beside the string `vendor_videocap_set(cap_path, VENDOR_VIDEOCAP_PARAM_DATA_LANE, &data_lane)` — literal 2 |
| OS08C10 brief | the part supports 4 |

Payload arithmetic (I):

```
3840 x 2162 x 12 bit x 25 fps = 2.491 Gbit/s  ->  1.246 Gbit/s per lane on 2 lanes
```

The one public OS08C10 driver runs the same part on 2 lanes at 1296 Mbps/lane.
If that is also this board's rate, the link is at **96 % utilisation at 25 fps**
and the 2-lane ceiling is `2 × 1296e6 / (3840 × 2162 × 12)` = **26.0 fps**.

Tier **I** — the per-lane rate is inferred from another board. It is a
consistency signal, not a measurement, and it is not binding anyway (the
pipeline never reaches 26 fps). Wiring the other two lanes is a PCB change.

---

## 4. ISP / IPP — one engine, three clients

This is the stage that starves the second sensor pipe. Measured at steady state,
geometry `7680x2160 @ 25/25` at `VIDEOENC 0 IN`, uptime 400–1300 s (all samples
outside the boot window).

### 4.1 Capture loses nothing — the sensors are exact (L)

`/proc/kflow_sie/info` accu counters are **cumulative** (unlike `/proc/hdal/*/info`,
which self-reset on read). Over a 60.04 s window:

| | sensor 0 (ISP 0) | sensor 2 (ISP 2) |
|---|---|---|
| VD interrupts | **1500** | **1500** |
| `new_ok` | 1500 | 1500 |
| `new_fail` / `drop` / `in_q_ful` / `out_q_ful` | 0 | 0 |

24.983 fps each, deltas identical every window, a **constant VD offset of 7**
(a start-time artefact — `ctl_sen` shows sensor 2's `chgmod` 304 ms after sensor
0's) and **zero drift over 1500 frames**. Both `VIDEOCAP 0` and `VIDEOCAP 2` OUT
read `NEW 25 / PROC 25 / PUSH 25`, all drops and errors zero.

They cannot drift — they are the same clock (§2). Two corroborating negatives
from the driver source: `_isf_vdocap_do_setportstruct` **refuses** any software
sync while the TGE is on (`"ERR:%s() Only support HW tge sync by pinmux"` when
`vcap_sync_set != 0`), and the SIE's own re-alignment loop
(`ctl_sie_vd_sync_isr_proc`) is reachable only via `ctl_sie_set_sync_info` mode 2,
which the live dump shows is mode 0. There is no FSIN/XVS pin: a scan of the
whole rootfs and app for `FSIN|XVS|XHS|sensor_sync|vsync_out` returns nothing,
and `sen_os08c10_539.cfg`'s only GPIOs are the two resets (`id_0_rst_pin 0x46`
= `S_GPIO_6`, `id_2_rst_pin 0x47` = `S_GPIO_7`).

Two more: the SIE's cross-sensor alignment block (`-----sync info-----`:
`mode, sync_id, sync_diff, adj_thres, adj_auto, …`) is **all zeros — disabled**,
and the SIE group/combine feature is unused (`-----group info-----`: each sensor
its own group, `comb_num 1`). The SIE is not pairing or stitching anything.

> Static analysis of `ctl_sen_tge_open` suggested cap 0 and cap 2 sit on
> *different* TGE channels (ch1/MCLK1 and ch2/MCLK2, selected by the
> `sensorsync` pinmux — FDT `/top@2,f0010000/sensorsync/pinmux = <0x84218421>`,
> the only non-zero sensor pinmux group). **The live reading disagrees and
> wins:** the channel bitmask reads `0x1` for both. Either way the measured
> result is the same. Resolving one-channel-vs-two would need
> `echo dumpinfo > /proc/kdrv_tge/cmd && dmesg` — a write, not done.

### 4.2 Where the frames actually go: one IPP at 99 % (L)

`/proc/kdrv_ipp/utilization` reads **`usage 99, fps 66`**, steady across every
sample. `/proc/kflow_ipp/info` shows three VideoProc clients on that one engine.
Measured `ctl_end` deltas over 60.64 s:

| IPP client | frames/s | what it is |
|---|---|---|
| `vdoprc0` | **25.00** | ISP pipe for sensor 0 (`VIDEOPROC 0`, `RAWALL`) |
| `vdoprc1` | **21.73** | ISP pipe for sensor 2 (`VIDEOPROC 1`, `RAWALL`) |
| `vdoprc3` | **19.97** | sub/ext stream splitter (`VIDEOPROC 3`, `YUVALL`) |
| **total** | **66.71** | matches the engine's own `fps 66` at `usage 99` |

The deficit is **entirely one-sided**. Five independent 1 s windows of
`/proc/hdal/vprc/info`:

| | `VIDEOPROC 0` IN | `VIDEOPROC 1` IN |
|---|---|---|
| PUSH | 25 25 25 25 25 | 25 25 25 25 25 |
| **drop** | **0 0 0 0 0** | **3 3 4 4 3** |
| PROC | 25 25 26 24 25 | 21 22 21 21 22 |

Cumulative since stream start the ratio is stable at 26296/30228 = **0.870**, so
this is persistent, not jitter. `ctl_drop` is 0 for both — frames are refused at
the VideoProc **input** (out depth 1) because the pipe is still busy, not
discarded inside the ISP.

```
sensor 0 --25.0--> SIE 0 --25.0--> VIDEOPROC 0 --25.0--\
                                                        >-- VSP --21--> VENC --20-->
sensor 2 --25.0--> SIE 2 --25.0--> VIDEOPROC 1 --21.7--/
                                        ^ 99%-busy shared IPP starves this one
```

25.00 + 21.73 = **46.7 of 50** sensor frames reach the ISP.

> **INFERENCE (not proven):** the deficit lands wholly on `vdoprc1` because the
> three clients are serviced in a fixed order each VD and pipe 1 is behind pipe
> 0. The ordering itself was not observed; only that the loss is persistent and
> one-sided.

### 4.3 `isp_dev_get_sync_item` is 3A/IQ metadata, not frame pairing (S)

`nvt_isp.ko`, `.text+0x8d84` (Ghidra `00108dc4`). A **24-slot ring of per-frame
ISP parameters** (0x54 bytes/slot, per ISP id), written by
`isp_dev_set_sync_item` and read by the 3A/IQ threads. `param_2` selects the
pipeline stage whose frame counter to index by — 0 `isp_sync_id_sie`,
1 `isp_sync_id_ipp`, 2 `isp_sync_id_enc`; `param_3` selects the item
(`AE_STATUS`, `TOTAL_GAIN`, `DGAIN`, `LV`, `EV_RATIO`, `CGAIN`, `CT`,
`HBS_PARAM`, the `ca`/`la`/`va` ROIs …).

The mismatch test at `001096xx` is `(cur_framecnt - slot_framecnt) < 0x18`. It
means *"the metadata for the frame I am asking about is ≥ 24 frames stale"* and
returns `-5`; the caller goes without that parameter. Callers, from `.rela.text`:
`ae_flow_auto_process`, `awb_flow_process` (3 sites) and **`iq_flow_process`
(50 sites)**. **Nothing pairs sensors anywhere in it.**

A `sync info not match` flood is therefore a **symptom of the 3A/IQ threads
running late**, and it over-reports badly — one late IQ pass emits up to 50
lines.

It is also gated harder than it looks. From the disassembly at `00109698`:

```
0010969c  and w20,w0,#0x20000000       ; dbg_mode & WRN bit
001096a0  tbz w0,#0x1d,0x001096b4      ; bit clear -> skip the unlimited printk
001096b0  b.hi 0x00109774              ; isp_debug_level > 1 -> PRINTK (no rate limit)
001096c0  bl  isp_dbg_check_wrn_msg    ; returns (arg == 0 && counter < 20)
001096c4  cbz w0,0x001096e0            ; -> return -5 silently
```

`isp_debug_level` is `.data+0x11f8` = **3** (file offset `0x14620`), so the level
gate passes. `dbg_mode[]` is `.bss` (`0x11b2f8`) = **0 at load**, so the
unlimited printk is off and the surviving path is capped by
`isp_dbg_check_wrn_msg` at **20 lines for the module's lifetime** (counter `.bss`
`0x11b320`, threshold `0x14`; `isp_dbg_clr_wrn_msg` has no caller in any hdal
module).

**A sustained flood therefore requires something to have set `dbg_mode` bit 29
(`0x20000000`) for that ISP id** — after which every mismatch prints, unlimited,
to `console=ttyS0,115200` (`/proc/cmdline`; `/proc/sys/kernel/printk` = `7 4 1 7`,
so plain `printk()` at level 4 reaches the UART). At ~65 chars that is ~5.6 ms of
blocking UART per line, ~280 ms per late IQ pass. If a flood was genuinely
observed it was self-amplifying and worth killing on its own — but it is a
consequence of the ISP being late, not a cause of frames failing to pair.

### 4.4 The real pairing mechanism: userspace, ±30000 ticks (S + L)

Pairing is not in the kernel. `device` runs a thread literally named
**`bc_stitch_main`** — confirmed live, `/proc/751/task/*/comm`. It pulls one
frame from `VIDEOPROC 0` out0 and one from `VIDEOPROC 1` out0 (200 ms timeout
each), compares `HD_VIDEO_FRAME+0x28` (timestamp), and pushes both into
`VIDEOPROC 2` in[0] as a 2-frame group.

The tolerance is a hardcoded immediate at `device` file offset `0x81974`,
verified by decoding the instruction word rather than trusting a decompiler:

```
0x81974: d2 8e a6 02  ->  sf=1 opc=2 fixed=0x25 hw=0 imm16=0x7530 Rd=x2
                      ->  MOVZ x2, #30000
```

followed by `subs`/`cneg`/`cmp`/`b.le` — i.e. `if (|tsA - tsB| <= 30000) push`.
Out of tolerance it increments a streak counter, logs `stitch thread frame %u
timestamp don't match`, and **still pushes the pair**; only on the 3rd
consecutive miss does it log `drop main %u.%s frame to sync` and release+re-pull
on whichever side is ahead. (All three format strings are in `device` at
`0x3345d8`, `0x334620`, `0x3345b0`.) The kernel side
(`_isf_vdoprc_iport_do_push`) pairs only on an ordered index nibble and an
exact-equality "count" field — no window, no timeout, no wait.

**This tolerance is not the limiter.** The missing frames are lost *before*
pairing: `VIDEOPROC 1 IN drop` is a kernel counter at the VideoProc **input**,
upstream of anything the stitch thread can see. A frame refused there never
reaches an out-queue and never gets a timestamp compared. The tolerance's only
visible effect is secondary — it is what discards the fast side's surplus, which
is why `VIDEOPROC 0 USER PULL` reads 26/s against `VIDEOPROC 1 USER PULL` 22/s.

Widening `0x7530` would buy nothing and would cost picture quality — it would
push temporally-offset halves through the stitcher, tearing the seam on motion.

### 4.5 The levers on IPP load

Only **less IPP work per second** helps, from one of:

- **fewer IPP clients** — `vdoprc3` alone accounts for 19.97 of the 66.71 jobs/s.
  This is the one that was tried and worked (§7.2).
- **fewer pixels per frame** — windowed sensor readout.
- **fewer per-frame ISP functions** — both raw pipes run `func_en 0x000002ad`
  with `WDR`, `DEFOG`, `3DNR`, `3DNR_STA`, `PRIMASK`, `YUV_SUBOUT` enabled
  (`/proc/kflow_ipp/info`, `---- ctrl handle func_en ----`). The bit-to-flag
  mapping is **not** sequential and has **not** been decoded — do not guess it.

Deeper input queues on `VIDEOPROC 1` cannot help: with the engine at 99 % the
shortfall is sustained throughput, so a longer queue only adds latency before
dropping the same frames.

### 4.6 `ctl_ipp_int_ime_path_adj` is a geometry check, not a rate check (S)

Located in `kflow_videoprocess.ko` `.rodata`:

| `.rodata` off | string |
|---|---|
| `0x0a13a0` | `ERR:%s() p%u precrp w/h (%u, %u) != in_size (%u, %u) or (w, 0) or (0, h)` |
| `0x0a1520` | `ERR:%s() p%u scl_size (%u, %u) != in_size (%u, %u)` |
| `0x0a14a0` | `ERR:%s() p%u postcrp w/h (%u, %u) != scl_size (%u, %u)` |
| `0x09eb00` | `WRN:%s() buf overflow in_size (%d,%d), max size (%d,%d)` |
| `0x095bf0` | `ctl_ipp_int_ime_path_adj` (the `__func__`) |

All three are **equality** checks — "the IME scaler is bypassed on this path, so
pre-crop, scale and post-crop sizes must agree exactly". A build that changed
input height to 720 and left the IME scaler configured for 2160 rejected with
`scl_size (3840, 2160) != in_size (3840, 720)`. **That is a patch bug, not a
bandwidth limit.** Keep `scl_size == in_size == post-crop` when changing
geometry.

---

## 5. The userspace pump

Static RE of `sections/app_extracted/device` (aarch64, ET_EXEC, load base
`0x400000`, so **file offset = VMA − 0x400000**), plus live `/proc` sampling.

### 5.1 The graph, and where userspace sits in it

```
VIDEOCAP 0 ──bound──► VIDEOPROC 0 (ISP, 3840x2160 NVX2, out depth 1) ─┐
VIDEOCAP 2 ──bound──► VIDEOPROC 1 (ISP, 3840x2160 NVX2, out depth 1) ─┤
                                                                      │ USERSPACE
                                            bc_stitch_main ◄──────────┘ pull x2
                                                   │ push x2
                                                   ▼
                              VIDEOPROC 2  pipe=VSP  in[0] bind_src (null)
                              out[0] 7680x2160 NVX2 depth 2
                              out[1]  256x2160 NVX2 depth 2  (seam strip)
                                                   │
                    ┌──────────────────────────────┴─── sometimes kernel-bound to
                    │ USERSPACE (when out[0] is unbound)   VIDEOENC_0_IN_0
                    ▼
             FUN_00481df0  pull(wait=0) → hd_videoenc_push_in_buf → VIDEOENC 0 in[0]

VIDEOPROC 3 ──bound──► VIDEOENC 0 in[1] (sub) and in[3] (ext)   ← never touches userspace
```

Two userspace hops exist, not one:

| hop | function | thread | always in path? |
|---|---|---|---|
| ISP ×2 → VSP in[0] | `FUN_00481800` | `bc_stitch_main` | **yes** — `VIDEOPROC 2 in[0] bind_src` is `(null)` in every sample |
| VSP out[0] → VENC in[0] | `FUN_00481df0` (thunk `0x00482470`) | a `bc_avencoder` thread (I) | only when `VIDEOPROC_2_OUT_0` is unbound |

`VIDEOPROC 2 out[0]`'s bind state **changes between builds** — it read
`VIDEOENC_0_IN_0` in one sample and `(null)` in another during the same session.
The VSP *input* is never bound, so `bc_stitch_main` cannot be bypassed.

### 5.2 The loops (S, decompiled)

`FUN_00481800` — `bc_stitch_main` (`prctl(PR_SET_NAME)` at `0x00481840`):

```c
for (;;) {
    lock(mtx);
    if (!stitching_enabled) { unlock; usleep(20000); continue; }   /* 0x481904 */
    rc0 = hd_videoproc_pull_out_buf(src0, &f0, 200);               /* 0x481924: mov w2,#0xc8 */
    rc1 = hd_videoproc_pull_out_buf(src1, &f1, 200);               /* 0x481950: mov w2,#0xc8 */
    if (rc0 || rc1) { release_what_we_got(); unlock; usleep(20000); continue; }
    if (llabs(f0.ts - f1.ts) >= 30001) { ... "drop main %u.%s frame to sync" ... }
    f0.tag = 'VSPE'; hd_videoproc_push_in_buf(vsp, &f0, 0, 0);     /* 0x4819f8 */
    f1.tag = 'VSPE'; hd_videoproc_push_in_buf(vsp, &f1, 0, 0);     /* 0x481a34 */
    hd_videoproc_release_out_buf(src0); hd_videoproc_release_out_buf(src1);
    unlock;
    sched_yield();                                                 /* 0x481b14 */
    usleep(10000);                                                 /* 0x481b18 */
}
```

`FUN_00481df0` — VSP out[0] → VIDEOENC 0 in[0]:

```c
for (;;) {
    if (!enabled) { usleep(20000); continue; }                     /* 0x481e54 */
    rc = hd_videoproc_pull_out_buf(vsp_out0, &frm, 0);             /* 0x481e7c: mov w2,#0x0  NON-BLOCKING */
    if (rc) {                                                      /* -0x38 / -0x0f silently ignored */
        usleep(20000);                                             /* 0x481e54 */
        continue;
    }
    hd_videoenc_push_in_buf(venc_in0, &frm, 0, 0);                 /* 0x481f18 -> FUN_00499e80 */
    gfx_scale(&frm, w, h, &scaled);                                /* -> FUN_00480510 */
    hd_videoproc_push_in_buf(other, &scaled, ...);
    enqueue 200-byte descriptor into a std::deque
        (if full: usleep(1000) x up to 100)                        /* 0x4821e0 */
    hd_videoproc_release_out_buf(vsp_out0, &frm);
    /* NO SLEEP ON THE SUCCESS PATH -- poll again immediately */
}
```

### 5.3 Patch sites (S, verified byte-for-byte in `app_extracted/device` *and* in the running binary)

| file offset | VMA | bytes | instruction | meaning |
|---|---|---|---|---|
| `0x081904` | `0x00481904` | `00 c4 89 52` | `movz w0,#0x4e20` | `bc_stitch_main` miss sleep, 20 ms |
| `0x081b18` | `0x00481b18` | `00 e2 84 52` | `movz w0,#0x2710` | `bc_stitch_main` success sleep, 10 ms |
| `0x081e54` | `0x00481e54` | `00 c4 89 52` | `movz w0,#0x4e20` | VSP→VENC pump miss sleep, 20 ms |
| `0x0821e0` | `0x004821e0` | `00 7d 80 52` | `movz w0,#0x3e8` | queue-full retry, 1 ms |
| `0x081924`, `0x081950` | | `02 19 80 52` | `movz w2,#0xc8` | pull `wait_time` = 200 ms (blocking) |
| `0x081e7c` | `0x00481e7c` | `02 00 80 52` | `movz w2,#0x0` | pull `wait_time` = 0 (non-blocking) |
| `0x081974` | `0x00481974` | `d2 8e a6 02` | `movz x2,#30000` | stitch pairing tolerance (§4.4) |

`movz w0,#imm16` encodes as `0x52800000 | (imm << 5)`; same-length 4-byte patch.

**All three sleep sites were patched 20/10/20 ms → 2 ms and it changed nothing**
— see `FPS_CEILING.md` F1. The offsets remain correct and are recorded for
whoever needs them next; the *theory* they were patched to test is dead.

### 5.4 What the pump does *not* do

- **No pixel copy.** `hd_videoproc_pull_out_buf` = `FUN_005b65b0` issues
  `ioctl(isf_fd, 0xc020490c, &arg)` (`isf_unit_pull_data`) and copies out a
  0x128-byte **descriptor**. Push is the mirror. No bounce buffer, no `memcpy` of
  frame data — the only `memcpy`s in `FUN_00481df0` move 200-byte descriptors
  into a `std::deque`.
- **No uncached-memcpy pathology.** `device` never faults in the frame pool:
  `VmSize 1.26 GB` vs `VmRSS 15.9 MB` (at 2500 s uptime) / `41 MB` (at 400 s). A
  single 25 MB frame touched per iteration would dominate RSS.
  `hd_common_mem_flush_cache` / `_cache_sync` exist in the binary but are not on
  this path.
- **CPU is not the constraint.** `bc_stitch_main` accumulated `utime 332` +
  `stime 1923` ticks over 2501 s uptime = **0.9 % of one core**. During recording
  `top` reads **88.8 % idle** with `device` at 1.8 %. A 500 MB/s memcpy would peg
  a core.
- **OSD is not why the pump exists.** OSD is a hardware stamp applied by the
  *encoder*: `hd_videoenc_open(enc_in_id, HD_MASK(i), &mask_path[i])` +
  `HD_VIDEOENC_PARAM_IN_STAMP_{BUF,IMG,ATTR}` / `IN_MASK_ATTR`
  (`Nvt52xAdapter_osd.cpp`, strings at `0x734f40`–`0x7353f8`).
  `/proc/hdal/venc/top` reports `do_osd ≈ 100 µs` on paths **0, 1 and 3** —
  including the two that are fully kernel-bound and never enter userspace.

### 5.5 Threading and buffers

- Every `device` thread is `policy=0` (SCHED_OTHER), `nice=0`, `prio=20`,
  `rt_prio=0`. No realtime priority anywhere.
- One stitch thread. Three `bc_avencoder` threads.
- `VIDEOPROC 0/1 out[0] depth = 1` — the ISP has a **single** output buffer, so
  it cannot start frame N+1 until userspace releases frame N.
  `VIDEOPROC 2 out[0]/out[1] depth = 2`.

### 5.6 The saturation measurement (L) — this is the load-bearing one

The pump consumes a fixed ~20 frames/s at 16.589 Mpix and **does not move when
more are offered**:

| main geometry | stitcher offers | pump takes | delivered |
|---|---|---|---|
| 7680×2160 (16.589 Mpix) | 22/s | **20, drop 2** | 20.0 |
| 7680×2160, ISP eased | **23/s** | **20, drop 3** | — |
| 4096×1152 (4.719 Mpix) | 22/s | **101, drop 79** | 22.2 |

At 4.7 Mpix the pump polls 101/s and mostly finds nothing — nowhere near its
limit. At 16.6 Mpix it saturates at 20/s = `20 × 16.589` = **332 Mpix/s**.

The middle row is the cleanest single proof: easing the ISP made the stitcher
produce **23/s** and the pump still delivered exactly **20**. More supply, same
output — that is the definition of the binding stage.

---

## 6. The H.265 encoder — measured, never binding

`/proc/hdal/venc/top` reports per-frame **hardware** encode time per stream.

Session 1, two reads ~5 minutes apart, three concurrent streams (L):

| stream | codec | w × h | pixels | `hw` min (µs) | Mpix/s |
|---|---|---|---|---|---|
| 0 | H.265 | 7680 × 2160 | 16 588 800 | 35 335 | **469.5** |
| 0 | H.265 | 7680 × 2160 | 16 588 800 | 35 410 | **468.5** |
| 3 | H.264 | 2560 × 720 | 1 843 200 | 3 957 | **465.8** |
| 1 | H.264 | 1536 × 432 | 663 552 | 1 439 | **461.1** |

Session 2, `cur / min / max` across four geometries including a second H.265
size (L):

| stream | pixels | hw µs (cur/min/max) | Mpix/s |
|---|---|---|---|
| 7680×2160 H.265 | 16.589 M | 35818 / 35276 / 36554 | 463.1 |
| 4096×1152 H.265 | 4.719 M | 10116 / 9988 / 10370 | 466.4 |
| 2560×720 H.264 | 1.843 M | 4201 / 3949 / 4673 | 438.7 |
| 1536×432 H.264 | 0.664 M | 1545 / 1432 / 1773 | 429.5 |

Two codecs, four resolutions, a 25× spread in frame size, two sessions — all
within ~2 % of the same pixel rate. **≈465 Mpix/s.**

```
465e6 / 480e6 (venc_clk) = 0.97 pixels per clock  ->  1 px/clk at 480 MHz
```

`h26x@2,f0a10000` is a **single** node and `venc_clk` a **single** clock — all
three streams share one engine, so the budget is additive.

Main-stream ceiling at 7680×2160 (I, from the measured rate):

| assumption | ceiling |
|---|---|
| main stream alone | `1000 / 35.7` = **28.0 fps** |
| substreams at stock rates (~108 ms/s reserved) | `(1000 − 108) / 35.7` = **25.0 fps** |
| substreams at the *same* rate as main (41.46 ms/frame-set) | **24.1 fps** |

The spread 24.1–28.0 is entirely the substream assumption. **All three are above
anything ever delivered**, so the encoder is not the constraint and the exact
figure does not matter. At the delivered 20.05 fps the engine sits at **83 %
duty**.

Confirmed directly by experiment: shedding the sub stream from 20 fps to 4 fps
freed ~25 ms/s of engine time and changed delivered fps by **−0.07**
(20.054 → 19.983). The engine was not the constraint.

No level check, MB-rate check or resolution clamp exists in the encoder driver:
the H.26x validator surface is entirely QP/GOP/flag/`ddr_id` shaped, with no
width, height, fps or bitrate range check (S). But there **is** a hard bitrate
limit at ~20 Mbps enforced somewhere below the advertised list — see
`FPS_CEILING.md` F4.

`/proc/kdrv_vdocdc/chn_info` shows the path-0 reference buffer at 25 067 520
bytes, consistent with 7680×2160 NV12.

---

## 7. DDR

### 7.1 What is known (L)

| fact | source |
|---|---|
| **1 GiB total**, single channel (`ddr_id: 0 … size: 0x40000000 active: 1`) | `dmesg nvtmem_dram_mapping_init` |
| Linux gets 256 MiB; HDAL media pool `0x10000000`+`0x30000000` = **768 MiB** | `dmesg nvtmem_load_hdal_base` |
| `/proc/device-tree/nvt_memory_cfg/dram/reg` = `0x0 + 0x40000000` | live |
| **No DDR/DRAM clock** anywhere in the 293-entry clock tree | `/sys/kernel/debug/clk` |
| `pll3` = 333.25 MHz, `enable_count` 1, parent `osc_in`, **no children** | the only unassigned active PLL (I) |
| `0_loader.bin` / `3_uboot.bin` contain **no** printable DDR strings | (S) |

### 7.2 The measured load (L)

| main geometry | BUSY | EFF | UTI | MB/s | pump state |
|---|---|---|---|---|---|
| 7680×2160 | **90–92** | 52 | 48 | **4905–5118** | saturated, 20/s |
| 4096×1152 | **79–80** | — | — | **3732–3839** | not saturated, polls 101/s |

**`EFF 52` is the controller's efficiency**: only ~52 % of theoretical is
achievable. Against a 10.66 GB/s theoretical peak the practical ceiling is
**~5.5 GB/s**, so 5118 MB/s is **~93 % of practical**. `BUSY 92` says the same
directly, and `UTI 48 / EFF 52` = 92 % reconciles all three. Reading `UTI 48` as
"half the bus is free" is the error corrected in `FPS_CEILING.md` F3.

DDR load tracks main-stream size, which is what a throughput-bound pump predicts.

> **(I)** A 10.66 GB/s theoretical peak is consistent with a **32-bit LPDDR4-2666**
> bus (4 B × 2666 MT/s = 10.66 GB/s). That would settle the open question of bus
> width — 16-bit is indeed impossible for this traffic. But the provenance of the
> 10.66 GB/s figure is not recorded in any source doc, so this is arithmetic, not
> a reading. Confirm against the DDR controller ID registers before relying on it.

---

## 8. The ISF bind protocol

`/dev/isf_flow0`, handled by `isf_flow_drv_ioctl` (`kflow_common.ko`).

| cmd | `_IOWR` | arg | driver call |
|---|---|---|---|
| `0xc00c4901` | `('I',1,12)` | 12 bytes | `isf_unit_set_bind` |
| `0xc00c4902` | `('I',2,12)` | 12 bytes | `isf_unit_get_bind` |
| `0xc020490c` | `('I',12,32)` | 32 bytes | `isf_unit_pull_data` |

The 12-byte argument, read straight off the dispatch — `copy_from_user(sp+0x40,
arg, 12)` then `ldp w1, w2, [sp, #68]` before `bl isf_unit_set_bind`:

```c
struct { u32 ret_slot; u32 src_path_id; u32 dst_path_id; };
```

`isf_unit_set_bind(minor, src, dst)` splits each path_id as
`unit = id>>16, port = id & 0xffff` and enforces direction:

| operand | required port class | branch |
|---|---|---|
| `src` | **out**, `port < 0x80` | `tst w3,#0xff80` → `b.eq` accept |
| `dst` | **in**, `port − 0x80 <= 0x7f` | accepted |

So a bind is strictly `some_unit.out[n] -> other_unit.in[m]`.

**The decode is confirmed, not assumed.** `get_bind` on all four encoder inputs,
before touching anything (L):

| path | `get_bind` result | `/proc/hdal/venc/info` says |
|---|---|---|
| `0x01110080` in[0] main | `0x00000000` | `bind_src (null)` |
| `0x01110081` in[1] sub | **`0x00940000`** | `bind_src VIDEOPROC_3_OUT_0` |
| `0x01110083` in[3] ext | **`0x00940001`** | `bind_src VIDEOPROC_3_OUT_1` |
| `0x01110087` in[7] jpeg | `0x00000000` | `bind_src (null)` |

The two bound ports read back exactly the unit/port `/proc` names independently,
confirming both the 12-byte layout and the `0x91+N` VideoProc unit-id mapping.
in[0] and in[7] are genuinely unbound — `device` pumps those two in userspace.

Client: `runtime/isfbind.c` — freestanding aarch64, no libc, 4.3 KB, built with
`aarch64-linux-gnu-gcc -O2 -nostdlib -static -ffreestanding`. Pushed to
`/mnt/tmp`, which is tmpfs, so a restart removes it and any bind it made — the
experiment is inherently reversible.

`device` does **not** materialise these ioctl numbers anywhere a static search
can reach:

| searched for | in `device` | in `libbase.so` |
|---|---|---|
| `movz w?, #0x490b/0x490c/0x4901/0x4905/0x490a` | **0 sites** | n/a |
| `movk w?, #0xc020/#0xc00c, lsl #16` | **0 sites** | n/a |
| 32-bit literals `0xc020490b` etc. | **0** | 0 |
| string `isf_flow0` | 1 | — |

`device` opens `/dev/isf_flow0` and `LD_PRELOAD` traces prove it issues
`0xc0204905` at runtime, so the command numbers are **computed** — an `_IOWR`
built from a variable `nr`, not folded to a constant. HDAL is statically linked
into `device` (no `hd_*` dynamic symbols). Locating the pump therefore means
recovering the ioctl dispatch by dataflow through a 4.9 MB stripped C++ binary,
which is `APP_REPLACEMENT_DESIGN.md`, not a patch.

---

## 9. Operational notes

### 9.1 The ISE scaler caps at 16× downscale

```
ERR:gximg_scale_by_ise() scale factor over 16, SrcW=7680,SrcH=2160,DstW=320,DstH=180
ERR:gfx_scale() scale fail
```

7680/320 = 24×, over the limit. `device` then loops on `gfx_scale()` failures
and **never finishes start-up: no nginx, no port 80, no HTTP API**, so
`flash_pak.py` cannot reach the camera. The stock table's smallest entry,
480×136, is exactly 7680/16 × 2160/16 — the limit itself.
`build_fps_demo.sh` refuses any aux geometry needing more than 16× and states
the minimum; self-tested, 320×180 is rejected before the build runs.

### 9.2 Restoring the HTTP API by hand when `device` stalls before nginx

Worth keeping — this is why the above cost minutes rather than a UART cable. The
2323 root shell was up throughout. nginx was not running because `device` never
got far enough to write `/mnt/tmp/nginx.conf`, but the template lives at
`/mnt/app/nginx_conf/conf/nginx.conf` with `_HTTP_PORT_` placeholders, and
`cgiserver.cgi` was already listening on `127.0.0.1:9527`:

```sh
mkdir -p /mnt/tmp/run /mnt/tmp/logs /mnt/tmp/download
cp /mnt/app/nginx_conf/conf/mime.types /mnt/tmp/mime.types
sed -e 's/_HTTP_PORT_/80/' -e 's/_HTTPS_PORT_/443/' -e 's/_RTMP_PORT_/1935/' \
    /mnt/app/nginx_conf/conf/nginx.conf > /mnt/tmp/nginx.conf
/mnt/app/nginx -p /mnt/tmp/ -c /mnt/tmp/nginx.conf
```

Port 80 comes up, the API answers, and a normal reflash recovers the camera.

### 9.3 The pak filename matters

`UpgradePrepare` returns `check err` (rspCode −3) for a file not named
`IPC_NT15NA416MP.<build>_<date>.Reolink-Duo-3-PoE.16MP.REOLINK*.pak`. This is
**not** a busy-camera condition and rebooting does not help; the flash tool's
"still busy, reboot and retry" advice is misleading here.

### 9.4 Geometry changes restart the pipeline

Changing main-stream geometry or frame rate restarts the video pipeline and
drops the shell and RTSP for ~15–30 s. Wait before sampling.

### 9.5 Counter semantics differ by proc file

- `/proc/hdal/*/info` counters are **rolling per-second rates** that self-reset
  on read.
- `/proc/kflow_sie/info` accu counters are **cumulative**.

Mixing the two up will produce nonsense. Sample the cumulative ones over a known
wall-clock window.
