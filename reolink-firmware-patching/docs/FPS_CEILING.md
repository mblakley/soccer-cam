# What limits the Duo 3 PoE's frame rate — the answer

Investigation 2026-08-15 → 2026-08-17, camera `192.168.86.24`, firmware base
`IPC_NT15NA416MP.4867_2505072124`. This document supersedes four working
documents written during the investigation and reconciles the places where they
disagreed. Per-stage readings, register values and offsets live in
[`FPS_STAGE_EVIDENCE.md`](FPS_STAGE_EVIDENCE.md).

---

## The result

**Best delivered frame rate achieved: 21.179 fps recorded / 21.218 fps RTSP at
7680×2160**, and **23.395 / 23.383 at 4096×1152.** Sustained, not a burst — a
4-minute full-resolution RTSP run held 21.385 fps with a window-to-window spread
of 0.058 fps and no decay. The stream is usable by a downstream tracker.

The camera as found was configured for 21 fps and **delivered 19.856**. Net
movement at full resolution across the investigation: **19.86 → 21.18 (+6.6 %)**.

> **The "21 fps" in the original question was never a delivered rate.** It is a
> policy value in Reolink's encoder capability table, and it is what the MP4
> header reports. **Every clip this camera writes carries the *requested* rate
> regardless of what was encoded** — a clip that says `25/1` can hold 19.98 fps
> of pictures. All numbers in this document are frame-count-over-PTS-span,
> measured with `verify/measure_clip_fps.py`. Never quote `r_frame_rate` from
> this camera as evidence of anything.

### What limits it

Two stages bind, in this order at 7680×2160. **Neither is a hardware engine's
rated throughput, which is why a stage-by-stage capability analysis missed both.**

1. **The userspace frame pump in `device`** (~332 Mpix/s → ~20 fps at
   16.589 Mpix). `VIDEOENC 0 in[0]` is the only video path on the camera that is
   **not kernel-bound**, so `device` moves each stitched frame itself. Offer it
   more and it takes the same number: with the stitcher producing 23/s it still
   delivered exactly 20. **(L)**

2. **One shared IPP (ISP) engine at 99 % usage, serving three VideoProc clients.**
   It starves `VIDEOPROC 1` — the second sensor's pipe — which drops 3–4 of every
   25 frames at its kernel input, so the stitcher only ever makes 22–23 pairs/s.
   Client split: `vdoprc0` 25.00/s + `vdoprc1` 21.73/s + `vdoprc3` 19.97/s =
   66.71, matching the engine's own `fps 66`. **(L)**

**Not limiting, all measured:** the sensor (25.00 fps, zero drops), capture/SIE
(25/s, zero drops), the H.265 encoder (**≈465 Mpix/s** — 28.0 fps for the main
stream alone, 83 % duty at the delivered rate), MIPI, or the bitrate.

### What would have to change to go faster

| target | what it needs | status |
|---|---|---|
| past ~21 at 7680×2160 | finish freeing the ISP — kill `VIDEOPROC 3`'s **outputs**, not just its input. Needs the API/`enc.cfg` size-code path; the `device` descriptor table reaches the input only. | open, next step |
| past ~22.5 | replace the pump. Needs `device` replaced, not patched — it computes its ioctl numbers at runtime, so no static search reaches the pump loop. | `APP_REPLACEMENT_DESIGN.md` (**but see §6**) |
| past ~25 | retime the TGE (`builds/build_tge_retime.sh`, built, sites verified, **not flashed**) | gated on the ISP first — §5 O3 |
| past ~26.8 at 2160 rows | **impossible.** The sensor needs 37.36 ms to read out 2162 rows; no TGE setting beats that. | hard wall |
| 60 fps | only by cutting rows, and only *after* the pump and ISP are gone. The encoder prices vertical FOV at `fps ≈ 54100/rows`, but the encoder is not what you are up against today. | not reachable now |

**Was 25 fps at 7680×2160 achieved? No — and it is not reachable on the shipped
pipeline.** A static analysis predicted it was. That prediction was wrong; §3
says exactly how.

### Confidence tiers

| tier | meaning |
|---|---|
| **S** | static — read out of the shipped binary; file + symbol/offset given |
| **L** | live — measured on the running camera |
| **I** | inferred — the arithmetic is shown and the assumption is named |

---

## 1. The measured record

