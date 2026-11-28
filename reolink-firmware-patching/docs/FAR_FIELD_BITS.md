# Far-field detail: where the main stream's bits go

**Status (2026-11-27): baseline measured on real game footage. Nothing in this
document has been run on the camera yet.** The camera was disconnected for this
work. Everything under "Measured" comes from archived game recordings and is
reproducible with `verify/stream_quality.py`. Everything under "To test on the
camera" is a plan.

The complaint: the far side of the pitch is blocky. The far ball is 3 to 8
pixels across, so detail lost there is the detail the detector and the viewer
need most.

## Measured

Two full 5-minute main-stream segments from away games, 7680x2160 H.265,
recorded on the 20480 kbps build:

| | Lakefront 2026-06-10 | Niagara Falls 2026-07-12 |
|---|---|---|
| video bitrate | 20.71 Mbps | 20.57 Mbps |
| frame rate delivered | 19.75 fps | 19.62 fps |
| keyframe interval | every 20 frames (1 s) | every 20 frames (1 s) |
| mean keyframe | 894 KB | 1023 KB |
| mean other frame | 90.9 KB | 84.1 KB |
| **keyframes' share of all bits** | **34.1 %** (7.06 Mbps) | **39.0 %** (8.02 Mbps) |
| left for the other 19 frames | 13.65 Mbps | 12.55 Mbps |

Three things follow.

**1. The stream is at its cap during games.** All nineteen full-length
Lakefront segments are 777 to 785 MB. So no setting can add bits; it can only move
them. This confirms the premise of `ENCODER_ROI_QP.md` on game footage; the
14 to 16 Mbps figures in `FIRMWARE_PATCH_NOTES.md` were indoor test scenes.

**2. A third of the bits buy keyframes.** One frame in twenty takes 34 to 39 %
of the stream. A keyframe is ten times the size of the frames between it.

**3. The far field pays for it once a second.** Per-region metrics on the
Lakefront segment, 100 frames from 2:00 in (regions inside the confirmed field
polygon, far touchline at y of about 160 to 330, near at 1060 to 1470):

| region | frames | blk8 | blk16 | blk32 | blk64 | detail |
|---|---|---|---|---|---|---|
| far (2496,180 2816x256) | all | 1.049 | 1.078 | 1.063 | 1.020 | 7.77 |
| | keyframes | 1.059 | 1.090 | 1.118 | 1.085 | 8.70 |
| | last of cycle | 1.049 | 1.076 | 1.047 | 1.012 | 7.36 |
| near (2496,1100 2560x256) | all | 1.025 | 1.033 | 1.054 | 1.053 | 32.08 |
| | keyframes | 1.068 | 1.122 | 1.254 | 1.249 | 34.01 |
| | last of cycle | 1.023 | 1.024 | 1.022 | 1.027 | 32.00 |

`blkN` is the blockiness index on the N-pixel grid (1.0 means no coding
structure). `detail` is mean absolute Laplacian.

- Far-field detail falls **15 %** between a keyframe and the frame before the
  next one (8.70 to 7.36), then snaps back. The near field loses 6 %. This is a
  1 Hz pulse in exactly the region that can least afford it.
- On the 8 and 16 pixel grids the far field carries about twice the near
  field's excess (0.049 and 0.078 against 0.025 and 0.033).
- The far band holds a quarter of the near band's detail, so it is cheap: bits
  moved there go a long way.

## How much bitrate would be enough (estimated 2026-11-27)

**The stream runs at QP 34 to 35.** Read from the slice headers of a 10 s
Lakefront clip (`ffmpeg -bsf:v trace_headers`): keyframes QP 34 to 35, other
frames QP 33 to 35, mean 34.2. The rate controller's allowed range is 25 to 51,
so the stream is nowhere near its floor. The headers also show Main profile,
level 5.0, 64-pixel CTBs, and per-block QP deltas enabled down to 16x16. This is
the frame-level QP; the per-block values after AQ cannot be read with any tool
we have.

**QP 35 is coarse for a far ball.** The quantiser step roughly doubles every 6
QP: about 36 at QP 35, about 18 at QP 29. A ball 3 to 8 pixels wide is a small,
low-contrast blob, and a blob whose transform coefficients fall under the step
is rounded away. **ESTIMATE, not measured:** the far field needs about QP 28 to
29, six steps lower than it gets.