Capture 25 fps throughout unless noted. Both paths measured; container headers
ignored. Full stage readings in `FPS_STAGE_EVIDENCE.md`.

| # | build / change | main geometry | recorded | RTSP | IPP use/fps | VPRC1 IN drop | DDR BUSY | bound by |
|---|---|---|---|---|---|---|---|---|
| 1 | as-found, capture 21 | 7680×2160 | **19.856** | — | — | — | — | pump |
| 2 | + `constantFrameRate=1` | 7680×2160 | 19.899 | — | — | — | — | pump (exposure excluded) |
| 3 | 4910, capture 25 | 7680×2160 | 20.054 | 20.136 | 99 / 66 | 3 | 92 | pump |
| 4 | capture **28** requested, sub shed | 7680×2160 | 19.983 | — | — | — | — | sensor clamps 28→25, then pump |
| 5 | 4913, pump sleep 20 ms → 2 ms | 7680×2160 | 20.013 | 19.964 | 99 / 66 | 3 | 91 | pump — **unchanged, see F1** |
| 6 | 4910 restored | 7680×2160 | 20.004 | 20.041 | 99 / 66 | 4 | 92 | pump |
| 7 | **4915 `aux640`** (first meas.) | 7680×2160 | 21.167 | 21.095 | 99 / 68 | 3 | 90–91 | pump |
| 8 | **4915 `aux640`** (final) | **7680×2160** | **21.179** | **21.218** | 99 / 68 | 3 | 90–91 | pump |
| 9 | 4910 | 4096×1152 | 22.170 | 22.179 | 99 / 66 | 3 | 79–80 | ISP starvation |
| 10 | 4912, sensor retimed to 26 | 4096×1152 | 22.208 | — | — | — | — | ISP — **retime bought nothing, F5** |
| 11 | **4915 `aux640`** | **4096×1152** | **23.395** | **23.383** | **79 / 53** | 3 | 78–79 | ISP starvation |
| 12 | runtime kernel bind (no flash) | 7680×2160 | — | 24.240 raw / **22.80 distinct** | — | — | — | stitcher — **restarts in ~4 min, F8** |

Rows 7 and 8 are the same build measured on different occasions; the 0.012 fps
difference is inside the observed window spread.

**Sustained stability at 7680×2160 on build 4915:**

| run | result |
|---|---|
| 4 min, 5133 frames, 30 s windows | **21.385 fps**, spread **0.058**, no decay |
| 90 s, 30 s windows | 21.261 / 21.221 / 21.190, spread **0.071** |

---

## 2. Where each stage tops out

```
sensor 0 --25.0--> SIE 0 --25.0--> VIDEOPROC 0 --25.0--\
                                                        >-- VSP --22..23--> [pump ~20..21] --> VENC
sensor 2 --25.0--> SIE 2 --25.0--> VIDEOPROC 1 --21.7--/
                                        ^ 99%-busy shared IPP starves this one
```

| stage | sustains at 7680×2160 | tier | binding? |
|---|---|---|---|
| Sensor | 25.00 fps (`chgmode_fpsx100 2500`, VD 40.02 ms) | L | no |
| Sensor, physical ceiling (TGE permitting) | 26.77 fps (2162-row readout = 37.36 ms) | I | no |
| `VIDEOCAP 0`/`2` out | 25/s, drop 0 | L | no |
| Per-sensor ISP `VIDEOPROC 0` | 25/s, drop 0 | L | no |
| **ISP `VIDEOPROC 1`** (shared IPP) | **21.7/s, drop 3–4 of 25** | **L** | **binds — 2nd** |
| VSP stitcher `VIDEOPROC 2` out | 22–23/s, drop 0 | L | follows its weaker input |
| **Userspace pump → `VIDEOENC 0` in[0]** | **~332 Mpix/s → ~20/s** | **L** | **binds — 1st** |
| H.265 encoder | ≈465 Mpix/s → 28.0 fps main alone; 83 % duty at 20 fps | L | no |
| MIPI CSI-2, 2 lanes of 4 | ~26.0 fps | I | no |
| DDR | 5118 MB/s = **~93 % of practical** ~5.5 GB/s | L | **near its ceiling — see F3** |
| Bitrate | hard encoder limit in (20, 21) Mbps | L | no (not a rate limit) |