**What that costs, by proxy.** Two seconds of the Lakefront clip, cut into four
full-width bands and re-encoded with x265 at fixed QP 35 and QP 29 (same GOP, no
B-frames, AQ off):

| band (rows) | share of frame | share of bits at QP 35 | cost of QP 35 → 29 |
|---|---|---|---|
| above the far touchline (0–159) | 7 % | 2.5 % | ×1.54 |
| far band (160–479) | 15 % | **4.4 %** | ×1.67 |
| rest of the pitch (480–1471) | 46 % | 45.1 % | ×1.53 |
| foreground below the near touchline (1472–2159) | 32 % | **48.0 %** | ×1.48 |
| whole frame | | | ×1.51 |

- **Uniformly, the answer is 31 to 41 Mbps.** ×1.51 on today's 20.7 Mbps is
  31 Mbps, and that is a floor: the source was already quantised at QP 35, so
  the detail a clean source would add at QP 29 is missing from the estimate.
  The usual rule (bitrate doubles per 6 QP) gives 41 Mbps.
- **For the far band alone, it is under 1 Mbps.** That band takes 4.4 % of the
  bits. Six QP lower costs it ×1.67, about 0.7 Mbps on the proxy, perhaps
  1.5 Mbps on a clean source.
- **Half the stream is spent below the near touchline.** At this field the
  bottom third of the picture is sideline and spectators, and it takes 48 % of
  the bits.

So raising the cap is the expensive way to get there: of every extra bit a
higher uniform bitrate buys, about 4 % lands on the far band. A window on the
far band (lever 2) asks the encoder for the same six QP at a twentieth of the
cost, and a 2 s keyframe interval (lever 1) frees several times what it needs.

Limits of this: one 2 s clip from one game; x265 is not the camera's encoder;
the proxy ignores AQ, which may already favour the flat far band.

## Putting the bits on the field (2026-11-27)

Goal as Mark stated it: most of the stream should go to the field polygon; the
sidelines and sky may look as bad as they like.

**Today the field gets about a third.** Same Lakefront clip, same x265 proxy,
with everything outside the confirmed field polygon blacked out and then the
reverse: the polygon is 35 % of the frame and takes 35 to 37 % of the bits. About
65 % of the stream, some 13 Mbps, is spent outside it.

| region | share of frame | share of bits |
|---|---|---|
| field polygon | 35 % | ~36 % |
| corners: inside the polygon's rows (160–1471) but outside it | 26 % | ~13 % |
| outside those rows (mostly foreground) | 39 % | ~51 % |

**What each mechanism can do with that.** Estimates, assuming the starved area
drops to a third of its present cost:

| mechanism | shape | field's share of bits | status |
|---|---|---|---|
| one ROI window on the field's rows | rectangle | ~36 % → ~60 %, about ×1.7 | proven on hardware (builds 4922 and 4923, EXP-ENC-08/09): the window moves bits |
| ten ROI windows | staircase around the polygon | → ~80 %, about ×2.2 | handler patched (`ROIQP_MULTIWIN=1`, build 4923, `ENCODER_ROI_QP.md` section 4b); **flashed 2026-11-28 and working**: windows are independent, a bad index is refused, and where two overlap the lower index wins (EXP-ENC-09). The daemon drives one window in 4923; ten from `winN=` lines in build 4924 (staged, not flashed). `fieldroi` still writes one rectangle |
| per-block QP map | the polygon itself | slightly better than ten windows | only reachable from inside `device` |
| privacy masks (`SetMask`) | black rectangles | masked area costs almost nothing | no flash; the camera reports the `mask` ability. Count and timing unverified. **The masked picture is gone, not degraded** |

A single rectangle captures most of the gain because most of the waste is one
full-width band of foreground. ×1.7 on the field is about four QP; ×2.2 is about
seven.

**The window has to be set per game.** Across the 23 Reolink games of 2026 with
a field polygon, the far touchline's top row ranges from 110 to 543 and the near
touchline's bottom row from 1283 to 2154. In four games the field runs to the
bottom edge of the frame. No fixed window fits; the horizontal extent barely
varies, so a per-game config is two numbers (top row, bottom row). How those
two numbers reach a camera that records unattended at the field is not solved.

## Levers, in the order to try them

### 1. Keyframe interval (no flash)

`GetEnc` reports `gop` with range 1 to 2 for the main stream, as a multiple of
the frame rate. The daily-driver setting in `PATCHING_GUIDE.md` is 1x, and the
footage confirms it: a keyframe every 20 frames.

Setting `gop=2` halves the number of keyframes. If keyframe size stays the same,
that frees 3.5 Mbps (Lakefront) to 4.0 Mbps (Niagara Falls) for the frames
between, **26 to 32 % more** than they get now. This is an upper bound: the rate
controller is free to spend some of it on bigger keyframes instead.

Costs, all bounded:

- Power-cut recovery loses up to one keyframe interval: 2 s instead of 1 s.
- Seeking decodes up to 40 frames instead of 20.
- The 1 Hz pulse becomes a 0.5 Hz pulse.

**UNVERIFIED:** `ReolinkCamera.apply_optimal_settings` sends `gop=40`, which is
outside the 1 to 2 range the camera reports. What the camera does with that
(reject the whole `SetEnc`, clamp it, or ignore the field) has to be read off
the camera. If it rejects, the bitrate and frame-rate changes in the same call
are being lost too.

Going beyond 2 s would need the range lifted in `router`, the same kind of patch
as the bitrate cap. Not located. Not worth looking for until 2x is measured.

### 1b. The encoder's own rate-control commands (no flash) — found 2026-11-27

The debug node that carries `setroi` also carries rate control. From the help
table in `kflow_videoenc.ko` (file offset 0x380d0 onward, read from the
extracted module):

```
setcbr   PathId Enable StaticTime ByteRate FrameRate GOP InitIQp MinIQp MaxIQp
         InitPQp MinPQp MaxPQp IPWeight            => setcbr 0 1 4 524288 30 15 26 10 40 26 10 40 0
setvbr   ...same, plus ChangePos
setevbr  ...plus KeyPPeriod, KeyPWeight, MotionAQStrength and four more
setfixqp PathId Enable IFixQP PFixQP
setaq    PathId Enable I_Str P_Str MaxAq MinAq
```

If these work, one shell session answers three things with no flash and no
rewrite: whether GOP can go past the API's 2 s, whether the QP floor of 25 can
be lowered, and whether the kernel accepts a byte rate above the 20.5 Mbps the
API refuses (which would place that ceiling in the app, not the encoder).

**UNVERIFIED, all of it.** Nobody has written one of these commands on this
camera. Two specific risks: `setroi` on the same node has an argument-width bug
(`ENCODER_ROI_QP.md` section 4) and these may share it; and `device` may
re-apply its own settings over ours. Read the kernel's echo in `dmesg` and
`/proc/hdal/venc/info` after each write, exactly as for `setroi`.

### 2. ROI window on the far band (flash)

`ENCODER_ROI_QP.md`. One rectangle with a QP delta. Because the far band is
low-detail, lowering its QP is cheap. Build `4922` is staged; see
`builds/BUILD_LOG.md`.

The rectangle is per-field. The example config assumes the pitch at y 760 to
1660; at Lakefront the far touchline is at y 160 to 330. The pak therefore bakes
a **disabled** default and does nothing until a `roi.conf` is on the card.

### 3. `nr3d` and global AQ strength (no flash)

Open from DECISIONS 2026-06-15: `nr3d` off keeps far more fine texture but adds
noise that costs bits. Worth re-running once levers 1 and 2 have freed some.
`/proc/kdrv_vdocdc/aq` changes global AQ strength live.

### 4. The 20.5 Mbps ceiling itself — traced 2026-11-27, not yet patched

`SetEnc` accepts 20480 kbps and rejects 21504. The camera works in kbps x 1024,
so those are 20,971,520 and 22,020,096 bps. **`device` provisions the main
encode path with a maximum of exactly 20,971,520 (`0x1400000`)**, which is why
20480 is the last value that works: it equals the provisioned maximum.