At 4096×1152 (4.719 Mpix) the pump would allow ~70 fps, so it stops binding and
the ISP alone sets the rate — which is why that geometry is faster.

---

## 3. The contradiction: static analysis said 25 fps, the camera does 21

The first document written in this investigation, `FPS_BOTTLENECK.md`, was a
static + read-only-live analysis that priced every hardware stage and found four
independent limits between 25 and 27 fps. It concluded a **realistic ceiling of
25 fps at 7680×2160** and opened with "the premise is wrong, and the answer is
better than 21".

**Verdict: the prediction was wrong. It was not right-but-blocked.** There is no
configuration of the shipped pipeline that reaches 25 fps at full resolution.

**But its hardware numbers were not wrong** — nearly every one was later
confirmed by direct measurement:

| `FPS_BOTTLENECK.md` claim | later measurement | verdict |
|---|---|---|
| PCLK is 150 MHz, not 120 | `/proc/kflow_sen/info` `pclk 150000000`; mode table → 149.95 MHz | **confirmed (L)** |
| H.265 encoder ≈466 Mpix/s | re-measured 463.1 / 466.4 / 438.7 / 429.5 over 4 geometries, 2 codecs, 2 sessions | **confirmed (L)** |
| Sensor mode clamps hard at 25.00 | request 28 → `fpsxbase (2500,2500,2500)`, VD 40.0 ms | **confirmed (L)** |
| Sensor full-height ceiling 26.7 fps | 26.76 (zero vblank at 2162 rows); TGE analysis independently 26.77 | **confirmed (S+L)** |
| 21 fps is a policy value, not a sensor wall | true — and understated: the camera never delivered 21 either | **confirmed (L)** |
| "Realistic ceiling 25 fps at 7680×2160" | measured best **21.18** | **WRONG** |
| DDR not binding, ≥2.5 GB/s | 5118 MB/s = ~93 % of practical | **WRONG — F3** |
| ISP fourth in line at ~27.1 fps, not binding | IPP saturated at usage 99, starving `VIDEOPROC 1` | **WRONG** |

### Why it missed, and it is the same reason twice

Its method was to price each **hardware engine** and rank the results. Both real
limits are invisible to that method:

1. **The userspace pump is not a hardware engine.** It is `device` moving frames
   between two kernel units that are not bound to each other. Nothing in a driver
   limit table, the clock tree, or `/proc/hdal/*/info`'s rate columns exposes it.
   It only appears when you **diff what a stage produces against what the next
   stage takes** — `VIDEOPROC 2 OUT 22–23/s` against `USER PULL 20, drop 2–3`.
   The read-only session read capability tables; it never diffed producer against
   consumer.

2. **The ISP was priced as one engine doing one job.** It modelled the ISP as
   processing this pipeline's 16.59 Mpix/frame once, at an assumed 1 px/clk
   → 27.1 fps. In fact **one IPP engine serves three VideoProc clients**, and the
   third — the sub/ext splitter `vdoprc3` — eats 19.97 of the 66.71 jobs/s it
   delivers. The substreams' cost was charged to the *encoder* budget and never
   to the ISP budget at all.

Both misses share a root cause: **capability tables describe engines, not
contention.** The lesson for the next investigation of this kind is to measure
producer-vs-consumer at every hop before ranking any engine's rated throughput.

### The near-miss worth keeping

`FPS_BOTTLENECK.md` recommended killing the substreams — "worth 12 % of the
encoder budget, free". **Right lever, wrong mechanism, and the mechanism is what
tells you how hard to pull:**

- Shedding substream **frame rate** (20 → 4 fps) freed ~25 ms/s of *encoder*
  time and moved delivered fps by **−0.07**. The encoder route buys nothing,
  because the encoder was never binding.
- Starving substream **input geometry** (2560×720 → 640×192), which cuts *IPP*
  work, bought **+1.18 fps** — the only change in the whole investigation that
  moved full-resolution delivery.

---

## 4. Superseded and falsified claims

Kept deliberately. Each of these cost real time, and a later reader who does not
know they were tried will try them again.

### F1 — "The pump's 20 fps is set by a hardcoded 20 ms poll-miss sleep" — FALSIFIED BY EXPERIMENT

*Source: `FRAME_PUMP.md`, its entire thesis.*