Checked with objdump against `app_extracted/device` (non-PIE, file offset =
VMA - 0x400000):

```
4983ac  ldr  w2, [x19, #116]       ; which path
4983b0  mov  w3, #0x800000         ; 8,388,608   (the other paths)
4983b8  mov  w0, #0x1400000        ; 20,971,520  (main path)
4983c4  csel w0, w0, w3, eq
4983dc  stp  w0, w3, [sp, #68]     ; into the config struct
49840c  mov  w1, #0x2              ; param 2 = PATH_CONFIG
498414  bl   5cd150                ; hd_videoenc_set
```

A second load of the same constant at `48c098` is stored to `obj+0x2894`, which
the trace found feeding the bitstream-buffer sizing.

**The comparison that rejects** is in the HDAL library linked into `device`,
not in the kernel. A validator at `5c16b0` (called once, from `5c9cd0`, before
the encode path starts) compares each rate-control bitrate with the path's
provisioned bitrate in the per-port shadow config (`port * 0xab8`):

```
5c1a48  ldr  w3, [x5, #28]         ; PATH_CONFIG bitrate (the provisioned max)
5c1a4c  ldr  w4, [x5, #228]        ; the CBR bitrate being asked for
5c1a50  cmp  w4, w3
5c1a54  b.ls 5c1890                ; asked <= provisioned: fine
5c1a68  adrp/add x2 -> "HD_VIDEOENC_PARAM_PATH_CONFIG: path config bitrate(%lu)
                        must be >= HD_H26XENC_CBR: bitrate(%lu)."
5c1a74  mov  w0, #-1
```

`b.ls` is "lower or same", which is why 20480 kbps passes by equality and 21504
fails. The VBR, EVBR and CVBR arms beside it are identical.

**The kernel does not check.** `NMR_VdoEnc_SetCBR` in `kflow_videoenc.ko`
(`.text` 0x10040) stores the byte rate into the per-path object and forwards
it; its only branches are a NULL guard, a log level, a changed-value test and
the path number. The same holds for the VBR / EVBR / CVBR setters, and the
provisioning setter validates resolution and memory range only. (Traced by a
subagent; the CBR setter re-read by me.)

**What the provisioned number sizes.** The kernel sizes the path's output
bitstream ring from it, as byte rate times a buffer length in milliseconds. The
6000 stored next to the bitrate at `4983cc` is very likely that length, giving
20,971,520 / 8 x 6 s = 15.7 MB today (INFERRED: the field name is not
confirmed). The ring comes out of the HDAL media pool, which the device tree
sets at 768 MiB (`hdal-memory/media`, 0x30000000 at 0x10000000). When a frame
does not fit, the kernel drops it and logs `size not enough, drop frame`; it
does not overrun.

**The other loads of `0x1400000` are not bitrates.** The five at `55b2a0`…
`55b4cc` sit in one function that fills a table of Q20 fixed-point constants
(-80.0, -62.0, -40.0, -20.0, 8.0, 30.0, 48.0, and 20.0 scaled by -0.5); it is
some gain curve. `obj+0x2894` is read in exactly two places. As a bitrate the
number lives only at `4983b8` and `48c098`.

So the map is: **one number, provisioned at two instructions in `device`,
enforced by one comparison in `device`, sizing one buffer in the kernel.**

Two ways to go past it:

1. **No flash: the kernel's own `setcbr`.** The debug command parses cleanly
   (thirteen `%d` into thirteen 4-byte slots; unlike `setroi` there is no
   overlap) and reaches the unchecked kernel setter, so a byte rate above
   2,621,440 will be accepted. The ring stays sized for 20.5 Mbps, which is
   about 4 s of stream at 30 Mbps instead of 6 s. Unknowns: whether `device`
   re-applies its own value, and whether the recorder keeps up. This is the
   first thing to try, because it answers "does more bitrate help the far
   field" before anything is flashed:
   `echo vdoenc setcbr 0 1 4 3276800 20 20 35 25 51 35 25 51 0 > /proc/hdal/venc/cmd`
   (3,276,800 bytes/s is 26.2 Mbps; take StaticTime, GOP and the QP fields from
   what `/proc/hdal/venc/info` shows at the time rather than from this line).