**The theory.** `FUN_00481df0` pulls non-blocking and `usleep(20000)`s on every
miss, quantising the frame period to `k × 20 ms + w`. 16.6 Mpix lands in the k=2
bin (≈50 ms → 20 fps), 4.7 Mpix in the k=1 bin (≈21 ms → 48 fps) — which would
explain why the two apparent "pixel rates" differ by 47 %, and why requesting
21 / 25 / 28 fps all deliver ~20.

**The test.** All three `usleep` sites verified byte-for-byte **in the running
binary** first, patched 20/10/20 ms → 2 ms, and confirmed live after flashing
(`00 fa 80 52` at all three).

**The result.** Delivered fps moved **−0.04 recorded and −0.17 RTSP.** Nothing.

**Independent disconfirmation.** If a fixed sleep set the period, inter-frame
deltas would cluster tightly around 50 ms. They do not, before or after:

| clip | mean delta | sigma | histogram |
|---|---|---|---|
| baseline | 49.66 ms | **17.33 ms** | 40 ms ×198, 41 ×75, 80 ×57, 39 ×55 |
| pump 2 ms | 49.92 ms | **17.05 ms** | 40 ms ×153, 39 ×68, 41 ×65, 80 ×45 |

The distribution is **bimodal on the 40 ms capture grid** (take a frame / skip a
frame), not a 50 ms period with small jitter. Sigma is 17 ms, not sub-ms.

**Cost:** 2 of 6 permitted flashes (one to test, one to revert).

**What survives from `FRAME_PUMP.md`** and is kept in `FPS_STAGE_EVIDENCE.md` §5:
the pump's structure, the loop decompilations, all patch-site offsets (still
correct), the finding that **`device` performs no pixel copy** (descriptor-only
ioctl, `VmRSS` 16–41 MB against `VmSize` 1.26 GB, `bc_stitch_main` at 0.9 % of one
core), and that OSD is composited by the encoder, not the pump.

**Unresolved tension this leaves — see O1.** The pump is throughput-bound, but
`device` does no copying. Those are both measured, and nothing yet reconciles
them.

### F2 — "Dual-sensor frame pairing fails, passing ~45 of 50 frames" — FALSIFIED BY MEASUREMENT

*Source: `FPS_DEMO_RESULTS.md` §2/§6 and `FPS_MAIN_BIND.md` §8.*

**The theory.** The two sensors' frame counters are offset, the ISP discards the
unmatched frames, and the stitched stream is capped at ~22.5 pairs/s. Cited
`isp_dev_get_sync_item: sync info not match` flooding `dmesg`.

**Falsified.** The sensors are **exactly synchronised** and cannot be otherwise:

- Both deliver **1500 VD in 60.04 s**, `new_fail`/`drop`/queue-full all zero, a
  constant VD offset of 7 (a start-time artefact) and **zero drift over 1500
  frames**. (L)
- They are the same clock. Both report `tge_vdhd 0x1` and byte-identical
  `tge_signal`; the TGE's four VD generators share one enable register and one
  LOAD strobe. TGE "phase" is rising/falling edge only — **there is no
  phase-offset register**. Software re-alignment is refused outright while
  `tge_en=1`, and there is no FSIN pin.

**`isp_dev_get_sync_item` was misread.** It is a **24-slot per-frame 3A/IQ
parameter ring**, not a pairing function. A mismatch means *"the metadata for
this frame is ≥24 frames stale"*; the caller goes without it. Callers are
`ae_flow_auto_process`, `awb_flow_process` (3 sites) and **`iq_flow_process`
(50 sites)** — one late IQ pass emits up to 50 lines, so the "flood" wildly
over-reports. It is a **symptom of the 3A/IQ threads running late, not a cause of
lost frames.**

**Real pairing** is in userspace (`bc_stitch_main`, ±30000-tick timestamp window
at `device` offset `0x81974`) and is **not** the limiter: the lost frames are
refused at `VIDEOPROC 1`'s *kernel input*, before any timestamp is compared.
Widening the window would buy nothing and would tear the seam on motion.

**The count was right, the mechanism was not:** `vdoprc0` 25.00 + `vdoprc1`
21.73 = **46.7 of 50**.

> **Obsolete advice — do not follow it.** `FPS_MAIN_BIND.md` §9 recommended
> *"fix the dual-sensor pairing first (it caps everything at 22.5 and may be a
> configuration problem)"*. **There is nothing to fix.** The 22.5 ceiling is IPP
> saturation, and the fix is to shed IPP load.

**This correction earned its keep by predicting.** The sensor-sync analysis
proposed shedding the `vdoprc3` IPP load and predicted IPP usage would fall off
99. Done the next session by starving `VIDEOPROC 3`'s input: at 4096×1152 IPP
went **99/66 → 79/53** and delivered fps rose **+1.22**. Falsifiable prediction,
made in advance, held.

### F3 — "DDR is not the binding constraint; half the bus is free" — CORRECTED

*Source: `FPS_BOTTLENECK.md` §6, and the reading of the raw counters.*

The counters read `BUSY 92, EFF 52, UTI 48, 5118 MB/s`. `UTI 48` against a
10.66 GB/s theoretical peak was read as "48 % used, half the bus free".

**Wrong. `EFF 52` is the controller's own efficiency figure** — only ~52 % of
theoretical is achievable. The practical ceiling is **~5.5 GB/s**, so 5118 MB/s
is **~93 % of it**. `BUSY 92` says the same directly, and `UTI 48 / EFF 52` = 92 %
reconciles all three readings.

`FPS_BOTTLENECK.md`'s own traffic estimate (~118 MB/frame → ~2.5 GB/s at 21 fps)
was also low by roughly 2× against the measured 5118 MB/s at 20 fps.

DDR tracks main-stream size exactly as a throughput-bound pump predicts:

| main geometry | DDR | pump state |
|---|---|---|
| 7680×2160 | BUSY 90–92, 4905–5118 MB/s | saturated, 20/s |
| 4096×1152 | BUSY 79–80, 3732–3839 MB/s | not saturated, polls 101/s |

**So DDR is not comfortably clear** — it is near its effective ceiling precisely
when the pump is stuck, and it relaxes when the pump is not. Whether DDR
bandwidth *is* the pump's binding resource or merely correlates with it was not
separated: see **O1**.

### F4 — "Raising the bitrate ceiling to 40 Mbps is free" — REJECTED BY HARDWARE, AND ALREADY KNOWN

The prediction that it would be free was sound and still holds: bitstream write
is ~2.5 MB/s against ~4900 MB/s of DDR frame traffic, and ISP and encoder are
billed per pixel, not per bit. Measured either side, frame rate and DDR are
identical (21.17 fps, BUSY 90–91 both ways).

It simply is not a bandwidth question. `SetEnc bitRate=40960` →
**`rspCode -13`, "set config failed"**. There is a separate hard limit inside the
encoder at ~20 Mbps, enforced below the advertised list.

> **This was already recorded and a flash was spent rediscovering it.**
> `FIRMWARE_PATCH_NOTES.md` §"Patches v12-v14" documents the binary search:
> 16384 ✓, **20480 ✓**, 21504 ✗, 22528 ✗, 24576 ✗ — ceiling in (20, 21) Mbps.
> The `-13` reproduces it exactly. **Read the patch notes before spending a flash.**

**Consequence that matters for build selection:** the ceiling patch **replaces**
the 20480 list entry rather than adding to it, so the 40960 build advertises
`[… 10240, 40960]` and can select **neither** — its maximum selectable is 10240.
The 20480 build is the correct daily driver.

### F5 — "Retiming the sensor's VTS raises the capture rate" — NO EFFECT

Both sensor drivers were retimed to `dft_fps 2600 / min_vd 2225`. It genuinely
took, at the driver (`fpsxbase (2600,2600,2600)`) **and at the chip**
(`i2c r_reg 0x380e/0x380f -> 0x08b1` = 2225, was 2314).

The frame rate did not move: VD averaged 40.35 ms = **24.79 fps**, delivered
22.208 against 22.170 unretimed — noise.

**Because `tge_en = 1`.** The SoC timing generator drives VD and both sensors are
slaves. Shortening the sensor's own VTS just makes it finish its readout earlier
inside a frame period the TGE still sets at ~40 ms.

The correct lever is the TGE's `hd_period` — `builds/build_tge_retime.sh`, and
see O3 for why it was not flashed.

### F6 — "Aux geometry can be shrunk arbitrarily" — BROKE THE APP LAYER

The first attempt used 320×180. 7680/320 = 24×, over the ISE scaler's hard **16×
downscale limit**:

```
ERR:gximg_scale_by_ise() scale factor over 16, SrcW=7680,SrcH=2160,DstW=320,DstH=180
ERR:gfx_scale() scale fail
```