2. **Flash: raise the provisioned value.** Change `00 28 a0 52` at file offsets
   `0x983b8` and `0x8c098` in `device` (to `00 40 a0 52` for 32768 kbps), and
   the API cap in `router` (`0x6351c`, the existing patch site) to match. Both
   `device` sites, so the ring is sized for what the path is allowed. **Not
   built.** It touches `device`, the process whose death takes the camera off
   the network.

EXP-ENC-02 says what either is worth: about 4 % of any extra bitrate lands on
the far band.

## Finding the field on the camera (2026-11-27)

The ROI window must follow the field, and the camera records unattended, so the
field has to be found on the camera. `fieldroi/` does that with the project's
existing field-outline model (`training/field_outline`, ResNet18, 10 keypoints),
rewritten as one dependency-free C file and built static for aarch64.

**The model is accurate enough for rows, with padding.** `field_outline_v2` on
five frames from each 2026 Reolink game, against the 14 human-verified polygons
(the model has probably seen most of these games in training, so this flatters
it):

| edge | median error | range | worst single frame |
|---|---|---|---|
| far line (top row) | +19 rows, i.e. too low | -47 to +152 | +179 |
| near line (bottom row) | -93 rows, i.e. too high | -493 to +46 | -642 |

- **Far edge: pad 200 rows upward and all 14 are covered.** Too low is the
  dangerous direction, and padding upward is nearly free (that band is ~4 % of
  the bits).
- **Near edge: weak when the pitch runs to the bottom of the frame.** The four
  worst cases (-184 to -493) are games where the true near edge is row 2154.
  There the model stops short, so the nearest strip of pitch would sit outside
  the window. Near objects are large, so this is the less harmful miss, but it
  is a miss.
- Frame-to-frame scatter is small (far error varies by ~15 rows within a game),
  so the error is a per-scene bias; averaging more frames will not fix it.
- On four July games with no polygon to score against, the overlays look right
  on two inspected by eye (Chili Vortex, Niagara Falls; far line within ~80
  rows).

**The C port matches the model.** `verify/test_fieldroi.sh`: keypoints within
0.25 px of onnxruntime on four games, and the aarch64 build under qemu gives the
same numbers as the host build.

**It refuses blank frames, but only because of a second check.** The network
always answers: shown a uniformly grey or black frame it returns a tidy outline
with ordinary scores. So the tool also requires the claimed field rows to be
grass-coloured (87 to 93 % on real games, 0 % on grey or black).

**Not done, and not known:**

| open | why it matters |
|---|---|
| Time on the camera | 2.3 to 4.1 s single-threaded on a desktop x86. The camera's Cortex-A53 will be several times slower; qemu timing says nothing. Must be measured |
| Memory on the camera | ~20 MB of working memory plus 45 MB of weights mapped read-only from the SD card. Free RAM while `device` runs has not been read |
| Getting a frame | The tool reads the camera's snapshot format: three archived `cmd=Snap` files are baseline 4:2:0 JPEG, 7680x2160, and it decodes them at one-eighth scale from the DC coefficients alone (under 1 MB instead of 50; mean error under 1.4 grey levels against a full decode). Across 27 games the 1/8-scale input moves the far row by a median of 1 (worst 95) and the near row by 15 (worst 141), nearly always outward. **Not wired up:** nothing on the camera fetches the snapshot yet, and whether `cmd=Snap` is safe to call while recording is unmeasured. busybox `wget` is on the camera and `S99_NetState` already uses it against the local API |
| When to run | Not designed. Once at recording start is the obvious choice; the camera may still be being aimed then |
| A window that works at all | Everything here is moot until the ROI window is proven on hardware (lever 2; phase D below) |

## To test on the camera

Everything above is offline work. This is the order for the first session with
the camera connected. Stop at the first failure; each phase leaves the camera
in a state the next can start from.

**Decided 2026-11-27 (Mark: "you decide"):**

- **No `device` bitrate patch is built yet.** It is the riskiest change on the
  list (it touches the process that owns the network) for the smallest expected
  gain (about 4 % of extra bitrate reaches the far band). Phase B answers
  whether more bitrate helps at all, with no flash. Build it only if it does.