`device` then loops on the failure and never finishes start-up: **no nginx, no
port 80, no HTTP API**, so `flash_pak.py` could not reach the camera. The stock
table's smallest entry, 480×136, is exactly 7680/16 × 2160/16 — the limit itself.

`build_fps_demo.sh` now **refuses** any aux geometry needing more than 16× and
states the minimum; self-tested, 320×180 is rejected before the build runs.

Recovery took minutes rather than a UART cable because the 2323 root shell was
up and nginx can be started by hand from its template — recipe in
`FPS_STAGE_EVIDENCE.md` §9.2.

### F7 — `FIRMWARE_PATCH_NOTES.md` §15's sensor timing — SUPERSEDED

§15 inferred `PCLK = 20 × 2592 × 2314 = 119.96 MHz` ("exactly 120 MHz") from a
**delivered** 20.00 fps, and concluded `120e6 / (2592 × 2170)` = **21.33 fps** is
the hard full-height ceiling.

**Delivered is not capture.** `/proc/kflow_sen/info` reports `pclk 150000000` and
the mode table multiplies out to 149.95 MHz three independent ways. The
full-height sensor ceiling is **26.7 fps, not 21.3**. §15 also verified its 21 fps
result from `r_frame_rate = 21/1` — the one field on this camera that cannot be
trusted.