- **No SD-card auto-run hook in 4922.** The session runs `fieldroi` by hand from
  the root shell. An auto-run hook is for unattended games and goes in a later
  build, once the detector has been timed and proven on the camera.
- **Flash order: 4922, then 4923.** One string change first, code second.

**The scene must be rate-limited, or every result is a false negative.** A
static indoor scene encodes at about 1.2 Mbps against the 20 Mbps cap
(`BUILD_LOG.md`, rollover measurements). There the encoder already sits at its
QP floor with bits to spare; moving bits around changes nothing. Point the
camera at something busy: game footage looping on a screen that fills as much
of the frame as possible, or outdoors at foliage. If the measured bitrate is
still under the cap, lower `bitRate` with `SetEnc` until the cap binds.

Local setup already done: `camera.env` (git-ignored; IP is the `camsh.py`
default and is confirmed in A1), the static aarch64 `fieldroi` and its weights
in `builds/out/fieldroi/`, and paks 4922 and 4923 in the firmware folder.

### A. Read-only baseline (nothing on the camera changes)

1. **Find it and identify the build.** `http://<cam>/downloadfile/soccercam/build.txt`
   must name the 4921 pak. Root shell on tcp/2323 answers (`runtime/camsh.py`).
2. **Save the state to restore later:** `GetEnc`, `GetIsp`, and
   `cat /proc/hdal/venc/info`.
3. **Read what the offline work could not:**
   - `venc/info`: out 0 is the 7680x2160 H.265 path; its `PATH CONFIG` row
     shows `bitrate 20971520` and the buffer length (`enc_ms`); the ROI table
     is empty; the live GOP and QP range.
   - `cat /proc/meminfo` and `cat /proc/hdal/comm/info`: free Linux memory and
     free video pool.
   - `GetEnc` after noting what `gop` the API reports (the code sends 40; the
     range is 1 to 2).
4. **Lock exposure** (`runtime/set_exposure.sh manual 110 20`), **capture 30 s**
   (`verify/stream_quality.py capture`), and confirm the bitrate is at the cap.
   This also confirms the live stream is the same encode path as the recorder
   (`venc/info` lists one 7680x2160 path).

### B. No-flash levers (all revert on reboot or by re-sending `SetEnc`)

5. **Keyframe interval through the API.** `gop=1`, capture 60 s; `gop=2`,
   capture 60 s; `stream_quality.py compare`. Expect the keyframe gap to go 20
   to 40, keyframe share to drop, far-region detail late in the cycle to rise.
6. **`setroi` negative control** (`ENCODER_ROI_QP.md` 7.1): on 4921 the echo
   must read `QP = 0, QPMode = 0`. The harness already shows this under qemu;
   this is the same line from the real kernel. Then disable it again.
7. **`setcbr`, one change at a time**, starting from exactly the values
   `venc/info` shows and reading `venc/info` and `dmesg` back after each:
   GOP beyond 2 s; then minimum QP below 25; then byte rate above 2,621,440
   (start at 3,276,800 = 26.2 Mbps). Capture 60 s at each setting that takes.
   Watch `dmesg` for `size not enough, drop frame` and check whether `device`
   puts its own values back.
8. **Global AQ strength** (`setaq`, small values only) if time allows.

### C. The field detector, by hand (no flash)

9. **Put it on the card.** Serve `builds/out/fieldroi/` from this machine and
   fetch both files with the camera's own `wget` into `/mnt/sda/soccercam/bin/`;
   check the md5 of each.
10. **Does a snapshot cost frames?** Capture the stream for 30 s while taking
    `cmd=Snap` three times; `verify/measure_clip_fps.py` on the capture.
11. **Run it** on a snapshot under `time`, at `nice 19`, watching `/proc/meminfo`.
    Record seconds and peak memory. Pointed at a screen or a yard it should
    either find the grass or refuse; pointed indoors it must refuse.

### D. Flash 4922 (format-string fix, daemon disabled by default)

12. `verify/test_setroi_patch.sh <4922 pak> fix`, then `flash/flash_pak.py`.
    Confirm `build.txt` names 4922, the stream still encodes, the ROI table is
    still empty, and `roi.log` says the encoder was left untouched.
13. **ROI readback** (7.3): `QP = -6, QPMode = 1` in `dmesg`, and a row in the
    ROI table.
14. **ROI A/B** through `roi.conf`: off, `qp=-6`, `qp=0`, and **`qp=+10` as a
    positive control**. A positive delta makes the rectangle visibly worse even
    if the encoder is not rate-limited, so it proves the window reaches the
    hardware and settles pixels against CTUs independently of the scene.
    `roi_qp_heatmap.py` for the picture, `stream_quality.py compare` for the
    numbers. Then a strong window on the "field" rows of the test scene.
15. **End to end:** `fieldroi snapshot.jpg > /mnt/sda/soccercam/roi.conf` and
    watch the daemon apply it within 30 s.

### E. Flash 4923 (ten windows) — only if D passed

16. `verify/test_setroi_patch.sh <4923 pak> multiwin`, flash, same confirmations
    as step 12.
17. Two windows on different indexes: both rows present in the ROI table. Then
    what the harness cannot show: overlapping windows, and which wins.
18. An out-of-range index is refused and the camera keeps running.

### After

19. Restore the saved `GetEnc` / exposure state unless a new setting is being
    kept on purpose. Record results in `training/docs/EXPERIMENTS.md` and decide:
    `gop` default, whether the `device` bitrate patch is worth building, and the
    auto-run hook.

A full-game soak (`ENCODER_ROI_QP.md` 7.6) needs a game.

## Would replacing the firmware remove these limits? (assessed 2026-11-27)

Assessed offline by reading the docs and the extracted firmware; spot-checked
against both. Short version: the limits that are policy live in `device` and
`router`, and the limits that are not policy live in binary kernel modules or
the sensor, which any replacement would have to keep.

- **The SoC is NT98539 (Novatek platform NS02302).** `1_fdt.bin` carries
  `novatek,ns02302`, U-Boot prints `CHIP[NS02302]`, the loader names
  `FW98539A.BIN`, and `device` was built from `cbb/lib/NT98539/`. The "NT9856x"
  in `FIRMWARE_PATCH_NOTES.md` and "NT98530" in `ISF_PARAM_MAP.md` are wrong for
  this unit.
- **Open firmware / own kernel: no path.** OpenIPC's Novatek work lists this
  platform as unsupported ("NS02201/02301/02302/02401 and NA51102 have no
  loader, board trees or QEMU machine to work from", OpenIPC/u-boot PR 99), and
  where it does support Novatek it still runs the vendor's binary media modules.
  It would unlock nothing on the list above.
- **Replacing `device` with our own app: possible, large, and bounded.** It
  would give full encoder control and a recorder that survives power cuts. It
  would not give 30 fps at 7680x2160: `FPS_CEILING.md` puts the sensor readout
  wall at about 26.8 fps. `APP_REPLACEMENT_DESIGN.md` understates the work in
  three ways that later docs establish: HDAL is statically linked into `device`
  and keeps shadow state, so it must be reimplemented rather than replayed;
  `device` also owns the IP address (`BRICK_POSTMORTEM.md`), so a dead app is a
  camera off the network, not "a camera that answers HTTP with no stream"; and
  its "very low risk" first stage is what caused the 4917 outage.
- **Before any of that, lever 1b.** If the debug node's rate-control commands
  work, most of what an app replacement would buy for the encoder is available
  today from a shell.

## Not known

| question | how it gets answered |
|---|---|
| Does `gop=2` survive a reboot and a day/night switch | step 5, then reboot |
| Does the rate controller give the freed bits to the frames between, or to bigger keyframes | step 5 |
| What the camera does with `gop=40` from `apply_optimal_settings` | `GetEnc` after an `apply_optimal_settings` run |
| Whether the live RTSP stream and the recorder share one encode path | step 3: `venc/info` lists the paths and their consumers |
| Everything in `ENCODER_ROI_QP.md` section 8 | steps 6 and 12 to 18 |
| Whether a `setcbr` value survives, and whether more bitrate helps the far field | step 7 |
| How long `fieldroi` takes on the camera, and whether a snapshot costs frames | steps 10 and 11 |