> **Note:** the correct 150 MHz figure was **already in the same file**, at the
> caveats block near line 128 (*"pclk = 150 MHz … max sensor fps at 4K is
> ~26.7 fps"*). §15 later contradicted it without noticing. The file disagreed
> with itself for the whole investigation.

§15's **remedy** — windowed sensor readout — is still the right remedy, for a
different reason: it helps because it cuts ISP load, not because the sensor is
slow.

Also retired: the §11 bullet stating `sen_chg_fps_os08c10` has *"no hardcoded
clamp"*. The clamp is real; it lives one call down, in
`sen_calc_chgmode_vd_os08c10`, and it is what silently resets any request above
25.00 fps.

### F8 — "The kernel bind is the fix" — WORKS, RAISES FPS, NOT SHIPPABLE

The bind is issuable at runtime with **zero flashes**, is accepted (`rc=0`), and
`/proc` immediately shows `bind_src VIDEOPROC_2_OUT_0`. Delivered rose
20.136 → 24.240 raw.

**The raw figure is inflated.** 36 of 607 packets arrive ~1 ms after their twin,
because `device`'s pump never stopped and both producers now feed the same
encoder input. Distinct delivered = **22.80 fps** — exactly the stitcher's output
rate, which is the correct result of moving the wall one stage upstream.

**Nothing functional regressed:** OSD timestamp, watermark and camera name all
survive, snapshots work, the stitch seam is clean. OSD is composited by the
encoder from `IN_STAMP_IMG`, which the bind does not touch.

**What breaks is the camera.** It restarts within ~4 minutes, reproducibly, *even
fully idle with no recording and no RTSP* — buffer reference/release accounting
on that port driven by two owners. Recovery is free: `/mnt/tmp` is tmpfs, so the
restart reverts the bind.

**Do not leave it bound unattended.** Making it permanent requires `device` to
stop pumping, which is not a binary patch — see §6.

---

## 5. Open questions

**O1 — Why exactly 332 Mpix/s?** The pump is throughput-bound; that is measured
and is the cleanest result in the investigation (stitcher offers 23/s, pump takes
20). But the *mechanism* is not located. `device` performs no pixel copy (F1), so
any copy is on the kernel side of `isf_unit_pull_data`/`push_in_buf`; DDR is
simultaneously at ~93 % of practical (F3). Whether DDR bandwidth is the binding
resource or merely correlated was **not separated**. Deciding it needs per-master
DDR accounting, or a pump-rate test at fixed geometry with DDR load varied
independently.

**O2 — The remaining aux kill.** `VIDEOPROC 3`'s out[0]/out[1] did **not** move
with the `device` descriptor table — they stayed 1536×432 and 2560×720 because
those sizes come from the API/`enc.cfg` size codes (70 and 81). VideoProc 3 now
upscales and the encoder still pays for them; sub and ext are correspondingly
soft, which was accepted. **Finding that size-code path is the next +fps at full
resolution.**

**O3 — TGE retime, built but not flashed.** `builds/build_tge_retime.sh` is
complete and its four patch sites are verified; `hd_period` 415 → 396 gives
26.15 fps capture. Deliberately not flashed: **capture is already the least
constrained stage** (25/s, drop 0) while the ISP drops 3–4 of 25. Feeding 26.15
into a saturated ISP produces more drops, not more frames. Do it **after** the
ISP is freed, **in daylight** (AE reprograms `hd_period`, so a night test is
uninterpretable), with someone able to power-cycle.

> ⚠️ **`build_tge_retime.sh` does not carry the boot-chain SHA-256 assertion**
> that `build_fps_demo.sh` and `build_roi_qp.sh` do. It hard-fails on an
> out-of-range `hd_period`, but it does not verify `loader`/`fdt`/`atf`/`uboot`/
> `kernel`/`ai` are byte-identical to the base pak. **Back-fill that guard before
> flashing it** — see `ENCODER_ROI_QP.md` §9.

**O4 — ISP pixels per clock** is still unmeasured; the 27.1 fps figure assumed
1 px/clk. Now moot for *ranking* (the ISP is measured saturated by client count,
not by pixel rate) but it would firm up the headroom estimate after O2.

**O5 — Encoder ceiling, exact value.** ≈465 Mpix/s gives 28.0 fps for the main
stream alone, 25.0 with substreams at stock rates, 24.1 with substreams at the
main stream's rate. The spread is entirely the substream assumption. All three
are far above anything delivered, so it was never worth pinning down.

### Closed since the investigation

**The 2-D warp mesh `get` path.** During the investigation `lut2d_ioctl get`
returned an all-zero table while the driver held a real mesh, so `set` was
correctly **not** attempted — writing that dump back would have blanked a live
warp rather than being the no-op the validation gate requires. **This is now
fixed on `main`**: the mesh dimension `n` belongs in `buf[1]`, and the reader is
confirmed live at 66 049 / 66 049 non-zero control points. See `vpe/README.md`.

> Two details from that episode are worth carrying, because both are general.
> `lut2d.py selftest` reported **17/17 passing on a file containing nothing** — an
> all-zero table satisfies every *structural* invariant (size, padding,
> round-trip, quarter-pixel exactness) by construction. The suite now checks
> **liveness first** and reports 19 gates. And the driver returned
> `align4(0) * 0` entries **with a success code**: a zero-length success is not a
> success.

---

## 6. A referral that does not yet exist

Three of the source documents send the pump work to
`docs/APP_REPLACEMENT_DESIGN.md`. **That document does not currently cover it.**
It scopes replacing `device` as a *pipeline configurator* — the ISF `'I'` ioctl
family, IQ blob hand-off, staged `LD_PRELOAD` trace-and-replay migration. The
words "pump", `pull_data` and `push_data` do not appear, and there is no
treatment of a frame-delivery loop. Its closest acknowledgements are an
"Unclear" row for pre-stitch frame access and an open unknown about whether
`nvtmpp` buffer pools can be driven from a non-`device` process — which is
exactly the question the pump replacement turns on.

Also note `APP_REPLACEMENT_DESIGN.md`'s frame-rate row still uses the retired
120 MHz model (`fps = 46296/(rows+8)`, "21 fps hard ceiling"). With the correct
57 870 lines/s the relation is `fps = 57870/(rows+8)`, and the ceiling is set by
the pump and ISP, not by VTS.

**Removing the pump needs that design extended first.** It is not a binary patch:
`device` does not materialise the ISF ioctl numbers anywhere a static search can
reach — 0 sites for the `movz`/`movk`/32-bit-literal forms — because they are
computed from a variable `nr` at runtime. Locating the pump loop means recovering
the ioctl dispatch by dataflow through a 4.9 MB stripped C++ binary.

---

## 7. Build numbers in this repo are not unique — check before you flash

**`4915` and `4916` each name two different paks.**

| number | in this document | in `BRICK_POSTMORTEM.md` |
|---|---|---|
| 4913 | pump-sleep patch (F1) | — |
| 4914 | — | pre-`ioctllog` baseline |
| **4915** | **`aux640`, the fps daily driver** | **first build carrying `ioctllog.so`** |
| **4916** | **bitrate ceiling 40960 (rejected)** | **`ioctllog.so` with pointer-following** |
| 4917 | — | the brick |

`BUILD_LOG.md` logs neither series — it stops at 4906. `FPS_BOTTLENECK.md` also
referred to "builds 4913/4914" failing an `ime_path_adj` geometry check, which
predates this investigation's own 4913.

**"Leave the camera on 4915" in this document means the fps `aux640` build from
`build_fps_demo.sh`.** Cite the change, not the number, and log new builds in
`BUILD_LOG.md`.

---

## 8. Camera state as left

| item | state |
|---|---|
| Firmware | **build 4915** (`aux640`): capture 25, aux inputs 640×192, bitrate ceiling 20480 |
| Sensor | stock drivers, retime reverted — `chgmode 2500`, VD 40.02 ms |
| Encoder | main 7680×2160 h265, sub 1536×432 h264 |
| Recording | **disabled**, netstate daemon in control, no `/mnt/sda/netstate/override` |
| Root shell | `S36_RootShell` on 2323, running |
| ISP | `constantFrameRate` restored to 0 (as-found) |
| Bind | cleared (runtime-only; reverted by restart) |
| Left on card | `/mnt/sda/fpsdemo/*.mp4`, ~25 MB of test clips |

Every flashed pak passed `verify/check_recording_default.sh` and the boot-chain
SHA-256 assertion before being written.

---

## 9. Tools

| file | purpose |
|---|---|
| `verify/measure_clip_fps.py` | **the measurement every number here rests on** — frame count over PTS span, reporting the container header alongside so a claimed-vs-delivered gap is visible |
| `builds/build_fps_demo.sh` | the builder that produced 4910–4916. Capture-fps + advertised-list + aux-geometry patches; refuses >16× ISE downscale; asserts the boot chain is SHA-256 identical to base and aborts if not |
| `builds/build_tge_retime.sh` | TGE `hd_period` retime, 387..415 enforced. **Not flashed** — see O3, and back-fill the boot-chain guard first |
| `runtime/isfbind.c` | freestanding aarch64 ISF bind/get client, no libc. Reproduces F8 |
| `runtime/camsh.py` | `recording_override_held()` — the one narrow, named capability for asserting the record-at-home flag while capturing a sample, so the general refusal stays enforced instead of being reworded around. A context manager, not a pair of calls: it releases in `finally` and then tests for the flag's *absence*, because `rm -f` exits 0 on a path it did not remove |

**Known gap in the override tooling.** `recording_override_held()` guarantees
release against an exception, but not against a hard kill of the host process
between hold and release — that leaves the camera recording at home with nothing
to notice it. Closing that needs a camera-side self-expiring watchdog (assert the
flag, and have the camera itself drop it after N minutes). The obvious
implementation, backgrounding a `sleep N; rm -f` under `nohup`, has **not** been
verified to survive `tcpsvd` closing the connection on this busybox build, so it
is deliberately not shipped rather than shipped untested — an expiry believed
armed and actually absent is worse than none. Until then: the flag is only ever
held inside the context manager, and `verify/check_recording_default.sh` keeps
the far more dangerous case, a *build* that asserts it at boot, impossible.

### Reproducing the headline measurement

```bash
# Recorded path
python verify/measure_clip_fps.py /path/to/clip.mp4

# Live path (RTSP), 25 s
ffmpeg -rtsp_transport tcp -i "rtsp://<user>:<pass>@<cam>:554/h264Preview_01_main" \
       -t 25 -c copy /tmp/rtsp_main.mp4
python verify/measure_clip_fps.py /tmp/rtsp_main.mp4
```

Recorded and RTSP agree to within ~0.05 fps in every paired sample, so either
path is valid evidence. **The container header is not.**

---

*Consolidated from `FPS_BOTTLENECK.md`, `FPS_DEMO_RESULTS.md`, `FPS_MAIN_BIND.md`,
`FRAME_PUMP.md` and the sensor-sync analysis, none of which survive as separate
documents. Cross-references: `FPS_STAGE_EVIDENCE.md` (all per-stage readings),
`ISF_PARAM_MAP.md` §1 (the ioctl table), `FIRMWARE_PATCH_NOTES.md` §15
(superseded by §3 and F7 here), `APP_REPLACEMENT_DESIGN.md` (§6),
`BRICK_POSTMORTEM.md` (why sensor `.ko` edits are the highest-risk class of
change on this device).*
